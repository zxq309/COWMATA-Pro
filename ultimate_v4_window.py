from __future__ import annotations

from ffmpeg_process_guard_v2_mixin import FFmpegProcessGuardV2Mixin
from release_hardening_mixin import ReleaseHardeningMixin
from ultimate_v3_window import MainWindow as UltimateV3Window


class MainWindow(
    ReleaseHardeningMixin,
    FFmpegProcessGuardV2Mixin,
    UltimateV3Window,
):
    """Final native desktop window with release hardening."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("牛尾环九轴视频标注工作台 · 正式桌面版")
