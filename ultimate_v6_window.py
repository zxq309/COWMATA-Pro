from __future__ import annotations

from dual_anchor_precision_mixin import DualAnchorPrecisionMixin
from ultimate_v4_window import MainWindow as UltimateV4Window


class MainWindow(
    DualAnchorPrecisionMixin,
    UltimateV4Window,
):
    """Precision dual-anchor desktop release with up to 10x playback."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            "牛尾环九轴视频标注工作台 · 正式桌面版 v6"
        )
