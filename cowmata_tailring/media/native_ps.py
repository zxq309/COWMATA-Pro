"""Read-only camera clock/byte index for the sampled HK and Shenmo PS formats.

These are explicitly versioned, sample-validated private layouts, not generic
MP4 creation dates. Unknown, discontinuous or damaged streams use the OCR path.
No filesystem dates, sequential filenames or adjacent files establish time.
"""
from __future__ import annotations

import bisect
import mmap
import statistics
from datetime import datetime
from pathlib import Path

SIGNATURE = "native-ps-hk-shenmo-v1"
PREFIX = b"\x00\x00\x01"
EPOCH = datetime(1970, 1, 1)
# A recorder restarting its stream PTS (camera reconnect) is bridged by the
# recorder's own clock; a longer silence inside one file is not trusted.
MAX_RUN_GAP_MS = 6 * 60 * 60 * 1000


def pts_value(data):
    if len(data) != 5 or any(not data[i] & 1 for i in (0, 2, 4)):
        raise ValueError("Invalid PES timestamp markers")
    return (((data[0] >> 1) & 7) << 30 | data[1] << 22 |
            (data[2] >> 1) << 15 | data[3] << 7 | data[4] >> 1) / 90


def hk_clock(packet):
    if len(packet) < 10:
        return None
    end = 10 + int.from_bytes(packet[8:10], "big")
    if end > len(packet) - 4:
        return None
    pos = 10
    while pos + 2 <= end:
        tag, size = packet[pos:pos + 2]
        payload = packet[pos + 2:pos + 2 + size]
        if pos + 2 + size > end:
            return None
        if tag == 0x40 and size == 14 and payload[:4] == b"HK\x01\x00":
            value = int.from_bytes(payload[4:10], "big")
            try:
                clock = datetime(2000 + (value >> 40), (value >> 36) & 15,
                                 (value >> 31) & 31, (value >> 26) & 31,
                                 (value >> 20) & 63, (value >> 14) & 63)
                if not 2010 <= clock.year <= 2099:
                    return None
                # Subsecond HK bits have not been independently calibrated.
                return (clock - EPOCH).total_seconds() * 1000
            except ValueError:
                return None
        pos += 2 + size
    return None


def shenmo_clock(header, timezone_minutes=480):
    if len(header) < 9 or header[6] & 0xc0 != 0x80 or not header[7] & 1:
        return None
    end, pos, flags = 9 + header[8], 9, header[7]
    if end > len(header):
        return None
    for mask, size in ((128, 5), (64, 5), (32, 6), (16, 3), (8, 1), (4, 1), (2, 2)):
        if flags & mask:
            pos += size
    if pos >= end:
        return None
    ext = header[pos]
    pos += 1
    if ext & 128:
        pos += 16
    if ext & 64:
        if pos >= end:
            return None
        pos += 1 + header[pos]
    if ext & 32:
        pos += 2
    if ext & 16:
        pos += 2
    if not ext & 1 or pos >= end:
        return None
    size = header[pos] & 127
    pos += 1
    if size < 9 or pos + size > end or header[pos] != 128:
        return None
    value = int.from_bytes(header[pos + 1:pos + 9], "little")
    if not 1262304000000 <= value < 4102444800000:
        return None
    return value + timezone_minutes * 60000


def packets(data, start=0, stop=None, cancelled=None):
    """Only contiguous, length-validated MPEG-2 PS packets; never scan payload."""
    size, pos, count = len(data) if stop is None else min(stop, len(data)), start, 0
    while pos + 6 <= size:
        if count % 256 == 0 and cancelled and cancelled():
            raise InterruptedError("原生时间索引已取消")
        count += 1
        if data[pos:pos + 3] != PREFIX:
            return
        code = data[pos + 3]
        if code == 0xba:
            if pos + 14 > size or data[pos + 4] & 0xc0 != 0x40:
                return
            length = 14 + (data[pos + 13] & 7)
        elif code == 0xb9:
            return
        elif code >= 0xbb:
            length = 6 + int.from_bytes(data[pos + 4:pos + 6], "big")
            if length == 6:
                return  # Unbounded PES needs a different parser, not guessing.
        else:
            return
        if pos + length > size:
            return
        yield pos, code, pos + length
        pos += length


def _scan(data, start=0, stop=None, timezone_minutes=480, cancelled=None):
    anchors, frames, keys, timestamps = [], [], [], []
    # Frame indices of anchors/keys and the byte of each frame keep PTS resets
    # (equal PTS values in different runs) unambiguous.
    anchor_frames, key_frames, frame_bytes = [], [], []
    pending_wall, pack, codec, family, end = None, start, None, None, start
    scr = None
    for pos, code, end in packets(data, start, stop, cancelled):
        if code == 0xba:
            pack = pos
            b = data[pos + 4:pos + 10]
            scr = (((b[0] >> 3) & 7) << 30 | (b[0] & 3) << 28 | b[1] << 20 |
                   (b[2] >> 3) << 15 | (b[2] & 3) << 13 | b[3] << 5 | b[4] >> 3) / 90
        elif code == 0xbc:
            stamp = hk_clock(data[pos:end])
            if stamp is not None:
                pending_wall, family = stamp, "hikvision-hk1"
        elif 0xe0 <= code <= 0xef:
            if code != 0xe0:
                raise ValueError("Multiple PS video streams require explicit channel inspection")
            header = data[pos:min(end, pos + 264)]
            if len(header) < 9 or header[6] & 0xc0 != 0x80 or 9 + header[8] > len(header):
                continue
            pts = pts_value(header[9:14]) if header[7] & 128 else None
            if pts is not None:
                timestamps.append([pos+9, pts, bool(header[7] & 64), True])
            stamp = shenmo_clock(header, timezone_minutes)
            if stamp is not None:
                family = "shenmo-pes2-epoch"
                # Shenmo's first access unit omits PES PTS but has a pack SCR
                # and the native per-frame clock. Keep its parameter sets.
                if pts is None:
                    pts = scr
            if pts is None:
                # Some Shenmo fragments have extension data but no PTS. Their
                # clock is useful for routing; full indexing requires PTS.
                continue
            if not frames or pts != frames[-1]:
                frames.append(pts)
                frame_bytes.append(pos)
            if pending_wall is not None:
                anchors.append([pts, pending_wall])
                anchor_frames.append(len(frames) - 1)
                pending_wall = None
            elif stamp is not None and (not anchors or anchors[-1][0] != pts):
                anchors.append([pts, stamp])
                anchor_frames.append(len(frames) - 1)
            payload = data[pos + 9 + header[8]:min(end, pos + 9 + header[8] + 4096)]
            # Parameter-set-bearing starts are retained as decode candidates;
            # the decoder must still validate the requested real frame.
            stripped = payload[1:] if payload.startswith(b"\0\0\0\1") else payload
            if stripped.startswith(PREFIX + b"\x67"):
                codec = "h264"
                keys.append([pts, pack])
                key_frames.append(len(frames) - 1)
            elif stripped.startswith(PREFIX + b"\x40"):
                codec = "hevc"
                keys.append([pts, pack])
                key_frames.append(len(frames) - 1)
        elif 0xc0 <= code <= 0xdf:
            header = data[pos:min(end, pos+264)]
            if len(header) >= 14 and header[6] & 0xc0 == 0x80 and header[7] & 128:
                timestamps.append([pos+9, pts_value(header[9:14]), bool(header[7] & 64), False])
    return {"anchors": anchors, "frames": frames, "keys": keys, "timestamps": timestamps,
            "anchor_frames": anchor_frames, "key_frames": key_frames, "frame_bytes": frame_bytes,
            "family": family, "codec": codec, "valid_end": end}


def native_hint(path, *, timezone_minutes=480, cancelled=None):
    """Tiny header + reverse tail lookup. Routing only, never verified evidence."""
    path = Path(path)
    with path.open("rb") as stream:
        if stream.read(4) != PREFIX + b"\xba":
            return None
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            head = _scan(data, stop=min(len(data), 1024 * 1024), timezone_minutes=timezone_minutes, cancelled=cancelled)
            if not head["anchors"]:
                return None
            family = head["family"]
            marker = PREFIX + (b"\xbc" if family == "hikvision-hk1" else b"\xe0")
            tail_start = max(0, len(data) - 4 * 1024 * 1024)
            pos = data.rfind(marker, tail_start)
            tail = None
            # A trailing accidental signature must not become a date. Bound
            # retries, require a valid enclosing pack and plausible continuity.
            for _ in range(8):
                if cancelled and cancelled():
                    raise InterruptedError("录像时间查找已取消")
                if pos < 0:
                    break
                pack = data.rfind(PREFIX + b"\xba", max(0, pos - 65536), pos)
                if pack >= 0:
                    candidate = _scan(data, pack, min(len(data), pack + 1024 * 1024), timezone_minutes, cancelled)
                    if candidate["family"] == family and candidate["anchors"]:
                        tail = candidate
                        break
                pos = data.rfind(marker, tail_start, pos)
            first_pts, first_wall = head["anchors"][0]
            start = first_wall - (first_pts - head["frames"][0])
            end = None
            if tail:
                last_pts, last_wall = tail["anchors"][-1]
                if 0 <= last_wall - start < 7 * 86400000:
                    # Conservative edge padding; exact last-frame coverage is
                    # supplied only by full inspection, never by this hint.
                    # Frames before the last anchor may belong to a run whose
                    # PTS was reset afterwards; they say nothing about the end.
                    later = tail["frames"][tail["anchor_frames"][-1]:]
                    end = last_wall + max(0, max(later) - last_pts) + 2000
            return {"start_ms": start, "end_ms": end, "native": SIGNATURE,
                    "family": family, "hint_only": True}


def read_native_index(path, *, timezone_minutes=480, cancelled=None):
    path = Path(path).resolve()
    before = path.stat()
    with path.open("rb") as stream:
        if stream.read(4) != PREFIX + b"\xba":
            return None
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            result = _scan(data, timezone_minutes=timezone_minutes, cancelled=cancelled)
    if not result["family"] or len(result["anchors"]) < 2 or len(result["frames"]) < 2 or not result["keys"]:
        return None
    frames, anchors = result.pop("frames"), result["anchors"]
    anchor_frames, key_frames = result.pop("anchor_frames"), result.pop("key_frames")
    frame_bytes = result.pop("frame_bytes")
    deltas = [b - a for a, b in zip(frames, frames[1:])]
    frame_ms = statistics.median(d for d in deltas if 0 < d <= 1000)
    runs = _clock_runs(frames, anchors, anchor_frames, frame_ms)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("录像在原生时间读取期间变化")
    first = frames[0]
    if before.st_size - result["valid_end"] > max(32 * 1024 * 1024, before.st_size // 8):
        raise ValueError("原生录像中段存在未解析内容，需完整 OCR 复核")
    result.update(signature=SIGNATURE, source_size=before.st_size, source_mtime_ns=before.st_mtime_ns,
                  first_pts_ms=first, frame_ms=frame_ms, duration_ms=runs[-1]["media_end"],
                  wall_start=runs[0]["wall_start"], timezone_minutes=timezone_minutes)
    if len(runs) > 1:
        # Media time stays contiguous (decoders and seek keys); each run keeps
        # its own recorder-clock start, so the real gap between runs is kept.
        result["runs"] = [[r["media_start"], r["media_end"], r["wall_start"]] for r in runs]
        result["patch_timestamps"] = True

    def position(pts, run):
        return pts - run["base"] + run["media_start"]

    def logical(pts, run):
        return max(run["media_start"], position(pts, run))

    def run_of_frame(index):
        return runs[bisect.bisect_right([r["first"] for r in runs], index) - 1]

    result["keys"] = [[position(p, run_of_frame(f)), byte] for (p, byte), f in zip(result["keys"], key_frames)]
    result["anchor_count"] = len(anchors)
    step = max(1, len(anchors) // 32)
    result["anchors"] = [[position(p, run_of_frame(f)), wall] for i, ((p, wall), f) in enumerate(zip(anchors, anchor_frames))
                         if i % step == 0 or i == len(anchors) - 1]
    # Existing bounded PS transport already has a compact timestamp codec.
    from .dahua_duration import DahuaTimestampPoint, _encode_timestamp_points
    run_bytes = [frame_bytes[r["first"]] for r in runs]
    points, previous = [], 0
    for byte, pts, dts, video in result.pop("timestamps"):
        index = max(0, bisect.bisect_right(run_bytes, byte - 9) - 1)
        run = runs[index]
        if len(runs) > 1:
            # Audio may reset a few packets before/after the video it is
            # interleaved with; it belongs to the neighbouring run whose PTS
            # range contains it. Anything else is not guessed.
            fits = [runs[i] for i in (index, index + 1, index - 1)
                    if 0 <= i < len(runs) and runs[i]["low"] - 5000 <= pts <= runs[i]["high"] + 5000]
            if not fits:
                raise ValueError("原生音视频包时间与录像分段不符，需复核")
            run = fits[0]
        value = max(0, round(logical(pts, run)))
        if value < previous:
            if previous-value > 100:
                raise ValueError("原生音视频包时间倒退，需复核")
            value = previous
        points.append(DahuaTimestampPoint(value,byte,dts,video))
        previous = value
    result["timestamp_data"] = _encode_timestamp_points(tuple(points))
    result["frame_count"] = len(frames)
    return result


def _clock_runs(frames, anchors, anchor_frames, frame_ms):
    """Split the stream at PTS resets and place every run by the recorder clock.

    Some recorders restart the stream PTS whenever a camera reconnects (for
    example every ~17 minutes, losing ~12.6 s of footage each time) while their
    per-frame clock stays continuous. Each run is checked on its own; runs may
    only move forward in recorder time. Anything else stays with OCR review.
    """
    starts = [0] + [i + 1 for i, (a, b) in enumerate(zip(frames, frames[1:])) if b - a < -100 or b - a > 10000]
    runs = []
    for first, stop in zip(starts, starts[1:] + [len(frames)]):
        lo, hi = bisect.bisect_left(anchor_frames, first), bisect.bisect_left(anchor_frames, stop)
        points = anchors[lo:hi]
        if not points:
            raise ValueError("原生录像 PTS 跳变且该段缺少画面时钟，转入分段 OCR 复核")
        if any(b[1] < a[1] or abs((b[1]-a[1])-(b[0]-a[0])) > 2000 for a, b in zip(points, points[1:])):
            raise ValueError("原生画面时钟跳变，转入分段 OCR 复核")
        offsets = [w - p for p, w in points]
        if max(offsets) - min(offsets) > 2000:
            raise ValueError("原生时钟相对 PTS 漂移，转入 OCR 复核")
        segment = frames[first:stop]
        base = segment[0]
        media_start = runs[-1]["media_end"] if runs else 0
        run = {"first": first, "base": base, "low": min(segment), "high": max(segment),
               "media_start": media_start, "media_end": media_start + max(segment) - base + frame_ms,
               "wall_start": base + offsets[0]}
        if runs:
            previous = runs[-1]
            gap = run["wall_start"] - (previous["wall_start"] + previous["media_end"] - previous["media_start"])
            if gap < -2000 or gap > MAX_RUN_GAP_MS:
                raise ValueError("原生录像 PTS 跳变处画面时钟不连续，转入分段 OCR 复核")
        runs.append(run)
    return runs


def native_runs(native):
    """(media_start, media_end, recorder wall start) per continuous run."""
    if native.get("runs"):
        return [tuple(float(v) for v in run) for run in native["runs"]]
    return [(0.0, float(native["duration_ms"]), float(native["wall_start"]))]


def native_wall_at(native, media_ms):
    """Recorder-clock time of a media position; positions keep their own run."""
    runs = native_runs(native)
    start, _, wall = next((run for run in runs if media_ms < run[1]), runs[-1])
    return wall + media_ms - start


def native_wall_end(native):
    start, end, wall = native_runs(native)[-1]
    return wall + end - start


def playback_index(path, native):
    """Reuse the proven callback transport; only in-memory packet copies change."""
    from .dahua_duration import (
        DahuaDurationIndex,
        DahuaPacketSummary,
        DahuaSeekPoint,
        _decode_timestamp_points,
    )
    return DahuaDurationIndex(str(Path(path).resolve()), native['source_size'], native['source_mtime_ns'],
                round(native['duration_ms']), 'native_ps', None, None, None,
                DahuaPacketSummary(native['frame_count'], native['frame_ms'], native['valid_end'], 1, 0, 0),
                native['frame_count'], tuple(DahuaSeekPoint(round(ms),pos) for ms,pos in native['keys']),
                _decode_timestamp_points(native['timestamp_data']))
