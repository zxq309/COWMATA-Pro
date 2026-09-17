"""Explicit manual, automatic and scheduled downloads with Ledger 1.3.1 CSV mirror."""

import queue
import threading
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDateTime, QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
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
            if self.operation == "login":
                client = self.client_factory(values, self.cancel, self.message.emit)
                try:
                    report["session"] = client.login(values.get("ledger_username", ""), values.get("ledger_password", ""))
                    self.status.emit("ledger", "当前 Pro 账号授权有效，可只读刷新 CSV")
                finally:
                    self.values.pop("ledger_password", None)
            elif self.operation == "probe":
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

                        plan = CsvPlan(values["ledger_directory"])
                        if not plan.by_device:
                            raise ValueError("请先刷新 CSV，才能用台账设备检测数据接口")
                        from datetime import timezone

                        end = datetime.now(CHINA)
                        device = next(iter(plan.by_device))
                        Client(values["server"], self.cancel).envelope(
                            "/device/data/count",
                            {
                                "device": device,
                                "startTime": (end - timedelta(minutes=1))
                                .astimezone(timezone.utc)
                                .strftime("%Y-%m-%dT%H:%M:%S"),
                                "endTime": end.astimezone(timezone.utc).strftime(
                                    "%Y-%m-%dT%H:%M:%S"
                                ),
                            },
                        )
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
                        raise ValueError(
                            "本轮台账刷新失败，未开始下载；下轮重新核对：" + str(exc)
                        ) from exc
                if self.operation == "all":
                    root = Path(values["data_root"])
                    root.mkdir(parents=True, exist_ok=True)
                    job = Job(
                        values["server"],
                        root,
                        "未分类",
                        (),
                        ("motion", "pulse", "temp"),
                        datetime.fromisoformat(values["start_time"]),
                        datetime.fromisoformat(values["end_time"])
                        if values.get("end_time")
                        else datetime.now(CHINA).replace(microsecond=0),
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


class PlanWorker(QObject):
    completed = Signal(object, str)
    finished = Signal()

    def __init__(self, folder, parent):
        super().__init__(parent)
        self.folder = folder
        self.cancel = threading.Event()
        self.mail = queue.Queue(maxsize=1)
        self.thread = None
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.poll)

    def start(self):
        def read():
            try:
                result = (CsvPlan(self.folder), "")
            except (OSError, ValueError, TypeError) as exc:
                result = (None, str(exc))
            self.mail.put(result)

        self.thread = threading.Thread(target=read, daemon=True, name="ledger-preview")
        self.thread.start()
        self.timer.start()

    def poll(self):
        try:
            result = self.mail.get_nowait()
        except queue.Empty:
            return
        self.timer.stop()
        if not self.cancel.is_set():
            self.completed.emit(*result)
        self.finished.emit()

    def isRunning(self):
        return bool(self.thread and self.thread.is_alive())

    def wait(self, milliseconds=3000):
        if self.thread:
            self.thread.join(milliseconds / 1000)
        return not self.isRunning()


class DownloadPlanTable(QTableWidget):
    """Keep headers readable at every window width and scroll long CSV plans."""

    def __init__(self):
        super().__init__(0, 8)
        self.setHorizontalHeaderLabels(
            [
                "下载状态",
                "完整设备编号",
                "牛耳标 / 标号",
                "佩戴开始",
                "佩戴结束",
                "类别",
                "核对说明",
                "CSV 行",
            ]
        )
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.setMinimumHeight(150)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

    def fit_columns(self):
        header = self.horizontalHeader()
        metrics = header.fontMetrics()
        widths = [
            max(metrics.horizontalAdvance(self.horizontalHeaderItem(col).text()) + 32,
                self.sizeHintForColumn(col) + 20)
            for col in range(self.columnCount())
        ]
        # Share spare width; never squeeze text to avoid horizontal scrolling.
        extra = max(0, self.viewport().width() - sum(widths))
        for col in range(len(widths)):
            widths[col] += extra // len(widths)
        widths[-1] += extra % len(widths)
        for col, width in enumerate(widths):
            self.setColumnWidth(col, width)
        self.verticalHeader().setDefaultSectionSize(max(36, self.fontMetrics().height() + 20))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_columns()


class ProDownloadDialog(TaskWindow):
    def __init__(
        self, parent=None, store=None, worker_factory=SyncWorker, launch_automatically=False
    ):
        super().__init__(parent)
        self.store = store or ProSettings()
        self.worker_factory = worker_factory
        self.worker = self.manual_dialog = None
        self.plan_worker = None
        self.plan_reload = False
        self.plan_records = []
        self.csv_issues = []
        self.log_pending = deque(maxlen=1000)
        self.log_timer = QTimer(self)
        self.log_timer.setInterval(150)
        self.log_timer.timeout.connect(self.flush_log)
        self.scheduling_stopped = False
        self.armed = False
        self.session = {}
        self.setWindowTitle("端侧数据下载")
        self.resize(1060, 720)
        outer = QVBoxLayout(self)
        outer.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel("端侧数据下载")
        font = title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        title.setFont(font)
        heading.addWidget(title, 1)
        self.config_button = QPushButton("配置下载…")
        self.config_button.clicked.connect(self.open_configuration)
        heading.addWidget(self.config_button)
        self.more_button = QPushButton("更多")
        self.more_menu = QMenu(self.more_button)
        self.more_button.setMenu(self.more_menu)
        heading.addWidget(self.more_button)
        outer.addLayout(heading)
        self.quick_fields = QWidget()
        quick = QHBoxLayout(self.quick_fields)
        quick.setContentsMargins(0, 0, 0, 0)
        self.mode = QComboBox()
        for label, value in [
            ("请选择下载方式…", ""),
            ("手动：点击后下载一轮", "manual"),
            ("自动：启动后按间隔补齐", "automatic"),
            ("定时：指定时间下载一轮", "scheduled"),
        ]:
            self.mode.addItem(label, value)
        self.mode.setCurrentIndex(
            max(0, self.mode.findData(self.store.value.get("download_mode", "")))
        )
        quick.addWidget(QLabel("下载方式"))
        quick.addWidget(self.mode, 1)
        modalities = QHBoxLayout()
        self.kind_checks = {}
        for key, label in [("motion", "九轴"), ("pulse", "PPG"), ("temp", "温度")]:
            check = QCheckBox(label)
            check.setChecked(True)
            check.setEnabled(False)
            check.setToolTip("样本通过核对后统一下载三类数据，包括 PPG")
            self.kind_checks[key] = check
            modalities.addWidget(check)
        quick.addLayout(modalities)
        outer.addWidget(self.quick_fields)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        outer.addWidget(self.summary)
        rules = QLabel(
            "每轮核对三份台账：五项已填写，且九轴、温度有效才下载。"
            "非产犊的“/”算已填写；产犊须有效起止时间。"
        )
        rules.setWordWrap(True)
        outer.addWidget(rules)
        self.config_dialog = QDialog(self)
        self.config_dialog.setWindowTitle("配置下载")
        self.config_dialog.resize(780, 610)
        config_layout = QVBoxLayout(self.config_dialog)
        self.fields = QTabWidget()
        config_layout.addWidget(self.fields, 1)

        def add_page(title):
            page = QWidget()
            form = QFormLayout(page)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.fields.addTab(scroll, title)
            return form

        form = add_page("下载规则")
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
        self.until_now = QCheckBox("截至每轮启动时（取消后指定结束时间）")
        self.until_now.setChecked(not self.store.value.get("end_time"))
        self.end_at = QDateTimeEdit()
        self.end_at.setCalendarPopup(True)
        self.end_at.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        china_now = QDateTime.fromString(
            datetime.now(CHINA).strftime("%Y-%m-%d %H:%M:%S"), "yyyy-MM-dd HH:mm:ss"
        )
        self.end_at.setDateTime(china_now)
        if self.store.value.get("end_time"):
            end_text = (
                datetime.fromisoformat(self.store.value["end_time"])
                .astimezone(CHINA)
                .strftime("%Y-%m-%d %H:%M:%S")
            )
            self.end_at.setDateTime(QDateTime.fromString(end_text, "yyyy-MM-dd HH:mm:ss"))
        self.end_at.setEnabled(not self.until_now.isChecked())
        self.until_now.toggled.connect(lambda checked: self.end_at.setEnabled(not checked))
        form.addRow("补齐终点（北京时间）", self.until_now)
        form.addRow(self.end_at)
        self.interval = QSpinBox()
        self.interval.setRange(1, 1440)
        self.interval.setSuffix(" 分钟")
        self.interval.setValue(self.store.value["interval_seconds"] // 60)
        form.addRow("自动检查间隔", self.interval)
        self.scheduled_at = QDateTimeEdit()
        self.scheduled_at.setCalendarPopup(True)
        self.scheduled_at.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.scheduled_at.setDateTime(china_now.addSecs(3600))
        if self.store.value.get("scheduled_time"):
            text = (
                datetime.fromisoformat(self.store.value["scheduled_time"])
                .astimezone(CHINA)
                .strftime("%Y-%m-%d %H:%M:%S")
            )
            self.scheduled_at.setDateTime(QDateTime.fromString(text, "yyyy-MM-dd HH:mm:ss"))
        form.addRow("定时启动（北京时间）", self.scheduled_at)
        form = add_page("现场记录与账号")
        self.sync_ledger = QCheckBox("每轮先从服务器刷新三个 CSV")
        self.sync_ledger.setChecked(self.store.value["sync_ledger"])
        form.addRow(self.sync_ledger)
        self.ledger_username = QLineEdit()
        self.ledger_password = QLineEdit()
        self.ledger_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.ledger_username.hide()
        self.ledger_password.hide()
        self.login_button = QPushButton('验证当前 Pro 账号授权')
        self.login_button.clicked.connect(lambda: self.start_task('login'))
        form.addRow(self.login_button)
        login_hint = QLabel('直接使用当前 Pro 登录会话只读刷新服务器 CSV，无需再次输入上传器密码；使用本地 CSV 时可取消每轮刷新。')
        login_hint.setWordWrap(True)
        form.addRow(login_hint)
        self.ledger_status = QLabel("现场记录：尚未检测")
        self.ledger_status.setWordWrap(True)
        form.addRow(self.ledger_status)
        form = add_page("高级连接")
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
        form.addRow(self.connection_fields)
        self.config_message = QLabel("已有配置已带入。保存配置后，在主窗口点击开始下载。")
        self.config_message.setWordWrap(True)
        config_layout.addWidget(self.config_message)
        self.config_save = QPushButton("保存并读取 CSV")
        self.config_save.clicked.connect(self.apply_configuration)
        config_layout.addWidget(self.config_save)
        self.config_dialog.rejected.connect(self.restore_configuration)
        self._config_snapshot = None
        self.plan_label = QLabel()
        self.plan_label.setWordWrap(True)
        plan_heading = QHBoxLayout()
        plan_heading.addWidget(self.plan_label, 1)
        self.plan_filter = QComboBox()
        for label, value in [
            ("全部样本", ""),
            ("可下载", "eligible"),
            ("待补全 / 核对", "pending"),
            ("不下载", "excluded"),
        ]:
            self.plan_filter.addItem(label, value)
        self.plan_filter.currentIndexChanged.connect(self.render_plan)
        plan_heading.addWidget(self.plan_filter)
        outer.addLayout(plan_heading)
        self.plan_table = DownloadPlanTable()
        outer.addWidget(self.plan_table, 1)
        self.status = QLabel("就绪")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumHeight(8)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        outer.addWidget(self.progress_bar)
        controls = QHBoxLayout()
        hint = QLabel("仅补齐缺失数据，已有文件保持原位。")
        hint.setWordWrap(True)
        controls.addWidget(hint, 1)
        self.stop_button = QPushButton("停止 / 取消定时")
        self.stop_button.clicked.connect(self.pause)
        controls.addWidget(self.stop_button)
        self.start_button = QPushButton("开始下载")
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self.start_selected)
        controls.addWidget(self.start_button)
        outer.addLayout(controls)
        self.log_dialog = QDialog(self)
        self.log_dialog.setWindowTitle("运行记录")
        self.log_dialog.resize(820, 500)
        log_layout = QVBoxLayout(self.log_dialog)
        self.motion_status = QLabel("原始数据：尚未检测")
        self.motion_status.setWordWrap(True)
        log_layout.addWidget(self.motion_status)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        log_layout.addWidget(self.log, 1)
        footer = QLabel(
            "已启动的任务在关闭此窗口后继续；退出 Pro 会停止。重新打开程序需再次启动。旧文件保持原位，仅补齐缺失数据。"
        )
        footer.setWordWrap(True)
        log_layout.addWidget(footer)
        close_log = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_log.rejected.connect(self.log_dialog.hide)
        log_layout.addWidget(close_log)
        self.more_menu.addAction("运行记录…", self.log_dialog.show)
        self.more_menu.addAction("查看 CSV 核对清单…", self.show_csv_issues)
        self.more_menu.addSeparator()
        self.more_menu.addAction("重新读取本地 CSV", self.refresh_plan)
        self.ledger_button = self.more_menu.addAction(
            "仅刷新三个 CSV", lambda: self.start_task("ledger")
        )
        self.probe_button = self.more_menu.addAction(
            "检测服务器连接", lambda: self.start_task("probe")
        )
        self.more_menu.addSeparator()
        for label, field in (
            ("打开数据目录", self.directory),
            ("打开现场记录目录", self.ledger_directory),
        ):
            self.more_menu.addAction(
                label,
                lambda checked=False, edit=field: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(edit.text())
                ),
            )
        self.more_menu.addSeparator()
        # Every visible download mode uses the same sample eligibility rules.
        self.refresh_plan()
        self.update_summary()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.timer_fired)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.mode_changed()
        if self.store.notice:
            self.append(self.store.notice)
        # launch_automatically is retained for callers, but never authorizes a transfer.

    def configuration_widgets(self):
        return [*self.fields.findChildren(QLineEdit), *self.fields.findChildren(QComboBox),
                *self.fields.findChildren(QSpinBox), *self.fields.findChildren(QDateTimeEdit),
                *self.fields.findChildren(QCheckBox)]

    def open_configuration(self):
        # Snapshot only editable rules, never login secrets or transient session state.
        self._config_snapshot = []
        for widget in self.configuration_widgets():
            if widget in (self.ledger_username, self.ledger_password):
                continue
            if isinstance(widget, QLineEdit):
                if isinstance(widget.parent(), (QSpinBox, QDateTimeEdit, QComboBox)):
                    continue
                value = widget.text()
            elif isinstance(widget, QComboBox):
                value = widget.currentIndex()
            elif isinstance(widget, QSpinBox):
                value = widget.value()
            elif isinstance(widget, QDateTimeEdit):
                value = widget.dateTime()
            else:
                value = widget.isChecked()
            self._config_snapshot.append((widget, value))
        self.config_dialog.open()

    def restore_configuration(self):
        for widget, value in self._config_snapshot or []:
            if isinstance(widget, QLineEdit):
                widget.setText(value)
            elif isinstance(widget, QComboBox):
                widget.setCurrentIndex(value)
            elif isinstance(widget, QSpinBox):
                widget.setValue(value)
            elif isinstance(widget, QDateTimeEdit):
                widget.setDateTime(value)
            else:
                widget.setChecked(value)
        self._config_snapshot = None
        self.ledger_password.clear()
        self.refresh_plan()
        self.update_summary()

    def apply_configuration(self):
        if self.save_settings():
            self.stop_scheduling()
            self.refresh_plan()
            self.update_summary()
            self._config_snapshot = None
            self.config_dialog.accept()
            self.status.setText("配置已保存，点击开始下载。")
        else:
            self.config_message.setText(self.status.text())

    def update_summary(self):
        end = "至今" if self.until_now.isChecked() else self.end_at.dateTime().toString("yyyy-MM-dd HH:mm")
        period = self.start_at.dateTime().toString("yyyy-MM-dd HH:mm") + " — " + end
        if self.mode.currentData() == "automatic":
            period += " · " + self.interval.text()
        elif self.mode.currentData() == "scheduled":
            period += " · " + self.scheduled_at.dateTime().toString("yyyy-MM-dd HH:mm")
        self.summary.setText(self.directory.text() + "\n" + period)
        self.summary.setToolTip(self.ledger_directory.text())

    @property
    def running(self):
        return any(worker is not self.plan_worker for worker in self.workers())

    def workers(self):
        result = [self.worker] if self.worker is not None else []
        if self.plan_worker is not None:
            result.append(self.plan_worker)
        if self.manual_dialog and self.manual_dialog.worker is not None:
            result.append(self.manual_dialog.worker)
        return result

    def append(self, message):
        self.log_pending.append(f"[{datetime.now(CHINA):%H:%M:%S}] {message}")
        if not self.log_timer.isActive():
            self.log_timer.start()

    def flush_log(self):
        if self.log_pending:
            self.log.appendPlainText("\n".join(self.log_pending))
            self.log_pending.clear()
        self.log_timer.stop()

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
            if not self.until_now.isChecked() and self._field_time(self.end_at) <= start:
                raise ValueError("补齐终点必须晚于起点")
            self.store.save(
                kinds=["motion", "pulse", "temp"],
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
                auto_enabled=False,
                download_mode=self.mode.currentData(),
                scheduled_time=self._field_time(self.scheduled_at).isoformat(),
                end_time=""
                if self.until_now.isChecked()
                else self._field_time(self.end_at).isoformat(),
                raw_connection=self.raw_mode.currentData(),
                raw_user=self.raw_user.text().strip(),
                raw_key=self.raw_key.text().strip(),
                raw_remote_port=self.raw_port.value(),
            )
            return True
        except (ValueError, OSError, KeyError) as exc:
            self.status.setText("设置未保存：" + str(exc))
            return False

    @staticmethod
    def _field_time(field):
        return datetime.strptime(field.dateTime().toString("yyyy-MM-dd HH:mm:ss"),
                                 "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA)

    def mode_changed(self):
        self.stop_scheduling()
        self.interval.setEnabled(self.mode.currentData() == 'automatic')
        self.scheduled_at.setEnabled(self.mode.currentData() == 'scheduled')
        self.status.setText('请选择规则后点击启动；当前未启动下载')
        self.start_button.setText({'automatic': '启用自动下载', 'scheduled': '设定定时下载'}.get(self.mode.currentData(), '开始下载'))
        self.update_summary()

    def start_selected(self):
        if self.running:
            return
        mode = self.mode.currentData()
        if not mode:
            self.status.setText('请先选择手动、自动或定时下载')
            return
        if not self.save_settings():
            return
        self.stop_scheduling()
        self.scheduling_stopped = False
        if mode == 'scheduled':
            delay = (self._field_time(self.scheduled_at) - datetime.now(CHINA)).total_seconds()
            if delay <= 0:
                self.status.setText('定时启动时间必须晚于当前时间')
                return
            self.armed = True
            self._schedule()
        else:
            self.armed = mode == 'automatic'
            self.start_task('all')

    def _schedule(self):
        mode = self.store.value.get('download_mode')
        if mode == 'scheduled':
            due = datetime.fromisoformat(self.store.value['scheduled_time'])
            delay = max(1, int((due - datetime.now(CHINA)).total_seconds() * 1000))
            self.status.setText('已启用定时下载：' + due.strftime('%Y-%m-%d %H:%M:%S') + '（北京时间）')
        else:
            delay = self.store.value['interval_seconds'] * 1000
            self.append('自动下载已启用，下一轮将在 ' + str(delay // 60000) + ' 分钟后检查。')
        self.timer.start(min(delay, 2147483647))

    def timer_fired(self):
        if not self.armed or self.scheduling_stopped:
            return
        if self.running:
            self.timer.start(1000)
            return
        if self.store.value.get('download_mode') == 'scheduled':
            if datetime.fromisoformat(self.store.value['scheduled_time']) > datetime.now(CHINA):
                self._schedule()
                return
            self.armed = False
        self.start_task('all', from_timer=True)

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
        values = dict(self.store.value)
        if self.session:
            values['session_token'] = self.session['token']
        if operation == 'login':
            values.update(ledger_username=self.ledger_username.text().strip(),
                          ledger_password=self.ledger_password.text())
            self.ledger_password.clear()
        self.worker = self.worker_factory(values, operation, self)
        self.worker.message.connect(self.append)
        self.worker.status.connect(self.connection_status)
        self.worker.progress.connect(self.progress_changed)
        self.worker.completed.connect(self.cycle_completed)
        self.worker.finished.connect(self.task_finished)
        self.fields.setEnabled(False)
        self.quick_fields.setEnabled(False)
        self.config_save.setEnabled(False)
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
        if report.get("session"):
            self.session = report["session"]
        self.refresh_plan()
        if report["canceled"]:
            text = "已停止，可稍后继续"
        elif report["errors"]:
            text = "本轮部分任务未完成：" + "；".join(report["errors"])
        else:
            text = "本轮完成"
        self.status.setText(text)
        self.config_message.setText(text)
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
        self.quick_fields.setEnabled(True)
        self.config_save.setEnabled(True)
        for b in (self.start_button, self.ledger_button, self.probe_button):
            b.setEnabled(True)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        if self.armed and not self.scheduling_stopped:
            self._schedule()

    def stop_scheduling(self):
        self.armed = False
        self.scheduling_stopped = True
        self.timer.stop()

    def stop_task(self):
        self.stop_scheduling()
        for worker in self.workers():
            worker.cancel.set()

    def pause(self):
        self.stop_task()
        self.scheduling_stopped = False
        self.status.setText("正在停止当前请求…" if self.running else "自动同步已暂停")

    def refresh_plan(self):
        if self.plan_worker is not None:
            self.plan_reload = True
            return
        self.plan_label.setText("正在后台核对三份 CSV…")
        self.plan_worker = PlanWorker(self.ledger_directory.text(), self)
        self.plan_worker.completed.connect(self.receive_plan)
        self.plan_worker.finished.connect(self.plan_finished)
        self.plan_worker.start()

    def plan_finished(self):
        worker, self.plan_worker = self.plan_worker, None
        if worker:
            worker.deleteLater()
        if self.plan_reload:
            self.plan_reload = False
            self.refresh_plan()

    def receive_plan(self, plan, error):
        if error:
            self.plan_records = []
            self.plan_table.setRowCount(0)
            self.plan_label.setText("CSV 尚未读取：" + error)
            self.csv_issues = []
            return
        from .csv_targets import FILES

        self.plan_records = [r for r in plan.preview() if r["source"] == FILES[0]]
        counts = Counter(r["eligibility"] for r in self.plan_records)
        self.plan_label.setText(
            f"样本 {len(self.plan_records)} · 可下载 {counts['eligible']} · "
            f"待补全 / 核对 {counts['pending']} · 不下载 {counts['excluded']}"
        )
        self.csv_issues = plan.issues + [
            dict(source=r["source"], row=r["row"], message=r["reason"])
            for r in self.plan_records
            if r["eligibility"] != "eligible"
        ]
        self.render_plan()

    def render_plan(self):
        selected = self.plan_filter.currentData()
        records = [r for r in self.plan_records if not selected or r["eligibility"] == selected]
        self.plan_table.setUpdatesEnabled(False)
        try:
            self.plan_table.setRowCount(len(records))
            labels = {"eligible": "可下载", "pending": "待补全 / 核对", "excluded": "不下载"}
            for row, record in enumerate(records):
                fields = [
                    labels[record["eligibility"]],
                    record["device"],
                    "-".join(v for v in (record["cow"], record["mark"]) if v),
                    record["start"][:16].replace("T", " "),
                    record["end"][:16].replace("T", " ") or "截至本轮",
                    record["category"],
                    record["reason"],
                    str(record["row"]),
                ]
                for col, value in enumerate(fields):
                    item = QTableWidgetItem(value)
                    item.setToolTip(record["reason"] + "\n" + record["warnings"])
                    self.plan_table.setItem(row, col, item)
            self.plan_table.fit_columns()
        finally:
            self.plan_table.setUpdatesEnabled(True)

    def show_csv_issues(self):
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.information(
            self,
            "CSV 核对清单",
            "\n".join(f"{x['source']} 第 {x['row']} 行：{x['message']}" for x in self.csv_issues)
            or "当前 CSV 没有无法解析的记录。",
        )

    def open_manual(self):
        if self.manual_dialog is None:
            from .dialog import DownloadDialog

            self.manual_dialog = DownloadDialog(self)
        self.manual_dialog.show()
        self.manual_dialog.raise_()

    def show(self):
        super().show()

    def reject(self):
        self.hide()

    def closeEvent(self, event):
        event.accept()
