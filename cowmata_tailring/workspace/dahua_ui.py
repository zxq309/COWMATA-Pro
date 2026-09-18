"""Independent original-video preparation, ending in standard classified MP4."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import dahua_tasks as tasks
from .data_category import CATEGORIES
from .storage import atomic_json


class DahuaPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = QSettings()
        self.job = None
        self.index = None
        self.process = None
        self.active = False
        self.groups = {}
        self.files = []
        self.json_sources = []
        self.disks = []
        self.buffer = b""
        self.error = ""
        outer = QVBoxLayout(self)
        help = QLabel("选择原始录像 → Channel 01–20 自动对应视角01–20 → 转为标准 MP4，按日期归类。")
        help.setWordWrap(True)
        outer.addWidget(help)
        row = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems(["录像文件 / 目录", "录像机原盘（只读）"])
        row.addWidget(self.mode)
        self.source_text = QLineEdit()
        self.source_text.setReadOnly(True)
        self.source_text.setPlaceholderText("DAV / DHAV 文件，或含原始录像的目录")
        row.addWidget(self.source_text, 1)
        self.disk_choice = QComboBox()
        self.disk_choice.setMinimumWidth(350)
        row.addWidget(self.disk_choice)
        self.files_button = QPushButton("选择文件…")
        self.files_button.clicked.connect(self.choose_files)
        row.addWidget(self.files_button)
        self.folder_button = QPushButton("选择目录…")
        self.folder_button.clicked.connect(self.choose_folder)
        row.addWidget(self.folder_button)
        self.disk_button = QPushButton("刷新原盘")
        self.disk_button.clicked.connect(lambda: self.start("disks", {}))
        row.addWidget(self.disk_button)
        outer.addLayout(row)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.mode_changed()
        row = QHBoxLayout()
        row.addWidget(QLabel("牧场目录"))
        self.target = QLineEdit()
        self.target.setPlaceholderText("选择保存归类结果的牧场")
        self.target.setText(str(self.settings.value("dahua/target", "")))
        row.addWidget(self.target, 1)
        self.target_button = QPushButton("选择…")
        self.target_button.clicked.connect(self.choose_target)
        row.addWidget(self.target_button)
        self.category = QComboBox()
        for key, title in CATEGORIES.items():
            if key != "pregnancy":
                self.category.addItem(title, key)
        self.category.setCurrentIndex(self.category.findData("calving"))
        row.addWidget(self.category)
        self.scenario = QComboBox()
        self.scenario.addItem("按录像日期归档视频", "mixed")
        self.scenario.addItem("关联已有九轴记录（可选）", "attach_video")
        row.addWidget(self.scenario)
        outer.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("时间范围（北京时间）"))
        self.start_time = QLineEdit()
        self.end_time = QLineEdit()
        self.start_time.setPlaceholderText("开始 YYYY-MM-DD HH:MM:SS，可空")
        self.end_time.setPlaceholderText("结束 YYYY-MM-DD HH:MM:SS，可空")
        row.addWidget(self.start_time)
        row.addWidget(self.end_time)
        self.midnight = QCheckBox("跨午夜按天拆分")
        self.midnight.setChecked(True)
        row.addWidget(self.midnight)
        outer.addLayout(row)
        row = QHBoxLayout()
        self.import_json = QCheckBox("同时导入外部传感器 JSON（可选）")
        row.addWidget(self.import_json)
        self.json_button = QPushButton("选择外部 JSON 目录…")
        self.json_button.clicked.connect(self.choose_json)
        row.addWidget(self.json_button)
        self.json_label = QLabel("仅转码视频无需选择；牧场中已有的九轴、PPG、温度会保留。")
        row.addWidget(self.json_label, 1)
        outer.addLayout(row)
        self.import_json.toggled.connect(self.json_toggled)
        self.json_toggled(False)
        self.output_hint = QLabel()
        self.output_hint.setWordWrap(True)
        outer.addWidget(self.output_hint)
        self.target.textChanged.connect(self.update_output_hint)
        self.category.currentIndexChanged.connect(self.update_output_hint)
        self.update_output_hint()
        row = QHBoxLayout()
        self.scan_button = QPushButton("扫描原始录像")
        self.scan_button.clicked.connect(self.scan)
        row.addWidget(self.scan_button)
        self.preview_button = QPushButton("核对已选通道缩略图")
        self.preview_button.clicked.connect(self.previews)
        row.addWidget(self.preview_button)
        self.resume_button = QPushButton("恢复上次视频任务")
        self.resume_button.clicked.connect(self.restore)
        row.addWidget(self.resume_button)
        row.addStretch()
        outer.addLayout(row)
        self.table = QTableWidget(20, 4)
        self.table.setHorizontalHeaderLabels(
            ["归档视角", "原通道 / 来源（自动匹配）", "静态预览", "数据状态"]
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 105)
        self.table.setColumnWidth(2, 150)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.setWordWrap(False)
        self.preview_paths = {}
        self.mapping = []
        for i, view in enumerate(tasks.VIEWS):
            self.table.setItem(i, 0, QTableWidgetItem(view))
            combo = QComboBox()
            combo.setMinimumContentsLength(12)
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.addItem("不接入此视角", "")
            self.table.setCellWidget(i, 1, combo)
            self.mapping.append(combo)
            combo.currentIndexChanged.connect(lambda index, row=i: self.mapping_changed(row))
            preview = QPushButton("尚未预览")
            preview.setEnabled(False)
            preview.clicked.connect(lambda checked=False, row=i: self.show_preview(row))
            self.table.setCellWidget(i, 2, preview)
            self.table.setItem(i, 3, QTableWidgetItem("未选择来源"))
        self.fit_table_rows()
        outer.addWidget(self.table, 1)
        self.status = QLabel(
            "扫描后自动填入同号通道；未发现的通道保留空视角文件夹，可直接开始转码。"
        )
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        outer.addWidget(self.bar)
        row = QHBoxLayout()
        self.run_button = QPushButton("开始转码并归类")
        self.run_button.clicked.connect(self.organize)
        row.addWidget(self.run_button)
        self.pause_button = QPushButton("暂停")
        self.pause_button.clicked.connect(self.pause)
        row.addWidget(self.pause_button)
        self.output_button = QPushButton("打开归类目录")
        self.output_button.clicked.connect(self.open_output)
        row.addWidget(self.output_button)
        self.report_button = QPushButton("查看视频任务记录")
        self.report_button.clicked.connect(self.open_report)
        row.addWidget(self.report_button)
        outer.addLayout(row)
        self.controls = [
            self.mode,
            self.source_text,
            self.disk_choice,
            self.files_button,
            self.folder_button,
            self.disk_button,
            self.target,
            self.target_button,
            self.category,
            self.scenario,
            self.start_time,
            self.end_time,
            self.midnight,
            self.import_json,
            self.json_button,
            self.scan_button,
            self.preview_button,
            self.resume_button,
            self.run_button,
            *self.mapping,
        ]
        self._disks_loaded = False
        self.disk_choice.addItem("请选择录像机原盘…", None)
        self.disk_choice.activated.connect(self.disk_selected)
        self.mode.setCurrentIndex(1)
        self.refresh()

    def fit_table_rows(self):
        # Match the controls and current font/DPI, not an empty thumbnail frame.
        height = max(
            34,
            self.fontMetrics().height() + 16,
            max(c.sizeHint().height() + 4 for c in self.mapping),
        )
        self.table.verticalHeader().setDefaultSectionSize(height)
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, height)
        self.table.setColumnWidth(
            0, max(105, self.fontMetrics().horizontalAdvance("固定归档视角") + 20)
        )
        self.table.setColumnWidth(
            2, max(150, self.fontMetrics().horizontalAdvance("查看大图") + 70)
        )

    def showEvent(self, event):
        super().showEvent(event)
        self.fit_table_rows()
        if self.mode.currentIndex() == 1 and not self._disks_loaded and not self.running:
            self._disks_loaded = True
            QTimer.singleShot(0, lambda: self.start("disks", {}))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "mapping"):
            self.fit_table_rows()

    def show_preview(self, row):
        image = QPixmap(self.preview_paths.get(row, ""))
        if image.isNull():
            return
        box = QDialog(self)
        box.setWindowTitle(tasks.VIEWS[row] + " · 静态预览")
        layout = QVBoxLayout(box)
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        available = self.screen().availableGeometry()
        label.setPixmap(
            image.scaled(
                min(1100, int(available.width() * 0.8)),
                min(750, int(available.height() * 0.75)),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        layout.addWidget(label)
        box.exec()

    @property
    def running(self):
        return self.active

    def mode_changed(self):
        disk = self.mode.currentIndex() == 1
        for widget in (self.source_text, self.files_button, self.folder_button):
            widget.setVisible(not disk)
        self.disk_choice.setVisible(disk)
        self.disk_button.setVisible(disk)
        if (
            disk
            and self.isVisible()
            and hasattr(self, "controls")
            and not self._disks_loaded
            and not self.running
        ):
            self._disks_loaded = True
            self.start("disks", {})

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择原始录像",
            "",
            "原始录像 (*.dav *.dhav *.h264 *.h265)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if paths:
            self.files = paths
            self.source_text.setText("；".join(paths))
            self.index = None
            self.scan()

    def choose_folder(self):
        directory = QFileDialog.getExistingDirectory(
            self, "选择原始录像目录", "", QFileDialog.Option.DontUseNativeDialog
        )
        if directory:
            self.files = [directory]
            self.source_text.setText(directory)
            self.index = None
            self.scan()

    def choose_target(self):
        directory = QFileDialog.getExistingDirectory(
            self, "选择输出牧场", self.target.text(), QFileDialog.Option.DontUseNativeDialog
        )
        if directory:
            self.target.setText(directory)

    def json_toggled(self, checked):
        if not checked:
            self.json_sources = []
        self.json_button.setVisible(checked)
        self.json_label.setText(
            "仅复制导入外部九轴 / PPG / 温度 JSON；不处理该目录中的视频。"
            if checked
            else "仅转码视频无需选择；牧场中已有的九轴、PPG、温度会保留。"
        )
        self.json_label.setToolTip("")

    def choose_json(self):
        directory = QFileDialog.getExistingDirectory(
            self, "选择外部九轴 / PPG / 温度 JSON 目录", "", QFileDialog.Option.DontUseNativeDialog
        )
        if directory:
            from .dataset_access import overlaps

            if self.target.text().strip() and overlaps(
                Path(directory), Path(self.target.text().strip())
            ):
                self.json_sources = []
                self.import_json.setChecked(False)
                self.status.setText("这是当前牧场已有的数据，无需再次导入；直接开始转码即可。")
                return
            self.json_sources = [directory]
            self.json_label.setText(directory)
            self.json_label.setToolTip(directory)

    def update_output_hint(self, *_):
        category = CATEGORIES.get(self.category.currentData(), "类别")
        if self.category.currentData() in {"pregnancy_early", "pregnancy_mid", "pregnancy_late"}:
            category = "怀孕 / " + category
        self.output_hint.setText(
            "保存位置：牧场目录 / "
            + category
            + " / Video / 日期 / 视角01–20 / 时间戳.mp4（自动建目录）"
        )

    def mapping_changed(self, row):
        group = self.mapping[row].currentData()
        if not hasattr(self, "table") or self.table.cellWidget(row, 2) is None:
            return
        self.preview_paths.pop(row, None)
        button = self.table.cellWidget(row, 2)
        button.setIcon(QIcon())
        button.setText("尚未预览")
        button.setEnabled(False)
        matched = tasks.automatic_mapping(self.groups).get(group) == tasks.VIEWS[row]
        message = (
            (
                ("同号通道 · " if matched else "已选择来源 · ")
                + str(self.groups.get(group, 0))
                + " 段"
            )
            if group
            else "无录像 / 不接入"
        )
        self.table.setItem(row, 3, QTableWidgetItem(message))

    def scan(self):
        if self.mode.currentIndex() == 1:
            disk = self.disk_choice.currentData()
            if not disk:
                self.status.setText("请刷新并选择录像机原盘")
                return
            request = dict(mode="disk", disk=disk)
        else:
            if not self.files:
                self.status.setText("请先选择原始录像")
                return
            request = dict(mode="files", files=self.files)
        self.job = tasks.task_root() / uuid.uuid4().hex
        self.index = None
        self.settings.setValue("dahua/last_job", str(self.job))
        self.start("scan", request, self.job)

    def selected_mapping(self):
        result = {}
        for view, combo in zip(tasks.VIEWS, self.mapping):
            group = combo.currentData()
            if not group:
                continue
            if group in result:
                raise ValueError("同一来源不能重复分配到不同视角")
            result[group] = view
        if not result:
            raise ValueError("没有可自动匹配的通道；请为无通道号的来源选择一个视角")
        return result

    def previews(self):
        try:
            self.start("previews", dict(groups=list(self.selected_mapping())), self.job)
        except ValueError as exc:
            self.status.setText(str(exc))

    def organize(self):
        try:
            if not self.index:
                raise ValueError("请先扫描原始录像")
            if not self.target.text().strip():
                raise ValueError("请选择输出牧场")
            options = dict(
                target=self.target.text().strip(),
                category=self.category.currentData(),
                scenario=self.scenario.currentData(),
                mapping=self.selected_mapping(),
                start=self.start_time.text().strip(),
                end=self.end_time.text().strip(),
                split_midnight=self.midnight.isChecked(),
                json_sources=self.json_sources if self.import_json.isChecked() else [],
            )
            self.settings.setValue("dahua/target", options["target"])
            self.start("organize", dict(options=options), self.job)
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))

    def restore(self):
        saved = str(self.settings.value("dahua/last_job", "")).strip()
        if not saved or not Path(saved).is_absolute():
            self.status.setText("没有可恢复的视频任务")
            return
        job = Path(saved)
        self.job = job
        self.start("restore", {}, job)

    def apply_restored_options(self, options):
        if not options:
            return
        self.target.setText(options["target"])
        self.category.setCurrentIndex(self.category.findData(options["category"]))
        self.scenario.setCurrentIndex(self.scenario.findData(options.get("scenario", "mixed")))
        self.start_time.setText(str(options.get("start") or ""))
        self.end_time.setText(str(options.get("end") or ""))
        self.midnight.setChecked(options.get("split_midnight", True))
        from .dataset_access import overlaps

        external = [
            p
            for p in options.get("json_sources", [])
            if not overlaps(Path(p), Path(options["target"]))
        ]
        self.import_json.setChecked(bool(external))
        self.json_sources = external
        if external:
            self.json_label.setText("；".join(external))
            self.json_label.setToolTip("；".join(external))
        if options.get("mapping"):
            for combo in self.mapping:
                combo.setCurrentIndex(0)
        for group, view in options.get("mapping", {}).items():
            combo = self.mapping[tasks.VIEWS.index(view)]
            combo.setCurrentIndex(combo.findData(group))

    def apply_disks(self, disks):
        self.disks = disks
        self._disks_loaded = True
        self.disk_choice.clear()
        self.disk_choice.addItem("请选择录像机原盘…", None)
        for disk in disks:
            letters = " / ".join(disk.get("letters", [])) or "无盘符"
            self.disk_choice.addItem(
                f"{letters} · 磁盘 {disk['number']} · {disk['model']} · {disk['size'] / 1e12:.2f} TB"
                + (" · 录像机原盘" if disk.get("dhfs") else " · 非支持的录像机原盘"),
                disk,
            )
            if not disk.get("dhfs"):
                item = self.disk_choice.model().item(self.disk_choice.count() - 1)
                if item:
                    item.setEnabled(False)
        self.status.setText("选择录像机原盘后自动只读扫描；无需打开盘符或格式化。")

    def disk_selected(self, index):
        disk = self.disk_choice.itemData(index)
        if disk and disk.get("dhfs") and not self.running:
            self.scan()

    def start(self, action, request, job=None):
        if self.running:
            return
        self.operation = action
        self.operation_job = (
            Path(job) if job else tasks.task_root() / ("devices-" + uuid.uuid4().hex)
        )
        self.operation_job.mkdir(parents=True, exist_ok=True)
        (self.operation_job / "dahua-cancel").unlink(missing_ok=True)
        atomic_json(
            self.operation_job / "dahua-request.json", dict(action=action, **request), backup=False
        )
        self.buffer = b""
        self.stderr = b""
        self.error = ""
        self.result_path = None
        self.active = True
        self.status.setText("后台读取与核对中…")
        self.bar.setRange(0, 0)
        self.refresh()
        if self.process:
            self.process.deleteLater()
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(lambda: self.read_error())
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        program = Path(sys.executable)
        if os.name == "nt" and program.with_name("pythonw.exe").exists():
            program = program.with_name("pythonw.exe")
        self.process.start(
            str(program),
            ["-I", "-B", str(Path(__file__).with_name("dahua_worker.py")), str(self.operation_job)],
        )

    def read_error(self):
        self.stderr = (self.stderr + bytes(self.process.readAllStandardError()))[-12000:]

    def read_output(self):
        self.buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                value = json.loads(line)
                event = value.get("event")
                if event == "progress":
                    total = value["total"]
                    self.bar.setRange(0, total if total else 0)
                    self.bar.setValue(value["current"])
                    self.status.setText(value["message"])
                elif event == "error":
                    self.error = value["error"]
                    self.status.setText(
                        ("已暂停：" if value.get("paused") else "处理失败：") + self.error
                    )
                elif event == "result":
                    self.result_path = value["path"]
                elif event == "preview":
                    self.apply_preview(value["row"])
                elif event == "row":
                    self.status.setText(value["row"].get("message", ""))
            except (ValueError, KeyError, TypeError):
                self.error = "任务输出无法解析，请查看视频任务记录"

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.error = "视频准备进程未能启动"
            self.active = False
            self.status.setText(self.error)
            self.refresh()

    def finished(self, *_):
        self.read_output()
        self.read_error()
        self.active = False
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        try:
            if self.result_path:
                result = tasks.read_json(self.result_path)
                if self.operation == "disks":
                    self.apply_disks(result["disks"])
                elif self.operation in {"scan", "restore"}:
                    self.apply_index(result)
                    if self.operation == "restore":
                        self.apply_restored_options(result.get("options", {}))
                elif self.operation == "organize":
                    self.output = result.get("output", "")
                    self.status.setText(
                        (
                            "归类完成"
                            if result.get("status") == "completed"
                            else "所选范围没有可输出录像"
                        )
                        + f"；待核对 {len(result.get('issues', []))} 项。原始录像保留。"
                    )
                elif self.operation == "previews":
                    self.status.setText("缩略图已就绪；同号通道已对应同号视角，可直接开始转码。")
            elif not self.error:
                self.status.setText("任务未完成：" + self.stderr.decode("utf-8", "replace")[-1500:])
        except (OSError, ValueError, KeyError) as exc:
            self.status.setText("读取任务结果失败：" + str(exc))
        self.refresh()

    def apply_index(self, index):
        if "rows" in index:
            index = tasks.index_summary(index)
        self.index = index
        self.groups = index["groups"]
        model = QStandardItemModel(self)
        empty = QStandardItem("不接入此视角")
        empty.setData("", Qt.ItemDataRole.UserRole)
        model.appendRow(empty)
        for group in sorted(self.groups, key=lambda g: (tasks.group_channel(g) or 999, g)):
            number = tasks.group_channel(group)
            title = f"Channel {number:02}" if group.startswith("channel:") and number else group
            item = QStandardItem(title + " · " + str(self.groups[group]) + " 段")
            item.setData(group, Qt.ItemDataRole.UserRole)
            model.appendRow(item)
        automatic = {view: group for group, view in tasks.automatic_mapping(self.groups).items()}
        previous = getattr(self, "_mapping_model", None)
        self._mapping_model = model
        for i, combo in enumerate(self.mapping):
            combo.blockSignals(True)
            combo.setModel(model)
            combo.setCurrentIndex(combo.findData(automatic.get(tasks.VIEWS[i], "")))
            combo.blockSignals(False)
            self.mapping_changed(i)
        if previous:
            previous.deleteLater()
        self.status.setText(
            f"扫描到 {index['total']} 段，已自动匹配 {len(automatic)} 个视角；异常索引 {index['invalid']} 段。"
            + ("可直接开始转码并归类。" if automatic else "未识别到明确通道号，请选择对应视角。")
        )

    def apply_preview(self, row):
        for i, combo in enumerate(self.mapping):
            if combo.currentData() != row["group"]:
                continue
            preview = row.get("preview", {})
            label = self.table.cellWidget(i, 2)
            if preview.get("image"):
                self.preview_paths[i] = preview["image"]
                label.setIcon(QIcon(preview["image"]))
                label.setIconSize(QSize(48, 27))
                label.setText("查看大图")
                label.setEnabled(True)
            self.table.setItem(
                i,
                3,
                QTableWidgetItem(
                    row.get("error")
                    or "原码流通道 " + str(preview.get("channels", "未知")) + "；待核对视角"
                ),
            )

    def pause(self):
        if self.running:
            (self.operation_job / "dahua-cancel").touch()
            self.status.setText("正在安全暂停，已完成的 MP4 保留用于继续…")

    def reset_idle_form(self):
        if self.running:
            return
        self.job = self.index = None
        self.groups = {}
        self.files = []
        self.json_sources = []
        self.source_text.clear()
        self.target.clear()
        self.start_time.clear()
        self.end_time.clear()
        self.import_json.setChecked(False)
        self.json_toggled(False)
        self.preview_paths = {}
        self.output = ""
        self.error = ""
        self.buffer = b""
        self.disk_choice.setCurrentIndex(-1)
        self.category.setCurrentIndex(self.category.findData("calving"))
        self.scenario.setCurrentIndex(0)
        self.midnight.setChecked(True)
        for row, combo in enumerate(self.mapping):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("不接入此视角", "")
            combo.blockSignals(False)
            self.table.item(row, 3).setText("尚未扫描")
            preview = self.table.cellWidget(row, 2)
            preview.setIcon(QIcon())
            preview.setText("尚未预览")
            preview.setEnabled(False)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.status.setText("请选择原始录像来源并扫描。")
        self.run_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.refresh()

    def request_shutdown(self):
        if self.running:
            self.pause()
        return not self.running

    def refresh(self):
        if not hasattr(self, "controls"):
            return
        for widget in self.controls:
            widget.setEnabled(not self.running)
        self.preview_button.setEnabled(not self.running and bool(self.index))
        self.run_button.setEnabled(not self.running and bool(self.index))
        self.pause_button.setEnabled(self.running)
        self.report_button.setEnabled(bool(self.job))

    def open_output(self):
        destination = getattr(self, "output", "") or self.target.text()
        if Path(destination).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(destination).resolve())))

    def open_report(self):
        if self.job and self.job.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.job)))
