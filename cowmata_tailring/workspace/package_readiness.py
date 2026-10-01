"""Dispatch readiness notes. Dispatch never waits for, or blocks, the downloader.

4.4.2: a package freezes the files on disk by name, size and stamp only (see
``collaboration_packages``). Nothing on the dispatch path parses sensor JSON or
probes recordings: the operator has already checked the data, and one odd file
must never hold back a whole package. ``video_span``/``covered`` remain for
coverage diagnostics.
"""
from __future__ import annotations

import json
import math
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from cowmata_tailring.edge_download.core import CHINA

from .storage import ProjectLock


@contextmanager
def download_guard(root):
    path = Path(root) / '.edge-download/sync.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = ProjectLock(path)
    try:
        if not lock.acquired:
            raise ValueError('下载正在写入此牧场：请等本轮下载结束，或先点“下载 → 暂停下载”，再统一录像目录')
        yield
    finally:
        lock.close()


def download_cycle_warning(root):
    """Advisory note about the downloader state; dispatch never blocks on it."""
    root = Path(root)
    try:
        state = json.loads((root / '.edge-download/csv-cycle.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if state.get('status') == 'complete' and not state.get('failed') and not state.get('canceled'):
        return None
    when = state.get('finished_at') or state.get('started_at') or ''
    return '下载循环状态为「' + str(state.get('status') or '未知') + '」(' + str(when) + ')；本次按当前已落地资料派发。'


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
