"""Calving evidence, outcome training and manual decision-model application."""

import json
from datetime import datetime
from pathlib import Path

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
from cowmata_tailring.workspace.event_models import available_packs
from cowmata_tailring.workspace.storage import atomic_json

from . import EVENT_CODES, EVENT_TITLES
from .behavior_ui import ResultChart, path_row
from .decision import ALGORITHMS, read_decision
from .registry import default_home
from .workbench_ui import JobWindow, fill, table


class DecisionWindow(JobWindow):
    def __init__(self, owner=None):
        super().__init__(owner)
        self.home = default_home().parent / "产犊决策"
        self.setWindowTitle("产犊预测 · 多指标综合决策")
        self.resize(1180, 850)
        outer = QVBoxLayout(self)
        note = QLabel(
            "先从原始数据汇总证据，再用产犊登记训练决策模型。训练完成后手动导入模型，查看逐牛趋势与关注窗口。"
        )
        note.setWordWrap(True)
        outer.addWidget(note)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        evidence = QWidget()
        el = QVBoxLayout(evidence)
        form = QFormLayout()
        self.raw, choose = path_row(self, form, "原始下载目录", str(configured_data_root()))
        self.behavior = QComboBox()
        self.behavior.addItem("全部已导入的行为模型", None)
        for code in EVENT_CODES:
            self.behavior.addItem(EVENT_TITLES[code], code)
        form.addRow("行为识别算法", self.behavior)
        self.selected_models = QLabel()
        self.selected_models.setWordWrap(True)
        form.addRow("行为模型", self.selected_models)
        self.refresh_models()
        el.addLayout(form)
        row = QHBoxLayout()
        refresh = QPushButton("刷新已导入行为模型")
        refresh.clicked.connect(self.refresh_models)
        build = QPushButton("生成综合证据")
        build.clicked.connect(self.build)
        row.addWidget(refresh)
        row.addWidget(build)
        el.addLayout(row)
        self.evidence_table = table(
            [
                "牛耳标",
                "时间（北京时间）",
                "活动量",
                "躺卧占比（可知部分）",
                "姿态覆盖率",
                "努责次数",
                "温度 °C",
                "PPG 覆盖率",
                "心率",
                "血氧",
            ]
        )
        el.addWidget(self.evidence_table, 1)
        self.evidence_note = QLabel(
            "心率与血氧尚无已验证的换算模型时保持为空；独立 Temp 文件按同牛、同设备和采集时段关联。"
        )
        self.evidence_note.setWordWrap(True)
        el.addWidget(self.evidence_note)
        self.tabs.addTab(evidence, "数据与证据")
        training = QWidget()
        tl = QVBoxLayout(training)
        form2 = QFormLayout()
        self.evidence_file, choose_evidence = path_row(self, form2, "综合证据 JSON", "", file=True)
        self.ledger = QLineEdit(r"F:\牛舍\_现场记录\扬大产犊登记汇总.csv")
        form2.addRow("产犊登记 CSV", self.ledger)
        self.estimator = QComboBox()
        for key, title in ALGORITHMS.items():
            self.estimator.addItem(title, key)
        form2.addRow("决策算法", self.estimator)
        self.horizon = QComboBox()
        for h in (6, 12, 24, 48):
            self.horizon.addItem(str(h) + " 小时内产犊", h)
        self.horizon.setCurrentIndex(2)
        form2.addRow("预测提前量", self.horizon)
        tl.addLayout(form2)
        train = QPushButton("训练并按牛验证")
        train.clicked.connect(self.train)
        tl.addWidget(train)
        self.metrics = table(["评价指标", "留出结果"])
        tl.addWidget(self.metrics, 1)
        self.metric_chart = ResultChart()
        tl.addWidget(self.metric_chart, 1)
        self.history = table(["运行目录", "状态", "算法", "结束时间"])
        self.history.itemSelectionChanged.connect(self.history_selected)
        tl.addWidget(self.history, 1)
        self.training_detail = QTextBrowser()
        tl.addWidget(self.training_detail, 1)
        self.tabs.addTab(training, "决策训练")
        decision = QWidget()
        dl = QVBoxLayout(decision)
        modelrow = QHBoxLayout()
        self.model = QLineEdit()
        self.model.setReadOnly(True)
        modelrow.addWidget(self.model, 1)
        load = QPushButton("手动导入决策模型…")
        load.clicked.connect(self.import_model)
        modelrow.addWidget(load)
        apply = QPushButton("开始综合决策")
        apply.clicked.connect(self.predict)
        modelrow.addWidget(apply)
        dl.addLayout(modelrow)
        filterrow = QHBoxLayout()
        self.cow = QComboBox()
        self.cow.currentTextChanged.connect(self.filter_rows)
        filterrow.addWidget(QLabel("查看牛耳标"))
        filterrow.addWidget(self.cow, 1)
        dl.addLayout(filterrow)
        self.decision_table = table(
            ["牛耳标", "时间（北京时间）", "预测提前量", "风险分数", "建议", "缺失指标", "模型版本"]
        )
        dl.addWidget(self.decision_table, 2)
        self.trend = ResultChart()
        dl.addWidget(self.trend, 1)
        self.explanation = QTextBrowser()
        dl.addWidget(self.explanation, 1)
        self.tabs.addTab(decision, "综合决策")
        footer = QHBoxLayout()
        open_result = QPushButton("打开本次结果")
        open_result.clicked.connect(self.open_output)
        footer.addWidget(open_result)
        footer.addWidget(self.status, 1)
        self.add_cancel(footer)
        outer.addLayout(footer)
        self.controls = [
            self.raw,
            choose,
            refresh,
            build,
            train,
            apply,
            load,
            self.estimator,
            self.horizon,
            self.behavior,
            self.ledger,
            choose_evidence,
            self.evidence_file,
        ]
        self.decision_rows = []
        self.refresh_history()

    def refresh_models(self):
        self.packs = [
            p for p in available_packs() if p.get("adapter") == "numeric-event-intervals-1"
        ]
        self.selected_models.setText(
            "；".join(
                p["version"]
                + " ("
                + p.get("modality", "motion")
                + ")："
                + ",".join(m["title"] for m in p["models"])
                for p in self.packs
            )
            or "尚未导入行为模型；可先汇总活动量、温度，或到行为识别窗口手动导入模型。"
        )

    def build(self):
        self.refresh_models()
        self.last_output = self.fresh_output("evidence")
        self.launch(
            dict(
                action="fusion390",
                root=self.raw.text(),
                suites=[str(p["root"]) for p in self.packs],
                selections={str(p["root"]): [m["code"] for m in p["models"]] for p in self.packs},
                codes=[self.behavior.currentData()] if self.behavior.currentData() else None,
                output=str(self.last_output),
                cache=str(self.home / "cache"),
            )
        )

    def train(self):
        if not self.evidence_file.text():
            self.status.setText("请先生成或选择综合证据 JSON。")
            return
        self.last_output = self.fresh_output(self.estimator.currentData())
        self.last_output.mkdir(parents=True, exist_ok=True)
        request = dict(
            action="decision_train390",
            evidence=self.evidence_file.text(),
            ledger=self.ledger.text(),
            algorithm=self.estimator.currentData(),
            horizon=self.horizon.currentData(),
            output=str(self.last_output),
        )
        atomic_json(
            self.last_output / "run-state.json",
            dict(request=request, status="running", created_at=datetime.now().isoformat()),
        )
        self.launch(request)
        self.refresh_history()

    def import_model(self):
        file, _ = QFileDialog.getOpenFileName(
            self, "选择训练结果中的 decision.json", str(self.home), "决策模型 (decision.json)"
        )
        if not file:
            return
        try:
            folder, doc = read_decision(file)
            self.model.setText(str(folder))
            self.show_metrics(doc)
            self.status.setText("决策模型已手动导入。")
        except (OSError, ValueError, KeyError) as exc:
            self.status.setText(str(exc))

    def predict(self):
        if not self.model.text() or not self.evidence_file.text():
            self.status.setText("请先生成证据并手动导入决策模型。")
            return
        self.last_output = self.fresh_output("decision")
        self.launch(
            dict(
                action="decision_predict390",
                evidence=self.evidence_file.text(),
                model=self.model.text(),
                output=str(self.last_output),
            )
        )

    def open_output(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if getattr(self, "last_output", None):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output)))

    def show_metrics(self, result):
        metrics = result.get("metrics", {})
        names = {
            "roc_auc": "ROC AUC",
            "pr_auc": "PR AUC",
            "brier": "Brier 分数（越低越好）",
            "sensitivity": "灵敏度",
            "specificity": "特异度",
            "precision": "精确率",
            "confusion_matrix": "混淆矩阵 TN/FP；FN/TP",
            "validation": "评价范围",
        }
        fill(self.metrics, [[title, metrics.get(key)] for key, title in names.items()])
        self.metric_chart.set_values(
            "按牛分组留出评价",
            [
                (names[k], metrics.get(k))
                for k in ("roc_auc", "pr_auc", "sensitivity", "specificity")
            ],
        )
        self.training_detail.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))

    def refresh_history(self):
        self.history_files = sorted((self.home / "runs").glob("*/run-state.json"), reverse=True)
        rows = []
        for file in self.history_files:
            item = json.loads(file.read_text(encoding="utf-8"))
            rows.append(
                [
                    file.parent.name,
                    item["status"],
                    ALGORITHMS.get(item["request"].get("algorithm")),
                    item.get("completed_at", item.get("created_at")),
                ]
            )
        fill(self.history, rows)

    def history_selected(self):
        row = self.history.currentRow()
        if 0 <= row < len(self.history_files):
            self.last_output = self.history_files[row].parent
            file = self.last_output / "决策训练评价.json"
            if file.is_file():
                self.show_metrics(json.loads(file.read_text(encoding="utf-8")))

    def finish(self, payload):
        if getattr(self, "request", {}).get("action") == "decision_train390":
            result, error, cancelled = payload
            file = Path(self.request["output"]) / "run-state.json"
            item = json.loads(file.read_text(encoding="utf-8"))
            item.update(
                status="canceled" if cancelled else "failed" if error else "complete",
                error=error,
                completed_at=datetime.now().isoformat(),
            )
            atomic_json(file, item)
        super().finish(payload)
        self.refresh_history()

    def accept_result(self, result):
        action = self.request["action"]
        if action == "fusion390":
            self.evidence_file.setText(str(Path(self.request["output"]) / "综合证据.json"))
            fill(
                self.evidence_table,
                [
                    [
                        r["cow_id"],
                        self.at(r["end_epoch_ms"]),
                        r["activity_index"],
                        r["lying_fraction_known"],
                        r["known_posture_coverage"],
                        r["straining_onsets"],
                        r["temperature_c"],
                        r.get("ppg_coverage"),
                        r["heart_rate_bpm"],
                        r["spo2_percent"],
                    ]
                    for r in result["rows"]
                ],
            )
            self.status.setText(
                f"生成 {len(result['rows'])} 个证据窗口，{len(result['issues'])} 项数据问题；可继续训练或导入决策模型。"
            )
        elif action == "decision_train390":
            self.show_metrics(result)
            self.status.setText("决策训练完成；在综合决策页手动导入 decision.json。")
        else:
            self.decision_rows = result["rows"]
            self.cow.blockSignals(True)
            self.cow.clear()
            self.cow.addItem("全部牛")
            self.cow.addItems(sorted({r["cow_id"] for r in result["rows"]}))
            self.cow.blockSignals(False)
            self.filter_rows()
            model = result["model"]
            importance = sorted(model["feature_importance"].items(), key=lambda x: -x[1])[:10]
            self.explanation.setPlainText(
                "模型："
                + ALGORITHMS[model["algorithm"]]
                + "\n分数未经本牧场概率校准；关注窗口需结合原始证据复核。\n训练中的主要指标（全局重要性）：\n"
                + "\n".join(f"{k}: {v:.3f}" for k, v in importance)
                + "\n\n按牛留出评价：\n"
                + json.dumps(model["metrics"], ensure_ascii=False, indent=2)
            )
            self.tabs.setCurrentIndex(2)
            self.status.setText("综合决策完成；风险分数、缺失指标和逐牛结果已导出。")

    @staticmethod
    def at(ms):
        from cowmata_tailring.edge_download.core import CHINA

        return datetime.fromtimestamp(ms / 1000, CHINA).strftime("%m-%d %H:%M:%S") if ms else "—"

    def filter_rows(self, *_):
        cow = self.cow.currentText()
        rows = [r for r in self.decision_rows if cow == "全部牛" or r["cow_id"] == cow]
        fill(
            self.decision_table,
            [
                [
                    r["cow_id"],
                    self.at(r["decision_epoch_ms"]),
                    r["horizon_hours"],
                    r["risk_score"],
                    r["warning_level"],
                    ",".join(r["missing_features"]),
                    r["decision_model"],
                ]
                for r in rows
            ],
        )
        self.trend.set_values(
            "最近窗口风险分数（0–1）",
            [(self.at(r["decision_epoch_ms"]), r["risk_score"]) for r in rows[-24:]],
        )
