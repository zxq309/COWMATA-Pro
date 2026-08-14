from __future__ import annotations

from final_desktop_window import MainWindow as FinalDesktopWindow
from transactional_project_mixin import TransactionalProjectMixin


class MainWindow(TransactionalProjectMixin, FinalDesktopWindow):
    """Transaction-safe final desktop window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("牛尾环九轴视频标注工作台 · 正式桌面版")
