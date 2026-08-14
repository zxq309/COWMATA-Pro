from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen

from interactive_plot import InteractiveSignalPlotWidget
from ui_helpers import format_relative, format_wall
from video_timeline import VideoTimelineWidget


class PrecisionSignalPlotWidget(InteractiveSignalPlotWidget):
    """A plot whose pointer actions use real decoded IMU sample times."""

    TIMESTAMP_MODE_LABELS = {
        "fixed-20ms": "固定 20 ms 推算",
        "frame-delta-ms": "帧间隔累计",
        "device-timestamp-ms": "设备相对时间",
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._sample_times_ms = np.empty(0, dtype=float)
        self._record_start_epoch_ms = 0
        self._timestamp_mode = ""
        self._sample_gap_threshold_ms = 100.0
        self._last_pointer_raw_ms: float | None = None

    def configure_time_context(self, record_start_epoch_ms: int) -> None:
        """Compatibility helper for callers that only have a record anchor."""

        self._record_start_epoch_ms = int(record_start_epoch_ms or 0)
        self.update()

    def set_time_reference(
        self,
        times_ms,
        record_start_epoch_ms: int,
        timestamp_mode: str = "",
        gap_threshold_ms: float = 100.0,
    ) -> None:
        """Set the authoritative IMU time array used by pointer actions."""

        values = np.asarray(times_ms, dtype=float)
        if values.ndim != 1:
            raise ValueError("IMU sample timestamps must be one-dimensional")
        if values.size and not np.all(np.isfinite(values)):
            raise ValueError("IMU sample timestamps contain non-finite values")
        if values.size > 1 and np.any(np.diff(values) < 0):
            raise ValueError("IMU sample timestamps are not ordered")
        self._sample_times_ms = values
        self._record_start_epoch_ms = int(record_start_epoch_ms or 0)
        self._timestamp_mode = str(timestamp_mode or "")
        self._sample_gap_threshold_ms = max(
            1.0, float(gap_threshold_ms or 100.0)
        )
        self._last_pointer_raw_ms = None
        self.update()

    def set_data(
        self,
        series,
        duration_ms: float,
        gap_threshold_ms: float = 100.0,
    ) -> None:
        items = list(series)
        candidates = [
            np.asarray(item.times_ms, dtype=float)
            for item in items
            if np.asarray(item.times_ms).size
        ]
        self._sample_times_ms = (
            max(candidates, key=lambda values: values.size)
            if candidates
            else np.empty(0, dtype=float)
        )
        self._sample_gap_threshold_ms = max(
            1.0, float(gap_threshold_ms or 100.0)
        )
        super().set_data(items, duration_ms, gap_threshold_ms)

    def clear_data(self) -> None:
        self._sample_times_ms = np.empty(0, dtype=float)
        self._record_start_epoch_ms = 0
        self._timestamp_mode = ""
        self._sample_gap_threshold_ms = 100.0
        self._last_pointer_raw_ms = None
        super().clear_data()

    def snap_time(self, time_ms: float) -> float:
        """Return the nearest real IMU sample time; equal ties choose earlier."""

        times = self._sample_times_ms
        if times.size == 0:
            return float(np.clip(time_ms, 0.0, self._duration_ms))
        position = int(np.searchsorted(times, float(time_ms), side="left"))
        if position <= 0:
            return float(times[0])
        if position >= times.size:
            return float(times[-1])
        before = float(times[position - 1])
        after = float(times[position])
        return (
            before
            if abs(float(time_ms) - before)
            <= abs(after - float(time_ms))
            else after
        )

    def sample_info(self, time_ms: float) -> tuple[int, float]:
        exact = self.snap_time(time_ms)
        times = self._sample_times_ms
        if times.size == 0:
            return -1, exact
        position = int(np.searchsorted(times, exact, side="left"))
        position = max(0, min(position, int(times.size) - 1))
        return position, float(times[position])

    def _time_for_x(self, x: float) -> float:
        raw_ms = super()._time_for_x(x)
        self._last_pointer_raw_ms = raw_ms
        return self.snap_time(raw_ms)

    def _pointer_is_in_gap(
        self, raw_ms: float | None
    ) -> tuple[bool, float]:
        times = self._sample_times_ms
        if raw_ms is None or times.size < 2:
            return False, 0.0
        position = int(np.searchsorted(times, raw_ms, side="left"))
        if position <= 0 or position >= times.size:
            return False, 0.0
        before = float(times[position - 1])
        after = float(times[position])
        in_gap = (
            before < raw_ms < after
            and after - before > self._sample_gap_threshold_ms
        )
        nearest_distance = min(
            abs(raw_ms - before), abs(after - raw_ms)
        )
        return in_gap, nearest_distance

    def _paint_playheads(self, painter: QPainter) -> None:
        # Keep the normal playhead and selection, but replace its interpolated
        # hover box with details from an actual IMU sample.
        hover = self._hover_ms
        self._hover_ms = None
        try:
            super()._paint_playheads(painter)
        finally:
            self._hover_ms = hover

        if (
            hover is None
            or not (self._view_t0 <= hover <= self._view_t1)
        ):
            return

        sample_index, exact_ms = self.sample_info(hover)
        in_gap, nearest_distance = self._pointer_is_in_gap(
            self._last_pointer_raw_ms
        )
        x = self._x_for_time(exact_ms)
        plot = self._plot_rect()
        lanes = self._lanes_rect()
        bottom = lanes.bottom() if lanes.height() else plot.bottom()
        painter.setPen(
            QPen(
                QColor("#c77700") if in_gap else QColor("#2d3748"),
                1,
                Qt.PenStyle.DashLine,
            )
        )
        painter.drawLine(QPointF(x, plot.top()), QPointF(x, bottom))

        line1 = (
            f"样本 #{sample_index + 1:,} / "
            f"{self._sample_times_ms.size:,}  ·  "
            f"相对 {format_relative(exact_ms)}"
            if sample_index >= 0
            else f"相对 {format_relative(exact_ms)}"
        )
        line2 = (
            "记录时间 "
            + format_wall(self._record_start_epoch_ms + exact_ms)
            if self._record_start_epoch_ms
            else ""
        )
        mode_label = self.TIMESTAMP_MODE_LABELS.get(
            self._timestamp_mode,
            self._timestamp_mode or "未声明",
        )
        line3 = "时间来源 " + mode_label
        line4 = (
            f"采样间隙 · 最近样本相差 {nearest_distance:,.3f} ms"
            if in_gap
            else ""
        )
        lines = [line1]
        if line2:
            lines.append(line2)
        lines.append(line3)
        if line4:
            lines.append(line4)

        box_width = 348.0
        box_height = 6.0 + 18.0 * len(lines)
        box_x = (
            x + 6.0
            if x + 6.0 + box_width <= self.width() - 4.0
            else x - box_width - 6.0
        )
        box_x = max(4.0, box_x)
        box = QRectF(
            box_x, plot.top() + 4.0, box_width, box_height
        )
        painter.fillRect(
            box,
            QColor(255, 249, 235, 244)
            if in_gap
            else QColor(255, 255, 255, 238),
        )
        painter.setPen(
            QPen(
                QColor("#dc8a00") if in_gap else QColor("#b9c4d2"),
                1,
            )
        )
        painter.drawRect(box)
        for line_index, line in enumerate(lines):
            painter.setPen(
                QColor("#a15c00")
                if in_gap and line == line4
                else QColor("#172033")
                if line_index == 0
                else QColor("#526277")
            )
            painter.drawText(
                QRectF(
                    box.left() + 6.0,
                    box.top() + 2.0 + 18.0 * line_index,
                    box.width() - 12.0,
                    18.0,
                ),
                Qt.AlignmentFlag.AlignLeft
                | Qt.AlignmentFlag.AlignVCenter,
                line,
            )


class PrecisionVideoTimelineWidget(VideoTimelineWidget):
    """Video timeline whose selection and readout agree to one millisecond."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._hover_ms: float | None = None

    def _time_for_x(self, x: float) -> float:
        # libVLC's public seek API accepts integer milliseconds.  Showing the
        # same rounded value avoids a false sub-millisecond precision claim.
        return float(round(super()._time_for_x(x)))

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._hover_ms is None or self._duration_ms <= 0:
            return
        painter = QPainter(self)
        track = self._track()
        ratio = self._hover_ms / self._duration_ms
        x = track.left() + ratio * track.width()
        painter.setPen(
            QPen(QColor("#2d3748"), 1, Qt.PenStyle.DashLine)
        )
        painter.drawLine(
            QPointF(x, track.top() - 3.0),
            QPointF(x, track.bottom() + 3.0),
        )
        width = 164.0
        box_x = (
            x + 5.0
            if x + width + 5.0 <= self.width()
            else x - width - 5.0
        )
        box = QRectF(
            max(2.0, box_x), track.top() + 2.0, width, 20.0
        )
        painter.fillRect(box, QColor(255, 255, 255, 238))
        painter.setPen(QColor("#172033"))
        painter.drawText(
            box,
            Qt.AlignmentFlag.AlignCenter,
            "视频位置 " + format_relative(self._hover_ms),
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._duration_ms > 0:
            self._hover_ms = self._time_for_x(event.position().x())
        super().mousePressEvent(event)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._duration_ms > 0:
            self._hover_ms = self._time_for_x(event.position().x())
        super().mouseMoveEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            self._hover_ms = None
            self.update()
        super().leaveEvent(event)
