"""A package is a frozen, complete snapshot of a finished download cycle."""
from __future__ import annotations

import hashlib
import json
import math
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from cowmata_tailring.edge_download.core import CHINA, record_datetime, validate_payload
from cowmata_tailring.edge_download.csv_targets import CsvPlan

from .catalog import assert_not_being_written, digest_file, file_stamp
from .device_identity import parse_device_folder
from .storage import ProjectLock


@contextmanager
def download_guard(root):
    path = Path(root) / '.edge-download/sync.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = ProjectLock(path)
    try:
        if not lock.acquired:
            raise ValueError('下载正在写入此牧场，请等本轮下载完成后再派包')
        yield
    finally:
        lock.close()


def ledger_state(root):
    root = Path(root)
    try:
        state = json.loads((root / '.edge-download/csv-cycle.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('缺少完整下载核对记录，请先在下载器核对所选日期') from exc
    if state.get('status') != 'complete' or state.get('failed') or state.get('canceled'):
        raise ValueError('本轮台账核对与下载尚未完整结束，请先完成下载再派包')
    try:
        plan = CsvPlan(state['ledger_directory'])
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError('台账读取失败，请在下载器重新核对三份 CSV') from exc
    if not plan.ready or plan.sources != state['sources']:
        raise ValueError('CSV 已更新或尚未核对完成，请先重新下载核对，再派包')
    return plan, state


def video_span(path, cancelled):
    from .video_intake import probe
    from .video_names import filename_wall
    wall = filename_wall(path)
    if wall is None:
        raise ValueError('录像名称缺少有效时间戳，请先归类：' + str(path))
    info = probe(path, cancelled)
    streams = [s for s in info.get('streams', []) if s.get('codec_type') == 'video']
    if not streams:
        raise ValueError('录像没有可读取的视频流：' + str(path))
    duration = float(info.get('format', {}).get('duration') or streams[0].get('duration') or 0) * 1000
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('录像时长无效：' + str(path))
    # Classification filenames use Beijing calendar time. This coverage check
    # does not create a scientific sensor/camera synchronization anchor.
    start = wall + datetime(1970, 1, 1, tzinfo=CHINA).timestamp() * 1000
    return start, start + duration


def covered(start, end, intervals):
    cursor = start
    for lo, hi in sorted(intervals):
        if hi < cursor:
            continue
        if lo > cursor + 1000:  # one-second filename precision, not an invented clip
            return False
        cursor = max(cursor, hi)
        if cursor + 1000 >= end:
            return True
    return False


def validate_complete(root, units, videos, *, views=None, cancelled=lambda: False, probe_cache=None):
    from .package_paths import check, safe_path
    from .sensor_records import parse_sensor_object
    root = Path(root)
    plan, cycle = ledger_state(root)
    cycle_hash = digest_file(root / '.edge-download/csv-cycle.json')
    failures, spans, sensor_hashes = [], [], {}
    for unit in units:
        missing = {'Motion', 'PPG', 'Temp'} - set(unit['modalities'])
        if missing:
            failures.append(unit['key'] + '：缺少 ' + '/'.join(sorted(missing)))
            continue
        identity = parse_device_folder(unit['owner'])
        for relative in unit['paths']:
            check(cancelled)
            path = safe_path(root, relative)
            before = file_stamp(path)
            assert_not_being_written(path)
            raw = path.read_bytes()
            data = json.loads(raw.decode('utf-8-sig'))
            kind = 'motion' if '/Motion/' in relative else 'pulse' if '/PPG/' in relative else 'temp'
            validate_payload(data, kind)
            stamp = record_datetime(data, kind)
            if stamp.strftime('%Y-%m-%d') != unit['day']:
                raise ValueError('采集日期与目录不一致：' + relative)
            if path.stem != stamp.strftime('%Y-%m-%d_%H-%M-%S'):
                raise ValueError('JSON 文件名与采集时间不一致：' + relative)
            wear, reason = plan.resolve_download(data.get('device', ''), stamp, data.get('cow_id', ''))
            if wear is None or wear.category != unit['category'] or wear.identity != identity:
                raise ValueError('台账、设备或健康类别不一致：' + relative + ' ' + reason)
            if not any(r['device'] == identity.device_id and
                       datetime.fromisoformat(r['start']) <= stamp < datetime.fromisoformat(r['end'])
                       for r in cycle.get('verified_ranges', [])):
                raise ValueError('所选记录尚未完成下载时段核对：' + relative)
            if kind != 'temp':
                sensor = parse_sensor_object(data, path, kind='ppg' if kind == 'pulse' else 'imu')
                if sensor.sample_count <= 0:
                    raise ValueError('原始数据没有有效采样：' + relative)
                spans.append((sensor.epoch_at(0), sensor.epoch_at(sensor.duration_ms), relative))
            if before != file_stamp(path):
                raise ValueError('核验期间源文件发生变化：' + relative)
            sensor_hashes[relative] = hashlib.sha256(raw).hexdigest()
    if failures:
        raise ValueError('\n'.join(failures))
    videos = set(videos)
    days = {u['day'] for u in units}
    extra_days = set()
    for lo, hi, _ in spans:
        day = datetime.fromtimestamp(lo / 1000, CHINA).date()
        end = datetime.fromtimestamp(hi / 1000, CHINA).date()
        while day <= end:
            if day.isoformat() not in days:
                extra_days.add(day.isoformat())
            day += timedelta(days=1)
    for day in extra_days | (days if views is None else set()):
        for path in (root / '录像' / day).rglob('*.mp4'):
            if views is None or path.parent.name in views:
                videos.add(path.relative_to(root).as_posix())
    if not videos:
        raise ValueError('缺少所选日期录像，不能派包')
    intervals = {}
    probe_cache = {} if probe_cache is None else probe_cache
    for relative in sorted(videos):
        check(cancelled)
        path = safe_path(root, relative)
        before = file_stamp(path)
        assert_not_being_written(path)
        key = (str(path), before)
        if key not in probe_cache:
            probe_cache[key] = video_span(path, cancelled)
        intervals.setdefault(path.parent.name, []).append(probe_cache[key])
        if before != file_stamp(path):
            raise ValueError('核验期间录像发生变化：' + relative)
    selected = views or sorted(intervals)
    for lo, hi, relative in spans:
        missing_views = [view for view in selected if not covered(lo, hi, intervals.get(view, []))]
        if missing_views:
            failures.append(relative + '：录像未覆盖采集时段 ' + '/'.join(missing_views))
    if failures:
        raise ValueError('\n'.join(failures[:40]))
    latest, _ = ledger_state(root)
    if latest.sources != plan.sources or cycle_hash != digest_file(root / '.edge-download/csv-cycle.json'):
        raise ValueError('核验期间 CSV 已变化，请重新核对')
    return dict(sources=plan.sources, cycle_sha256=cycle_hash, sensor_sha256=sensor_hashes, video_paths=sorted(videos),
                checked_at=datetime.now(CHINA).isoformat(), sensor_files=sum(len(u['paths']) for u in units), video_files=len(videos))
