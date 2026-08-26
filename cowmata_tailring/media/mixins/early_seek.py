from __future__ import annotations

from pathlib import Path


class EarlySeekMixin:
    """Remember a requested shared position even before media metadata loads."""

    def __init__(self) -> None:
        self._early_seek_waiting_for_duration = False
        super().__init__()

    def _seek_video_to_playhead(self) -> None:
        if (
            self.media is not None
            and self.video_path
            and self.data is not None
            and self.video_start_wall_ms is not None
            and self.media.duration_ms() <= 0
        ):
            self._early_seek_waiting_for_duration = True
            target = max(
                0.0,
                float(
                    self.data_create_time_ms
                    + self.playhead_ms
                    - self.video_start_wall_ms
                ),
            )
            self._arm_ui_seek(target)
            self.video_status.setText(
                f"{Path(self.video_path).name} · 等待解码器后同步定位"
            )
            self.media.set_time_ms(target)
            return
        self._early_seek_waiting_for_duration = False
        super()._seek_video_to_playhead()

    def _on_media_duration(self, duration_ms: int) -> None:
        super()._on_media_duration(duration_ms)
        pending = self._ui_pending_seek_video_ms
        if (
            self._early_seek_waiting_for_duration
            and duration_ms > 0
            and pending is not None
            and self.media is not None
        ):
            self._early_seek_waiting_for_duration = False
            self.media.set_time_ms(min(float(duration_ms), pending))
