"""Model iteration and four-evidence task windows; human labels remain separate."""
from __future__ import annotations

import csv
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cowmata_tailring.annotation.defaults import DEFAULT_LABELS, LEGACY_DEFAULT_LABELS
from cowmata_tailring.ui.task_window import TaskWindow
from cowmata_tailring.workspace.storage import atomic_json

from . import EVENT_TITLES
from .registry import activate, active_suite, default_home, install_suite, list_suites
from .runner import run_job

TITLES = {**{r["code"]: r["name"] for r in LEGACY_DEFAULT_LABELS + DEFAULT_LABELS}, **EVENT_TITLES}


def display(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def percent(value):
    return f"{value:.2%}" if value is not None else "—"


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    widget.setAlternatingRowColors(True)
    return widget


def fill(widget, rows):
    widget.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            item = QTableWidgetItem(display(value))
            item.setToolTip(display(value))
            widget.setItem(i, j, item)
    widget.resizeColumnsToContents()


def export_table(owner, widget):
    name, _ = QFileDialog.getSaveFileName(owner, "另存为 CSV", "监测报告.csv", "CSV (*.csv)")
    if name:
        with Path(name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(widget.horizontalHeaderItem(c).text() for c in range(widget.columnCount()))
            writer.writerows([widget.item(r, c).text() if widget.item(r, c) else ""
                              for c in range(widget.columnCount())] for r in range(widget.rowCount()))


def dataset_default(name):
    app = Path(__file__).resolve().parents[2]
    for base in [Path.cwd(), app, *app.parents]:
        for candidate in (base / "科牧特_数据集" / name, base / name):
            if candidate.is_dir():
                return str(candidate)
    return ""


class JobWindow(TaskWindow):
    completed = Signal(object)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.home = default_home()
        self.running = False
        self.cancelled = threading.Event()
        self.closing = False
        self.controls = []
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.completed.connect(self.finish)
        self.poller = QTimer(self)
        self.poller.setInterval(500)
        self.poller.timeout.connect(self.poll)

    def launch(self, request):
        if self.running:
            return
        self.running = True
        self.closing = False
        self.cancelled = threading.Event()
        self.request = request
        self.progress_path = self.home / "jobs" / (uuid.uuid4().hex + ".json")
        self.status.setText("后台处理中，可继续使用标注窗口。")
        for control in self.controls:
            control.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.poller.start()
        cancel = self.cancelled

        def work():
            result, error = None, None
            try:
                result = run_job(request, cancelled=cancel.is_set, progress_path=self.progress_path)
            except Exception as exc:
                error = str(exc)
            self.completed.emit((result, error, cancel.is_set()))
        threading.Thread(target=work, name="algorithm-workbench", daemon=True).start()

    def poll(self):
        try:
            progress = json.loads(self.progress_path.read_text(encoding="utf-8"))
            self.status.setText(f"{progress['message']} · {progress['done']}/{progress['total']}")
        except (OSError, ValueError, KeyError):
            pass

    def finish(self, payload):
        self.poller.stop()
        self.running = False
        for control in self.controls:
            control.setEnabled(True)
        self.cancel_button.setEnabled(False)
        result, error, cancelled = payload
        if cancelled:
            self.status.setText("已取消；已有模型与人工标签保留。")
        elif error:
            self.status.setText("任务未完成：" + error)
        else:
            try:
                self.accept_result(result)
            except (OSError, ValueError, KeyError) as exc:
                self.status.setText("结果读取失败：" + str(exc))
        if self.closing:
            self.close()

    def add_cancel(self, layout):
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancelled_request)
        layout.addWidget(self.cancel_button)

    def cancelled_request(self):
        self.cancelled.set()
        self.status.setText("正在停止后台任务…")

    def closeEvent(self, event):
        if self.running:
            self.closing = True
            self.cancelled_request()
            event.ignore()
        else:
            event.accept()

    def fresh_output(self, kind):
        return self.home / "runs" / (kind + "-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])


class AlgorithmWorkbench(JobWindow):
    def __init__(self, owner):
        super().__init__(owner)
        self.setWindowTitle("算法管理 · 数据规律、训练与版本评估")
        self.resize(1100, 740)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("通用事件按原始记录隔离验证；努责按牛关联验证。原始数据和标签不在此修改。"))
        line = QHBoxLayout()
        line.addWidget(QLabel("行为数据集"))
        self.dataset = QLineEdit(dataset_default("COWMATA_Behavior_Dataset"))
        settings = self.home / "settings.json"
        if settings.is_file():
            try:
                saved = json.loads(settings.read_text(encoding="utf-8")).get("behavior_dataset", "")
                if Path(saved).is_dir():
                    self.dataset.setText(saved)
            except (OSError, ValueError):
                pass
        line.addWidget(self.dataset, 1)
        browse = QPushButton("选择目录")
        browse.clicked.connect(self.browse)
        line.addWidget(browse)
        layout.addLayout(line)
        buttons = QHBoxLayout()
        for text, action in (("分析共同规律", lambda: self.start_action("patterns")),
                             ("训练新版本", lambda: self.start_action("train")),
                             ("导入模型版本", self.import_version)):
            button = QPushButton(text)
            button.clicked.connect(action)
            buttons.addWidget(button)
            self.controls.append(button)
        self.add_cancel(buttons)
        layout.addLayout(buttons)
        versions = QHBoxLayout()
        self.versions = QComboBox()
        versions.addWidget(self.versions, 1)
        use = QPushButton("使用所选版本 / 回退")
        use.clicked.connect(self.use_version)
        versions.addWidget(use)
        self.controls.extend([self.dataset, browse, self.versions, use])
        layout.addLayout(versions)
        self.active = QLabel()
        layout.addWidget(self.active)
        self.tabs = QTabWidget()
        self.metrics = table(["版本", "事件", "验证单位", "已标事件", "命中数", "已标召回率",
                              "每小时候选", "准确率", "F1", "适用范围"])
        self.patterns = table(["事件", "事件数", "可用事件数", "时长中位数(s)", "时长P90(s)",
                               "姿态变化≥10°占比", "角速度突增占比", "重复包络占比"])
        self.records = table(["时间", "任务", "输出目录"])
        self.tabs.addTab(self.metrics, "模型比较与评价")
        self.tabs.addTab(self.patterns, "共同规律")
        self.tabs.addTab(self.records, "训练与分析记录")
        layout.addWidget(self.tabs, 1)
        bottom = QHBoxLayout()
        export = QPushButton("当前表另存为 CSV")
        export.clicked.connect(lambda: export_table(self, self.tabs.currentWidget()))
        bottom.addWidget(export)
        folder = QPushButton("打开算法文件夹")
        folder.clicked.connect(self.open_folder)
        bottom.addWidget(folder)
        layout.addLayout(bottom)
        layout.addWidget(self.status)
        self.refresh_versions()
        history = sorted((self.home / "runs").glob("*"), reverse=True)
        fill(self.records, [[datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                            p.name.split("-")[0], str(p)] for p in history if p.is_dir()])
        patterns = [p / "规律报告.json" for p in history if (p / "规律报告.json").is_file()]
        try:
            suite = active_suite(self.home)
            if not patterns and suite and (suite["root"] / "patterns.json").is_file():
                patterns = [suite["root"] / "patterns.json"]
            if patterns:
                self.fill_patterns(json.loads(patterns[0].read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            pass

    def fill_patterns(self, result):
        fill(self.patterns, [[TITLES.get(r["code"], r["code"]), r["events"], r["usable_events"],
            r["duration_median_s"], r["duration_p90_s"], percent(r["orientation_change_ge10_share"]),
            percent(r["gyro_burst_ge1_5_share"]), percent(r["repeated_envelope_share"])] for r in result["patterns"]])

    def browse(self):
        value = QFileDialog.getExistingDirectory(self, "选择行为数据集", self.dataset.text())
        if value:
            self.dataset.setText(value)

    def open_folder(self):
        self.home.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.home)))

    def refresh_versions(self):
        self.versions.clear()
        rows = []
        for suite in list_suites(self.home):
            self.versions.addItem(suite["version"], suite["version"])
            try:
                report = json.loads((suite["root"] / suite["report"]).read_text(encoding="utf-8"))
                for m in report["models"]:
                    rows.append([suite["version"], m.get("title", m.get("code")),
                        "原始记录" if m.get("validation_unit") == "record" else "牛",
                        m.get("known_events"), m.get("matched_events"), percent(m.get("known_recall")),
                        m.get("candidates_per_hour"), m.get("precision"), m.get("f1"), m.get("reason", "")])
            except (OSError, ValueError, KeyError):
                continue
        fill(self.metrics, rows)
        try:
            suite = active_suite(self.home)
            self.active.setText("当前使用：" + (suite["version"] if suite else "未安装模型"))
            if suite:
                self.versions.setCurrentIndex(self.versions.findData(suite["version"]))
        except (OSError, ValueError, KeyError) as exc:
            self.active.setText(str(exc))

    def use_version(self):
        try:
            activate(self.home, self.versions.currentData())
            self.refresh_versions()
            self.status.setText("已切换。下一次候选扫描和产犊证据计算将使用此版本。")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status.setText(str(exc))

    def import_version(self):
        source = QFileDialog.getExistingDirectory(self, "选择包含 suite.json 的模型版本目录")
        if source:
            try:
                install_suite(source, self.home, make_active=False)
                self.refresh_versions()
                self.status.setText("版本已导入；比较评价表后可选择使用。")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.status.setText(str(exc))

    def start_action(self, action):
        if not self.dataset.text().strip() or not Path(self.dataset.text()).is_dir():
            self.status.setText("请先选择有效的行为数据集目录。")
            return
        atomic_json(self.home / "settings.json", {"behavior_dataset": self.dataset.text()})
        self.output = self.fresh_output("events" if action == "train" else "patterns")
        self.launch(dict(action=action, dataset=self.dataset.text(), cache=str(self.home / "cache"),
                         output=str(self.output)))

    def accept_result(self, result):
        if self.request["action"] == "train":
            install_suite(self.output, self.home, make_active=False)
            self.refresh_versions()
            self.versions.setCurrentIndex(self.versions.findData(result["version"]))
            self.status.setText("训练完成。新版本已保存，查看评价表后点击“使用所选版本”。")
        else:
            self.fill_patterns(result)
            self.tabs.setCurrentWidget(self.patterns)
            self.status.setText("共同规律已提取；占比描述已有样本，不代表所有事件必然满足。")
        i = self.records.rowCount()
        self.records.insertRow(i)
        for j, value in enumerate([datetime.now().isoformat(timespec="seconds"), self.request["action"], str(self.output)]):
            self.records.setItem(i, j, QTableWidgetItem(value))
        self.records.resizeColumnsToContents()


class EvidenceTrend(QWidget):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.setMinimumHeight(270)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        keys = [("activity_index", "活动量指数"), ("lying_fraction_known", "已知姿态中的躺卧占比"),
                ("straining_seconds", "努责候选秒数"), ("temperature_c", "传感器温度 °C")]
        height = self.height()/4
        for k, (key, title) in enumerate(keys):
            top = k*height
            painter.setPen(QColor("#697887"))
            painter.drawText(8, int(top+17), title)
            painter.drawLine(185, int(top+height-8), self.width()-15, int(top+height-8))
            values = [r[key] for r in self.rows if r[key] is not None]
            if not values:
                painter.drawText(200, int(top+30), "暂无有效数据")
                continue
            lo, hi = min(values), max(values)
            span = max(hi-lo, abs(hi)*.1, .01)
            painter.drawText(self.width()-80, int(top+17), f"{hi:.3f}")
            painter.drawText(self.width()-80, int(top+height-10), f"{lo:.3f}")
            previous = None
            painter.setPen(QPen(QColor("#187b78"), 2))
            for i, row in enumerate(self.rows):
                value = row[key]
                if value is None:
                    previous = None
                    continue
                x = 195 + i*(self.width()-290)/max(1, len(self.rows)-1)
                y = top+height-13-(value-lo)/span*(height-30)
                if previous is not None:
                    prev_row = self.rows[i-1]
                    contiguous = (prev_row["asset_id"] == row["asset_id"]
                                  and prev_row["end_ms"] == row["start_ms"])
                    if contiguous:
                        painter.drawLine(int(previous[0]), int(previous[1]), int(x), int(y))
                painter.drawEllipse(int(x)-2, int(y)-2, 4, 4)
                previous = (x, y)


class CalvingEvidenceWindow(JobWindow):
    def __init__(self, owner):
        super().__init__(owner)
        self.setWindowTitle("产犊预测 · 四项证据与趋势")
        self.resize(1180, 800)
        self.result = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("活动量、躺卧占比、努责、温度；当前不输出风险等级、产犊概率或提前预警结论。"))
        line = QHBoxLayout()
        self.dataset = QLineEdit(dataset_default("COWMATA_CalvingPred_Dataset"))
        line.addWidget(self.dataset, 1)
        browse = QPushButton("选择产犊数据集")
        browse.clicked.connect(self.browse)
        line.addWidget(browse)
        layout.addLayout(line)
        actions = QHBoxLayout()
        current = QPushButton("计算当前记录")
        current.clicked.connect(lambda: self.start_action(True))
        dataset = QPushButton("更新整个数据集证据")
        dataset.clicked.connect(lambda: self.start_action(False))
        actions.addWidget(current)
        actions.addWidget(dataset)
        self.add_cancel(actions)
        layout.addLayout(actions)
        self.cows = QComboBox()
        self.cows.currentIndexChanged.connect(self.refresh_rows)
        layout.addWidget(self.cows)
        self.trend = EvidenceTrend()
        layout.addWidget(self.trend)
        self.tabs = QTabWidget()
        self.evidence = table(["牛号", "记录", "窗口开始", "活动量指数", "活动变化", "躺卧占比(已知)",
            "姿态覆盖率", "躺卧占比下界", "躺卧占比上界", "努责次数", "努责秒数", "传感器温度°C",
            "温度变化°C", "九轴覆盖率", "未知秒数", "缺失秒数"])
        self.evaluation = table(["评价项", "数值", "口径与限制"])
        self.tabs.addTab(self.evidence, "四项证据")
        self.tabs.addTab(self.evaluation, "监测评价")
        layout.addWidget(self.tabs, 1)
        export = QPushButton("当前表另存为 CSV")
        export.clicked.connect(lambda: export_table(self, self.tabs.currentWidget()))
        layout.addWidget(export)
        layout.addWidget(self.status)
        self.controls = [current, dataset, browse, self.dataset]
        self.status.setText("每10分钟一行；躺卧由起立/卧倒推断，缺口和未知姿态单列。变化量使用同牛此前24小时的可用基线。")

    def browse(self):
        value = QFileDialog.getExistingDirectory(self, "选择产犊数据集", self.dataset.text())
        if value:
            self.dataset.setText(value)

    def start_action(self, current):
        try:
            suite = active_suite(self.home)
            if suite is None:
                raise ValueError("请先在算法管理中安装模型。")
            self.output = self.fresh_output("calving")
            request = dict(action="evidence", suite=str(suite["root"]), cache=str(self.home/"cache"),
                           output=str(self.output))
            if current:
                w = self.owner
                if not w.motion or not w.work or not w.work.project.cow_id.strip():
                    raise ValueError("请先打开九轴记录并核对牛号。")
                request["record"] = dict(raw=str(w.motion.source_path), asset_id=w.work.asset_id,
                                         cow_id=w.work.project.cow_id, events=[], identity_eligible=True)
            else:
                if not self.dataset.text().strip() or not Path(self.dataset.text()).is_dir():
                    raise ValueError("请选择有效的产犊数据集目录。")
                request["dataset"] = self.dataset.text()
            self.launch(request)
        except (OSError, ValueError, KeyError) as exc:
            self.status.setText(str(exc))

    def accept_result(self, result):
        self.result = result
        self.cows.clear()
        for cow in sorted({r["cow_id"] for r in result["rows"]}):
            self.cows.addItem(cow, cow)
        fill(self.evaluation, [[r["metric"], r["value"], r["scope"]] for r in result["evaluation"]])
        self.refresh_rows()
        self.status.setText(f"已完成 · 模型 {result['model_version']} · {len(result['rows'])} 个窗口 · "
                            f"{len(result['issues'])} 项数据问题 · 报告：{self.output}")

    def refresh_rows(self):
        if self.result is None:
            return
        rows = sorted([r for r in self.result["rows"] if r["cow_id"] == self.cows.currentData()],
                      key=lambda r: (r["start_epoch_ms"] or 0, r["asset_id"], r["start_ms"]))
        self.trend.rows = rows
        self.trend.update()
        values = []
        for r in rows:
            when = (datetime.fromtimestamp(r["start_epoch_ms"]/1000).strftime("%m-%d %H:%M:%S")
                    if r["start_epoch_ms"] is not None else f"相对 {r['start_ms']/1000:.0f}s")
            values.append([r["cow_id"], Path(r["source"]).stem, when, r["activity_index"], r["activity_index_change"],
                percent(r["lying_fraction_known"]), percent(r["known_posture_coverage"]), percent(r["lying_fraction_lower"]),
                percent(r["lying_fraction_upper"]), r["straining_onsets"], r["straining_seconds"], r["temperature_c"],
                r["temperature_c_change"], percent(r["motion_coverage"]), r["unknown_seconds"], r["missing_seconds"]])
        fill(self.evidence, values)
