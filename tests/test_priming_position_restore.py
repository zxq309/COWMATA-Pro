from __future__ import annotations

from pathlib import Path

from cowmata_tailring.app.mixins.cancel_safe_open import CancelSafeOpenMixin
from cowmata_tailring.media.engine import MediaEngine
from cowmata_tailring.media.mixins import priming_playback
from cowmata_tailring.media.mixins.priming_playback import PrimingPlaybackMixin


class _TextWidget:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, value: str) -> None:  # noqa: N802
        self.text = value


class _Timeline:
    def __init__(self) -> None:
        self.positions: list[float] = []

    def set_position(self, value: float) -> None:
        self.positions.append(float(value))


class _Signal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback: object) -> None:
        self.callbacks.append(callback)

    def emit(self, value: int) -> None:
        for callback in self.callbacks:
            callback(value)


class _Media:
    def __init__(self) -> None:
        self.time_changed = _Signal()
        self.current_path = ""
        self.time_ms = 1_345
        self.duration = 704_425
        self.playing = False
        self.video_outputs = 0
        self.set_time_calls: list[float] = []
        self.pause_calls: list[bool] = []
        self.play_calls = 0

    def set_time_ms(self, value: float) -> bool:
        self.time_ms = int(round(value))
        self.set_time_calls.append(float(value))
        return True

    def duration_ms(self) -> int:
        return self.duration

    def is_playing(self) -> bool:
        return self.playing

    def get_time_ms(self) -> int:
        return self.time_ms

    def video_output_count(self) -> int:
        return self.video_outputs

    def pause(self, paused: bool = True) -> None:
        self.pause_calls.append(bool(paused))
        self.playing = not paused

    def play(self) -> bool:
        self.play_calls += 1
        self.playing = True
        return True


class _PrimingBase:
    def __init__(self) -> None:
        self.video_path = ""
        self.media = _Media()
        self.data = None
        self.data_create_time_ms = 0
        self.playhead_ms = 0.0
        self.video_start_wall_ms = None
        self.video_timeline = _Timeline()
        self.video_status = _TextWidget()
        self.play_btn = _TextWidget()
        self.open_succeeds = True
        self.base_seek_calls = 0
        self.armed_targets: list[float] = []
        self.apply_rate_calls = 0
        self._autopause_on_load = False

    def open_video(self, path: str | None = None) -> None:
        if self.open_succeeds and path:
            self.video_path = str(Path(path).resolve())
            self.media.current_path = self.video_path

    def _seek_video_to_playhead(self) -> None:
        self.base_seek_calls += 1

    def _arm_ui_seek(self, target_ms: float) -> None:
        self.armed_targets.append(float(target_ms))
        self.video_timeline.set_position(target_ms)

    def toggle_play(self) -> None:
        pass

    def _on_media_time(self, _video_time_ms: int) -> None:
        pass

    def _on_playing_changed(self, _playing: bool) -> None:
        pass

    def _smooth_playhead_tick(self) -> None:
        pass

    def _apply_rate(self) -> None:
        self.apply_rate_calls += 1


class _PrimingHarness(PrimingPlaybackMixin, _PrimingBase):
    pass


class _TimePresentationBlocker:
    def __init__(self) -> None:
        self.blocked_time_calls = 0
        super().__init__()

    def _on_media_time(self, _video_time_ms: int) -> None:
        self.blocked_time_calls += 1


class _BlockedTimeHarness(
    _TimePresentationBlocker,
    PrimingPlaybackMixin,
    _PrimingBase,
):
    pass


class _CancelHarness(
    CancelSafeOpenMixin,
    PrimingPlaybackMixin,
    _PrimingBase,
):
    def _show_error(self, _message: str) -> None:
        pass


class _VoutLib:
    def libvlc_media_player_has_vout(self, _player: object) -> int:
        return 2


class _VoutHarness:
    video_output_count = MediaEngine.video_output_count

    def __init__(self) -> None:
        self._closed = False
        self._player = object()
        self._lib = _VoutLib()


def test_cold_open_restores_initial_frame_after_decoder_priming() -> None:
    player = _PrimingHarness()

    player.open_video("mb00043.mp4")

    assert player._prime_pending_seek_ms == 0.0
    assert player._prime_pending_seek_is_initial

    player._finish_media_priming()

    assert player.media.set_time_calls == [0.0]
    assert player.media.time_ms == 0
    assert player.armed_targets == [0.0]
    assert player._prime_pending_seek_ms is None
    assert not player._prime_pending_seek_is_initial
    assert player.base_seek_calls == 0


def test_aligned_switch_keeps_existing_safe_seek_pipeline() -> None:
    player = _PrimingHarness()
    player.open_video("next.mp4")
    player.data = object()
    player.data_create_time_ms = 10_000
    player.playhead_ms = 2_500.0
    player.video_start_wall_ms = 9_000

    player._seek_video_to_playhead()

    assert player._prime_pending_seek_ms == 3_500.0
    assert not player._prime_pending_seek_is_initial

    player._finish_media_priming()

    assert player.base_seek_calls == 1
    assert player.media.set_time_calls == []
    assert player._prime_pending_seek_ms is None


def test_failed_open_restores_initial_seek_state(tmp_path: Path) -> None:
    old_path = str((tmp_path / "old.mp4").resolve())
    requested = tmp_path / "new.mp4"
    requested.write_bytes(b"video")
    player = _CancelHarness()
    player.open_succeeds = False
    player.video_path = old_path
    player.media.current_path = old_path
    player._media_primed = True
    player._queued_play_after_prime = True
    player._prime_pending_seek_ms = 12_345.0
    player._prime_pending_seek_is_initial = False
    player._prime_generation = 9
    player._prime_duration_ready = True
    player._prime_decoder_started = True
    player._prime_frame_ready = True
    player._prime_pause_requested = True

    player.open_video(str(requested))

    assert player._media_primed
    assert player._queued_play_after_prime
    assert player._prime_pending_seek_ms == 12_345.0
    assert not player._prime_pending_seek_is_initial
    assert player._prime_generation == 9
    assert player._prime_duration_ready
    assert player._prime_decoder_started
    assert player._prime_frame_ready
    assert player._prime_pause_requested


def test_cached_duration_waits_for_real_video_output_before_pause() -> None:
    player = _PrimingHarness()
    player.open_video("cached.mp4")
    player.media.playing = True
    player.media.time_ms = 0

    player._schedule_load_autopause()
    player._on_playing_changed(True)

    assert player._prime_duration_ready
    assert not player._prime_frame_ready
    assert player.media.pause_calls == []
    assert not player._media_primed

    player.media.video_outputs = 1
    player.media.time_ms = 67
    player._on_media_time(67)

    assert player._prime_frame_ready
    assert player.media.pause_calls == [True]
    assert player._media_primed
    assert player.media.set_time_calls == [0.0]


def test_cold_probe_can_finish_after_first_frame_is_already_paused() -> None:
    player = _PrimingHarness()
    player.media.duration = 0
    player.open_video("cold.mp4")
    player.media.playing = True
    player.media.video_outputs = 1
    player.media.time_ms = 67

    player._on_playing_changed(True)

    assert player._prime_frame_ready
    assert player.media.pause_calls == [True]
    assert not player._media_primed

    player.media.duration = 704_425
    player._schedule_load_autopause()

    assert player._media_primed
    assert player.media.set_time_calls == [0.0]


def test_direct_time_observer_bypasses_switch_presentation_blocker() -> None:
    player = _BlockedTimeHarness()
    player.open_video("switched.mp4")
    player._schedule_load_autopause()
    player.media.playing = True
    player.media.video_outputs = 1
    player.media.time_ms = 228

    player._on_media_time(228)

    assert player.blocked_time_calls == 1
    assert not player._prime_frame_ready
    assert player.media.pause_calls == []

    player.media.time_changed.emit(228)

    assert player._prime_frame_ready
    assert player.media.pause_calls == [True]
    assert player._media_primed
    assert player.media.set_time_calls == [0.0]


def test_media_engine_reports_libvlc_video_output_count() -> None:
    engine = _VoutHarness()

    assert engine.video_output_count() == 2


def test_priming_watchdogs_schedule_retry_and_timeout(monkeypatch) -> None:
    scheduled: list[tuple[int, object]] = []

    class _CoreApplication:
        @staticmethod
        def instance() -> object:
            return object()

    class _Timer:
        @staticmethod
        def singleShot(delay_ms: int, callback: object) -> None:  # noqa: N802
            scheduled.append((delay_ms, callback))

    monkeypatch.setattr(priming_playback, "QCoreApplication", _CoreApplication)
    monkeypatch.setattr(priming_playback, "QTimer", _Timer)
    player = _PrimingHarness()

    player.open_video("watchdogs.mp4")

    assert [delay for delay, _callback in scheduled] == [3_000, 7_000]


def test_retry_restarts_decoder_only_for_current_open() -> None:
    player = _PrimingHarness()
    player.open_video("retry.mp4")
    generation = player._prime_generation

    player._retry_media_priming(generation)

    assert player.media.play_calls == 1
    assert player.apply_rate_calls == 1
    assert "重试视频首帧初始化" in player.video_status.text

    player._retry_media_priming(generation - 1)

    assert player.media.play_calls == 1
    assert player.apply_rate_calls == 1


def test_timeout_unfreezes_player_when_no_first_frame_arrives() -> None:
    player = _PrimingHarness()
    player.open_video("timeout.mp4")
    generation = player._prime_generation
    player.media.playing = True
    player._queued_play_after_prime = True
    player._autopause_on_load = True

    player._expire_media_priming(generation)

    assert player._media_primed
    assert not player._queued_play_after_prime
    assert not player._autopause_on_load
    assert player._prime_pending_seek_ms is None
    assert not player._prime_pending_seek_is_initial
    assert player.media.pause_calls == [True]
    assert not player.media.playing
    assert "首帧初始化超时" in player.video_status.text
