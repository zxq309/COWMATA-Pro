"""Single-behavior training and manual-model recognition task window."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from cowmata_tailring.edge_download.pro_settings import configured_data_root
from cowmata_tailring.workspace.storage import atomic_json

from . import EVENT_CODES, EVENT_TITLES
from .registry import install_suite, read_suite
from .workbench_ui import JobWindow, dataset_default, fill, table


class ResultChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = []
        self.title = "完成任务后显示评价或识别分布"
        self.setMinimumHeight(170)

    def set_values(self, title, values):
        self.title = title
        self.values = [(str(k), float(v)) for k, v in values if v is not None][:30]
        self.setToolTip("\n".join(f"{k}: {v:.4g}" for k, v in self.values))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#f4f8ef"))
        p.setPen(QColor("#234139"))
        p.drawText(QRectF(12, 5, self.width() - 24, 30), Qt.AlignmentFlag.AlignLeft, self.title)
        if not self.values:
            p.drawText(
                QRectF(15, 50, self.width() - 30, 80), Qt.AlignmentFlag.AlignCenter, "暂无结果"
            )
            return
        maximum = max(max(v for _, v in self.values), 1.0)
        width = (self.width() - 40) / len(self.values)
        for i, (label, value) in enumerate(self.values):
            h = max(0, value) / maximum * (self.height() - 90)
            x = 20 + i * width
            p.fillRect(QRectF(x, self.height() - 45 - h, width * 0.75, h), QColor("#80ac38"))
            p.drawText(
                QRectF(x, self.height() - 66 - h, width, 20),
                Qt.AlignmentFlag.AlignLeft,
                f"{value:.3g}",
            )
            p.drawText(
                QRectF(x, self.height() - 40, width, 36),
                Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                label,
            )


def path_row(owner, layout, label, initial="", *, file=False):
    edit = QLineEdit(initial)
    row = QHBoxLayout()
    row.addWidget(edit, 1)
    button = QPushButton("选择…")

    def choose():
        value = (
            QFileDialog.getOpenFileName(owner, label, edit.text(), "JSON (*.json)")[0]
            if file
            else QFileDialog.getExistingDirectory(owner, label, edit.text())
        )
        if value:
            edit.setText(value)

    button.clicked.connect(choose)
    row.addWidget(button)
    layout.addRow(label, row)
    return edit, button


class BehaviorWindow(JobWindow):
    def __init__(self, owner=None):
        super().__init__(owner)
        self.setWindowTitle("行为识别 · 训练与识别")
        self.resize(1140, 840)
        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel("行为算法"))
        self.algorithm = QComboBox()
        for code in EVENT_CODES:
            self.algorithm.addItem(EVENT_TITLES[code], code)
        header.addWidget(self.algorithm, 1)
        self.modality = QComboBox()
        self.modality.addItem("九轴 Motion", "motion")
        self.modality.addItem("光学 PPG", "ppg")
        header.addWidget(self.modality)
        outer.addLayout(header)
        note = QLabel(
            "与“自动生成候选”共用事件代码和模型。每次训练一个行为；识别前手动导入对应数据类型的模型。"
        )
        note.setWordWrap(True)
        outer.addWidget(note)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        train = QWidget()
        tr = QVBoxLayout(train)
        form = QFormLayout()
        self.dataset, choose = path_row(
            self, form, "训练数据集（Raw / Label）", dataset_default("COWMATA_Behavior_Dataset")
        )
        tr.addLayout(form)
        commands = QHBoxLayout()
        scan = QPushButton("读取全部训练记录")
        scan.clicked.connect(self.inspect)
        fit = QPushButton("训练所选算法")
        fit.clicked.connect(self.train)
        folder = QPushButton("打开模型库")
        folder.clicked.connect(self.open_library)
        for button in (scan, fit, folder):
            commands.addWidget(button)
        tr.addLayout(commands)
        self.record_table = table(
            ["牛耳标", "设备编号", "现场标号", "数据类型", "标签数量", "原始记录", "内容 SHA-256"]
        )
        tr.addWidget(self.record_table, 2)
        self.train_tabs = QTabWidget()
        self.history = table(["训练时间 / 版本", "算法", "数据类型", "状态", "数据集", "结果目录"])
        self.history.itemSelectionChanged.connect(self.select_history)
        self.metrics = table(["指标", "值", "说明"])
        self.chart = ResultChart()
        self.details = QTextBrowser()
        self.train_tabs.addTab(self.history, "全部训练记录")
        self.train_tabs.addTab(self.metrics, "评价结果")
        charts = QTabWidget()
        self.density_chart = ResultChart()
        self.importance_chart = ResultChart()
        charts.addTab(self.chart, "留出召回")
        charts.addTab(self.density_chart, "候选密度")
        charts.addTab(self.importance_chart, "特征重要性")
        self.train_tabs.addTab(charts, "可视化分析")
        self.train_tabs.addTab(self.details, "训练配置与数据问题")
        tr.addWidget(self.train_tabs, 2)
        self.tabs.addTab(train, "训练")
        recognition = QWidget()
        rr = QVBoxLayout(recognition)
        form2 = QFormLayout()
        self.raw, choose_raw = path_row(self, form2, "原始下载目录", str(configured_data_root()))
        self.model = QLineEdit()
        self.model.setReadOnly(True)
        modelrow = QHBoxLayout()
        modelrow.addWidget(self.model, 1)
        import_button = QPushButton("手动导入模型…")
        import_button.clicked.connect(self.import_model)
        modelrow.addWidget(import_button)
        form2.addRow("已导入模型", modelrow)
        rr.addLayout(form2)
        start = QPushButton("开始识别所选行为")
        start.clicked.connect(self.recognize)
        rr.addWidget(start)
        self.recognition_table = table(
            [
                "牛耳标",
                "设备编号",
                "现场标号",
                "行为",
                "开始（北京时间）",
                "结束（北京时间）",
                "模型分数",
                "来源",
            ]
        )
        rr.addWidget(self.recognition_table, 3)
        self.recognition_chart = ResultChart()
        rr.addWidget(self.recognition_chart, 1)
        self.recognition_note = QLabel(
            "模型分数用于筛选候选，识别结果保留原始记录引用，可导出复核。"
        )
        self.recognition_note.setWordWrap(True)
        rr.addWidget(self.recognition_note)
        self.tabs.addTab(recognition, "识别")
        footer = QHBoxLayout()
        output = QPushButton("打开本次结果")
        output.clicked.connect(self.open_output)
        footer.addWidget(output)
        footer.addWidget(self.status, 1)
        self.add_cancel(footer)
        outer.addLayout(footer)
        self.controls = [
            self.algorithm,
            self.modality,
            self.dataset,
            choose,
            self.raw,
            choose_raw,
            scan,
            fit,
            start,
            import_button,
        ]
        self.algorithm.currentIndexChanged.connect(lambda *_: self.model.clear())
        self.modality.currentIndexChanged.connect(lambda *_: self.model.clear())
        self.refresh_history()

    def open_library(self):
        self.home.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.home.parent)))

    def open_output(self):
        output = getattr(self, "last_output", None)
        if output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(output)))

    def inspect(self):
        self.launch(dict(action="inspect390", dataset=self.dataset.text(), training=True))

    def train(self):
        dataset = self.dataset.text().strip()
        if not dataset or not Path(dataset).is_dir():
            self.status.setText("请先选择存在的训练数据集目录，再开始训练。")
            return
        code = self.algorithm.currentData()
        modality = self.modality.currentData()
        self.last_output = self.fresh_output(code.lower() + "-" + modality)
        self.last_output.mkdir(parents=True, exist_ok=True)
        request = dict(
            action="train",
            dataset=self.dataset.text(),
            codes=[code],
            modality=modality,
            output=str(self.last_output),
            cache=str(self.home / "cache"),
        )
        atomic_json(
            self.last_output / "run-state.json",
            dict(request=request, status="running", created_at=datetime.now().isoformat()),
        )
        self.launch(request)
        self.refresh_history()

    def import_model(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "选择训练结果中的 suite.json", str(self.home), "模型清单 (suite.json)"
        )
        if not filename:
            return
        try:
            suite = read_suite(Path(filename).parent)
            if self.algorithm.currentData() not in {m["code"] for m in suite["models"]}:
                raise ValueError("该模型不包含所选行为")
            if suite.get("modality", "motion") != self.modality.currentData():
                raise ValueError("模型数据类型不匹配，请在顶部选择九轴或 PPG")
            installed = install_suite(suite["root"], self.home, make_active=True)
            self.model.setText(str(installed))
            self.status.setText("模型已导入；相同行为的自动候选和产犊分析也可使用此版本。")
        except (OSError, ValueError, KeyError) as exc:
            self.status.setText(str(exc))

    def recognize(self):
        if not self.model.text():
            self.status.setText("请先手动导入所选行为模型。")
            return
        self.last_output = self.fresh_output("recognition")
        self.launch(
            dict(
                action="recognize390",
                root=self.raw.text(),
                suite=self.model.text(),
                code=self.algorithm.currentData(),
                output=str(self.last_output),
                cache=str(self.home / "cache"),
            )
        )

    def refresh_history(self):
        self.history_files = sorted((self.home / "runs").glob("*/run-state.json"), reverse=True)
        rows = []
        for file in self.history_files:
            try:
                item = json.loads(file.read_text(encoding="utf-8"))
                req = item["request"]
                rows.append(
                    [
                        file.parent.name,
                        ",".join(EVENT_TITLES.get(c, c) for c in req.get("codes", [])),
                        req.get("modality"),
                        item["status"],
                        req.get("dataset"),
                        str(file.parent),
                    ]
                )
            except (OSError, ValueError, KeyError):
                rows.append([file.parent.name, "", "", "记录不可读", "", ""])
        fill(self.history, rows)

    def select_history(self):
        row = self.history.currentRow()
        if 0 <= row < len(self.history_files):
            folder = self.history_files[row].parent
            self.last_output = folder
            self.show_report(folder)

    def show_report(self, folder):
        report = folder / "评估报告.json"
        state = folder / "run-state.json"
        info = json.loads(state.read_text(encoding="utf-8")) if state.is_file() else {}
        if report.is_file():
            result = json.loads(report.read_text(encoding="utf-8"))
            metrics = []
            chart = []
            for model in result.get("models", []):
                title = model.get("title", model["code"])
                metrics.extend(
                    [
                        [title + " · 已知事件召回", model.get("known_recall"), "按留出记录评估"],
                        [
                            title + " · 候选 / 小时",
                            model.get("candidates_per_hour"),
                            "未标注片段不自动视为负例",
                        ],
                        [
                            title + " · 独立验证组",
                            model.get("validation_groups"),
                            model.get("validation_unit"),
                        ],
                        [title + " · 精确率 / F1", model.get("f1"), model.get("reason")],
                    ]
                )
                chart.append((title + "召回率", model.get("known_recall")))
            fill(self.metrics, metrics)
            self.chart.set_values("留出验证：已知事件召回率", chart)
            validation = result.get("validation_records", [])
            self.density_chart.set_values(
                "每份留出记录的候选密度 / 小时",
                [
                    (
                        str(i + 1),
                        r.get("candidates", 0) / max(r.get("observed_seconds", 0) / 3600, 1e-6),
                    )
                    for i, r in enumerate(validation)
                ],
            )
            weights = (
                result.get("models", [{}])[0].get("feature_importance", {})
                if result.get("models")
                else {}
            )
            self.importance_chart.set_values(
                "最终训练模型的全局特征重要性", sorted(weights.items(), key=lambda x: -x[1])[:10]
            )
            info["evaluation"] = result
        self.details.setPlainText(json.dumps(info, ensure_ascii=False, indent=2))
        inputs = folder / "training-inputs.json"
        if inputs.is_file():
            self.show_records(json.loads(inputs.read_text(encoding="utf-8")))

    def show_records(self, index):
        fill(
            self.record_table,
            [
                [
                    r["cow_id"],
                    r["device_id"],
                    r.get("field_mark", ""),
                    r.get("modality", "motion"),
                    len(r["events"]),
                    r["raw"],
                    r["asset_id"],
                ]
                for r in index["records"]
            ],
        )
        self.status.setText(
            f"已读取 {len(index['records'])} 份唯一记录；{len(index.get('issues', []))} 项数据问题。"
        )
        self.details.setPlainText(json.dumps(index.get("issues", []), ensure_ascii=False, indent=2))

    def finish(self, payload):
        if getattr(self, "request", {}).get("action") == "train":
            result, error, cancelled = payload
            folder = Path(self.request["output"])
            state = folder / "run-state.json"
            item = json.loads(state.read_text(encoding="utf-8"))
            item.update(
                status="canceled" if cancelled else "failed" if error else "complete",
                completed_at=datetime.now().isoformat(),
                error=error,
            )
            atomic_json(state, item)
        super().finish(payload)
        self.refresh_history()

    def accept_result(self, result):
        action = self.request["action"]
        if action == "inspect390":
            self.show_records(result)
        elif action == "train":
            self.show_report(Path(self.request["output"]))
            self.status.setText(
                "训练完成。模型和全部评价已保存到模型库；到识别页手动导入 suite.json 后使用。"
            )
        else:
            from collections import Counter

            from cowmata_tailring.edge_download.core import CHINA

            def at(ms):
                return datetime.fromtimestamp(ms / 1000, CHINA).strftime("%Y-%m-%d %H:%M:%S")

            fill(
                self.recognition_table,
                [
                    [
                        r["cow_id"],
                        r["device_id"],
                        r["field_mark"],
                        EVENT_TITLES.get(r["code"], r["code"]),
                        at(r["start_epoch_ms"]),
                        at(r["end_epoch_ms"]),
                        r["score"],
                        r["source"],
                    ]
                    for r in result["rows"]
                ],
            )
            self.recognition_chart.set_values(
                "各牛候选事件数", Counter(r["cow_id"] for r in result["rows"]).items()
            )
            self.recognition_note.setText(
                f"读取 {result['records']} 份记录，产生 {len(result['rows'])} 个候选；{len(result['issues'])} 项数据问题。完整记录和问题清单已保存。"
            )
            self.status.setText("识别完成；结果可在模型库的本次运行目录查看。")
