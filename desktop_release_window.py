from __future__ import annotations

from audited_window import MainWindow as AuditedWindow
from early_seek_mixin import EarlySeekMixin


class MainWindow(EarlySeekMixin, AuditedWindow):
    """Canonical production window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("牛尾环九轴视频标注工作台 · 桌面正式版")
