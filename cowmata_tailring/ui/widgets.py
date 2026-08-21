from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPen, QWheelEvent
from PySide6.QtWidgets import QWidget


@dataclass(slots=True)
class PlotSeries:
    key: str
    name: str
    unit: str
    color: str
    times_ms: np.ndarray
    values: np.ndarray


class SignalPlotWidget(QWidget):
    """Native multi-channel plot with a shared playhead and annotation lanes."""

    seekRequested = Signal(float)
    rangeSelected = Signal(float, float)
    viewChanged = Signal(float, float)
    hoverChanged = Signal(float)

    LEFT = 86
    RIGHT = 12
    TOP = 8
    BOTTOM = 30
    LANE_HEIGHT = 18
    LOW_FREQUENCY_KEYS = frozenset({"temperature", "motion"})
    LOW_FREQUENCY_GAP_FACTOR = 1.5

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(680, 420)
        self._series: list[PlotSeries] = []
        self._duration_ms = 1.0
        self._view_t0 = 0.0
        self._view_t1 = 1.0
        self._playhead_ms = 0.0
        self._hover_ms: float | None = None
        self._auto_y = True
        self._events: list[dict[str, Any]] = []
        self._labels: list[dict[str, Any]] = []
        self._drag_mode: str | None = None
        self._drag_start_x = 0.0
        self._drag_last_x = 0.0
        self._drag_start_t = 0.0
        self._pan_start_view = (0.0, 1.0)
        self._selection_t: float | None = None
        self._gap_threshold_ms = 100.0
        self._lanes_visible = True

    @property
    def view_range(self) -> tuple[float, float]:
        return self._view_t0, self._view_t1

    def set_data(
        self,
        series: Iterable[PlotSeries],
        duration_ms: float,
        gap_threshold_ms: float = 100.0,
    ) -> None:
        self._series = list(series)
        self._duration_ms = max(1.0, float(duration_ms))
        self._view_t0 = 0.0
        self._view_t1 = self._duration_ms
        self._gap_threshold_ms = max(1.0, float(gap_threshold_ms))
        self.update()

    def clear_data(self) -> None:
        self._series.clear()
        self._duration_ms = 1.0
        self._view_t0 = 0.0
        self._view_t1 = 1.0
        self.update()

    def set_auto_y(self, enabled: bool) -> None:
        self._auto_y = bool(enabled)
        self.update()

    def set_playhead(self, time_ms: float) -> None:
        self._playhead_ms = float(np.clip(time_ms, 0.0, self._duration_ms))
        self.update()

    def set_events(self, labels: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
        self._labels = labels
        self._events = events
        self.update()

    def set_view(self, t0: float, t1: float, emit: bool = False) -> None:
        min_span = min(200.0, self._duration_ms)
        span = max(min_span, float(t1) - float(t0))
        span = min(span, self._duration_ms)
        t0 = float(np.clip(t0, 0.0, max(0.0, self._duration_ms - span)))
        t1 = t0 + span
        changed = abs(t0 - self._view_t0) > 0.01 or abs(t1 - self._view_t1) > 0.01
        self._view_t0, self._view_t1 = t0, t1
        if changed and emit:
            self.viewChanged.emit(t0, t1)
        self.update()

    def show_all(self) -> None:
        self.set_view(0.0, self._duration_ms, True)

    def center_on(self, time_ms: float) -> None:
        if self._view_t0 <= time_ms <= self._view_t1:
            return
        span = self._view_t1 - self._view_t0
        self.set_view(time_ms - span * 0.35, time_ms + span * 0.65, True)

    def set_lanes_visible(self, visible: bool) -> None:
        self._lanes_visible = bool(visible)
        self.update()

    def _lane_count(self) -> int:
        if not self._lanes_visible:
            return 0
        return min(16, len(self._labels))

    def _plot_rect(self) -> QRectF:
        lane_h = self._lane_count() * self.LANE_HEIGHT
        return QRectF(
            self.LEFT,
            self.TOP,
            max(1, self.width() - self.LEFT - self.RIGHT),
            max(1, self.height() - self.TOP - self.BOTTOM - lane_h),
        )

    def _lanes_rect(self) -> QRectF:
        plot = self._plot_rect()
        return QRectF(plot.left(), plot.bottom(), plot.width(), self._lane_count() * self.LANE_HEIGHT)

    def _x_for_time(self, time_ms: float) -> float:
        r = self._plot_rect()
        return r.left() + (time_ms - self._view_t0) / max(1e-9, self._view_t1 - self._view_t0) * r.width()

    def _time_for_x(self, x: float) -> float:
        r = self._plot_rect()
        ratio = (x - r.left()) / max(1.0, r.width())
        return float(np.clip(self._view_t0 + ratio * (self._view_t1 - self._view_t0), 0.0, self._duration_ms))

    def _gap_threshold_for_series(self, series: PlotSeries) -> float:
        """Use each low-frequency bucket series' own sampling cadence."""

        threshold = self._gap_threshold_ms
        if series.key not in self.LOW_FREQUENCY_KEYS:
            return threshold
        diffs = np.diff(np.asarray(series.times_ms, dtype=float))
        valid_diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
        if valid_diffs.size:
            threshold = max(
                threshold,
                float(np.median(valid_diffs))
                * self.LOW_FREQUENCY_GAP_FACTOR,
            )
        return threshold

    @staticmethod
    def _nice_number(value: float) -> str:
        a = abs(value)
        if a >= 1000:
            return f"{value:.0f}"
        if a >= 100:
            return f"{value:.1f}"
        if a >= 10:
            return f"{value:.2f}"
        return f"{value:.3f}"

    @staticmethod
    def _format_time(ms: float, with_ms: bool = False) -> str:
        ms = max(0.0, ms)
        total_s = ms / 1000.0
        h = int(total_s // 3600)
        m = int((total_s % 3600) // 60)
        s = total_s % 60
        if with_ms:
            return f"{h:02d}:{m:02d}:{s:06.3f}"
        return f"{h:02d}:{m:02d}:{int(s):02d}"

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(self.rect(), QColor("#f6f8fb"))
        if not self._series:
            p.setPen(QColor("#65758b"))
            p.setFont(QFont("Microsoft YaHei UI", 11))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "打开九轴 JSON 后显示真实数据")
            return

        plot = self._plot_rect()
        rows = len(self._series)
        row_h = plot.height() / max(1, rows)
        p.setFont(QFont("Microsoft YaHei UI", 8))

        for row, series in enumerate(self._series):
            top = plot.top() + row * row_h
            rect = QRectF(plot.left(), top, plot.width(), row_h)
            p.fillRect(rect, QColor("#ffffff" if row % 2 == 0 else "#fbfcfe"))
            p.setPen(QPen(QColor("#dde4ed"), 1))
            p.drawRect(rect)
            p.setPen(QColor("#142033"))
            p.drawText(QRectF(4, top + 2, self.LEFT - 9, 15), Qt.AlignmentFlag.AlignLeft, series.name)
            p.setPen(QColor("#738197"))
            p.drawText(QRectF(4, top + 17, self.LEFT - 9, 14), Qt.AlignmentFlag.AlignLeft, series.unit)

            times = series.times_ms
            values = series.values
            if times.size == 0 or values.size == 0:
                continue
            i0 = max(0, int(np.searchsorted(times, self._view_t0, side="left")) - 1)
            i1 = min(times.size, int(np.searchsorted(times, self._view_t1, side="right")) + 1)
            if i1 <= i0:
                continue
            tv = times[i0:i1]
            vv = values[i0:i1]
            finite = np.isfinite(vv)
            if not finite.any():
                continue
            yf = vv[finite]
            if self._auto_y:
                ylo = float(np.min(yf))
                yhi = float(np.max(yf))
            else:
                full_finite = values[np.isfinite(values)]
                ylo = float(np.min(full_finite)) if full_finite.size else -1.0
                yhi = float(np.max(full_finite)) if full_finite.size else 1.0
            if not np.isfinite(ylo) or not np.isfinite(yhi):
                ylo, yhi = -1.0, 1.0
            if abs(yhi - ylo) < 1e-9:
                pad = max(1.0, abs(yhi) * 0.05)
                ylo -= pad
                yhi += pad
            else:
                pad = (yhi - ylo) * 0.06
                ylo -= pad
                yhi += pad

            p.setPen(QColor("#7f8da1"))
            p.drawText(
                QRectF(4, top + row_h - 16, self.LEFT - 9, 14),
                Qt.AlignmentFlag.AlignLeft,
                self._nice_number(ylo),
            )
            p.drawText(
                QRectF(4, top + 1, self.LEFT - 9, 14),
                Qt.AlignmentFlag.AlignRight,
                self._nice_number(yhi),
            )
            if ylo <= 0 <= yhi:
                zy = rect.bottom() - (0 - ylo) / (yhi - ylo) * rect.height()
                p.setPen(QPen(QColor("#e8edf3"), 1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(rect.left(), zy), QPointF(rect.right(), zy))

            def y_for_value(value: float) -> float:
                return rect.bottom() - (value - ylo) / (yhi - ylo) * rect.height()

            color = QColor(series.color)
            p.setPen(QPen(color, 1))
            pixel_count = max(1, int(rect.width()))
            if vv.size > pixel_count * 2:
                # Draw a per-pixel min/max envelope so long recordings remain responsive.
                bins = np.floor(
                    (tv - self._view_t0) / max(1e-9, self._view_t1 - self._view_t0) * pixel_count
                ).astype(np.int64)
                bins = np.clip(bins, 0, pixel_count - 1)
                changes = np.r_[0, np.flatnonzero(np.diff(bins)) + 1]
                mins = np.minimum.reduceat(vv, changes)
                maxs = np.maximum.reduceat(vv, changes)
                bx = bins[changes]
                for px, lo, hi in zip(bx, mins, maxs):
                    if not (np.isfinite(lo) and np.isfinite(hi)):
                        continue
                    x = rect.left() + float(px)
                    p.drawLine(QPointF(x, y_for_value(float(lo))), QPointF(x, y_for_value(float(hi))))
            else:
                path = QPainterPath()
                started = False
                last_t: float | None = None
                series_gap_threshold = self._gap_threshold_for_series(series)
                for t, value in zip(tv, vv):
                    if not np.isfinite(value):
                        started = False
                        last_t = None
                        continue
                    x = self._x_for_time(float(t))
                    y = y_for_value(float(value))
                    gap = (
                        last_t is not None
                        and float(t) - last_t > series_gap_threshold
                    )
                    if not started or gap:
                        path.moveTo(x, y)
                        started = True
                    else:
                        path.lineTo(x, y)
                    last_t = float(t)
                p.drawPath(path)

        self._paint_events(p)
        self._paint_time_axis(p)
        self._paint_playheads(p)

    def _paint_events(self, p: QPainter) -> None:
        lanes = self._lanes_rect()
        if lanes.height() <= 0:
            return
        for i, label in enumerate(self._labels[: self._lane_count()]):
            y = lanes.top() + i * self.LANE_HEIGHT
            p.fillRect(QRectF(lanes.left(), y, lanes.width(), self.LANE_HEIGHT), QColor("#f7f9fc"))
            p.setPen(QColor("#dce3ec"))
            p.drawLine(QPointF(lanes.left(), y), QPointF(lanes.right(), y))
            p.setPen(QColor(label.get("color", "#75839a")))
            p.drawText(
                QRectF(4, y, self.LEFT - 8, self.LANE_HEIGHT),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                str(label.get("name", "")),
            )
        for event in self._events:
            li = int(event.get("li", -1))
            if li < 0 or li >= self._lane_count():
                continue
            t0 = float(event.get("t0", 0.0))
            raw_t1 = event.get("t1")
            y = lanes.top() + li * self.LANE_HEIGHT + 3
            color = QColor(self._labels[li].get("color", "#4f8cff"))
            color.setAlpha(185)
            if raw_t1 is None:
                x = self._x_for_time(t0)
                p.setPen(QPen(color, 2))
                p.drawLine(QPointF(x, y), QPointF(x, y + self.LANE_HEIGHT - 6))
            else:
                t1 = float(raw_t1)
                x0, x1 = self._x_for_time(t0), self._x_for_time(t1)
                p.fillRect(QRectF(min(x0, x1), y, max(2.0, abs(x1 - x0)), self.LANE_HEIGHT - 6), color)

    def _paint_time_axis(self, p: QPainter) -> None:
        plot = self._plot_rect()
        lanes = self._lanes_rect()
        y = lanes.bottom() if lanes.height() else plot.bottom()
        p.setPen(QColor("#6f7c90"))
        for n in range(6):
            ratio = n / 5
            x = plot.left() + ratio * plot.width()
            t = self._view_t0 + ratio * (self._view_t1 - self._view_t0)
            p.drawLine(QPointF(x, y), QPointF(x, y + 4))
            p.drawText(
                QRectF(x - 45, y + 5, 90, 18),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                self._format_time(t),
            )

    def _paint_playheads(self, p: QPainter) -> None:
        plot = self._plot_rect()
        lanes = self._lanes_rect()
        bottom = lanes.bottom() if lanes.height() else plot.bottom()
        if self._view_t0 <= self._playhead_ms <= self._view_t1:
            x = self._x_for_time(self._playhead_ms)
            p.setPen(QPen(QColor("#1268d3"), 2))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, bottom))
        if self._hover_ms is not None and self._view_t0 <= self._hover_ms <= self._view_t1:
            x = self._x_for_time(self._hover_ms)
            p.setPen(QPen(QColor("#2d3748"), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, bottom))
            text = self._format_time(self._hover_ms, True)
            box = QRectF(min(x + 5, self.width() - 100), plot.top() + 4, 94, 20)
            p.fillRect(box, QColor(255, 255, 255, 225))
            p.setPen(QColor("#172033"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        if self._selection_t is not None and self._drag_mode == "select":
            x0 = self._x_for_time(self._selection_t)
            x1 = self._drag_last_x
            p.fillRect(
                QRectF(min(x0, x1), plot.top(), abs(x1 - x0), bottom - plot.top()),
                QColor(18, 104, 211, 38),
            )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self._series:
            return
        self.setFocus()
        self._drag_start_x = event.position().x()
        self._drag_last_x = self._drag_start_x
        self._drag_start_t = self._time_for_x(self._drag_start_x)
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._drag_mode = "pan"
            self._pan_start_view = (self._view_t0, self._view_t1)
        else:
            self._drag_mode = "select"
            self._selection_t = self._drag_start_t
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        x = event.position().x()
        self._hover_ms = self._time_for_x(x)
        self.hoverChanged.emit(self._hover_ms)
        if self._drag_mode == "pan":
            plot = self._plot_rect()
            span = self._pan_start_view[1] - self._pan_start_view[0]
            delta = (self._drag_start_x - x) / max(1.0, plot.width()) * span
            self.set_view(self._pan_start_view[0] + delta, self._pan_start_view[1] + delta, True)
        elif self._drag_mode == "select":
            self._drag_last_x = x
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self._drag_mode:
            return
        x = event.position().x()
        moved = abs(x - self._drag_start_x)
        if self._drag_mode == "select":
            t1 = self._time_for_x(x)
            if moved <= 4:
                self.seekRequested.emit(t1)
            else:
                self.rangeSelected.emit(min(self._drag_start_t, t1), max(self._drag_start_t, t1))
        self._drag_mode = None
        self._selection_t = None
        self.update()
        event.accept()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if not self._drag_mode:
            self._hover_ms = None
            self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if not self._series:
            return
        center = self._time_for_x(event.position().x())
        span = self._view_t1 - self._view_t0
        factor = 0.75 if event.angleDelta().y() > 0 else 1.333333
        new_span = float(np.clip(span * factor, min(200.0, self._duration_ms), self._duration_ms))
        ratio = (center - self._view_t0) / max(1e-9, span)
        self.set_view(center - ratio * new_span, center + (1 - ratio) * new_span, True)
        event.accept()
