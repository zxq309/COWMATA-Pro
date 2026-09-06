from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPen

from cowmata_tailring.ui.cached_widgets import CachedSignalPlotWidget


class InteractiveSignalPlotWidget(CachedSignalPlotWidget):
    eventSelected = Signal(int)
    eventChanged = Signal(int, float, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._event_drag: dict[str, Any] | None = None
        self._selected_event_id: int | None = None

    def set_events(self, labels, events) -> None:
        super().set_events(labels, events)
        if self._selected_event_id is not None and not any(
            int(item.get("id", -1)) == self._selected_event_id
            for item in self._events
        ):
            self._selected_event_id = None

    def clear_data(self) -> None:
        self._selected_event_id = None
        super().clear_data()

    def set_selected_event(self, event_id: int | None) -> None:
        selected = None if event_id is None else int(event_id)
        if selected == self._selected_event_id:
            return
        self._selected_event_id = selected
        self._invalidate_static()

    def _selected_event(self) -> dict[str, Any] | None:
        if self._selected_event_id is None:
            return None
        return next(
            (
                item
                for item in self._events
                if int(item.get("id", -1)) == self._selected_event_id
            ),
            None,
        )

    def focus_event(
        self,
        event_id: int,
        *,
        minimum_span_ms: float = 30_000.0,
        padding_ratio: float = 0.25,
    ) -> bool:
        """Select an interval and fit it into a waveform review window."""

        self.set_selected_event(event_id)
        target = self._selected_event()
        if target is None:
            return False
        start = float(target.get("t0", 0.0))
        raw_end = target.get("t1")
        if raw_end is None:
            span = min(self._duration_ms, max(1.0, float(minimum_span_ms)))
            self.set_view(start - span / 2.0, start + span / 2.0, True)
            return True
        end = float(raw_end)
        duration = max(0.0, end - start)
        span = max(
            float(minimum_span_ms),
            duration * (1.0 + 2.0 * max(0.0, float(padding_ratio))),
        )
        span = min(self._duration_ms, max(1.0, span))
        padding = max(0.0, span - duration) / 2.0
        self.set_view(start - padding, end + padding, True)
        return True

    def _selected_boundary_hit(self, x: float, y: float):
        target = self._selected_event()
        if target is None or target.get("t1") is None:
            return None
        plot = self._plot_rect()
        if not plot.contains(x, y):
            return None
        start_x = self._x_for_time(float(target.get("t0", 0.0)))
        end_x = self._x_for_time(float(target["t1"]))
        tolerance = 9.0
        if abs(x - start_x) <= tolerance:
            return target, "left"
        if abs(x - end_x) <= tolerance:
            return target, "right"
        return None

    def _hit_event(self, x: float, y: float):
        selected_boundary = self._selected_boundary_hit(x, y)
        if selected_boundary is not None:
            return selected_boundary
        lanes = self._lanes_rect()
        if not lanes.contains(x, y) or not self._labels:
            return None
        label_index = int((y - lanes.top()) // self.LANE_HEIGHT)
        tolerance = 6.0
        for event in reversed(self._events):
            if int(event.get("li", -1)) != label_index:
                continue
            x0 = self._x_for_time(float(event.get("t0", 0)))
            raw_end = event.get("t1")
            if raw_end is None:
                if abs(x - x0) <= tolerance:
                    return event, "move"
                continue
            x1 = self._x_for_time(float(raw_end))
            left, right = min(x0, x1), max(x0, x1)
            if abs(x - left) <= tolerance:
                return event, "left"
            if abs(x - right) <= tolerance:
                return event, "right"
            if left < x < right:
                return event, "move"
        return None

    def _paint_events(self, painter) -> None:
        self._paint_selected_waveform_interval(painter)
        super()._paint_events(painter)
        self._paint_selected_lane_handles(painter)

    def _paint_selected_waveform_interval(self, painter) -> None:
        target = self._selected_event()
        if target is None or target.get("t1") is None:
            return
        label_index = int(target.get("li", -1))
        color = QColor("#2676d9")
        if 0 <= label_index < len(self._labels):
            color = QColor(self._labels[label_index].get("color", "#2676d9"))
        start = float(target.get("t0", 0.0))
        end = float(target["t1"])
        x0 = self._x_for_time(start)
        x1 = self._x_for_time(end)
        left, right = min(x0, x1), max(x0, x1)
        plot = self._plot_rect()

        shade = QColor(color)
        shade.setAlpha(34)
        painter.fillRect(
            QRectF(left, plot.top(), max(2.0, right - left), plot.height()),
            shade,
        )
        boundary = QColor(color)
        boundary.setAlpha(235)
        painter.setPen(QPen(boundary, 2))
        for x in (x0, x1):
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            handle = QRectF(x - 5.0, plot.top() + 3.0, 10.0, 18.0)
            painter.fillRect(handle, boundary)

        label_name = "标注"
        if 0 <= label_index < len(self._labels):
            label_name = str(self._labels[label_index].get("name", label_name))
        text = (
            f"{label_name}  {self._format_time(start, True)} — "
            f"{self._format_time(end, True)}"
        )
        text_width = min(350.0, max(180.0, right - left))
        text_left = max(plot.left(), min(left + 8.0, plot.right() - text_width))
        badge = QRectF(text_left, plot.top() + 4.0, text_width, 18.0)
        badge_color = QColor("#ffffff")
        badge_color.setAlpha(225)
        painter.fillRect(badge, badge_color)
        painter.setPen(boundary)
        painter.drawText(
            badge.adjusted(5.0, 0.0, -3.0, 0.0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            text,
        )

    def _paint_selected_lane_handles(self, painter) -> None:
        target = self._selected_event()
        if target is None or target.get("t1") is None:
            return
        label_index = int(target.get("li", -1))
        if label_index < 0 or label_index >= self._lane_count():
            return
        lanes = self._lanes_rect()
        y = lanes.top() + label_index * self.LANE_HEIGHT + 2.0
        x0 = self._x_for_time(float(target.get("t0", 0.0)))
        x1 = self._x_for_time(float(target["t1"]))
        left, right = min(x0, x1), max(x0, x1)
        painter.setPen(QPen(QColor("#0b3d78"), 2))
        painter.drawRect(
            QRectF(left, y, max(2.0, right - left), self.LANE_HEIGHT - 4.0)
        )
        painter.fillRect(
            QRectF(x0 - 3.0, y, 6.0, self.LANE_HEIGHT - 4.0),
            QColor("#ffffff"),
        )
        painter.fillRect(
            QRectF(x1 - 3.0, y, 6.0, self.LANE_HEIGHT - 4.0),
            QColor("#ffffff"),
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._hit_event(
                event.position().x(), event.position().y()
            )
            if hit is not None:
                target, mode = hit
                if not getattr(self, "event_editable", True):
                    self.eventSelected.emit(int(target.get("id", -1)))
                    event.accept()
                    return
                self._event_drag = {
                    "event": target,
                    "mode": mode,
                    "start_x": event.position().x(),
                    "t0": float(target.get("t0", 0)),
                    "t1": (
                        None
                        if target.get("t1") is None
                        else float(target["t1"])
                    ),
                }
                self.eventSelected.emit(int(target.get("id", -1)))
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._event_drag is None:
            hit = self._hit_event(event.position().x(), event.position().y())
            if hit is None:
                self.unsetCursor()
            elif hit[1] in {"left", "right"}:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            super().mouseMoveEvent(event)
            return
        drag = self._event_drag
        target = drag["event"]
        start_time = self._time_for_x(drag["start_x"])
        now = self._time_for_x(event.position().x())
        delta = now - start_time
        original_t0 = drag["t0"]
        original_t1 = drag["t1"]
        mode = drag["mode"]
        if mode == "move":
            if original_t1 is None:
                target["t0"] = max(
                    0.0, min(self._duration_ms, original_t0 + delta)
                )
            else:
                duration = original_t1 - original_t0
                new_t0 = max(
                    0.0,
                    min(self._duration_ms - duration, original_t0 + delta),
                )
                target["t0"] = new_t0
                target["t1"] = new_t0 + duration
        elif mode == "left" and original_t1 is not None:
            candidate = max(
                0.0, min(original_t1, original_t0 + delta)
            )
            if candidate < original_t1:
                target["t0"] = candidate
        elif mode == "right" and original_t1 is not None:
            candidate = min(
                self._duration_ms, max(original_t0, original_t1 + delta)
            )
            if candidate > original_t0:
                target["t1"] = candidate
        self._invalidate_static()
        event.accept()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._event_drag is None:
            self.unsetCursor()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._event_drag is None:
            super().mouseReleaseEvent(event)
            return
        target = self._event_drag["event"]
        self._event_drag = None
        self.unsetCursor()
        self._invalidate_static()
        self.eventChanged.emit(
            int(target.get("id", -1)),
            float(target.get("t0", 0)),
            target.get("t1"),
        )
        event.accept()
