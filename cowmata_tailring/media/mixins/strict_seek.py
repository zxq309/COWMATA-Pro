from __future__ import annotations

from cowmata_tailring.ui.i18n import t


class StrictSeekMixin:
    """Reject stale timestamps instead of treating them as a confirmed seek."""

    SEEK_CONFIRM_TOLERANCE_MS = 250.0

    def _seek_from_video_timeline(self, video_ms: float) -> None:
        if self.data is not None and self.video_start_wall_ms is not None:
            limits = self._shared_video_limits()
            if limits is None or limits[1] - limits[0] < 1.0:
                self._clear_ui_seek()
                if self.media is not None:
                    self.media.pause(True)
                self.video_status.setText(
                    t("数据与视频没有共同覆盖时间，已拒绝定位")
                )
                return
        super()._seek_from_video_timeline(video_ms)

    def _on_media_time(self, video_time_ms: int) -> None:
        pending = self._ui_pending_seek_video_ms
        readiness = (
            getattr(self.media, "seek_target_is_ready", None)
            if self.media is not None
            else None
        )
        media_ready = bool(readiness(pending)) if (
            pending is not None and callable(readiness)
        ) else False
        if (
            pending is not None
            and not media_ready
            and abs(float(video_time_ms) - pending)
            > self.SEEK_CONFIRM_TOLERANCE_MS
        ):
            # The independent watchdog performs retries and the 8 s expiry.
            # Do not let the lower legacy 1.5 s tolerance accept an old frame.
            return
        super()._on_media_time(video_time_ms)
