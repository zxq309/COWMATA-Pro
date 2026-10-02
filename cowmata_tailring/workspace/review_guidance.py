"""Plain-language review states and which recordings still need a time check.

Internal codes stay unchanged in saved files; only what operators read changes.
"""
from __future__ import annotations

from pathlib import PurePosixPath

CONFIRMATION_TEXT = {
    "confirmed": "已确认",
    "needs_review": "需复核",
    "legacy_unreviewed": "旧标注·待确认",
    "video_draft": "视频草稿",
}

SOURCE_STATE_TEXT = {"pending": "未索引", "ready": "可用", "review": "待复核", "invalid": "异常",
                     "ignored": "已忽略", "missing": "缺失"}


def confirmation_text(code):
    return CONFIRMATION_TEXT.get(code or "legacy_unreviewed", str(code))


def effective_intervals(row):
    """Intervals the player uses, after any saved manual readings."""
    metadata = row.get("metadata") or {}
    readings = metadata.get("manual_readings")
    # Readings that agree with a recorder-verified clock keep the recorder spans.
    if readings and metadata.get("duration_ms") and not metadata.get("manual_readings_agree"):
        from .clocks import manual_video_metadata
        try:
            return manual_video_metadata(metadata, readings).get("intervals", [])
        except (ValueError, KeyError, TypeError):
            return []
    return metadata.get("intervals", [])


def needs_time_check(row):
    """A playable recording whose picture time is not verified, so labels on it cannot be confirmed."""
    if row.get("kind") != "video" or row.get("state") not in {"ready", "review"}:
        return False
    intervals = effective_intervals(row)
    return row.get("state") == "review" or not intervals or any(not item.get("verified") for item in intervals)


def verified_share(row):
    """Share of the recording (0..1) whose time mapping is verified."""
    duration = float((row.get("metadata") or {}).get("duration_ms") or 0)
    if duration <= 0:
        return 0.0
    covered = sum(max(0.0, float(i["media_end"]) - float(i["media_start"]))
                  for i in effective_intervals(row) if i.get("verified"))
    return max(0.0, min(1.0, covered / duration))


def file_label(row):
    return PurePosixPath(str(row.get("path", "")).replace("\\", "/")).name or str(row.get("path", ""))
