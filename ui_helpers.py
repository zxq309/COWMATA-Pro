from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QTextEdit,
)


BEIJING = timezone(timedelta(hours=8))


def as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    if is_dataclass(value):
        return asdict(value)
    return dict(vars(value))


def format_relative(ms: float) -> str:
    seconds = max(0.0, float(ms)) / 1000.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours:02d}:{minutes:02d}:{seconds % 60:06.3f}"


def format_wall(epoch_ms: float | None) -> str:
    if not epoch_ms:
        return "—"
    return datetime.fromtimestamp(epoch_ms / 1000.0, BEIJING).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3]


def parse_video_filename(path: str, data_epoch_ms: int | None) -> int | None:
    stem = Path(path).stem
    full = re.search(
        r"(20\d{2})[-_](\d{2})[-_](\d{2})[ T_-]"
        r"(\d{2})[-_:](\d{2})[-_:](\d{2})(?:[._-](\d{1,3}))?",
        stem,
    )
    if full:
        year, month, day, hour, minute, second = map(int, full.groups()[:6])
        milliseconds = int((full.group(7) or "0").ljust(3, "0"))
        value = datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            milliseconds * 1000,
            tzinfo=BEIJING,
        )
        return int(value.timestamp() * 1000)

    clock = re.search(
        r"(?<!\d)(\d{2})[-_:](\d{2})[-_:](\d{2})"
        r"(?:[._-](\d{1,3}))?(?!\d)",
        stem,
    )
    if clock and data_epoch_ms:
        base = datetime.fromtimestamp(data_epoch_ms / 1000.0, BEIJING)
        hour, minute, second = map(int, clock.groups()[:3])
        milliseconds = int((clock.group(4) or "0").ljust(3, "0"))
        value = base.replace(
            hour=hour,
            minute=minute,
            second=second,
            microsecond=milliseconds * 1000,
        )
        return int(value.timestamp() * 1000)
    return None


class LabelDialog(QDialog):
    def __init__(
        self, initial: dict[str, Any] | None = None, parent=None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("标签设置")
        self.name_edit = QLineEdit()
        self.en_edit = QLineEdit()
        self.key_edit = QLineEdit()
        self.color_edit = QLineEdit("#4f8cff")
        self.type_combo = QComboBox()
        self.type_combo.addItem("区间", "interval")
        self.type_combo.addItem("点事件", "point")
        self.category_edit = QLineEdit("事件")
        self.min_duration = QDoubleSpinBox()
        self.min_duration.setRange(0, 86_400)
        self.min_duration.setDecimals(3)
        self.definition_edit = QTextEdit()
        self.definition_edit.setMaximumHeight(100)

        form = QFormLayout(self)
        form.addRow("名称", self.name_edit)
        form.addRow("英文名", self.en_edit)
        form.addRow("快捷键", self.key_edit)
        form.addRow("颜色", self.color_edit)
        form.addRow("类型", self.type_combo)
        form.addRow("类别", self.category_edit)
        form.addRow("最短持续时间/秒", self.min_duration)
        form.addRow("操作性定义", self.definition_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        if initial:
            self.name_edit.setText(str(initial.get("name", "")))
            self.en_edit.setText(str(initial.get("en", "")))
            self.key_edit.setText(str(initial.get("key", "")))
            self.color_edit.setText(str(initial.get("color", "#4f8cff")))
            index = self.type_combo.findData(initial.get("type", "interval"))
            self.type_combo.setCurrentIndex(max(0, index))
            self.category_edit.setText(str(initial.get("cat", "事件")))
            self.min_duration.setValue(float(initial.get("minDur", 0)))
            self.definition_edit.setPlainText(str(initial.get("def", "")))

    def result_value(self) -> dict[str, Any]:
        return {
            "name": self.name_edit.text().strip() or "未命名",
            "en": self.en_edit.text().strip(),
            "key": self.key_edit.text().strip(),
            "color": self.color_edit.text().strip() or "#4f8cff",
            "type": self.type_combo.currentData(),
            "cat": self.category_edit.text().strip() or "事件",
            "minDur": float(self.min_duration.value()),
            "overlap": True,
            "def": self.definition_edit.toPlainText().strip(),
        }

