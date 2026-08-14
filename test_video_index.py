from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ui_helpers import BEIJING
from video_index import (
    VideoIndexEntry,
    build_video_index,
    matching_videos,
    review_target_ms,
)


class VideoIndexTests(unittest.TestCase):
    def test_build_uses_filename_time_and_reuses_duration_cache(self) -> None:
        calls: list[str] = []

        def probe(path) -> dict:
            calls.append(str(path))
            return {"format": {"duration": "12.5"}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "CAM01_2026-08-14_12-30-00.mp4"
            video.write_bytes(b"video")
            cache = root / "cache.json"
            expected = int(
                datetime(2026, 8, 14, 12, 30, tzinfo=BEIJING).timestamp()
                * 1000
            )

            first = build_video_index(root, expected, cache_path=cache, probe=probe)
            second = build_video_index(root, expected, cache_path=cache, probe=probe)

            self.assertEqual(len(first.entries), 1)
            self.assertEqual(first.entries[0].start_wall_ms, expected)
            self.assertEqual(first.entries[0].duration_ms, 12_500.0)
            self.assertEqual(len(second.entries), 1)
            self.assertEqual(len(calls), 1)

    def test_build_reports_files_without_a_parseable_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "untimed.mp4").write_bytes(b"video")
            result = build_video_index(
                root,
                None,
                probe=lambda _path: {"format": {"duration": "1"}},
            )
            self.assertEqual(result.scanned, 1)
            self.assertEqual(result.missing_time, 1)
            self.assertEqual(result.entries, ())

    def test_matching_orders_overlaps_by_closest_start(self) -> None:
        entries = [
            VideoIndexEntry("first.mp4", 1000, 1000.0, 1, 1),
            VideoIndexEntry("second.mp4", 1500, 2000.0, 1, 1),
        ]
        matches = matching_videos(entries, 1750, 2250)
        self.assertEqual(
            [item.path for item in matches], ["second.mp4", "first.mp4"]
        )

    def test_review_target_clamps_preroll_to_video_start(self) -> None:
        video = VideoIndexEntry("segment.mp4", 12_000, 20_000.0, 1, 1)
        self.assertEqual(
            review_target_ms(10_000, 3_000, video, preroll_ms=5_000),
            2_000.0,
        )


if __name__ == "__main__":
    unittest.main()
