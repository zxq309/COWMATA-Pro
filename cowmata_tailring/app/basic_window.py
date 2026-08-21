from __future__ import annotations

from cowmata_tailring.app._layer_v4 import MainWindow as UltimateV4Window
from cowmata_tailring.media.mixins.dual_anchor_precision import DualAnchorPrecisionV2Mixin
from cowmata_tailring.media.mixins.ended_replay import EndedReplayMixin
from cowmata_tailring.ui.i18n import t


class MainWindow(
    EndedReplayMixin,
    DualAnchorPrecisionV2Mixin,
    UltimateV4Window,
):
    """Sample-accurate dual-anchor desktop release with up to 10x playback."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            t("COWMATA 尾环视频与九轴标注工具")
        )
