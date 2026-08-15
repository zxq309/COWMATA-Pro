from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import video_index
from hikvision_index import (
    HikvisionSegment,
    is_hikvision_storage_directory,
    parse_osd_datetime_text,
)
from ui_helpers import BEIJING
from video_index import VideoIndexCancelled, build_video_index


class HikvisionTimestampTests(unittest.TestCase):
    def test_parse_tolerates_extra_hour_digit_and_bad_second_separator(self) -> None:
        self.assertEqual(
            parse_osd_datetime_text("2026年07月29日116:58:06"),
            datetime(2026, 7, 29, 16, 58, 6, tzinfo=BEIJING),
        )
        self.assertEqual(
            parse_osd_datetime_text("2026年07月29日18:11日53"),
            datetime(2026, 7, 29, 18, 11, 53, tzinfo=BEIJING),
        )

    def test_hikvision_directory_detection_requires_raw_block_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "HIKWS"
            marker.write_bytes(b"HIK")
            video = root / "hiv00000.mp4"
            video.write_bytes(b"\x00\x00\x01\xba")
            self.assertTrue(is_hikvision_storage_directory(root, [video]))


class HikvisionBuildTests(unittest.TestCase):
    def test_build_uses_osd_segments_and_reuses_cache(self) -> None:
        segment = HikvisionSegment(
            start_wall_ms=1_785_313_086_000,
            duration_ms=10_000.0,
            playback_offset_ms=2_000.0,
            channel="通道1",
            width=2560,
            height=1440,
            ocr_confidence=0.95,
            ocr_text="2026年07月29日16:58:06",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "HIKWS").write_bytes(b"HIK")
            video = root / "hiv00000.mp4"
            video.write_bytes(b"\x00\x00\x01\xba" + b"video")
            empty = root / "hiv00001.mp4"
            empty.write_bytes(b"\x00" * 16)
            cache = root / "cache.json"

            with (
                patch.object(video_index, "create_ocr_engine", return_value=object()),
                patch.object(
                    video_index,
                    "index_hikvision_file",
                    return_value=[segment],
                ) as index_file,
            ):
                first = build_video_index(root, None, cache_path=cache)
                second = build_video_index(root, None, cache_path=cache)

            self.assertEqual(index_file.call_count, 1)
            self.assertEqual(first.index_kind, "hikvision_osd")
            self.assertEqual(first.channels, ("通道1",))
            self.assertEqual(first.missing_time, 1)
            self.assertEqual(len(second.entries), 1)
            entry = second.entries[0]
            self.assertEqual(entry.playback_offset_ms, 2_000.0)
            self.assertEqual(
                entry.effective_video_start_wall_ms,
                segment.start_wall_ms - 2_000.0,
            )

    def test_cancel_writes_partial_cache_and_next_scan_resumes(self) -> None:
        segment = HikvisionSegment(
            start_wall_ms=1_785_313_086_000,
            duration_ms=10_000.0,
            playback_offset_ms=0.0,
            channel="通道1",
            width=2560,
            height=1440,
            ocr_confidence=0.95,
            ocr_text="2026年07月29日16:58:06",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "HIKWS").write_bytes(b"HIK")
            for index in range(3):
                (root / f"hiv{index:05d}.mp4").write_bytes(
                    b"\x00\x00\x01\xba" + bytes([index])
                )
            cache = root / "cache.json"
            cancel_checks = 0

            def cancelled() -> bool:
                nonlocal cancel_checks
                cancel_checks += 1
                return cancel_checks >= 2

            with (
                patch.object(video_index, "create_ocr_engine", return_value=object()),
                patch.object(
                    video_index,
                    "index_hikvision_file",
                    return_value=[segment],
                ) as first_index,
            ):
                with self.assertRaises(VideoIndexCancelled):
                    build_video_index(
                        root,
                        None,
                        cache_path=cache,
                        cancelled=cancelled,
                    )

            self.assertEqual(first_index.call_count, 1)
            self.assertTrue(cache.is_file())

            with (
                patch.object(video_index, "create_ocr_engine", return_value=object()),
                patch.object(
                    video_index,
                    "index_hikvision_file",
                    return_value=[segment],
                ) as resumed_index,
            ):
                resumed = build_video_index(root, None, cache_path=cache)

            self.assertEqual(resumed_index.call_count, 2)
            self.assertEqual(
                [Path(call.args[0]).name for call in resumed_index.call_args_list],
                ["hiv00001.mp4", "hiv00002.mp4"],
            )
            self.assertEqual(len(resumed.entries), 3)


if __name__ == "__main__":
    unittest.main()
