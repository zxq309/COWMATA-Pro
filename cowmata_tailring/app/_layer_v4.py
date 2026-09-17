from __future__ import annotations

from cowmata_tailring.app._layer_v3 import MainWindow as UltimateV3Window
from cowmata_tailring.app.mixins.release_hardening import ReleaseHardeningMixin
from cowmata_tailring.media.mixins.ffmpeg_process_guard_v2 import FFmpegProcessGuardV2Mixin
from cowmata_tailring.ui.i18n import t


class MainWindow(
    ReleaseHardeningMixin,
    FFmpegProcessGuardV2Mixin,
    UltimateV3Window,
):
    """Final native desktop window with release hardening."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(t("牛尾环九轴视频标注工作台 · 正式桌面版"))
