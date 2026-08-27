from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer

from cowmata_tailring.ui.i18n import t


class PrimingPlaybackMixin:
    """Queue the user's one-click play request until HEVC priming finishes."""

    PRIME_RETRY_DELAY_MS = 3_000
    PRIME_TIMEOUT_MS = 7_000

    def __init__(self) -> None:
        self._media_primed = False
        self._queued_play_after_prime = False
        self._prime_pending_seek_ms: float | None = None
        self._prime_pending_seek_is_initial = False
        self._prime_generation = 0
        self._prime_duration_ready = False
        self._prime_decoder_started = False
        self._prime_frame_ready = False
        self._prime_pause_requested = False
        super().__init__()

    def open_video(self, path: str | None = None) -> None:
        previous_path = self.video_path
        self._prime_generation += 1
        generation = self._prime_generation
        self._media_primed = False
        self._queued_play_after_prime = False
        self._prime_pending_seek_ms = None
        self._prime_pending_seek_is_initial = False
        self._prime_duration_ready = False
        self._prime_decoder_started = False
        self._prime_frame_ready = False
        self._prime_pause_requested = False
        super().open_video(path)
        requested = (
            os.path.abspath(os.fspath(path))
            if isinstance(path, (str, os.PathLike))
            else None
        )
        current = (
            os.path.abspath(self.media.current_path or "")
            if self.media is not None
            else ""
        )
        opened = bool(
            self.video_path
            and current
            and os.path.normcase(os.path.abspath(self.video_path))
            == os.path.normcase(current)
            and (
                requested is None
                or os.path.normcase(current) == os.path.normcase(requested)
            )
        )
        if opened:
            # VLC must play briefly to initialise HEVC. Restore the requested
            # starting frame after that priming playback instead of exposing
            # however far the decoder advanced while metadata was scanned.
            self._prime_pending_seek_ms = 0.0
            self._prime_pending_seek_is_initial = True
            self._prime_duration_ready = bool(self.media.duration_ms() > 0)
            self._schedule_priming_watchdogs(generation)
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
                if (
                    self.media is not None
                    and not self._prime_frame_ready
                    and not self._prime_pause_requested
                    and not self.media.is_playing()
                ):
                    self.media.play()
                    self._apply_rate()
            else:
                self.play_btn.setText(t("▶ 播放"))
            return
        super().toggle_play()

    def _schedule_load_autopause(self) -> None:
        """Wait for a rendered frame instead of pausing on metadata alone."""

        self._prime_duration_ready = True
        self._observe_priming_frame()
        self._try_finish_media_priming()

    def _schedule_priming_watchdogs(self, generation: int) -> None:
        if QCoreApplication.instance() is None:
            return
        QTimer.singleShot(
            self.PRIME_RETRY_DELAY_MS,
            lambda: self._retry_media_priming(generation),
        )
        QTimer.singleShot(
            self.PRIME_TIMEOUT_MS,
            lambda: self._expire_media_priming(generation),
        )

    def _priming_attempt_is_current(self, generation: int) -> bool:
        return bool(
            generation == self._prime_generation
            and not self._media_primed
            and self.video_path
            and self.media is not None
            and not getattr(self, "_desktop_closing", False)
        )

    def _retry_media_priming(self, generation: int) -> None:
        if (
            not self._priming_attempt_is_current(generation)
            or self._prime_frame_ready
            or self.media is None
        ):
            return
        self.video_status.setText(
            f"{Path(self.video_path).name} · "
            + t("正在重试视频首帧初始化")
        )
        self.media.play()
        self._apply_rate()

    def _expire_media_priming(self, generation: int) -> None:
        if (
            not self._priming_attempt_is_current(generation)
            or self._prime_frame_ready
        ):
            return
        media = self.media
        self._media_primed = True
        self._queued_play_after_prime = False
        self._autopause_on_load = False
        self._prime_pending_seek_ms = None
        self._prime_pending_seek_is_initial = False
        if media is not None and media.is_playing():
            media.pause(True)
        self._finish_media_priming()
        message = t(
            "视频首帧初始化超时，已解除界面冻结；"
            "请点击播放重试或重新打开该视频"
        )
        self.video_status.setText(f"{Path(self.video_path).name} · {message}")
        status_bar = getattr(self, "statusBar", None)
        if callable(status_bar):
            status_bar().showMessage(message, 0)

    def _observe_priming_frame(
        self,
        video_time_ms: int | float | None = None,
    ) -> None:
        if self._media_primed or not self.video_path or self.media is None:
            return
        playing = bool(self.media.is_playing())
        if playing:
            self._prime_decoder_started = True
        output_count = getattr(self.media, "video_output_count", None)
        video_outputs = int(output_count()) if callable(output_count) else 0
        current_time = (
            float(self.media.get_time_ms())
            if video_time_ms is None
            else float(video_time_ms)
        )
        if (
            self._prime_decoder_started
            and playing
            and video_outputs > 0
            and current_time > 0.0
        ):
            self._prime_frame_ready = True
            if not self._prime_pause_requested:
                self._prime_pause_requested = True
                self.media.pause(True)
        self._try_finish_media_priming()

    def _try_finish_media_priming(self) -> None:
        if (
            self._media_primed
            or not self._prime_duration_ready
            or not self._prime_frame_ready
            or not self._prime_pause_requested
            or self.media is None
            or self.media.is_playing()
        ):
            return
        self._media_primed = True
        if QCoreApplication.instance() is None:
            self._finish_media_priming()
        else:
            QTimer.singleShot(0, self._finish_media_priming)

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
        self._observe_priming_frame(video_time_ms)
        if not self._media_primed and self._prime_pending_seek_ms is not None:
            return
        super()._on_media_time(video_time_ms)

    def _on_playing_changed(self, playing: bool) -> None:
        super()._on_playing_changed(playing)
        if playing:
            self._prime_decoder_started = True
            self._observe_priming_frame()
        else:
            self._try_finish_media_priming()

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
