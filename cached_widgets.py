from __future__ import annotations

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtGui import QPainter, QPixmap

from widgets import PlotSeries, SignalPlotWidget


class CachedSignalPlotWidget(SignalPlotWidget):
    """Signal plot whose expensive curves are cached separately from playheads."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._static_cache: QPixmap | None = None
        self._cache_size = self.size()
        self._capture_pending = False
        self._capturing = False

    def _invalidate_static(self) -> None:
        self._static_cache = None
        self._capture_pending = False
        self.update()

    def set_data(
        self,
        series,
        duration_ms: float,
        gap_threshold_ms: float = 100.0,
    ) -> None:
        checked: list[PlotSeries] = []
        for item in series:
            times = np.asarray(item.times_ms, dtype=float)
            values = np.asarray(item.values, dtype=float)
            if times.ndim != 1 or values.ndim != 1:
                raise ValueError(f"{item.key}: time/value arrays must be 1-D")
            if times.size != values.size:
                raise ValueError(f"{item.key}: time/value lengths differ")
            if times.size > 1 and np.any(np.diff(times) < 0):
                raise ValueError(f"{item.key}: timestamps are not ordered")
            checked.append(
                PlotSeries(
                    item.key,
                    item.name,
                    item.unit,
                    item.color,
                    times,
                    values,
                )
            )
        super().set_data(checked, duration_ms, gap_threshold_ms)
        self._invalidate_static()

    def clear_data(self) -> None:
        super().clear_data()
        self._events = []
        self._labels = []
        self._hover_ms = None
        self._selection_t = None
        self._playhead_ms = 0.0
        self._invalidate_static()

    def set_auto_y(self, enabled: bool) -> None:
        super().set_auto_y(enabled)
        self._invalidate_static()

    def set_events(self, labels, events) -> None:
        super().set_events(labels, events)
        self._invalidate_static()

    def set_view(self, t0: float, t1: float, emit: bool = False) -> None:
        old = self.view_range
        super().set_view(t0, t1, emit)
        if self.view_range != old:
            self._invalidate_static()

    def set_playhead(self, time_ms: float) -> None:
        self._playhead_ms = float(
            np.clip(time_ms, 0.0, self._duration_ms)
        )
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._cache_size = self.size()
        self._invalidate_static()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        super().mouseMoveEvent(event)
        self.update()

    def _static_cache_matches_widget(self) -> bool:
        """Compare the cache and widget in device-independent pixels."""

        cache = self._static_cache
        if cache is None:
            return False
        if abs(cache.devicePixelRatio() - self.devicePixelRatioF()) > 0.01:
            return False
        logical_size = cache.deviceIndependentSize()
        return (
            abs(logical_size.width() - self.width()) < 1.0
            and abs(logical_size.height() - self.height()) < 1.0
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._capturing:
            super().paintEvent(event)
            return
        if self._static_cache_matches_widget():
            painter = QPainter(self)
            painter.drawPixmap(0, 0, self._static_cache)
            self._paint_playheads(painter)
            return
        super().paintEvent(event)
        if not self._capture_pending:
            self._capture_pending = True
            QTimer.singleShot(0, self._capture_static)

    def _capture_static(self) -> None:
        self._capture_pending = False
        if not self.isVisible() or self.width() <= 0 or self.height() <= 0:
            return
        saved_playhead = self._playhead_ms
        saved_hover = self._hover_ms
        saved_drag = self._drag_mode
        saved_selection = self._selection_t
        self._playhead_ms = -1.0
        self._hover_ms = None
        self._drag_mode = None
        self._selection_t = None
        self._capturing = True
        try:
            self._static_cache = self.grab()
        finally:
            self._capturing = False
            self._playhead_ms = saved_playhead
            self._hover_ms = saved_hover
            self._drag_mode = saved_drag
            self._selection_t = saved_selection
        self.update()
