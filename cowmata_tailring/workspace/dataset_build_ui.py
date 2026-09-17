"""Five simple dataset workflows with live, resumable Raw/Label exports."""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QProcess, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
)

from cowmata_tailring.ui.task_window import TaskWindow

from .paired_dataset import TASKS
from .theme import STYLE


class PairModel(QAbstractTableModel):
    COLUMNS = [('status','状态'),('kind','信号'),('behavior','行为'),('source','来源文件'),
               ('raw_target','原始数据'),('label_target','标签数据'),('seconds','耗时/秒'),('message','说明')]
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows=[]
    def replace(self, rows):
        same = len(self.rows) == len(rows) and all(a.get('id', a.get('source')) == b.get('id', b.get('source')) for a,b in zip(self.rows, rows))
        if not same:
            self.beginResetModel()
        self.rows=rows
        if not same:
            self.endResetModel()
        elif rows:
            self.dataChanged.emit(self.index(0,0), self.index(len(rows)-1,len(self.COLUMNS)-1))
    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.rows)
    def columnCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.COLUMNS)
    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role==Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section][1] if orientation==Qt.Orientation.Horizontal else section+1
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        key=self.COLUMNS[index.column()][0]
        value=self.rows[index.row()].get(key,'')
        if role==Qt.ItemDataRole.ToolTipRole:
            return str(value)
        if role==Qt.ItemDataRole.DisplayRole:
            if key=='status':
                return {'pending':'待处理','processing':'处理中','done':'已完成','reused':'已复用','error':'异常'}.get(value,value)
            if key in {'source','raw_target','label_target'}:
                return Path(value).name if value else ''
            return str(value)


class DatasetRecordsWindow(TaskWindow):
    def __init__(self, parent, root):
        super().__init__(parent,Qt.WindowType.Window)
        self.root=Path(root)
        self.stamp=None
        self.setWindowTitle('数据集完整记录 · '+self.root.name)
        self.resize(1400,750)
        layout=QVBoxLayout(self)
        self.summary=QLabel()
        layout.addWidget(self.summary)
        self.model=PairModel(self)
        self.table=QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(3,230)
        self.table.setColumnWidth(4,260)
        self.table.setColumnWidth(5,260)
        layout.addWidget(self.table)
        self.table.doubleClicked.connect(self.review)
        layout.addWidget(QLabel('双击已完成记录，复核对应标签；记录每秒刷新。'))
        self.timer=QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()
    def refresh(self):
        path=self.root/'构建状态.json'
        try:
            stat=path.stat()
            stamp=(stat.st_size,stat.st_mtime_ns)
            if stamp==self.stamp:
                return
            data=json.loads(path.read_text(encoding='utf-8'))
            self.stamp=stamp
            self.model.replace(data['rows'])
            self.summary.setText(summary(data))
        except (OSError,ValueError):
            return
    def review(self,index):
        row=self.model.rows[index.row()]
        path=str(self.root/row['label_relative']) if row.get('label_relative') else row.get('label_target')
        if path and Path(path).is_file():
            from .history_window import HistoryWindow
            window=getattr(self, 'review_window', None)
            if window is None or window.disposed:
                window=HistoryWindow(path, reusable=True)
                self.review_window=window
            else:
                if window.future is not None or not window.confirm_pending():
                    return
                window.path=Path(path)
                window.setWindowTitle('COWMATA Pro™ · 复核与修改 · '+window.path.name)
                window.begin_load(None)
            window.show()
            window.raise_()
    def closeEvent(self,event):
        window=getattr(self, 'review_window', None)
        if window is not None and not window.disposed:
            window.dispose()
            if not window.disposed:
                event.ignore()
                return
        self.timer.stop()
        super().closeEvent(event)


def summary(snapshot):
    c=snapshot['counts']
    return f"配对 {c['total']} · 已完成 {c['done']} · 已复用 {c['reused']} · 处理中 {c['processing']} · 待处理 {c['pending']} · 异常 {c['errors']} · 本次 {snapshot.get('seconds',0):.1f} 秒"


class DatasetBuildWindow(TaskWindow):
    def __init__(self, owner, mode=0):
        super().__init__(owner,Qt.WindowType.Window)
        self.owner=owner
        self.running=False
        self.job=None
        self.output=''
        self.buffer=b''
        self.process=None
        self.settings=QSettings()
        self.setStyleSheet(STYLE)
        self.resize(1100,700)
        outer=QVBoxLayout(self)
        form=QFormLayout()
        self.task=QComboBox()
        for key,value in TASKS.items():
            self.task.addItem(value[0],key)
        form.addRow('构建任务',self.task)
        self.sources=QPlainTextEdit()
        self.sources.setMaximumHeight(75)
        self.sources.setPlaceholderText('选择牧场或类别目录；每行一个来源。')
        row=QHBoxLayout()
        row.addWidget(self.sources,1)
        add=QPushButton('选择来源…')
        add.clicked.connect(self.add_source)
        row.addWidget(add)
        form.addRow('① 标注来源',row)
        self.target=QLineEdit(self.settings.value('dataset370/target','',type=str))
        self.target.setPlaceholderText('选择包含 COWMATA 数据集文件夹的总目录')
        row=QHBoxLayout()
        row.addWidget(self.target,1)
        choose=QPushButton('选择目录…')
        choose.clicked.connect(self.choose_output)
        row.addWidget(choose)
        form.addRow('② 数据集总目录',row)
        outer.addLayout(form)
        self.layout_hint=QLabel()
        self.layout_hint.setWordWrap(True)
        outer.addWidget(self.layout_hint)
        self.status=QLabel('③ 更新完整数据集：补入新增数据，复用已有数据，保留标签修改历史。')
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.model=PairModel(self)
        self.table=QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for i,width in enumerate((80,65,125,210,190,190,80,230)):
            self.table.setColumnWidth(i,width)
        outer.addWidget(self.table,1)
        self.bar=QProgressBar()
        outer.addWidget(self.bar)
        row=QHBoxLayout()
        self.build_button=QPushButton('更新完整数据集')
        self.build_button.setObjectName('primary')
        self.build_button.clicked.connect(self.submit)
        self.cancel=QPushButton('暂停')
        self.cancel.clicked.connect(self.cancel_job)
        self.resume=QPushButton('继续更新')
        self.resume.clicked.connect(self.resume_job)
        self.records=QPushButton('实时记录')
        self.records.clicked.connect(self.open_records)
        self.open_output=QPushButton('打开数据集目录')
        self.open_output.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(self.output)) if self.output else None)
        for button in (self.build_button,self.cancel,self.resume,self.records,self.open_output):
            row.addWidget(button)
        outer.addLayout(row)
        self.timer=QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.task.currentIndexChanged.connect(self.task_changed)
        self.set_task(mode)
        if getattr(owner,'catalog',None):
            self.sources.setPlainText(str(owner.catalog.root))
        self.controls()
    def set_task(self, mode):
        if self.running:
            return
        index=list(TASKS).index(mode) if isinstance(mode,str) else max(0,min(int(mode),len(TASKS)-1))
        if self.task.currentIndex() == index and self.job is not None:
            return
        self.task.setCurrentIndex(index)
        self.task_changed()
    def task_changed(self,*_):
        spec=TASKS[self.task.currentData()]
        self.setWindowTitle('COWMATA Pro™ · '+spec[0])
        self.layout_hint.setText(spec[1]+' / 行为 / Motion 或 PPG / Raw、Label\n文件名：设备-耳标-现场标记_采集日期_采集时间_raw.json / label.json')
        if not self.running:
            self.model.replace([])
            self.output=''
            self.job=None
    def add_source(self):
        path=QFileDialog.getExistingDirectory(self,'选择牧场或类别目录')
        if path:
            self.sources.appendPlainText(path)
    def choose_output(self):
        path=QFileDialog.getExistingDirectory(self,'选择数据集总目录')
        if path:
            self.target.setText(path)
    def submit(self,*_):
        sources=[p.strip().strip('"') for p in self.sources.toPlainText().splitlines() if p.strip()]
        if not sources or not self.target.text().strip():
            self.status.setText('请先选择来源和数据集总目录。')
            return
        if getattr(self.owner,'work',None):
            self.owner.save_current()
            if self.owner.dirty:
                self.status.setText('当前标注尚未保存，请处理保存问题后再构建。')
                return
        request=dict(action='paired_build',sources=sources,target=self.target.text().strip(),task=self.task.currentData(),layout='current')
        self.job=(Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'COWMATA Annotator/dataset-jobs'/uuid.uuid4().hex).resolve()
        self.job.mkdir(parents=True)
        (self.job/'request.json').write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
        self.settings.setValue('dataset370/target',request['target'])
        self.settings.setValue('dataset370/'+request['task']+'/job',str(self.job))
        self.model.replace([])
        self.output=''
        self.start_job()
    def start_job(self):
        if self.running:
            return
        (self.job/'cancel').unlink(missing_ok=True)
        self.running=True
        self.buffer=b''
        self.snapshot_path=None
        self.snapshot_stamp=None
        self.status.setText('正在核对来源并更新完整数据集…')
        self.bar.setRange(0,0)
        self.controls()
        if self.process:
            self.process.deleteLater()
        self.process=QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read_progress)
        self.process.finished.connect(self.job_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.process.start(sys.executable,['-I','-B',str(Path(__file__).with_name('dataset_worker.py')),str(self.job)])
    def read_progress(self):
        self.buffer+=bytes(self.process.readAllStandardOutput())
        while b'\n' in self.buffer:
            line,self.buffer=self.buffer.split(b'\n',1)
            try:
                value=json.loads(line)
            except ValueError:
                continue
            if value.get('event')=='snapshot':
                self.snapshot_path=Path(value['path'])
                self.output=str(self.snapshot_path.parent)
    def refresh(self):
        path=getattr(self,'snapshot_path',None)
        if path:
            try:
                stat=path.stat()
                stamp=(stat.st_size,stat.st_mtime_ns)
                if stamp!=self.snapshot_stamp:
                    data=json.loads(path.read_text(encoding='utf-8'))
                    self.snapshot_stamp=stamp
                    self.model.replace(data['rows'])
                    self.status.setText(summary(data))
                    c=data['counts']
                    self.bar.setRange(0,max(1,c['total']))
                    self.bar.setValue(c['done']+c['reused']+c['errors'])
            except (OSError,ValueError):
                pass
        self.controls()
    def controls(self):
        self.task.setEnabled(not self.running)
        self.sources.setEnabled(not self.running)
        self.target.setEnabled(not self.running)
        self.build_button.setEnabled(not self.running)
        self.cancel.setEnabled(self.running)
        last = self.job or Path(self.settings.value('dataset370/'+self.task.currentData()+'/job','',type=str))
        self.resume.setEnabled(not self.running and (last/'paired-plan.json').is_file())
        self.records.setEnabled(bool(self.output))
        self.open_output.setEnabled(bool(self.output))
    def cancel_job(self):
        if self.running:
            (self.job/'cancel').touch()
            self.status.setText('正在暂停并保存已完成配对…')
    def resume_job(self):
        last = self.job or Path(self.settings.value('dataset370/'+self.task.currentData()+'/job','',type=str))
        if (last/'request.json').is_file():
            request=json.loads((last/'request.json').read_text(encoding='utf-8'))
            self.sources.setPlainText('\n'.join(request['sources']))
            self.target.setText(request['target'])
            self.job=last
            self.start_job()
    def open_records(self):
        if self.output:
            window=DatasetRecordsWindow(self,self.output)
            window.show()
    def job_finished(self,code,*_):
        self.read_progress()
        self.running=False
        self.refresh()
        if code:
            error=self.job/'error.json'
            self.status.setText(json.loads(error.read_text(encoding='utf-8'))['error'] if error.is_file() else '任务未完成，请查看任务记录。')
        self.controls()
    def process_error(self,error):
        if error==QProcess.ProcessError.FailedToStart:
            self.running=False
            self.status.setText(self.process.errorString())
            self.controls()
    def closeEvent(self,event):
        if self.running:
            self.cancel_job()
            self.hide()
            event.ignore()
        else:
            self.timer.stop()
            event.accept()
    def showEvent(self,event):
        self.timer.start()
        super().showEvent(event)
