from __future__ import annotations

import csv
import json
import os
import queue
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from cowmata_tailring.ui.task_window import TaskWindow

from .decider import _anchors, algorithm_home
from .paths import APP_ROOT, CLASSIC, LLM, TRAINER, version_key

PPG_SOURCES = ("金姆脉诊", "逍遥脉诊")


def package_version(folder: Path | None) -> str:
    if folder is None:
        return ""
    return next((p.name for p in (folder, *folder.parents) if version_key(p.name)), "")


def trainer_home(app_root=APP_ROOT, track=CLASSIC, version=""):
    if not version:
        return None
    layouts = (((track, TRAINER, "产犊", version), ("算法",)), ((TRAINER, "产犊", version), ("算法",)))
    for anchor in _anchors(app_root):
        for before, after in layouts:
            folder = anchor.joinpath(*before, *after)
            if (folder / "calving.py").is_file() and (folder / "runtime" / "python.exe").is_file():
                return folder
    return None


def build_command(package: Path, action: str, ppg: str | None = None):
    args = [str(package / "runtime" / "python.exe"), "-X", "utf8", str(package / "calving.py"), action]
    if ppg:
        args += ["--ppg", ppg]
    return args


class ForwardDecisionWindow(TaskWindow):
    def __init__(self, owner=None, app_root=APP_ROOT):
        super().__init__(owner)
        self.app_root = app_root
        self.process = None
        self.stream = None
        self._lines = queue.Queue()
        self.setWindowTitle("正向决策")
        self.resize(860, 640)
        layout = QVBoxLayout(self)
        grid = QGridLayout()
        grid.addWidget(QLabel("算法路线"), 0, 0)
        self.route = QComboBox()
        self.route.addItem(CLASSIC, CLASSIC)
        self.route.addItem(LLM, LLM)
        self.route.currentIndexChanged.connect(self.refresh_state)
        grid.addWidget(self.route, 0, 1)
        grid.addWidget(QLabel("脉诊来源"), 0, 2)
        self.ppg = QComboBox()
        self.ppg.addItems(PPG_SOURCES)
        grid.addWidget(self.ppg, 0, 3)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        grid.addWidget(self.status, 1, 0, 1, 4)
        layout.addLayout(grid)
        row = QHBoxLayout()
        for title, handler in (("正向决策一次", self.decide_once), ("盲测（不使用产犊时间）", self.blindtest),
                               ("盲测评估", self.blind_eval), ("打开盲测报告", self.open_blind_report),
                               ("打开监测总览", self.open_overview), ("打开结果文件夹", self.open_results)):
            button = QPushButton(title)
            button.clicked.connect(handler)
            row.addWidget(button)
        self.cancel_button = QPushButton("取消任务")
        self.cancel_button.clicked.connect(self.cancel_job)
        self.cancel_button.setEnabled(False)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        self.summary = QLabel("暂无盲测评估摘要。")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("background:#edf4fb; padding:8px; border-radius:4px")
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 0)
        layout.addWidget(self.table, 1)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        layout.addWidget(self.log)
        self.poll = QTimer(self)
        self.poll.setInterval(120)
        self.poll.timeout.connect(self.read_process)
        self.refresh_state()

    def homes(self):
        forward = algorithm_home(self.app_root, self.route.currentData())
        return forward, trainer_home(self.app_root, self.route.currentData(), package_version(forward))

    def refresh_state(self):
        forward, trainer = self.homes()
        if forward is None:
            self.status.setText(f"未找到{self.route.currentText()}正向决策器：目录树\\{self.route.currentText()}\\5_正向决策器\\产犊\\<版本>\\推理算法。")
        else:
            text = f"正向决策器：{forward}"
            if trainer is None:
                text += "\n未找到同版本训练器算法；盲测评估不可用。"
            else:
                text += f"\n训练器算法：{trainer}"
            self.status.setText(text)
        self.load_summary()
        self.load_table()

    def forward_version_dir(self):
        forward, _trainer = self.homes()
        if forward is None:
            return None
        return forward.parent if forward.name == "推理算法" else forward

    def run_package(self, package, action, ppg=None):
        if self.process is not None:
            self.status.setText("已有任务在运行，请先取消或等待完成。")
            return
        if package is None:
            self.status.setText("缺少算法包，无法运行。")
            return
        cmd = build_command(Path(package), action, ppg)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.process = subprocess.Popen(cmd, cwd=str(package), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, encoding="utf-8", errors="replace", env=env, creationflags=flags)
        except OSError as exc:
            self.process = None
            self.status.setText("启动失败：" + str(exc))
            return
        self.log.appendPlainText("> " + " ".join(cmd))
        self.cancel_button.setEnabled(True)
        def pump(process=self.process):
            if process.stdout is not None:
                for line in process.stdout:
                    self._lines.put(line.rstrip())
        threading.Thread(target=pump, name="forward-decision-log", daemon=True).start()
        self.poll.start()

    def read_process(self):
        if self.process is None:
            return
        for _ in range(80):
            try:
                self.log.appendPlainText(self._lines.get_nowait())
            except queue.Empty:
                break
        code = self.process.poll()
        if code is None:
            return
        self.poll.stop()
        self.status.setText("任务完成。" if code == 0 else f"任务失败，退出码 {code}。")
        self.process = None
        self.cancel_button.setEnabled(False)
        self.load_summary()
        self.load_table()

    def cancel_job(self):
        if self.process is not None:
            self.process.terminate()
            self.status.setText("已请求取消任务。")

    def decide_once(self):
        forward, _ = self.homes()
        self.run_package(forward, "decide", self.ppg.currentText())

    def blindtest(self):
        forward, _ = self.homes()
        self.run_package(forward, "blindtest", self.ppg.currentText())

    def blind_eval(self):
        _forward, trainer = self.homes()
        self.run_package(trainer, "blind-eval")

    def result_dir(self):
        base = self.forward_version_dir()
        return None if base is None else base / "盲测结果"

    def open_path(self, path, missing):
        if path is None or not Path(path).exists():
            self.status.setText(missing)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path))))

    def open_blind_report(self):
        base = self.result_dir()
        self.open_path(None if base is None else base / "盲测报告.html", "未找到盲测报告。")

    def open_overview(self):
        base = self.forward_version_dir()
        self.open_path(None if base is None else base / "前端对接" / "监测总览.html", "未找到监测总览。")

    def open_results(self):
        self.open_path(self.result_dir(), "未找到结果文件夹。")

    def load_summary(self):
        base = self.result_dir()
        path = None if base is None else base / "盲测评估摘要.json"
        if path is None or not path.is_file():
            self.summary.setText("暂无盲测评估摘要。")
            return
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
            head = doc.get("headline") or {}
            def pct(v):
                return "—" if v is None else f"{float(v) * 100:.1f}%"
            target = head.get("target_accuracy", 0.88)
            reached = "已达到" if head.get("reached") else "未达到"
            self.summary.setText(
                f"版本 {doc.get('version','')} · {doc.get('generated','')}\n"
                f"准确率 {pct(head.get('accuracy'))}　误报率 {pct(head.get('false_alarm_rate'))}　漏报率 {pct(head.get('miss_rate'))}\n"
                f"提前 {doc.get('window_h', 2.5)} 小时目标 {float(target)*100:.0f}%：{reached}"
            )
        except (OSError, ValueError, TypeError) as exc:
            self.summary.setText("评估摘要读取失败：" + str(exc))

    def load_table(self):
        base = self.result_dir()
        path = None if base is None else base / "盲测_逐头.csv"
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        if path is None or not path.is_file():
            return
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
        except OSError as exc:
            self.status.setText("逐头结果读取失败：" + str(exc))
            return
        if not rows:
            return
        headers = list(rows[0].keys())
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, key in enumerate(headers):
                self.table.setItem(r, c, QTableWidgetItem(str(row.get(key, ""))))


def open_forward_decision(window):
    current = getattr(window, "_forward_decision_window", None)
    if current is None:
        current = ForwardDecisionWindow(window)
        window._forward_decision_window = current
        current.destroyed.connect(lambda *_: setattr(window, "_forward_decision_window", None))
    current.show()
    current.raise_()
    current.activateWindow()
    return current

