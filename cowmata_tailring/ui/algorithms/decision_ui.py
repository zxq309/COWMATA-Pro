"""健康与繁殖 · 产犊综合决策与预测（4.3.3）.

The window only builds requests for :mod:`cowmata_engine` (run in the bounded algorithm worker)
and renders its JSON results: feature-module status, decision dataset regularities, training
diagnostics for every algorithm, and rolling multi-horizon prediction.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from cowmata_tailring.edge_download.pro_settings import configured_data_root
from cowmata_tailring.ui.algorithms.charts import BarChart, LineChart
from cowmata_tailring.ui.algorithms.workbench_ui import JobWindow, dataset_default, fill, table
from cowmata_tailring.workspace.storage import atomic_json

HORIZON_CHOICES = (6, 12, 24, 48)
LEVEL_COLORS = {"临产": "#c23b4a", "高度关注": "#d0742c", "关注": "#b08a12", "正常": "#3f9c3a", "数据不足": "#8b8b8b"}


def _engine():
    from cowmata_engine.decision.models import ALGORITHMS, DEFAULT_ALGORITHMS
    from cowmata_engine.decision.predict import OUTPUT_FIELDS
    from cowmata_engine.features import FEATURE_MODULES, feature_catalog

    return ALGORITHMS, DEFAULT_ALGORITHMS, OUTPUT_FIELDS, FEATURE_MODULES, feature_catalog


def pct(value):
    return None if value is None else f"{float(value):.1%}"


def path_line(owner, form, label, initial="", *, kind="dir", pattern="JSON (*.json)"):
    edit = QLineEdit(initial)
    row = QHBoxLayout()
    row.addWidget(edit, 1)
    button = QPushButton("选择…")

    def choose():
        if kind == "dir":
            value = QFileDialog.getExistingDirectory(owner, label, edit.text())
        else:
            value = QFileDialog.getOpenFileName(owner, label, edit.text(), pattern)[0]
        if value:
            edit.setText(value)

    button.clicked.connect(choose)
    row.addWidget(button)
    form.addRow(label, row)
    return edit, button


METRIC_TEXT = """
<h3>一、推理输出（每头牛、每个整点预测时刻）</h3>
<ul>
<li><b>6 / 12 / 24 / 48 小时内产犊概率</b>：按牛留出后等渗校准的概率，四个提前量单调不减。</li>
<li><b>预警等级与建议</b>：正常 / 关注 / 高度关注 / 临产 / 数据不足；阈值取按牛留出 Youden 指数最优点，连续 2 个整点超过阈值才形成一次预警。</li>
<li><b>预计距产犊小时数</b>：分位数梯度提升给出 P10 / P50 / P90，经共形校正（CQR）使 80% 区间在留出数据上实际覆盖 80%。</li>
<li><b>主要驱动特征</b>：XGBoost（或堆叠中的 XGBoost）用精确 TreeSHAP 贡献；其他算法按相对本牛基线的 z 分数排序。</li>
<li><b>数据覆盖与缺失特征</b>：7 项特征近 6 小时覆盖率；参考历史 &lt;24 小时时基线类特征不可靠并明确标注。</li>
</ul>
<h3>二、训练评价（全部按牛分组交叉验证，同一头牛不会同时出现在训练与验证）</h3>
<ul>
<li><b>窗口级</b>：ROC AUC（含按牛自助法 95% 置信区间）、PR AUC、Brier、Log loss、ECE 校准误差、灵敏度、特异度、精确率、阴性预测值、F1、MCC、混淆矩阵。</li>
<li><b>事件级（牧场真正关心）</b>：产犊检出率、首次有效预警提前量（中位数与四分位）、提前量外的误报次数 / 牛·天。</li>
<li><b>剩余时间</b>：72 h 与 24 h 内预测的平均绝对误差、80% 区间实际覆盖率。</li>
<li><b>可视化</b>：ROC、PR、校准曲线、风险随距产犊时间变化、XGBoost 逐轮训练/验证损失、学习曲线、特征组置换重要性、单列重要性、提前量分布、各特征产前规律曲线（中位数 + 四分位带）。</li>
</ul>
<h3>三、综合研判后去掉或不输出的指标</h3>
<ul>
<li><b>精确到分钟的产犊时刻 / “分钟级误差”</b>：牧场登记时间常被取整到整点或半点，视频娩出标签只覆盖少数牛，无法给出可信的分钟级真值；高邮牧场实测 72 h 内剩余时间中位误差约 30 h，因此只输出经共形校正的 80% 区间，作“今天 / 明天 / 更晚”的粗判断。</li>
<li><b>跨牧场泛化精度</b>：目前只有一个牧场的数据，只能报告按牛留出结果，界面明确标注“未做跨牧场外部验证”。</li>
<li><b>难产 / 死胎风险、犊牛性别、产后疾病</b>：没有对应的登记真值，无法训练与验证，不输出。</li>
<li><b>深度时序模型（LSTM / Transformer）作为主模型</b>：有结局的产犊只有百余次，深度模型方差过大且不可解释；模型库改用树集成、提升、堆叠与分位数剩余时间回归。</li>
<li><b>未经校准的“风险分数”</b>：4.3.2 及以前的风险分数未校准，4.3.3 全部改为交叉拟合校准概率。</li>
<li><b>心率、血氧</b>：只有在对应特征模块给出通过验证的窗口值时才进入模型；信号质量不足的窗口保持为空，不用 0 占位。</li>
</ul>
"""


class DecisionWindow(JobWindow):
    def __init__(self, owner=None):
        super().__init__(owner)
        from cowmata_tailring.algorithms.paths import model_home

        algorithms, defaults, _, modules, _ = _engine()
        self.home = model_home().parent / "产犊决策"
        self.setWindowTitle("产犊预测 · 7 项特征综合决策与预测")
        self.resize(1320, 900)
        self.report = None
        self.prediction = None
        self.profile = {}
        outer = QVBoxLayout(self)
        note = QLabel("心率、血氧、活动量、温度、躺卧占比、努责占比、角速度频谱熵 7 项特征按牛对齐到整点；"
                      "模型库含规则打分、基线偏离、逻辑回归、决策树、随机森林、极端随机树、AdaBoost、XGBoost、堆叠集成与剩余时间回归，"
                      "全部按牛分组验证。预警不等于确认产犊，须结合视频与现场复核。")
        note.setWordWrap(True)
        outer.addWidget(note)
        modelrow = QHBoxLayout()
        self.model = QLineEdit()
        self.model.setReadOnly(True)
        self.model.setPlaceholderText("训练完成后自动选用；也可手动导入 decision.json")
        modelrow.addWidget(QLabel("当前决策模型"))
        modelrow.addWidget(self.model, 1)
        load = QPushButton("导入决策模型…")
        load.clicked.connect(self.import_model)
        modelrow.addWidget(load)
        outer.addLayout(modelrow)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self._build_prediction_tab()
        self._build_features_tab()
        self._build_dataset_tab()
        self._build_training_tab(algorithms, defaults)
        guide = QTextBrowser()
        guide.setHtml(METRIC_TEXT)
        self.tabs.addTab(guide, "输出与评价指标")
        footer = QHBoxLayout()
        open_result = QPushButton("打开本次结果")
        open_result.clicked.connect(self.open_output)
        footer.addWidget(open_result)
        footer.addWidget(self.status, 1)
        self.add_cancel(footer)
        outer.addLayout(footer)
        self.controls = [load, self.folder_choose, self.folder_predict, self.refresh_button,
                         self.features_root, self.raw_root, self.ledger, self.video_root, self.build_button,
                         self.dataset_file, self.algorithm_list, self.feature_pick, self.require_all, self.horizon,
                         self.folds, self.train_button]
        self.refresh_features()
        self.refresh_history()
        try:
            saved = json.loads((self.home / "selected-model.json").read_text(encoding="utf-8"))
            self._set_model(saved["path"], quiet=True)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    # ------------------------------------------------------------------ tabs
    def _build_prediction_tab(self):
        page = QWidget()
        self.folder_page = page
        layout = QVBoxLayout(page)
        self.folder_enabled = QCheckBox("手动开启文件夹滚动预测")
        self.folder_enabled.toggled.connect(self.folder_mode_changed)
        layout.addWidget(self.folder_enabled)
        row = QHBoxLayout()
        self.folder_file = QLineEdit()
        self.folder_file.setReadOnly(True)
        self.folder_file.setPlaceholderText("选择包含连续多日 Motion / PPG / Temp JSON 的文件夹（建议 3–7 天）")
        row.addWidget(self.folder_file, 1)
        self.folder_choose = QPushButton("选择 JSON 文件夹…")
        self.folder_choose.clicked.connect(self.receive_folder)
        row.addWidget(self.folder_choose)
        self.folder_predict = QPushButton("开始按时间预测")
        self.folder_predict.clicked.connect(self.predict_folder)
        row.addWidget(self.folder_predict)
        layout.addLayout(row)
        self.folder_summary = QLabel("文件夹预测已关闭，等待手动开启。")
        self.folder_summary.setWordWrap(True)
        layout.addWidget(self.folder_summary)
        cowrow = QHBoxLayout()
        cowrow.addWidget(QLabel("查看牛耳标"))
        self.cow = QComboBox()
        self.cow.currentTextChanged.connect(self.show_cow)
        cowrow.addWidget(self.cow, 1)
        layout.addLayout(cowrow)
        split = QSplitter(Qt.Orientation.Vertical)
        self.folder_table = table(["牛耳标", "预测时刻", "6h 概率", "12h 概率", "24h 概率", "48h 概率", "预警等级",
                                   "预计距产犊 h（P10–P50–P90）", "主要驱动", "缺失特征 / 参考历史", "模型版本"])
        split.addWidget(self.folder_table)
        charts = QTabWidget()
        self.risk_chart = LineChart(xlabel="预测时刻（北京时间）", ylabel="产犊概率")
        self.eta_chart = LineChart(xlabel="预测时刻（北京时间）", ylabel="预计距产犊小时数")
        self.alert_table = table(["牛耳标", "首次超阈", "确认预警", "最后预警", "峰值概率", "连续窗口", "预计产犊时刻", "关注截至"])
        self.coverage_table = table(["牛耳标", "起", "止", "跨度 h", "预测时刻数", "最大缺口 h", "各特征覆盖率"])
        self.explanation = QTextBrowser()
        charts.addTab(self.risk_chart, "风险趋势")
        charts.addTab(self.eta_chart, "预计产犊时间")
        charts.addTab(self.alert_table, "连续预警时段")
        charts.addTab(self.coverage_table, "数据覆盖与缺口")
        charts.addTab(self.explanation, "模型说明")
        split.addWidget(charts)
        split.setSizes([420, 360])
        layout.addWidget(split, 1)
        self.tabs.addTab(page, "滚动预测")

    def _build_features_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel("7 项决策特征由独立算法模块（cowmata_engine.features）计算；缺失模块不会用 0 替代，只在结果中标为缺失。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.feature_table = table(["特征", "模块状态", "版本", "数据类型", "输出列", "主列", "产前规律（模块声明）"])
        layout.addWidget(self.feature_table, 2)
        layout.addWidget(QLabel("行为识别算法（努责、卧倒、起立等事件为躺卧占比、努责占比特征提供依据）"))
        self.behavior_table = table(["行为", "算法实现", "外部模型目录", "已有版本化模型"])
        layout.addWidget(self.behavior_table, 1)
        self.refresh_button = QPushButton("刷新模块状态")
        self.refresh_button.clicked.connect(self.refresh_features)
        layout.addWidget(self.refresh_button)
        self.tabs.addTab(page, "特征模块")

    def _build_dataset_tab(self):
        page = QWidget()
        self.dataset_page = page
        layout = QVBoxLayout(page)
        form = QFormLayout()
        default = dataset_default("COWMATA_CalvingPred_Dataset")
        self.features_root, _ = path_line(self, form, "特征数据集（Features）", str(Path(default) / "Features") if default else "")
        self.raw_root, _ = path_line(self, form, "或：原始数据目录（现场计算特征）", "")
        self.raw_root.setPlaceholderText(str(configured_data_root()))
        self.ledger, _ = path_line(self, form, "产犊登记 CSV", "", kind="file", pattern="CSV (*.csv)")
        self.video_root, _ = path_line(self, form, "犊牛娩出标签数据集（可选）", default)
        layout.addLayout(form)
        self.build_button = QPushButton("构建决策数据集（对齐特征 + 派生趋势 + 关联产犊结局）")
        self.build_button.clicked.connect(self.build_dataset)
        layout.addWidget(self.build_button)
        split = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        tl = QHBoxLayout(top)
        self.dataset_summary = table(["项目", "数值"])
        tl.addWidget(self.dataset_summary, 1)
        self.coverage_chart = BarChart(title="各特征决策时刻覆盖率")
        tl.addWidget(self.coverage_chart, 1)
        split.addWidget(top)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        pick = QHBoxLayout()
        pick.addWidget(QLabel("产前规律曲线"))
        self.profile_column = QComboBox()
        self.profile_column.currentTextChanged.connect(self.show_profile)
        pick.addWidget(self.profile_column, 1)
        bl.addLayout(pick)
        tabs = QTabWidget()
        self.profile_chart = LineChart(xlabel="距产犊（小时，负数为产前）", ylabel="近 1 小时均值")
        self.univariate_table = table(["输入列", "单独区分度 AUC", "产前方向", "覆盖率", "24h 内中位数", "24h 外中位数"])
        tabs.addTab(self.profile_chart, "产前规律（中位数 + 四分位带）")
        tabs.addTab(self.univariate_table, "单特征区分度")
        bl.addWidget(tabs, 1)
        split.addWidget(bottom)
        split.setSizes([260, 460])
        layout.addWidget(split, 1)
        self.tabs.addTab(page, "决策数据集")

    def _build_training_tab(self, algorithms, defaults):
        page = QWidget()
        layout = QVBoxLayout(page)
        head = QHBoxLayout()
        form = QFormLayout()
        self.dataset_file, _ = path_line(self, form, "决策数据集目录", "")
        self.horizon = QComboBox()
        for h in HORIZON_CHOICES:
            self.horizon.addItem(f"{h} 小时内产犊", h)
        self.horizon.setCurrentIndex(2)
        form.addRow("主预测提前量", self.horizon)
        self.folds = QSpinBox()
        self.folds.setRange(3, 20)
        self.folds.setValue(5)
        form.addRow("按牛分组折数", self.folds)
        self.feature_pick = QListWidget()
        for key, (_, title) in _engine()[3].items():
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setCheckState(Qt.CheckState.Checked)
            self.feature_pick.addItem(item)
        self.feature_pick.setMaximumHeight(120)
        self.feature_pick.setToolTip("只用勾选的特征训练；取消勾选可做特征消融对比")
        form.addRow("参与决策的特征", self.feature_pick)
        self.require_all = QCheckBox("只在所选特征都有数据的时段训练（同一人群公平对比）")
        form.addRow("", self.require_all)
        head.addLayout(form, 2)
        self.algorithm_list = QListWidget()
        for key, info in algorithms.items():
            item = QListWidgetItem(f"{info['title']} · {info['family']}")
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setToolTip(info["note"])
            item.setCheckState(Qt.CheckState.Checked if key in defaults else Qt.CheckState.Unchecked)
            self.algorithm_list.addItem(item)
        self.algorithm_list.setMaximumHeight(170)
        head.addWidget(self.algorithm_list, 2)
        layout.addLayout(head)
        self.train_button = QPushButton("训练全部所选算法并按牛交叉验证")
        self.train_button.clicked.connect(self.train)
        layout.addWidget(self.train_button)
        split = QSplitter(Qt.Orientation.Vertical)
        self.leaderboard = table(["算法", "类别", "ROC AUC（95% CI）", "PR AUC", "Brier", "ECE", "灵敏度", "特异度", "F1",
                                  "产犊检出率", "提前量中位 h", "误报/牛·天", "耗时 s"])
        self.leaderboard.itemSelectionChanged.connect(self.show_algorithm)
        split.addWidget(self.leaderboard)
        charts = QTabWidget()
        self.roc_chart = LineChart(xlabel="假阳性率", ylabel="真阳性率")
        self.pr_chart = LineChart(xlabel="召回率", ylabel="精确率")
        self.cal_chart = LineChart(xlabel="预测概率", ylabel="实际产犊比例")
        self.hours_chart = LineChart(xlabel="距产犊（小时）", ylabel="留出概率")
        self.loss_chart = LineChart(xlabel="提升轮数", ylabel="Log loss")
        self.learning_chart = LineChart(xlabel="训练牛数", ylabel="ROC AUC")
        self.group_chart = BarChart(title="特征组置换重要性（AUC 下降）")
        self.column_chart = BarChart(title="最终模型单列重要性（前 20）")
        self.lead_chart = BarChart(title="首次有效预警提前量分布")
        self.metric_table = table(["指标", "数值", "说明"])
        self.event_table = table(["牛耳标", "产犊时间", "提前量覆盖", "是否检出", "提前量 h", "误报次数", "最高概率"])
        self.history = table(["训练目录", "状态", "最优算法", "提前量", "结束时间"])
        self.history.itemSelectionChanged.connect(self.history_selected)
        for widget, title in ((self.metric_table, "评价指标"), (self.roc_chart, "ROC"), (self.pr_chart, "PR"),
                              (self.cal_chart, "校准"), (self.hours_chart, "风险-距产犊时间"),
                              (self.loss_chart, "训练曲线"), (self.learning_chart, "学习曲线"),
                              (self.group_chart, "特征组重要性"), (self.column_chart, "单列重要性"),
                              (self.lead_chart, "提前量分布"), (self.event_table, "逐次产犊"),
                              (self.history, "训练记录")):
            charts.addTab(widget, title)
        split.addWidget(charts)
        split.setSizes([230, 480])
        layout.addWidget(split, 1)
        self.tabs.addTab(page, "决策训练")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def at(ms, fmt="%m-%d %H:%M"):
        from cowmata_tailring.edge_download.core import CHINA

        return datetime.fromtimestamp(ms / 1000, CHINA).strftime(fmt) if ms else "—"

    def engine_job(self, engine, kind):
        self.last_output = self.fresh_output(kind)
        self.last_output.mkdir(parents=True, exist_ok=True)
        request = dict(action="engine433", engine=dict(engine, output=engine.get("output", str(self.last_output))))
        atomic_json(self.last_output / "run-state.json",
                    dict(request=request, status="running", created_at=datetime.now().isoformat()))
        self.launch(request)
        return request

    def open_output(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if getattr(self, "last_output", None):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output)))

    def _set_model(self, path, quiet=False):
        from cowmata_engine.decision.predict import read_model

        folder, manifest, _, _ = read_model(path)
        self.model.setText(str(folder))
        atomic_json(self.home / "selected-model.json", {"path": str(folder)})
        self.explanation.setPlainText(self._model_text(manifest))
        if not quiet:
            self.status.setText(f"已选用决策模型 {manifest['version']}。")

    @staticmethod
    def _model_text(m):
        lines = [f"模型版本：{m['version']}", f"最优算法：{m['algorithm_title']}", f"主提前量：{m['horizon_hours']} 小时",
                 f"训练：{m['training_cows']} 头牛，{m['training_rows']} 个整点窗口", m["validation"],
                 "阈值（按牛留出 Youden 指数最优）：" + "，".join(f"{h} h = {v:.2f}" for h, v in m["thresholds"].items()),
                 "特征版本：" + "，".join(f"{k}={v}" for k, v in (m.get("feature_versions") or {}).items()), "",
                 "主提前量留出评价："]
        metrics = m.get("metrics", {})
        for key in ("roc_auc", "pr_auc", "brier", "ece", "sensitivity", "specificity", "f1"):
            if key in metrics:
                lines.append(f"  {key}: {metrics[key]:.3f}")
        events = m.get("events", {})
        lines.append(f"  产犊检出率: {pct(events.get('event_sensitivity'))}；提前量中位 {events.get('lead_time_median_h')} h；"
                     f"误报 {events.get('false_alerts_per_cow_day')} 次/牛·天")
        return "\n".join(lines)

    def import_model(self):
        file, _ = QFileDialog.getOpenFileName(self, "选择训练结果中的 decision.json", str(self.home), "决策模型 (decision.json)")
        if file:
            try:
                self._set_model(file)
            except (OSError, ValueError, KeyError) as exc:
                self.status.setText(str(exc))

    def _feature_title(self, key):
        _, _, _, modules, _ = _engine()
        return modules.get(key, (None, "时段/上下文" if key == "context" else key))[1]

    # ------------------------------------------------------------------ features
    def refresh_features(self):
        _, _, _, modules, catalog = _engine()
        rows = []
        for item in catalog():
            rows.append([item.get("title") or modules[item["key"]][1],
                         "可用" if item["ready"] else "未接入：" + (item.get("error") or "")[:60],
                         item.get("version"), item.get("modality"), ", ".join(item.get("columns", [])), item.get("primary"),
                         item.get("expected_change")])
        fill(self.feature_table, rows)
        try:
            from cowmata_engine.behavior import catalog as behaviors

            fill(self.behavior_table, [[b["title"], b["implementation"], b["artifact_dir"],
                                        "是" if b["has_versioned_models"] else "否"] for b in behaviors()])
        except (OSError, ValueError, ImportError) as exc:
            fill(self.behavior_table, [["行为算法目录不可用", str(exc), "", ""]])

    # ------------------------------------------------------------------ dataset
    def build_dataset(self):
        features, raw = self.features_root.text().strip(), self.raw_root.text().strip()
        use_features = bool(features) and Path(features).is_dir()
        if not use_features and not (raw and Path(raw).is_dir()):
            self.status.setText("请选择存在的特征数据集目录或原始数据目录。")
            return
        engine = dict(action="decision.build_dataset", features_root=features if use_features else None,
                      raw_root=None if use_features else raw, ledger=self.ledger.text().strip() or None,
                      calving_dataset=self.video_root.text().strip() or None)
        self.engine_job(engine, "dataset")

    def show_dataset(self, summary, folder=None):
        fill(self.dataset_summary, [
            ["决策时刻（整点）", summary["rows"]], ["有产犊结局的时刻", summary["labelled_rows"]],
            ["牛数 / 有结局牛数", f"{summary['cows']} / {summary['calving_cows']}"],
            ["产犊事件（视频 / 登记）", f"{summary['calvings']}（{summary['label_sources'].get('video', 0)} / "
                                      f"{summary['label_sources'].get('ledger', 0)}）"],
            *[[f"{h} 内产犊的正例时刻", n] for h, n in summary["positives"].items()],
            ["输入列数", len(summary["input_columns"])], ["缺失特征模块", "、".join(map(self._feature_title, summary["missing_features"])) or "无"],
            ["数据问题", summary.get("issue_count", 0)], ["耗时 s", summary.get("elapsed_seconds")]])
        self.coverage_chart.set_values("各特征决策时刻覆盖率",
                                       [(self._feature_title(k), v) for k, v in summary["feature_coverage"].items()])
        self.profile = summary.get("profile", {})
        self.profile_column.blockSignals(True)
        self.profile_column.clear()
        self.profile_column.addItems(sorted(self.profile))
        self.profile_column.blockSignals(False)
        self.show_profile()
        fill(self.univariate_table, [[r["column"], r["auc"], r["direction"], pct(r["coverage"]), r["median_positive"],
                                      r["median_negative"]] for r in summary.get("univariate", [])])
        if folder:
            self.dataset_file.setText(str(folder))

    def show_profile(self, *_):
        name = self.profile_column.currentText()
        points = self.profile.get(name, [])
        self.profile_chart.set_data(f"{name} · 产前 7 天规律（按 6 小时分箱）",
                                    [("中位数", [(p["hours_before"], p["median"]) for p in points])],
                                    bands=[("四分位", [(p["hours_before"], p["q25"], p["q75"]) for p in points])])

    # ------------------------------------------------------------------ training
    def selected_algorithms(self):
        return [self.algorithm_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.algorithm_list.count())
                if self.algorithm_list.item(i).checkState() == Qt.CheckState.Checked]

    def train(self):
        dataset = self.dataset_file.text().strip()
        if not dataset or not (Path(dataset) / "decision_table.csv").is_file():
            self.status.setText("请先构建或选择包含 decision_table.csv 的决策数据集目录。")
            return
        algorithms = self.selected_algorithms()
        if not algorithms:
            self.status.setText("请至少勾选一种决策算法。")
            return
        keys = [self.feature_pick.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.feature_pick.count())
                if self.feature_pick.item(i).checkState() == Qt.CheckState.Checked]
        if not keys:
            self.status.setText("请至少勾选一项参与决策的特征。")
            return
        engine = dict(action="decision.train", dataset=dataset, algorithms=algorithms,
                      horizon=self.horizon.currentData(), folds=self.folds.value())
        if len(keys) < self.feature_pick.count():
            engine["features"] = keys
        if self.require_all.isChecked():
            engine["require"] = keys
        self.engine_job(engine, "train")
        self.refresh_history()

    def show_report(self, report):
        self.report = report
        rows = []
        for item in report["leaderboard"]:
            m, e = item["metrics"], item["events"]
            ci = m.get("roc_auc_ci95")
            rows.append([item["title"], item["family"], f"{m['roc_auc']:.3f}" + (f"（{ci[0]:.2f}–{ci[1]:.2f}）" if ci else ""),
                         m["pr_auc"], m["brier"], m["ece"], pct(m["sensitivity"]), pct(m["specificity"]), m["f1"],
                         pct(e["event_sensitivity"]), e["lead_time_median_h"], e["false_alerts_per_cow_day"], item["seconds"]])
        fill(self.leaderboard, rows)
        self.group_chart.set_values("特征组置换重要性（验证折 AUC 下降）",
                                    [(self._feature_title(r["feature"]), r["auc_drop"]) for r in report["group_importance"]])
        self.column_chart.set_values("最终模型单列重要性（前 20）", report["column_importance"][:20])
        learning = report.get("learning_curve", [])
        self.learning_chart.set_data("学习曲线（按牛留出）", [
            ("验证 AUC", [(r["cows"], r["valid_auc"]) for r in learning]),
            ("训练 AUC", [(r["cows"], r["train_auc"]) for r in learning if r.get("train_auc") is not None])])
        fill(self.event_table, [[e["cow_id"], self.at(e["calving_epoch_ms"]), pct(e["horizon_coverage"]),
                                 "是" if e["detected"] else "否", e["lead_hours"], e["false_alerts"], e["max_probability"]]
                                for e in report.get("per_event", [])])
        if self.leaderboard.rowCount():
            self.leaderboard.selectRow(0)
        self.show_algorithm()

    def show_algorithm(self):
        if not self.report:
            return
        board = self.report["leaderboard"]
        item = board[min(max(0, self.leaderboard.currentRow()), len(board) - 1)]
        curves, m, e = item["curves"], item["metrics"], item["events"]
        best = board[0]
        self.roc_chart.set_data(f"ROC · {item['title']}（AUC {m['roc_auc']:.3f}）", [
            (item["title"], curves["roc"]), *([("最优：" + best["title"], best["curves"]["roc"])] if best is not item else [])],
            diagonal=True, xrange=(0, 1), yrange=(0, 1))
        self.pr_chart.set_data(f"PR · {item['title']}（AP {m['pr_auc']:.3f}，基线 {m['prevalence']:.3f}）",
                               [(item["title"], curves["pr"])], hlines=[("正例比例", m["prevalence"])],
                               xrange=(0, 1), yrange=(0, 1))
        self.cal_chart.set_data(f"校准曲线（ECE {m['ece']:.3f}）",
                                [(item["title"], [(c["predicted"], c["observed"]) for c in curves["calibration"]])],
                                diagonal=True, xrange=(0, 1), yrange=(0, 1))
        risk = curves["risk_by_hours"]
        self.hours_chart.set_data("留出概率随距产犊时间变化（中位数 + 四分位）",
                                  [("中位数", [(r["hours_before"], r["median"]) for r in risk])],
                                  bands=[("四分位", [(r["hours_before"], r["q25"], r["q75"]) for r in risk])],
                                  hlines=[("预警阈值", m["threshold"])])
        series = []
        for fold in item.get("fold_curves", []):
            series.append((f"第{fold['fold']}折 训练", list(enumerate(fold["train_logloss"]))))
            if fold.get("valid_logloss"):
                series.append((f"第{fold['fold']}折 验证", list(enumerate(fold["valid_logloss"]))))
        if series:
            self.loss_chart.set_data(f"{item['title']} · 逐轮训练/验证损失", series[:10])
        else:
            self.loss_chart.clear(f"{item['title']} 为非迭代算法；逐轮曲线见 XGBoost")
        hist = e.get("lead_hist", [])
        self.lead_chart.set_values("首次有效预警提前量分布（次）", [(f"{i * 6}–{i * 6 + 6} h", v) for i, v in enumerate(hist)])
        tte = self.report.get("time_to_calving_eval") or {}
        fill(self.metric_table, [
            ["ROC AUC", m["roc_auc"], "按牛分组留出" + (f"，95% CI {m['roc_auc_ci95']}" if m.get("roc_auc_ci95") else "")],
            ["PR AUC", m["pr_auc"], f"正例比例 {m['prevalence']:.3f}"], ["Brier", m["brier"], "越低越好"],
            ["Log loss", m["log_loss"], "越低越好"], ["ECE", m["ece"], "10 箱期望校准误差"],
            ["阈值", m["threshold"], "留出 Youden 指数（灵敏度+特异度−1）最优"], ["灵敏度", m["sensitivity"], ""], ["特异度", m["specificity"], ""],
            ["精确率", m["precision"], ""], ["阴性预测值", m["npv"], ""], ["F1", m["f1"], ""], ["MCC", m["mcc"], ""],
            ["混淆矩阵", m["confusion_matrix"], "[[TN, FP], [FN, TP]]"],
            ["产犊检出率", e["event_sensitivity"], f"{e['detected']}/{e['evaluable_calvings']} 次可评估产犊"],
            ["提前量中位 / 四分位 h", e["lead_time_median_h"], e["lead_time_iqr_h"]],
            ["误报 / 牛·天", e["false_alerts_per_cow_day"],
             f"提前量外 {e['exposure_cow_days']} 牛·天，连续 {e['persistence_hours']} 小时算一次预警"],
            *([["剩余时间 MAE（72 h 内）", tte.get("mae_hours_within_72h"), "小时"],
               ["剩余时间 MAE（24 h 内）", tte.get("mae_hours_within_24h"), "小时"],
               ["80% 区间覆盖率", tte.get("interval80_coverage"), "理想值 0.8"]] if tte and "error" not in tte else []),
        ])

    def refresh_history(self):
        self.history_files = sorted((self.home / "runs").glob("train-*/run-state.json"), reverse=True)
        rows = []
        for file in self.history_files:
            try:
                item = json.loads(file.read_text(encoding="utf-8"))
                manifest = file.parent / "decision.json"
                doc = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
                rows.append([file.parent.name, item.get("status"), doc.get("algorithm_title"), doc.get("horizon_hours"),
                             item.get("completed_at", item.get("created_at"))])
            except (OSError, ValueError):
                rows.append([file.parent.name, "记录不可读", "", "", ""])
        fill(self.history, rows)

    def history_selected(self):
        row = self.history.currentRow()
        if 0 <= row < len(self.history_files):
            folder = self.history_files[row].parent
            report = folder / "决策训练报告.json"
            if report.is_file():
                self.last_output = folder
                self.show_report(json.loads(report.read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ prediction
    def folder_mode_changed(self, enabled):
        self.folder_summary.setText("已开启：选择文件夹后开始按时间预测，也可重新分析当前目录。" if enabled
                                    else "文件夹预测已关闭，等待手动开启。")
        if not enabled and self.running and getattr(self, "request", {}).get("engine", {}).get("action") == "decision.predict_folder":
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
            self.status.setText("请先训练或导入一次决策模型。")
            return
        if not self.folder_file.text():
            self.status.setText("请选择包含连续多日 JSON 的文件夹。")
            return
        self.engine_job(dict(action="decision.predict_folder", folder=self.folder_file.text(), model=self.model.text()),
                        "predict")

    def show_prediction(self, result):
        self.prediction = result
        rows = result["rows"]
        fill(self.folder_table, [[
            r["cow_id"], self.at(r["decision_epoch_ms"]), *[pct(r["risk"].get(f"{h}h")) for h in HORIZON_CHOICES],
            r["warning_level"],
            (f"{r['hours_to_calving_p10']}–{r['hours_to_calving_p50']}–{r['hours_to_calving_p90']}"
             if r.get("hours_to_calving_p50") is not None else "—"),
            "；".join(d["title"] + ("↑" if (d.get("z") or 0) > 0 else "↓" if d.get("z") is not None else "") for d in r["drivers"]),
            (",".join(self._feature_title(k) for k in r["missing_features"]) or "无") + " / " + r["history_status"],
            r["model_version"]] for r in rows])
        self._color_levels()
        fill(self.alert_table, [[a["cow_id"], self.at(a["first_alert_ms"]), self.at(a["confirmed_alert_ms"]),
                                 self.at(a["last_alert_ms"]), pct(a["peak_risk"]), a["windows"],
                                 self.at(a.get("predicted_calving_epoch_ms")), self.at(a["attention_until_ms"])]
                                for a in result.get("alert_episodes", [])])
        fill(self.coverage_table, [[c["cow_id"], self.at(c["first_ms"]), self.at(c["last_ms"]), c["span_hours"],
                                    c["decision_points"], c["largest_gap_hours"],
                                    "，".join(f"{self._feature_title(k)} {v:.0%}" for k, v in c["features"].items() if v)]
                                   for c in result.get("coverage", [])])
        self.cow.blockSignals(True)
        self.cow.clear()
        self.cow.addItems(sorted({r["cow_id"] for r in rows}))
        self.cow.blockSignals(False)
        self.show_cow()
        levels = {}
        for r in rows:
            levels[r["warning_level"]] = levels.get(r["warning_level"], 0) + 1
        info = result.get("input", {})
        used = "、".join(self._feature_title(k) for k in result.get("used_features", []))
        self.folder_summary.setText(f"完成 {info.get('decision_points', len(rows))} 个整点预测、"
                                    f"{len(result.get('alert_episodes', []))} 段连续预警；"
                                    + "，".join(f"{k} {v}" for k, v in levels.items()) + f"。使用特征：{used}。")

    def _color_levels(self):
        from PySide6.QtGui import QColor

        for i in range(self.folder_table.rowCount()):
            item = self.folder_table.item(i, 6)
            if item and item.text() in LEVEL_COLORS:
                item.setForeground(QColor(LEVEL_COLORS[item.text()]))

    def show_cow(self, *_):
        if not self.prediction:
            return
        cow = self.cow.currentText()
        rows = sorted((r for r in self.prediction["rows"] if r["cow_id"] == cow), key=lambda r: r["decision_epoch_ms"])
        if not rows:
            return
        hours = [(r["decision_epoch_ms"] - rows[0]["decision_epoch_ms"]) / 3_600_000 for r in rows]
        ticks = [(hours[i], self.at(rows[i]["decision_epoch_ms"])) for i in range(0, len(rows), max(1, len(rows) // 6))]
        self.risk_chart.set_data(f"牛 {cow} · 各提前量产犊概率", [
            (f"{h} h 内", [(x, r["risk"][f"{h}h"]) for x, r in zip(hours, rows) if r["risk"].get(f"{h}h") is not None])
            for h in HORIZON_CHOICES], hlines=[("主提前量阈值", rows[0]["threshold"])], yrange=(0, 1), xticks=ticks)
        if rows[0].get("hours_to_calving_p50") is not None:
            self.eta_chart.set_data(f"牛 {cow} · 预计距产犊小时数（P50 与 80% 区间）",
                                    [("P50", [(x, r["hours_to_calving_p50"]) for x, r in zip(hours, rows)])],
                                    bands=[("P10–P90", [(x, r["hours_to_calving_p10"], r["hours_to_calving_p90"])
                                                        for x, r in zip(hours, rows)])], xticks=ticks)

    # ------------------------------------------------------------------ results
    def finish(self, payload):
        request = getattr(self, "request", {})
        if request.get("action") == "engine433":
            result, error, cancelled = payload
            file = Path(request["engine"]["output"]) / "run-state.json"
            try:
                item = json.loads(file.read_text(encoding="utf-8"))
                item.update(status="canceled" if cancelled else "failed" if error else "complete", error=error,
                            completed_at=datetime.now().isoformat())
                atomic_json(file, item)
            except (OSError, ValueError):
                pass
        super().finish(payload)
        self.refresh_history()

    def accept_result(self, result):
        action = self.request.get("engine", {}).get("action")
        if action == "decision.build_dataset":
            self.show_dataset(result, self.request["engine"]["output"])
            self.status.setText(f"决策数据集已构建：{result['rows']} 个整点、{result['calving_cows']} 头有结局的牛；"
                                "可到“决策训练”页训练。")
            self.tabs.setCurrentWidget(self.dataset_page)
        elif action == "decision.train":
            self.show_report(result)
            try:
                self._set_model(self.request["engine"]["output"], quiet=True)
            except (OSError, ValueError, KeyError):
                pass
            best = result["leaderboard"][0]
            self.status.setText(f"训练完成：最优 {best['title']}（PR AUC {best['metrics']['pr_auc']:.3f}，"
                                f"产犊检出率 {best['events']['event_sensitivity']:.0%}），已自动选用为当前决策模型。")
        elif action == "decision.predict_folder":
            self.show_prediction(result)
            self.tabs.setCurrentWidget(self.folder_page)
            self.status.setText("滚动预测完成；逐时刻概率、预警时段和数据覆盖已导出到本次结果目录。")
