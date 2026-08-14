from __future__ import annotations

from release_window import MainWindow as ReleaseWindow
from seek_confirmation_mixin import SeekConfirmationMixin
from smooth_playhead_mixin import SmoothPlayheadMixin


class MainWindow(
    SmoothPlayheadMixin,
    SeekConfirmationMixin,
    ReleaseWindow,
):
    """Final audited window with confirmed seeks and smooth shared cursor."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("牛尾环九轴视频标注工作台 · 正式桌面版")
