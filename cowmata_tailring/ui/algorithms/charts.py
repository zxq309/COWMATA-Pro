"""Lightweight QPainter charts for algorithm training / prediction windows (no extra deps)."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

PALETTE = ("#187b78", "#d0742c", "#5465c8", "#9a4fb0", "#3f9c3a", "#c23b4a", "#8a6d1f", "#2f8fc7", "#6b6b6b", "#b05c8f")
BACKGROUND = QColor("#fbfcf8")
AXIS = QColor("#56636f")
GRID = QColor("#e3e7e0")


def _nice(lo, hi):
    if not math.isfinite(lo) or not math.isfinite(hi):
        return 0.0, 1.0
    if hi - lo < 1e-12:
        pad = abs(hi) * 0.1 or 1.0
        return lo - pad, hi + pad
    pad = (hi - lo) * 0.05
    return lo - pad, hi + pad


def _fmt(value):
    if abs(value) >= 1000 or (abs(value) < 0.01 and value != 0):
        return f"{value:.2g}"
    return f"{value:.3g}"


class LineChart(QWidget):
    """Series of (x, y) lines with optional shaded bands, reference lines and legend."""

    def __init__(self, parent=None, *, title="暂无数据", xlabel="", ylabel=""):
        super().__init__(parent)
        self.title, self.xlabel, self.ylabel = title, xlabel, ylabel
        self.series, self.bands, self.hlines, self.diagonal = [], [], [], False
        self.xrange = self.yrange = None
        self.xticks = None
        self.setMinimumHeight(220)

    def clear(self, title=None):
        self.series, self.bands, self.hlines, self.diagonal = [], [], [], False
        self.xrange = self.yrange = self.xticks = None
        if title is not None:
            self.title = title
        self.update()

    def set_data(self, title, series, *, bands=(), hlines=(), diagonal=False, xlabel=None, ylabel=None,
                 xrange=None, yrange=None, xticks=None):
        """series: [(name, [(x, y), ...])]; bands: [(name, [(x, lo, hi), ...])]; hlines: [(name, y)]."""
        self.title = title
        self.series = [(str(n), [(float(x), float(y)) for x, y in pts if _ok(x) and _ok(y)]) for n, pts in series]
        self.bands = [(str(n), [(float(x), float(a), float(b)) for x, a, b in pts if _ok(x) and _ok(a) and _ok(b)])
                      for n, pts in bands]
        self.hlines = [(str(n), float(y)) for n, y in hlines if _ok(y)]
        self.diagonal = diagonal
        self.xlabel = self.xlabel if xlabel is None else xlabel
        self.ylabel = self.ylabel if ylabel is None else ylabel
        self.xrange, self.yrange, self.xticks = xrange, yrange, xticks
        self.setToolTip("\n".join(f"{n}: {len(p)} 点" for n, p in self.series))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), BACKGROUND)
        p.setPen(QColor("#234139"))
        p.drawText(QRectF(10, 4, self.width() - 20, 22), Qt.AlignmentFlag.AlignLeft, self.title)
        xs = [x for _, pts in self.series for x, _ in pts] + [x for _, pts in self.bands for x, *_ in pts]
        ys = ([y for _, pts in self.series for _, y in pts] + [v for _, pts in self.bands for _, a, b in pts for v in (a, b)]
              + [y for _, y in self.hlines])
        if not xs:
            p.drawText(QRectF(0, 30, self.width(), self.height() - 40), Qt.AlignmentFlag.AlignCenter, "暂无结果")
            return
        x0, x1 = self.xrange or _nice(min(xs), max(xs))
        y0, y1 = self.yrange or _nice(min(ys), max(ys))
        left, top, right, bottom = 58, 30, self.width() - 14, self.height() - 42
        legend_w = 0
        if len(self.series) + len(self.bands) > 1 or self.hlines or self.bands:
            names = [n for n, _ in self.series] + [n + "（区间）" for n, _ in self.bands] + [n for n, _ in self.hlines]
            legend_w = min(230, 40 + max(p.fontMetrics().horizontalAdvance(n) for n in names))
            right -= legend_w
        if right - left < 40 or bottom - top < 30:
            return

        def px(x):
            return left + (x - x0) / max(x1 - x0, 1e-12) * (right - left)

        def py(y):
            return bottom - (y - y0) / max(y1 - y0, 1e-12) * (bottom - top)

        for yv in _ticks(y0, y1, 5):
            p.setPen(QPen(GRID, 1))
            p.drawLine(QPointF(left, py(yv)), QPointF(right, py(yv)))
            p.setPen(AXIS)
            p.drawText(QRectF(2, py(yv) - 8, left - 6, 16), Qt.AlignmentFlag.AlignRight, _fmt(yv))
        ticks = self.xticks or [(v, None) for v in _ticks(x0, x1)]
        for xv, label in ticks:
            p.setPen(QPen(GRID, 1))
            p.drawLine(QPointF(px(xv), top), QPointF(px(xv), bottom))
            p.setPen(AXIS)
            p.drawText(QRectF(px(xv) - 40, bottom + 3, 80, 16), Qt.AlignmentFlag.AlignCenter,
                       label if label is not None else _fmt(xv))
        p.setPen(QPen(AXIS, 1))
        p.drawRect(QRectF(left, top, right - left, bottom - top))
        p.drawText(QRectF(left, bottom + 20, right - left, 18), Qt.AlignmentFlag.AlignCenter, self.xlabel)
        p.save()
        p.translate(12, (top + bottom) / 2)
        p.rotate(-90)
        p.drawText(QRectF(-100, -10, 200, 18), Qt.AlignmentFlag.AlignCenter, self.ylabel)
        p.restore()
        p.setClipRect(QRectF(left, top, right - left, bottom - top))
        if self.diagonal:
            p.setPen(QPen(QColor("#9aa39a"), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(px(x0), py(y0)), QPointF(px(x1), py(y1)))
        for k, (_, pts) in enumerate(self.bands):
            if len(pts) < 2:
                continue
            color = QColor(PALETTE[k % len(PALETTE)])
            color.setAlpha(55)
            path = QPainterPath(QPointF(px(pts[0][0]), py(pts[0][2])))
            for x, _, hi in pts[1:]:
                path.lineTo(px(x), py(hi))
            for x, lo, _ in reversed(pts):
                path.lineTo(px(x), py(lo))
            path.closeSubpath()
            p.fillPath(path, color)
        for _, y in self.hlines:
            p.setPen(QPen(QColor("#c23b4a"), 1.2, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(left, py(y)), QPointF(right, py(y)))
        for k, (_, pts) in enumerate(self.series):
            color = QColor(PALETTE[k % len(PALETTE)])
            p.setPen(QPen(color, 2))
            for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
                p.drawLine(QPointF(px(xa), py(ya)), QPointF(px(xb), py(yb)))
            if len(pts) <= 60:
                for x, y in pts:
                    p.drawEllipse(QPointF(px(x), py(y)), 2.2, 2.2)
        p.setClipping(False)
        if legend_w:
            y = top + 4
            entries = ([(n, PALETTE[k % len(PALETTE)], "line") for k, (n, _) in enumerate(self.series)]
                       + [(n + "（区间）", PALETTE[k % len(PALETTE)], "band") for k, (n, _) in enumerate(self.bands)]
                       + [(n, "#c23b4a", "dash") for n, _ in self.hlines])
            for name, color, kind in entries[:14]:
                pen = QPen(QColor(color), 2, Qt.PenStyle.DashLine if kind == "dash" else Qt.PenStyle.SolidLine)
                p.setPen(pen)
                p.drawLine(QPointF(right + 10, y + 7), QPointF(right + 28, y + 7))
                p.setPen(AXIS)
                p.drawText(QRectF(right + 32, y, legend_w - 34, 16), Qt.AlignmentFlag.AlignLeft, name)
                y += 17


class BarChart(QWidget):
    """Horizontal bars (label, value); negative values drawn to the left of zero."""

    def __init__(self, parent=None, *, title="暂无数据"):
        super().__init__(parent)
        self.title, self.values, self.unit = title, [], ""
        self.setMinimumHeight(200)

    def set_values(self, title, values, *, unit=""):
        self.title, self.unit = title, unit
        self.values = [(str(k), float(v)) for k, v in values if _ok(v)][:30]
        self.setToolTip("\n".join(f"{k}: {v:.4g}{unit}" for k, v in self.values))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), BACKGROUND)
        p.setPen(QColor("#234139"))
        p.drawText(QRectF(10, 4, self.width() - 20, 22), Qt.AlignmentFlag.AlignLeft, self.title)
        if not self.values:
            p.drawText(QRectF(0, 30, self.width(), self.height() - 40), Qt.AlignmentFlag.AlignCenter, "暂无结果")
            return
        label_w = min(260, max(80, 7 * max(len(k) for k, _ in self.values) + 10))
        left, right, top = label_w + 12, self.width() - 60, 30
        row_h = max(12, min(26, (self.height() - top - 8) / len(self.values)))
        lo, hi = min(0.0, min(v for _, v in self.values)), max(0.0, max(v for _, v in self.values))
        span = max(hi - lo, 1e-12)
        zero = left + (0 - lo) / span * (right - left)
        for i, (label, value) in enumerate(self.values):
            y = top + i * row_h
            p.setPen(AXIS)
            p.drawText(QRectF(4, y, label_w, row_h), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
            x = left + (value - lo) / span * (right - left)
            color = QColor(PALETTE[0] if value >= 0 else PALETTE[5])
            p.fillRect(QRectF(min(zero, x), y + 2, abs(x - zero), row_h - 4), color)
            p.setPen(AXIS)
            p.drawText(QRectF(max(zero, x) + 4, y, 60, row_h), Qt.AlignmentFlag.AlignVCenter, _fmt(value) + self.unit)
        p.setPen(QPen(AXIS, 1))
        p.drawLine(QPointF(zero, top), QPointF(zero, top + row_h * len(self.values)))


def _ticks(lo, hi, count=6):
    span = hi - lo
    if span <= 0 or not math.isfinite(span):
        return [lo]
    raw = span / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    start = math.ceil(lo / step) * step
    return [start + i * step for i in range(int((hi - start) / step) + 1)]


def _ok(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
