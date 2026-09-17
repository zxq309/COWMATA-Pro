from __future__ import annotations

from cowmata_tailring.app._layer_audited import MainWindow as AuditedWindow
from cowmata_tailring.media.mixins.early_seek import EarlySeekMixin
from cowmata_tailring.ui.i18n import t


class MainWindow(EarlySeekMixin, AuditedWindow):
    """Canonical production window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(t("牛尾环九轴视频标注工作台 · 桌面正式版"))
