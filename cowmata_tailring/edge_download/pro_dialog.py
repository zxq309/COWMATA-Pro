"""4.4.1 端侧数据: one button. Click = download continuously from 样本试验台账; click again = pause.

Each round refreshes the three ledger CSVs, moves 待产犊 wearings that now have an outcome to 产犊 /
孕晚期, downloads every authorised record up to two minutes ago (complete files only, written
atomically) and starts the next round a minute later. Pausing cancels the round, deletes unfinished
temporary files and stops the decider; clicking again resumes where the data ends.
"""

import queue
import threading
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDateTime, QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
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
from .download_notes import NotesStore, notes_path
from .download_status import STATES, day_cutoff, record_day
from .pro_settings import ProSettings
from .raw_connection import raw_connection
from .site_records import SCHEMAS, LedgerClient, refresh_records


class SyncWorker(QThread):
    message = Signal(str)
    status = Signal(str, str)
    progress = Signal(int, int)
    completed = Signal(object)
    ledger_refreshed = Signal(object)

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
                    report["ledger_attempted"] = True
                    try:
                        report["ledger"] = self.refresher(values, self.cancel, self.message.emit)
                        self.ledger_refreshed.emit(report["ledger"])
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
                    extra = {}
                    if values.get("realtime"):
                        extra["realtime"] = True
                        try:
                            from .pending_reconcile import reconcile_pending

                            reconcile_pending(root, values["ledger_directory"], self.message.emit)
                        except (OSError, ValueError) as exc:
                            self.message.emit("待产犊核对未完成：" + str(exc))
                    if values.get("only_records") is not None:
                        extra["only"] = values["only_records"]
                    if values.get("force_download"):
                        extra["force"] = True
                    try:
                        with raw_connection(values, self.cancel, self.message.emit, self.connector):
                            result = self.runner(
                                job, self.cancel, self.message.emit, self.progress.emit, **extra
                            )
                        report["motion"] = result
                        self.status.emit(
                            "motion",
                            f"保存 {result.saved}，已存在 {result.skipped}，"
                            f"已完成跳过 {getattr(result, 'settled', 0)} 个设备日，"
                            f"待补齐 {result.pending}，失败 {result.failed}",
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

    def __init__(self, folder, parent, data_root=None, reconcile=False):
        super().__init__(parent)
        self.folder = folder
        self.data_root = data_root
        self.reconcile = reconcile
        self.cancel = threading.Event()
        self.mail = queue.Queue(maxsize=1)
        self.thread = None
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.poll)

    def start(self):
        def read():
            try:
                plan = CsvPlan(self.folder)
                plan.reconciled = {}
                if self.reconcile and self.data_root:
                    # Opening the downloader first re-files 待产犊 wearings the ledger has closed.
                    from .pending_reconcile import reconcile_pending

                    plan.reconciled = reconcile_pending(self.data_root, self.folder, plan=plan)
                result = (plan, "")
            except (OSError, ValueError, TypeError) as exc:
                result = (None, str(exc))
            else:
                # Local state is read off the GUI thread: scanning the data
                # folders of a few hundred records must never freeze the window.
                try:
                    from .csv_targets import FILES
                    from .download_status import local_status

                    records = [r for r in plan.preview() if r["source"] == FILES[0]]
                    plan.local_status = local_status(records, self.data_root) if self.data_root else {}
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    plan.local_status = {}
                    plan.local_status_error = str(exc)
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
        super().__init__(0, 6)
        self.setHorizontalHeaderLabels(["状态", "牛号", "设备号", "分类", "佩戴", "文件"])
        self.verticalHeader().hide()
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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


class LedgerReportWindow(QDialog):
    """Non-modal, copyable ledger problems and persisted outcome notes.

    Opening this window never blocks a running download, and every cell can
    be selected and copied so the list can be sent to the field staff.
    """

    ISSUE_HEADERS = ["表名", "CSV 行", "牛号", "设备号原值", "字段", "原始内容", "原因", "建议"]
    NOTE_HEADERS = ["来源表", "CSV 行", "牛号", "设备号", "原文", "采用的下载范围", "状态", "说明"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("台账问题与备注记录")
        self.setModal(False)
        self.resize(1100, 520)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.issue_table = self._table(self.ISSUE_HEADERS)
        self.note_table = self._table(self.NOTE_HEADERS)
        self.tabs.addTab(self.issue_table, "问题清单（格式 / 身份错误）")
        self.tabs.addTab(self.note_table, "备注记录（结局等，已落盘）")
        layout.addWidget(self.tabs, 1)
        buttons = QHBoxLayout()
        hint = QLabel("问题记录只跳过本身，不影响其他正常记录下载；内容可复制后发给现场核对。")
        hint.setWordWrap(True)
        buttons.addWidget(hint, 1)
        copy_button = QPushButton("复制当前页")
        copy_button.clicked.connect(self.copy_page)
        buttons.addWidget(copy_button)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.hide)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    @staticmethod
    def _table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    @staticmethod
    def _fill(table, rows):
        table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                table.setItem(r, c, item)

    def set_issues(self, issues):
        self._fill(self.issue_table, [
            [x.get("source", ""), x.get("row", ""), x.get("cow", ""),
             x.get("device", ""), x.get("field", ""), x.get("raw", ""),
             x.get("message", ""), x.get("suggestion", "")]
            for x in issues
        ])

    def set_notes(self, notes):
        self._fill(self.note_table, [
            [n.get("source", ""), n.get("row", ""), n.get("cow", ""),
             n.get("device", "") or "（未关联设备）", n.get("text", ""), self._range(n),
             "当前台账" if n.get("current", True) else "历史（台账已刷新）", n.get("message", "")]
            for n in notes
        ])

    @staticmethod
    def _range(note):
        start, end = note.get("download_start", ""), note.get("download_end", "")
        if not start:
            return ""
        return (start[:16].replace("T", " ") + " — "
                + (end[:16].replace("T", " ") if end else "未记录结束"))

    def copy_page(self):
        table = self.tabs.currentWidget()
        lines = ["\t".join(table.horizontalHeaderItem(c).text() for c in range(table.columnCount()))]
        for r in range(table.rowCount()):
            lines.append("\t".join(
                table.item(r, c).text() if table.item(r, c) else ""
                for c in range(table.columnCount())
            ))
        QApplication.clipboard().setText("\n".join(lines))


class RoundNoticeDialog(QDialog):
    """One non-modal reminder when a download round finishes."""

    retry_requested = Signal()

    def __init__(self, parent, lines, retry=False):
        super().__init__(parent)
        self.setWindowTitle("下载完成提醒")
        self.setModal(False)
        layout = QVBoxLayout(self)
        for line in lines:
            label = QLabel(line)
            label.setWordWrap(True)
            layout.addWidget(label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        if retry:
            retry_button = QPushButton("重试下载失败的数据")
            retry_button.clicked.connect(self._retry)
            buttons.addWidget(retry_button)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.hide)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def _retry(self):
        self.retry_requested.emit()
        self.hide()


class ProDownloadDialog(TaskWindow):
    def __init__(
        self, parent=None, store=None, worker_factory=SyncWorker, launch_automatically=False,
        refresh_on_open=False,
    ):
        super().__init__(parent)
        self.store = store or ProSettings()
        self.worker_factory = worker_factory
        self.worker = self.manual_dialog = None
        self.plan_worker = None
        self.plan_reload = False
        self.plan_records = []
        self.csv_issues = []
        self.csv_notes = []
        self.report_window = None
        self._round_notice = None
        self._round_notice_signature = None
        self.row_status = {}
        self._row_positions = {}
        self._problem_notice_signature = None
        self.log_pending = deque(maxlen=1000)
        self.log_timer = QTimer(self)
        self.log_timer.setInterval(150)
        self.log_timer.timeout.connect(self.flush_log)
        self.scheduling_stopped = False
        self.armed = False
        self.session = {}
        self._refresh_on_open = refresh_on_open
        self._first_show = True
        self.setWindowTitle("端侧数据")
        self.resize(1060, 720)
        outer = QVBoxLayout(self)
        outer.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel("端侧数据")
        title.setObjectName("heading")
        heading.addWidget(title)
        self.plan_label = QLabel()
        self.plan_label.setObjectName("muted")
        heading.addWidget(self.plan_label, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索牛号 / 设备号")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(220)
        self.search.textChanged.connect(self.render_plan)
        heading.addWidget(self.search)
        self.more_button = QPushButton("更多")
        self.more_menu = QMenu(self.more_button)
        self.more_button.setMenu(self.more_menu)
        heading.addWidget(self.more_button)
        # One button: 下载 starts continuous download from 样本试验台账, 暂停 stops it.
        self.download_button = QPushButton("下载")
        self.download_button.setObjectName("primary")
        self.download_button.setProperty("mainAction", True)
        self.download_button.setCheckable(True)
        self.download_button.setMinimumWidth(112)
        self.download_button.clicked.connect(self.toggle_download)
        heading.addWidget(self.download_button)
        outer.addLayout(heading)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.rules_text = (
            "按样本试验台账下载：五项已填写、九轴与温度有效的记录，下载九轴、PPG、温度，"
            "按台账分类保存（产犊 / 待产犊 / 孕晚期 …）。每轮先刷新台账，并把台账已补产犊时间的待产犊移到产犊或孕晚期。"
        )
        self.rules = QLabel(self.rules_text)
        self.rules.setWordWrap(True)
        self.csv_receipt = QLabel()
        self.csv_receipt.setTextFormat(Qt.TextFormat.PlainText)
        self.csv_receipt.setWordWrap(True)
        self.csv_receipt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.csv_receipt.hide()
        self.config_dialog = QDialog(self)
        self.config_dialog.setWindowTitle("下载设置")
        self.config_dialog.resize(760, 560)
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

        form = add_page("保存位置")
        self.directory = QLineEdit(self.store.display_path(self.store.value["data_root"]))
        self.ledger_directory = QLineEdit(self.store.display_path(self.store.value["ledger_directory"]))
        for label, field in (
            ("数据（1_下载器\\牧场）", self.directory),
            ("台账", self.ledger_directory),
        ):
            row = QHBoxLayout()
            row.addWidget(field, 1)
            button = QPushButton("选择…")
            row.addWidget(button)
            button.clicked.connect(lambda checked=False, edit=field: self.choose_folder(edit))
            form.addRow(label, row)
        self.path_hint = QLabel("默认：数据 1_下载器\\扬大_高邮牧场，台账 1_下载器\\扬大_高邮牧场\\台账。")
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
        self.interval = QSpinBox()
        self.interval.setRange(1, 120)
        self.interval.setSuffix(" 分钟")
        self.interval.setValue(max(1, min(120, int(self.store.value.get("interval_seconds", 60)) // 60)))
        form.addRow("每轮间隔", self.interval)
        form = add_page("台账与账号")
        self.sync_ledger = QCheckBox("每轮先从服务器刷新三个 CSV")
        self.sync_ledger.setChecked(self.store.value["sync_ledger"])
        form.addRow(self.sync_ledger)
        self.ledger_username = QLineEdit()
        self.ledger_password = QLineEdit()
        self.ledger_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.ledger_username.hide()
        self.ledger_password.hide()
        self.login_button = QPushButton("验证当前账号")
        self.login_button.clicked.connect(lambda: self.start_task("login"))
        form.addRow(self.login_button)
        self.ledger_status = QLabel("台账：尚未检测")
        self.ledger_status.setWordWrap(True)
        form.addRow(self.ledger_status)
        form = add_page("连接")
        self.connection_fields = QWidget()
        connection_form = QFormLayout(self.connection_fields)
        self.server = QLineEdit(self.store.value["server"])
        connection_form.addRow("数据接口", self.server)
        self.raw_mode = QComboBox()
        self.raw_mode.addItem("按设备访问数据服务器", "http")
        self.raw_mode.addItem("复用已授权的本机通道", "authorized_task")
        self.raw_mode.addItem("SSH 直连", "direct_ssh")
        self.raw_mode.setCurrentIndex(
            max(0, self.raw_mode.findData(self.store.value["raw_connection"]))
        )
        connection_form.addRow("原始数据连接", self.raw_mode)
        self.raw_fields = QWidget()
        raw_form = QFormLayout(self.raw_fields)
        self.raw_user = QLineEdit(self.store.value["raw_user"])
        self.raw_key = QLineEdit(self.store.value["raw_key"])
        self.raw_port = QSpinBox()
        self.raw_port.setRange(1, 65535)
        self.raw_port.setValue(self.store.value["raw_remote_port"])
        raw_form.addRow("SSH 用户", self.raw_user)
        key_row2 = QHBoxLayout()
        key_row2.addWidget(self.raw_key)
        choose_raw = QPushButton("选择…")
        choose_raw.clicked.connect(self.choose_raw_key)
        key_row2.addWidget(choose_raw)
        raw_form.addRow("SSH 私钥", key_row2)
        raw_form.addRow("远端端口", self.raw_port)
        connection_form.addRow(self.raw_fields)
        self.raw_fields.setVisible(self.raw_mode.currentData() == "direct_ssh")
        self.raw_mode.currentIndexChanged.connect(
            lambda: self.raw_fields.setVisible(self.raw_mode.currentData() == "direct_ssh")
        )
        self.ledger_host = QLineEdit(self.store.value["ledger_host"])
        self.ledger_port = QSpinBox()
        self.ledger_port.setRange(1, 65535)
        self.ledger_port.setValue(self.store.value["ledger_port"])
        host_row = QHBoxLayout()
        host_row.addWidget(self.ledger_host)
        host_row.addWidget(self.ledger_port)
        connection_form.addRow("台账服务器 / 端口", host_row)
        self.ledger_user = QLineEdit(self.store.value["ledger_user"])
        connection_form.addRow("台账 SSH 用户", self.ledger_user)
        self.remote_directory = QLineEdit(self.store.value["ledger_server_directory"])
        connection_form.addRow("服务器台账位置", self.remote_directory)
        self.key = QLineEdit(self.store.value["ledger_key"])
        key_row = QHBoxLayout()
        key_row.addWidget(self.key)
        key_button = QPushButton("选择…")
        key_button.clicked.connect(self.choose_key)
        key_row.addWidget(key_button)
        connection_form.addRow("台账授权文件", key_row)
        form.addRow(self.connection_fields)
        self.config_message = QLabel("")
        self.config_message.setWordWrap(True)
        config_layout.addWidget(self.config_message)
        self.config_save = QPushButton("保存")
        self.config_save.setObjectName("primary")
        self.config_save.clicked.connect(self.apply_configuration)
        config_layout.addWidget(self.config_save)
        self.config_dialog.rejected.connect(self.restore_configuration)
        self._config_snapshot = None
        self.local_state = {}
        self.plan_table = DownloadPlanTable()
        self.plan_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.plan_table.customContextMenuRequested.connect(self._table_menu)
        outer.addWidget(self.plan_table, 1)
        self.status = QLabel("就绪")
        self.status.setWordWrap(False)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        outer.addWidget(self.progress_bar)
        self.info_line = QLabel()
        self.info_line.setWordWrap(False)
        self.info_line.setObjectName("muted")
        outer.addWidget(self.info_line)
        self.log_dialog = QDialog(self)
        self.log_dialog.setWindowTitle("运行记录")
        self.log_dialog.resize(820, 500)
        log_layout = QVBoxLayout(self.log_dialog)
        for widget in (self.summary, self.rules, self.csv_receipt):
            log_layout.addWidget(widget)
        self.motion_status = QLabel("原始数据：尚未检测")
        self.motion_status.setWordWrap(True)
        log_layout.addWidget(self.motion_status)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        log_layout.addWidget(self.log, 1)
        close_log = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_log.rejected.connect(self.log_dialog.hide)
        log_layout.addWidget(close_log)
        self.more_menu.addAction("设置…", self.open_configuration)
        self.more_menu.addAction("运行记录…", self.log_dialog.show)
        self.more_menu.addAction("问题清单…", self.show_csv_issues)
        self.more_menu.addAction("备注记录…", self.show_notes_window)
        self.more_menu.addSeparator()
        self.ledger_button = self.more_menu.addAction("只刷新台账", lambda: self.start_task("ledger"))
        self.probe_button = self.more_menu.addAction("检测连接", lambda: self.start_task("probe"))
        self.redownload_button = self.more_menu.addAction("重新下载所选…", self.redownload_selected)
        self.more_menu.addSeparator()
        folders_menu = self.more_menu.addMenu("打开目录")
        for label, field in (("数据", self.directory), ("台账", self.ledger_directory)):
            folders_menu.addAction(
                label,
                lambda checked=False, edit=field: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(self.store.resolve_path(edit.text())))
                ),
            )
        self.more_menu.addAction("风险等级总览", self.open_decision_app)
        self._download_days = set()
        self.refresh_plan(reconcile=True)
        self.update_summary()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.timer_fired)
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
            self.refresh_plan()
            self.update_summary()
            self._config_snapshot = None
            self.config_dialog.accept()
            self.status.setText("设置已保存")
        else:
            self.config_message.setText(self.status.text())

    def update_summary(self):
        self.summary.setText(self.directory.text() + "\n自 " + self.start_at.dateTime().toString("yyyy-MM-dd HH:mm")
                             + " 起，下载到当前 · 每轮间隔 " + self.interval.text())
        self.summary.setToolTip(self.ledger_directory.text())
        self.refresh_info_line()

    def refresh_info_line(self):
        """One quiet line at the bottom; details stay in the tooltip and 运行记录."""
        receipt = self.csv_receipt.text().splitlines() if not self.csv_receipt.isHidden() else []
        state = "下载中 · 实时" if self.armed else "已暂停"
        parts = [state, self.directory.text()]
        if receipt:
            parts.append(receipt[0])
        self.info_line.setText(" · ".join(parts))
        self.info_line.setToolTip("\n".join([self.summary.text(), self.rules_text, *receipt, "详细记录：更多 → 运行记录"]))

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
        selected = QFileDialog.getExistingDirectory(self, "选择保存位置", str(self.store.resolve_path(field.text())))
        if selected:
            field.setText(self.store.display_path(selected))

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
                raise ValueError("请先暂停下载再修改设置")
            start = datetime.strptime(
                self.start_at.dateTime().toString("yyyy-MM-dd HH:mm:ss"), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=CHINA)
            if start >= datetime.now(CHINA):
                raise ValueError("补齐起点应早于当前时间")
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
                download_mode="automatic",
                scheduled_time="",
                end_time="",
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

    def toggle_download(self):
        if self.armed:
            self.pause_download()
        else:
            self.start_download()

    def start_download(self):
        """下载: continuous rounds from 样本试验台账 until 暂停; the decider follows the new data."""
        self._sync_button(True)
        if self.armed and self.running:
            return
        if not self.save_settings():
            self._sync_button(False)
            return
        self.armed = True
        self.scheduling_stopped = False
        from .decider import start as start_decider

        start_decider(self.append)
        self.refresh_info_line()
        if self.running:
            return
        self.start_task("all", refresh_ledger=True)

    def pause_download(self):
        """暂停: stop the round, delete unfinished temporary files, stop the decider."""
        self.stop_task()
        self._sync_button(False)
        self._paused_cleanup = True
        if not self.running:
            self._cleanup_partials()
        self.status.setText("正在暂停…" if self.running else "已暂停；再次点击“下载”从断点继续")
        from .decider import stop as stop_decider

        stop_decider(self.append)
        self.refresh_info_line()

    # Menu commands of earlier versions map onto the one-button model.
    def update_and_download(self):
        self.start_download()

    def start_selected(self):
        self.start_download()

    def pause(self):
        self.pause_download()

    def _sync_button(self, on):
        self.download_button.blockSignals(True)
        self.download_button.setChecked(on)
        self.download_button.setText("暂停" if on else "下载")
        self.download_button.blockSignals(False)

    def _cleanup_partials(self):
        """Remove unfinished temporary files of this session (complete JSON files are kept)."""
        self._paused_cleanup = False
        try:
            root = self.store.resolve_path(self.directory.text())
        except (ValueError, OSError):
            return 0
        if not root.is_dir():
            return 0
        removed = 0
        days = set(self._download_days) or {datetime.now(CHINA).strftime("%Y-%m-%d")}
        for folder in [root, root / ".edge-download"]:
            for pattern in (".edge-*.partial", ".edge-*.part", ".edge-auto-*.part"):
                for path in folder.glob(pattern):
                    path.unlink(missing_ok=True)
                    removed += 1
        for category in [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]:
            for base in [category, *[c for c in category.iterdir() if c.is_dir() and c.name.startswith("孕")]]:
                for modality in ("Motion", "PPG", "Temp"):
                    for day in days:
                        folder = base / modality / day
                        if not folder.is_dir():
                            continue
                        for pattern in (".edge-*.partial", ".edge-*.part", ".edge-auto-*.part"):
                            for path in folder.rglob(pattern):
                                path.unlink(missing_ok=True)
                                removed += 1
        if removed:
            self.append(f"已删除 {removed} 个未完成的临时文件")
        self._download_days.clear()
        return removed

    def _schedule(self):
        delay = max(60, int(self.store.value.get("interval_seconds", 60))) * 1000
        self.status.setText(f"本轮完成，{delay // 60000} 分钟后继续下载")
        self.timer.start(min(delay, 2147483647))

    def timer_fired(self):
        if not self.armed or self.scheduling_stopped:
            return
        if self.running:
            self.timer.start(1000)
            return
        self.start_task("all", from_timer=True, refresh_ledger=True)

    def open_decision_app(self):
        from .decider import decision_app

        app = decision_app()
        if app is None:
            self.status.setText("还没有风险等级总览：下载后由正向决策器生成")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(app)))

    def _table_menu(self, position):
        menu = QMenu(self)
        menu.addAction("重新下载所选…", self.redownload_selected)
        menu.exec(self.plan_table.viewport().mapToGlobal(position))

    def start_task(self, operation="all", from_timer=False, refresh_ledger=False, skip_ledger=False,
                   only=None, force=False):
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
        if refresh_ledger:
            values['sync_ledger'] = True
        if skip_ledger:
            # Retrying failed batches reuses the just-verified local CSV copy.
            values['sync_ledger'] = False
        if only is not None:
            # A date or rows picked in the table: download just those records.
            values['only_records'] = [dict(device=r["device"], start=r["start"], end=r["end"]) for r in only]
            values['force_download'] = bool(force)
        if self.session:
            values['session_token'] = self.session['token']
        if operation == "all" and self.armed and only is None:
            values["realtime"] = True
        if operation == 'login':
            values.update(ledger_username=self.ledger_username.text().strip(),
                          ledger_password=self.ledger_password.text())
            self.ledger_password.clear()
        self.worker = self.worker_factory(values, operation, self)
        self.row_status = {}
        self.worker.message.connect(self.append)
        self.worker.message.connect(self.track_download)
        self.worker.status.connect(self.connection_status)
        self.worker.progress.connect(self.progress_changed)
        self.worker.completed.connect(self.cycle_completed)
        self.worker.ledger_refreshed.connect(self.receive_ledger_receipt)
        self.worker.finished.connect(self.task_finished)
        self.fields.setEnabled(False)
        self.config_save.setEnabled(False)
        for b in (self.ledger_button, self.probe_button, self.redownload_button):
            b.setEnabled(False)
        if operation == "probe":
            self.status.setText("正在分别检测台账和数据连接…")
        elif operation == "ledger" or (operation == "all" and values["sync_ledger"]):
            self.status.setText("正在更新台账…")
        elif only is not None:
            self.status.setText(("正在重新下载 " if force else "正在下载 ") + f"所选 {len(only)} 条记录…")
        else:
            self.status.setText("正在下载…")
        if operation == "ledger" or (operation == "all" and values["sync_ledger"]):
            self.csv_receipt.setText("正在从服务器读取并核验三个 CSV…")
            self.csv_receipt.setStyleSheet("")
            self.csv_receipt.show()
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

    def receive_ledger_receipt(self, result):
        checked = datetime.fromtimestamp(result.get("checked_at", datetime.now(CHINA).timestamp()), CHINA)
        lines = ["CSV 刷新完成 · " + checked.strftime("%Y-%m-%d %H:%M:%S") + "（北京时间）"]
        for sheet, schema in SCHEMAS.items():
            diff = result.get("changes", {}).get(sheet)
            count = result.get("counts", {}).get(sheet, 0)
            if diff is None:
                detail = f"已核验，共 {count} 条"
            elif diff["baseline"] == "rebuilt":
                detail = f"已重建 {count} 条；本地旧表格式不兼容，未计变更"
            elif not any(diff[k] for k in ("added", "updated", "removed")):
                detail = f"未发现新增或更新，共 {count} 条"
            else:
                detail = f"新增 {diff['added']} · 更新 {diff['updated']} · 移除 {diff['removed']} · 共 {count} 条"
                if diff["baseline"] == "new":
                    detail = "首次读取；" + detail
            lines.append(schema["filename"] + "：" + detail)
        self.csv_receipt.setText("\n".join(lines))
        self.csv_receipt.setStyleSheet("background: #edf6e7; color: #294622; padding: 6px;")
        self.csv_receipt.show()
        self.refresh_info_line()
        self.append("\n".join(lines))
        # The visible cycle moves to its second phase: raw-data download.
        if getattr(self.worker, "operation", "") == "all":
            self.status.setText("台账更新完成，正在下载…")
        self.refresh_plan()

    def cycle_completed(self, report):
        if report.get("session"):
            self.session = report["session"]
        self.refresh_plan()
        if report.get("ledger_attempted") and report.get("ledger") is None:
            reason = "已停止" if report["canceled"] else "；".join(report["errors"])
            self.csv_receipt.setText("本轮 CSV 刷新未完成：" + reason)
            self.csv_receipt.setStyleSheet("background: #fff0e5; color: #8a321b; padding: 6px;")
            self.csv_receipt.show()
            self.refresh_info_line()
        motion = report.get("motion")
        if report["canceled"]:
            text = "已停止，可稍后继续"
        elif report["errors"]:
            text = "本轮部分任务未完成：" + "；".join(report["errors"])
        elif report.get("ledger_attempted") and motion is None:
            text = "台账已更新完成"
        elif motion is not None:
            text = (f"本轮完成：新增 {motion.saved} · 已存在 {motion.skipped}"
                    f" · 已完成设备日 {getattr(motion, 'settled', 0)} · 失败 {motion.failed}")
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
        if report.get("motion") is not None and not report.get("canceled") and (report["motion"].failed or report["errors"]):
            self._notify_round(report)

    def _notify_round(self, report):
        """One reminder per finished round; identical outcomes stay silent."""
        result = report["motion"]
        errors = [str(e) for e in report["errors"]]
        signature = (result.saved, result.skipped, result.failed, tuple(errors))
        if signature == self._round_notice_signature:
            return
        self._round_notice_signature = signature
        lines = [f"本轮下载结束：新增 {result.saved} · 已存在 {result.skipped} · 失败 {result.failed}"]
        if result.pending:
            lines.append(f"待补齐 {result.pending}（台账补全后下轮自动补）")
        if result.failed or errors:
            lines.append("有失败批次或失败步骤，可点击下方按钮重试：")
            lines += errors[:5]
        notice = RoundNoticeDialog(self, lines, retry=bool(result.failed or errors))
        notice.retry_requested.connect(self.retry_failed)
        self._round_notice = notice
        notice.show()
        notice.raise_()
        notice.activateWindow()

    def retry_failed(self):
        """Run one more download cycle over the same ledger; existing files skip."""
        if self.running:
            return
        self.start_task("all", skip_ledger=True)

    def task_finished(self):
        worker, self.worker = self.worker, None
        if worker:
            worker.deleteLater()
        self.fields.setEnabled(True)
        self.config_save.setEnabled(True)
        for b in (self.ledger_button, self.probe_button, self.redownload_button):
            b.setEnabled(True)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        if getattr(self, "_paused_cleanup", False):
            self._cleanup_partials()
            self.status.setText("已暂停；再次点击“下载”从断点继续")
        if self.armed and not self.scheduling_stopped:
            self._schedule()
        self.refresh_info_line()

    def stop_scheduling(self):
        self.armed = False
        self.scheduling_stopped = True
        self.timer.stop()

    def stop_task(self):
        self.stop_scheduling()
        for worker in self.workers():
            worker.cancel.set()

    def refresh_plan(self, reconcile=False):
        if self.plan_worker is not None:
            self.plan_reload = True
            return
        self.plan_label.setText("正在核对台账…")
        try:
            folder = self.store.resolve_path(self.ledger_directory.text())
        except (ValueError, OSError) as exc:
            self.plan_label.setText("台账目录未就绪：" + str(exc))
            return
        try:
            data_root = str(self.store.resolve_path(self.directory.text()))
        except (ValueError, OSError):
            data_root = None
        self.plan_worker = PlanWorker(str(folder), self, data_root, reconcile=reconcile)
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

        all_sample_records = [r for r in plan.preview() if r["source"] == FILES[0]]
        ignored = sum(r["eligibility"] == "pending" for r in all_sample_records)
        # Rows without enough ledger information are intentionally invisible to
        # the download plan. They are not errors and must never block complete
        # rows from downloading; the raw CSV remains available from 更多 → 打开目录.
        self.plan_records = [r for r in all_sample_records if r["eligibility"] != "pending"]
        self.local_state = dict(getattr(plan, "local_status", {}) or {})
        counts = Counter(r["eligibility"] for r in self.plan_records)
        states = Counter(self._state(r) for r in self.plan_records)
        done = states["downloaded"] + states["current"]
        self.plan_label.setText(
            f"样本 {len(all_sample_records)} · 下载 {counts['eligible']}（已完成 {done} · 待下载 {states['partial'] + states['missing'] + states['today']}）"
            f" · 不下载 {counts['excluded']} · 待补全 {ignored}"
        )
        moved = getattr(plan, "reconciled", None) or {}
        if moved:
            text = "待产犊核对：" + "，".join(f"{n} 个文件归入 {c}" for c, n in moved.items())
            self.append(text)
            self.status.setText(text)
        # Only format/identity errors form the problem list; merely missing
        # values stay hidden as pending rows and never mix into it.
        self.csv_issues = list(plan.issues)
        self.csv_notes = self._persist_notes(plan)
        if plan.issues and self.isVisible():
            signature = tuple((x.get("source"), x.get("row"), x.get("message")) for x in plan.issues)
            if signature != self._problem_notice_signature:
                self._problem_notice_signature = signature
                QTimer.singleShot(0, lambda issues=list(plan.issues): self.show_plan_problems(issues))
        self.render_plan()

    def _persist_notes(self, plan):
        """Archive plan notes beside the download data; reload history too."""
        try:
            data_root = self.store.resolve_path(self.directory.text())
            store = NotesStore(notes_path(data_root)).merge(
                getattr(plan, "notes", []), getattr(plan, "fingerprint", "")
            )
            store.save()
            return store.records
        except (OSError, ValueError) as exc:
            self.append("备注记录未能保存，本轮仅显示：" + str(exc))
            return list(getattr(plan, "notes", []))

    def _state(self, record):
        """Local state of one record; without a scan, eligible rows count as missing."""
        info = self.local_state.get(record["row"])
        if info:
            return info["state"]
        if record["eligibility"] == "excluded":
            return "invalid" if "无效" in str(record.get("reason", "")) else "excluded"
        return "missing" if record["eligibility"] == "eligible" else record["eligibility"]

    def _matches(self, record, selected):
        state = self._state(record)
        if selected == "pending":
            return state in ("missing", "partial")
        if selected == "eligible":
            return record["eligibility"] == "eligible"
        if selected == "excluded":
            return record["eligibility"] == "excluded"
        if selected == "downloaded":
            return state in ("downloaded", "current")
        return not selected or state == selected

    def render_plan(self):
        needle = self.search.text().strip().casefold()
        records = [r for r in self.plan_records
                   if not needle or needle in " ".join((r["device"], r["cow"], r["mark"])).casefold()]

        def order(record):
            try:
                stamp = datetime.fromisoformat(record["start"]).timestamp()
            except (TypeError, ValueError):
                stamp = 0
            # Unfinished records first, newest first; downloaded and excluded after.
            return STATES.get(self._state(record), ("", "", 9))[2], -stamp

        records.sort(key=order)
        self.shown_records = records
        self.plan_table.setUpdatesEnabled(False)
        try:
            self.plan_table.clearContents()
            self.plan_table.setRowCount(len(records))
            self._row_positions = {}
            for row, record in enumerate(records):
                self._row_positions[record["row"]] = row
                state = self._state(record)
                label, colour, _ = STATES.get(state, (state, "", 9))
                label = self.row_status.get(record["row"]) or label
                files = self.local_state.get(record["row"], {}).get("files", "")
                end = record["end"][5:16].replace("T", " ") if record["end"] else "佩戴中"
                fields = [
                    label,
                    "-".join(v for v in (record["cow"], record["mark"]) if v),
                    record["device"],
                    record["category"],
                    record["start"][5:16].replace("T", " ") + " → " + end,
                    "" if record["eligibility"] != "eligible" else str(files),
                ]
                for col, value in enumerate(fields):
                    item = QTableWidgetItem(value)
                    item.setToolTip(record["reason"] + ("\n" + record["warnings"] if record["warnings"] else ""))
                    if col == 0 and colour:
                        item.setBackground(QColor(colour))
                    self.plan_table.setItem(row, col, item)
            self.plan_table.fit_columns()
        finally:
            self.plan_table.setUpdatesEnabled(True)

    def _selected_records(self):
        rows = sorted({index.row() for index in self.plan_table.selectionModel().selectedRows()})
        shown = getattr(self, "shown_records", [])
        return [shown[row] for row in rows if row < len(shown)]

    def redownload_selected(self):
        """Point-to-point re-download of selected rows found problematic while labelling."""
        if self.running:
            return
        records = [r for r in self._selected_records() if r["eligibility"] == "eligible"]
        if not records:
            self.status.setText("请先在表格中选择要重新下载的记录（可用上方搜索框按设备号 / 牛号查找）")
            return
        devices = sorted({r["device"] for r in records})
        answer = QMessageBox.question(
            self, "重新下载所选记录",
            f"将从服务器重新下载 {len(records)} 条记录（设备 {'、'.join(devices[:6])}"
            f"{' 等' if len(devices) > 6 else ''}）的九轴、PPG、温度。\n\n"
            "内容不同的本地文件会先原样备份到数据目录 .edge-download/recovery，再替换为服务器数据；"
            "内容一致的文件保持不变。确定重新下载吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.stop_scheduling()
        self.start_task("all", skip_ledger=True, only=records, force=True)

    def track_download(self, message):
        """Mirror the downloader's live progress into the 本轮下载 column."""
        text = str(message)
        if text.startswith(("已完成，跳过：", "本轮下载完成：", "今日数据仍在实时上传")):
            self.status.setText(text)
            return
        if text.startswith("正在下载 "):
            try:
                device, day_text = text[len("正在下载 "):].split(" ", 1)
                day = datetime.strptime(day_text[:10], "%Y-%m-%d").replace(tzinfo=CHINA)
            except ValueError:
                return
            self.status.setText(text)
            self._download_days.add(day.strftime("%Y-%m-%d"))
            for row in self.row_status:
                if self.row_status[row] == "正在下载":
                    self._set_row_status(row, "无缺失")
            for record in self.plan_records:
                if record["device"] != device:
                    continue
                start = datetime.fromisoformat(record["start"])
                end = datetime.fromisoformat(record["end"]) if record["end"] else None
                if start < day + timedelta(days=1) and (end is None or end > day):
                    self._set_row_status(record["row"], "正在下载")
            return
        for prefix, label in (("已下载：", "已下载"), ("已存在：", "已存在"),
                              ("已存在，跳过下载：", "已存在")):
            if text.startswith(prefix):
                identity = self._path_identity(text[len(prefix):].strip())
                if identity:
                    self._mark_records(identity, label)
                return
        if text.startswith("查询失败 "):
            try:
                device, rest = text[len("查询失败 "):].split(" ", 1)
                day_text = rest.split("：")[0]
                day = datetime.strptime(day_text, "%Y-%m-%d").replace(tzinfo=CHINA)
            except ValueError:
                return
            for record in self.plan_records:
                if record["device"] != device:
                    continue
                start = datetime.fromisoformat(record["start"])
                end = datetime.fromisoformat(record["end"]) if record["end"] else None
                if start < day + timedelta(days=1) and (end is None or end > day):
                    if self.row_status.get(record["row"]) in ("正在下载", ""):
                        self._set_row_status(record["row"], "查询失败")
            return
        if text.startswith("下载失败 "):
            parts = text.split(" ")
            if len(parts) > 1 and "/" in parts[1]:
                device = parts[1].split("/")[1]
                for record in self.plan_records:
                    if record["device"] == device and self.row_status.get(record["row"]) == "正在下载":
                        self._set_row_status(record["row"], "有失败批次，见日志")

    @staticmethod
    def _path_identity(relative):
        parts = [p for p in relative.replace("\\", "/").split("/") if p]
        if len(parts) < 5:
            return None
        device = parts[3].split("-")[0].upper()
        try:
            stamp = datetime.strptime(parts[4][:19], "%Y-%m-%d_%H-%M-%S").replace(tzinfo=CHINA)
        except ValueError:
            return None
        return device, stamp

    def _mark_records(self, identity, label):
        device, stamp = identity
        for record in self.plan_records:
            if record["device"] != device:
                continue
            start = datetime.fromisoformat(record["start"])
            end = datetime.fromisoformat(record["end"]) if record["end"] else None
            if start <= stamp and (end is None or stamp < end):
                self._set_row_status(record["row"], label)

    def _set_row_status(self, row, text):
        self.row_status[row] = text
        position = self._row_positions.get(row)
        if position is not None and position < self.plan_table.rowCount():
            self.plan_table.setItem(position, 0, QTableWidgetItem(text))

    def _ensure_report_window(self):
        if self.report_window is None:
            self.report_window = LedgerReportWindow(self)
        return self.report_window

    def show_csv_issues(self):
        window = self._ensure_report_window()
        window.set_issues(self.csv_issues)
        window.set_notes(self.csv_notes)
        window.tabs.setCurrentWidget(window.issue_table)
        window.show()
        window.raise_()
        window.activateWindow()

    def show_notes_window(self):
        window = self._ensure_report_window()
        window.set_notes(self.csv_notes)
        window.tabs.setCurrentWidget(window.note_table)
        window.show()
        window.raise_()
        window.activateWindow()

    def show_plan_problems(self, issues):
        # Non-modal: normal records keep downloading while this stays open.
        window = self._ensure_report_window()
        window.set_issues(issues)
        window.set_notes(self.csv_notes)
        window.tabs.setCurrentWidget(window.issue_table)
        window.show()
        window.raise_()
        window.activateWindow()

    def open_manual(self):
        if self.manual_dialog is None:
            from .dialog import DownloadDialog

            self.manual_dialog = DownloadDialog(self)
        self.manual_dialog.show()
        self.manual_dialog.raise_()

    def show(self):
        super().show()

    def showEvent(self, event):
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            if self._refresh_on_open and self.store.value.get("sync_ledger", True):
                QTimer.singleShot(0, self._load_initial_ledger)

    def _load_initial_ledger(self):
        # Only a read-only ledger refresh. Raw transfer and timers still require Start.
        if self.isVisible() and not self.running:
            self.start_task("ledger")

    def reject(self):
        self.hide()

    def closeEvent(self, event):
        event.accept()
