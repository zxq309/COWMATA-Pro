"""Data-preparation download window: automatic motion plus Ledger 1.1.0 CSV mirror."""

import threading
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDateTime, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cowmata_tailring.ui.task_window import TaskWindow

from .connection import ensure_connection
from .core import CHINA, Cancelled, Job
from .csv_download import run_csv_job
from .csv_targets import CsvPlan
from .pro_settings import ProSettings
from .raw_connection import raw_connection
from .site_records import SCHEMAS, LedgerClient, refresh_records


class SyncWorker(QThread):
    message = Signal(str)
    status = Signal(str, str)
    progress = Signal(int, int)
    completed = Signal(object)

    def __init__(
        self,
        values,
        operation="all",
        parent=None,
        *,
        connector=ensure_connection,
        runner=run_csv_job,
        refresher=refresh_records,
        client_factory=LedgerClient,
    ):
        super().__init__(parent)
        self.values, self.operation = dict(values), operation
        self.cancel = threading.Event()
        self.connector, self.runner, self.refresher = connector, runner, refresher
        self.client_factory = client_factory

    def run(self):
        report = dict(errors=[], motion=None, ledger=None, canceled=False)
        try:
            values = self.values
            if self.operation == "probe":
                try:
                    client = self.client_factory(values, self.cancel, self.message.emit)
                    for sheet in SCHEMAS:
                        client.pull(sheet)
                    self.status.emit("ledger", "连接正常，三个 CSV 的内容和校验均通过")
                except Cancelled:
                    raise
                except Exception as exc:
                    report["errors"].append(str(exc))
                    self.status.emit("ledger", str(exc))
                try:
                    with raw_connection(values, self.cancel, self.message.emit, self.connector):
                        from .core import Client

                        plan = CsvPlan(values['ledger_directory'])
                        if not plan.by_device:
                            raise ValueError('请先刷新 CSV，才能用台账设备检测数据接口')
                        from datetime import timezone
                        end = datetime.now(CHINA)
                        device = next(iter(plan.by_device))
                        Client(values['server'], self.cancel).envelope('/device/data/count', {
                            'device': device,
                            'startTime': (end-timedelta(minutes=1)).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),
                            'endTime': end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')})
                    self.status.emit("motion", "连接正常")
                except Cancelled:
                    raise
                except Exception as exc:
                    report["errors"].append(str(exc))
                    self.status.emit("motion", str(exc))
            else:
                if values["sync_ledger"] or self.operation == "ledger":
                    try:
                        report["ledger"] = self.refresher(values, self.cancel, self.message.emit)
                        self.status.emit(
                            "ledger",
                            "核验完成，更新 " + str(report["ledger"]["changed"]) + " 个 CSV",
                        )
                    except Cancelled:
                        raise
                    except Exception as exc:
                        report["errors"].append("台账：" + str(exc))
                        self.status.emit("ledger", str(exc))
                        self.message.emit("台账本轮未更新，保留现有 CSV：" + str(exc))
                if self.operation == "all":
                    root = Path(values["data_root"])
                    root.mkdir(parents=True, exist_ok=True)
                    job = Job(
                        values["server"],
                        root,
                        "未分类",
                        (),
                        tuple(values.get("kinds", ["motion", "pulse", "temp"])),
                        datetime.fromisoformat(values["start_time"]),
                        datetime.now(CHINA).replace(microsecond=0),
                        Path(values["ledger_directory"]),
                    )
                    try:
                        with raw_connection(values, self.cancel, self.message.emit, self.connector):
                            result = self.runner(
                                job, self.cancel, self.message.emit, self.progress.emit
                            )
                        report["motion"] = result
                        self.status.emit(
                            "motion",
                            f"保存 {result.saved}，已存在 {result.skipped}，待补齐 {result.pending}，失败 {result.failed}",
                        )
                        if result.failed:
                            report["errors"].append("原始数据有失败批次，请查看日志")
                        report["canceled"] = result.canceled
                    except Cancelled:
                        raise
                    except Exception as exc:
                        report["errors"].append("原始数据：" + str(exc))
                        self.status.emit("motion", str(exc))
        except Cancelled:
            report["canceled"] = True
        except Exception as exc:
            report["errors"].append(str(exc))
            self.message.emit(str(exc))
        self.completed.emit(report)


class ProDownloadDialog(TaskWindow):
    def __init__(
        self, parent=None, store=None, worker_factory=SyncWorker, launch_automatically=True
    ):
        super().__init__(parent)
        self.store = store or ProSettings()
        self.worker_factory = worker_factory
        self.worker = self.manual_dialog = None
        self.scheduling_stopped = False
        self.setWindowTitle("端侧数据下载 · CSV 自动配置")
        self.resize(980, 820)
        outer = QVBoxLayout(self)
        intro = QLabel("从现场 CSV 自动读取设备、耳标、现场标号和佩戴时段，下载九轴、PPG 与温度。")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        self.fields = QWidget()
        form = QFormLayout(self.fields)
        self.directory = QLineEdit(self.store.value["data_root"])
        self.ledger_directory = QLineEdit(self.store.value["ledger_directory"])
        for label, field in (
            ("数据保存位置", self.directory),
            ("本地现场记录位置", self.ledger_directory),
        ):
            row = QHBoxLayout()
            row.addWidget(field, 1)
            button = QPushButton("选择…")
            row.addWidget(button)
            button.clicked.connect(lambda checked=False, edit=field: self.choose_folder(edit))
            form.addRow(label, row)
        self.path_hint = QLabel(
            "现场记录固定更新：样本试验台账.csv、扬大产犊登记汇总.csv、扬大测试设备台账.csv"
        )
        self.path_hint.setWordWrap(True)
        form.addRow(self.path_hint)
        modalities = QHBoxLayout()
        self.kind_checks = {}
        for key, label in [('motion','九轴'),('pulse','PPG'),('temp','温度')]:
            check = QCheckBox(label)
            check.setChecked(key in self.store.value.get('kinds', ['motion','pulse','temp']))
            self.kind_checks[key] = check
            modalities.addWidget(check)
        form.addRow('下载数据类型', modalities)
        self.plan_label = QLabel()
        self.plan_label.setWordWrap(True)
        form.addRow(self.plan_label)
        self.plan_table = QTableWidget(0, 7)
        self.plan_table.setHorizontalHeaderLabels(['完整设备编号','牛耳标','现场标号','佩戴开始','佩戴结束','类别','CSV 来源行'])
        self.plan_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.plan_table.setMinimumHeight(190)
        form.addRow('CSV 自动配置的下载对象', self.plan_table)
        refresh_plan = QPushButton('重新读取本地 CSV')
        refresh_plan.clicked.connect(self.refresh_plan)
        form.addRow(refresh_plan)
        self.refresh_plan()
        self.start_at = QDateTimeEdit()
        self.start_at.setCalendarPopup(True)
        self.start_at.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.start_at.setDateTime(
            QDateTime.fromString(
                datetime.fromisoformat(self.store.value["start_time"])
                .astimezone(CHINA)
                .strftime("%Y-%m-%d %H:%M:%S"),
                "yyyy-MM-dd HH:mm:ss",
            )
        )
        form.addRow("补齐起点（北京时间）", self.start_at)
        self.interval = QSpinBox()
        self.interval.setRange(1, 1440)
        self.interval.setSuffix(" 分钟")
        self.interval.setValue(self.store.value["interval_seconds"] // 60)
        form.addRow("自动检查间隔", self.interval)
        self.sync_ledger = QCheckBox("每轮先从服务器刷新三个 CSV")
        self.sync_ledger.setChecked(self.store.value["sync_ledger"])
        form.addRow(self.sync_ledger)
        self.connection_toggle = QPushButton("展开服务器连接设置 ▾")
        self.connection_toggle.setCheckable(True)
        form.addRow(self.connection_toggle)
        group = QGroupBox("服务器连接")
        group_layout = QVBoxLayout(group)
        self.connection_fields = QWidget()
        connection_form = QFormLayout(self.connection_fields)
        self.server = QLineEdit(self.store.value["server"])
        connection_form.addRow("数据下载接口", self.server)
        self.raw_mode = QComboBox()
        self.raw_mode.addItem("按设备访问数据服务器（九轴 / PPG / 温度）", "http")
        self.raw_mode.addItem("复用独立下载器的已授权通道", "authorized_task")
        self.raw_mode.addItem("真实网卡直连 SSH（与上传器相同连接方式）", "direct_ssh")
        self.raw_mode.setCurrentIndex(
            max(0, self.raw_mode.findData(self.store.value["raw_connection"]))
        )
        connection_form.addRow("原始数据连接方式", self.raw_mode)
        self.raw_fields = QWidget()
        raw_form = QFormLayout(self.raw_fields)
        self.raw_user = QLineEdit(self.store.value["raw_user"])
        self.raw_key = QLineEdit(self.store.value["raw_key"])
        self.raw_port = QSpinBox()
        self.raw_port.setRange(1, 65535)
        self.raw_port.setValue(self.store.value["raw_remote_port"])
        raw_form.addRow("九轴 SSH 用户", self.raw_user)
        key_row2 = QHBoxLayout()
        key_row2.addWidget(self.raw_key)
        choose_raw = QPushButton("选择九轴授权…")
        choose_raw.clicked.connect(self.choose_raw_key)
        key_row2.addWidget(choose_raw)
        raw_form.addRow("九轴 SSH 私钥", key_row2)
        raw_form.addRow("远端九轴接口端口", self.raw_port)
        connection_form.addRow(self.raw_fields)
        self.raw_fields.setVisible(self.raw_mode.currentData() == "direct_ssh")
        self.raw_mode.currentIndexChanged.connect(
            lambda: self.raw_fields.setVisible(self.raw_mode.currentData() == "direct_ssh")
        )
        note = QLabel(
            "默认按 CSV 设备访问数据服务器。也可选择复用独立下载器已授权的本机通道，或使用单独授权的 SSH。台账授权用于读取现场记录。"
        )
        note.setWordWrap(True)
        connection_form.addRow(note)
        self.ledger_host = QLineEdit(self.store.value["ledger_host"])
        self.ledger_port = QSpinBox()
        self.ledger_port.setRange(1, 65535)
        self.ledger_port.setValue(self.store.value["ledger_port"])
        host_row = QHBoxLayout()
        host_row.addWidget(self.ledger_host)
        host_row.addWidget(self.ledger_port)
        connection_form.addRow("台账 SSH 服务器 / 端口", host_row)
        self.ledger_user = QLineEdit(self.store.value["ledger_user"])
        connection_form.addRow("台账 SSH 用户", self.ledger_user)
        self.remote_directory = QLineEdit(self.store.value["ledger_server_directory"])
        connection_form.addRow("服务器现场记录位置", self.remote_directory)
        self.key = QLineEdit(self.store.value["ledger_key"])
        key_row = QHBoxLayout()
        key_row.addWidget(self.key)
        key_button = QPushButton("选择已有授权…")
        key_button.clicked.connect(self.choose_key)
        key_row.addWidget(key_button)
        connection_form.addRow("上传器已有授权文件", key_row)
        group_layout.addWidget(self.connection_fields)
        group.hide()
        self.connection_toggle.toggled.connect(group.setVisible)
        self.connection_toggle.toggled.connect(
            lambda opened: self.connection_toggle.setText("收起服务器连接设置 ▴" if opened else "展开服务器连接设置 ▾")
        )
        form.addRow(group)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.fields)
        scroll.setMinimumHeight(280)
        outer.addWidget(scroll, 2)
        options = QHBoxLayout()
        self.auto = QCheckBox("在 Pro 运行期间自动同步")
        self.auto.setChecked(self.store.value["auto_enabled"])
        options.addWidget(self.auto)
        options.addStretch()
        save = QPushButton("保存设置")
        save.clicked.connect(self.save_settings)
        options.addWidget(save)
        outer.addLayout(options)
        controls = QHBoxLayout()
        self.start_button = QPushButton("一键下载 / 立即同步")
        self.ledger_button = QPushButton("仅刷新三个 CSV")
        self.probe_button = QPushButton("检测服务器连接")
        self.stop_button = QPushButton("停止并暂停自动")
        for button in (self.start_button, self.ledger_button, self.probe_button, self.stop_button):
            controls.addWidget(button)
        outer.addLayout(controls)
        self.start_button.clicked.connect(lambda: self.start_task("all"))
        self.ledger_button.clicked.connect(lambda: self.start_task("ledger"))
        self.probe_button.clicked.connect(lambda: self.start_task("probe"))
        self.stop_button.clicked.connect(self.pause)
        self.motion_status, self.ledger_status, self.status = (
            QLabel("原始数据：尚未检测"),
            QLabel("现场记录：尚未检测"),
            QLabel("就绪"),
        )
        for label in (self.motion_status, self.ledger_status, self.status):
            label.setWordWrap(True)
            outer.addWidget(label)
        self.progress_bar = QProgressBar()
        outer.addWidget(self.progress_bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        outer.addWidget(self.log, 1)
        links = QHBoxLayout()
        for label, field in (
            ("打开数据目录", self.directory),
            ("打开现场记录目录", self.ledger_directory),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda checked=False, edit=field: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(edit.text())
                )
            )
            links.addWidget(button)
        manual = QPushButton("查看 CSV 核对清单…")
        manual.clicked.connect(self.show_csv_issues)
        links.addWidget(manual)
        outer.addLayout(links)
        footer = QLabel(
            "关闭此窗口可继续同步；退出 Pro 会停止。台账修订会重新核对数据分类，原始 JSON 保持不变。"
        )
        footer.setWordWrap(True)
        outer.addWidget(footer)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.start_task("all", from_timer=True))
        self.auto.toggled.connect(self.auto_changed)
        if self.store.notice:
            self.append(self.store.notice)
        if launch_automatically and self.auto.isChecked():
            self.timer.start(500)

    @property
    def running(self):
        return bool(self.workers())

    def workers(self):
        result = [self.worker] if self.worker is not None else []
        if self.manual_dialog and self.manual_dialog.worker is not None:
            result.append(self.manual_dialog.worker)
        return result

    def append(self, message):
        self.log.appendPlainText(f"[{datetime.now(CHINA):%H:%M:%S}] {message}")

    def choose_folder(self, field):
        selected = QFileDialog.getExistingDirectory(self, "选择保存位置", field.text())
        if selected:
            field.setText(selected)

    def choose_key(self):
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择上传器已授权的私钥文件", self.key.text()
        )
        if selected:
            self.key.setText(selected)

    def choose_raw_key(self):
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择已授权的九轴 SSH 私钥", self.raw_key.text()
        )
        if selected:
            self.raw_key.setText(selected)

    def save_settings(self):
        try:
            if self.running:
                raise ValueError("请先停止当前任务再修改设置")
            start = datetime.strptime(
                self.start_at.dateTime().toString("yyyy-MM-dd HH:mm:ss"), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=CHINA)
            if start >= datetime.now(CHINA):
                raise ValueError("补齐起点应早于当前时间")
            if not any(check.isChecked() for check in self.kind_checks.values()):
                raise ValueError("请至少选择一种数据类型")
            self.store.save(
                kinds=[key for key, check in self.kind_checks.items() if check.isChecked()],
                server=self.server.text().strip().rstrip("/"),
                data_root=self.directory.text().strip(),
                ledger_directory=self.ledger_directory.text().strip(),
                start_time=start.isoformat(),
                interval_seconds=self.interval.value() * 60,
                sync_ledger=self.sync_ledger.isChecked(),
                ledger_host=self.ledger_host.text().strip(),
                ledger_port=self.ledger_port.value(),
                ledger_user=self.ledger_user.text().strip(),
                ledger_server_directory=self.remote_directory.text().strip(),
                ledger_key=self.key.text().strip(),
                auto_enabled=self.auto.isChecked(),
                raw_connection=self.raw_mode.currentData(),
                raw_user=self.raw_user.text().strip(),
                raw_key=self.raw_key.text().strip(),
                raw_remote_port=self.raw_port.value(),
            )
            return True
        except (ValueError, OSError, KeyError) as exc:
            self.status.setText("设置未保存：" + str(exc))
            return False

    def start_task(self, operation="all", from_timer=False):
        if self.running:
            if from_timer and not self.scheduling_stopped:
                self.timer.start(1000)
            return
        if from_timer and self.scheduling_stopped:
            return
        self.scheduling_stopped = False
        if not self.save_settings():
            return
        self.timer.stop()
        self.worker = self.worker_factory(self.store.value, operation, self)
        self.worker.message.connect(self.append)
        self.worker.status.connect(self.connection_status)
        self.worker.progress.connect(self.progress_changed)
        self.worker.completed.connect(self.cycle_completed)
        self.worker.finished.connect(self.task_finished)
        self.fields.setEnabled(False)
        for b in (self.start_button, self.ledger_button, self.probe_button):
            b.setEnabled(False)
        self.status.setText("正在同步…" if operation != "probe" else "正在分别检测台账和数据连接…")
        self.progress_bar.setRange(0, 0)
        self.worker.start()

    def connection_status(self, kind, message):
        (self.ledger_status if kind == "ledger" else self.motion_status).setText(
            ("现场记录：" if kind == "ledger" else "原始数据：") + message
        )
        self.append(message)

    def progress_changed(self, done, total):
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(done)

    def cycle_completed(self, report):
        self.refresh_plan()
        if report["canceled"]:
            text = "已停止，可稍后继续"
        elif report["errors"]:
            text = "本轮部分任务未完成：" + "；".join(report["errors"])
        else:
            text = "本轮完成"
        self.status.setText(text)
        self.append(text)
        try:
            result = report["motion"]
            self.store.save(
                last_cycle=dict(
                    finished_at=datetime.now(CHINA).isoformat(),
                    saved=result.saved if result else 0,
                    skipped=result.skipped if result else 0,
                    failed=len(report["errors"]),
                    error="；".join(report["errors"]),
                    canceled=report["canceled"],
                    ledger=report["ledger"],
                ),
                next_run=None,
            )
        except (OSError, ValueError) as exc:
            self.append("运行状态未保存：" + str(exc))

    def task_finished(self):
        worker, self.worker = self.worker, None
        if worker:
            worker.deleteLater()
        self.fields.setEnabled(True)
        for b in (self.start_button, self.ledger_button, self.probe_button):
            b.setEnabled(True)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        if self.auto.isChecked() and not self.scheduling_stopped:
            self.timer.start(self.store.value["interval_seconds"] * 1000)
            self.append(
                "自动同步开启，下次检查将在 "
                + str(self.store.value["interval_seconds"] // 60)
                + " 分钟后进行。"
            )

    def auto_changed(self, checked):
        try:
            self.store.save(auto_enabled=checked)
        except (OSError, ValueError) as exc:
            self.append("自动同步设置未保存：" + str(exc))
        if checked:
            self.scheduling_stopped = False
            if not self.running:
                self.timer.start(100)
        else:
            self.stop_task()
            self.scheduling_stopped = False

    def stop_scheduling(self):
        self.scheduling_stopped = True
        self.timer.stop()

    def stop_task(self):
        self.stop_scheduling()
        for worker in self.workers():
            worker.cancel.set()

    def pause(self):
        self.auto.setChecked(False)
        self.stop_task()
        self.scheduling_stopped = False
        self.status.setText("正在停止当前请求…" if self.running else "自动同步已暂停")

    def refresh_plan(self):
        try:
            plan = CsvPlan(self.ledger_directory.text())
            records = plan.preview()
            self.plan_table.setRowCount(len(records))
            for row, record in enumerate(records):
                fields = [record['device'], record['cow'], record['mark'], record['start'][:16].replace('T',' '),
                          record['end'][:16].replace('T',' ') or '截至本轮', record['category'],
                          record['source'] + ':' + str(record['row'])]
                for col, value in enumerate(fields):
                    item = QTableWidgetItem(value)
                    item.setToolTip(record['folder'] + '\n' + record['warnings'])
                    self.plan_table.setItem(row, col, item)
            self.plan_table.resizeColumnsToContents()
            self.plan_label.setText(f'已读取 {len(plan.by_device)} 台设备 / {len(records)} 段佩戴记录；{len(plan.issues)} 行待核对。现场标号可为空。')
            self.csv_issues = plan.issues
        except (OSError, ValueError) as exc:
            self.plan_label.setText('CSV 尚未读取：' + str(exc))
            self.csv_issues = []

    def show_csv_issues(self):
        from PySide6.QtWidgets import QMessageBox
        self.refresh_plan()
        QMessageBox.information(self, 'CSV 核对清单', '\n'.join(
            f"{x['source']} 第 {x['row']} 行：{x['message']}" for x in self.csv_issues) or '当前 CSV 没有无法解析的记录。')

    def open_manual(self):
        if self.manual_dialog is None:
            from .dialog import DownloadDialog

            self.manual_dialog = DownloadDialog(self)
        self.manual_dialog.show()
        self.manual_dialog.raise_()

    def show(self):
        self.scheduling_stopped = False
        super().show()

    def reject(self):
        self.hide()

    def closeEvent(self, event):
        event.accept()
