"""Calving evidence, outcome training and manual decision-model application."""

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
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

from cowmata_tailring.algorithms import EVENT_CODES, EVENT_TITLES
from cowmata_tailring.application.algorithm_services import (
    available_model_packs, default_home, job as run_job, model_home,
)
from cowmata_tailring.algorithms.decision import ALGORITHMS, read_decision
from cowmata_tailring.ui.algorithms.behavior_ui import ResultChart, path_row
from cowmata_tailring.ui.algorithms.workbench_ui import JobWindow, fill, table


class DecisionWindow(JobWindow):
    def __init__(self, owner=None):
        super().__init__(owner)
        self.home = model_home().parent / "产犊决策"
        self.setWindowTitle("产犊预测 · 多指标综合决策")
        self.resize(1180, 850)
        outer = QVBoxLayout(self)
        note = QLabel(
            "手动选择包含连续多日 JSON 的文件夹，按时间滚动预测各时段风险。预测不需要台账，台账与数据集仅用于训练。"
        )
        note.setWordWrap(True)
        outer.addWidget(note)
        modelrow = QHBoxLayout()
        self.model = QLineEdit()
        self.model.setReadOnly(True)
        self.model.setPlaceholderText("先导入一次决策模型，后续选择 JSON 文件夹即可")
        modelrow.addWidget(self.model, 1)
        load = QPushButton("手动导入决策模型…")
        load.clicked.connect(self.import_model)
        modelrow.addWidget(load)
        outer.addLayout(modelrow)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.folder_page = QWidget()
        sl = QVBoxLayout(self.folder_page)
        self.folder_enabled = QCheckBox("手动开启文件夹滚动预测")
        self.folder_enabled.toggled.connect(self.folder_mode_changed)
        sl.addWidget(self.folder_enabled)
        folder_note = QLabel("建议连续3至7天，默认以3天为准备目标；少于24小时仍可分析，但标记参考历史不足。按时间读取目录中的 Motion、PPG 和 Temp，不要求另交台账。")
        folder_note.setWordWrap(True)
        sl.addWidget(folder_note)
        file_row = QHBoxLayout()
        self.folder_file = QLineEdit()
        self.folder_file.setReadOnly(True)
        self.folder_file.setPlaceholderText("选择包含连续多日 JSON 的文件夹")
        file_row.addWidget(self.folder_file, 1)
        folder_choose = QPushButton("选择 JSON 文件夹…")
        folder_choose.clicked.connect(self.receive_folder)
        file_row.addWidget(folder_choose)
        folder_predict = QPushButton("开始按时间预测")
        folder_predict.clicked.connect(self.predict_folder)
        file_row.addWidget(folder_predict)
        sl.addLayout(file_row)
        self.folder_summary = QLabel("文件夹预测已关闭，等待手动开启。")
        self.folder_summary.setWordWrap(True)
        sl.addWidget(self.folder_summary)
        self.folder_table = table(["牛耳标", "采样窗口截至", "预测可用时刻", "关注窗口截至", "风险分数", "建议", "信号覆盖率", "温度 °C", "缺失指标 / 参考历史", "模型版本"])
        sl.addWidget(self.folder_table, 3)
        self.coverage_table = table(["牛耳标", "数据跨度/小时", "有效信号/小时", "最大缺口/小时", "预测窗口数"])
        self.alert_table = table(["牛耳标", "首次预警", "最后预警", "关注窗口截至", "最高风险分数", "连续窗口数"])
        details = QTabWidget()
        details.addTab(self.coverage_table, "数据覆盖与缺口")
        details.addTab(self.alert_table, "连续预警时段")
        sl.addWidget(details, 1)
        folder_limits = QLabel("预测提前量由导入的模型决定。采样时间与预测可用时间分开显示，整包收到后才预测；每个时段只使用当时已收到的参考数据；高风险表示未来窗口需关注，不等于已确认产犊。没有新数据时结果不会更新，点击开始可重新分析目录。")
        folder_limits.setWordWrap(True)
        sl.addWidget(folder_limits)
        self.tabs.addTab(self.folder_page, "文件夹滚动预警")
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
        self.decision_page = decision
        dl = QVBoxLayout(decision)
        apply = QPushButton("开始综合决策")
        apply.clicked.connect(self.predict)
        dl.addWidget(apply)
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
            self.folder_file,
            folder_choose,
            folder_predict,
        ]
        self.decision_rows = []
        self.refresh_history()
        try:
            saved = json.loads((self.home / "selected-model.json").read_text(encoding="utf-8"))
            folder, _ = read_decision(saved["path"])
            self.model.setText(str(folder))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def refresh_models(self):
        self.packs = [
            p for p in available_model_packs() if p.get("adapter") == "numeric-event-intervals-1"
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
            atomic_json(self.home / "selected-model.json", {"path": str(folder)})
            self.show_metrics(doc)
            self.status.setText("决策模型已手动导入。")
        except (OSError, ValueError, KeyError) as exc:
            self.status.setText(str(exc))

    def folder_mode_changed(self, enabled):
        self.folder_summary.setText("已开启：选择文件夹后开始按时间预测，也可重新分析当前目录。" if enabled else "文件夹预测已关闭，等待手动开启。")
        if not enabled and self.running and getattr(self, "request", {}).get("action") == "folder_predict393":
            self.cancelled_request()

    def receive_folder(self):
        file = QFileDialog.getExistingDirectory(self, "选择包含连续多日 JSON 的文件夹", self.folder_file.text())
        if file:
            self.folder_file.setText(file)
            if self.folder_enabled.isChecked():
                self.predict_folder()
            else:
                self.folder_summary.setText("文件夹已选择；手动开启后点击开始按时间预测。")

    def predict_folder(self):
        if not self.folder_enabled.isChecked():
            self.status.setText("请先手动开启文件夹滚动预测。")
            return
        if not self.model.text():
            self.status.setText("请先导入一次决策模型，后续选择 JSON 文件夹即可。")
            return
        if not self.folder_file.text():
            self.status.setText("请选择包含连续多日 JSON 的文件夹。")
            return
        self.last_output = self.fresh_output("folder-prediction")
        self.launch(dict(action="folder_predict393", folder=self.folder_file.text(),
                         model=self.model.text(), model_home=str(default_home()),
                         output=str(self.last_output), cache=str(self.home / "cache")))

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
            if action == "folder_predict393":
                fill(self.folder_table, [[r["cow_id"] or "未提供", self.at(r["end_epoch_ms"]), self.at(r["decision_epoch_ms"]),
                    self.at(r["forecast_end_ms"]), r["risk_score"], r["warning_level"], max(r.get("motion_coverage") or 0, r.get("ppg_coverage") or 0),
                    r.get("temperature_c"), ",".join(r["missing_features"]) + " / " + r["history_status"], r["decision_model"]] for r in result["rows"]])
                info = result["input"]
                self.folder_summary.setText(f"已完成 {info['sensor_records']} 份信号记录、{len(result['rows'])} 个预测时段、{len(result['alert_windows'])} 段连续预警；预测未来 {info['horizon_hours']} 小时。")
                fill(self.coverage_table, [[r['cow_id'] or '未提供', r['span_hours'], r['effective_signal_hours'], r['largest_gap_hours'], r['windows']] for r in result['coverage']])
                fill(self.alert_table, [[r['cow_id'] or '未提供', self.at(r['first_warning_ms']), self.at(r['last_warning_ms']), self.at(r['forecast_end_ms']), r['max_score'], r['windows']] for r in result['alert_windows']])
                self.tabs.setCurrentWidget(self.folder_page)
            else:
                self.tabs.setCurrentWidget(self.decision_page)
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
