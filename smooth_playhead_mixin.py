from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import QTimer

from ui_helpers import format_relative, format_wall


class SmoothPlayheadMixin:
    """Interpolate coarse recorder timestamps into a smooth shared cursor."""

    SMOOTH_CORRECTION_GAIN = 0.5
    SMOOTH_MAX_CORRECTION_FRACTION = 0.12

    def __init__(self) -> None:
        now = time.monotonic()
        self._smooth_anchor_video_ms = 0.0
        self._smooth_anchor_monotonic = now
        self._smooth_display_video_ms = 0.0
        self._smooth_last_tick_monotonic = now
        self._smooth_last_raw_video_ms = 0.0
        self._smooth_last_raw_monotonic = now
        self._smooth_seek_confirmation_serial = 0
        self._smooth_playing = False
        super().__init__()
        self._smooth_timer = QTimer(self)
        self._smooth_timer.setInterval(30)
        self._smooth_timer.timeout.connect(self._smooth_playhead_tick)

    def _on_media_time(self, video_time_ms: int) -> None:
        now = time.monotonic()
        raw = float(video_time_ms)
        self._smooth_last_raw_video_ms = raw
        self._smooth_last_raw_monotonic = now

        serial_getter = (
            getattr(self.media, "seek_confirmation_serial", None)
            if self.media is not None
            else None
        )
        serial = int(serial_getter()) if callable(serial_getter) else 0
        confirmed_seek = serial > self._smooth_seek_confirmation_serial
        if confirmed_seek:
            self._smooth_seek_confirmation_serial = serial
            self._reset_smooth_clock(raw, now)
            super()._on_media_time(video_time_ms)
            return

        pending_seek = (
            getattr(self, "_ui_pending_seek_video_ms", None) is not None
        )
        actively_smoothing = bool(
            self.media is not None
            and self.media.is_playing()
            and not pending_seek
        )
        if actively_smoothing:
            # Do not copy VLC's sparse public timestamp directly into the UI.
            # The timer keeps moving and uses this sample only as calibration.
            return

        self._reset_smooth_clock(raw, now)
        super()._on_media_time(video_time_ms)

    def _on_playing_changed(self, playing: bool) -> None:
        super()._on_playing_changed(playing)
        was_smoothing = self._smooth_playing
        self._smooth_playing = bool(playing)
        now = time.monotonic()
        if playing:
            actual = (
                float(self.media.get_time_ms())
                if self.media is not None
                else self._smooth_display_video_ms
            )
            # MediaEngine.poll emits time_changed before playing_changed.
            # Preserve the already visible paused/seeked position here; using
            # the first coarse VLC value as a new UI anchor causes a large
            # jump exactly when the Play button is pressed at high rates.
            self._smooth_anchor_video_ms = self._smooth_display_video_ms
            self._smooth_anchor_monotonic = now
            self._smooth_last_tick_monotonic = now
            self._smooth_last_raw_video_ms = actual
            self._smooth_last_raw_monotonic = now
            self._smooth_timer.start()
        else:
            self._smooth_timer.stop()
            if self.media is not None:
                if self._preserve_smooth_position_on_pause(was_smoothing):
                    paused_position = self._smooth_display_video_ms
                else:
                    paused_position = float(self.media.get_time_ms())
                self._reset_smooth_clock(paused_position, now)
                self._present_smooth_position(paused_position)

    def _preserve_smooth_position_on_pause(
        self, was_smoothing: bool
    ) -> bool:
        if not was_smoothing or getattr(self, "_source_switch_active", False):
            return False
        if getattr(self, "_ui_pending_seek_video_ms", None) is not None:
            return False
        linked = getattr(self, "_timelines_are_linked", None)
        return not callable(linked) or bool(linked())

    def _reset_smooth_clock(
        self, video_ms: float, now: float | None = None
    ) -> None:
        moment = time.monotonic() if now is None else float(now)
        value = max(0.0, float(video_ms))
        self._smooth_anchor_video_ms = value
        self._smooth_anchor_monotonic = moment
        self._smooth_display_video_ms = value
        self._smooth_last_tick_monotonic = moment
        self._smooth_last_raw_video_ms = value
        self._smooth_last_raw_monotonic = moment

    def _present_smooth_position(self, video_ms: float) -> bool:
        duration = (
            float(self.media.duration_ms())
            if self.media is not None
            else 0.0
        )
        if duration > 0:
            video_ms = float(np.clip(video_ms, 0.0, duration))

        limits = self._shared_video_limits()
        reached_boundary = False
        if limits is not None:
            clamped = float(np.clip(video_ms, limits[0], limits[1]))
            reached_boundary = abs(clamped - video_ms) > 0.01
            video_ms = clamped

        self._smooth_display_video_ms = video_ms
        self.video_timeline.set_position(video_ms)
        self.video_clock_label.setText(
            f"视频 {format_relative(video_ms)}"
        )
        if self.video_start_wall_ms is not None:
            wall = self.video_start_wall_ms + video_ms
            self.wall_clock_label.setText(
                f"画面时间 {format_wall(wall)}"
            )
            linked = getattr(self, "_timelines_are_linked", None)
            may_sync_data = not callable(linked) or bool(linked())
            if self.data is not None and may_sync_data:
                data_ms = wall - self.data_create_time_ms
                if 0.0 <= data_ms <= self.data_duration_ms:
                    self._set_playhead_visual(data_ms)
        return reached_boundary

    def _smooth_playhead_tick(self) -> None:
        if (
            not self._smooth_playing
            or self.media is None
            or not self.media.is_playing()
            or getattr(self, "_ui_pending_seek_video_ms", None) is not None
            or self._media_seek_confirmation_pending()
        ):
            return
        now = time.monotonic()
        elapsed = max(0.0, now - self._smooth_last_tick_monotonic)
        self._smooth_last_tick_monotonic = now
        raw_age = max(0.0, now - self._smooth_last_raw_monotonic)
        rate = max(0.05, float(self.media.get_rate()))
        nominal_speed = 1000.0 * rate
        raw_estimate_now = (
            self._smooth_last_raw_video_ms + raw_age * nominal_speed
        )
        error_ms = raw_estimate_now - self._smooth_display_video_ms
        max_correction = (
            nominal_speed * self.SMOOTH_MAX_CORRECTION_FRACTION
        )
        correction_speed = float(
            np.clip(
                error_ms * self.SMOOTH_CORRECTION_GAIN,
                -max_correction,
                max_correction,
            )
        )
        video_ms = self._smooth_display_video_ms + elapsed * max(
            0.0, nominal_speed + correction_speed
        )
        self._smooth_anchor_video_ms = video_ms
        self._smooth_anchor_monotonic = now
        reached_boundary = self._present_smooth_position(video_ms)

        if reached_boundary:
            self._smooth_timer.stop()
            self._smooth_playing = False
            self.media.pause(True)
            self.media.set_time_ms(video_ms)
            self.statusBar().showMessage(
                "已到达视频与九轴数据的共同覆盖边界", 3000
            )

    def _media_seek_confirmation_pending(self) -> bool:
        if self.media is None:
            return False
        checker = getattr(self.media, "seek_confirmation_pending", None)
        return bool(checker()) if callable(checker) else False
