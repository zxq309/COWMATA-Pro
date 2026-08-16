from __future__ import annotations

from annotation_review import AnnotationReviewMixin
from model_assist import ModelAssistMixin
from ultimate_v7_window import MainWindow as ExistingV7Window


class MainWindow(AnnotationReviewMixin, ModelAssistMixin, ExistingV7Window):
    """Independent copy of v7 with review-first IMU model assistance."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            "牛尾环九轴视频标注工作台 · 模型辅助整合版"
        )
