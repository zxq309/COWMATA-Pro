from __future__ import annotations

from cancel_safe_open_mixin import CancelSafeOpenMixin
from canonical_window import MainWindow as CanonicalWindow


class MainWindow(CancelSafeOpenMixin, CanonicalWindow):
    """Final desktop window after cancellation-safety audit."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("牛尾环九轴视频标注工作台 · 正式桌面版")
