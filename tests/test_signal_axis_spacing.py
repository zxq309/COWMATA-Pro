import re

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.clocks import Anchor, ClockMap, wall_ms
from cowmata_tailring.workspace.signal_panel import ReviewWaveform


@pytest.mark.parametrize("width", [300, 360, 420, 500, 620, 800, 1100])
@pytest.mark.parametrize("font_size", [11, 15, 21])
def test_full_timestamp_tick_rectangles_do_not_overlap(width, font_size):
    app = QApplication.instance() or QApplication([])
    wave = ReviewWaveform()
    wave.resize(width, 220)
    wave.set_clock(ClockMap([Anchor(0, wall_ms("2026-08-03 11:44:00"))]))
    wave._view_t0, wave._view_t1 = 0, 28000
    font = QFont("Segoe UI")
    font.setPixelSize(font_size)
    metrics = QFontMetrics(font)
    labels = []

    class Painter:
        def fontMetrics(self):
            return metrics

        def setPen(self, *_):
            pass

        def drawLine(self, *_):
            pass

        def drawText(self, rectangle, _alignment, text):
            labels.append((QRectF(rectangle), text))

    wave._paint_time_axis(Painter())
    assert labels
    for rectangle, text in labels:
        assert re.fullmatch(r"2026-08-03 11:44:\d{2}", text)
        assert metrics.horizontalAdvance(text) <= rectangle.width()
        assert rectangle.left() >= 0
        if rectangle.width() <= wave.width():
            assert rectangle.right() <= wave.width()
        else:
            # An unusually enlarged font may exceed the minimum widget width;
            # one tick still must not acquire an overlapping second label.
            assert len(labels) == 1
    for (left, _), (right, _) in zip(labels, labels[1:]):
        assert left.right() + 4 <= right.left(), (width, font_size, left, right)
    wave.close()
    assert app is not None
