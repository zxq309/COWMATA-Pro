"""Choose a farm, immediate category, modality and one acquisition day."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout

from .theme import STYLE


def child_directories(root):
    return sorted(
        (
            p
            for p in Path(root).iterdir()
            if p.is_dir()
            and not p.name.startswith(".")
            and not p.is_symlink()
            and not getattr(p, "is_junction", lambda: False)()
        ),
        key=lambda p: p.name,
    )


def category_choices(farm):
    return [
        p for p in child_directories(farm) if p.name not in {"Motion", "PPG", "Temp", "Video", "录像", "科牧特_协作标注", "标注工程"}
    ]


def stage_choices(category):
    return (
        [p for p in child_directories(category) if p.name in {"孕早期", "孕中期", "孕晚期"}]
        if Path(category).name == "怀孕"
        else []
    )


def date_choices(category, modality):
    from datetime import date

    # Dates are a property of the category, not of one preferred sensor.
    # Use the selected modality first, then union the other sensor folders so
    # a late-arriving Motion/PPG/Temp stream cannot make the picker look empty.
    selected = modality if modality in {"Motion", "PPG", "Temp"} else None
    roots = ([Path(category) / selected] if selected else [])
    roots.extend(Path(category) / name for name in ("Motion", "PPG", "Temp") if name != selected)
    result = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in child_directories(root):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name):
                continue
            try:
                date.fromisoformat(path.name)
            except ValueError:
                continue
            result.add(path.name)
    return sorted(result)


def modality_options():
    """Stable project-opening choices shared by administrator and operator."""
    return [
        ("全部数据（九轴 / PPG / 温度）", "all"),
        ("九轴数据 Motion", "Motion"),
        ("脉诊数据 PPG", "PPG"),
        ("温度数据 Temp", "Temp"),
    ]


class ProjectPicker(QDialog):
    def __init__(self, farm, parent=None):
        super().__init__(parent)
        self.farm = Path(farm).resolve(strict=True)
        self.setWindowTitle("打开单日工程 · 确认牧场")
        self.resize(720, 360)
        self.setStyleSheet(STYLE)
        outer = QVBoxLayout(self)
        title = QLabel("当前牧场：" + self.farm.name + "\n请确认这是本批数据所属牧场。")
        title.setObjectName("sectionTitle")
        title.setWordWrap(True)
        outer.addWidget(title)
        path = QLabel(str(self.farm))
        path.setWordWrap(True)
        outer.addWidget(path)
        form = QFormLayout()
        self.category = QComboBox()
        self.stage = QComboBox()
        self.modality = QComboBox()
        self.day = QComboBox()
        for label, value in modality_options():
            self.modality.addItem(label, value)
        for folder in category_choices(self.farm):
            self.category.addItem(folder.name, str(folder))
        for title, control in (
            ("数据类别", self.category),
            ("孕期阶段", self.stage),
            ("原始数据类型", self.modality),
            ("采集日期（单选）", self.day),
        ):
            form.addRow(title, control)
        outer.addLayout(form)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        outer.addWidget(self.hint)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Open).setText("确认牧场并打开该日")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        self.category.currentIndexChanged.connect(self.change_category)
        self.stage.currentIndexChanged.connect(self.change_dates)
        self.modality.currentIndexChanged.connect(self.change_dates)
        self.change_category()

    def category_root(self):
        return self.stage.currentData() or self.category.currentData()

    def change_category(self):
        self.stage.blockSignals(True)
        self.stage.clear()
        selected = self.category.currentData()
        stages = stage_choices(selected) if selected else []
        for path in stages:
            self.stage.addItem(path.name, str(path))
        self.stage.setEnabled(bool(stages))
        self.stage.blockSignals(False)
        self.change_dates()

    def change_dates(self):
        self.day.clear()
        root = self.category_root()
        dates = date_choices(root, self.modality.currentData()) if root else []
        self.day.addItems(dates)
        selected = self.modality.currentData()
        ppg = selected == "PPG"
        temp = selected == "Temp"
        self.buttons.button(QDialogButtonBox.StandardButton.Open).setEnabled(
            bool(dates)
        )
        self.hint.setText(
            "按设备采样配置显示 PPG 波形；标签与原始 PPG 成对保存，可用于训练及识别。"
            if ppg
            else "仅加载所选日期温度和对应录像；前日跨午夜录像按覆盖时间补充。其他日期不扫描。"
            if temp
            else "默认加载所选日期九轴、PPG、温度和对应录像；前日跨午夜录像按覆盖时间补充。"
            if dates
            else "所选类别没有规范日期目录。请核对牧场根目录，或先在数据整理中归类。"
        )

    def selection(self):
        return dict(
            root=self.category_root(),
            farm=str(self.farm),
            day=self.day.currentText(),
            modality=self.modality.currentData(),
        )
