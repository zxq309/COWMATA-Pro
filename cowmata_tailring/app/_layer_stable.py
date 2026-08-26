from __future__ import annotations

import os

from cowmata_tailring.annotation.mixins.annotation import AnnotationMixin
from cowmata_tailring.app.mixins.activity import ActivityMixin
from cowmata_tailring.media.mixins.playback import PlaybackMixin
from cowmata_tailring.media.stable_engine import StableMediaEngine
from cowmata_tailring.ui.i18n import t
from cowmata_tailring.ui.interactive_plot import InteractiveSignalPlotWidget
from cowmata_tailring.ui.main_window import MainWindow as BaseMainWindow
from cowmata_tailring.ui.video_timeline import VideoTimelineWidget


class MainWindow(
    ActivityMixin,
    PlaybackMixin,
    AnnotationMixin,
    BaseMainWindow,
):
    """Complete production window without browser or blocking VLC shutdown."""

    def __init__(self) -> None:
        previous = os.environ.get("BOVINE_NO_MEDIA")
        os.environ["BOVINE_NO_MEDIA"] = "1"
        try:
            super().__init__()
        finally:
            if previous is None:
                os.environ.pop("BOVINE_NO_MEDIA", None)
            else:
                os.environ["BOVINE_NO_MEDIA"] = previous

        old_plot = self.plot
        parent = old_plot.parentWidget()
        layout = parent.layout()
        replacement = InteractiveSignalPlotWidget(parent)
        layout.replaceWidget(old_plot, replacement)
        old_plot.hide()
        old_plot.deleteLater()
        self.plot = replacement
        self.ui.plot = replacement

        try:
            self.auto_y_check.toggled.disconnect()
        except RuntimeError:
            pass
        try:
            self.full_view_btn.clicked.disconnect()
        except RuntimeError:
            pass
        self.auto_y_check.toggled.connect(self.plot.set_auto_y)
        self.full_view_btn.clicked.connect(self.plot.show_all)
        self.plot.seekRequested.connect(self.set_playhead)
        self.plot.rangeSelected.connect(self._range_selected)
        self.plot.eventSelected.connect(self._select_event_by_id)
        self.plot.eventChanged.connect(self._event_changed_on_plot)
        self.plot.set_events(self.labels, self.events)

        self.video_timeline = VideoTimelineWidget(
            self.video_surface.parentWidget()
        )
        self.video_surface.parentWidget().layout().insertWidget(
            2, self.video_timeline
        )
        self.video_timeline.seekRequested.connect(
            self._seek_from_video_timeline
        )

        self.media = StableMediaEngine(self.video_surface, parent=self)
        self.media.status_changed.connect(self._on_media_status)
        self.media.error_occurred.connect(self._show_error)
        self.media.time_changed.connect(self._on_media_time)
        self.media.duration_changed.connect(self._on_media_duration)
        self.media.playing_changed.connect(self._on_playing_changed)
        self.media.ended.connect(self._on_media_ended)
        self.media.timeline_analysis_message.connect(
            self._on_timeline_analysis_message
        )
        self.activity_times_ms = __import__("numpy").empty(0)
        self.activity_values = __import__("numpy").empty(0)
        self.static_g = float("nan")
        self._rebuild_shortcuts()
        self._refresh_enabled()

    def _select_event_by_id(self, event_id: int) -> None:
        self.selected_event_id = event_id
        for row in range(self.event_table.rowCount()):
            item = self.event_table.item(row, 0)
            if item is not None and int(item.data(256)) == event_id:
                self.event_table.selectRow(row)
                break

    def _event_changed_on_plot(
        self, event_id: int, _start_ms: float, _end_ms
    ) -> None:
        self.selected_event_id = event_id
        self.events.sort(
            key=lambda item: (
                float(item.get("t0", 0)),
                int(item.get("id", 0)),
            )
        )
        self._refresh_events()
        self._autosave()

    def _seek_from_video_timeline(self, video_ms: float) -> None:
        if self.media is None:
            return
        self.media.set_time_ms(video_ms)
        if self.data is not None and self.video_start_wall_ms is not None:
            self._set_playhead_visual(
                self.video_start_wall_ms
                + video_ms
                - self.data_create_time_ms
            )

    def _on_media_duration(self, duration_ms: int) -> None:
        super()._on_media_duration(duration_ms)
        self.video_timeline.set_duration(duration_ms)

    def _on_timeline_analysis_message(
        self, message: str, timeout_ms: int
    ) -> None:
        self.statusBar().showMessage(t(message), max(0, int(timeout_ms)))

    def _on_media_time(self, video_time_ms: int) -> None:
        super()._on_media_time(video_time_ms)
        self.video_timeline.set_position(video_time_ms)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._autosave()
        if self.media is not None:
            self.media.close()
        event.accept()
