"""Live, read-only view of the same committed snapshot used by classification."""
import csv
import io
import re
from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
)

from cowmata_tailring.ui.task_window import TaskWindow

from .classification_report import (
    CSV_FIELDS,
    counts,
    csv_bytes,
    read_snapshot,
    source_key,
    summary_text,
)


class ReportModel(QAbstractTableModel):
    def __init__(self, parent=None, detailed=False):
        super().__init__(parent)
        self.detailed = detailed
        self.fields = CSV_FIELDS + (["核验(秒)", "识别(秒)", "传输(秒)", "开始时间", "结束时间"] if detailed else [])
        self.rows = []
        self.cells = []

    def replace(self, rows):
        same = len(self.rows) == len(rows) and all(source_key(a.get('source', '')) == source_key(b.get('source', '')) for a,b in zip(self.rows, rows))
        if not same:
            self.beginResetModel()
        self.rows = rows
        self.cells = list(csv.reader(io.StringIO(csv_bytes(rows).decode('utf-8-sig'))))[1:]
        if self.detailed:
            for cell, row in zip(self.cells, rows):
                cell.extend(str(row.get(k, '')) for k in ('health_seconds', 'recognition_seconds', 'transfer_seconds', 'started_at', 'finished_at'))
        if not same:
            self.endResetModel()
        elif rows:
            self.dataChanged.emit(self.index(0,0), self.index(len(rows)-1,len(self.fields)-1))

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.cells)

    def columnCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.fields)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return self.fields[section] if orientation == Qt.Orientation.Horizontal else section+1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        value = self.cells[index.row()][index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return value
        if role == Qt.ItemDataRole.DisplayRole:
            if not self.detailed and index.column() == 4 and value:
                return Path(value).name
            if not self.detailed and index.column() == 5 and value:
                return '/'.join(Path(value).parent.parts[-3:])
            return value
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 1:
            status = self.rows[index.row()].get('status')
            return QColor('#a4382b' if status in {'blocked', 'invalid'} else '#6b746f' if status == 'empty_video' else '#246139')


class ClassificationReportWindow(TaskWindow):
    def __init__(self, parent, job_provider, *, detailed=False):
        super().__init__(parent, Qt.WindowType.Window)
        self.job_provider = job_provider
        self.revision = None
        self.snapshot = None
        self.setWindowTitle('归类记录 · 按视角实时更新')
        self.resize(1440 if detailed else 1100, 740 if detailed else 660)
        layout = QVBoxLayout(self)
        self.summary = QLabel('等待归类记录')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.updated = QLabel('每秒自动刷新；每个文件一行，显示最新状态')
        layout.addWidget(self.updated)
        self.detailed = detailed
        self.sheets = {}
        self.sheet_tabs = QTabWidget()
        self.sheet_tabs.setDocumentMode(True)
        layout.addWidget(self.sheet_tabs, 1)
        self.sheet_summary = QLabel()
        self.sheet_summary.setWordWrap(True)
        layout.addWidget(self.sheet_summary)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details)
        self.sheet_tabs.currentChanged.connect(self.select_sheet)
        self.add_sheet('未分配')
        row = QHBoxLayout()
        row.addWidget(QLabel('Excel 直接打开的是静态快照；实时进度请在本窗口查看。'))
        row.addStretch()
        self.export_scope = QComboBox()
        self.export_scope.addItems(['当前视角', '全部记录'])
        row.addWidget(self.export_scope)
        export = QPushButton('另存为 CSV…')
        export.clicked.connect(self.export_current)
        row.addWidget(export)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def add_sheet(self, key):
        model = ReportModel(self, self.detailed)
        table = QTableView()
        table.setModel(model)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, width in enumerate((50, 95, 105, 100, 180, 210, 90, 210)):
            table.setColumnWidth(i, width)
        table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        table.clicked.connect(self.show_details)
        self.sheets[key] = (table, model)
        self.sheet_tabs.addTab(table, key)
        self.select_sheet()

    def current_sheet(self):
        current = self.sheet_tabs.currentWidget()
        return next((key for key, (table, _) in self.sheets.items() if table is current), None)

    def select_sheet(self, *_):
        key = self.current_sheet()
        if key is None:
            return
        self.table, self.model = self.sheets[key]
        self.sheet_summary.setText(key + ' · ' + summary_text(counts(self.model.rows)))
        self.show_details(self.table.currentIndex())

    def clear_sheets(self):
        self.sheet_tabs.blockSignals(True)
        for table, model in self.sheets.values():
            table.deleteLater()
            model.deleteLater()
        self.sheets.clear()
        self.sheet_tabs.clear()
        self.sheet_tabs.blockSignals(False)
        self.details.clear()

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self):
        job = self.job_provider()
        if not job:
            self.snapshot = self.revision = None
            self._file_stamp = None
            if list(self.sheets) != ['未分配'] or self.model.rows:
                self.clear_sheets()
                self.add_sheet('未分配')
            self.summary.setText('等待归类记录')
            self.details.clear()
            self.updated.setText('每秒自动刷新；每个文件一行，显示最新状态')
            return
        try:
            stat = (Path(job) / 'report-state.json').stat()
            stamp = (str(job), stat.st_size, stat.st_mtime_ns)
            if self.snapshot is not None and getattr(self, '_file_stamp', None) == stamp:
                return
            snapshot = read_snapshot(job)
            self._file_stamp = stamp
        except (OSError, ValueError):
            return
        version = (str(job), snapshot['revision'])
        if version == self.revision:
            return
        selected_sheet = self.current_sheet()
        if self.revision and self.revision[0] != str(job):
            self.clear_sheets()
            selected_sheet = None
        self.snapshot, self.revision = snapshot, version
        grouped = defaultdict(list)
        for row in snapshot['rows']:
            key = str(row.get('owner') or row.get('device_id') or row.get('device') or '未分配')
            grouped[key].append(row)
        if not grouped:
            grouped['未分配'] = []
        for key in list(self.sheets):
            if key not in grouped:
                table, model = self.sheets.pop(key)
                self.sheet_tabs.removeTab(self.sheet_tabs.indexOf(table))
                table.deleteLater()
                model.deleteLater()
        ordered = sorted(grouped, key=lambda key: [(0, int(p)) if p.isdigit() else (1, p.casefold()) for p in re.split(r'(\d+)', key)])
        for key in ordered:
            if key not in self.sheets:
                self.add_sheet(key)
            table, model = self.sheets[key]
            selected = table.currentIndex().row()
            selected_key = source_key(model.rows[selected]['source']) if 0 <= selected < len(model.rows) else None
            scroll = table.verticalScrollBar().value()
            model.replace(grouped[key])
            for i, row in enumerate(model.rows):
                if source_key(row['source']) == selected_key:
                    table.selectRow(i)
                    break
            table.verticalScrollBar().setValue(scroll)
            self.sheet_tabs.setTabText(self.sheet_tabs.indexOf(table), f'{key}（{len(model.rows)}）')
        self.sheets = {key: self.sheets[key] for key in ordered}
        for i, key in enumerate(ordered):
            self.sheet_tabs.tabBar().moveTab(self.sheet_tabs.indexOf(self.sheets[key][0]), i)
        if selected_sheet in self.sheets:
            self.sheet_tabs.setCurrentWidget(self.sheets[selected_sheet][0])
        self.select_sheet()
        self.summary.setText(summary_text(snapshot['counts']) + f" · 本次运行 {snapshot.get('elapsed_seconds', 0):.1f} 秒")
        self.updated.setText('每秒自动刷新 · 更新时间：' + snapshot['updated_at'])

    def show_details(self, index):
        if not index.isValid() or not 0 <= index.row() < len(self.model.rows):
            self.details.clear()
            return
        row = self.model.rows[index.row()]
        timing = ' · '.join(f'{name} {row[key]} 秒' for key, name in (
            ('health_seconds', '核验'), ('recognition_seconds', '识别'), ('transfer_seconds', '传输')) if row.get(key) is not None)
        self.details.setText(row.get('source', '')+'\n'+row.get('target', '')+'\n'+row.get('message', '')+ ('\n'+timing if timing else ''))

    def export_payload(self, all_records=False):
        self.refresh()
        rows = self.snapshot['rows'] if all_records and self.snapshot else self.model.rows
        return csv_bytes(rows)

    def export_current(self):
        self.refresh()
        if self.snapshot is None:
            return
        all_records = self.export_scope.currentIndex() == 1
        payload = self.export_payload(all_records)
        label = '全部记录' if all_records else str(self.current_sheet())
        label = re.sub(r'[<>:"/\\|?*]', '_', label)
        path, _ = QFileDialog.getSaveFileName(self, '另存当前归类记录', '归类记录-'+label+'.csv', 'CSV (*.csv)')
        if path:
            try:
                Path(path).write_bytes(payload)
            except OSError as exc:
                QMessageBox.warning(self, '无法保存 CSV', str(exc))
