"""Single-camera algorithm inspection, deliberately separate from candidates.

Results live in algorithm_inspections, never event_model_runs, drafts or human
intervals. Both interfaces use the same hash-checked model engine/cache.
"""
from __future__ import annotations

import json
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .algorithm_catalog import bindings
from .event_models import available_packs, predict_one
from .signal_panel import reference_text


class AlgorithmPanel(QFrame):
    completed = Signal(object)
    exitRequested = Signal()

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.spec = None
        self.running = False
        self.cancelled = threading.Event()
        self.view_token = None
        self.clock_revision = None
        self.job_token = None
        self.setObjectName("algorithmPanel")
        self.setMinimumWidth(280)
        self.setMaximumWidth(400)
        layout = QVBoxLayout(self)
        self.heading = QLabel("行为识别")
        self.heading.setObjectName("sectionTitle")
        layout.addWidget(self.heading)
        self.camera = QComboBox()
        self.camera.setToolTip("只显示所选摄像头；切换保留同一参考时刻")
        self.camera.currentIndexChanged.connect(self.switch_camera)
        layout.addWidget(self.camera)
        self.versions = QComboBox()
        self.versions.setToolTip("已注册并校验的算法版本；原片不参与九轴模型输入")
        layout.addWidget(self.versions)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.run_button = QPushButton("运行算法")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.start)
        buttons.addWidget(self.run_button)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.items = QListWidget()
        self.items.setToolTip("算法结果，不是标签；双击定位九轴和已校准录像，人工独立判断")
        self.items.itemDoubleClicked.connect(self.review)
        self.items.currentItemChanged.connect(self.show_details)
        layout.addWidget(self.items, 1)
        self.label_button = QPushButton("选择对应人工标签")
        self.label_button.setToolTip("只切换人工标签类型，不创建区间、不接受模型结果")
        self.label_button.clicked.connect(self.choose_label)
        layout.addWidget(self.label_button)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(125)
        self.details.hide()
        details = QPushButton("算法与质量详情")
        details.setCheckable(True)
        details.toggled.connect(self.details.setVisible)
        layout.addWidget(details)
        layout.addWidget(self.details)
        back = QPushButton("返回标注布局")
        back.clicked.connect(self.exitRequested)
        layout.addWidget(back)
        self.completed.connect(self.receive)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.check_context)
        self.timer.start()

    def token(self):
        w = self.owner
        return (w.load_generation, str(w.catalog.root) if w.catalog else None,
                w.work.asset_id if w.work else None, w.work.project.cow_id if w.work else None,
                w.current_stamp, self.spec.code if self.spec else None)

    def set_algorithm(self, spec):
        self.cancel()
        self.spec = spec
        self.heading.setText(("行为识别 · " if spec.domain == "behavior" else "健康与繁殖 · ") + spec.title)
        self.versions.clear()
        try:
            self.bindings = bindings(spec, available_packs())
        except (OSError, ValueError, KeyError) as exc:
            self.bindings = []
            self.details.setPlainText(str(exc))
        for pack, model in self.bindings:
            self.versions.addItem(pack["version"] + " · " + model["title"])
        self.label_button.setVisible(spec.domain == "behavior")
        self.update_enabled()
        self.refresh_results()
        self.refresh_cameras()
        self.status.setText("单视角算法检查；结果不自动写入候选标注或人工标签。" if self.bindings else
                            "算法待接入 · 暂无预测结果\n可切换单个视角观察，不生成占位预测或健康结论。")

    def refresh_cameras(self):
        board = self.owner.board
        selected = list(board.selected)
        if selected != [self.camera.itemData(i) for i in range(self.camera.count())]:
            self.camera.blockSignals(True)
            self.camera.clear()
            for name in selected:
                self.camera.addItem(name, name)
            self.camera.blockSignals(False)
        self.camera.blockSignals(True)
        self.camera.setCurrentIndex(self.camera.findData(board.main_camera))
        self.camera.blockSignals(False)

    def switch_camera(self, *_):
        if self.camera.currentData():
            self.owner.board.set_main(self.camera.currentData())

    def update_enabled(self):
        enabled = bool(getattr(self, "bindings", [])) and not self.running
        self.versions.setEnabled(enabled)
        self.run_button.setEnabled(enabled)
        self.cancel_button.setEnabled(self.running)

    def check_context(self):
        if self.running and self.job_token != self.token():
            self.cancel()
        revision = self.owner.work.clock.revision if self.owner.work else None
        if self.spec and (self.view_token != self.token() or self.clock_revision != revision):
            self.refresh_results()
        if not self.isHidden():
            self.refresh_cameras()

    def start(self):
        w = self.owner
        if self.running or not self.bindings or not w.writable_work() or not w.motion:
            return
        if w._candidate_window is not None and w._candidate_window.running:
            self.status.setText("候选标注正在推理，请结束或取消后再运行本算法。")
            return
        if not w.work.project.cow_id.strip():
            self.status.setText("请先核对当前记录的牛号，不能按设备名推测。")
            return
        if w.motion.version != 2:
            self.status.setText("当前算法版本只接受完整 V2 九轴；当前记录仍可人工标注。")
            return
        self.running = True
        self.cancelled = threading.Event()
        self.job_token = self.token()
        token, cancel = self.job_token, self.cancelled
        pack, model = self.bindings[self.versions.currentIndex()]
        source, asset, cow = w.motion.source_path, w.work.asset_id, w.work.project.cow_id
        duration, cache = w.motion.duration_ms, w.catalog.meta / "cache" / "events"
        self.update_enabled()
        self.status.setText("后台运行中；可继续观察单路录像。结果是待验证的算法输出。")

        def run():
            result, error = None, None
            try:
                result = predict_one(pack, model, source, asset, cow, duration, cache, cancelled=cancel.is_set)
            except Exception as exc:
                error = str(exc)
            self.completed.emit((token, result, error, cancel.is_set()))
        threading.Thread(target=run, name="algorithm-inspection", daemon=True).start()

    def receive(self, payload):
        token, result, error, cancelled = payload
        self.running = False
        self.update_enabled()
        if token != self.token() or self.owner._closed:
            return
        if cancelled:
            self.status.setText("已取消；没有接受未完成结果。")
            return
        if error:
            self.status.setText("算法未完成 · 状态未知；人工标注仍可用。")
            self.details.setPlainText(error)
            self.details.show()
            return
        w = self.owner
        if not w.writable_work():
            return
        w.work.checkpoint()
        inspected = {**result, "inspection_code": self.spec.code, "kind": "algorithm_output_not_label"}
        w.work.project.extras.setdefault("algorithm_inspections", {})[result["id"]] = inspected
        w.dirty = True
        w.save_current()
        self.refresh_results()
        self.status.setText("算法完成；双击结果定位。无结果不等于无事件，分数不等于概率。")

    def cancel(self):
        self.cancelled.set()

    def refresh_results(self):
        self.view_token = self.token()
        self.items.clear()
        self.details.clear()
        w = self.owner
        self.clock_revision = w.work.clock.revision if w.work else None
        if not w.work or not self.spec:
            return
        for run in w.work.project.extras.get("algorithm_inspections", {}).values():
            if run["identity"]["cow_id"] != w.work.project.cow_id or run.get("inspection_code") != self.spec.code:
                continue
            candidates = run["candidates"] or [None]
            for candidate in candidates:
                text = "无事件点 · 点击检查质量"
                if candidate:
                    when = reference_text(w.work.clock, candidate["point_ms"], True) if w.work.clock.anchors else f"相对 {candidate['point_ms'] / 1000:.3f} 秒"
                    text = f"{when} · 分数 {candidate['score']:.3f}"
                item = QListWidgetItem(text + " · " + run["version"])
                item.setToolTip(item.text())
                item.setData(Qt.ItemDataRole.UserRole, (run, candidate))
                self.items.addItem(item)

    def show_details(self, *_):
        item = self.items.currentItem()
        if item:
            run, point = item.data(Qt.ItemDataRole.UserRole)
            self.details.setPlainText(json.dumps({"identity": run["identity"], "point": point, "quality": run["audit"]}, ensure_ascii=False, indent=2))

    def review(self, *_):
        item = self.items.currentItem()
        if not item or self.view_token != self.token():
            return
        _, point = item.data(Qt.ItemDataRole.UserRole)
        if not point:
            self.details.show()
            return
        w = self.owner
        w.board.play(False)
        w.selection = None
        at = point["point_ms"]
        w.plot.set_view(max(0, at - 30000), min(w.motion.duration_ms, at + 30000))
        w.seek_imu(at)
        if w.work.clock.anchors:
            w.board.seek(w.work.clock.map(at))
        else:
            self.status.setText("已定位九轴；同步尚未确认，请先核对视频对齐关系。")

    def choose_label(self):
        w = self.owner
        if not w.work:
            return
        index = next((i for i, label in enumerate(w.work.project.labels) if label.code == self.spec.code), None)
        if index is None:
            from PySide6.QtWidgets import QMessageBox

            from cowmata_tailring.annotation.core import Label
            from cowmata_tailring.annotation.defaults import DEFAULT_LABELS
            template = next((value for value in DEFAULT_LABELS if value["code"] == self.spec.code), None)
            if template is None or not w.writable_work():
                return
            if QMessageBox.question(self, "补充人工标签", "当前旧工程没有此标签。追加「" + template["name"] + "」？\n旧标签与已标区间不改变。") != QMessageBox.StandardButton.Yes:
                return
            w.work.checkpoint()
            # Appending leaves all stored label indices untouched. Existing
            # keyboard bindings are not remapped in a historical project.
            template = {**template, "key": ""}
            w.work.project.labels.append(Label.from_dict(template))
            w.dirty = True
            w.save_current()
            index = len(w.work.project.labels) - 1
        w.labels.blockSignals(True)
        w.labels.clear()
        for i, label in enumerate(w.work.project.labels):
            w.labels.addItem(f"[{label.key}] {label.name}", i)
        w.labels.setCurrentIndex(index)
        w.labels.blockSignals(False)
        self.status.setText("已选择人工标签；仍需观察视频、独立确定起止并确认真值。")
