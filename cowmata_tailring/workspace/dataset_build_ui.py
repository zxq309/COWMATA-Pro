"""Dataset construction UI: audit, behavior samples and decision inputs."""

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
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
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

from .behavior_dataset import BEHAVIORS
from .theme import STYLE


class DatasetBuildWindow(QDialog):
    def __init__(self, owner, mode=0):
        super().__init__(owner, Qt.WindowType.Window)
        self.owner, self.process, self.job = owner, None, None
        self.running = False
        self.output = ""
        self.setWindowTitle("COWMATA · 数据集构建")
        self.resize(1000, 790)
        self.setStyleSheet(STYLE)
        outer = QVBoxLayout(self)
        form = QFormLayout()
        self.sources = QPlainTextEdit()
        self.sources.setMaximumHeight(115)
        self.sources.setPlaceholderText(
            "每行一个类别工程、导出标注目录或单文件标注；可同时选择不同类别。"
        )
        row = QHBoxLayout()
        row.addWidget(self.sources)
        add = QPushButton("添加目录…")
        add.clicked.connect(self.add_source)
        row.addWidget(add)
        form.addRow("标注来源", row)
        self.target = QLineEdit()
        self.target.setPlaceholderText("构建到新的目录，已有数据集不会被覆盖")
        row = QHBoxLayout()
        row.addWidget(self.target)
        choose = QPushButton("选择父目录…")
        choose.clicked.connect(self.choose_output)
        row.addWidget(choose)
        form.addRow("数据集目录", row)
        outer.addLayout(form)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        audit = QWidget()
        box = QVBoxLayout(audit)
        box.addWidget(QLabel("先检查标签、牛耳标、归类与来源。检查不会修改标签或创建训练样本。"))
        self.audit_button = QPushButton("检查标签与来源")
        self.audit_button.clicked.connect(lambda: self.submit("dataset_audit"))
        box.addWidget(self.audit_button)
        box.addStretch()
        self.tabs.addTab(audit, "标签与来源检查")
        behaviors = QWidget()
        box = QVBoxLayout(behaviors)
        text = QLabel(
            "每个行为单独建数据集，按耳标区分文件和样本。\n"
            "爬跨仅用于发情，努责仅用于产犊；其他行为合并全部所选类别，不再按类别嵌套子目录。"
        )
        text.setWordWrap(True)
        box.addWidget(text)
        grid = QGridLayout()
        self.behavior_checks = {}
        for i, (code, title) in enumerate(BEHAVIORS.items()):
            check = QCheckBox(title)
            check.setChecked(True)
            self.behavior_checks[code] = check
            grid.addWidget(check, i // 4, i % 4)
        box.addLayout(grid)
        self.native_features = QCheckBox(
            "同时生成已接入算法的训练特征（起立、卧倒、抬尾、甩尾、排尿；耗时较长）"
        )
        box.addWidget(self.native_features)
        self.behavior_button = QPushButton("构建所选行为数据集")
        self.behavior_button.clicked.connect(lambda: self.submit("behavior_build"))
        box.addWidget(self.behavior_button)
        self.tabs.addTab(behaviors, "按行为与耳标构建")
        decision = QWidget()
        box = QVBoxLayout(decision)
        text = QLabel(
            "从完整连续九轴、温度和母标签构建综合决策输入。\n"
            "保留产犊 T0、来源、质量与缺失情况；人工标签不作为算法输入。PPG 继续预留。"
        )
        text.setWordWrap(True)
        box.addWidget(text)
        self.decision_button = QPushButton("构建综合决策数据集")
        self.decision_button.clicked.connect(lambda: self.submit("decision_build"))
        box.addWidget(self.decision_button)
        box.addStretch()
        self.tabs.addTab(decision, "综合决策构建")
        guide = QWidget()
        box = QVBoxLayout(guide)
        text = QLabel(
            "1. 先保存当前标注，再检查来源与牛耳标。\n\n"
            "2. 同一头牛只进入一个训练、验证或测试集合；全部行为共用 cow-splits.json。少于三头牛暂不自动划分。\n\n"
            "3. 未标注不等于负例，身份冲突和未复核标签不进入训练；点事件与区间保留真实时间。\n\n"
            "4. 文件名只含耳标、设备与起始时间；行为样本的起始时间保留毫秒。原始九轴不插值、不补帧。\n\n"
            "5. 输出目录保留样本索引、待核清单和校验清单；原标签修改后构建到新批次目录。"
        )
        text.setWordWrap(True)
        box.addWidget(text)
        box.addStretch()
        self.tabs.addTab(guide, "按牛划分与构建说明")
        self.tabs.setCurrentIndex(mode)
        self.status = QLabel("请选择标注来源。")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(170)
        outer.addWidget(self.details)
        self.bar = QProgressBar()
        outer.addWidget(self.bar)
        row = QHBoxLayout()
        self.cancel = QPushButton("暂停任务")
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self.cancel_job)
        self.open_output = QPushButton("打开结果目录")
        self.open_output.setEnabled(False)
        self.open_output.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.output))
        )
        row.addWidget(self.cancel)
        row.addWidget(self.open_output)
        outer.addLayout(row)
        if getattr(owner, "catalog", None):
            self.sources.setPlainText(str(owner.catalog.root))

    def add_source(self):
        path = QFileDialog.getExistingDirectory(self, "选择标注来源")
        if path:
            self.sources.appendPlainText(path)

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择数据集父目录")
        if path:
            self.target.setText(str(Path(path) / "新数据集"))

    def submit(self, action):
        sources = [
            p.strip().strip('"') for p in self.sources.toPlainText().splitlines() if p.strip()
        ]
        if not sources or action != "dataset_audit" and not self.target.text().strip():
            self.status.setText("请填写标注来源；构建任务还需指定新数据集目录。")
            return
        if getattr(self.owner, "work", None):
            if getattr(self.owner.catalog,'standalone',False) and self.owner.standalone_unsaved():
                previous=self.owner._standalone_saved_path
                if not self.owner.save_user_annotations():
                    return
                sources=[self.owner._standalone_saved_path if p==previous else p for p in sources]
                self.sources.setPlainText('\n'.join(sources))
            self.owner.save_current()
            if self.owner.dirty:
                self.status.setText("当前标签未保存，请先处理保存错误。")
                return
        request = dict(
            action=action,
            sources=sources,
            target=self.target.text().strip(),
            native_features=self.native_features.isChecked(),
            behaviors=[code for code, check in self.behavior_checks.items() if check.isChecked()],
        )
        self.start_job(request)

    def start_job(self, request):
        if self.running:
            return
        self.job = (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            / "COWMATA Annotator/dataset-jobs"
            / uuid.uuid4().hex
        )
        self.job.mkdir(parents=True)
        (self.job / "request.json").write_text(
            json.dumps(request, ensure_ascii=False), encoding="utf-8"
        )
        self.running = True
        self.tabs.setEnabled(False)
        self.cancel.setEnabled(True)
        self.bar.setRange(0, 0)
        self.details.clear()
        self.progress_buffer = ""
        self.status.setText("后台处理中，来源保持不变。")
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read_progress)
        self.process.finished.connect(self.job_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.process.start(
            sys.executable,
            ["-B", str(Path(__file__).with_name("dataset_worker.py")), str(self.job)],
        )

    def read_progress(self):
        self.progress_buffer += bytes(self.process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        lines = self.progress_buffer.split("\n")
        self.progress_buffer = lines.pop()
        for line in lines:
            try:
                self.status.setText(str(json.loads(line).get("path", "")))
            except ValueError:
                pass

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.job_finished(-1)

    def job_finished(self, code, *_):
        self.running = False
        self.tabs.setEnabled(True)
        self.cancel.setEnabled(False)
        self.bar.setRange(0, 100)
        if code or not (self.job / "result.json").is_file():
            error = self.job / "error.json"
            self.status.setText(
                json.loads(error.read_text(encoding="utf-8")).get("error")
                if error.is_file()
                else "任务未完成，请检查任务记录。"
            )
            self.bar.setValue(0)
            return
        result = json.loads((self.job / "result.json").read_text(encoding="utf-8"))
        names = {
            "documents": "标注文件",
            "events": "事件",
            "cows": "牛数",
            "review_events": "待核事件",
            "behavior_samples": "行为样本",
            "behavior_review": "待核标签",
            "raw_sources": "完整九轴来源",
            "target": "结果目录",
        }
        lines = [names[k] + "：" + str(v) for k, v in result.items() if k in names]
        lines += [
            str(r.get("cow_id", "")) + " / " + r["reason"] for r in result.get("issues", [])[:20]
        ]
        self.details.setPlainText("\n".join(lines))
        self.output = result.get("target", "")
        self.open_output.setEnabled(bool(self.output))
        self.bar.setValue(100)
        self.status.setText("处理完成，请核对结果与待核清单。")

    def cancel_job(self):
        if self.running:
            (self.job / "cancel").touch()

    def closeEvent(self, event):
        if self.running:
            self.cancel_job()
            event.ignore()
        else:
            event.accept()
