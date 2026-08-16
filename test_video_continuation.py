from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from dual_anchor_precision_v2_mixin import DualAnchorPrecisionV2Mixin


class _FakeMedia:
    def __init__(self, path: str) -> None:
        self.current_path = os.path.abspath(path)
        self.video_ms = 1_000_000.0
        self.playing = False
        self.duration = 973_000.0

    def is_playing(self) -> bool:
        return self.playing

    def get_time_ms(self) -> float:
        return self.video_ms

    def duration_ms(self) -> float:
        return self.duration

    def set_time_ms(self, value: float) -> bool:
        self.video_ms = float(value)
        return True

    def pause(self, paused: bool = True) -> None:
        self.playing = not paused

    def play(self) -> bool:
        self.playing = True
        return True


class _FakePlot:
    def __init__(self, view: tuple[float, float]) -> None:
        self.view_range = view

    def set_view(self, start: float, end: float) -> None:
        self.view_range = (start, end)


class _FakeStatusBar:
    def __init__(self) -> None:
        self.message = ""

    def showMessage(self, message: str, _duration: int) -> None:  # noqa: N802
        self.message = message


class _FakeTimeline:
    def __init__(self) -> None:
        self.position = 0.0

    def set_position(self, value: float) -> None:
        self.position = float(value)


class _VideoOpenBase:
    def open_video(self, path: str | None = None) -> None:
        if path is None:
            return
        if os.path.basename(path) == "raise.mp4":
            raise RuntimeError("simulated video open failure")
        normalized = os.path.abspath(path)
        self.media.current_path = normalized
        self.media.video_ms = 0.0
        self.video_path = normalized
        self.playhead_ms = 0.0
        self.plot.view_range = (0.0, 3_600_000.0)
        self.video_start_wall_ms = self.data_create_time_ms
        self.align_method = "default"
        self._media_primed = False

    def open_json(self, path: str | None = None) -> None:
        if path is None:
            return
        if os.path.basename(path) == "raise.json":
            raise RuntimeError("simulated JSON open failure")
        self.data = SimpleNamespace(name=os.path.basename(path))
        self.data_path = os.path.abspath(path)
        self.data_create_time_ms += int(self.data_duration_ms)
        self.playhead_ms = 0.0
        self.video_start_wall_ms = self.data_create_time_ms
        self.align_method = "default"

    def set_playhead(self, value: float, seek_video: bool = True) -> None:
        self.playhead_ms = float(value)
        self.seek_requested = bool(seek_video)

    def _set_playhead_visual(self, value: float) -> None:
        self.playhead_ms = float(value)

    def _update_alignment_status(self) -> None:
        self.alignment_refreshed = True

    def _clear_ui_seek(self) -> None:
        self.seek_cleared = True

    def _finish_media_priming(self) -> None:
        self.priming_finished = True


class _Harness(DualAnchorPrecisionV2Mixin, _VideoOpenBase):
    def __init__(self, old_path: str) -> None:
        self._timelines_linked = True
        self._source_switch_active = False
        self._source_switch_kind = ""
        self._source_switch_snapshot = {}
        self._source_switch_mapping_committed = False
        self._pending_data_anchor_ms = None
        self._pending_video_anchor_ms = None
        self._independent_video_selection_active = False
        self._play_requested_after_anchor_confirmation = False
        self._media_primed = True
        self._restoring_project = False
        self.data = SimpleNamespace(name="old")
        self.data_path = os.path.abspath("old.json")
        self.data_create_time_ms = 1_800_000_000_000
        self.data_duration_ms = 3_599_969.0
        self.video_path = os.path.abspath(old_path)
        self.media = _FakeMedia(old_path)
        self.playhead_ms = 545_980.0
        self.plot = _FakePlot((480_000.0, 600_000.0))
        self.video_timeline = _FakeTimeline()
        self.video_start_wall_ms = self.data_create_time_ms
        self.align_method = "pin"
        self.seek_requested = False
        self.seek_cleared = False
        self.priming_finished = False
        self.alignment_refreshed = False
        self._status_bar = _FakeStatusBar()

    def statusBar(self) -> _FakeStatusBar:  # noqa: N802
        return self._status_bar

    def _refresh_enabled(self) -> None:
        pass

    def _apply_rate(self) -> None:
        pass

    def _update_alignment_status(self) -> None:
        self.alignment_refreshed = True


class VideoContinuationTest(unittest.TestCase):
    def test_switch_keeps_data_cursor_and_view(self) -> None:
        window = _Harness("hiv00000.mp4")

        window.open_video("hiv00001.mp4")

        self.assertEqual(window.playhead_ms, 545_980.0)
        self.assertEqual(window.plot.view_range, (480_000.0, 600_000.0))
        self.assertEqual(
            window.video_start_wall_ms,
            window.data_create_time_ms + 545_980,
        )
        self.assertEqual(window.align_method, "continuation")
        self.assertTrue(window.seek_requested)
        self.assertTrue(window.seek_cleared)
        self.assertTrue(window.alignment_refreshed)
        self.assertIn("00:09:05.980", window.statusBar().message)
        self.assertTrue(window._source_switch_active)

        window._media_primed = True
        window._complete_video_source_switch()

        self.assertFalse(window._source_switch_active)
        self.assertTrue(window._timelines_are_linked())
        self.assertIn("续接完成", window.statusBar().message)

    def test_project_restore_does_not_apply_segment_continuation(self) -> None:
        window = _Harness("hiv00000.mp4")
        window._restoring_project = True

        window.open_video("hiv00001.mp4")

        self.assertEqual(window.playhead_ms, 0.0)
        self.assertEqual(window.align_method, "default")

    def test_paused_video_recovers_when_vlc_omits_pause_signal(self) -> None:
        window = _Harness("hiv00000.mp4")
        window.open_video("hiv00001.mp4")
        expected = os.path.normcase(os.path.abspath("hiv00001.mp4"))

        self.assertTrue(window._source_switch_active)
        self.assertFalse(window._media_primed)
        window._recover_video_source_switch(expected, retries_left=12)

        self.assertTrue(window._media_primed)
        self.assertTrue(window.priming_finished)
        self.assertFalse(window._source_switch_active)
        self.assertTrue(window._timelines_are_linked())
        self.assertIn("续接完成", window.statusBar().message)

    def test_switch_json_keeps_video_position_and_starts_new_data_at_zero(self) -> None:
        window = _Harness("hiv00000.mp4")
        old_video_ms = window.media.get_time_ms()

        window.open_json("new.json")

        self.assertEqual(window.playhead_ms, 0.0)
        self.assertEqual(window.media.get_time_ms(), old_video_ms)
        self.assertEqual(
            window.video_start_wall_ms,
            window.data_create_time_ms - old_video_ms,
        )
        self.assertEqual(window.align_method, "data_continuation")
        self.assertTrue(window._timelines_are_linked())

    def test_switch_video_preserves_unlocked_calibration_mode(self) -> None:
        window = _Harness("hiv00000.mp4")
        window._timelines_linked = False

        window.open_video("hiv00001.mp4")
        window._media_primed = True
        window._complete_video_source_switch()

        self.assertEqual(window.playhead_ms, 545_980.0)
        self.assertFalse(window._source_switch_active)
        self.assertFalse(window._timelines_are_linked())

    def test_switch_json_preserves_unlocked_calibration_mode(self) -> None:
        window = _Harness("hiv00000.mp4")
        window._timelines_linked = False
        old_video_ms = window.media.get_time_ms()

        window.open_json("new.json")

        self.assertEqual(window.playhead_ms, 0.0)
        self.assertEqual(window.media.get_time_ms(), old_video_ms)
        self.assertFalse(window._source_switch_active)
        self.assertFalse(window._timelines_are_linked())

    def test_cancelled_video_switch_restores_positions_and_playback(self) -> None:
        window = _Harness("hiv00000.mp4")
        window.media.playing = True
        original = (
            window.playhead_ms,
            window.plot.view_range,
            window.media.get_time_ms(),
            window.video_start_wall_ms,
            window.align_method,
        )

        window.open_video(None)

        self.assertEqual(
            (
                window.playhead_ms,
                window.plot.view_range,
                window.media.get_time_ms(),
                window.video_start_wall_ms,
                window.align_method,
            ),
            original,
        )
        self.assertFalse(window._source_switch_active)
        self.assertTrue(window.media.is_playing())

    def test_failed_json_switch_restores_positions_and_playback(self) -> None:
        window = _Harness("hiv00000.mp4")
        window.media.playing = True
        original = (
            window.playhead_ms,
            window.plot.view_range,
            window.media.get_time_ms(),
            window.video_start_wall_ms,
            window.align_method,
        )

        with self.assertRaisesRegex(RuntimeError, "JSON open failure"):
            window.open_json("raise.json")

        self.assertEqual(
            (
                window.playhead_ms,
                window.plot.view_range,
                window.media.get_time_ms(),
                window.video_start_wall_ms,
                window.align_method,
            ),
            original,
        )
        self.assertFalse(window._source_switch_active)
        self.assertTrue(window.media.is_playing())


if __name__ == "__main__":
    unittest.main()
