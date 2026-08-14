from __future__ import annotations

from interactive_plot import InteractiveSignalPlotWidget
from timeline_window import MainWindow as TimelineWindow


class MainWindow(TimelineWindow):
    """Final interaction layer: event selection, movement and resizing."""

    def __init__(self) -> None:
        super().__init__()
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
        self._rebuild_shortcuts()

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
            key=lambda item: (float(item.get("t0", 0)), int(item.get("id", 0)))
        )
        self._refresh_events()
        self._autosave()

