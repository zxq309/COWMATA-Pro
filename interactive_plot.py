from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal

from cached_widgets import CachedSignalPlotWidget


class InteractiveSignalPlotWidget(CachedSignalPlotWidget):
    eventSelected = Signal(int)
    eventChanged = Signal(int, float, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._event_drag: dict[str, Any] | None = None

    def _hit_event(self, x: float, y: float):
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

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._hit_event(
                event.position().x(), event.position().y()
            )
            if hit is not None:
                target, mode = hit
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
            target["t0"] = max(
                0.0, min(original_t1, original_t0 + delta)
            )
        elif mode == "right" and original_t1 is not None:
            target["t1"] = min(
                self._duration_ms, max(original_t0, original_t1 + delta)
            )
        self._invalidate_static()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._event_drag is None:
            super().mouseReleaseEvent(event)
            return
        target = self._event_drag["event"]
        self._event_drag = None
        self._invalidate_static()
        self.eventChanged.emit(
            int(target.get("id", -1)),
            float(target.get("t0", 0)),
            target.get("t1"),
        )
        event.accept()

