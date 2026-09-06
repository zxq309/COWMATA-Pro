"""Whole-project coverage and conservative, calibrated IMU handoff decisions."""
from __future__ import annotations

from dataclasses import dataclass

from .clocks import ClockMap, VideoTimeline


@dataclass(frozen=True)
class CoverageStatus:
    code: str
    cameras: tuple[str, ...] = ()
    next_ms: float | None = None


def video_coverage(timeline: VideoTimeline, rows, reference_ms, selected, *, scan_complete, aligned):
    """Never equate a filtered view, unreadable OSD, or partial scan with no video."""
    matches = tuple(camera for camera in timeline.cameras if timeline.locate(camera, reference_ms))
    if matches:
        return CoverageStatus("covered" if set(matches) & set(selected) else "other_views", matches)
    if not scan_complete or any(row["kind"] == "video" and row["state"] == "pending" for row in rows):
        return CoverageStatus("indexing")
    videos = [row for row in rows if row["kind"] == "video" and row["state"] not in {"missing", "ignored"}]
    if not videos:
        return CoverageStatus("no_videos")
    # A reviewed middle span alone cannot prove anything about an unread tail.
    for row in videos:
        intervals = row["metadata"].get("intervals", [])
        duration = row["metadata"].get("duration_ms", 0)
        tolerance = row["metadata"].get("timeline", {}).get("frameDurationMs", 100)
        if (row["state"] != "ready" or not intervals or duration <= 0
                or min(span["media_start"] for span in intervals) > tolerance
                or max(span["media_end"] for span in intervals) < duration - tolerance):
            return CoverageStatus("unresolved_video")
    if not aligned:
        return CoverageStatus("alignment_unknown")
    starts = [timeline.next_start(camera, reference_ms) for camera in timeline.cameras]
    future = [value for value in starts if value is not None]
    if future:
        return CoverageStatus("gap", next_ms=min(future))
    bounds = timeline.bounds()
    return CoverageStatus("exhausted" if bounds and reference_ms >= bounds[1] else "unresolved_video")


def continuation_target(rows, current_asset, device, cow_id, reference_ms, load_work):
    """Return only one unambiguous same-cow recording covering this calibrated time.

    create_time is deliberately absent from this decision. It can order a browser
    list but cannot justify joining or stretching independently uploaded records.
    """
    if not cow_id:
        return None, "cow_unconfirmed"
    matches = []
    seen = {current_asset}
    for row in rows:
        if (row["kind"] != "imu" or row["state"] != "ready" or not row["asset_id"]
                or row["asset_id"] in seen or row["metadata"].get("device") != device):
            continue
        seen.add(row["asset_id"])
        work = load_work(row["asset_id"])
        if not work or work.get("project", {}).get("cow_id") != cow_id:
            continue
        try:
            clock = ClockMap.from_dict(work.get("clock", {}))
            if len(clock.anchors) < 2:
                continue
            source_ms = clock.map(reference_ms, inverse=True)
            if 0 <= source_ms <= row["metadata"].get("duration_ms", 0) and clock.quality(source_ms) == "interpolated":
                matches.append(row)
        except (ValueError, TypeError, KeyError):
            continue
    if len(matches) == 1:
        return matches[0], "ready"
    return None, "ambiguous" if matches else "no_calibrated_record"
