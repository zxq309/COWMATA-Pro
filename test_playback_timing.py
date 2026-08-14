from __future__ import annotations

import unittest
from unittest.mock import patch

from playback_mixin import PlaybackMixin
from seek_watchdog_mixin import SeekWatchdogMixin
from smooth_playhead_mixin import SmoothPlayheadMixin


class _Text:
    def __init__(self) -> None:
        self.value = ""

    def setText(self, value: str) -> None:  # noqa: N802
        self.value = str(value)


class _Timeline:
    def __init__(self) -> None:
        self.position = 0.0

    def set_position(self, value: float) -> None:
        self.position = float(value)


class _Media:
    def __init__(self, video_ms: float = 10_000.0, rate: float = 1.0) -> None:
        self.video_ms = float(video_ms)
        self.rate = float(rate)
        self.playing = False
        self.seek_calls: list[float] = []
        self.barrier_pending = False
        self.confirmation_serial = 0

    def is_playing(self) -> bool:
        return self.playing

    def play(self) -> bool:
        self.playing = True
        return True

    def pause(self, paused: bool = True) -> None:
        self.playing = not paused

    def get_time_ms(self) -> float:
        return self.video_ms

    def set_time_ms(self, value: float) -> bool:
        self.video_ms = float(value)
        self.seek_calls.append(float(value))
        return True

    def get_rate(self) -> float:
        return self.rate

    def duration_ms(self) -> float:
        return 60_000.0

    def seek_confirmation_pending(self) -> bool:
        return self.barrier_pending

    def seek_confirmation_serial(self) -> int:
        return self.confirmation_serial


class _ResumeHarness:
    def __init__(self) -> None:
        self.media = _Media()
        self.video_path = "hiv00002.mp4"
        self.playhead_ms = 10_000.0
        self.seek_calls = 0
        self.rate_calls = 0

    def _video_coverage(self) -> tuple[float, float]:
        return 0.0, 60_000.0

    def _seek_video_to_playhead(self) -> None:
        self.seek_calls += 1

    def set_playhead(self, value: float) -> None:
        self.playhead_ms = float(value)

    def _apply_rate(self) -> None:
        self.rate_calls += 1


class _Timer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def start(self) -> None:
        self.stopped = False


class _StatusBar:
    def showMessage(self, _message: str, _duration: int) -> None:  # noqa: N802
        pass


class _WatchdogHarness:
    SEEK_CONFIRM_TOLERANCE_MS = 50.0

    def __init__(self) -> None:
        self._ui_pending_seek_video_ms = 10_000.0
        self._ui_seek_deadline = 100.0
        self._seek_watchdog = _Timer()
        self.media = _Media(10_000.0)
        self.confirmed: list[int] = []
        self._status_bar = _StatusBar()

    def _on_media_time(self, value: int) -> None:
        self.confirmed.append(int(value))
        self._ui_pending_seek_video_ms = None

    def statusBar(self) -> _StatusBar:  # noqa: N802
        return self._status_bar


class _SmoothBase:
    def _on_media_time(self, value: int) -> None:
        self.raw_presentations.append(int(value))

    def _on_playing_changed(self, _playing: bool) -> None:
        pass


class _SmoothHarness(SmoothPlayheadMixin, _SmoothBase):
    def __init__(self, rate: float = 1.0) -> None:
        self.media = _Media(1_000.0, rate)
        self.media.playing = True
        self._smooth_playing = True
        self._smooth_anchor_video_ms = 1_000.0
        self._smooth_anchor_monotonic = 10.0
        self._smooth_display_video_ms = 1_000.0
        self._smooth_last_tick_monotonic = 10.0
        self._smooth_last_raw_video_ms = 1_000.0
        self._smooth_last_raw_monotonic = 10.0
        self._smooth_seek_confirmation_serial = 0
        self._ui_pending_seek_video_ms = None
        self.video_timeline = _Timeline()
        self.video_clock_label = _Text()
        self.wall_clock_label = _Text()
        self.video_start_wall_ms = None
        self.data = None
        self.raw_presentations: list[int] = []
        self._smooth_timer = _Timer()

    def _shared_video_limits(self):
        return None


class PlaybackTimingTest(unittest.TestCase):
    def test_ordinary_resume_does_not_seek_again(self) -> None:
        window = _ResumeHarness()

        PlaybackMixin.toggle_play(window)

        self.assertTrue(window.media.is_playing())
        self.assertEqual(window.seek_calls, 0)
        self.assertEqual(window.rate_calls, 1)

    def test_watchdog_confirms_without_reissuing_seek(self) -> None:
        window = _WatchdogHarness()

        with patch("seek_watchdog_mixin.time.monotonic", return_value=20.0):
            SeekWatchdogMixin._check_pending_seek(window)

        self.assertEqual(window.confirmed, [10_000])
        self.assertEqual(window.media.seek_calls, [])

    def test_sparse_raw_time_is_calibration_not_direct_ui_jump(self) -> None:
        window = _SmoothHarness()

        with patch("smooth_playhead_mixin.time.monotonic", return_value=10.8):
            window._on_media_time(1_700)

        self.assertEqual(window.raw_presentations, [])
        self.assertEqual(window._smooth_last_raw_video_ms, 1_700.0)
        self.assertEqual(window._smooth_display_video_ms, 1_000.0)

    def test_first_raw_time_before_playing_signal_does_not_jump_ui(self) -> None:
        window = _SmoothHarness()
        window._smooth_playing = False

        with patch("smooth_playhead_mixin.time.monotonic", return_value=10.8):
            window._on_media_time(2_700)

        self.assertEqual(window.raw_presentations, [])
        self.assertEqual(window._smooth_display_video_ms, 1_000.0)

    def test_one_x_keeps_advancing_beyond_old_400_ms_limit(self) -> None:
        window = _SmoothHarness(rate=1.0)

        with patch("smooth_playhead_mixin.time.monotonic", return_value=11.25):
            window._smooth_playhead_tick()

        self.assertGreater(window.video_timeline.position, 2_100.0)
        self.assertLess(window.video_timeline.position, 2_500.0)

    def test_ten_x_advances_smoothly_per_timer_tick(self) -> None:
        window = _SmoothHarness(rate=10.0)

        with patch("smooth_playhead_mixin.time.monotonic", return_value=10.03):
            window._smooth_playhead_tick()

        advance = window.video_timeline.position - 1_000.0
        self.assertGreater(advance, 250.0)
        self.assertLess(advance, 350.0)

    def test_sparse_raw_time_keeps_advancing_while_media_is_playing(self) -> None:
        window = _SmoothHarness(rate=1.0)

        with patch("smooth_playhead_mixin.time.monotonic", return_value=12.1):
            window._smooth_playhead_tick()

        self.assertGreater(window.video_timeline.position, 2_800.0)

    def test_seek_barrier_pauses_smooth_clock(self) -> None:
        window = _SmoothHarness(rate=1.0)
        window.media.barrier_pending = True

        with patch("smooth_playhead_mixin.time.monotonic", return_value=12.1):
            window._smooth_playhead_tick()

        self.assertEqual(window.video_timeline.position, 0.0)

    def test_confirmed_seek_reanchors_instead_of_interpolating(self) -> None:
        window = _SmoothHarness(rate=1.0)
        window.media.confirmation_serial = 1

        with patch("smooth_playhead_mixin.time.monotonic", return_value=10.8):
            window._on_media_time(4_000)

        self.assertEqual(window.raw_presentations, [4_000])
        self.assertEqual(window._smooth_display_video_ms, 4_000.0)

    def test_linked_pause_preserves_continuous_position(self) -> None:
        window = _SmoothHarness(rate=1.0)
        window._smooth_display_video_ms = 3_000.0
        window.media.video_ms = 1_000.0
        window.media.playing = False

        with patch("smooth_playhead_mixin.time.monotonic", return_value=12.0):
            window._on_playing_changed(False)

        self.assertEqual(window._smooth_display_video_ms, 3_000.0)
        self.assertEqual(window.video_timeline.position, 3_000.0)

    def test_unlinked_pause_uses_independent_video_position(self) -> None:
        window = _SmoothHarness(rate=1.0)
        window._smooth_display_video_ms = 3_000.0
        window.media.video_ms = 1_000.0
        window.media.playing = False
        window._timelines_are_linked = lambda: False

        with patch("smooth_playhead_mixin.time.monotonic", return_value=12.0):
            window._on_playing_changed(False)

        self.assertEqual(window._smooth_display_video_ms, 1_000.0)
        self.assertEqual(window.video_timeline.position, 1_000.0)


if __name__ == "__main__":
    unittest.main()
