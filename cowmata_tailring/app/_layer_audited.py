from __future__ import annotations

from cowmata_tailring.app._layer_hardened import MainWindow as ReleaseWindow
from cowmata_tailring.media.mixins.seek_confirmation import SeekConfirmationMixin
from cowmata_tailring.media.mixins.smooth_playhead import SmoothPlayheadMixin
from cowmata_tailring.ui.i18n import t


class MainWindow(
    SmoothPlayheadMixin,
    SeekConfirmationMixin,
    ReleaseWindow,
):
    """Final audited window with confirmed seeks and smooth shared cursor."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(t("牛尾环九轴视频标注工作台 · 正式桌面版"))
