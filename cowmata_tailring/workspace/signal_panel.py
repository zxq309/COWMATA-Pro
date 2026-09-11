"""Compact, gap-preserving waveforms and an independent annotation strip."""
from __future__ import annotations

import html

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from cowmata_tailring.ui.interactive_plot import InteractiveSignalPlotWidget

from .clocks import wall_ms, wall_text


def reference_text(clock, milliseconds, with_ms=False):
    value = int(round(clock.map(milliseconds))) if with_ms else int(clock.map(milliseconds))
    return wall_text(value) + (f".{value % 1000:03d}" if with_ms else "")


class TimePositionSpinBox(QDoubleSpinBox):
    """Display/edit reference timestamps while retaining relative seconds internally."""

    def __init__(self, parent=None):
        self.clock = None
        super().__init__(parent)
        self.setKeyboardTracking(False)

    def set_clock(self, clock):
        self.clock = clock if clock and clock.anchors else None
        self.setSuffix("" if self.clock else " 秒")
        self.setMinimumWidth(215 if self.clock else 0)
        self.lineEdit().setText(self.textFromValue(self.value()) + self.suffix())

    def textFromValue(self, value):
        return reference_text(self.clock, value * 1000, True) if self.clock else super().textFromValue(value)

    def valueFromText(self, text):
        if not self.clock:
            return super().valueFromText(text)
        try:
            return self.clock.map(wall_ms(text), inverse=True) / 1000
        except (ValueError, OverflowError):
            return self.value()

    def validate(self, text, pos):
        if not self.clock:
            return super().validate(text, pos)
        try:
            value = self.clock.map(wall_ms(text), inverse=True) / 1000
            valid = self.minimum() - 0.0005 <= value <= self.maximum() + 0.0005
        except (ValueError, OverflowError):
            valid = False
        return (QValidator.State.Acceptable if valid else QValidator.State.Intermediate, text, pos)


class ReviewWaveform(InteractiveSignalPlotWidget):
    GROUPS = (("a", "加速度"), ("g", "角速度"), ("m", "磁场"))
    COLORS = ("#159c8d", "#627de5", "#d59338")
    LEFT = 80

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 92)
        self.set_lanes_visible(False)
        self.group = "all"
        self.clock = None
        self.setToolTip("滚轮缩放 · Shift 拖动平移 · 拖动选择区间 · 悬停读取原始样本")

    def set_clock(self, clock):
        self.clock = clock if clock and clock.anchors else None
        self._invalidate_static()

    def clear_data(self):
        self.clock = None
        super().clear_data()

    def _format_time(self, ms, with_ms=False):
        return reference_text(self.clock, ms, with_ms) if self.clock else super()._format_time(ms, with_ms)

    def set_group(self, group):
        self.group = group
        self._invalidate_static()

    def visible_groups(self):
        groups = [(name, [s for s in self._series if s.key in {prefix + axis for axis in "xyz"}])
                  for prefix, name in self.GROUPS if self.group in {"all", prefix}]
        if self.group == "extra":
            groups = [(s.name, [s]) for s in self._series if s.key in {"temperature", "motion"}]
        return [(name, series) for name, series in groups if series]

    def sample_at(self, series, when):
        """Return a real nearest sample, never interpolate across a gap."""
        ts = series.times_ms
        pos = int(np.searchsorted(ts, when))
        if not len(ts) or when < ts[0] or when > ts[-1]:
            return None
        if 0 < pos < len(ts) and ts[pos] - ts[pos - 1] > self._gap_threshold_for_series(series) and when != ts[pos]:
            return None
        idx = min(pos, len(ts) - 1)
        if pos > 0 and abs(ts[pos - 1] - when) <= abs(ts[idx] - when):
            idx = pos - 1
        value = float(series.values[idx])
        return (float(ts[idx]), value) if np.isfinite(value) else None

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if not self._plot_rect().contains(event.position()) or self._drag_mode or self._event_drag:
            QToolTip.hideText()
            return
        when = self._time_for_x(event.position().x())
        lines = ["<b>" + self._format_time(when, True) + "</b>"]
        for name, series in self.visible_groups():
            values = []
            for item in series:
                sample = self.sample_at(item, when)
                value = "—" if sample is None else f"{sample[1]:.4g} {html.escape(item.unit)}"
                values.append(f"{html.escape(item.key.upper())}: {value}")
            lines.append(html.escape(name) + " · " + " &nbsp; ".join(values))
        lines.append("— 表示缺口或无有效样本；读数来自最近的原始采样点")
        QToolTip.showText(event.globalPosition().toPoint(), "<br>".join(lines), self)

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)

    def paintEvent(self, event):
        if not self._static_cache_matches_widget():
            dpr = self.devicePixelRatioF()
            cache = QPixmap(round(self.width() * dpr), round(self.height() * dpr))
            cache.setDevicePixelRatio(dpr)
            cache.fill(QColor("#ffffff"))
            painter = QPainter(cache)
            self._draw_curves(painter)
            self._paint_intervals(painter)
            painter.end()
            self._static_cache = cache
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._static_cache)
        self._paint_playheads(painter)

    def _paint_intervals(self, painter):
        # Part of the static layer: playback moves the cursor without redrawing
        # every annotation. View, selection and boundary changes invalidate it.
        painter.save()
        painter.setClipRect(self._plot_rect())
        for target in self._events:
            if target.get("t1") is None or target["t1"] <= target["t0"]:
                continue  # Point events have no interval to shade.
            if target["t1"] <= self._view_t0 or target["t0"] >= self._view_t1:
                continue
            x0, x1 = self._x_for_time(target["t0"]), self._x_for_time(target["t1"])
            selected = target["id"] == self._selected_event_id
            li = int(target.get("li", -1))
            color = QColor(self._labels[li].get("color", "#159c8d") if 0 <= li < len(self._labels) else "#159c8d")
            shade = QColor(color)
            shade.setAlpha(45 if selected else 25)
            painter.fillRect(QRectF(x0, self.TOP, max(1, x1 - x0), self._plot_rect().height()), shade)
            painter.setPen(QPen(color, 2 if selected else 1,
                               Qt.PenStyle.SolidLine if target.get("confirmation") == "confirmed" else Qt.PenStyle.DashLine))
            for x in (x0, x1):
                painter.drawLine(QPointF(x, self.TOP), QPointF(x, self._plot_rect().bottom()))
                if selected:
                    painter.fillRect(QRectF(x - 5, self.TOP + 3, 10, 18), color)
        painter.restore()

    def _draw_curves(self, p):
        plot = self._plot_rect()
        groups = self.visible_groups()
        if not groups:
            p.setPen(QColor("#6c8385"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "选择九轴记录后显示波形")
            return
        row_h = plot.height() / len(groups)
        for row, (name, series) in enumerate(groups):
            area = QRectF(plot.left(), plot.top() + row * row_h, plot.width(), row_h)
            if row % 2 == 0:
                p.fillRect(area, QColor("#f4f8f8"))
            p.setPen(QColor("#5f777b"))
            p.drawText(QRectF(8, area.top(), self.LEFT - 12, row_h), Qt.AlignmentFlag.AlignVCenter, name)
            slices = []
            for item in series:
                lo = max(0, np.searchsorted(item.times_ms, self._view_t0) - 1)
                hi = min(len(item.times_ms), np.searchsorted(item.times_ms, self._view_t1, side="right") + 1)
                slices.append((item, item.times_ms[lo:hi], item.values[lo:hi]))
            finite = [values[np.isfinite(values)] for _, _, values in slices if len(values)]
            finite = [values for values in finite if len(values)]
            if not finite:
                continue
            ymin, ymax = min(float(v.min()) for v in finite), max(float(v.max()) for v in finite)
            span = max(1e-6, ymax - ymin)
            p.save()
            p.setClipRect(area)
            for index, (item, times, values) in enumerate(slices):
                if not len(times):
                    continue
                p.setPen(QPen(QColor(self.COLORS[index % 3]), 1.1))
                xs = plot.left() + (times - self._view_t0) / (self._view_t1 - self._view_t0) * plot.width()
                ys = area.bottom() - 5 - (values - ymin) / span * max(1, row_h - 10)
                # Per-pixel min/max envelopes retain brief spikes; no line is
                # drawn between bins, so downsampling cannot bridge real gaps.
                if len(xs) > plot.width() * 2:
                    valid = np.isfinite(ys)
                    xs, ys = xs[valid], ys[valid]
                    if not len(xs):
                        continue
                    bins = np.floor(xs).astype(np.int64)
                    starts = np.r_[0, np.flatnonzero(np.diff(bins)) + 1]
                    mins, maxs = np.minimum.reduceat(ys, starts), np.maximum.reduceat(ys, starts)
                    for x, low, high in zip(bins[starts], mins, maxs):
                        p.drawLine(QPointF(float(x), float(low)), QPointF(float(x), float(high) + .5))
                else:
                    path, previous = QPainterPath(), None
                    threshold = self._gap_threshold_for_series(item)
                    for x, y, when in zip(xs, ys, times):
                        if not np.isfinite(y):
                            previous = None
                            continue
                        if previous is None or when - previous > threshold:
                            path.moveTo(float(x), float(y))
                        else:
                            path.lineTo(float(x), float(y))
                        previous = when
                    p.drawPath(path)
            p.restore()
        self._paint_time_axis(p)

    def _paint_time_axis(self, painter):
        plot = self._plot_rect()
        painter.setPen(QColor("#78908c"))
        # Centering then clamping edge labels can make them overlap even when
        # the tick spacing fits. Check the final text rectangles, including
        # proportional-font widths, and reduce the number of complete labels.
        for count in range(6, 0, -1):
            ticks = []
            for index in range(count):
                fraction = index / (count - 1) if count > 1 else 0
                x = plot.left() + fraction * plot.width()
                when = self._view_t0 + fraction * (self._view_t1 - self._view_t0)
                text = self._format_time(when)
                width = max(90, painter.fontMetrics().horizontalAdvance(text) + 12)
                left = max(0, min(self.width() - width, x - width / 2))
                ticks.append((x, QRectF(left, plot.bottom() + 5, width, 18), text))
            if all(left[1].right() + 8 <= right[1].left() for left, right in zip(ticks, ticks[1:])):
                break
        for x, rectangle, text in ticks:
            painter.drawLine(QPointF(x, plot.bottom()), QPointF(x, plot.bottom() + 4))
            painter.drawText(rectangle, Qt.AlignmentFlag.AlignCenter, text)


class EventStrip(QWidget):
    selected = Signal(int)

    def __init__(self, wave, parent=None):
        super().__init__(parent)
        self.wave = wave
        self.hits = []
        self.setMinimumHeight(26)
        self.setMouseTracking(True)
        self.setToolTip("单击标签选中；拖动左右两端调整起止，拖动中间整体平移。波形上也可拖动所选标签边界；修改后需复核。")

    def refresh(self):
        used = sorted({int(e["li"]) for e in self.wave._events if 0 <= int(e["li"]) < len(self.wave._labels)})
        self.setFixedHeight(max(26, len(used) * 26))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#f0f6f5"))
        labels, events = self.wave._labels, self.wave._events
        used = sorted({int(e["li"]) for e in events if 0 <= int(e["li"]) < len(labels)})
        self.hits = []
        if not used:
            p.setPen(QColor("#738687"))
            p.drawText(self.rect().adjusted(10, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter,
                       "标签轨道")
        for row, li in enumerate(used):
            label = labels[li]
            p.setPen(QColor("#496162"))
            p.drawText(QRectF(8, row * 26, 70, 26), Qt.AlignmentFlag.AlignVCenter,
                       p.fontMetrics().elidedText(str(label["name"]), Qt.TextElideMode.ElideRight, 68))
            p.save()
            p.setClipRect(QRectF(self.wave.LEFT, row * 26, self.width() - self.wave.LEFT, 26))
            for item in (e for e in events if int(e["li"]) == li):
                x0 = self.wave._x_for_time(item["t0"])
                x1 = self.wave._x_for_time(item["t1"] if item.get("t1") is not None else item["t0"])
                rect = QRectF(x0, row * 26 + 4, max(5, x1 - x0), 18)
                color = QColor(label.get("color", "#159c8d"))
                color.setAlpha(170)
                p.setBrush(color)
                pen = QPen(QColor("#597a76"), 1, Qt.PenStyle.DashLine) if item.get("confirmation") != "confirmed" else QPen(Qt.PenStyle.NoPen)
                if item["id"] == self.wave._selected_event_id:
                    pen = QPen(QColor("#124f50"), 2)
                p.setPen(pen)
                p.drawRoundedRect(rect, 4, 4)
                if item["id"] == self.wave._selected_event_id and item.get("t1") is not None:
                    for edge in (x0, x1):
                        p.fillRect(QRectF(edge - 3, row * 26 + 5, 6, 16), QColor("#ffffff"))
                self.hits.append((rect, int(item["id"])))
            p.restore()
        x = self.wave._x_for_time(self.wave._playhead_ms)
        if self.wave.LEFT <= x <= self.width():
            p.setPen(QPen(QColor("#1268d3"), 1))
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().x() >= self.wave.LEFT:
            for rect, identifier in reversed(self.hits):
                if rect.contains(event.position()):
                    self.wave.set_selected_event(identifier)
                    target = self.wave._selected_event()
                    if target and getattr(self.wave, "event_editable", True):
                        mode = self._drag_mode(rect, target, event.position().x())
                        self.wave._event_drag = {"event": target, "mode": mode, "start_x": event.position().x(),
                                                 "t0": target["t0"], "t1": target.get("t1")}
                    self.selected.emit(identifier)
                    self.update()
                    event.accept()
                    return

    @staticmethod
    def _drag_mode(rect, target, x):
        if target.get("t1") is not None:
            if abs(x - rect.left()) <= 9:
                return "left"
            if abs(x - rect.right()) <= 9:
                return "right"
        return "move"

    def mouseMoveEvent(self, event):
        if self.wave._event_drag is not None:
            # The strip and waveform share the same x/time scale and constraints.
            self.wave.mouseMoveEvent(event)
            self.update()
            return
        for rect, identifier in reversed(self.hits):
            if rect.contains(event.position()):
                target = next(e for e in self.wave._events if e["id"] == identifier)
                mode = self._drag_mode(rect, target, event.position().x())
                self.setCursor(Qt.CursorShape.SizeAllCursor if mode == "move" else Qt.CursorShape.SizeHorCursor)
                return
        self.unsetCursor()

    def mouseReleaseEvent(self, event):
        if self.wave._event_drag is not None:
            self.wave.mouseReleaseEvent(event)
            self.update()
        self.unsetCursor()


class SignalPanel(QWidget):
    """Controller-compatible plot API with physically separate event rows."""
    seekRequested = Signal(float)
    rangeSelected = Signal(float, float)
    eventSelected = Signal(int)
    eventChanged = Signal(int, float, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("signalCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.wave = ReviewWaveform()
        self.toolbar = QHBoxLayout()
        self.toolbar.setSpacing(8)
        self.toolbar.setContentsMargins(10, 2, 10, 2)
        title = QLabel("九轴信号")
        title.setObjectName("sectionTitle")
        self.toolbar.addWidget(title)
        self.group = QComboBox()
        self.group.addItems(["九轴 · XYZ 分组", "加速度", "角速度", "磁场", "温度 / 活动量"])
        self.group.currentIndexChanged.connect(lambda i: self.wave.set_group(["all", "a", "g", "m", "extra"][i]))
        self.toolbar.addWidget(self.group)
        legend = QLabel('<span style="color:#159c8d">X</span> / <span style="color:#627de5">Y</span> / <span style="color:#d59338">Z</span>')
        legend.setStyleSheet("color:#6c8385; font-size:11px")
        legend.setToolTip("X：青绿 · Y：蓝紫 · Z：琥珀；同组共用物理尺度，悬停显示数值与单位")
        self.toolbar.addWidget(legend)
        hint = QLabel("标签：拖动两端改起止 · 拖动中间平移")
        hint.setToolTip("先单击标注列表或标签轨道选中；波形上的左右手柄也可直接拖动。修改后请回看复核。")
        self.toolbar.addWidget(hint)
        self.toolbar.addStretch(1)
        layout.addLayout(self.toolbar)
        layout.addWidget(self.wave, 1)
        self.track = EventStrip(self.wave)
        self.track.setToolTip(self.track.toolTip() + "；虚线轮廓表示待复核，不代表已确认真值")
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setMaximumHeight(56)
        self.scroll.setWidget(self.track)
        layout.addWidget(self.scroll)
        for name in ("seekRequested", "rangeSelected", "eventSelected", "eventChanged"):
            getattr(self.wave, name).connect(getattr(self, name).emit)
        self.track.selected.connect(self.eventSelected.emit)
        self.wave.viewChanged.connect(lambda *_: self.track.update())

    @property
    def view_range(self):
        return self.wave.view_range

    def set_data(self, *args, **kwargs):
        self.wave.set_data(*args, **kwargs)
        self.track.refresh()

    def set_clock(self, clock):
        self.wave.set_clock(clock)

    def clear_data(self):
        self.wave.clear_data()
        self.track.refresh()

    def set_events(self, labels, events):
        self.wave.set_events(labels, events)
        self.track.refresh()
        self.scroll.setFixedHeight(min(56, self.track.height()))

    def set_view(self, *args, **kwargs):
        self.wave.set_view(*args, **kwargs)
        self.track.update()

    def set_playhead(self, when):
        self.wave.set_playhead(when)
        # Restored sessions, typed positions and long playback must keep the
        # current sample visible. Preserve zoom and avoid recentering each frame.
        self.wave.center_on(self.wave._playhead_ms)
        self.track.update()

    def set_selected_event(self, identifier):
        self.wave.set_selected_event(identifier)
        self.track.update()

    def focus_event(self, *args, **kwargs):
        result = self.wave.focus_event(*args, **kwargs)
        self.track.update()
        return result
