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
            self.cache.fill(QColor("#edf3f2"))
            if self.effects_enabled:
                p = QPainter(self.cache)
                gradient = QLinearGradient(0, 0, self.width(), self.height())
                gradient.setColorAt(0, QColor("#dcece4"))
                gradient.setColorAt(.5, QColor("#eff2ed"))
                gradient.setColorAt(1, QColor("#e7edf7"))
                p.fillRect(self.rect(), gradient)
                for x, y, radius, color in ((.15, .1, .7, "#a9dbcd"), (.92, .7, .55, "#d1d5f3")):
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
QFrame#sourcePanel, QFrame#eventPanel {background:rgba(255,255,255,224); border:1px solid rgba(255,255,255,245); border-radius:12px;}
QWidget#signalCard {background:white;}
QPushButton, QToolButton {background:rgba(255,255,255,208); border-color:rgba(160,192,181,145);}
QPushButton#primary {background:#087e72; color:white; border-color:#087e72;}
QPushButton:checked {background:#d8ede7; color:#00695e;}
QListWidget, QTableWidget {background:rgba(255,255,255,220);}
QMenu, QDialog {background:#f3f7f5;}
QStatusBar {background:#e4eeeb; color:#385d55;}
QComboBox QAbstractItemView {background:#f4f8f6; color:#223c41;}
"""
