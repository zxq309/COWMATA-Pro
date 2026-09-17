"""Independent work windows, with explicit lifetime ownership by the host."""
from __future__ import annotations

import weakref

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog


class TaskWindow(QDialog):
    def __init__(self, owner=None, flags=Qt.WindowType.Window):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint | Qt.WindowType.WindowCloseButtonHint)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        if owner is not None:
            self.setWindowIcon(owner.windowIcon())
            self.setFont(owner.font())
            self.setStyleSheet(owner.styleSheet())
            if not hasattr(owner, '_task_windows'):
                owner._task_windows = {}
            key = id(self)
            owner._task_windows[key] = self
            reference = weakref.ref(owner)
            def released(*_):
                parent = reference()
                if parent is not None:
                    getattr(parent, '_task_windows', {}).pop(key, None)
            self.destroyed.connect(released)
            owner.destroyed.connect(self.deleteLater)

    def show(self):
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        super().show()


def close_task_windows(owner):
    from shiboken6 import isValid
    for window in list(getattr(owner, '_task_windows', {}).values()):
        if isValid(window):
            if not close_task_windows(window) or not window.close():
                return False
    return True
