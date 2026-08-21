"""Dialogs and provenance helpers for correcting model-generated annotations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from cowmata_tailring.ui.helpers import format_relative
from cowmata_tailring.ui.i18n import t

BODY_CODES = {"LYING", "STANDING", "WALKING"}
# PredictFull_使用指南.md only marks tail wagging as evidence-limited.
RESEARCH_MODEL_CODES = {"TAIL_WAGGING"}
MODEL_EDIT_CODES = (
    "STANDING",
    "LYING",
    "WALKING",
    "TAIL_RAISED",
    "TAIL_WAGGING",
    "STANDING_UP",
    "LYING_DOWN",
    "URINATION",
    "DEFECATION",
)
MODEL_LABEL_NAMES = {
    "STANDING": "站立",
    "LYING": "躺卧",
    "WALKING": "行走",
    "TAIL_RAISED": "抬尾",
    "TAIL_WAGGING": "甩尾",
    "STANDING_UP": "起立",
    "LYING_DOWN": "卧倒",
    "URINATION": "排尿",
    "DEFECATION": "排便",
}
MODEL_LABEL_LAYERS = {
    **{code: "body_state" for code in ("STANDING", "LYING", "WALKING")},
    **{code: "tail_action" for code in ("TAIL_RAISED", "TAIL_WAGGING")},
    **{code: "posture_transition" for code in ("STANDING_UP", "LYING_DOWN")},
    **{code: "objective_event" for code in ("URINATION", "DEFECATION")},
}


def adjust_prediction_row(
    source: dict[str, Any],
    *,
    code: str,
    label: str,
    layer: str,
    start_ms: float,
    end_ms: float,
    edited_at: str | None = None,
) -> dict[str, Any]:
    """Apply a traceable human correction without losing model provenance."""

    row = dict(source)
    previous = {
        "code": str(row.get("code", "")),
        "start_ms": float(row.get("start_ms", 0.0)),
        "end_ms": float(row.get("end_ms", 0.0)),
    }
    row.setdefault("prediction_original_code", previous["code"])
    row.setdefault("prediction_original_start_ms", previous["start_ms"])
    row.setdefault("prediction_original_end_ms", previous["end_ms"])
    history = [
        dict(item)
        for item in row.get("prediction_adjustment_history", [])
        if isinstance(item, dict)
    ]
    history.append(
        {
            "edited_at": edited_at
            or datetime.now().astimezone().isoformat(timespec="seconds"),
            "previous": previous,
            "updated": {
                "code": str(code),
                "start_ms": float(start_ms),
                "end_ms": float(end_ms),
            },
        }
    )
    row.update(
        {
            "code": str(code),
            "label": str(label),
            "layer": str(layer),
            "start_ms": float(start_ms),
            "end_ms": float(end_ms),
            "duration_s": round((float(end_ms) - float(start_ms)) / 1000.0, 3),
            "human_adjusted": True,
            "prediction_adjustment_history": history,
            "candidate_type": (
                "continuous_state" if str(code) in BODY_CODES else "event_candidate"
            ),
            "model_support": (
                "research" if str(code) in RESEARCH_MODEL_CODES else "formal"
            ),
            "review_priority": (
                "research" if str(code) in RESEARCH_MODEL_CODES else "standard"
            ),
            "review_recommended": True,
            "_recommended": True,
        }
    )
    existing_note = str(row.get("_review_note", "")).strip()
    adjustment_note = "人工已调整标签或边界"
    row["_review_note"] = (
        existing_note + "；" + adjustment_note
        if existing_note and adjustment_note not in existing_note
        else existing_note or adjustment_note
    )
    return row


class PredictionEditDialog(QDialog):
    """Edit one model suggestion while keeping its original values traceable."""

    def __init__(
        self,
        prediction: dict[str, Any],
        label_options: list[tuple[str, str, str]],
        duration_ms: float,
        current_position_callback: Callable[[], float] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.prediction = prediction
        self.label_options = label_options
        self.current_position_callback = current_position_callback
        self.setWindowTitle(t("修改预测标注"))
        self.setMinimumWidth(470)

        layout = QVBoxLayout(self)
        original = QLabel(
            "原始/当前建议："
            f"{prediction.get('label', prediction.get('code', ''))} · "
            f"{format_relative(float(prediction.get('start_ms', 0.0)))} — "
            f"{format_relative(float(prediction.get('end_ms', 0.0)))}"
        )
        original.setWordWrap(True)
        layout.addWidget(original)

        form = QFormLayout()
        self.label_combo = QComboBox()
        current_code = str(prediction.get("code", "")).upper()
        selected_index = 0
        for index, (code, name, layer) in enumerate(label_options):
            self.label_combo.addItem(f"{name}（{code}）", (code, name, layer))
            if code == current_code:
                selected_index = index
        self.label_combo.setCurrentIndex(selected_index)

        maximum_seconds = max(
            0.001,
            float(duration_ms) / 1000.0,
            float(prediction.get("end_ms", 0.0)) / 1000.0,
        )
        self.start_spin = self._time_spin(maximum_seconds)
        self.end_spin = self._time_spin(maximum_seconds)
        self.start_spin.setValue(float(prediction.get("start_ms", 0.0)) / 1000.0)
        self.end_spin.setValue(float(prediction.get("end_ms", 0.0)) / 1000.0)
        form.addRow("标签", self.label_combo)
        form.addRow("开始（相对九轴）", self.start_spin)
        form.addRow("结束（相对九轴）", self.end_spin)
        layout.addLayout(form)

        if current_position_callback is not None:
            position_row = QHBoxLayout()
            start_here = QPushButton("开始 = 当前播放位置")
            end_here = QPushButton("结束 = 当前播放位置")
            start_here.clicked.connect(lambda: self._use_current(self.start_spin))
            end_here.clicked.connect(lambda: self._use_current(self.end_spin))
            position_row.addWidget(start_here)
            position_row.addWidget(end_here)
            layout.addLayout(position_row)

        hint = QLabel(
            "时间单位为秒，可输入到毫秒。修改只改变待复核标注，原始模型类别和边界会保留在追溯字段中。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#58677b;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _time_spin(maximum_seconds: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(3)
        spin.setRange(0.0, maximum_seconds)
        spin.setSingleStep(0.100)
        spin.setSuffix(" s")
        return spin

    def _use_current(self, target: QDoubleSpinBox) -> None:
        if self.current_position_callback is None:
            return
        target.setValue(float(self.current_position_callback()) / 1000.0)

    def values(self) -> tuple[str, str, str, float, float]:
        code, name, layer = self.label_combo.currentData()
        return (
            str(code),
            str(name),
            str(layer),
            self.start_spin.value() * 1000.0,
            self.end_spin.value() * 1000.0,
        )

    def accept(self) -> None:
        _code, _name, _layer, start_ms, end_ms = self.values()
        if end_ms <= start_ms:
            QMessageBox.warning(self, t("时间范围无效"), t("结束时间必须晚于开始时间。"))
            return
        super().accept()
