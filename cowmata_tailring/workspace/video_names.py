"""Strict classified MP4 filename parser without media or UI dependencies."""

import re
from datetime import datetime
from pathlib import Path

_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})(?:__\d{3})?$", re.ASCII)


def filename_wall(path):
    path = Path(path)
    if path.suffix.lower() != ".mp4":
        return None
    match = _PATTERN.fullmatch(path.stem)
    if not match:
        return None
    try:
        return (
            datetime.strptime(match[1] + " " + match[2], "%Y-%m-%d %H-%M-%S") - datetime(1970, 1, 1)
        ).total_seconds() * 1000
    except ValueError:
        return None


def adjacent_filename_duration(path, *, maximum_ms=6 * 60 * 60 * 1000):
    """Return a bounded browse-only duration from the next named clip.

    Dahua recordings are sometimes MPEG-PS payloads carrying an ``.mp4``
    suffix.  Their FFprobe header duration can be wildly wrong (for example,
    20 hours for a 34-minute segment).  The next distinct filename timestamp
    is a safe routing hint, but is deliberately not treated as verified clock
    evidence.
    """
    path = Path(path)
    start = filename_wall(path)
    if start is None or not path.parent.is_dir():
        return None
    candidates = []
    try:
        for sibling in path.parent.iterdir():
            if not sibling.is_file() or sibling.suffix.lower() != ".mp4":
                continue
            value = filename_wall(sibling)
            if value is not None and value > start:
                candidates.append(value)
    except OSError:
        return None
    if not candidates:
        return None
    duration = min(candidates) - start
    return duration if 1000 <= duration <= maximum_ms else None
