from __future__ import annotations

import unittest

from ended_replay_mixin import EndedReplayMixin
from safe_media_engine import SafeMediaEngine


class _Value:
    def __init__(self, value=None) -> None:
        self.value = value

    def setText(self, value: str) -> None:  # noqa: N802
        self.value = value


class _Slider:
    def value(self) -> int:
        return 75


class _Check:
    def isChecked(self) -> bool:  # noqa: N802
        return True


class _Timeline:
    def __init__(self) -> None:
        self.position = 0.0

    def set_position(self, value: float) -> None:
        self.position = float(value)


class _Media:
    def __init__(self) -> None:
        self.current_status = "ended"
        self.current_path = "hiv00001.mp4"
        self.replay_target = None
        self.pending_cleared = False
        self.volume = None
        self.muted = None

    def duration_ms(self) -> int:
        return 1_007_442

    def clear_pending_seek(self) -> None:
        self.pending_cleared = True

    def replay_from_ms(self, value: float) -> bool:
        self.replay_target = float(value)
        self.current_status = "opening"
        return True

    def set_volume(self, value: int) -> None:
        self.volume = value

    def mute(self, value: bool) -> None:
        self.muted = value


class _LowerWindow:
    def __init__(self) -> None:
        self.media = _Media()
        self.video_path = "hiv00001.mp4"
        self.video_start_wall_ms = 1_800_000_005_528
        self.data_create_time_ms = 1_800_000_000_000
        self.data_duration_ms = 3_599_969.0
        self.data = object()
        self.video_timeline = _Timeline()
        self.video_clock_label = _Value()
        self.wall_clock_label = _Value()
        self.video_status = _Value()
        self.play_btn = _Value()
        self.volume_slider = _Slider()
        self.mute_check = _Check()
        self.playhead_ms = 1_012_970.0
        self.reset_called = False
        self.rate_applied = False
        self.lower_toggle_called = False

    def _on_media_ended(self) -> None:
        self.play_btn.setText("▶ 播放")

    def _reset_pending_pair(self, cancel_queued_seek: bool = False) -> None:
        self.reset_called = bool(cancel_queued_seek)

    def _set_playhead_visual(self, value: float) -> None:
        self.playhead_ms = float(value)

    def _apply_rate(self) -> None:
        self.rate_applied = True

    def toggle_play(self) -> None:
        self.lower_toggle_called = True


class _Window(EndedReplayMixin, _LowerWindow):
    pass


class EndedReplayTest(unittest.TestCase):
    def test_direct_play_reopens_from_start(self) -> None:
        window = _Window()
        window._on_media_ended()

        window.toggle_play()

        self.assertEqual(window.media.replay_target, 0.0)
        self.assertEqual(window.video_timeline.position, 0.0)
        self.assertEqual(window.playhead_ms, 5_528.0)
        self.assertTrue(window.reset_called)
        self.assertTrue(window.media.pending_cleared)
        self.assertTrue(window.rate_applied)
        self.assertEqual(window.media.volume, 75)
        self.assertTrue(window.media.muted)
        self.assertFalse(window._media_reached_end)

    def test_drag_after_end_replays_from_selected_position(self) -> None:
        window = _Window()
        window._on_media_ended()

        window._queue_video_timeline_seek(600_000.0)
        self.assertEqual(window.video_timeline.position, 600_000.0)
        self.assertIn("已选择重播位置", window.video_status.value)

        window.toggle_play()

        self.assertEqual(window.media.replay_target, 600_000.0)
        self.assertEqual(window.playhead_ms, 605_528.0)
        self.assertFalse(window.lower_toggle_called)

    def test_normal_play_still_uses_existing_chain(self) -> None:
        window = _Window()
        window.media.current_status = "paused"

        window.toggle_play()

        self.assertTrue(window.lower_toggle_called)

    def test_engine_replay_reopens_and_queues_target(self) -> None:
        class EngineStub:
            _path = "hiv00001.mp4"
            _pending_seek_ms = None

            def duration_ms(self) -> int:
                return 1_000

            def open(self, path: str) -> bool:
                self.opened = path
                self._pending_seek_ms = None
                return True

            def play(self) -> bool:
                self.played = True
                return True

            def _fail(self, _message: str) -> bool:
                return False

        engine = EngineStub()

        result = SafeMediaEngine.replay_from_ms(engine, 1_000)

        self.assertTrue(result)
        self.assertEqual(engine.opened, "hiv00001.mp4")
        self.assertEqual(engine._pending_seek_ms, 999)
        self.assertTrue(engine.played)


if __name__ == "__main__":
    unittest.main()
