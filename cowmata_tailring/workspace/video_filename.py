"""Classified MP4 naming is the user's confirmed camera calendar anchor."""

from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path

from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment

from .clocks import wall_text
from .demand import camera_folder
from .video_names import adjacent_filename_duration, filename_wall

SIGNATURE = "cowmata-classified-filename-2"


def named_intervals(start, duration, native=None, *, verified=True, warnings=()):
    """Spans anchored at the confirmed filename time.

    A recorder that restarted its stream inside the file (see
    native_ps._clock_runs) contributes one span per run, offset by its own
    clock, so footage after each restart keeps its real time.
    """
    from cowmata_tailring.media.native_ps import native_runs

    runs = native_runs(native) if native and native.get("runs") else [(0.0, float(duration), 0.0)]
    origin = runs[0][2]
    return [dict(wall_start=start + wall - origin, wall_end=start + wall - origin + media_end - media_start,
                 media_start=media_start, media_end=media_end, verified=verified, warnings=list(warnings))
            for media_start, media_end, wall in runs]


def metadata_from_name(path, relative, info, timeline=None):
    start = filename_wall(path)
    if start is None:
        return None
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("文件中没有视频流")
    duration = None
    duration_basis = "timeline" if timeline else "header"
    for value in (
        (timeline.duration_ms / 1000 if timeline else None),
        video.get("duration"),
        info.get("format", {}).get("duration"),
    ):
        try:
            duration = float(value) * 1000
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            break
        duration = None
    # Recorder MPEG-PS streams often report a bogus multi-hour header.  A
    # neighbouring filename gives a bounded browse hint while keeping the
    # record explicitly pending manual time verification.
    format_name = str(info.get("format", {}).get("format_name", ""))
    if not timeline and "mpeg" in format_name and "mp4" not in format_name:
        hint = adjacent_filename_duration(path)
        if hint is not None and (duration is None or duration > 6 * 60 * 60 * 1000):
            duration = hint
            duration_basis = "adjacent_filename"
    if duration is None:
        raise ValueError("视频时长无法读取：文件可能没下载完或已损坏。请重新下载这段录像，或用「下载 → 数据归类 → 录像转码与归类」重新处理")
    frame_ms = 40.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            rate = float(Fraction(video.get(key, "0")))
            if math.isfinite(rate) and rate > 0:
                frame_ms = 1000 / rate
                break
        except (ValueError, TypeError, ZeroDivisionError):
            pass
    stat = Path(path).stat()
    timeline = timeline or MediaTimelineIndex(
        str(path),
        stat.st_size,
        stat.st_mtime_ns,
        0,
        frame_ms,
        (TimelineSegment(0, duration, 0, duration),),
        (),
    )
    result = dict(
        camera=camera_folder(str(relative).replace("\\", "/")),
        duration_ms=duration,
        width=video.get("width"),
        height=video.get("height"),
        codec=video.get("codec_name"),
        format=info.get("format", {}).get("format_name"),
        header_duration=info.get("format", {}).get("duration"),
        timeline=timeline.to_dict(),
        time_engine=SIGNATURE,
        version=SIGNATURE,
        time_basis="classified_filename",
        filename_anchor=Path(path).name,
        filename_wall_ms=start,
        intervals=named_intervals(start, duration, timeline.native),
        samples=[],
        warnings=[],
        needs_review=duration_basis == "adjacent_filename",
        preview="",
        start_display=wall_text(start, filename=True),
        duration_basis=duration_basis,
    )
    if duration_basis == "adjacent_filename":
        result["warnings"] = ["文件名相邻片段仅提供浏览时长；录像机时钟仍需核验"]
        result["intervals"][0]["verified"] = False
        result["intervals"][0]["warnings"] = list(result["warnings"])
    if timeline.discontinuities:
        result["needs_review"] = True
        result["warnings"] = ["视频数据包时钟不连续（录像机中途断流或校时），需人工核验时间：左侧“核验” → 选中这段录像 → 核验所选视频时间"]
        for interval in result["intervals"]:
            interval["verified"] = False
            interval["warnings"] = result["warnings"]
    return result


def bind_filename_location(metadata, relative, stamp):
    """Filename anchors belong to locations even when bytes share one SHA-256."""
    if metadata.get("time_engine") != SIGNATURE or metadata.get("manual_readings"):
        return metadata
    start = filename_wall(relative)
    if start is None:
        return metadata
    duration = metadata.get("duration_ms", 0)
    import json

    size, mtime = json.loads(stamp)[:2]
    timeline = metadata.get("timeline", {})
    return {
        **metadata,
        "camera": camera_folder(str(relative).replace("\\", "/")),
        "filename_anchor": Path(relative).name,
        "filename_wall_ms": start,
        "start_display": wall_text(start, filename=True),
        "timeline": {
            **timeline,
            "source": {**timeline.get("source", {}), "size": size, "mtimeNs": mtime},
        },
        "intervals": named_intervals(start, duration, timeline.get("native"),
                                     verified=not metadata.get("needs_review", False),
                                     warnings=metadata.get("warnings", [])),
    }
