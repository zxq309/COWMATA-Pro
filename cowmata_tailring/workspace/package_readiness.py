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

from .catalog import VIDEO_SUFFIXES, assert_not_being_written, digest_file, file_stamp
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
    """Snapshot the explicitly selected date folders without reclassifying data."""
    from .package_paths import check, safe_path
    root = Path(root)
    sensor_hashes, warnings = {}, []
    for unit in units:
        for relative in unit['paths']:
            check(cancelled)
            path = safe_path(root, relative)
            before = file_stamp(path)
            assert_not_being_written(path)
            raw = path.read_bytes()
            if before != file_stamp(path):
                raise ValueError('核验期间源文件发生变化：' + str(path))
            sensor_hashes[relative] = hashlib.sha256(raw).hexdigest()
    days = {u['day'] for u in units}
    videos = set(videos)
    for day in days:
        for path in (root / '录像' / day).rglob('*'):
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES and (views is None or path.parent.name in views):
                videos.add(path.relative_to(root).as_posix())
        if not any(v.startswith('录像/' + day + '/') for v in videos):
            warnings.append('所选日期无录像目录或视频：' + str(root / '录像' / day))
    for relative in sorted(videos):
        check(cancelled)
        path = safe_path(root, relative)
        assert_not_being_written(path)
    return dict(validation_scope='selected_directory', sensor_sha256=sensor_hashes, video_paths=sorted(videos),
                warnings=warnings, checked_at=datetime.now(CHINA).isoformat(),
                sensor_files=len(sensor_hashes), video_files=len(videos))
