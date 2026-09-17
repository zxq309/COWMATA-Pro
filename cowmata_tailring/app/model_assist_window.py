from __future__ import annotations

from cowmata_tailring.annotation.review import AnnotationReviewMixin
from cowmata_tailring.app.basic_window import MainWindow as ExistingV7Window
from cowmata_tailring.model_assist.assist import ModelAssistMixin
from cowmata_tailring.ui.i18n import t


class MainWindow(AnnotationReviewMixin, ModelAssistMixin, ExistingV7Window):
    """Independent copy of v7 with review-first IMU model assistance."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            t("COWMATA 尾环视频与九轴标注工具 · 模型辅助")
        )
