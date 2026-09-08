"""One non-modal, cancellable candidate queue; human video review stays separate."""
from __future__ import annotations

import copy
import json
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .event_models import available_packs, predict_one


class CandidateWindow(QDialog):
    resultReady = Signal(object)
    jobFinished = Signal(object)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.running = False
        self.cancelled = threading.Event()
        self.job_token = None
        self.view_token = None
        self.setWindowTitle("事件候选 · 模型预测后需人工看录像")
        self.resize(700, 520)
        layout = QVBoxLayout(self)
        self.packs = available_packs()
        controls = QHBoxLayout()
        self.versions = QComboBox()
        for pack in self.packs:
            self.versions.addItem(pack["version"])
        controls.addWidget(self.versions)
        self.models = QComboBox()
        controls.addWidget(self.models, 1)
        self.start_button = QPushButton("扫描当前完整九轴")
        self.start_button.clicked.connect(self.start)
        controls.addWidget(self.start_button)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        controls.addWidget(self.cancel_button)
        layout.addLayout(controls)
        self.status = QLabel("单任务后台推理，最多使用两个逻辑 CPU；分数不是概率，空结果不是负样本。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.items = QListWidget()
        self.items.itemDoubleClicked.connect(self.review)
        self.items.currentItemChanged.connect(self.show_audit)
        layout.addWidget(self.items, 1)
        actions = QHBoxLayout()
        for text, action in (("定位九轴与录像", self.review), ("所选九轴区间建视频草稿", self.make_draft),
                             ("暂无法判断", lambda: self.verdict("unknown")), ("排除该候选", lambda: self.verdict("rejected"))):
            button = QPushButton(text)
            button.clicked.connect(action)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.audit = QPlainTextEdit()
        self.audit.setReadOnly(True)
        self.audit.setMaximumHeight(115)
        self.audit.hide()
        self.details = QPushButton("质量 / 版本详情")
        self.details.setCheckable(True)
        self.details.toggled.connect(self.audit.setVisible)
        layout.addWidget(self.details)
        layout.addWidget(self.audit)
        self.versions.currentIndexChanged.connect(self.refresh_models)
        self.refresh_models()
        self.resultReady.connect(self.receive)
        self.jobFinished.connect(self.finished_job)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.check_context)
        self.timer.start()
        self.refresh_results()

    def token(self):
        w = self.owner
        return (w.load_generation, str(w.catalog.root) if w.catalog else None,
                w.work.asset_id if w.work else None, w.work.project.cow_id if w.work else None, w.current_stamp)

    def refresh_models(self, *_):
        self.models.clear()
        self.models.addItem("全部五类（逐个运行）", None)
        if self.packs:
            for model in self.packs[self.versions.currentIndex()]["models"]:
                self.models.addItem(model["title"], model["id"])
        self.start_button.setEnabled(bool(self.packs) and not self.running)

    def check_context(self):
        if self.running and self.token() != self.job_token:
            self.cancel()
            self.status.setText("记录、牛号或工程已改变；旧任务结果不会写入当前记录。")
        if self.view_token != self.token():
            self.refresh_results()

    def start(self):
        w = self.owner
        if getattr(w, "algorithm_panel", None) is not None and w.algorithm_panel.running:
            self.status.setText("独立算法正在运行，请结束或取消后再扫描候选。")
            return
        if self.running or not self.packs or not w.writable_work() or not w.motion:
            return
        if not w.work.project.cow_id.strip():
            self.status.setText("请先核对当前记录的牛号；不能按设备名猜牛。")
            return
        if w.motion.version != 2:
            self.status.setText("此版五类模型只接受完整 V2 原始九轴记录；其他格式仍可人工标注。")
            return
        self.running = True
        self.cancelled = threading.Event()
        self.job_token = self.token()
        token, cancel = self.job_token, self.cancelled
        pack = self.packs[self.versions.currentIndex()]
        selected = [m for m in pack["models"] if self.models.currentData() in (None, m["id"])]
        source, asset, cow = w.motion.source_path, w.work.asset_id, w.work.project.cow_id
        duration, cache = w.motion.duration_ms, w.catalog.meta / "cache" / "events"
        for control in (self.start_button, self.versions, self.models):
            control.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText("正在后台扫描；可继续播放录像和标注。关闭本窗口会取消剩余任务。")

        def run():
            errors = []
            try:
                for model in selected:
                    if cancel.is_set():
                        break
                    try:
                        result = predict_one(pack, model, source, asset, cow, duration, cache, cancelled=cancel.is_set)
                        if not cancel.is_set():
                            self.resultReady.emit((token, result))
                    except Exception as exc:
                        if not cancel.is_set():
                            errors.append(model["title"] + ": " + str(exc))
            finally:
                if not w._closed:
                    self.jobFinished.emit((token, errors, cancel.is_set()))
        threading.Thread(target=run, name="event-candidates", daemon=True).start()

    def receive(self, payload):
        token, result = payload
        w = self.owner
        if token != self.token() or self.cancelled.is_set() or w._closed or not w.writable_work():
            return
        w.work.checkpoint()
        runs = w.work.project.extras.setdefault("event_model_runs", {})
        old = {c["id"]: c for c in runs.get(result["id"], {}).get("candidates", [])}
        for candidate in result["candidates"]:
            if candidate["id"] in old:
                candidate["review_status"] = old[candidate["id"]].get("review_status", "pending")
        runs[result["id"]] = result
        w.dirty = True
        w.save_current()
        self.refresh_results()
        self.status.setText("候选已保存；双击定位，人工修正边界后再建立视频草稿。")

    def finished_job(self, payload):
        token, errors, was_cancelled = payload
        self.running = False
        for control in (self.start_button, self.versions, self.models):
            control.setEnabled(bool(self.packs))
        self.cancel_button.setEnabled(False)
        if token != self.token():
            return
        self.status.setText("已取消；已经保存的候选保留。" if was_cancelled else
                            "扫描完成；无候选或质量拒绝均不代表无事件。")
        if errors:
            self.status.setText("部分模型未完成，状态未知；人工标注仍可用。")
            self.details.setChecked(True)
            self.audit.setPlainText("\n\n".join(errors))

    def cancel(self):
        self.cancelled.set()

    def refresh_results(self):
        self.view_token = self.token()
        self.items.clear()
        w = self.owner
        for run in w.work.project.extras.get("event_model_runs", {}).values() if w.work else []:
            if run["identity"]["cow_id"] != w.work.project.cow_id:
                continue
            if not run["candidates"]:
                item = QListWidgetItem(run["version"] + " · " + run["model_title"] + " · 无候选（点击看质量报告）")
                item.setData(Qt.ItemDataRole.UserRole, (run["id"], None))
                self.items.addItem(item)
            for candidate in run["candidates"]:
                status = {"pending": "待复核", "unknown": "暂未知", "rejected": "已排除", "drafted": "已建草稿"}.get(candidate["review_status"], candidate["review_status"])
                text = f"{candidate['point_ms'] / 1000:.3f}s · {run['model_title']} · {candidate['score']:.3f} · {status} · {run['version']}"
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, (run["id"], candidate["id"]))
                self.items.addItem(item)

    def selected(self):
        item = self.items.currentItem()
        if not item or self.view_token != self.token():
            return None, None
        key, cid = item.data(Qt.ItemDataRole.UserRole)
        run = self.owner.work.project.extras.get("event_model_runs", {}).get(key)
        return run, next((c for c in run["candidates"] if c["id"] == cid), None) if run else None

    def show_audit(self, *_):
        run, candidate = self.selected()
        self.audit.setPlainText(json.dumps({"candidate": candidate, "quality": run["audit"]}, ensure_ascii=False, indent=2) if run else "")
        if run and not candidate:
            self.details.setChecked(True)

    def review(self, *_):
        run, candidate = self.selected()
        if not candidate:
            return
        w = self.owner
        w.board.play(False)
        w.selection = None  # never reuse an unrelated old waveform selection
        point = candidate["point_ms"]
        w.plot.set_view(max(0, point - 30000), min(w.motion.duration_ms, point + 30000))
        w.seek_imu(point)
        if w.work.clock.anchors:
            w.board.seek(w.work.clock.map(point))
            self.status.setText("已定位候选附近；请看录像并在九轴上选择真实起止范围。")
        else:
            self.status.setText("已定位九轴；同步未知，视频位置保持不变，请先核对同步点。")

    def make_draft(self):
        run, candidate = self.selected()
        w = self.owner
        if not candidate or not w.writable_work() or not w.selection:
            self.status.setText("请先选择候选，再在九轴波形上拖选实际范围；预测点不是动作边界。")
            return
        index = next((i for i, label in enumerate(w.work.project.labels) if label.code == candidate["code"]), None)
        if index is None:
            self.status.setText("当前标签配置中没有该事件类型，请先核对标签配置。")
            return
        w.labels.setCurrentIndex(index)
        before = len(w.work.drafts)
        w.mark_selection()
        if len(w.work.drafts) > before:
            w.work.drafts[-1]["model_candidate"] = {"run_identity": copy.deepcopy(run["identity"]), "candidate": copy.deepcopy(candidate)}
            candidate["review_status"] = "drafted"
            w.dirty = True
            w.save_current()
            self.refresh_results()
            self.status.setText("仅建立待复核草稿；确认真值仍需牛号、有效同步和到位的录像证据。")

    def verdict(self, value):
        run, candidate = self.selected()
        w = self.owner
        if candidate and w.writable_work():
            w.work.checkpoint()
            candidate["review_status"] = value
            w.dirty = True
            w.save_current()
            self.refresh_results()

    def closeEvent(self, event):
        self.cancel()
        event.accept()  # retain the single dialog instance until owner closes
