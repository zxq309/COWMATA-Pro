from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from media_timeline import (
    PacketTimestamp,
    build_timeline_index,
    load_timeline_cache,
    parse_ffprobe_packets,
    save_timeline_cache,
)


class MediaTimelineIndexTests(unittest.TestCase):
    def _source(self, directory: str) -> Path:
        source = Path(directory) / "sample.ps"
        source.write_bytes(b"\x00\x00\x01\xba" + b"x" * 64)
        return source

    def test_continuous_packets_keep_identity_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(directory)
            packets = [
                PacketTimestamp(float(value), 100.0)
                for value in (20_000, 20_100, 20_200, 20_300)
            ]

            index = build_timeline_index(source, packets)

            self.assertFalse(index.is_corrected)
            self.assertEqual(len(index.segments), 1)
            self.assertAlmostEqual(index.duration_ms, 400.0)
            raw, segment = index.public_to_raw_ms(250.0)
            self.assertAlmostEqual(raw, 250.0)
            self.assertEqual(segment, 0)

    def test_large_pts_gap_is_collapsed_bidirectionally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(directory)
            packets = [
                PacketTimestamp(value, 100.0)
                for value in (
                    [20_000.0, 20_100.0, 20_200.0]
                    + [6_320_200.0 + offset for offset in range(0, 6_000, 100)]
                )
            ]

            index = build_timeline_index(source, packets)

            self.assertTrue(index.is_corrected)
            self.assertEqual(len(index.discontinuities), 1)
            self.assertEqual(len(index.segments), 2)
            self.assertAlmostEqual(index.raw_duration_ms, 6_306_200.0)
            self.assertAlmostEqual(index.duration_ms, 6_300.0)
            raw, segment = index.public_to_raw_ms(350.0)
            self.assertAlmostEqual(raw, 6_300_250.0)
            self.assertEqual(segment, 1)
            public, segment = index.raw_to_public_ms(raw, segment)
            self.assertAlmostEqual(public, 350.0)
            self.assertEqual(segment, 1)

    def test_short_fragment_after_gap_is_not_seekable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(directory)
            index = build_timeline_index(
                source,
                [
                    PacketTimestamp(20_000.0, 100.0),
                    PacketTimestamp(20_100.0, 100.0),
                    PacketTimestamp(20_200.0, 100.0),
                    PacketTimestamp(6_320_200.0, 100.0),
                    PacketTimestamp(6_320_300.0, 100.0),
                ],
            )

            self.assertTrue(index.is_corrected)
            self.assertAlmostEqual(index.duration_ms, 300.0)
            raw, segment = index.public_to_raw_ms(index.duration_ms)
            self.assertAlmostEqual(raw, 300.0)
            self.assertEqual(segment, 0)
            public, segment = index.raw_to_public_ms(6_300_250.0)
            self.assertAlmostEqual(public, 300.0)
            self.assertEqual(segment, 1)

    def test_backward_reset_is_left_to_player_without_unsafe_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(directory)
            index = build_timeline_index(
                source,
                [
                    PacketTimestamp(700_000.0, 100.0),
                    PacketTimestamp(700_100.0, 100.0),
                    PacketTimestamp(100.0, 100.0),
                    PacketTimestamp(200.0, 100.0),
                ],
            )

            self.assertFalse(index.is_corrected)
            self.assertEqual(len(index.segments), 1)

    def test_cache_round_trip_requires_unchanged_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(directory)
            cache = Path(directory) / "cache"
            index = build_timeline_index(
                source,
                [
                    PacketTimestamp(0.0, 100.0),
                    PacketTimestamp(100.0, 100.0),
                ],
            )
            save_timeline_cache(cache, index)

            loaded = load_timeline_cache(cache, source)
            self.assertEqual(loaded, index)

            source.write_bytes(source.read_bytes() + b"changed")
            self.assertIsNone(load_timeline_cache(cache, source))

    def test_ffprobe_parser_uses_dts_when_pts_is_missing(self) -> None:
        packets = parse_ffprobe_packets(
            [
                "pts_time=N/A|dts_time=12.500000|"
                "duration_time=0.080000|pos=144",
                "pts_time=12.580000|dts_time=12.580000|"
                "duration_time=0.080000|pos=N/A",
            ]
        )

        self.assertEqual(len(packets), 2)
        self.assertAlmostEqual(packets[0].pts_ms, 12_500.0)
        self.assertAlmostEqual(packets[0].duration_ms, 80.0)
        self.assertEqual(packets[0].position, 144)
        self.assertIsNone(packets[1].position)


if __name__ == "__main__":
    unittest.main()
