"""Cached frosted-tint background and documented Windows 11 Mica title bar.

Video surfaces and waveform canvases stay opaque. No video-frame blur pass,
desktop capture, whole-window opacity or undocumented Windows API is used.
"""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QWidget


def apply_mica(hwnd, enabled=True):
    if sys.platform != "win32" or sys.getwindowsversion().build < 22621:
        return {"supported": False, "enabled": False}
    try:
        dll = ctypes.WinDLL("dwmapi")
        call = dll.DwmSetWindowAttribute
        call.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        call.restype = ctypes.c_long
        material = ctypes.c_int(2 if enabled else 1)  # MAINWINDOW / NONE
        result = call(ctypes.c_void_p(hwnd), 38, ctypes.byref(material), ctypes.sizeof(material))
        return {"supported": True, "enabled": enabled and result == 0, "hresult": result}
    except (OSError, AttributeError):
        return {"supported": False, "enabled": False}


class FrostedCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.effects_enabled = True
        self.cache = None

    def set_effects(self, enabled):
        self.effects_enabled = bool(enabled)
        self.cache = None
        self.update()

    def resizeEvent(self, event):
        self.cache = None
        super().resizeEvent(event)

    def paintEvent(self, event):
        dpr = self.devicePixelRatioF()
        if self.cache is None or self.cache.devicePixelRatio() != dpr:
            self.cache = QPixmap(round(self.width() * dpr), round(self.height() * dpr))
            self.cache.setDevicePixelRatio(dpr)
            self.cache.fill(QColor("#f3f7f0"))
            if self.effects_enabled:
                p = QPainter(self.cache)
                gradient = QLinearGradient(0, 0, self.width(), self.height())
                gradient.setColorAt(0, QColor("#e5f1d8"))
                gradient.setColorAt(.5, QColor("#f4f8ef"))
                gradient.setColorAt(1, QColor("#e2f1f4"))
                p.fillRect(self.rect(), gradient)
                for x, y, radius, color in ((.15, .1, .7, "#8add66"), (.92, .7, .55, "#35afc8")):
                    glow = QRadialGradient(self.width() * x, self.height() * y, self.width() * radius)
                    center = QColor(color)
                    center.setAlpha(85)
                    glow.setColorAt(0, center)
                    glow.setColorAt(1, QColor(255, 255, 255, 0))
                    p.fillRect(self.rect(), glow)
                p.end()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self.cache)


GLASS_STYLE = """
QWidget {background:transparent;}
QFrame#sourcePanel, QFrame#eventPanel, QFrame#algorithmPanel {background:rgba(255,255,255,235); border:1px solid #d7e4d0; border-radius:12px;}
QWidget#signalCard {background:white;}
QPushButton, QToolButton {background:rgba(255,255,255,208); border-color:rgba(160,192,181,145);}
QPushButton#primary {background:#8add66; color:#20351c; border-color:#69b24e;}
QPushButton#primary:disabled {background:#edf0e9; color:#849080; border-color:#d9dfd2;}
QPushButton:checked {background:#dff3d1; color:#294514;}
QListWidget, QTableWidget {background:rgba(255,255,255,220);}
QMenu, QDialog {background:#f3f7f5;}
QMenuBar {background:#eaf3e4; color:#20332a; border-bottom:1px solid #ccdcbc;}
QMenuBar::item {background:transparent; color:#20332a;}
QMenuBar::item:selected, QMenuBar::item:pressed {background:white; border-color:#92c142;}
QStatusBar {background:#eaf3e4; color:#315225;}
QComboBox QAbstractItemView {background:#f7faf4; color:#20332a;}
"""
