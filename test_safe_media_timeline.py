from __future__ import annotations

import unittest

from media_timeline import (
    MediaTimelineIndex,
    TimelineDiscontinuity,
    TimelineSegment,
)
from safe_media_engine import (
    _effective_timeline_duration_ms,
    _fractional_seek_clock,
    _uses_fractional_timeline_seek,
)


def _index(
    duration_ms: float,
    *,
    raw_duration_ms: float | None = None,
) -> MediaTimelineIndex:
    raw_duration = (
        duration_ms if raw_duration_ms is None else raw_duration_ms
    )
    corrected = raw_duration_ms is not None
    segments = (
        TimelineSegment(0.0, duration_ms, 0.0, duration_ms),
    )
    discontinuities = ()
    if corrected:
        segments = (
            TimelineSegment(0.0, duration_ms, 0.0, duration_ms),
            TimelineSegment(
                raw_duration,
                raw_duration,
                duration_ms,
                duration_ms,
            ),
        )
        discontinuities = (
            TimelineDiscontinuity(
                packet_index=1,
                previous_pts_ms=duration_ms,
                current_pts_ms=raw_duration,
                delta_ms=raw_duration - duration_ms,
            ),
        )
    return MediaTimelineIndex(
        source_path="hiv00086.mp4",
        source_size=268_435_456,
        source_mtime_ns=1,
        first_pts_ms=0.0,
        frame_duration_ms=80.0,
        segments=segments,
        discontinuities=discontinuities,
    )


class SafeMediaTimelineTests(unittest.TestCase):
    def test_continuous_packet_duration_extends_short_player_duration(self) -> None:
        index = _index(973_631.0)

        self.assertEqual(
            _effective_timeline_duration_ms(245_420, index),
            973_631,
        )
        self.assertTrue(_uses_fractional_timeline_seek(245_420, index))

    def test_continuous_packet_duration_never_shortens_player_duration(self) -> None:
        index = _index(245_420.0)

        self.assertEqual(
            _effective_timeline_duration_ms(973_631, index),
            973_631,
        )
        self.assertFalse(_uses_fractional_timeline_seek(973_631, index))

    def test_gap_corrected_timeline_keeps_collapsed_duration(self) -> None:
        index = _index(968_196.0, raw_duration_ms=25_009_368.0)

        self.assertTrue(index.is_corrected)
        self.assertEqual(
            _effective_timeline_duration_ms(25_009_368, index),
            968_196,
        )
        self.assertFalse(
            _uses_fractional_timeline_seek(25_009_368, index)
        )

    def test_fractional_seek_preserves_true_logical_target(self) -> None:
        fraction, offset = _fractional_seek_clock(
            600_000,
            245_420,
            973_631,
        )

        self.assertAlmostEqual(fraction, 600_000 / 973_631)
        self.assertAlmostEqual(245_420 * fraction + offset, 600_000)


if __name__ == "__main__":
    unittest.main()
