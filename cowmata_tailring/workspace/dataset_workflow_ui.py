"""Reusable old-label import and mother-dataset export window."""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .data_category import CATEGORIES, category_root
from .theme import STYLE


class DatasetWorkflowWindow(QDialog):
    def __init__(self, owner, mode=0):
        super().__init__(owner,Qt.WindowType.Window)
        self.owner, self.process, self.job, self.plan_path = owner, None, None, None
        self.running = False
        self.output = ''
        self.setWindowTitle('COWMATA · 旧标签与算法数据集')
        self.resize(1000,770)
        self.setStyleSheet(STYLE)
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        legacy = QWidget()
        form = QFormLayout(legacy)
        self.label_sources = QPlainTextEdit()
        self.raw_sources = QPlainTextEdit()
        for control in (self.label_sources,self.raw_sources):
            control.setMaximumHeight(105)
            control.setPlaceholderText('每行一个文件或目录；也可使用右侧按钮添加目录')
        self._paths_row(form,'旧标签 CSV / BORIS',self.label_sources)
        self._paths_row(form,'原始九轴来源',self.raw_sources)
        self.target = QLineEdit()
        self._directory_row(form,'资源或牧场目录',self.target)
        self.farm = QLineEdit('扬大_高邮牧场')
        form.addRow('牧场名称',self.farm)
        self.category = QComboBox()
        self.category.addItem('请选择本批类别','')
        for code in ('calving','estrus','pregnancy_early','pregnancy_mid','pregnancy_late','disease','healthy'):
            text = ('怀孕 / ' if code.startswith('pregnancy_') else '')+CATEGORIES[code]
            self.category.addItem(text,code)
        form.addRow('类别与孕期',self.category)
        hint = QLabel('先预览，再迁移。原始标签、来源与时间字段保留；牛号冲突待核，不进入训练。\n'
                      '产犊统一归在产犊目录；人工干预和数据缺失保留为元数据。未知时间不会补造。')
        hint.setWordWrap(True)
        form.addRow(hint)
        actions = QHBoxLayout()
        self.preview = QPushButton('审查并预览旧标签')
        self.execute = QPushButton('执行迁移并生成新版标注')
        self.preview.clicked.connect(self.preview_migration)
        self.execute.clicked.connect(self.execute_migration)
        self.execute.setEnabled(False)
        actions.addWidget(self.preview)
        actions.addWidget(self.execute)
        form.addRow(actions)
        self.tabs.addTab(legacy,'① 旧标签迁移')
        datasets = QWidget()
        dataset_form = QFormLayout(datasets)
        self.dataset_sources = QPlainTextEdit()
        self.dataset_sources.setMaximumHeight(140)
        self.dataset_sources.setPlaceholderText('每行一个已保存标注工程、导出标注目录或单文件标注')
        self._paths_row(dataset_form,'新版标注来源',self.dataset_sources)
        self.dataset_target = QLineEdit()
        self.dataset_target.setPlaceholderText('新的批次目录；已有数据集不会被覆盖')
        self._directory_row(dataset_form,'新数据集目录',self.dataset_target)
        self.historical = QLineEdit()
        self._directory_row(dataset_form,'历史算法数据集（可选）',self.historical)
        self.allow_proxy = QCheckBox('保留旧代理负样本用于训练；不作为验证、测试真值')
        dataset_form.addRow(self.allow_proxy)
        self.dataset_help = QLabel('使用当前已保存标注，优先于旧导出快照。原始九轴保持完整，窗口由算法适配器生成。\n'
            '起立、卧倒、排尿、抬尾、甩尾与综合决策共用母标签和按牛划分。未标注区间是未知，不能自动当负样本。\n'
            '牛号冲突、未复核标注列入复核表；PPG 继续保留占位。')
        self.dataset_help.setWordWrap(True)
        dataset_form.addRow(self.dataset_help)
        self.export = QPushButton('从标注生成算法数据集')
        self.export.clicked.connect(self.export_data)
        dataset_form.addRow(self.export)
        self.tabs.addTab(datasets,'② 从标注生成数据集')
        self.tabs.setCurrentIndex(mode)
        self.status = QLabel('请选择来源；预览不会修改原始数据。')
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(140)
        outer.addWidget(self.details)
        self.bar = QProgressBar()
        outer.addWidget(self.bar)
        footer = QHBoxLayout()
        self.cancel = QPushButton('暂停任务')
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self.cancel_job)
        self.open_output = QPushButton('打开结果目录')
        self.open_output.setEnabled(False)
        self.open_output.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(self.output)))
        footer.addWidget(self.cancel)
        footer.addWidget(self.open_output)
        outer.addLayout(footer)
        catalog = getattr(owner,'catalog',None)
        if catalog:
            self.dataset_sources.setPlainText(str(catalog.root))
            self.target.setText(str(catalog.root))
        for control in (self.label_sources,self.raw_sources):
            control.textChanged.connect(self.invalidate)
        for control in (self.target,self.farm):
            control.textEdited.connect(self.invalidate)
        self.category.currentIndexChanged.connect(self.invalidate)

    def _paths_row(self, form, title, control):
        row = QHBoxLayout()
        row.addWidget(control)
        add = QPushButton('添加目录…')
        def choose():
            path = QFileDialog.getExistingDirectory(self,title)
            if path:
                control.appendPlainText(path)
        add.clicked.connect(choose)
        row.addWidget(add)
        form.addRow(title,row)

    def _directory_row(self, form, title, control):
        row = QHBoxLayout()
        row.addWidget(control)
        choose = QPushButton('选择…')
        def pick():
            path = QFileDialog.getExistingDirectory(self,title,control.text())
            if path:
                control.setText(path)
                self.invalidate()
        choose.clicked.connect(pick)
        row.addWidget(choose)
        form.addRow(title,row)

    @staticmethod
    def paths(control):
        return [p.strip().strip('"') for p in control.toPlainText().splitlines() if p.strip()]

    def invalidate(self, *_):
        self.plan_path = None
        self.execute.setEnabled(False)

    def preview_migration(self):
        code = self.category.currentData()
        if not code or not self.paths(self.label_sources) or not self.target.text().strip():
            self.status.setText('请填写旧标签来源、目标目录和类别；怀孕须明确选择孕期。')
            return
        from .organization import safe_name
        try:
            farm = safe_name(self.farm.text().strip())
            target = category_root(self.target.text().strip(),farm,code)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.start_job({'action':'legacy_preview','labels':self.paths(self.label_sources),'raw':self.paths(self.raw_sources),
                        'target':str(target),'category':code,'identity_policy':'review'})

    def execute_migration(self):
        if self.plan_path:
            self.start_job({'action':'legacy_execute','plan':self.plan_path})

    def export_data(self):
        if not self.paths(self.dataset_sources) or not self.dataset_target.text().strip():
            self.status.setText('请填写标注来源与一个尚不存在的新数据集批次目录。')
            return
        if getattr(self.owner,'work',None):
            self.owner.save_current()
            if getattr(self.owner, 'dirty', False):
                self.status.setText('当前标注未能保存，请先处理保存错误再生成数据集。')
                return
        self.start_job({'action':'dataset_export','sources':self.paths(self.dataset_sources),
                        'target':self.dataset_target.text().strip(),'historical':self.historical.text().strip() or None,
                        'allow_proxy':self.allow_proxy.isChecked()})

    def start_job(self, request):
        if self.running:
            return
        self.job = Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'COWMATA Annotator/dataset-jobs'/uuid.uuid4().hex
        self.job.mkdir(parents=True)
        (self.job/'request.json').write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
        self.running = True
        self.tabs.setEnabled(False)
        self.cancel.setEnabled(True)
        self.bar.setRange(0,0)
        self.status.setText('后台处理中；原始数据与已有数据集保留。')
        self.details.clear()
        self.progress_buffer = ''
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read_progress)
        self.process.finished.connect(self.job_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.process.start(sys.executable,['-B',str(Path(__file__).with_name('dataset_worker.py')),str(self.job)])

    def read_progress(self):
        self.progress_buffer += bytes(self.process.readAllStandardOutput()).decode('utf-8',errors='replace')
        lines = self.progress_buffer.split('\n')
        self.progress_buffer = lines.pop()
        for line in lines:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            self.status.setText(str(value.get('path','')))

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.job_finished(-1)

    def job_finished(self, code, *_):
        self.running = False
        self.tabs.setEnabled(True)
        self.cancel.setEnabled(False)
        self.bar.setRange(0,100)
        result = self.job/'result.json'
        error = self.job/'error.json'
        if code != 0 or not result.is_file():
            message = json.loads(error.read_text(encoding='utf-8')).get('error') if error.is_file() else '任务未完成，请查看任务记录。'
            self.status.setText(message)
            self.bar.setValue(0)
            return
        value = json.loads(result.read_text(encoding='utf-8'))
        names = {'raw_files':'来源九轴文件','unique_raw':'去重后九轴文件','labels':'旧标签文件',
                 'matched_events':'已匹配事件','unresolved':'待核条目','identity_conflicts':'牛号冲突',
                 'events':'保留事件','documents':'新版标注文件','positive_events':'可用正事件',
                 'negative_events':'确认负事件','review_events':'待核事件','independent_cows':'独立牛数',
                 'raw_sources':'完整九轴文件','target':'结果目录'}
        summary = value.get('summary',value)
        lines = [names[k]+'：'+str(v) for k,v in summary.items() if k in names]
        for head in value.get('algorithms',[]):
            lines.append(head['head']+'：'+('满足训练输入条件' if head['ready_to_fit'] else head['reason']))
        self.details.setPlainText('\n'.join(lines))
        self.bar.setValue(100)
        if value.get('action') == 'legacy_preview':
            self.plan_path = value['plan']
            self.execute.setEnabled(True)
            self.status.setText('预览完成，请核对数量。执行后生成可继续编辑的工程和单文件标注；待核项保留。')
        else:
            self.output = value.get('target','')
            self.open_output.setEnabled(bool(self.output))
            self.status.setText('任务完成。结果目录含来源索引、复核记录与校验清单。')

    def cancel_job(self):
        if self.running:
            (self.job/'cancel').touch()
            self.status.setText('已请求暂停，等待当前文件处理完成。')

    def closeEvent(self,event):
        if self.running:
            self.cancel_job()
            event.ignore()
        else:
            event.accept()
