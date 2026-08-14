from __future__ import annotations

from desktop_release_window import MainWindow as DesktopReleaseWindow
from priming_playback_mixin import PrimingPlaybackMixin


class MainWindow(PrimingPlaybackMixin, DesktopReleaseWindow):
    """Canonical desktop window handed to the user."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("牛尾环九轴视频标注工作台 · 正式桌面版")
