"""Classified MP4 naming is the user's confirmed camera calendar anchor."""

from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path

from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment

from .clocks import wall_text
from .demand import camera_folder
from .video_names import filename_wall

SIGNATURE = "cowmata-classified-filename-2"


def metadata_from_name(path, relative, info, timeline=None):
    start = filename_wall(path)
    if start is None:
        return None
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("文件中没有视频流")
    duration = None
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
    if duration is None:
        raise ValueError("视频时长无法读取，请在数据准备中核对文件")
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
        time_basis="classified_filename",
        filename_anchor=Path(path).name,
        filename_wall_ms=start,
        intervals=[
            dict(
                wall_start=start,
                wall_end=start + duration,
                media_start=0,
                media_end=duration,
                verified=True,
                warnings=[],
            )
        ],
        samples=[],
        warnings=[],
        needs_review=False,
        preview="",
        start_display=wall_text(start, filename=True),
    )
    if timeline.discontinuities:
        result["needs_review"] = True
        result["warnings"] = ["视频数据包时钟不连续，请在数据准备中核对"]
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
        "intervals": [
            dict(
                wall_start=start,
                wall_end=start + duration,
                media_start=0,
                media_end=duration,
                verified=not metadata.get("needs_review", False),
                warnings=metadata.get("warnings", []),
            )
        ],
    }
