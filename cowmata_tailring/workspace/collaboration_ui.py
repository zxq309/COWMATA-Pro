"""Three offline collaboration tasks, all disk work outside the GUI thread."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from . import collaboration_packages as packages
from .dispatch_planner_ui import DispatchPlanner
from .catalog import VIDEO_SUFFIXES
from .farm_layout import shared_farm
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
        hint = QLabel({'dispatch': '按所选目录和各包勾选日期一键派包，仅包含 JSON 与视频。已派日期默认标记并跳过；异常记录到派包报告。',
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
            self.purpose = QComboBox()
            self.purpose.addItem('标注任务（跳过已完成）', 'annotation')
            self.purpose.addItem('复核任务（原始 JSON 与视频）', 'review')
            controls.addWidget(self.purpose)
            self.scan = QPushButton('扫描资料与派发状态')
            self.scan.clicked.connect(self.scan_inventory)
            controls.addWidget(self.scan)
            controls.addStretch()
            controls.addWidget(QLabel('派发'))
            self.count = QSpinBox()
            self.count.setRange(1, 99)
            self.count.setValue(3)
            controls.addWidget(self.count)
            controls.addWidget(QLabel('个包（每包独立选类别、日期）'))
            layout.addLayout(controls)
            self.planner = DispatchPlanner(self, self.count.value())
            layout.addWidget(self.planner, 1)
            self.planner.changed.connect(self.invalidate)
            self.count.valueChanged.connect(self.planner.set_count)
            self.purpose.currentIndexChanged.connect(lambda: self.planner.set_purpose(self.purpose.currentData()))
            self.root.textChanged.connect(self.clear_inventory)
            self.controls += [self.purpose, self.scan, self.count, self.planner]
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
        self.preview = QPushButton('预览派包方案')
        self.preview.setVisible(mode == 'dispatch')
        self.preview.clicked.connect(lambda: self.preview_plan())
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
        if hasattr(self, 'log'):
            self.log.clear()

    def clear_inventory(self, *_):
        if self.future:
            return
        self.units, self.plans = [], []
        self.planner.clear_inventory()

    def scan_inventory(self):
        root = self.root.text().strip()
        if not root:
            self.status.setText('请选择牧场根目录或其中一个健康类别目录')
            return
        # Accept a category folder selected from the standard Windows picker,
        # then make the normalized farm root visible before scanning starts.
        try:
            # Resolve both a farm root and a selected category folder through
            # the same strict layout helper used by package validation.  This
            # avoids an empty planner when the Windows picker returns
            # ``...\\产犊`` instead of the directory carrying the farm marker.
            normalized = packages.farm_root(root)
            root = str(normalized)
            self.root.setText(root)
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            self.log.appendPlainText(str(exc))
            return
        categories = {pane.category.currentText() for pane in self.planner.panes}
        self.status.setText('正在扫描牧场资料与派发状态，请稍候…')
        self.log.appendPlainText('扫描根目录：' + root)
        def operation():
            return {category: packages.inventory(root, category, cancelled=self.stop.is_set) for category in sorted(categories)}
        def loaded(value):
            self.planner.load_inventory(value)
            self.status.setText('扫描完成。请在每个包中直接勾选单日或多日；已派日期默认标记并跳过。')
        self.run(operation, loaded)

    def preview_plan(self, *, dispatch_after=False):
        self.plans = []
        groups = self.planner.groups()
        if any(not group for group in groups):
            empty = '、'.join(str(i) for i, group in enumerate(groups, 1) if not group)
            self.status.setText('包 ' + empty + ' 未选择可派日期，请选择日期或减少包数。')
            return
        root, purpose = self.root.text(), self.purpose.currentData()
        replace_previous = False
        if self.planner.allow_repackage.isChecked():
            repeated = [u for group in groups for u in group if u.get('dispatches')]
            if repeated:
                from PySide6.QtWidgets import QMessageBox
                confirm = QMessageBox(self)
                confirm.setWindowTitle('再次派发确认')
                confirm.setText(f'本次选择包含 {len(repeated)} 个已派设备日。')
                confirm.setInformativeText('确认后会先生成新 ZIP；新 ZIP 校验成功后，才清理这些日期的旧派包。')
                cleanup = QCheckBox('确认清理旧派包 ZIP（新包失败时保留旧包）', confirm)
                cleanup.setChecked(True)
                confirm.setCheckBox(cleanup)
                confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                confirm.setDefaultButton(QMessageBox.StandardButton.No)
                answer = confirm.exec()
                if answer != QMessageBox.StandardButton.Yes:
                    repeated_keys = {u['key'] for u in repeated}
                    for pane in self.planner.panes:
                        pane.selected_days.difference_update({u['day'] for u in pane.selected_units() if u['key'] in repeated_keys})
                    self.planner.refresh()
                    self.status.setText('已取消再次派发，原有派发记录保持不变。')
                    return
                if not cleanup.isChecked():
                    self.status.setText('请勾选清理旧派包 ZIP，或点击“否”取消本次再次派发。')
                    return
                replace_previous = True
        def loaded(plans):
            self.plans = plans
            if replace_previous:
                for plan in plans:
                    plan['replace_previous'] = True
            lines = []
            for plan in plans:
                views = {e['path'].split('/')[-2] for e in plan['entries'] if Path(e['path']).suffix.lower() in VIDEO_SUFFIXES}
                lines.append(f'包 {plan["part"]} · {"、".join(plan["categories"])} · 日期：{"、".join(plan["dates"])}')
                lines.append(f'  {len(plan["units"])} 个设备日 · {len(plan["sensor_paths"])} 份记录 · 全量 {len(views)} 个视角 · 预计 {size_text(plan["estimated_bytes"])}')
            lines.append('完全按你在各包中勾选的日期派发，同日整组保留；不会自动均分或改动选择。')
            lines.append('按牧场原目录树保存所选日期的 JSON、视频及必要清单，不更改来源文件。')
            for plan in plans:
                lines.extend(plan.get('readiness', {}).get('warnings', []))
            self.log.setPlainText('\n'.join(lines))
            if dispatch_after:
                self.execute()
        self.run(lambda: packages.plan_dispatch_groups(root, groups, purpose=purpose, cancelled=self.stop.is_set), loaded)

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
                self.preview_plan(dispatch_after=True)
                return
            plans = self.plans
            def completed(paths):
                self.log.appendPlainText('\n'.join(str(p) for p in paths))
                self.plans = []
                self.status.setText('原始数据包已完成。已派日期标记已更新。')
                completed_log = self.log.toPlainText()
                sent = {u['key']: p['package_id'] for p in plans for u in p['units']}
                for units in self.planner.inventory_by_category.values():
                    for unit in units:
                        if unit['key'] in sent:
                            unit.setdefault('dispatches', []).append(sent[unit['key']])
                self.planner.refresh()
                self.log.setPlainText(completed_log)
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
                if self.mode == 'dispatch':
                    self.record_dispatch_error(exc)

    def record_dispatch_error(self, exc):
        from datetime import datetime
        from uuid import uuid4
        from .storage import atomic_json
        from .farm_layout import COLLABORATION
        try:
            root = packages.farm_root(self.root.text().strip())
            report = root / COLLABORATION / '派包报告' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8] + '.json')
            atomic_json(report, dict(error_type=type(exc).__name__, message=str(exc), root=str(root),
                selected_packages=[[dict(category=u['category'], day=u['day'], owner=u['owner'])
                                    for u in group] for group in self.planner.groups()]), backup=False)
            self.log.appendPlainText('异常报告：' + str(report))
        except (OSError, ValueError) as report_error:
            self.log.appendPlainText('异常报告保存失败：' + str(report_error))

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
