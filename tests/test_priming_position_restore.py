from __future__ import annotations

from pathlib import Path

from cowmata_tailring.app.mixins.cancel_safe_open import CancelSafeOpenMixin
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


class _Media:
    def __init__(self) -> None:
        self.current_path = ""
        self.time_ms = 1_345
        self.set_time_calls: list[float] = []

    def set_time_ms(self, value: float) -> bool:
        self.time_ms = int(round(value))
        self.set_time_calls.append(float(value))
        return True

    def duration_ms(self) -> int:
        return 704_425

    def is_playing(self) -> bool:
        return False


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


class _PrimingHarness(PrimingPlaybackMixin, _PrimingBase):
    pass


class _CancelHarness(
    CancelSafeOpenMixin,
    PrimingPlaybackMixin,
    _PrimingBase,
):
    def _show_error(self, _message: str) -> None:
        pass


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

    player.open_video(str(requested))

    assert player._media_primed
    assert player._queued_play_after_prime
    assert player._prime_pending_seek_ms == 12_345.0
    assert not player._prime_pending_seek_is_initial
