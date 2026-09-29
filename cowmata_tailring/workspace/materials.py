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
        if not self.effects_enabled:
            # Non-glass look: let the stylesheet's own backgrounds paint the
            # page; a solid canvas fill here would occlude the layout after
            # toggling (the reported beige overlay).
            if self.cache is not None:
                self.cache = None
            return
        if self.cache is None or self.cache.devicePixelRatio() != dpr:
            self.cache = QPixmap(round(self.width() * dpr), round(self.height() * dpr))
            self.cache.setDevicePixelRatio(dpr)
            self.cache.fill(QColor("#f5f7fa"))
            p = QPainter(self.cache)
            gradient = QLinearGradient(0, 0, self.width(), self.height())
            gradient.setColorAt(0, QColor("#eef6ea"))
            gradient.setColorAt(.5, QColor("#f5f7fa"))
            gradient.setColorAt(1, QColor("#eaf5f8"))
            p.fillRect(self.rect(), gradient)
            for x, y, radius, color in ((.15, .1, .7, "#8add66"), (.92, .7, .55, "#35afc8")):
                glow = QRadialGradient(self.width() * x, self.height() * y, self.width() * radius)
                center = QColor(color)
                center.setAlpha(46)
                glow.setColorAt(0, center)
                glow.setColorAt(1, QColor(255, 255, 255, 0))
                p.fillRect(self.rect(), glow)
            p.end()
        if self.cache is not None:
            painter = QPainter(self)
            painter.drawPixmap(0, 0, self.cache)


GLASS_STYLE = """
QWidget {background:transparent;}
QFrame#sourcePanel, QFrame#eventPanel, QFrame#algorithmPanel {background:rgba(255,255,255,232); border:1px solid rgba(221,226,233,220); border-radius:12px;}
QWidget#signalCard {background:#FFFFFF;}
QPushButton, QToolButton {background:rgba(255,255,255,215); border-color:rgba(205,212,221,200);}
QPushButton#primary {background:#8ADD66; color:#153A0C; border-color:#7ACD57;}
QPushButton#primary:disabled {background:#F0F2F5; color:#A1AAB5; border-color:#E6E9EE;}
QPushButton:checked, QToolButton:checked {background:#DDF1F6; color:#0E5F70; border-color:#9BD3E0;}
QListWidget, QTableWidget {background:rgba(255,255,255,225);}
QMenu {background:#FFFFFF;}
QDialog {background:#F5F7FA;}
QMenuBar {background:rgba(251,252,253,235); color:#1C2530; border-bottom:1px solid #E3E7ED;}
QMenuBar::item {background:transparent; color:#1C2530;}
QMenuBar::item:selected, QMenuBar::item:pressed {background:#E9EDF2;}
QStatusBar {background:rgba(245,247,250,235); color:#6B7785;}
QComboBox QAbstractItemView {background:#FFFFFF; color:#1C2530;}
"""
