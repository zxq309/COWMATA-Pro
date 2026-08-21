from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from cowmata_tailring.ui.helpers import format_relative


class VideoTimelineWidget(QWidget):
    seekRequested = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(54)
        self.setMaximumHeight(64)
        self.setMouseTracking(True)
        self._duration_ms = 0.0
        self._position_ms = 0.0
        self._dragging = False

    def set_duration(self, duration_ms: float) -> None:
        self._duration_ms = max(0.0, float(duration_ms))
        self._position_ms = min(self._position_ms, self._duration_ms)
        self.update()

    def set_position(self, position_ms: float) -> None:
        self._position_ms = max(
            0.0, min(float(position_ms), self._duration_ms)
        )
        self.update()

    def _track(self) -> QRectF:
        return QRectF(12, 8, max(1, self.width() - 24), 24)

    def _time_for_x(self, x: float) -> float:
        track = self._track()
        ratio = (x - track.left()) / max(1.0, track.width())
        return max(0.0, min(1.0, ratio)) * self._duration_ms

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f7f9fc"))
        track = self._track()
        painter.fillRect(track, QColor("#e4eaf2"))
        painter.setPen(QPen(QColor("#c3cedc"), 1))
        painter.drawRect(track)
        if self._duration_ms > 0:
            ratio = self._position_ms / self._duration_ms
            x = track.left() + ratio * track.width()
            painter.fillRect(
                QRectF(track.left(), track.top(), x - track.left(), track.height()),
                QColor(82, 139, 216, 72),
            )
            painter.setPen(QPen(QColor("#1268d3"), 2))
            painter.drawLine(
                QPointF(x, track.top() - 3),
                QPointF(x, track.bottom() + 3),
            )
        painter.setPen(QColor("#536277"))
        painter.drawText(
            QRectF(track.left(), 35, 130, 18),
            Qt.AlignmentFlag.AlignLeft,
            format_relative(self._position_ms),
        )
        painter.drawText(
            QRectF(track.right() - 130, 35, 130, 18),
            Qt.AlignmentFlag.AlignRight,
            format_relative(self._duration_ms),
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._duration_ms > 0
        ):
            self._dragging = True
            value = self._time_for_x(event.position().x())
            self.set_position(value)
            self.seekRequested.emit(value)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._dragging:
            value = self._time_for_x(event.position().x())
            self.set_position(value)
            self.seekRequested.emit(value)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

