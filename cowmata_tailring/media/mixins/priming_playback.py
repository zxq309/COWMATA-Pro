from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer

from cowmata_tailring.ui.i18n import t


class PrimingPlaybackMixin:
    """Queue the user's one-click play request until HEVC priming finishes."""

    def __init__(self) -> None:
        self._media_primed = False
        self._queued_play_after_prime = False
        self._prime_pending_seek_ms: float | None = None
        self._prime_pending_seek_is_initial = False
        super().__init__()

    def open_video(self, path: str | None = None) -> None:
        previous_path = self.video_path
        self._media_primed = False
        self._queued_play_after_prime = False
        self._prime_pending_seek_ms = None
        self._prime_pending_seek_is_initial = False
        super().open_video(path)
        if self.video_path:
            # VLC must play briefly to initialise HEVC. Restore the requested
            # starting frame after that priming playback instead of exposing
            # however far the decoder advanced while metadata was scanned.
            self._prime_pending_seek_ms = 0.0
            self._prime_pending_seek_is_initial = True
        elif self.video_path == previous_path:
            self._media_primed = True

    def toggle_play(self) -> None:
        if not self.video_path:
            return
        if not self._media_primed:
            self._queued_play_after_prime = (
                not self._queued_play_after_prime
            )
            if self._queued_play_after_prime:
                self.play_btn.setText(t("⏳ 初始化后播放"))
                self.video_status.setText(
                    f"{Path(self.video_path).name} · 正在初始化，稍后自动播放"
                )
            else:
                self.play_btn.setText(t("▶ 播放"))
            return
        super().toggle_play()

    def _seek_video_to_playhead(self) -> None:
        if (
            not self._media_primed
            and self.video_path
            and self.data is not None
            and self.video_start_wall_ms is not None
        ):
            target = max(
                0.0,
                float(
                    self.data_create_time_ms
                    + self.playhead_ms
                    - self.video_start_wall_ms
                ),
            )
            self._prime_pending_seek_ms = target
            self._prime_pending_seek_is_initial = False
            self.video_timeline.set_position(target)
            self.video_status.setText(
                f"{Path(self.video_path).name} · 初始化后同步定位"
            )
            return
        super()._seek_video_to_playhead()

    def _on_media_time(self, video_time_ms: int) -> None:
        if not self._media_primed and self._prime_pending_seek_ms is not None:
            return
        super()._on_media_time(video_time_ms)

    def _on_playing_changed(self, playing: bool) -> None:
        super()._on_playing_changed(playing)
        if (
            not playing
            and not self._media_primed
            and self.video_path
            and self.media is not None
            and self.media.duration_ms() > 0
        ):
            self._media_primed = True
            QTimer.singleShot(0, self._finish_media_priming)

    def _finish_media_priming(self) -> None:
        target = self._prime_pending_seek_ms
        restore_initial_position = self._prime_pending_seek_is_initial
        self._prime_pending_seek_ms = None
        self._prime_pending_seek_is_initial = False
        if target is not None:
            if (
                restore_initial_position
                and self.media is not None
                and self.video_path
            ):
                arm_ui_seek = getattr(self, "_arm_ui_seek", None)
                if callable(arm_ui_seek):
                    arm_ui_seek(target)
                elif hasattr(self, "video_timeline"):
                    self.video_timeline.set_position(target)
                self.media.set_time_ms(target)
            else:
                # A source switch may replace the default zero target with a
                # synchronized data/video target. Keep its existing overlap
                # checks and seek-confirmation chain intact.
                super()._seek_video_to_playhead()
        if self._queued_play_after_prime:
            self._queued_play_after_prime = False
            QTimer.singleShot(100, self._play_after_priming)
        else:
            self.play_btn.setText(t("▶ 播放"))

    def _play_after_priming(self) -> None:
        if (
            self._media_primed
            and self.media is not None
            and not self.media.is_playing()
        ):
            super().toggle_play()

    def _smooth_playhead_tick(self) -> None:
        if not self._media_primed:
            return
        super()._smooth_playhead_tick()
