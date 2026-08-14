from __future__ import annotations

from cached_widgets import CachedSignalPlotWidget
from final_window import MainWindow as MediaSafeWindow


class MainWindow(MediaSafeWindow):
    """Final window with cached native plots and safe media lifecycle."""

    def __init__(self) -> None:
        super().__init__()
        old_plot = self.plot
        parent = old_plot.parentWidget()
        layout = parent.layout()
        replacement = CachedSignalPlotWidget(parent)
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
        self.plot.set_events(self.labels, self.events)
        self._rebuild_shortcuts()

