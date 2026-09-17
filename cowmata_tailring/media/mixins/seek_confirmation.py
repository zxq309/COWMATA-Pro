from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from cowmata_tailring.ui.i18n import t


class SeekConfirmationMixin:
    """Keep both playheads together while a long-GOP HEVC seek settles."""

    def __init__(self) -> None:
        self._ui_pending_seek_video_ms: float | None = None
        self._ui_seek_deadline = 0.0
        super().__init__()

    def _arm_ui_seek(self, target_ms: float) -> None:
        self._ui_pending_seek_video_ms = float(target_ms)
        now = time.monotonic()
        self._ui_seek_deadline = now + 8.0
        if hasattr(self, "video_timeline"):
            self.video_timeline.set_position(target_ms)

    def _clear_ui_seek(self) -> None:
        self._ui_pending_seek_video_ms = None
        self._ui_seek_deadline = 0.0

    def _seek_video_to_playhead(self) -> None:
        if (
            self.media is None
            or not self.video_path
            or self.data is None
            or self.video_start_wall_ms is None
        ):
            self._clear_ui_seek()
            super()._seek_video_to_playhead()
            return
        target = float(
            self.data_create_time_ms
            + self.playhead_ms
            - self.video_start_wall_ms
        )
        limits = self._shared_video_limits()
        if limits is None or not (limits[0] <= target <= limits[1]):
            self._clear_ui_seek()
            super()._seek_video_to_playhead()
            return
        self._arm_ui_seek(target)
        self.video_status.setText(
            f"{Path(self.video_path).name} · 正在同步定位"
        )
        self.media.set_time_ms(target)

    def _seek_from_video_timeline(self, video_ms: float) -> None:
        limits = self._shared_video_limits()
        if limits is not None:
            video_ms = float(np.clip(video_ms, limits[0], limits[1]))
        self._arm_ui_seek(video_ms)
        super()._seek_from_video_timeline(video_ms)

    def _on_media_time(self, video_time_ms: int) -> None:
        pending = self._ui_pending_seek_video_ms
        if pending is not None:
            readiness = getattr(self.media, "seek_target_is_ready", None)
            media_ready = (
                bool(readiness(pending))
                if callable(readiness)
                else True
            )
            # The media engine performs generation-aware confirmation.  Older
            # engines retain the legacy nearby-keyframe tolerance.
            if media_ready and (
                callable(readiness)
                or abs(float(video_time_ms) - pending) <= 1500.0
            ):
                self._clear_ui_seek()
            elif time.monotonic() >= self._ui_seek_deadline:
                self._clear_ui_seek()
                self.statusBar().showMessage(
                    t("视频定位未在 8 秒内确认，已显示播放器实际位置"),
                    5000,
                )
            else:
                # A seek request is sent exactly once.  Reissuing it while
                # playback is running restarts long-GOP decoding and makes a
                # handful of frames loop until the eight-second timeout.
                return
        super()._on_media_time(video_time_ms)
