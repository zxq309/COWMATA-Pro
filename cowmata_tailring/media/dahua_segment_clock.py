from __future__ import annotations

import os
import re
from pathlib import Path

from cowmata_tailring.media.dahua_duration import is_dahua_program_stream

MIN_SEGMENT_DURATION_MS = 60_000
MAX_SEGMENT_DURATION_MS = 6 * 60 * 60 * 1000
SEGMENT_DURATION_MATCH_ABSOLUTE_MS = 5_000
SEGMENT_DURATION_MATCH_RELATIVE = 0.08

_NUMBERED_RECORDING = re.compile(r"(.*?)(\d+)(\.[^.]+)")


def _numbered_recording_identity(path: Path) -> tuple[str, int, str] | None:
    match = _NUMBERED_RECORDING.fullmatch(path.name)
    if match is None:
        return None
    prefix, digits, suffix = match.groups()
    return prefix.casefold(), int(digits), suffix.casefold()


def dahua_segment_clock_delta_ms(
    source_path: str | os.PathLike[str],
    target_path: str | os.PathLike[str],
    source_duration_ms: int | float,
    target_duration_ms: int | float = 0,
) -> int | None:
    """Return a validated relative start-time delta for adjacent recordings."""

    source = Path(source_path).resolve()
    target = Path(target_path).resolve()
    if os.path.normcase(str(source.parent)) != os.path.normcase(
        str(target.parent)
    ):
        return None

    source_identity = _numbered_recording_identity(source)
    target_identity = _numbered_recording_identity(target)
    if source_identity is None or target_identity is None:
        return None
    source_prefix, source_number, source_suffix = source_identity
    target_prefix, target_number, target_suffix = target_identity
    number_delta = target_number - source_number
    if (
        source_prefix != target_prefix
        or source_suffix != target_suffix
        or abs(number_delta) != 1
    ):
        return None

    reference_duration = int(
        round(
            float(
                source_duration_ms
                if number_delta > 0
                else target_duration_ms
            )
        )
    )
    if not MIN_SEGMENT_DURATION_MS <= reference_duration <= MAX_SEGMENT_DURATION_MS:
        return None
    if not is_dahua_program_stream(source) or not is_dahua_program_stream(target):
        return None

    try:
        delta_ms = int(
            round(
                (target.stat().st_mtime_ns - source.stat().st_mtime_ns)
                / 1_000_000.0
            )
        )
    except OSError:
        return None
    absolute_delta = abs(delta_ms)
    if (
        not MIN_SEGMENT_DURATION_MS
        <= absolute_delta
        <= MAX_SEGMENT_DURATION_MS
        or delta_ms * number_delta <= 0
    ):
        return None

    tolerance_ms = max(
        SEGMENT_DURATION_MATCH_ABSOLUTE_MS,
        int(round(reference_duration * SEGMENT_DURATION_MATCH_RELATIVE)),
    )
    if abs(absolute_delta - reference_duration) > tolerance_ms:
        return None
    return delta_ms


__all__ = ["dahua_segment_clock_delta_ms"]
