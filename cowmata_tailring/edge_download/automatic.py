"""Bounded, paginated downloads without a manually maintained cow/device list.

The server cursor identifies a scan position, not a sensor batch UID. Every
cycle starts a new snapshot, so late uploads with old collection times remain
discoverable. Only completed files are cached; no unfinished page is skipped.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .core import (
    CATEGORIES,
    CHINA,
    Cancelled,
    Client,
    DownloadError,
    Job,
    Result,
    checked_path,
    segment,
    validate_payload,
    validate_url,
)
from .deduplication import DuplicateCatalog, RootSyncLock
from .ledger import LedgerPlan, load_plan
from .ledger_organization import organize

PAGE_SIZE = 250
MIN_FREE_BYTES = 64 * 1024 * 1024
IDENTITY_FIELDS = ('cow_id', 'animal_number', 'animalNumber')


class _StopCycle(DownloadError):
    """A local prerequisite failed; do not repeat the failure for every batch."""


@dataclass(frozen=True)
class CowCode:
    original: str
    cow_number: str
    ear_tag: str
    status: str


def split_cow_code(value) -> CowCode:
    """Preserve the database code; derive names without guessing an identity."""
    original = '' if value is None else str(value)
    code = original.strip()
    if not code:
        return CowCode(original, '', '', 'pending_review')
    if re.fullmatch(r'[0-9A-Fa-f]{12}', code):
        return CowCode(original, '', '', 'pending_review')
    match = re.fullmatch(r'([0-9]{1,5})([A-Za-z][A-Za-z0-9]*)?', code)
    if not match:
        return CowCode(original, '', '', 'pending_review')
    number, tag = match.groups()
    return CowCode(original, number, (tag or '').upper(),
                   'parsed' if tag else 'missing_tag')


def _check(cancel):
    if cancel.is_set():
        raise Cancelled()


def _check_disk(root, required=MIN_FREE_BYTES):
    if shutil.disk_usage(root).free < required:
        raise _StopCycle('保存磁盘空间不足，已停止本轮自动下载；请腾出至少 64 MiB 可用空间后重试')


def _integer(value, field, minimum=0):
    if isinstance(value, bool):
        raise DownloadError(f'自动清单 {field} 必须为整数')
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r'[0-9]{1,32}', value):
        result = int(value)
    else:
        raise DownloadError(f'自动清单 {field} 必须为整数')
    if result < minimum:
        raise DownloadError(f'自动清单 {field} 超出允许范围')
    return result


def _device(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9A-Fa-f]{12}', value.strip()):
        raise DownloadError('自动清单缺少有效的 12 位设备编号')
    return value.strip().upper()


def _identity(data):
    values = []
    for field in IDENTITY_FIELDS:
        value = data.get(field)
        if value in (None, ''):
            continue
        if isinstance(value, bool) or not isinstance(value, str | int):
            raise DownloadError(f'历史牛号字段 {field} 格式错误')
        text = str(value).strip()
        if text:
            values.append(text)
    if len({value.casefold() for value in values}) > 1:
        raise DownloadError('同一批记录的历史牛号字段互相冲突')
    return values[0] if values else ''


def _validate_job(job):
    validate_url(job.base_url)
    if urlsplit(job.base_url).query:
        raise DownloadError('下载服务器应填写接口根地址，不含查询参数')
    if not job.farm.is_absolute() or not job.farm.is_dir():
        raise DownloadError('自动下载保存目录不存在或不是绝对路径')
    if job.category not in CATEGORIES:
        raise DownloadError('自动下载类别无效')
    if tuple(job.kinds) != ('motion',):
        raise DownloadError('当前一键下载接口仅提供 Motion；PPG 和温度需使用设备下载')
    if (job.start.tzinfo is None or job.end.tzinfo is None or job.start >= job.end
            or job.start.microsecond or job.end.microsecond):
        raise DownloadError('自动下载范围必须为带时区的整秒时间，且结束晚于开始')


def _page(client, job, cursor, snapshot):
    params = dict(cursor=cursor, limit=PAGE_SIZE,
                  startTime=job.start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),
                  endTime=job.end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'))
    if snapshot is not None:
        params['snapshotUid'] = snapshot
    try:
        data = client.envelope('/device/data/records', params)
    except DownloadError as exc:
        if 'HTTP 404' in str(exc):
            raise DownloadError('服务器尚未支持一键下载，请升级 3090 下载接口（/device/data/records）') from exc
        raise
    if not isinstance(data, dict):
        raise DownloadError('自动清单必须是对象')
    records = data.get('records')
    if not isinstance(records, list) or len(records) > PAGE_SIZE:
        raise DownloadError('自动清单 records 格式或分页大小错误')
    next_cursor = _integer(data.get('nextCursor'), 'nextCursor')
    current_snapshot = _integer(data.get('snapshotUid'), 'snapshotUid')
    scanned = _integer(data.get('scanned'), 'scanned')
    more = data.get('hasMore')
    if not isinstance(more, bool):
        raise DownloadError('自动清单 hasMore 必须为布尔值')
    if snapshot is not None and snapshot != current_snapshot:
        raise DownloadError('自动清单 snapshotUid 在分页期间发生变化')
    if not cursor <= next_cursor <= current_snapshot:
        raise DownloadError('自动清单游标越界或发生倒退')
    if more and (next_cursor <= cursor or next_cursor >= current_snapshot):
        raise DownloadError('自动清单游标未前进或 hasMore 与快照矛盾')
    if not more and next_cursor < current_snapshot:
        raise DownloadError('自动清单末页尚未扫描至快照终点，拒绝将不完整清单当作完成')
    if not len(records) <= scanned <= PAGE_SIZE or (scanned > 0 and next_cursor == cursor):
        raise DownloadError('自动清单 scanned 与记录或游标不一致')
    if more and scanned == 0:
        raise DownloadError('自动清单未扫描任何记录却声明后续分页')
    checked = []
    for item in records:
        if not isinstance(item, dict):
            raise DownloadError('自动清单项必须是对象')
        uid = _integer(item.get('uid'), 'uid', 1)
        device = _device(item.get('device'))
        stamp = _integer(item.get('create_time'), 'create_time', 1)
        if uid != stamp:
            raise DownloadError('自动清单批次 UID 与采集起始毫秒 create_time 不一致')
        try:
            when = datetime.fromtimestamp(stamp / 1000, CHINA)
        except (ValueError, OSError, OverflowError) as exc:
            raise DownloadError('自动清单采集时间超出允许范围') from exc
        if not job.start <= when < job.end:
            raise DownloadError('自动清单采集时间不在请求范围内')
        revision = item.get('revision')
        if isinstance(revision, bool) or not isinstance(revision, str | int) or str(revision) == '':
            raise DownloadError('自动清单缺少有效 revision')
        downloadable = item.get('downloadable')
        archive_state = item.get('archive_state')
        if not isinstance(downloadable, bool) or not isinstance(archive_state, str):
            raise DownloadError('自动清单缺少有效归档状态')
        checked.append(dict(uid=uid, device=device, cow_id=_identity(item), create_time=stamp,
                            revision=str(revision), downloadable=downloadable,
                            archive_state=archive_state))
    return checked, next_cursor, current_snapshot, more, scanned


def _file_sha256(path, cancel):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            _check(cancel)
            block = stream.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def _save_record(job, data, cancel):
    """Keep the payload and original identity fields; add derived display fields."""
    validate_payload(data, 'motion')
    code = split_cow_code(_identity(data))
    exported = dict(data)
    exported.update(cow_number=code.cow_number, ear_tag=code.ear_tag, cow_code_status=code.status)
    device = _device(exported.get('device'))
    name = (f'{device}-{code.cow_number}-{code.ear_tag or "未提供"}'
            if code.cow_number else f'{device}-待核对')
    stamp = datetime.fromtimestamp(_integer(exported.get('create_time'), 'create_time', 1) / 1000, CHINA)
    folder = checked_path(job.farm, Path(job.category) / 'Motion' / stamp.strftime('%Y-%m-%d') / segment(name))
    folder.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(exported, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    digest = hashlib.sha256(encoded).hexdigest()
    stem = stamp.strftime('%Y-%m-%d_%H-%M-%S')
    candidates = [checked_path(job.farm, folder / (stem + suffix + '.json'))
                  for suffix in ('', '_' + digest[:16], '_' + digest)]
    for path in candidates:
        _check(cancel)
        if path.is_file() and _file_sha256(path, cancel) == digest:
            return path, False, digest
    _check_disk(job.farm, len(encoded) + MIN_FREE_BYTES)
    fd, temporary = tempfile.mkstemp(prefix='.edge-auto-', suffix='.part', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        for path in candidates:
            _check(cancel)
            try:
                if os.name == 'nt':
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
                return path, True, digest
            except FileExistsError:
                if path.is_file() and _file_sha256(path, cancel) == digest:
                    return path, False, digest
        raise DownloadError('自动下载文件名冲突，现有文件已保留')
    finally:
        Path(temporary).unlink(missing_ok=True)


def run_automatic_job(job: Job, cancel, log=lambda message: None,
                      progress=lambda done, total: None, client_factory=Client) -> Result:
    _validate_job(job)
    try:
        with RootSyncLock(job.farm, cancel):
            return _run_automatic_locked(job, cancel, log, progress, client_factory)
    except Cancelled:
        return Result(canceled=True)


def _run_automatic_locked(job: Job, cancel, log=lambda message: None,
                          progress=lambda done, total: None, client_factory=Client) -> Result:
    """Run one complete server scan. The GUI owns repetition and its interval.

    No user-entered identity is used. Page errors stop this scan; individual
    failures remain uncached and are retried during the next snapshot. The
    source scan cursor is never persisted as a successful-download watermark.
    """
    _validate_job(job)
    result = Result()
    if cancel.is_set():
        result.canceled = True
        return result
    client = client_factory(job.base_url, cancel, log)
    state = checked_path(job.farm, '.edge-download')
    state.mkdir(exist_ok=True)
    db = sqlite3.connect(checked_path(job.farm, state / 'automatic.sqlite3'), timeout=10, isolation_level=None)
    processed = discovered = 0
    try:
        db.execute('CREATE TABLE IF NOT EXISTS completed ('
                   'key TEXT PRIMARY KEY, revision TEXT NOT NULL, path TEXT NOT NULL, '
                   'sha256 TEXT NOT NULL, identity TEXT NOT NULL, create_time INTEGER NOT NULL)')
        plan = load_plan(job.ledger_directory, state, log) if job.ledger_directory else LedgerPlan()
        organize(job.farm, plan, db, cancel, log)
        duplicates = DuplicateCatalog(job, db, cancel, log)
        catalogs = {job.category: duplicates}
        log('检查当前目录内已下载的数据和重复副本…')
        result.recycled += duplicates.refresh()
        log(f'本地重复检查完成：回收 {result.recycled} 份重复副本；内容不同的记录保留。')
        # Keep duplicate tracking on SQLite rather than retaining all page/detail
        # objects or failed-payload diagnostics in Python memory.
        db.execute('PRAGMA temp_store=FILE')
        db.execute('CREATE TEMP TABLE seen (key TEXT, revision TEXT, identity TEXT, '
                   'create_time INTEGER, PRIMARY KEY(key, revision))')
        cursor, snapshot = 0, None
        while True:
            _check(cancel)
            try:
                records, next_cursor, snapshot, more, scanned = _page(client, job, cursor, snapshot)
            except DownloadError as exc:
                result.failed += 1
                log(f'自动清单查询失败：{exc}')
                break
            log(f'自动扫描：游标 {cursor} → {next_cursor} / 快照 {snapshot}；'
                f'本页扫描 {scanned} 条，匹配 {len(records)} 批')
            discovered += len(records)
            for item in records:
                _check(cancel)
                key = json.dumps([job.base_url.rstrip('/'), item['device'], item['uid'], 'motion', job.category],
                                 ensure_ascii=False, separators=(',', ':'))
                identity = item['cow_id'].casefold()
                try:
                    seen = db.execute('SELECT identity,create_time FROM seen WHERE key=? AND revision=?',
                                      (key, item['revision'])).fetchone()
                    if seen:
                        if seen != (identity, item['create_time']):
                            raise DownloadError('同 UID/revision 的自动清单身份或时间冲突')
                        if item['downloadable']:
                            result.skipped += 1
                        continue
                    db.execute('INSERT INTO seen VALUES (?,?,?,?)',
                               (key, item['revision'], identity, item['create_time']))
                    if not item['downloadable']:
                        result.pending += 1
                        log(f'待补齐 motion/{item["device"]}/{item["uid"]}：{item["archive_state"]}；未记为完成')
                        continue
                    cached = db.execute('SELECT revision,path,sha256,identity,create_time FROM completed WHERE key=?', (key,)).fetchone()
                    if cached and cached[0] == item['revision'] and cached[3:] == (identity, item['create_time']):
                        path = checked_path(job.farm, cached[1])
                        if path.is_file() and _file_sha256(path, cancel) == cached[2]:
                            result.skipped += 1
                            continue
                    # An empty requested identity prevents manual Target values
                    # from filling a source record whose cow identity is absent.
                    _check_disk(job.farm)
                    data = client.record('motion', item['uid'], item['device'], '')
                    if not isinstance(data, dict) or _device(data.get('device')) != item['device']:
                        raise DownloadError('详情设备编号与自动清单不一致')
                    if _integer(data.get('create_time'), 'create_time', 1) != item['create_time']:
                        raise DownloadError('详情采集时间与自动清单不一致')
                    historical = _identity(data)
                    if historical and item['cow_id'] and historical.casefold() != identity:
                        raise DownloadError('详情历史牛号与自动清单不一致')
                    data = dict(data)
                    if not historical and item['cow_id']:
                        data['cow_id'] = item['cow_id']
                        data['cow_identity_source'] = 'records_list'
                    category = plan.classify(data)['category'] if plan.value else job.category
                    routed = replace(job, category=category)
                    if category not in catalogs:
                        catalogs[category] = DuplicateCatalog(routed, db, cancel, log)
                        result.recycled += catalogs[category].refresh()
                    duplicates = catalogs[category]
                    equivalent = duplicates.find_equivalent(data)
                    if equivalent is not None:
                        path, digest = equivalent
                        saved = False
                    else:
                        path, saved, digest = _save_record(routed, data, cancel)
                    duplicates.remember(path)
                    result.recycled += duplicates.recycle_equivalents(path, data)
                    _check(cancel)
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        db.execute('INSERT OR REPLACE INTO completed VALUES (?,?,?,?,?,?)',
                                   (key, item['revision'], str(path.relative_to(job.farm.resolve())),
                                    digest, identity, item['create_time']))
                        _check(cancel)
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
                    result.saved += int(saved)
                    result.skipped += int(not saved)
                    log(('已下载：' if saved else '重复跳过：') + str(path.relative_to(job.farm.resolve())))
                except _StopCycle:
                    raise
                except (DownloadError, OSError, sqlite3.Error, ValueError) as exc:
                    db.rollback()
                    result.failed += 1
                    log(f'失败 motion/{item["device"]}/{item["uid"]}：{exc}')
                finally:
                    processed += 1
                    progress(processed, discovered)
            cursor = next_cursor
            if not more:
                break
    except Cancelled:
        result.canceled = True
    except _StopCycle as exc:
        result.failed += 1
        log(str(exc))
    finally:
        try:
            db.rollback()
        finally:
            db.close()
    return result
