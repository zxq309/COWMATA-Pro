from __future__ import annotations

import unittest

from dual_anchor_precision_v2_mixin import DualAnchorPrecisionV2Mixin
from playback_mixin import PlaybackMixin


class _FakeMedia:
    def __init__(self, video_ms: float = 4_000.0) -> None:
        self.video_ms = float(video_ms)
        self.seek_calls: list[float] = []
        self.playing = False

    def is_playing(self) -> bool:
        return self.playing

    def get_time_ms(self) -> float:
        return self.video_ms

    def set_time_ms(self, value: float) -> bool:
        self.video_ms = float(value)
        self.seek_calls.append(float(value))
        return True

    def duration_ms(self) -> float:
        return 3_600_000.0

    def pause(self, paused: bool = True) -> None:
        self.playing = not paused

    def play(self) -> bool:
        self.playing = True
        return True


class _FakePlot:
    @staticmethod
    def snap_time(value: float) -> float:
        return float(value)


class _FakeStatusBar:
    def __init__(self) -> None:
        self.message = ""

    def showMessage(self, message: str, _duration: int) -> None:  # noqa: N802
        self.message = str(message)


class _FakeTimeline:
    def __init__(self) -> None:
        self.position = 0.0

    def set_position(self, value: float) -> None:
        self.position = float(value)


class _AlignmentBase:
    def set_playhead(self, value: float, seek_video: bool = True) -> None:
        self.playhead_ms = float(value)
        self.data_seek_calls.append((float(value), bool(seek_video)))
        if seek_video:
            target = (
                self.data_create_time_ms
                + self.playhead_ms
                - self.video_start_wall_ms
            )
            self.media.set_time_ms(target)

    def _set_playhead_visual(self, value: float) -> None:
        self.playhead_ms = float(value)

    def _queue_video_timeline_seek(self, value: float) -> None:
        self.video_queue_calls.append(float(value))

    def _seek_from_video_timeline(self, value: float) -> None:
        self.media.set_time_ms(value)
        self._set_playhead_visual(
            self.video_start_wall_ms
            + float(value)
            - self.data_create_time_ms
        )

    def pin_alignment(self) -> None:
        PlaybackMixin.pin_alignment(self)

    def nudge_alignment(self, delta_ms: float) -> None:
        PlaybackMixin.nudge_alignment(self, delta_ms)

    def _seek_video_to_playhead(self) -> None:
        PlaybackMixin._seek_video_to_playhead(self)


class _Harness(DualAnchorPrecisionV2Mixin, _AlignmentBase):
    def __init__(self, align_method: str = "pin") -> None:
        self._timelines_linked = align_method not in {"none", "default"}
        self._source_switch_active = False
        self._source_switch_kind = ""
        self._source_switch_snapshot = {}
        self._source_switch_mapping_committed = False
        self._pending_data_anchor_ms = 1_000.0
        self._pending_video_anchor_ms = 2_000.0
        self._independent_video_selection_active = True
        self._play_requested_after_anchor_confirmation = False
        self._media_primed = True
        self.data = object()
        self.data_create_time_ms = 1_800_000_000_000
        self.data_duration_ms = 3_600_000.0
        self.video_path = "hiv00001.mp4"
        self.video_start_wall_ms = self.data_create_time_ms + 10_000
        self.align_method = align_method
        self.playhead_ms = 14_000.0
        self.media = _FakeMedia(4_000.0)
        self.plot = _FakePlot()
        self.video_timeline = _FakeTimeline()
        self.data_seek_calls: list[tuple[float, bool]] = []
        self.video_queue_calls: list[float] = []
        self.alignment_refreshes = 0
        self.pending_intervals = {}
        self._status_bar = _FakeStatusBar()

    def _update_alignment_status(self) -> None:
        self.alignment_refreshes += 1

    def statusBar(self) -> _FakeStatusBar:  # noqa: N802
        return self._status_bar

    def _show_pending_pair(self) -> None:
        pass


class TimelineLinkingTest(unittest.TestCase):
    def test_all_effective_alignment_methods_enable_linking(self) -> None:
        for method in (
            "pin",
            "filename",
            "corner",
            "manual",
            "continuation",
            "anchor",
        ):
            with self.subTest(method=method):
                self.assertTrue(_Harness(method)._timelines_are_linked())
        for method in ("none", "default"):
            with self.subTest(method=method):
                self.assertFalse(_Harness(method)._timelines_are_linked())

    def test_pinned_data_drag_also_seeks_video_while_paused(self) -> None:
        window = _Harness("pin")

        window._data_timeline_selected(25_000.0)

        self.assertEqual(window.data_seek_calls, [(25_000.0, True)])
        self.assertEqual(window.media.seek_calls, [15_000.0])
        self.assertIsNone(window._pending_data_anchor_ms)
        self.assertIsNone(window._pending_video_anchor_ms)

    def test_pinned_video_drag_uses_linked_seek_chain_while_paused(self) -> None:
        window = _Harness("pin")

        window._queue_video_timeline_seek(8_000.0)
        window._seek_from_video_timeline(8_000.0)

        self.assertEqual(window.video_queue_calls, [8_000.0])
        self.assertEqual(window.media.seek_calls, [8_000.0])
        self.assertEqual(window.playhead_ms, 18_000.0)
        self.assertIsNone(window._pending_video_anchor_ms)

    def test_default_alignment_keeps_independent_anchor_mode(self) -> None:
        window = _Harness("default")
        window._pending_data_anchor_ms = None
        window._pending_video_anchor_ms = None
        window._independent_video_selection_active = False
        shown: list[bool] = []
        window._show_pending_pair = lambda: shown.append(True)

        window._data_timeline_selected(25_000.0)

        self.assertEqual(window.playhead_ms, 25_000.0)
        self.assertEqual(window._pending_data_anchor_ms, 25_000.0)
        self.assertEqual(window.data_seek_calls, [])
        self.assertEqual(window.media.seek_calls, [])
        self.assertEqual(shown, [True])

    def test_pin_action_clears_anchors_and_enables_linking(self) -> None:
        window = _Harness("default")

        window.pin_alignment()

        self.assertEqual(window.align_method, "pin")
        self.assertTrue(window._timelines_are_linked())
        self.assertIsNone(window._pending_data_anchor_ms)
        self.assertIsNone(window._pending_video_anchor_ms)

    def test_positive_nudge_moves_video_forward(self) -> None:
        window = _Harness("pin")

        window.nudge_alignment(1_000.0)

        self.assertEqual(
            window.video_start_wall_ms,
            window.data_create_time_ms + 9_000,
        )
        self.assertEqual(window.media.seek_calls, [5_000.0])
        self.assertEqual(window.align_method, "manual")
        self.assertTrue(window._timelines_are_linked())

    def test_negative_nudge_moves_video_backward(self) -> None:
        window = _Harness("pin")

        window.nudge_alignment(-1_000.0)

        self.assertEqual(
            window.video_start_wall_ms,
            window.data_create_time_ms + 11_000,
        )
        self.assertEqual(window.media.seek_calls, [3_000.0])

    def test_unlock_allows_independent_positions_then_repin(self) -> None:
        window = _Harness("pin")

        window.pin_alignment()
        window._data_timeline_selected(25_000.0)
        window._pending_video_anchor_ms = 8_000.0
        window.pin_alignment()

        self.assertTrue(window._timelines_are_linked())
        self.assertEqual(window.playhead_ms, 25_000.0)
        self.assertEqual(
            window.video_start_wall_ms,
            window.data_create_time_ms + 17_000,
        )

    def test_unlocked_nudge_moves_only_video(self) -> None:
        window = _Harness("pin")
        original_wall = window.video_start_wall_ms
        window._timelines_linked = False
        window._show_pending_pair = lambda: None
        window._queue_video_timeline_seek = window.video_queue_calls.append

        window.nudge_alignment(1_000.0)

        self.assertEqual(window.video_start_wall_ms, original_wall)
        self.assertEqual(window.video_queue_calls, [5_000.0])


if __name__ == "__main__":
    unittest.main()
