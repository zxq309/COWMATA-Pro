from __future__ import annotations

import os
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

EMPTY_VIDEO_FILE = "empty"
ZERO_FILLED_VIDEO_PLACEHOLDER = "zero_filled"
SAMPLE_BYTES = 4_096


@lru_cache(maxsize=8_192)
def _video_placeholder_issue_for_identity(
    path: str,
    size: int,
    mtime_ns: int,
) -> str | None:
    del mtime_ns
    if size <= 0:
        return EMPTY_VIDEO_FILE
    sample_size = min(SAMPLE_BYTES, size)
    offsets = {
        0,
        max(0, (size - sample_size) // 2),
        max(0, size - sample_size),
    }
    try:
        with open(path, "rb", buffering=0) as source:
            sampled = False
            for offset in sorted(offsets):
                source.seek(offset)
                payload = source.read(sample_size)
                if not payload:
                    continue
                sampled = True
                if any(payload):
                    return None
    except OSError:
        return None
    return ZERO_FILLED_VIDEO_PLACEHOLDER if sampled else None


def video_placeholder_issue(
    path: str | os.PathLike[str],
) -> str | None:
    source = Path(path).resolve()
    try:
        stat = source.stat()
    except OSError:
        return None
    return _video_placeholder_issue_for_identity(
        str(source),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def neighboring_playable_video_paths(
    current_path: str | os.PathLike[str],
    files: Iterable[Path],
) -> tuple[Path | None, Path | None, int, int]:
    candidates = list(files)
    current_key = os.path.normcase(os.path.abspath(os.fspath(current_path)))
    current_index = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if os.path.normcase(os.path.abspath(candidate)) == current_key
        ),
        None,
    )
    if current_index is None:
        return None, None, 0, 0

    def find(direction: int) -> tuple[Path | None, int]:
        skipped = 0
        index = current_index + direction
        while 0 <= index < len(candidates):
            candidate = candidates[index]
            if video_placeholder_issue(candidate) is None:
                return candidate, skipped
            skipped += 1
            index += direction
        return None, skipped

    previous, previous_skipped = find(-1)
    following, following_skipped = find(1)
    return previous, following, previous_skipped, following_skipped


__all__ = [
    "EMPTY_VIDEO_FILE",
    "ZERO_FILLED_VIDEO_PLACEHOLDER",
    "neighboring_playable_video_paths",
    "video_placeholder_issue",
]
