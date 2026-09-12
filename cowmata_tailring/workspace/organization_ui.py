"""Nonmodal data workspace; heavy work runs in one private child process."""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QProcess, QSettings, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .data_category import CATEGORIES, PREGNANCY_STAGES
from .dataset_access import ensure_available, overlaps
from .organization import VIEWS
from .theme import STYLE


def task_root():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "COWMATA Annotator" / "organization-tasks"


class PlanModel(QAbstractTableModel):
    COLUMNS = (("source_folder", "设备目录 / 来源"), ("status", "状态"),
               ("suggested_folder", "建议目录名（需人工确认）"), ("device", "设备 / 视角"),
               ("cow_id", "牛耳标"), ("field_mark", "现场记号"), ("record_date", "采集日期"),
               ("source", "来源文件"), ("target", "归类目标"), ("size", "大小"), ("message", "审查说明"))
    STATES = {"ready": "可归类", "skip": "保留原处", "junk": "待隔离", "quarantine": "移至隔离", "invalid": "需核对", "blocked": "已拦截", "existing": "已归档"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def columnCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section][1] if orientation == Qt.Orientation.Horizontal else section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        key = self.COLUMNS[index.column()][0]
        if role == Qt.ItemDataRole.ForegroundRole and key == "status":
            return QColor({"ready": "#367622", "skip": "#687b73", "junk": "#9a6800", "quarantine": "#9a6800",
                           "invalid": "#a33d28", "blocked": "#b52c25"}.get(row["status"], "#20332a"))
        if role in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole}:
            if key == "size":
                return f"{row.get(key, 0) / 1024**2:,.2f} MiB"
            if key == "status":
                return self.STATES.get(row.get(key), row.get(key, ""))
            if key == "device":
                return row.get("device_id") or row.get("owner") or row.get("device", "")
            if key == "source_folder":
                return row.get(key) or Path(row.get("source", "")).parent.name
            return row.get(key, "")
        return None


class OrganizationWindow(QDialog):
    def __init__(self, window):
        super().__init__(window, Qt.WindowType.Window)
        self.owner = window
        self.settings = QSettings()
        self.process = None
        self._active_task = False
        self.stderr = b""
        self.buffer = b""
        self.job = None
        self.plan_job = None
        self.plan = None
        self.report = None
        self.result_pending = None
        self.last_error = ""
        self.pause_pending = False
        self.setWindowTitle("COWMATA · 数据整理")
        self.resize(1240, 790)
        self.setMinimumSize(950, 650)
        self.setStyleSheet(STYLE + "QTabWidget::pane {border:1px solid #d7e4d0; background:white;}"
                          "QTabBar::tab {padding:10px 25px; background:#eaf3e4;}"
                          "QTabBar::tab:selected {background:#8add66; color:#20351c; font-weight:700;}")
        icon = Path(__file__).resolve().parents[2] / "assets/fluent/folder_open.svg"
        self.setWindowIcon(QIcon(str(icon)))
        outer = QVBoxLayout(self)
        title = QLabel("数据整理 · 审查、归类与异常追踪")
        title.setObjectName("sectionTitle")
        title.setStyleSheet("font-size:21px; padding:8px;")
        outer.addWidget(title)
        subtitle = QLabel("先审查来源命名与数据，再归类、标注和导出。单份、单日或跨日均支持；同设备换日期或换牛逐记录保留。")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)
        target_bar = QHBoxLayout()
        target_bar.addWidget(QLabel("数据工程"))
        self.target = QLineEdit()
        current = getattr(window, "catalog", None)
        self.target.setText(str(current.root) if current else self.settings.value("organization/target", "", type=str))
        self.target.setPlaceholderText("选择已有数据工程，或填写新工程的绝对目录")
        target_bar.addWidget(self.target, 1)
        self.target_browse = QPushButton("选择目录…")
        self.target_browse.clicked.connect(self.choose_target)
        target_bar.addWidget(self.target_browse)
        outer.addLayout(target_bar)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        audit_page, classify_page, report_page = QWidget(), QWidget(), QWidget()
        self.tabs.addTab(audit_page, "① 数据审查")
        self.tabs.addTab(classify_page, "② 数据归类")
        self.tabs.addTab(report_page, "③ 异常报告")
        audit_box = QVBoxLayout(audit_page)
        explanation = QLabel("先只读审查所选来源（未添加来源时审查上方工程）。不全量读取录像，不做 OCR、转码或切片。\n"
                             "系统临时文件和空文件可先预览隔离；异常九轴与已有标签保留，不能按“废料”直接删除。")
        explanation.setWordWrap(True)
        audit_box.addWidget(explanation)
        audit_actions = QHBoxLayout()
        self.audit_button = QPushButton("开始数据审查")
        self.audit_button.setObjectName("primary")
        self.audit_button.clicked.connect(self.start_audit)
        self.clean_button = QPushButton("预览隔离清理")
        self.clean_button.clicked.connect(self.preview_quarantine)
        audit_actions.addWidget(self.audit_button)
        audit_actions.addWidget(self.clean_button)
        audit_actions.addStretch()
        audit_box.addLayout(audit_actions)
        box = QVBoxLayout(classify_page)
        self.sources = QTableWidget(0, 3)
        self.sources.setHorizontalHeaderLabels(["素材类型", "来源绝对路径（文件或目录）", "固定视角"])
        self.sources.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.sources.setMaximumHeight(170)
        self.sources.setMinimumHeight(110)
        box.addWidget(self.sources)
        identity_rule = QLabel("九轴设备目录：完整设备编号-牛耳标号-现场记号，例如 546C50CA07D5-00123-w1。\n"
                               "设备号须为 12 位十六进制；耳标保留前导 0；现场记号保留大小写。耳标取前5位数字，余下去横线为现场标记；倒序取唯一5位耳标，歧义拦截。")
        identity_rule.setWordWrap(True)
        box.addWidget(identity_rule)
        add_bar = QHBoxLayout()
        self.add_buttons = []
        for text, callback in (("添加九轴文件…", self.add_imu_files),
                               ("添加九轴目录…", lambda: self.add_directory("imu")),
                               ("添加录像目录…", lambda: self.add_directory("video")),
                               ("添加录像文件…", self.add_video_files),
                               ("移除所选来源", self.remove_source)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            add_bar.addWidget(button)
            self.add_buttons.append(button)
        box.addLayout(add_bar)
        form = QFormLayout()
        dates = QHBoxLayout()
        self.start_date, self.end_date = QLineEdit(), QLineEdit()
        self.start_date.setPlaceholderText("可留空：自动读取真实采集日期")
        self.end_date.setPlaceholderText("可选：仅用于核对开始日期范围")
        dates.addWidget(self.start_date)
        dates.addWidget(QLabel("至"))
        dates.addWidget(self.end_date)
        form.addRow("采集日期", dates)
        self.category = QComboBox()
        self.category.addItem("请选择本批数据类别（必选）", "")
        for code in ("calving", "estrus", "pregnancy", "disease", "healthy"):
            label = CATEGORIES[code]
            self.category.addItem(label, code)
        self.category.setToolTip("数据类别随标注保存；所有类别均可自由标注六类通用行为。不同类别请分批整理。")
        form.addRow("数据类别", self.category)
        self.pregnancy_stage = QComboBox()
        self.pregnancy_stage.addItem("请选择孕期阶段（怀孕批次必选）", "")
        for code in PREGNANCY_STAGES:
            self.pregnancy_stage.addItem(CATEGORIES[code], code)
        self.pregnancy_stage.setToolTip("归入怀孕/孕早期、孕中期或孕晚期；不根据行为或产犊日期自动推断孕期。")
        form.addRow("孕期阶段", self.pregnancy_stage)
        self.pregnancy_stage.setEnabled(False)
        self.farm = QLineEdit("扬大_高邮牧场")
        form.addRow("牧场目录", self.farm)
        self.transfer_mode = QComboBox()
        self.transfer_mode.addItem("复制，保留原件", "copy")
        self.transfer_mode.addItem("同盘移动，跨盘复制", "move")
        self.transfer_mode.currentIndexChanged.connect(self.invalidate_plan)
        form.addRow("保存方式", self.transfer_mode)
        form.addRow("PPG", QLabel("已预留：按实际日期创建 PPG；当前不参与波形或标注计算"))
        self.allow_partial = QCheckBox("仅归档通过校验项，异常原件保留并列入报告")
        self.allow_partial.toggled.connect(self.partial_changed)
        form.addRow("异常处理", self.allow_partial)
        self.note = QLineEdit()
        self.note.setPlaceholderText("例如：沿用现有绑定；设备用于孕晚期行为观察。未知信息留空，不自动生成事件。")
        form.addRow("设备与实验说明", self.note)
        box.addLayout(form)
        classify_actions = QHBoxLayout()
        self.preview_button = QPushButton("校验命名并预览归类")
        self.preview_button.setObjectName("primary")
        self.preview_button.clicked.connect(self.preview_import)
        self.normalize_button = QPushButton("预览规范视角名称")
        self.normalize_button.clicked.connect(lambda: self.start_job({"action": "normalize", "target": self.target.text().strip()}))
        classify_actions.addWidget(self.preview_button)
        classify_actions.addWidget(self.normalize_button)
        self.legacy_dataset_button = QPushButton("旧标签与算法数据集…")
        self.legacy_dataset_button.clicked.connect(lambda: self.owner.open_dataset_workflow(0))
        classify_actions.addWidget(self.legacy_dataset_button)
        classify_actions.addStretch()
        box.addLayout(classify_actions)
        report_box = QVBoxLayout(report_page)
        report_text = QLabel("报告列出临时文件、空文件、九轴结构异常、路径冲突及跨盘拦截。来源、目标、状态和任务记录均可追溯。\n"
                             "绑定与实验表沿用原记录；参加某实验不等于已发生某事件，不自动标注正负样本。")
        report_text.setWordWrap(True)
        report_box.addWidget(report_text)
        report_actions = QHBoxLayout()
        self.export_button = QPushButton("导出审查报告 CSV…")
        self.export_button.clicked.connect(self.export_report)
        self.resume_button = QPushButton("继续上次整理任务")
        self.resume_button.clicked.connect(self.resume_task)
        self.load_task_button = QPushButton("载入整理任务…")
        self.load_task_button.clicked.connect(self.choose_task)
        self.logs_button = QPushButton("打开任务记录")
        self.logs_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.job or task_root()))))
        for button in (self.export_button, self.resume_button, self.load_task_button, self.logs_button):
            report_actions.addWidget(button)
        report_actions.addStretch()
        report_box.addLayout(report_actions)
        self.summary = QLabel("尚未审查 · 原始文件保留")
        self.summary.setStyleSheet("background:#e5f4db; padding:9px; border-radius:7px; font-weight:600;")
        outer.addWidget(self.summary)
        self.model = PlanModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, width in enumerate((270, 90, 310, 140, 90, 90, 105, 295, 300, 90, 330)):
            self.table.setColumnWidth(i, width)
        outer.addWidget(self.table, 1)
        self.status = QLabel("其他目录可同时标注；同目录须先保存并暂停。按所选方式归档，保留校验与恢复记录。")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        outer.addWidget(self.bar)
        bottom = QHBoxLayout()
        self.pause_project = QPushButton("保存并暂停当前工程")
        self.pause_project.clicked.connect(self.save_and_pause)
        self.execute_button = QPushButton("执行归档")
        self.execute_button.setObjectName("primary")
        self.execute_button.clicked.connect(self.execute_plan)
        self.cancel_button = QPushButton("暂停整理")
        self.cancel_button.clicked.connect(self.cancel)
        self.open_button = QPushButton("打开整理后的工程")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_result)
        for button in (self.pause_project, self.execute_button, self.cancel_button, self.open_button):
            bottom.addWidget(button)
        outer.addLayout(bottom)
        for field in (self.target, self.start_date, self.end_date, self.note, self.farm):
            field.textEdited.connect(self.invalidate_plan)
        self.sources.itemChanged.connect(self.invalidate_plan)
        self.category.currentIndexChanged.connect(self.invalidate_plan)
        self.category.currentIndexChanged.connect(lambda: self.pregnancy_stage.setEnabled(self.category.currentData() == "pregnancy" and not self.running))
        self.pregnancy_stage.currentIndexChanged.connect(self.invalidate_plan)
        self.render()

    @property
    def running(self):
        # QProcess can become NotRunning before its queued finished signal.
        # Keep controls locked until stdout/results have been fully consumed.
        return self._active_task

    def invalidate_plan(self, *_):
        if self.running:
            return
        self.plan = self.plan_job = None
        self.report = None
        self.open_button.setEnabled(False)
        self.render()

    def partial_changed(self, checked):
        if self.plan and self.plan.get("schema") == "cowmata-resources-3.4":
            self.plan["allow_partial"] = checked
            (self.plan_job / "plan.json").write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8")
        self.render()

    def choose_target(self):
        value = QFileDialog.getExistingDirectory(self, "选择数据工程目录", self.target.text())
        if value:
            self.target.setText(value)
            self.invalidate_plan()

    def add_source(self, kind, path, camera="视角01"):
        row = self.sources.rowCount()
        self.sources.insertRow(row)
        item = QTableWidgetItem("九轴" if kind == "imu" else "录像")
        item.setData(Qt.ItemDataRole.UserRole, kind)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.sources.setItem(row, 0, item)
        self.sources.setItem(row, 1, QTableWidgetItem(str(path)))
        combo = QComboBox()
        combo.addItems(VIEWS)
        combo.setCurrentText(camera)
        combo.setEnabled(kind == "video")
        combo.currentTextChanged.connect(self.invalidate_plan)
        self.sources.setCellWidget(row, 2, combo)
        self.invalidate_plan()

    def add_imu_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择单份或多份原始九轴", "", "JSON (*.json)")
        for path in paths:
            self.add_source("imu", path)

    def add_video_files(self):
        from .catalog import VIDEO_SUFFIXES
        patterns = " ".join("*" + ext for ext in sorted(VIDEO_SUFFIXES))
        paths, _ = QFileDialog.getOpenFileNames(self, "选择单个或多个录像", "", "Video (" + patterns + ")")
        for path in paths:
            self.add_source("video", path)

    def add_directory(self, kind):
        path = QFileDialog.getExistingDirectory(self, "选择九轴目录" if kind == "imu" else "选择一路录像目录")
        if path:
            used = sum(self.sources.item(i, 0).data(Qt.ItemDataRole.UserRole) == "video" for i in range(self.sources.rowCount()))
            self.add_source(kind, path, VIEWS[min(used, 7)])

    def remove_source(self):
        for row in sorted({i.row() for i in self.sources.selectedIndexes()}, reverse=True):
            self.sources.removeRow(row)
        self.invalidate_plan()

    def source_specs(self):
        return [{"kind": self.sources.item(i, 0).data(Qt.ItemDataRole.UserRole),
                 "path": self.sources.item(i, 1).text().strip(), "camera": self.sources.cellWidget(i, 2).currentText()}
                for i in range(self.sources.rowCount())]

    def start_audit(self):
        roots = [s["path"] for s in self.source_specs()] or [self.target.text().strip()]
        self.start_job({"action": "audit", "roots": roots})

    def preview_import(self):
        category = self.category.currentData()
        if category == "pregnancy":
            category = self.pregnancy_stage.currentData()
            if not category:
                self.status.setText("请先选择孕期阶段：孕早期、孕中期或孕晚期。")
                return
        self.start_job({"action": "import", "target": self.target.text().strip(), "sources": self.source_specs(),
                        "start": self.start_date.text().strip(), "end": self.end_date.text().strip(), "note": self.note.text(),
                        "category": category, "farm": self.farm.text().strip(), "transfer": self.transfer_mode.currentData()})

    def preview_quarantine(self):
        if self.report:
            self.start_job({"action": "quarantine", "report": self.report, "target": self.target.text().strip()})

    def start_job(self, request, job=None):
        if self.running or self.pause_pending:
            return
        if request["action"] != "execute":
            self.plan = None
            self.plan_job = None
            self.open_button.setEnabled(False)
            if request["action"] != "quarantine":
                self.report = None
        self.job = Path(job) if job else task_root() / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        self.job.mkdir(parents=True, exist_ok=True)
        (self.job / "cancel").unlink(missing_ok=True)
        (self.job / "request.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        self.settings.setValue("organization/target", self.target.text())
        self.result_pending, self.last_error, self.buffer, self.stderr = None, "", b"", b""
        self.status.setText("后台处理中，标注窗口可继续使用其他工程…")
        self.bar.setRange(0, 0)
        if self.process is not None:
            self.process.deleteLater()
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(self.read_error)
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)
        program = Path(sys.executable)
        if os.name == "nt" and program.with_name("pythonw.exe").is_file():
            program = program.with_name("pythonw.exe")
        self._active_task = True
        self.open_button.setEnabled(False)
        self.process.start(str(program), ["-I", "-B", str(Path(__file__).with_name("organization_worker.py")), str(self.job)])
        self.render()

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._active_task = False
            self.last_error = "无法启动整理进程：" + self.process.errorString()
            self.status.setText(self.last_error)
            self.bar.setRange(0, 1000)
            self.render()

    def read_error(self):
        self.stderr = (self.stderr + bytes(self.process.readAllStandardError()))[-8192:]

    def read_output(self):
        self.buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if message["event"] == "progress":
                total, current = message["total"], message["current"]
                if total:
                    self.bar.setRange(0, 1000)
                    self.bar.setValue(round(current / total * 1000))
                self.status.setText(f"已处理 {current}" + (f" / {total}" if total else "") + " · " + message["path"])
            elif message["event"] == "result":
                self.result_pending = self.job / "result.json"
            elif message["event"] == "error":
                self.last_error = message["message"]

    def process_finished(self, code, _status):
        self.read_output()
        self.read_error()
        self._active_task = False
        self.bar.setRange(0, 1000)
        if code == 0 and self.result_pending:
            result = json.loads(self.result_pending.read_text(encoding="utf-8"))
            self.model.set_rows(result.get("rows", []))
            counts = Counter(r["status"] for r in result.get("rows", []))
            naming_issues = sum(bool(r.get("naming_issue")) for r in result.get("rows", []))
            size = sum(r["size"] for r in result.get("rows", []) if r["status"] == "ready")
            self.summary.setText(f"共 {len(result.get('rows', []))} 项 · 可归类 {counts['ready']} 项 / {size / 1024**3:.2f} GiB · 命名待规范 {naming_issues} 项 · 待核对 {counts['invalid']} 项 · 拦截 {counts['blocked']} 项 · 临时文件 {counts['junk']} 项")
            if result["mode"] == "audit":
                self.report = result
                self.status.setText("审查完成。可预览隔离临时/空文件，再到数据归类确认目标。异常原件继续保留。")
            elif result.get("completed"):
                self.plan = None
                self.completed_target = result["target"]
                self.status.setText(f"整理完成：{result['moved']} 项，{result['seconds']:.2f} 秒；文件身份校验与任务记录已保存。")
                if result.get("unresolved"):
                    self.status.setText(self.status.text() + f" 待核实 {result['unresolved']} 项，见整理异常.csv；原件保留。")
                self.bar.setValue(1000)
                self.owner.tell(self.status.text())
                self.open_button.setEnabled(result["mode"] != "quarantine")
            else:
                self.plan, self.plan_job = result, self.job
                self.partial_changed(self.allow_partial.isChecked())
                self.status.setText("设备命名需先规范：请逐项核对建议并人工修改来源目录后重新审查。本批禁止执行，有效九轴原件保留。" if naming_issues else
                                    "预览完成，尚未移动文件。核对来源、目标和异常后点击执行；本批按真实日期归档，支持跨盘复制。")
        else:
            self.status.setText(self.last_error or self.stderr.decode("utf-8", errors="replace") or "任务未完成，请查看任务记录并重试；原文件不会被覆盖。")
        self.render()

    def execute_plan(self):
        if not self.plan or self.running:
            return
        paths = [self.plan["target"], *(s["path"] for s in self.plan["sources"])]
        try:
            ensure_available(paths, "organize", owner=self.plan["id"])
        except OSError as exc:
            self.status.setText(str(exc))
            return
        count = sum(r["status"] in {"ready", "quarantine"} for r in self.plan["rows"])
        message = f"归档 {count} 项；已有目标不覆盖。" + ("同盘原文件将移至新目录，跨盘保留原件。" if self.plan.get("transfer") == "move" else "复制并保留原件。")
        message += "临时/空文件将移到工程外隔离目录，可按任务记录恢复。" if self.plan["mode"] == "quarantine" else "目录为牧场/类别/Motion、PPG、Video/日期；移动核对文件身份，复制核对 SHA-256。"
        if QMessageBox.question(self, "确认本次整理", message) != QMessageBox.StandardButton.Yes:
            return
        self.settings.setValue("organization/resume_job", str(self.plan_job))
        self.start_job({"action": "execute"}, self.plan_job)

    def resume_task(self):
        job = Path(self.settings.value("organization/resume_job", "", type=str))
        if not (job / "plan.json").is_file():
            self.status.setText("没有可继续的整理任务。")
            return
        self.load_task(job)

    def choose_task(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择整理任务的 plan.json", str(task_root()), "Plan (plan.json)")
        if path:
            self.load_task(Path(path).parent)

    def load_task(self, job):
        try:
            plan = json.loads((job / "plan.json").read_text(encoding="utf-8"))
            if plan.get("mode") not in {"import", "normalize", "quarantine"} or not isinstance(plan.get("rows"), list):
                raise ValueError("Invalid organization plan")
            self.target.setText(plan.get("resource_root", plan["target"]))
            self.farm.setText(plan.get("farm", "扬大_高邮牧场"))
            self.transfer_mode.setCurrentIndex(max(0, self.transfer_mode.findData(plan.get("transfer", "copy"))))
            self.start_date.setText(plan.get("start", ""))
            self.end_date.setText(plan.get("end", ""))
            code = plan.get("category", "")
            self.category.setCurrentIndex(max(0, self.category.findData("pregnancy" if code in PREGNANCY_STAGES else code)))
            self.pregnancy_stage.setCurrentIndex(max(0, self.pregnancy_stage.findData(code)))
            self.note.setText(plan.get("note", ""))
            self.sources.setRowCount(0)
            for spec in plan["sources"]:
                if spec.get("kind") in {"imu", "video"}:
                    self.add_source(spec["kind"], spec["path"], spec.get("camera", VIEWS[0]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status.setText(str(exc))
            self.invalidate_plan()
            return
        self.plan = plan
        self.plan_job = job
        self.allow_partial.setChecked(bool(plan.get("allow_partial")))
        self.model.set_rows(self.plan["rows"])
        self.status.setText("已恢复原计划。点击执行后核对文件身份，跳过已完成项。")
        self.render()

    def cancel(self):
        if self.running:
            (self.job / "cancel").touch()
            self.status.setText("正在暂停，完成当前文件后停止；已完成项和记录保留。")

    def save_and_pause(self):
        if self.running or self.pause_pending or not getattr(self.owner, "catalog", None):
            return
        root = self.owner.catalog.root
        paths = [self.plan["target"], *(s["path"] for s in self.plan["sources"])] if self.plan else [self.target.text()]
        if not any(p and overlaps(root, p) for p in paths):
            self.status.setText("当前标注工程与整理目录无冲突，可以同时进行。")
            return
        self.pause_pending = True
        self.owner.pause_for_organization(self.pause_finished)
        self.render()

    def pause_finished(self, message):
        self.pause_pending = False
        self.status.setText(message)
        self.render()

    def open_result(self):
        if not self.running and self.target.text().strip():
            self.owner.open_project(getattr(self, "completed_target", self.target.text().strip()))
            self.owner.raise_()

    def export_report(self):
        if self.job and (self.job / "report.csv").is_file():
            path, _ = QFileDialog.getSaveFileName(self, "导出数据审查报告", "数据审查报告.csv", "CSV (*.csv)")
            if path:
                shutil.copy2(self.job / "report.csv", path)

    def render(self):
        busy = self.running or self.pause_pending
        for widget in (self.target, self.target_browse, self.sources, self.start_date, self.end_date, self.note, self.category, self.farm, self.transfer_mode, self.allow_partial,
                       self.audit_button, self.preview_button, self.normalize_button, self.resume_button, self.load_task_button, *self.add_buttons):
            widget.setEnabled(not busy)
        self.clean_button.setEnabled(not busy and bool(self.report and any(r.get("quarantine") for r in self.report["rows"])))
        self.execute_button.setEnabled(not busy and bool(self.plan and any(r["status"] in {"ready", "existing", "quarantine"} for r in self.plan["rows"])
                                                     and (self.plan.get("allow_partial") or not any(r["status"] in {"blocked", "invalid"} for r in self.plan["rows"]))))
        self.cancel_button.setEnabled(self.running)
        self.pregnancy_stage.setEnabled(not busy and self.category.currentData() == "pregnancy")
        self.pause_project.setEnabled(not busy and bool(getattr(self.owner, "catalog", None)))
        self.export_button.setEnabled(not busy and bool(self.job and (self.job / "report.csv").is_file()))

    def closeEvent(self, event):
        if self.running or self.pause_pending:
            self.hide()
            event.ignore()
        else:
            event.accept()
