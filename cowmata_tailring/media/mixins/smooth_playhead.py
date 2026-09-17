from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import QTimer

from cowmata_tailring.ui.helpers import format_relative, format_wall
from cowmata_tailring.ui.i18n import t


class SmoothPlayheadMixin:
    """Interpolate coarse recorder timestamps into a smooth shared cursor."""

    SMOOTH_CORRECTION_GAIN = 0.5
    SMOOTH_MAX_CORRECTION_FRACTION = 0.12
    SMOOTH_MAX_CLOCK_ERROR_MS = 300.0
    SMOOTH_MAX_SLEW_MULTIPLIER = 2.0
    SMOOTH_PAUSE_RECONCILE_MAX_MS = 300.0

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
        self._smooth_gap_latched = False
        self._smooth_user_pause_pending = False
        super().__init__()
        self._smooth_timer = QTimer(self)
        self._smooth_timer.setInterval(30)
        self._smooth_timer.timeout.connect(self._smooth_playhead_tick)

    def _calibrate_smooth_clock(self, video_time_ms: int) -> str:
        """Record a VLC sample without necessarily moving the UI cursor."""

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
            return "confirmed"

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
            return "suppress"

        self._reset_smooth_clock(raw, now)

        return "present"

    def _on_media_time(self, video_time_ms: int) -> None:
        if (
            self._smooth_user_pause_pending
            and self.media is not None
            and not self.media.is_playing()
            and abs(
                float(video_time_ms) - self._smooth_display_video_ms
            )
            > self.SMOOTH_PAUSE_RECONCILE_MAX_MS
        ):
            # MediaEngine emits time_changed before playing_changed.  A stale
            # final VLC sample must not bypass the pause reconciliation guard
            # and pull the video/IMU cursors away from the action instant.
            return
        calibration_mode = self._calibrate_smooth_clock(video_time_ms)
        if calibration_mode == "suppress":
            return
        super()._on_media_time(video_time_ms)

    def _on_playing_changed(self, playing: bool) -> None:
        super()._on_playing_changed(playing)
        was_smoothing = self._smooth_playing
        self._smooth_playing = bool(playing)
        now = time.monotonic()
        if playing:
            self._smooth_user_pause_pending = False
            self._smooth_gap_latched = False
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
            if (
                abs(actual - self._smooth_display_video_ms)
                <= self.SMOOTH_PAUSE_RECONCILE_MAX_MS
            ):
                reference_position = actual
            else:
                # Sparse Hikvision clocks can still report the sample from
                # before Pause.  Starting the estimate there makes the image
                # resume while both UI playheads remain frozen until that old
                # clock catches up.  The frozen public position is the safe
                # resume anchor; the next genuine sample recalibrates it.
                reference_position = self._smooth_display_video_ms
            self._smooth_last_raw_video_ms = reference_position
            self._smooth_last_raw_monotonic = now
            self._smooth_timer.start()
        else:
            self._smooth_timer.stop()
            if self.media is not None:
                if self._preserve_smooth_position_on_pause(was_smoothing):
                    actual = float(self.media.get_time_ms())
                    if (
                        abs(actual - self._smooth_display_video_ms)
                        <= self.SMOOTH_PAUSE_RECONCILE_MAX_MS
                    ):
                        paused_position = actual
                    else:
                        # Some malformed streams keep reporting an old clock
                        # for seconds while their picture advances.  A large
                        # discrepancy is therefore treated as stale metadata.
                        paused_position = self._smooth_display_video_ms
                else:
                    paused_position = float(self.media.get_time_ms())
                self._reset_smooth_clock(paused_position, now)
                self._present_smooth_position(paused_position)
            self._smooth_user_pause_pending = False

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

        limits = self._smooth_video_limits()
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

    def _smooth_video_limits(self):
        """Return the playback boundary used by the smooth video clock."""

        return self._shared_video_limits()

    def _smooth_playhead_tick(self) -> None:
        if self._media_timeline_gap_active():
            # A corrected Hikvision timeline can contain hours-long raw PTS
            # gaps with no frames. Hold the public cursor at the last frame;
            # extrapolating through the gap makes the IMU appear to continue
            # after the picture has stopped.
            self._smooth_gap_latched = True
            return
        if self._smooth_gap_latched:
            # The decoder has entered the next real segment. Re-anchor to its
            # public timestamp before allowing interpolation to resume. A
            # public timestamp may be unchanged across a collapsed gap, so
            # relying only on time_changed would otherwise leave the cursor
            # frozen forever.
            self._smooth_gap_latched = False
            if self.media is not None:
                now = time.monotonic()
                actual = float(self.media.get_time_ms())
                self._reset_smooth_clock(actual, now)
                self._present_smooth_position(actual)
            return
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
        reference_elapsed = max(
            0.0, now - self._smooth_last_raw_monotonic
        )
        nominal_speed = self._media_public_clock_speed_ms_per_s()
        raw_estimate_now = (
            self._smooth_last_raw_video_ms
            + reference_elapsed * nominal_speed
        )
        # Advance the reference every tick.  Applying a newly selected rate or
        # a newly available duration scale only to this short interval avoids
        # retroactively multiplying several seconds of sparse VLC clock age.
        self._smooth_last_raw_video_ms = raw_estimate_now
        self._smooth_last_raw_monotonic = now
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
        # Interpolation is presentation-only: it must never become a second
        # authoritative clock.  Keep it close to VLC's latest timestamp plus
        # the elapsed-time estimate.  When decoding temporarily falls behind,
        # hold the cursor until the real video clock catches up rather than
        # letting the pinned IMU timeline drift ahead of the visible frame.
        lower_bound = raw_estimate_now - self.SMOOTH_MAX_CLOCK_ERROR_MS
        upper_bound = raw_estimate_now + self.SMOOTH_MAX_CLOCK_ERROR_MS
        if video_ms > upper_bound:
            video_ms = max(self._smooth_display_video_ms, upper_bound)
        elif video_ms < lower_bound:
            video_ms = lower_bound
        # A high-rate decoder can drop forward to a distant keyframe and
        # publish tens of public seconds in one sparse sample.  Seek
        # confirmation already handles intentional jumps; ordinary playback
        # must instead slew toward that sample over several timer frames.
        maximum_advance = (
            elapsed
            * nominal_speed
            * self.SMOOTH_MAX_SLEW_MULTIPLIER
        )
        video_ms = min(
            video_ms,
            self._smooth_display_video_ms + maximum_advance,
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
                t("已到达视频与九轴数据的共同覆盖边界"), 3000
            )

    def _refresh_playhead_for_user_action(self) -> None:
        """Bring the shared cursor to the exact key/pause action instant."""

        self._smooth_playhead_tick()

    def _prepare_playhead_for_user_pause(self) -> None:
        """Freeze the shared cursor before libVLC changes pause state."""

        self._refresh_playhead_for_user_action()
        self._smooth_user_pause_pending = True

    def _media_public_clock_speed_ms_per_s(self) -> float:
        if self.media is None:
            return 0.0
        getter = getattr(
            self.media,
            "public_clock_speed_ms_per_s",
            None,
        )
        if callable(getter):
            try:
                return max(0.0, float(getter()))
            except (TypeError, ValueError):
                pass
        return 1000.0 * max(0.05, float(self.media.get_rate()))

    def _media_timeline_gap_active(self) -> bool:
        if self.media is None:
            return False
        value = getattr(self.media, "timeline_gap_active", False)
        return bool(value() if callable(value) else value)

    def _media_seek_confirmation_pending(self) -> bool:
        if self.media is None:
            return False
        checker = getattr(self.media, "seek_confirmation_pending", None)
        return bool(checker()) if callable(checker) else False
