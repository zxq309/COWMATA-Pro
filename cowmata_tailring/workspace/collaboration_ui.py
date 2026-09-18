"""Three offline collaboration tasks, all disk work outside the GUI thread."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from cowmata_tailring.edge_download.core import CHINA

from . import collaboration_packages as packages
from .farm_layout import CATEGORY_PATHS, shared_farm
from .native_folders import choose_folders
from .theme import STYLE

TITLES = dict(dispatch='派发原始数据包', returns='生成标注数据包', receive='接收标注数据包', open='打开协作原始数据包', layout='统一牧场录像目录')


def size_text(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


class CollaborationDialog(QDialog):
    def __init__(self, mode, parent=None, root=''):
        super().__init__(parent)
        self.mode, self.units, self.plans = mode, [], []
        self.result_root = None
        self.future = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='collaboration')
        self.stop = threading.Event()
        self.latest = (0, 0, '')
        self.setWindowTitle('COWMATA Pro · ' + TITLES[mode])
        self.resize(1060, 740 if mode == 'dispatch' else 480)
        self.setStyleSheet(STYLE)
        layout = QVBoxLayout(self)
        hint = QLabel({'dispatch': '先更新 CSV 并完成下载、归类，再派包。Motion、PPG、Temp 齐全且录像覆盖采集时段才可派发；容量仅提示，不设上限。',
                       'returns': '保存标注后生成回传 ZIP。只包含标注结果和证据图，保留任务编号及目录结构。',
                       'receive': '先完整校验任务、目录、原始数据和标签，再接收。重复内容跳过；冲突保留并生成报告。',
                       'open': '选择原始数据 ZIP，软件核验并解包后直接打开工程。无需手动整理文件夹。',
                       'layout': '将分类下的 Video 移至牧场根目录的录像，更新标注引用并保留迁移记录。请先保存并关闭该牧场工程；原始传感器数据保持原样。'}[mode])
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        row.addWidget(QLabel('解包位置' if mode == 'open' else '牧场根目录'))
        self.root = QLineEdit(str(root))
        row.addWidget(self.root, 1)
        self.pick = QPushButton('选择目录…')
        self.pick.clicked.connect(self.choose_root)
        row.addWidget(self.pick)
        layout.addLayout(row)
        self.controls = [self.root, self.pick]
        if mode == 'dispatch':
            controls = QHBoxLayout()
            self.category = QComboBox()
            self.category.addItems(list(CATEGORY_PATHS))
            controls.addWidget(QLabel('健康类别'))
            controls.addWidget(self.category)
            self.purpose = QComboBox()
            self.purpose.addItem('标注任务（跳过已完成）', 'annotation')
            self.purpose.addItem('复核任务（带已有标签）', 'review')
            controls.addWidget(self.purpose)
            self.scan = QPushButton('扫描资料与派发状态')
            self.scan.clicked.connect(self.scan_inventory)
            controls.addWidget(self.scan)
            controls.addStretch()
            controls.addWidget(QLabel('分成'))
            self.count = QSpinBox()
            self.count.setRange(1, 999)
            self.count.setValue(3)
            controls.addWidget(self.count)
            controls.addWidget(QLabel('个包'))
            layout.addLayout(controls)
            filters = QHBoxLayout()
            self.dates = QListWidget()
            self.dates.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
            self.dates.setMaximumHeight(100)
            self.views = QListWidget()
            self.views.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
            self.views.setMaximumHeight(100)
            filters.addWidget(QLabel('日期（可多选）'))
            filters.addWidget(self.dates, 1)
            filters.addWidget(QLabel('录像视角（可多选）'))
            filters.addWidget(self.views, 1)
            layout.addLayout(filters)
            self.table = QTableWidget(0, 7)
            self.table.setHorizontalHeaderLabels(['派发', '日期', '设备 / 牛号', '数据类型', '记录数', '标注状态', '派发情况'])
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            self.table.verticalHeader().setDefaultSectionSize(34)
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.setWordWrap(False)
            layout.addWidget(self.table, 1)
            self.dates.itemSelectionChanged.connect(self.filter_dates)
            self.table.itemChanged.connect(self.invalidate)
            self.category.currentIndexChanged.connect(self.clear_inventory)
            self.purpose.currentIndexChanged.connect(self.clear_inventory)
            self.count.valueChanged.connect(self.invalidate)
            self.views.itemSelectionChanged.connect(self.invalidate)
            self.root.textChanged.connect(self.clear_inventory)
            self.controls += [self.category, self.purpose, self.scan, self.count, self.dates, self.views, self.table]
        if mode in {'receive', 'open'}:
            row = QHBoxLayout()
            self.zip_path = QLineEdit()
            self.zip_path.setReadOnly(True)
            row.addWidget(self.zip_path, 1)
            self.pick_zip = QPushButton('选择 ZIP…')
            self.pick_zip.clicked.connect(self.choose_zip)
            row.addWidget(self.pick_zip)
            layout.addLayout(row)
            self.controls += [self.zip_path, self.pick_zip]
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(170)
        layout.addWidget(self.log)
        self.status = QLabel('等待选择资料')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.preview = QPushButton('预览均分方案')
        self.preview.setVisible(mode == 'dispatch')
        self.preview.clicked.connect(self.preview_plan)
        buttons.addWidget(self.preview)
        self.start = QPushButton(TITLES[mode])
        self.start.clicked.connect(self.execute)
        buttons.addWidget(self.start)
        self.cancel = QPushButton('关闭')
        self.cancel.clicked.connect(self.cancel_or_close)
        buttons.addWidget(self.cancel)
        layout.addLayout(buttons)
        self.controls += [self.preview, self.start]
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)

    def choose_root(self):
        selected = choose_folders(self, '选择牧场根目录' if self.mode != 'open' else '选择解包位置', self.root.text())
        if selected:
            self.root.setText(selected[0])

    def choose_zip(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '选择标注 ZIP（可多选）', '', 'ZIP (*.zip)') if self.mode == 'receive' else ([], '')
        if self.mode == 'open':
            path, _ = QFileDialog.getOpenFileName(self, '选择原始数据 ZIP', '', 'ZIP (*.zip)')
            paths = [path] if path else []
        if paths:
            self.selected_zips = paths
            self.zip_path.setText('；'.join(paths))

    def invalidate(self, *_):
        self.plans = []
        if self.mode == 'dispatch' and not self.future:
            selected = sum(not self.table.isRowHidden(i) and self.table.item(i, 0) is not None and
                           self.table.item(i, 0).checkState() == Qt.CheckState.Checked for i in range(self.table.rowCount()))
            self.count.setMaximum(max(1, selected))

    def clear_inventory(self, *_):
        if self.future:
            return
        self.units, self.plans = [], []
        self.table.setRowCount(0)
        self.dates.clear()
        self.views.clear()

    def filter_dates(self):
        selected = {i.text() for i in self.dates.selectedItems()}
        for row, unit in enumerate(self.units):
            self.table.setRowHidden(row, unit['day'] not in selected)
        self.invalidate()

    def scan_inventory(self):
        root, category = self.root.text(), self.category.currentText()
        def operation():
            units = packages.inventory(root, category, cancelled=self.stop.is_set)
            views = sorted({p.name for d in (Path(root) / '录像').iterdir() if d.is_dir()
                            for p in d.iterdir() if p.is_dir() and p.name.startswith('视角')})
            return units, views
        def loaded(value):
            self.units, views = value
            self.table.blockSignals(True)
            self.table.setRowCount(len(self.units))
            for row, unit in enumerate(self.units):
                annotation_state = {'done': '已完成标注', 'partial': '部分标注 / 草稿', 'new': '未标注'}[unit['annotation_status']]
                dispatched = ('新增资料待补派' if unit['dispatch_status'] == 'supplement' else
                              '已派资料有变更，待核对' if unit['dispatch_status'] == 'changed' else
                              f'已派发 {len(unit["dispatches"])} 次' if unit['dispatches'] else '未派发')
                missing = {'Motion', 'PPG', 'Temp'} - set(unit['modalities'])
                values = ['', unit['day'], unit['owner'], ' / '.join(unit['modalities']), str(len(unit['paths'])), annotation_state,
                           '缺少 ' + '/'.join(sorted(missing)) if missing else dispatched]
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setToolTip(text)
                    if column == 0:
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        eligible = unit['annotation_status'] != 'done' if self.purpose.currentData() == 'annotation' else bool(unit['annotated_records'])
                        eligible = eligible and not missing
                        item.setCheckState(Qt.CheckState.Checked if eligible and not unit['dispatches'] else Qt.CheckState.Unchecked)
                        if not eligible:
                            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                    self.table.setItem(row, column, item)
            self.table.blockSignals(False)
            self.dates.blockSignals(True)
            self.dates.clear()
            for day in sorted({u['day'] for u in self.units}):
                item = QListWidgetItem(day, self.dates)
                item.setSelected(day < datetime.now(CHINA).strftime('%Y-%m-%d'))
            self.dates.blockSignals(False)
            self.views.clear()
            for view in views:
                item = QListWidgetItem(view, self.views)
                item.setSelected(True)
            self.filter_dates()
            self.status.setText(f'共 {len(self.units)} 个设备日期条目；默认选择历史日期，已派发和缺项条目不重复勾选。')
        self.run(operation, loaded)

    def preview_plan(self):
        self.plans = []
        units = [u for i, u in enumerate(self.units) if not self.table.isRowHidden(i)
                 and self.table.item(i, 0).checkState() == Qt.CheckState.Checked]
        root, count = self.root.text(), self.count.value()
        views = [item.text() for item in self.views.selectedItems()]
        purpose = self.purpose.currentData()
        def loaded(plans):
            self.plans = plans
            lines = []
            for plan in plans:
                lines.append(f'第 {plan["part"]} 包：{len(plan["units"])} 个设备日期 · {len(plan["dates"])} 天 · '
                             f'{len(plan["sensor_paths"])} 份记录 · 预计 {size_text(plan["estimated_bytes"])}')
                lines.append('  已核验三类 JSON、台账身份和所选录像时段覆盖。')
            lines.append('容量仅作提示。录像不重复压缩；同一包内共享录像只保存一份。')
            self.log.setPlainText('\n'.join(lines))
        self.run(lambda: packages.plan_dispatch(root, units, count=count, views=views, purpose=purpose, cancelled=self.stop.is_set), loaded)

    def execute(self):
        root = self.root.text().strip()
        if not root:
            self.status.setText('请选择牧场根目录')
            return
        options = dict(cancelled=self.stop.is_set, progress=self.on_progress)
        if self.mode == 'layout':
            from .farm_migration import migrate
            def completed(report):
                self.log.setPlainText(str(report))
            self.run(lambda: migrate(root), completed)
        elif self.mode == 'dispatch':
            if not self.plans:
                self.status.setText('请先预览均分方案，核对日期、视角和容量。')
                return
            plans = self.plans
            def completed(paths):
                self.log.appendPlainText('\n'.join(str(p) for p in paths))
                self.plans = []
                self.status.setText('原始数据包已完成。重新扫描可查看最新派发状态。')
            self.run(lambda: packages.dispatch(root, plans, **options), completed)
        elif self.mode == 'returns':
            self.run(lambda: packages.make_return(root, **options), lambda p: self.log.setPlainText(str(p)))
        elif self.mode == 'receive':
            paths = getattr(self, 'selected_zips', [])
            if not paths:
                self.status.setText('请选择标注 ZIP。')
                return
            def receive():
                result = []
                for path in paths:
                    packages.check(self.stop.is_set)
                    try:
                        result.append((path, packages.receive_return(root, path, **options)))
                    except (OSError, ValueError) as exc:
                        result.append((path, {'error': str(exc)}))
                return result
            def completed(results):
                lines = []
                for path, report in results:
                    lines.append(Path(path).name)
                    if 'error' in report:
                        lines.append('拒绝接收：' + report['error'])
                    else:
                        lines.append(f'已接收 {report["imported"]}，重复 {report["unchanged"]}，冲突 {len(report["conflicts"])}')
                        lines.extend(report['conflicts'])
                        if report.get('archive'):
                            lines.append('冲突报告：' + report['archive'])
                self.log.setPlainText('\n'.join(lines))
            self.run(receive, completed)
        else:
            paths = getattr(self, 'selected_zips', [])
            if not paths:
                self.status.setText('请选择派发的原始 ZIP。')
                return
            def loaded(value):
                self.result_root = value
                self.accept()
            self.run(lambda: packages.open_raw_package(paths[0], root, **options), loaded)

    def on_progress(self, done, total, text):
        self.latest = (done, total, text)

    def run(self, operation, callback):
        if self.future:
            return
        self.stop.clear()
        self.latest = (0, 0, '')
        self.callback = callback
        for widget in self.controls:
            widget.setEnabled(False)
        self.cancel.setText('取消任务')
        self.status.setText('后台核验中…')
        self.progress.setRange(0, 0)
        self.future = self.pool.submit(operation)

    def poll(self):
        if not self.future:
            return
        done, total, text = self.latest
        if total:
            self.progress.setRange(0, 1000)
            self.progress.setValue(round(done / total * 1000))
            self.status.setText(f'{size_text(done)} / {size_text(total)} · {text}')
        if self.future.done():
            future, self.future = self.future, None
            for widget in self.controls:
                widget.setEnabled(True)
            self.cancel.setText('关闭')
            self.progress.setRange(0, 100)
            try:
                value = future.result()
                self.progress.setValue(100)
                self.status.setText('已完成')
                self.callback(value)
            except Exception as exc:
                self.progress.setValue(0)
                self.status.setText(str(exc))
                self.log.appendPlainText(str(exc))

    def cancel_or_close(self):
        if self.future:
            self.stop.set()
            self.status.setText('正在取消；已完成的独立分包仍会保留。')
        else:
            self.reject()

    def reject(self):
        if self.future:
            self.cancel_or_close()
            return
        self.pool.shutdown(wait=False)
        super().reject()

    def closeEvent(self, event):
        if self.future:
            self.cancel_or_close()
            event.ignore()
        else:
            self.pool.shutdown(wait=False)
            event.accept()


def open_dialog(window, mode):
    catalog = getattr(window, 'catalog', None)
    if catalog and mode == 'returns':
        window.save_current()
        if window.dirty:
            return
    root = str(shared_farm(catalog.root) or catalog.root) if catalog else ''
    if mode == 'receive' and catalog:
        # Receiving requires exclusive access. Never close or discard unsaved work implicitly.
        window.tell('接收前请先保存并关闭相关标注工程；其他牧场可以继续打开。')
    dialog = CollaborationDialog(mode, window, root)
    if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_root:
        from .project_picker import ProjectPicker
        from .storage import read_json
        assignment = read_json(dialog.result_root / packages.ASSIGNMENT)
        categories, days = assignment['categories'], assignment['dates']
        if len(categories) == len(days) == 1:
            window.open_project(dialog.result_root / categories[0], day=days[0])
        else:
            picker = ProjectPicker(dialog.result_root, window)
            if picker.exec() == QDialog.DialogCode.Accepted:
                choice = picker.selection()
                window.open_project(choice['root'], day=choice['day'])
    dialog.pool.shutdown(wait=False)
