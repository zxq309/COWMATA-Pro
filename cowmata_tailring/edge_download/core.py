"""Download-only port of MotionDownloadManager / ServerRecordConverter.

No annotation, plotting, database credentials or model runtime dependencies.
Wire timestamps are UTC; farm directory dates use fixed China time (UTC+08).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request

from .transport import urlopen

CHINA = timezone(timedelta(hours=8))
MODALITIES = {'motion': 'Motion', 'pulse': 'PPG', 'temp': 'Temp'}
CATEGORIES = ('产犊', '发情', '正常', '疫病', '怀孕', '待核对', '怀孕/孕早期', '怀孕/孕中期', '怀孕/孕晚期', '未分类')


class DownloadError(ValueError):
    pass


class TransferFailed(DownloadError):
    pass


class Cancelled(Exception):
    pass


def segment(value: str) -> str:
    value = str(value).strip()
    if (not value or value in ('.', '..') or value.endswith(('.', ' '))
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            or value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                                               *(f'COM{i}' for i in range(10)),
                                               *(f'LPT{i}' for i in range(10))}):
        raise DownloadError(f'无效目录名称：{value!r}')
    return value


def checked_path(root: Path, relative) -> Path:
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise DownloadError('保存路径超出所选牧场目录')
    return result


@dataclass(frozen=True)
class Target:
    device: str = ''
    cow: str = ''
    mark: str = ''

    def validate(self):
        if not (self.device or self.cow):
            raise DownloadError('每行至少填写设备编号或牛耳标')
        for value in (self.device, self.cow, self.mark):
            if value:
                segment(value)
                if '-' in value:
                    raise DownloadError('设备、牛耳标、现场记号不能含连字符 -')


@dataclass(frozen=True)
class Job:
    base_url: str
    farm: Path
    category: str
    targets: tuple[Target, ...]
    kinds: tuple[str, ...]
    start: datetime
    end: datetime
    ledger_directory: Path | None = None

    def validate(self):
        validate_url(self.base_url)
        if urlsplit(self.base_url).query:
            raise DownloadError('下载服务器应填写接口根地址，不含查询参数')
        if not self.farm.is_absolute() or not self.farm.is_dir():
            raise DownloadError('请选择已存在的牧场根目录')
        if self.category not in CATEGORIES:
            raise DownloadError('请选择采集类别（怀孕需选择孕期）')
        if not self.targets or not self.kinds or any(k not in MODALITIES for k in self.kinds):
            raise DownloadError('请填写下载对象并选择 Motion、PPG 或温度')
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise DownloadError('结束时间必须晚于开始时间')
        for target in self.targets:
            target.validate()
            if urlsplit(self.base_url).hostname in {'device.cowmata.com', 'data.cowmata.com'} and not target.device:
                raise DownloadError('当前设备服务器需填写设备编号；牛号查询请使用 3090 接口')
            if any(k != 'motion' for k in self.kinds) and not target.device:
                raise DownloadError('PPG / 温度查询需要设备编号；仅按牛号查询时请选择 Motion')
        # Multiple identities on one device require separate dated tasks.
        keys = [t.device.upper() if t.device else 'cow:' + t.cow for t in self.targets]
        if len(set(keys)) != len(keys):
            raise DownloadError('同一设备/牛号请勿重复填写；换绑前后请分别选择时间范围下载')


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise DownloadError('服务器地址必须是无内嵌密码的 HTTP/HTTPS 地址')
    if parsed.fragment:
        raise DownloadError('服务器地址不能含片段标记')
    return url


class Client:
    def __init__(self, base_url, cancel: threading.Event, log=lambda message: None):
        self.base_url = base_url.rstrip('/')
        self.cancel = cancel
        self.log = log

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def get(self, url, limit=32 * 1024 * 1024):
        validate_url(url)
        for attempt in range(4):
            self.check()
            try:
                req = Request(url, headers={'User-Agent': 'CowmataEdgeDownloader/1.0',
                                           'Accept': 'application/json, application/octet-stream'})
                with urlopen(req, timeout=30) as response:
                    validate_url(response.url)
                    if urlsplit(url).scheme == 'https' and urlsplit(response.url).scheme != 'https':
                        raise DownloadError('拒绝 HTTPS 降级重定向')
                    if int(response.headers.get('Content-Length', 0)) > limit:
                        raise DownloadError('响应超过允许大小')
                    chunks, length = [], 0
                    while True:
                        self.check()
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        length += len(chunk)
                        if length > limit:
                            raise DownloadError('响应超过允许大小')
                        chunks.append(chunk)
                    expected = response.headers.get('Content-Length')
                    if expected is not None and length != int(expected):
                        raise DownloadError('下载不完整：实际长度与 Content-Length 不一致')
                    return b''.join(chunks)
            except HTTPError as exc:
                try:
                    if exc.code < 500 and exc.code != 429:
                        try:
                            payload = json.loads(exc.read(8192))
                            message = payload.get('message', '') if isinstance(payload, dict) else ''
                        except (ValueError, OSError):
                            message = ''
                        raise DownloadError(f'HTTP {exc.code}：{message or "服务器拒绝请求"}') from exc
                    error = exc
                finally:
                    exc.close()
            except (URLError, OSError, TimeoutError) as exc:
                error = exc
            except ValueError as exc:
                raise DownloadError(str(exc)) from exc
            if attempt == 3:
                raise TransferFailed(f'连接失败（初次及 3 次重试均失败）：{error}') from error
            self.log(f'连接中断，正在重试（{attempt + 1}/3）')
            if self.cancel.wait(0.25 * (attempt + 1)):
                raise Cancelled()

    def envelope(self, path, params):
        raw = self.get(self.base_url + path + '?' + urlencode(params))
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict) or str(obj.get('code')) != '0':
                raise DownloadError(str(obj.get('message', obj.get('msg', '服务器响应失败')))
                                    if isinstance(obj, dict) else '服务器响应不是对象')
            if not isinstance(obj.get('data'), dict):
                raise DownloadError('响应缺少 data 对象')
            return obj['data']
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DownloadError('服务器返回无效 JSON') from exc

    def listing(self, target, start, end, kinds):
        self.check()
        params = {'device': target.device} if target.device else {'cow': target.cow}
        params.update(startTime=start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),
                      endTime=end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'))
        try:
            data = self.envelope('/device/data/count', params)
        except DownloadError as exc:
            # Existing 3090 API has no pagination; split dense time windows.
            if '10000' in str(exc) and (end - start).total_seconds() > 2:
                middle = start + timedelta(seconds=int((end - start).total_seconds() // 2))
                yield from self.listing(target, start, middle, kinds)
                yield from self.listing(target, middle, end, kinds)
                return
            raise
        for kind in kinds:
            records = data.get(kind + 'Records', [])
            uids = data.get(kind + 'Uids', [])
            if not isinstance(records, list) or not isinstance(uids, list):
                raise DownloadError('数据清单格式错误')
            if not records:
                if uids and not target.device:
                    raise DownloadError('该服务器不支持按牛号跨设备查询，请填写设备编号')
                records = [dict(uid=uid, device=target.device) for uid in uids]
            for record in records:
                if not isinstance(record, dict):
                    raise DownloadError('无效记录清单项')
                try:
                    uid = int(str(record['uid']))
                except (KeyError, ValueError, TypeError) as exc:
                    raise DownloadError('清单 UID 无效') from exc
                device = str(record.get('device') or target.device).strip().upper()
                if uid <= 0 or not device:
                    raise DownloadError('清单缺少设备编号或 UID')
                if target.device and device != target.device.upper():
                    raise DownloadError('清单设备编号与查询不一致')
                yield kind, uid, device, str(record.get('cow_id') or '').strip()

    def record(self, kind, uid, device, cow):
        for attempt in range(4):
            self.check()
            try:
                return self._record_once(kind, uid, device, cow)
            except TransferFailed:
                raise
            except DownloadError as exc:
                if attempt == 3:
                    raise
                self.log(f'数据校验失败，重试 {attempt+1}/3 · {kind}/{device}/{uid} · {exc}')
                if self.cancel.wait(.25*(attempt+1)):
                    raise Cancelled() from exc

    def _record_once(self, kind, uid, device, cow):
        params = {'uid': uid}
        if urlsplit(self.base_url).hostname not in {'device.cowmata.com', 'data.cowmata.com'}:
            params['device'] = device
        data = self.envelope('/device/data/' + kind, params)
        if data.get('url'):
            payload = self.get(urljoin(self.base_url + '/', data['url']), 256 * 1024 * 1024)
            try:
                external = json.loads(payload)
            except (UnicodeError, ValueError):
                external = None
            if isinstance(external, dict):
                if 'code' in external or 'success' in external:
                    if ('code' in external and str(external['code']) != '0') or external.get('success') is False:
                        raise DownloadError('外部 JSON 返回错误状态')
                    external = external.get('data')
                if not isinstance(external, dict):
                    raise DownloadError('外部 JSON 内容无效')
                for key in ('device', 'cow_id', 'animal_number', 'animalNumber', 'create_time'):
                    if data.get(key) not in (None, '') and external.get(key) not in (None, ''):
                        if str(data[key]).upper() != str(external[key]).upper():
                            raise DownloadError(f'外部数据 {key} 与元数据不一致')
                for key, value in external.items():
                    if data.get(key) in (None, '') and key != 'url':
                        data[key] = value
            elif kind == 'motion':
                data['imu'] = base64.b64encode(payload).decode('ascii')
            else:
                raise DownloadError('PPG / 温度外部数据必须为 JSON')
        if str(data.get('device', '')).upper() != device.upper():
            raise DownloadError('详情设备编号与查询不一致')
        historical = str(data.get('cow_id') or data.get('animal_number') or data.get('animalNumber') or '').strip()
        if cow and historical and historical.casefold() != cow.casefold():
            raise DownloadError(f'历史牛号不一致：查询 {cow}，记录 {historical}')
        # Explicit user-provided ear tag is allowed for old device-only endpoints.
        # Identity inferred from CSV/query belongs in the plan, never in raw JSON.
        if not (historical or cow):
            self.log('记录无历史牛号，按设备下载并归入待核对目录')
        data.pop('url', None)
        validate_payload(data, kind)
        return data


def decoded(data, field, required=False):
    value = data.get(field)
    if value in ('', None) and not required:
        return b''
    try:
        value = ''.join(value.split())
        raw = base64.b64decode(value + '=' * (-len(value) % 4), validate=True)
        if not raw:
            raise ValueError('empty')
        return raw
    except (AttributeError, TypeError, ValueError) as exc:
        raise DownloadError(f'{field} 不是有效的非空 Base64 数据') from exc


def validate_payload(data, kind):
    try:
        stamp = int(str(data.get('create_time')))
        if stamp <= 0:
            raise ValueError()
        datetime.fromtimestamp(stamp / 1000, CHINA)
    except (ValueError, TypeError, OSError, OverflowError) as exc:
        raise DownloadError('记录缺少有效的毫秒 create_time') from exc
    if kind == 'motion':
        raw = decoded(data, 'imu', True)
        frame = {'0': 18, '1': 20, '2': 22}.get(str(data.get('version')))
        if not frame or len(raw) % frame:
            raise DownloadError('IMU 版本或帧长度不正确（支持 v0/v1/v2）')
        integrity = data.get('_integrity') or {}
        if not isinstance(integrity, dict):
            raise DownloadError('完整性元数据必须是对象')
        checks = {'imu_size': len(raw), 'frame_bytes': frame, 'frame_count': len(raw) // frame,
                  'imu_sha256': hashlib.sha256(raw).hexdigest()}
        for key, expected in checks.items():
            if key in integrity and str(integrity[key]).lower() != str(expected):
                raise DownloadError(f'原始 BIN 完整性校验失败：{key}')
    elif kind == 'temp':
        value = data.get('data')
        if value is None or isinstance(value, bool) or not isinstance(value, int | float | str | dict | list):
            raise DownloadError('温度记录缺少有效原始 data 字段')
    else:
        green, infrared, acc = (decoded(data, key) for key in ('data', 'ir_data', 'imu_data'))
        if not (green or infrared) or len(green) % 2 or len(infrared) % 2 or len(acc) % 6:
            raise DownloadError('PPG 光学通道或同步 ACC 长度无效')


def fingerprint(data, kind):
    fields = ('imu',) if kind == 'motion' else ('data', 'ir_data', 'imu_data')
    h = hashlib.sha256()
    h.update(json.dumps([kind, str(data['device']).upper(), data['create_time'],
                         data.get('version')], separators=(',', ':')).encode())
    if kind == 'temp':
        h.update(json.dumps(data['data'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
        return h.hexdigest()
    for field in fields:
        raw = decoded(data, field)
        h.update(len(raw).to_bytes(8, 'big'))
        h.update(raw)
    return h.hexdigest()


def save_record(job, target, kind, data, cancel):
    validate_payload(data, kind)
    device = segment(str(data['device']).upper())
    cow = segment(data.get('cow_id') or data.get('animal_number') or data.get('animalNumber') or target.cow or '待核对')
    stamp = datetime.fromtimestamp(int(data['create_time']) / 1000, CHINA)
    day = checked_path(job.farm, Path(job.category) / MODALITIES[kind]
                       / stamp.strftime('%Y-%m-%d'))
    prefix = f'{device}-{cow}-'
    mark = target.mark
    if not mark:
        matches = set()
        for stream in MODALITIES.values():
            sibling = checked_path(job.farm, Path(job.category) / stream / stamp.strftime('%Y-%m-%d'))
            if sibling.is_dir():
                matches.update(p.name for p in sibling.iterdir()
                               if p.is_dir() and p.name.upper().startswith(prefix.upper()))
        if len(matches) > 1:
            raise DownloadError('同日存在多个现场记号，请明确填写现场记号')
        if matches:
            mark = next(iter(matches))[len(prefix):]
    folder = checked_path(job.farm, day / (prefix + segment(mark or '待核对')))
    folder.mkdir(parents=True, exist_ok=True)
    name = stamp.strftime('%Y-%m-%d_%H-%M-%S')
    digest = fingerprint(data, kind)
    for existing in folder.glob(name + '*.json'):
        if cancel.is_set():
            raise Cancelled()
        checked_path(job.farm, existing)
        try:
            old = json.loads(existing.read_bytes())
            if fingerprint(old, kind) == digest:
                return existing, False
        except (ValueError, KeyError, TypeError):
            continue
    encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    # Windows rename refuses existing targets (also works on exFAT); POSIX
    # uses hard links because its rename would overwrite existing originals.
    fd, temporary = tempfile.mkstemp(prefix='.edge-', suffix='.part', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if cancel.is_set():
            raise Cancelled()
        for suffix in ('', '_' + digest):
            final = checked_path(job.farm, folder / (name + suffix + '.json'))
            try:
                if os.name == 'nt':
                    os.rename(temporary, final)
                else:
                    os.link(temporary, final)
                return final, True
            except FileExistsError:
                try:
                    if fingerprint(json.loads(final.read_bytes()), kind) == digest:
                        return final, False
                except (ValueError, KeyError, TypeError):
                    pass
        raise DownloadError('目标文件冲突，已保留现有文件')
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass
class Result:
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    canceled: bool = False
    pending: int = 0
    recycled: int = 0


def run_job(job, cancel, log=lambda message: None, progress=lambda done, total: None,
            client_factory=Client):
    """Re-query the full requested range; cache completed files, retry failures.

    This also catches late uploads without advancing a lossy time watermark.
    A fresh sqlite connection belongs to each worker, never to the GUI thread.
    """
    job.validate()
    result = Result()
    client = client_factory(job.base_url, cancel, log)
    state = checked_path(job.farm, '.edge-download')
    state.mkdir(exist_ok=True)
    db_path = checked_path(job.farm, state / 'completed.sqlite3')
    db = sqlite3.connect(db_path, timeout=10)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS completed (key TEXT PRIMARY KEY, path TEXT, size INTEGER, mtime INTEGER)')
        seen = set()
        for target in job.targets:
            start = job.start
            while start < job.end:
                client.check()
                end = min(start + timedelta(days=1), job.end)
                log(f'查询 {target.device or target.cow} · {start:%Y-%m-%d %H:%M} ～ {end:%Y-%m-%d %H:%M}')
                try:
                    items = list(client.listing(target, start, end, job.kinds))
                except DownloadError as exc:
                    result.failed += 1
                    log(f'清单失败：{exc}')
                    start = end
                    continue
                for kind in job.kinds:
                    if not any(item[0] == kind for item in items):
                        log(f'{"Motion" if kind == "motion" else "PPG"}：该时段服务器未返回数据')
                for kind, uid, device, history_cow in items:
                    client.check()
                    key = json.dumps([job.base_url.rstrip('/'), job.category, target.cow,
                                      target.mark, kind, uid, device, history_cow])
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        cached = db.execute('SELECT path,size,mtime FROM completed WHERE key=?', (key,)).fetchone()
                        if cached:
                            previous = checked_path(job.farm, cached[0])
                            if previous.is_file():
                                stat = previous.stat()
                                if stat.st_size == cached[1] and stat.st_mtime_ns == cached[2]:
                                    result.skipped += 1
                                    progress(result.saved + result.skipped + result.failed, len(seen))
                                    continue
                        if target.cow and history_cow and target.cow.casefold() != history_cow.casefold():
                            raise DownloadError(f'清单历史牛号为 {history_cow}，与填写的 {target.cow} 不一致')
                        data = client.record(kind, uid, device, target.cow or history_cow)
                        actual = datetime.fromtimestamp(int(data['create_time']) / 1000, CHINA)
                        if not job.start <= actual < job.end:
                            raise DownloadError('记录采集时间不在所选范围内')
                        path, saved = save_record(job, target, kind, data, cancel)
                        stat = path.stat()
                        db.execute('INSERT OR REPLACE INTO completed VALUES (?,?,?,?)',
                                   (key, str(path.relative_to(job.farm.resolve())), stat.st_size, stat.st_mtime_ns))
                        db.commit()
                        result.saved += int(saved)
                        result.skipped += int(not saved)
                        log(('已下载：' if saved else '重复跳过：') + str(path.relative_to(job.farm.resolve())))
                    except (DownloadError, OSError, sqlite3.Error) as exc:
                        result.failed += 1
                        log(f'失败 {kind}/{device}/{uid}：{exc}')
                    progress(result.saved + result.skipped + result.failed, len(seen))
                start = end
    except Cancelled:
        result.canceled = True
    finally:
        db.close()
    return result
