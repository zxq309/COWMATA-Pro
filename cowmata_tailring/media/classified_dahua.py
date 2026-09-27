"""Use the existing Dahua frame/byte clock for timestamp-named PS recordings."""
from pathlib import Path
from .dahua_duration import (DahuaPacketSummary, build_dahua_duration_index,
    is_dahua_program_stream, scan_dahua_program_stream, _encode_timestamp_points)
from .timeline import MediaTimelineIndex, TimelineSegment


def classified_dahua_timeline(path, cancelled=lambda: False):
    if not is_dahua_program_stream(path):
        return None
    path = Path(path)
    before = path.stat()
    if cancelled():
        raise InterruptedError('录像索引已取消')
    scan = scan_dahua_program_stream(path)
    if scan.frame_count <= 0 or not 5 <= scan.frame_duration_ms <= 250 or not scan.keyframes:
        raise ValueError('大华码流缺少有效帧计数、帧时钟或关键帧')
    packets = DahuaPacketSummary(scan.frame_count, scan.frame_duration_ms, before.st_size, 1, 0, 0)
    index = build_dahua_duration_index(path, packets, scan, allow_adjacent=False)
    after = path.stat()
    if cancelled():
        raise InterruptedError('录像索引已取消')
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError('录像索引期间文件变化')
    native = dict(signature='dahua-classified-frame-clock-1', family='dahua-program-frame-clock',
        source_size=before.st_size, source_mtime_ns=before.st_mtime_ns,
        first_pts_ms=0, frame_ms=scan.frame_duration_ms, frame_count=scan.frame_count,
        duration_ms=index.duration_ms, valid_end=before.st_size,
        keys=[[k.time_ms,k.byte_offset] for k in index.seek_points],
        timestamp_data=_encode_timestamp_points(index.timestamp_points), patch_timestamps=True)
    return MediaTimelineIndex(str(path.resolve()),before.st_size,before.st_mtime_ns,0,
        scan.frame_duration_ms,(TimelineSegment(0,index.duration_ms,0,index.duration_ms),),(),native=native)
