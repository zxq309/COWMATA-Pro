from __future__ import annotations

from production_window import MainWindow as CachedWindow
from video_timeline import VideoTimelineWidget


class MainWindow(CachedWindow):
    """Adds a native video timeline sharing the same playback clock."""

    def __init__(self) -> None:
        super().__init__()
        self.video_timeline = VideoTimelineWidget(
            self.video_surface.parentWidget()
        )
        layout = self.video_surface.parentWidget().layout()
        layout.insertWidget(2, self.video_timeline)
        self.video_timeline.seekRequested.connect(
            self._seek_from_video_timeline
        )

    def _seek_from_video_timeline(self, video_ms: float) -> None:
        if self.media is None:
            return
        self.media.set_time_ms(video_ms)
        if self.data is not None and self.video_start_wall_ms is not None:
            relative = (
                self.video_start_wall_ms
                + video_ms
                - self.data_create_time_ms
            )
            self._set_playhead_visual(relative)

    def _on_media_duration(self, duration_ms: int) -> None:
        super()._on_media_duration(duration_ms)
        self.video_timeline.set_duration(duration_ms)

    def _on_media_time(self, video_time_ms: int) -> None:
        super()._on_media_time(video_time_ms)
        self.video_timeline.set_position(video_time_ms)

