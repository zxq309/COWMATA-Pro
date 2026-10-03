"""Three offline collaboration tasks, all disk work outside the GUI thread."""
from __future__ import annotations

import os
import threading
import time
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSettings, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
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

LAST_ROOT = 'collaboration/dispatch_root'
TITLES = dict(dispatch='派发原始数据包', returns='生成标注数据包', receive='接收标注数据包', open='打开协作原始数据包', layout='统一牧场录像目录')
# 4.4.5: packages are folders; each task offers candidate target folders and moves / writes the folder there.
TARGET_LABELS = dict(dispatch='派发到', returns='保存到', receive='接收后移到', open='放到')
RECENT_TARGETS = 'collaboration/targets/'
KEEP_IN_PLACE = '不移动（留在原位置）'


def size_text(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


def duration_text(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f'{hours} 小时 {minutes} 分'
    if minutes:
        return f'{minutes} 分 {secs} 秒'
    return f'{secs} 秒'


class CollaborationDialog(QDialog):
    def __init__(self, mode, parent=None, root=''):
        super().__init__(parent)
        self.mode, self.units, self.plans = mode, [], []
        self.result_root = None
        self.future = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='collaboration')
        self.stop = threading.Event()
        self.latest = (0, 0, '')
        self.progress_kind = 'bytes'
        self.samples = deque()
        self.report = None
        self.remember = False
        self.setWindowTitle('COWMATA Annotator · ' + TITLES[mode])
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1060, 780 if mode == 'dispatch' else 560)
        self.setStyleSheet(STYLE)
        self.selected_packages = []
        layout = QVBoxLayout(self)
        hint = QLabel({'dispatch': '按所选目录和各包勾选日期一键派包，仅包含 JSON 与视频。每个包生成一个文件夹（不压缩），'
                                   '直接放到“派发到”选择的位置。已派日期默认标记并跳过；异常记录到派包报告。',
                       'returns': '保存标注后生成标注数据包文件夹（不压缩）：只含标注结果和证据图，保留任务编号及目录结构，'
                                  '直接放到“保存到”选择的位置。',
                       'receive': '选择标注数据包文件夹（可多选；旧版 ZIP 也可）。先完整校验任务、目录、原始数据和标签再接收，'
                                  '接收后把数据包文件夹移到“接收后移到”选择的位置。重复内容跳过；冲突保留并生成报告。',
                       'open': '选择派发的原始数据包文件夹（旧版 ZIP 也可）。软件核验后直接打开工程；'
                               '“放到”选其他位置时先把整个文件夹移过去。',
                       'layout': '将分类下的 Video 移至牧场根目录的录像，更新标注引用并保留迁移记录。请先保存并关闭该牧场工程；原始传感器数据保持原样。'}[mode])
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        root_label = QLabel('任务目录' if mode == 'returns' else '牧场根目录')
        row.addWidget(root_label)
        self.root = QLineEdit(str(root))
        row.addWidget(self.root, 1)
        self.pick = QPushButton('选择目录…')
        self.pick.clicked.connect(self.choose_root)
        row.addWidget(self.pick)
        layout.addLayout(row)
        if mode == 'open':  # opening needs only the package and where to put it
            for widget in (root_label, self.root, self.pick):
                widget.hide()
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
            row.addWidget(QLabel('标注数据包' if mode == 'receive' else '原始数据包'))
            self.package_path = QLineEdit()
            self.package_path.setReadOnly(True)
            self.package_path.setPlaceholderText('选择标注员交回的数据包文件夹（可多选）' if mode == 'receive'
                                                 else '选择派发的原始数据包文件夹（…_原始）')
            row.addWidget(self.package_path, 1)
            self.pick_package = QPushButton('选择数据包文件夹…')
            self.pick_package.clicked.connect(self.choose_package)
            row.addWidget(self.pick_package)
            self.pick_zip = QPushButton('旧版 ZIP…')
            self.pick_zip.setToolTip('4.4.4 及以前生成的 ZIP 数据包仍可' + ('接收' if mode == 'receive' else '解包打开'))
            self.pick_zip.clicked.connect(self.choose_zip)
            row.addWidget(self.pick_zip)
            layout.addLayout(row)
            self.controls += [self.package_path, self.pick_package, self.pick_zip]
        if mode in TARGET_LABELS:
            row = QHBoxLayout()
            row.addWidget(QLabel(TARGET_LABELS[mode]))
            self.target = QComboBox()
            self.target.setEditable(True)
            self.target.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            self.target.setMinimumContentsLength(48)
            self.target.setToolTip('候选位置：可直接选，也可输入或“选择其他位置…”')
            self.target.currentIndexChanged.connect(self.show_target_note)
            row.addWidget(self.target, 1)
            self.pick_target = QPushButton('选择其他位置…')
            self.pick_target.clicked.connect(self.choose_target)
            row.addWidget(self.pick_target)
            layout.addLayout(row)
            self.target_note = QLabel('')
            self.target_note.setWordWrap(True)
            layout.addWidget(self.target_note)
            self.controls += [self.target, self.pick_target]
            self.target_timer = QTimer(self)
            self.target_timer.setSingleShot(True)
            self.target_timer.setInterval(400)
            self.target_timer.timeout.connect(self.refresh_targets)
            self.root.textChanged.connect(lambda *_: self.target_timer.start())
            self.refresh_targets()
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
        application = QCoreApplication.instance()
        if application is not None:
            # Quitting the application cancels a running task instead of waiting for it.
            application.aboutToQuit.connect(self.stop.set)

    def choose_root(self):
        selected = choose_folders(self, '选择任务目录（原始数据包文件夹或其中的牧场目录）' if self.mode == 'returns'
                                  else '选择牧场根目录', self.root.text())
        if selected:
            self.root.setText(selected[0])

    def choose_package(self):
        start = self.selected_packages[0] if self.selected_packages else ''
        if self.mode == 'receive':
            paths = choose_folders(self, '选择标注数据包文件夹（可多选）', start, multiple=True)
        else:
            paths = choose_folders(self, '选择原始数据包文件夹', start)
        if paths:
            self.set_packages(paths)

    def choose_zip(self):
        if self.mode == 'receive':
            paths, _ = QFileDialog.getOpenFileNames(self, '选择旧版标注 ZIP（可多选）', '', 'ZIP (*.zip)')
        else:
            path, _ = QFileDialog.getOpenFileName(self, '选择旧版原始数据 ZIP', '', 'ZIP (*.zip)')
            paths = [path] if path else []
        if paths:
            self.set_packages(paths)

    def set_packages(self, paths):
        self.selected_packages = [str(Path(p)) for p in paths]
        self.package_path.setText('；'.join(self.selected_packages))
        if self.mode == 'open':
            self.refresh_targets(reset=True)

    def recent_targets(self):
        value = QSettings().value(RECENT_TARGETS + self.mode, [])
        if isinstance(value, str):
            value = [value]
        return [str(v) for v in value or [] if v]

    def remember_target(self, path):
        if not path:
            return
        recent = [p for p in self.recent_targets() if os.path.normcase(p) != os.path.normcase(path)]
        QSettings().setValue(RECENT_TARGETS + self.mode, [path, *recent][:8])

    def refresh_targets(self, reset=False):
        """Fill the target box with the candidates of this task: default first, recent, then drives."""
        if not hasattr(self, 'target'):
            return
        typed = '' if reset else self.target.currentText().strip()
        package = self.selected_packages[0] if self.selected_packages else None
        try:
            candidates = packages.destination_candidates(self.mode, self.root.text().strip() or None, package=package,
                                                         recent=self.recent_targets())
        except (OSError, ValueError):
            candidates = []
        self.target.blockSignals(True)
        self.target.clear()
        for path, note in candidates:
            self.target.addItem(path, note)
            self.target.setItemData(self.target.count() - 1, note, Qt.ItemDataRole.ToolTipRole)
        if self.mode == 'receive':
            self.target.addItem(KEEP_IN_PLACE, '接收后数据包文件夹留在原位置')
        index = self.target.findText(typed) if typed else 0
        if index >= 0:
            self.target.setCurrentIndex(index)
        else:
            self.target.setEditText(typed)
        self.target.blockSignals(False)
        self.show_target_note()

    def show_target_note(self, *_):
        if not hasattr(self, 'target'):
            return
        index = self.target.currentIndex()
        note = self.target.itemData(index) if index >= 0 and self.target.itemText(index) == self.target.currentText() else ''
        what = {'dispatch': '原始数据包文件夹', 'returns': '标注数据包文件夹', 'receive': '接收完的标注数据包文件夹',
                'open': '原始数据包文件夹'}[self.mode]
        if self.mode == 'open' and note and '不移动' in note:
            self.target_note.setText('就在原位置打开，不移动文件夹。')
        elif self.target.currentText().strip() == KEEP_IN_PLACE:
            self.target_note.setText('接收后数据包文件夹留在原位置，不移动。')
        elif self.target.currentText().strip():
            self.target_note.setText(what + '将放到：' + self.target.currentText().strip() + ('　（' + note + '）' if note else ''))
        else:
            self.target_note.setText('请选择或输入目标位置。')

    def choose_target(self):
        selected = choose_folders(self, '选择目标位置', self.target.currentText().strip())
        if selected:
            self.target.setEditText(selected[0])
            self.show_target_note()

    def target_path(self):
        """The chosen target folder ('' = leave in place); relative entries are refused."""
        text = self.target.currentText().strip() if hasattr(self, 'target') else ''
        if not text or text == KEEP_IN_PLACE:
            return ''
        if not Path(text).is_absolute():
            raise ValueError('请选择完整的目标位置（含盘符），例如 E:\\2_标注器\\派包')
        return str(Path(text))

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
            return {category: packages.inventory(root, category, cancelled=self.stop.is_set, progress=self.on_progress)
                    for category in sorted(categories)}
        def loaded(value):
            self.planner.load_inventory(value)
            self.status.setText('扫描完成。请在每个包中直接勾选单日或多日；已派日期默认标记并跳过。')
            if self.remember:
                QSettings().setValue(LAST_ROOT, root)
        self.run(operation, loaded, 'count')

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
                confirm.setInformativeText('确认后会先生成新的数据包文件夹；新包校验成功后，才清理这些日期的旧派包'
                                           '（还没人打开的旧文件夹删除，已有人标注的旧文件夹保留）。')
                cleanup = QCheckBox('确认清理旧派包（新包失败时保留旧包）', confirm)
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
                    self.status.setText('请勾选清理旧派包，或点击“否”取消本次再次派发。')
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
            lines.append('合计 ' + size_text(sum(plan['estimated_bytes'] for plan in plans)) + '；每个包一个文件夹，按原样复制，不压缩。')
            try:
                lines.append('派发到：' + (self.target_path() or '（未选择）'))
            except ValueError as exc:
                lines.append(str(exc))
            lines.append('完全按你在各包中勾选的日期派发，同日整组保留；不会自动均分或改动选择。')
            lines.append('按牧场原目录树保存所选日期的 JSON、视频及必要清单，不更改来源文件。')
            notes = list(dict.fromkeys(w for plan in plans for w in plan.get('readiness', {}).get('warnings', [])))
            lines.extend(notes[:40])
            if len(notes) > 40:
                lines.append('……另有 ' + str(len(notes) - 40) + ' 条提示，派包完成后写入派包报告')
            self.log.setPlainText('\n'.join(lines))
            if dispatch_after:
                self.execute()
        self.run(lambda: packages.plan_dispatch_groups(root, groups, purpose=purpose, cancelled=self.stop.is_set), loaded)

    def execute(self):
        root = self.root.text().strip()
        if not root and self.mode != 'open':
            self.status.setText('请选择任务目录' if self.mode == 'returns' else '请选择牧场根目录')
            return
        try:
            target = self.target_path()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        if self.mode in {'dispatch', 'returns'} and not target:
            self.status.setText('请选择或输入目标位置（' + TARGET_LABELS[self.mode] + '）')
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
            report = self.report = {}
            def completed(paths):
                if paths:
                    self.remember_target(target)
                records = report.get('packages', [])
                sent = {key: r['package_id'] for r in records if not r['error'] for key in r['units']}
                for units in self.planner.inventory_by_category.values():
                    for unit in units:
                        if unit['key'] in sent:
                            unit.setdefault('dispatches', []).append(sent[unit['key']])
                self.planner.refresh()
                self.plans = []
                failed = sum(bool(r['error']) for r in records)
                self.log.setPlainText('\n'.join(self.report_lines(report)))
                self.status.setText('已生成 ' + str(len(paths)) + ' 个包，' + str(failed) + ' 个包未生成（原因见上方）。' if failed
                                    else '原始数据包文件夹已生成到 ' + target + '。已派日期标记已更新。')
            self.run(lambda: packages.dispatch(root, plans, destination=target, report=report, **options), completed)
        elif self.mode == 'returns':
            def made(path):
                self.remember_target(target)
                self.log.setPlainText('标注数据包文件夹：' + str(path) + '\n把这个文件夹交回派包人，在“接收标注数据包”里选择它即可。')
                self.status.setText('标注数据包已生成到 ' + target)
            self.run(lambda: packages.make_return(root, destination=target, **options), made)
        elif self.mode == 'receive':
            paths = list(self.selected_packages)
            if not paths:
                self.status.setText('请选择标注数据包文件夹。')
                return
            def receive():
                result = []
                for path in paths:
                    packages.check(self.stop.is_set)
                    try:
                        result.append((path, packages.receive_return(root, path, archive_to=target or None, **options)))
                    except (OSError, ValueError) as exc:
                        result.append((path, {'error': str(exc)}))
                return result
            def completed(results):
                lines = []
                for path, report in results:
                    lines.append(Path(path).name)
                    if 'error' in report:
                        lines.append('拒绝接收（数据包未移动）：' + report['error'])
                    else:
                        lines.append(f'已接收 {report["imported"]}，重复 {report["unchanged"]}，冲突 {len(report["conflicts"])}')
                        ignored = report.get('ignored_extra') or []
                        if ignored:
                            lines.append('已忽略清单之外的文件 ' + str(len(ignored)) + ' 个（未导入）')
                            for item in ignored[:20]:
                                state = '与清单内文件相同' if item.get('identical') else '与清单内文件不同，请与标注人确认'
                                lines.append('  ' + item.get('path', '') + '：' + state)
                            if len(ignored) > 20:
                                lines.append('  ……另有 ' + str(len(ignored) - 20) + ' 个未显示')
                        lines.extend(report['conflicts'])
                        if report.get('archive'):
                            lines.append('冲突报告：' + report['archive'])
                        if report.get('moved_to'):
                            lines.append('数据包已移到：' + report['moved_to'])
                        if report.get('not_moved'):
                            lines.append('数据包未移动：' + report['not_moved'])
                if target and any('error' not in report for _, report in results):
                    self.remember_target(target)
                self.selected_packages = [p for p, report in results if 'error' in report]
                self.package_path.setText('；'.join(self.selected_packages))
                self.log.setPlainText('\n'.join(lines))
            self.run(receive, completed)
        else:
            paths = list(self.selected_packages)
            if not paths:
                self.status.setText('请选择派发的原始数据包文件夹。')
                return
            if not target and Path(paths[0]).suffix.lower() == '.zip':
                self.status.setText('旧版 ZIP 需要选择解包位置（放到）。')
                return
            def loaded(value):
                if target and os.path.normcase(target) != os.path.normcase(str(Path(paths[0]).parent)):
                    self.remember_target(target)
                self.result_root = value
                self.accept()
            self.run(lambda: packages.open_raw_package(paths[0], target or None, **options), loaded)

    def on_progress(self, done, total, text):
        self.latest = (done, total, text)

    def progress_text(self, done, total, text):
        if self.progress_kind == 'count':
            return '正在扫描：' + text + ' · ' + str(done) + '/' + str(total)
        now = time.monotonic()
        if not self.samples or self.samples[-1][1] != done:
            self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[0][0] > 30:
            self.samples.popleft()
        parts = [size_text(done) + ' / ' + size_text(total)]
        started, first = self.samples[0]
        if now - started >= 2 and done > first:
            rate = (done - first) / (now - started)
            parts += [size_text(rate) + '/s', '剩余约 ' + duration_text((total - done) / rate)]
        if text:
            parts.append(text)
        return ' · '.join(parts)

    def report_lines(self, report):
        lines = []
        for record in report.get('packages', []):
            head = '包 ' + str(record['part']) + '：'
            if record['error']:
                lines.append(head + '未生成 · ' + record['error'])
                continue
            lines.append(head + '已生成 · ' + record['output'])
            if record['skipped']:
                lines.append('  跳过 ' + str(len(record['skipped'])) + ' 个文件（正在写入、已移走或读取失败），其余照常打包')
            replaced = record.get('replaced') or {}
            for old in replaced.get('removed', []):
                lines.append('  已清理旧派包：' + old)
            for old in replaced.get('kept', []):
                lines.append('  旧派包已有人打开或标注，保留：' + old)
        if report.get('reclaimed_bytes'):
            lines.append('已清理上次中断留下的临时文件 ' + size_text(report['reclaimed_bytes']))
        saved = self.save_report(report)
        if saved:
            lines.append('派包报告：' + str(saved))
        return lines

    def save_report(self, report):
        """Keep every dispatch result, including skipped files and per-package errors."""
        from datetime import datetime
        from uuid import uuid4

        from .farm_layout import collaboration_home
        from .storage import atomic_json
        try:
            root = packages.farm_root(self.root.text().strip())
            path = collaboration_home(root) / '派包报告' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8] + '.json')
            atomic_json(path, dict(root=str(root), **report), backup=False)
        except (OSError, ValueError):
            return None
        return path

    def run(self, operation, callback, kind='bytes'):
        if self.future:
            return
        self.stop.clear()
        self.latest = (0, 0, '')
        self.progress_kind = kind
        self.samples.clear()
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
            self.status.setText(self.progress_text(done, total, text))
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
        from .farm_layout import collaboration_home
        try:
            root = packages.farm_root(self.root.text().strip())
            report = collaboration_home(root) / '派包报告' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8] + '.json')
            atomic_json(report, dict(error_type=type(exc).__name__, message=str(exc), root=str(root),
                selected_packages=[[dict(category=u['category'], day=u['day'], owner=u['owner'])
                                    for u in group] for group in self.planner.groups()],
                packages=(self.report or {}).get('packages', [])), backup=False)
            self.log.appendPlainText('异常报告：' + str(report))
        except (OSError, ValueError) as report_error:
            self.log.appendPlainText('异常报告保存失败：' + str(report_error))

    def cancel_or_close(self):
        if self.future:
            self.stop.set()
            self.status.setText('正在取消；已完成的独立分包仍会保留。')
        else:
            self.reject()

    def confirm_cancel(self):
        """Esc or the window close button must not silently stop hours of packaging."""
        if self.progress_kind == 'bytes' and not self.stop.is_set():
            answer = QMessageBox.question(self, '取消任务', '任务仍在进行。取消后，已完成的包保留，未完成的包不会生成。确定取消？')
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.cancel_or_close()

    def reject(self):
        if self.future:
            self.confirm_cancel()
            return
        self.pool.shutdown(wait=False)
        super().reject()

    def closeEvent(self, event):
        if self.future:
            self.confirm_cancel()
            event.ignore()
        else:
            self.pool.shutdown(wait=False)
            super().closeEvent(event)


def open_dialog(window, mode):
    catalog = getattr(window, 'catalog', None)
    if catalog and mode == 'returns':
        window.save_current()
        if window.dirty:
            return
    root = str(shared_farm(catalog.root) or catalog.root) if catalog else ''
    if mode == 'dispatch':
        open_dispatch(window, root)
        return
    if mode == 'receive' and catalog:
        # Receiving requires exclusive access. Never close or discard unsaved work implicitly.
        window.tell('接收前请先保存并关闭相关标注工程；其他牧场可以继续打开。')
    dialog = CollaborationDialog(mode, window, root)
    try:
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
    finally:
        # A closed modal dialog is not reused: stop its poll timer and free it instead of
        # leaving a hidden child of the main window behind after every use.
        dialog.timer.stop()
        dialog.pool.shutdown(wait=False)
        dialog.deleteLater()


def open_dispatch(window, root):
    """Non-modal (4.4.2): annotation and other work continue while packages are written."""
    current = getattr(window, '_dispatch_dialog', None)
    if current is not None:
        current.showNormal()
        current.raise_()
        current.activateWindow()
        return current
    from cowmata_tailring.edge_download.paths import site_farm

    root = root or QSettings().value(LAST_ROOT, '', str) or str(site_farm() or '')
    dialog = CollaborationDialog('dispatch', window, root)
    dialog.remember = True
    window._dispatch_dialog = dialog
    # A weak reference: this slot must never be what keeps the parent window alive,
    # or deleting the dialog would destroy its parent mid-destruction.
    owner = weakref.ref(window)

    def finished(*_):
        parent = owner()
        if parent is not None and getattr(parent, '_dispatch_dialog', None) is dialog:
            parent._dispatch_dialog = None
        dialog.deleteLater()
    dialog.finished.connect(finished)
    dialog.show()
    if dialog.root.text().strip():
        QTimer.singleShot(0, dialog.scan_inventory)
    return dialog

