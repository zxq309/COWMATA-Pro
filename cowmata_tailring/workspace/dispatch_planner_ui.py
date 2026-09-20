"""Independent package sheets; one checkable row per date, all camera views."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cowmata_tailring.edge_download.core import CHINA

from .farm_layout import CATEGORY_PATHS


def eligible(unit, purpose, allow_repackage=False):
    if purpose == 'review':
        return bool(unit.get('annotated_records'))
    return unit['annotation_status'] != 'done' and (allow_repackage or not unit['dispatches'])


class PackageSheet(QWidget):
    def __init__(self, planner):
        super().__init__(planner)
        self.planner = planner
        self.selected_days = set()
        self.by_day = {}
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        controls.addWidget(QLabel('本包健康类别'))
        self.category = QComboBox()
        self.category.addItems(list(CATEGORY_PATHS))
        controls.addWidget(self.category)
        controls.addStretch()
        self.clear = QPushButton('清空本包日期')
        self.clear.clicked.connect(self.clear_dates)
        controls.addWidget(self.clear)
        layout.addLayout(controls)
        self.dates_summary = QLineEdit()
        self.dates_summary.setReadOnly(True)
        self.dates_summary.setAccessibleName('本包已选日期')
        layout.addWidget(self.dates_summary)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['选择', '日期', '可派 / 全部设备', '可派记录数', '标注与派发状态', '本包安排'])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setWordWrap(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setMinimumSectionSize(80)
        for col in range(6):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents if col < 4 else QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet('QTableView::indicator {width:20px; height:20px;}')
        self.table.itemChanged.connect(self.changed)
        self.table.cellClicked.connect(self.toggle_row)
        layout.addWidget(self.table, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.category.currentTextChanged.connect(self.category_changed)

    def category_changed(self):
        self.selected_days.clear()
        self.planner.refresh()

    def clear_dates(self):
        self.selected_days.clear()
        self.planner.refresh()

    def available(self, day):
        return [u for u in self.by_day.get(day, []) if eligible(u, self.planner.purpose, self.planner.allow_repackage.isChecked())]

    def selected_units(self):
        return [u for day in sorted(self.selected_days) for u in self.available(day)]

    def render(self, used):
        self.by_day = defaultdict(list)
        for u in self.planner.inventory_by_category.get(self.category.currentText(), []):
            self.by_day[u['day']].append(u)
        self.selected_days &= {day for day in self.by_day if self.available(day)}
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(self.by_day))
            for row, day in enumerate(sorted(self.by_day)):
                units, ready = self.by_day[day], self.available(day)
                occupied = used.get((self.category.currentText(), day))
                selected = day in self.selected_days
                done = sum(u['annotation_status'] == 'done' for u in units)
                sent = sum(bool(u['dispatches']) for u in units)
                incomplete = sum(bool({'Motion','PPG','Temp'} - set(u['modalities'])) for u in units)
                state = f'已完成 {done} · 已派 {sent} · 缺项 {incomplete}'
                placement = '已选入本包' if selected else f'已选入包 {occupied}' if occupied else '可选择' if ready else '暂无可派资料'
                values = ['已选' if selected else '未选', day, f'{len(ready)} / {len(units)}',
                          str(sum(len(u['paths']) for u in ready)), state, placement]
                detail = '\n'.join(f'{u["owner"]} · ' + ('本次可派' if u in ready else '本次跳过') for u in units)
                for col, text in enumerate(values):
                    item = self.table.item(row, col)
                    if item is None:
                        item = QTableWidgetItem()
                        self.table.setItem(row, col, item)
                    item.setText(text)
                    item.setToolTip(detail if col in (2, 3, 4) else text)
                    item.setBackground(QColor('#dff3d1') if selected else QColor('#ffffff'))
                    if col == 0:
                        item.setData(Qt.ItemDataRole.UserRole, day)
                        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
                        if ready and (not occupied or selected):
                            flags |= Qt.ItemFlag.ItemIsEnabled
                        item.setFlags(flags)
                        item.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
            days = sorted(self.selected_days)
            text = f'已选 {len(days)} 天：' + ('、'.join(days) if days else '请勾选下方日期（支持单日或多日）')
            self.dates_summary.setText(text)
            self.dates_summary.setCursorPosition(0)
            self.dates_summary.setToolTip(text)
            self.summary.setText(f'本包：{len(days)} 天 · {len(self.selected_units())} 个设备日；每个日期仅一行，同日可派设备完整纳入。'
                                 if self.by_day else '本类别尚无扫描资料，请点击“扫描资料与派发状态”。')
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)

    def changed(self, item):
        if item.column() != 0:
            return
        day = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked and item.flags() & Qt.ItemFlag.ItemIsEnabled:
            self.selected_days.add(day)
        else:
            self.selected_days.discard(day)
        self.planner.refresh()

    def toggle_row(self, row, col):
        item = self.table.item(row, 0)
        if col and item.flags() & Qt.ItemFlag.ItemIsEnabled:
            item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked)


class DispatchPlanner(QWidget):
    changed = Signal()

    def __init__(self, parent=None, count=3):
        super().__init__(parent)
        self.inventory_by_category = {}
        self.purpose = 'annotation'
        self.panes = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel('所选日期下的 JSON 与录像按原目录树派发；不改名、不移动原始文件。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.allow_repackage = QCheckBox('允许再次派发已派日期（预览后再次确认）')
        self.allow_repackage.setToolTip('默认关闭；打开后已派日期仍会显示“已派”标记，并在预览时弹窗确认。')
        self.allow_repackage.stateChanged.connect(lambda *_: self.refresh())
        layout.addWidget(self.allow_repackage)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.set_count(count)

    def set_count(self, count):
        while len(self.panes) < count:
            pane = PackageSheet(self)
            self.panes.append(pane)
            self.tabs.addTab(pane, f'包 {len(self.panes)}')
        while len(self.panes) > count:
            pane = self.panes.pop()
            self.tabs.removeTab(len(self.panes))
            pane.deleteLater()
        self.refresh()

    def load_inventory(self, categories):
        self.inventory_by_category.update(categories)
        self.refresh()

    def clear_inventory(self):
        self.inventory_by_category.clear()
        for pane in self.panes:
            pane.selected_days.clear()
        self.refresh()

    def set_purpose(self, purpose):
        self.purpose = purpose
        for pane in self.panes:
            pane.selected_days.clear()
        self.refresh()

    def refresh(self):
        # Drop selections that no longer exist before calculating cross-package ownership.
        for pane in self.panes:
            rows = self.inventory_by_category.get(pane.category.currentText(), [])
            pane.selected_days &= {u['day'] for u in rows if eligible(u, self.purpose, self.allow_repackage.isChecked())}
        used = {(p.category.currentText(), d): i for i, p in enumerate(self.panes, 1) for d in p.selected_days}
        for i, pane in enumerate(self.panes, 1):
            pane.render(used)
            self.tabs.setTabText(i-1, f'包 {i} · {pane.category.currentText()} · 已选 {len(pane.selected_days)} 天')
        self.changed.emit()

    def groups(self):
        return [pane.selected_units() for pane in self.panes]

    def balance(self):
        today = datetime.now(CHINA).strftime('%Y-%m-%d')
        for category in {p.category.currentText() for p in self.panes}:
            panes = [p for p in self.panes if p.category.currentText() == category]
            chosen = set().union(*(p.selected_days for p in panes))
            by_day = defaultdict(list)
            for u in self.inventory_by_category.get(category, []):
                if eligible(u, self.purpose) and (u['day'] in chosen if chosen else u['day'] < today):
                    by_day[u['day']].append(u)
            for pane in panes:
                pane.selected_days.clear()
            # Keep whole device-days together, then minimize the largest
            # workload dimensions in a deterministic order.  A day with a
            # long PPG/Motion record must not be treated as equal to a tiny
            # day merely because both contain one device.
            weights = [[0, 0, 0] for _ in panes]
            for day, units in sorted(by_day.items(), key=lambda p: (-len(p[1]), -sum(len(u['paths']) for u in p[1]), p[0])):
                day_weight = (len(units), sum(len(u['paths']) for u in units), sum(int(u.get('bytes', 0) or 0) for u in units))
                index = min(range(len(panes)), key=lambda i: (*weights[i], i))
                panes[index].selected_days.add(day)
                for pos, value in enumerate(day_weight):
                    weights[index][pos] += value
        self.refresh()
