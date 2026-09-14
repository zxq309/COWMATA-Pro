"""Live, read-only view of the same committed snapshot used by classification."""
import csv
import io
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
)

from .classification_report import CSV_FIELDS, csv_bytes, read_snapshot, source_key, summary_text


class ReportModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.cells = []

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.cells = list(csv.reader(io.StringIO(csv_bytes(rows).decode('utf-8-sig'))))[1:]
        self.endResetModel()

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.cells)

    def columnCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(CSV_FIELDS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return CSV_FIELDS[section] if orientation == Qt.Orientation.Horizontal else section+1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        value = self.cells[index.row()][index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return value
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 4 and value:
                return Path(value).name
            if index.column() == 5 and value:
                return '/'.join(Path(value).parent.parts[-3:])
            return value
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 1:
            status = self.rows[index.row()].get('status')
            return QColor('#a4382b' if status in {'blocked', 'invalid'} else '#6b746f' if status == 'empty_video' else '#246139')


class ClassificationReportWindow(QDialog):
    def __init__(self, parent, job_provider):
        super().__init__(parent, Qt.WindowType.Window)
        self.job_provider = job_provider
        self.revision = None
        self.snapshot = None
        self.setWindowTitle('实时归类记录')
        self.resize(1100, 660)
        layout = QVBoxLayout(self)
        self.summary = QLabel('等待归类记录')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.updated = QLabel('每秒自动刷新；每个文件一行，显示最新状态')
        layout.addWidget(self.updated)
        self.model = ReportModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, width in enumerate((55, 105, 105, 105, 180, 200, 90, 225)):
            self.table.setColumnWidth(i, width)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details)
        self.table.clicked.connect(self.show_details)
        row = QHBoxLayout()
        row.addWidget(QLabel('Excel 直接打开的是静态快照；实时进度请在本窗口查看。'))
        row.addStretch()
        export = QPushButton('另存当前 CSV…')
        export.clicked.connect(self.export_current)
        row.addWidget(export)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

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
            return
        try:
            snapshot = read_snapshot(job)
        except (OSError, ValueError):
            return
        version = (str(job), snapshot['revision'])
        if version == self.revision:
            return
        selected = self.table.currentIndex().row()
        selected_key = source_key(self.model.rows[selected]['source']) if 0 <= selected < len(self.model.rows) else None
        scroll = self.table.verticalScrollBar().value()
        self.snapshot, self.revision = snapshot, version
        self.model.replace(snapshot['rows'])
        self.summary.setText(summary_text(snapshot['counts']))
        self.updated.setText('每秒自动刷新 · 更新时间：' + snapshot['updated_at'])
        for i, row in enumerate(self.model.rows):
            if source_key(row['source']) == selected_key:
                self.table.selectRow(i)
                self.show_details(self.model.index(i, 0))
                break
        self.table.verticalScrollBar().setValue(scroll)

    def show_details(self, index):
        row = self.model.rows[index.row()]
        self.details.setText(row.get('source', '')+'\n'+row.get('target', '')+'\n'+row.get('message', ''))

    def export_current(self):
        self.refresh()
        if self.snapshot is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, '另存当前归类记录', '归类记录.csv', 'CSV (*.csv)')
        if path:
            try:
                self.refresh()
                Path(path).write_bytes(csv_bytes(self.snapshot['rows']))
            except OSError as exc:
                QMessageBox.warning(self, '无法保存 CSV', str(exc))
