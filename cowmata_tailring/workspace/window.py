from __future__ import annotations

import copy
import csv
import hashlib
import json
import threading
import uuid
from collections import Counter, OrderedDict
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from cowmata_tailring.annotation.core import (
    export_boris_csv,
    export_events_csv,
    export_meta_json,
    export_sample_multihot_csv,
    load_project,
    save_project,
)
from cowmata_tailring.annotation.data import load_motion_json
from cowmata_tailring.ui.interactive_plot import InteractiveSignalPlotWidget
from cowmata_tailring.ui.widgets import PlotSeries

from .catalog import Catalog, file_stamp
from .clocks import ClockMap, VideoTimeline, intervals_from_rows, wall_ms, wall_text
from .coverage import continuation_target, video_coverage
from .dialogs import MappingDialog, SourceTimeDialog
from .playback import VideoBoard
from .storage import SnapshotWriter, atomic_json, read_json, unique_batch
from .work import SessionWork
from .worker import IndexWorker


class MainWindow(QMainWindow):
    motionReady = Signal(object)
    continuationReady = Signal(object)
    backgroundError = Signal(str)
    exportReady = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("COWMATA · 九轴与多视角标注工作台")
        self.resize(1600, 1000)
        self.catalog = None
        self.worker = None
        self.retired = []
        self.rows = []
        self.settings = {}
        self.work = None
        self.motion = None
        self.current_row = None
        self.current_stamp = None
        self.source_available = False
        self.load_generation = 0
        self.motion_cache = OrderedDict()
        self.imu_ms = 0.0
        self.linked = False
        self.active_event = None
        self.selection = None
        self.busy_controls = False
        self.dirty = False
        self.snapshot_writer = SnapshotWriter()
        self._closed = False
        self._legacy_windows = []
        self._history_windows = []
        self._candidate_window = None
        self._capture_dialog = None
        self._archive_dialog = None
        self._export_running = False
        self._last_scan_complete = False
        self._coverage_second = None
        self._continuation_second = None
        self._continuation_pending = None
        self._work_cache = {}
        self._build_ui()
        self.motionReady.connect(self._motion_loaded)
        self.continuationReady.connect(self._continuation_ready)
        self.backgroundError.connect(self.tell)
        self.exportReady.connect(self._export_finished)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._directory_changed)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(1200)
        self.debounce.timeout.connect(self.refresh_sources)
        self.save_timer = QTimer(self)
        self.save_timer.setInterval(5000)
        self.save_timer.timeout.connect(self.auto_save)
        self.save_timer.start()
        self.source_timer = QTimer(self)
        self.source_timer.setInterval(2000)
        self.source_timer.timeout.connect(self.check_active_sources)
        self.source_timer.start()

    def _button(self, title, handler, layout):
        button = QPushButton(title)
        button.clicked.connect(handler)
        layout.addWidget(button)
        return button

    def _action(self, menu, title, function, shortcut=None):
        action = QAction(title, self)
        action.triggered.connect(function)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        menu.addAction(action)
        return action

    def _build_ui(self):
        files = self.menuBar().addMenu("工程")
        self._action(files, "打开数据工程…", self.choose_project, "Ctrl+O")
        self._action(files, "保存人工成果", self.save_current, "Ctrl+S")
        self._action(files, "导出当前成果…", self.export_work)
        self._action(files, "导出所选九轴片段（含标签）…", lambda: self.export_work(snippet=True))
        self._action(files, "打开历史标注回看…", self.open_history, "Ctrl+Shift+O")
        self._action(files, "训练与兼容格式（批量导出）…", self.export_training)
        self._action(files, "导入旧单视频工程…", self.import_legacy)
        self._action(files, "打开旧版单视频窗口", self.open_legacy)
        materials = self.menuBar().addMenu("素材")
        self._action(materials, "刷新 / 复制完成，重新检查", self.refresh_sources, "F5")
        self._action(materials, "素材与时间核验…", self.source_manager)
        self._action(materials, "新增唯一拷贝批次…", self.new_batch)
        self._action(materials, "全文件内容核验（耗时）", lambda: self.worker and self.worker.request("audit"))
        self._action(materials, "录像归档副本核验（不删除原片）…", self.verify_video_archive)
        sync = self.menuBar().addMenu("同步")
        self._action(sync, "九轴同步锚点与未确认区间…", self.edit_mapping)
        self._action(sync, "当前主视角相机时钟校准…", self.edit_camera_mapping)
        self._action(sync, "跳到下一个录像覆盖时段", lambda: self.board.next_coverage())
        edit = self.menuBar().addMenu("标注")
        self._action(edit, "新版事件候选预测…", self.open_candidates)
        self._action(edit, "撤销", self.undo, "Ctrl+Z")
        self._action(edit, "重做", lambda: self.undo(True), "Ctrl+Y")
        self._action(edit, "确认所选草稿为九轴真值", self.confirm_selected)
        self._action(edit, "留存多视角证据图（每视角一张）…", self.capture_evidence)
        self._action(edit, "删除所选标注/草稿", self.delete_selected)
        view = self.menuBar().addMenu("视图")
        self._action(view, "全屏 / 退出全屏", self.toggle_fullscreen, "F11")
        self._action(view, "退出单路放大", lambda: self.board.enlarge(None), "Esc")
        self._action(view, "双画面主视角宽度…", self.set_two_view_ratio)
        self._action(view, "显示完整九轴记录", lambda: self.motion and self.plot.set_view(0, self.motion.duration_ms), "F")
        self._action(view, "性能与索引诊断…", self.diagnostics)
        self._action(view, "切换硬件 / 软件解码（下次打开工程生效）", self.toggle_decode)

        toolbar = QToolBar("工程入口")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        open_button = QPushButton("打开工程目录")
        open_button.clicked.connect(self.choose_project)
        toolbar.addWidget(open_button)
        self.root_label = QLabel("   软件与数据可以放在不同位置   ")
        toolbar.addWidget(self.root_label)
        refresh = QPushButton("刷新素材")
        refresh.clicked.connect(self.refresh_sources)
        toolbar.addWidget(refresh)
        manager = QPushButton("素材 / 时间核验")
        manager.clicked.connect(self.source_manager)
        toolbar.addWidget(manager)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 6, 8, 6)
        self.banner = QLabel("请选择包含九轴 JSON 和录像的数据工程目录。程序只会新增一个“标注工程”目录，不改原始文件。")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background:#eaf1f8; color:#24415e; padding:7px; border-radius:4px")
        outer.addWidget(self.banner)
        self.coverage_label = QLabel("")
        self.coverage_label.setWordWrap(True)
        outer.addWidget(self.coverage_label)
        horizontal = QSplitter(Qt.Orientation.Horizontal)
        side = QWidget()
        sidebar = QVBoxLayout(side)
        sidebar.setContentsMargins(0, 0, 5, 0)
        sidebar.addWidget(QLabel("九轴设备 / 记录"))
        self.devices = QComboBox()
        self.devices.currentIndexChanged.connect(self.refresh_records)
        sidebar.addWidget(self.devices)
        self.records = QListWidget()
        self.records.currentItemChanged.connect(self.select_record)
        sidebar.addWidget(self.records, 1)
        self.cow = QLineEdit()
        self.cow.setPlaceholderText("本记录牛号（人工确认）")
        self.cow.editingFinished.connect(self.set_cow)
        sidebar.addWidget(self.cow)
        sidebar.addWidget(QLabel("勾选 1–8 个视角 · 拖动调整顺序"))
        self.cameras = QListWidget()
        self.cameras.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.cameras.itemChanged.connect(self.select_cameras)
        self.cameras.model().rowsMoved.connect(self.select_cameras)
        sidebar.addWidget(self.cameras, 1)
        layout_row = QHBoxLayout()
        self.layout_choice = QComboBox()
        self.layout_choice.addItems(["自动网格", "1 列", "2 列", "3 列", "4 列", "主画面 + 辅画面"])
        self.layout_choice.currentIndexChanged.connect(self.set_layout)
        layout_row.addWidget(self.layout_choice)
        sidebar.addLayout(layout_row)
        self.strict = QCheckBox("严格同步：等有效画面到位")
        self.strict.toggled.connect(lambda value: setattr(self.board, "strict_sync", value))
        sidebar.addWidget(self.strict)
        self.compatibility = QCheckBox("监控兼容缓存（可选，8 GiB）")
        self.compatibility.setToolTip("只为当前播放/下一片生成无重编码封装缓存；原件不改，不全量转换。用于异常 PS 录像的流畅播放。")
        self.compatibility.toggled.connect(self.toggle_compatibility)
        sidebar.addWidget(self.compatibility)
        side.setMinimumWidth(225)
        horizontal.addWidget(side)

        right = QSplitter(Qt.Orientation.Vertical)
        top = QSplitter(Qt.Orientation.Horizontal)
        signal_panel = QWidget()
        signal_layout = QVBoxLayout(signal_panel)
        signal_layout.setContentsMargins(0, 0, 0, 0)
        imu_controls = QHBoxLayout()
        imu_controls.addWidget(QLabel("九轴位置"))
        self.imu_position = QDoubleSpinBox()
        self.imu_position.setDecimals(3)
        self.imu_position.setRange(0, 1e8)
        self.imu_position.setSuffix(" 秒")
        self.imu_position.editingFinished.connect(lambda: self.seek_imu(self.imu_position.value() * 1000))
        imu_controls.addWidget(self.imu_position)
        self.link = QCheckBox("同步跟随")
        self.link.toggled.connect(self.toggle_link)
        imu_controls.addWidget(self.link)
        self._button("钉住对应点", self.pin, imu_controls)
        signal_layout.addLayout(imu_controls)
        self.alignment_label = QLabel("设备采集时间自动定位；相机时钟偏差可人工校准")
        self.alignment_label.setWordWrap(True)
        signal_layout.addWidget(self.alignment_label)
        self.plot = self.create_plot()
        self.plot.setMinimumSize(420, 300)
        self.plot.seekRequested.connect(self.seek_imu)
        self.plot.rangeSelected.connect(self.select_range)
        self.plot.eventSelected.connect(self.select_plot_event)
        self.plot.eventChanged.connect(self.edit_plot_event)
        signal_layout.addWidget(self.plot, 1)
        top.addWidget(signal_panel)
        video_panel = QWidget()
        video_layout = QVBoxLayout(video_panel)
        video_layout.setContentsMargins(0, 0, 0, 0)
        self.board = self.create_board()
        self.board.timeChanged.connect(self.video_time_changed)
        self.board.playbackChanged.connect(self.playback_changed)
        self.board.notice.connect(self.tell)
        video_layout.addWidget(self.board, 1)
        controls = QHBoxLayout()
        self._button("−10s", lambda: self.board.seek(self.board.reference_ms - 10000), controls)
        self._button("[ 帧", lambda: self.board.step(-1), controls)
        self.play_button = self._button("播放", self.toggle_play, controls)
        self._button("帧 ]", lambda: self.board.step(1), controls)
        self._button("+10s", lambda: self.board.seek(self.board.reference_ms + 10000), controls)
        self.speed = QComboBox()
        self.speed.addItems(["0.25×", "0.5×", "1×", "2×", "4×"])
        self.speed.setCurrentIndex(2)
        self.speed.currentIndexChanged.connect(lambda i: self.board.set_rate([.25, .5, 1, 2, 4][i]))
        controls.addWidget(self.speed)
        video_layout.addLayout(controls)
        time_row = QHBoxLayout()
        self.wall_input = QLineEdit()
        self.wall_input.setPlaceholderText("2026-08-03 12:44:58")
        self.wall_input.returnPressed.connect(self.jump_wall)
        time_row.addWidget(self.wall_input, 1)
        self._button("跳转", self.jump_wall, time_row)
        self._button("下一覆盖", self.board.next_coverage, time_row)
        video_layout.addLayout(time_row)
        self.video_slider = QSlider(Qt.Orientation.Horizontal)
        self.video_slider.setRange(0, 1000000)
        self.video_slider.sliderReleased.connect(self.slider_seek)
        video_layout.addWidget(self.video_slider)
        top.addWidget(video_panel)
        top.setSizes([650, 800])
        right.addWidget(top)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.labels = QComboBox()
        # Reuse the complete v4 protocol and its original shortcuts.
        prototype = SessionWork("0" * 64)
        for i, label in enumerate(prototype.project.labels):
            self.labels.addItem(f"[{label.key}] {label.name}", i)
            if label.key and label.code != "SYNC_ANCHOR":
                shortcut = QShortcut(QKeySequence(label.key), self)
                shortcut.activated.connect(lambda index=i: self.mark(index))
        row.addWidget(self.labels, 1)
        self.mark_button = self._button("开始 / 结束视频动作", lambda: self.mark(self.labels.currentIndex()), row)
        self._button("所选九轴区间 → 候选标注", self.mark_selection, row)
        self._button("确认所选草稿为真值", self.confirm_selected, row)
        self._button("保存", self.save_current, row)
        bottom_layout.addLayout(row)
        editing = QHBoxLayout()
        self._button("编辑标签 / 边界 / 备注", self.edit_selected, editing)
        self._button("补充当前画面证据", self.update_evidence, editing)
        self._button("回看所选结束点", lambda: self.review_selected(at_end=True), editing)
        self._button("删除所选", self.delete_selected, editing)
        editing.addStretch(1)
        bottom_layout.addLayout(editing)
        self.event_status = QLabel("先看视频即可记录动作草稿，不需要先认出九轴是什么事件。")
        bottom_layout.addWidget(self.event_status)
        self.events = QTableWidget(0, 6)
        self.events.setHorizontalHeaderLabels(["类型", "标签", "开始 / 九轴秒", "结束 / 九轴秒", "状态", "备注"])
        self.events.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.events.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.events.horizontalHeader().setStretchLastSection(True)
        self.events.doubleClicked.connect(self.review_selected)
        bottom_layout.addWidget(self.events)
        right.addWidget(bottom)
        right.setSizes([720, 220])
        horizontal.addWidget(right)
        horizontal.setSizes([240, 1350])
        outer.addWidget(horizontal, 1)
        self.setCentralWidget(central)
        self.index_status = QLabel("尚未打开工程")
        self.statusBar().addWidget(self.index_status, 1)
        for key, function in (("Space", self.toggle_play), ("[", lambda: self.board.step(-1)),
                              ("]", lambda: self.board.step(1)), ("Left", lambda: self.board.seek(self.board.reference_ms - 100)),
                              ("Right", lambda: self.board.seek(self.board.reference_ms + 100)), ("0", self.pin)):
            QShortcut(QKeySequence(key), self, activated=function)
        for i in range(8):
            QShortcut(QKeySequence(f"Alt+{i + 1}"), self,
                      activated=lambda index=i: self.board.set_main(self.board.selected[index]) if index < len(self.board.selected) else None)

    def create_plot(self):
        return InteractiveSignalPlotWidget()

    def create_board(self):
        return VideoBoard()

    def tell(self, message):
        self.banner.setText(str(message))

    def choose_project(self):
        root = QFileDialog.getExistingDirectory(self, "选择包含九轴和多视角录像的工程目录")
        if root:
            self.open_project(root)

    def _retire_project(self):
        self.board.play(False)
        self.load_generation += 1
        self._continuation_pending = None
        self._continuation_second = None
        self._coverage_second = None
        self._last_scan_complete = False
        self._work_cache.clear()
        if self.worker:
            worker, catalog = self.worker, self.catalog
            worker.cancel()
            if worker.thread.is_alive():
                self.retired.append((worker, catalog))
                # Keep the QObject and connection alive until its last emission.
                worker.finished.connect(lambda w=worker, c=catalog: self._release_retired(w, c))
            else:
                catalog.close()
        elif self.catalog:
            self.catalog.close()
        self.worker = self.catalog = None
        self.board.select([])

    def _release_retired(self, worker, catalog):
        catalog.close()
        self.retired = [(w, c) for w, c in self.retired if w is not worker]

    def open_project(self, root):
        self.save_current()
        if self.dirty and self.catalog and not self.catalog.readonly:
            return
        self._retire_project()
        try:
            self.catalog = Catalog(root)
            self.settings = self.catalog.settings()
            self.active_event = self.settings.get("active_event")
            if self.active_event:
                self.active_event["assets"] = set(self.active_event["assets"])
                self.event_status.setText("已恢复上次尚未结束的视频动作；用原标签键结束，起点和证据均已保留。")
            self.current_row = self.motion = self.work = None
            self.board.reference_ms = 0
            self.source_available = False
            self.motion_cache.clear()
            self.plot.clear_data()
            self.root_label.setText("   " + str(self.catalog.root) + "   ")
            self.board.software_decode = self.settings.get("software_decode", False)
            self.layout_choice.setCurrentIndex(self.settings.get("layout", 0))
            self.strict.setChecked(self.settings.get("strict_sync", False))
            self.compatibility.setChecked(self.settings.get("compatibility", False))
            self.rows = self.catalog.rows()
            self.refresh_lists()
            if self.catalog.readonly:
                self.tell("此工程正在另一个窗口中写入，当前只读浏览；不能保存、索引或确认标注。")
            else:
                self.worker = IndexWorker(self.catalog, self)
                current = self.worker
                current.scanned.connect(lambda result, w=current: self.scan_completed(result) if w is self.worker else None)
                current.indexed.connect(lambda result, w=current: self.scan_completed(None) if w is self.worker else None)
                current.progress.connect(lambda text, w=current: self.index_status.setText(text) if w is self.worker else None)
                current.failed.connect(lambda text, w=current: self.tell("索引异常：" + text) if w is self.worker else None)
                current.start()
                self.tell("正在核对整个工程目录；已索引的素材可以先使用，新素材在后台检查。原视频和 JSON 不会被修改。")
            QSettings().setValue("workspace/last_root", str(self.catalog.root))
        except (OSError, ValueError, RuntimeError) as exc:
            self.tell("无法打开工程：" + str(exc))

    def refresh_sources(self):
        if self.worker:
            self.worker.request()
            self.tell("已请求重新核对。正在复制的文件仍需通过稳定性和可读性检查，不会强行加载。")

    def _directory_changed(self, folder):
        if self.catalog and Path(folder).is_relative_to(self.catalog.meta):
            return
        self.tell("检测到工程目录内容变化，正在合并核对；当前人工成果会保留。复制中的新文件暂不加载。")
        self._last_scan_complete = False
        self.update_coverage(force=True)
        self.debounce.start()
        self.check_active_sources()

    def scan_completed(self, result):
        if not self.catalog:
            return
        if result is not None:
            self._last_scan_complete = result.complete
            directories = [p for p in result.directories if Path(p) != self.catalog.root]
            # Directory root is watched too; ignore metadata-only notifications by
            # creating the single metadata directory before watcher registration.
            directories.insert(0, str(self.catalog.root))
            old = set(self.watcher.directories())
            remove = list(old - set(directories))
            if remove:
                self.watcher.removePaths(remove)
            additions = list(set(directories) - old)
            if additions:
                failed = self.watcher.addPaths(additions)
                if failed:
                    self.tell("部分目录不能实时监视，仍会通过定期全目录核对发现变化。")
            if result.added or result.changed or result.missing:
                self.tell(f"素材变化：新增 {len(result.added)}，变化/改名 {len(result.changed)}，原位置缺失 {len(result.missing)}。人工标注已保留。")
            if result.errors:
                self.tell("本次目录核对不完整，未据此判定删除：" + "；".join(result.errors[:2]))
        self.rows = self.catalog.rows()
        self.refresh_lists()
        counts = Counter(r["state"] for r in self.rows)
        self.index_status.setText(f"素材 {len(self.rows)} · 可用 {counts['ready']} · 待复核 {counts['review']} · 等待 {counts['pending']} · 异常 {counts['invalid']} · 缺失 {counts['missing']}")
        self.check_active_sources()
        self.update_coverage(force=True)

    def refresh_lists(self):
        current_device = self.devices.currentData() or self.settings.get("device")
        devices = sorted({r["metadata"].get("device", "未知设备") for r in self.rows if r["kind"] == "imu" and r["state"] == "ready"})
        self.devices.blockSignals(True)
        self.devices.clear()
        for device in devices:
            self.devices.addItem(device, device)
        index = self.devices.findData(current_device)
        if index >= 0:
            self.devices.setCurrentIndex(index)
        self.devices.blockSignals(False)
        self.refresh_records()
        mappings = {name: ClockMap.from_dict(value) for name, value in self.settings.get("camera_maps", {}).items()}
        intervals = intervals_from_rows([r for r in self.rows if r["kind"] == "video"], self.settings.get("camera_overrides", {}))
        timeline = VideoTimeline(intervals, mappings)
        self.board.configure(self.catalog, [r for r in self.rows if r["kind"] == "video"], timeline)
        camera_names = list(timeline.cameras)
        previous = self.board.selected or self.settings.get("device_views", {}).get(str(self.devices.currentData()), self.settings.get("selected_cameras", []))
        order = self.settings.get("camera_order", [])
        camera_names.sort(key=lambda c: (c not in order, order.index(c) if c in order else c))
        self.cameras.blockSignals(True)
        self.cameras.clear()
        for name in camera_names:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in previous else Qt.CheckState.Unchecked)
            self.cameras.addItem(item)
        if not previous and camera_names:
            for i in range(min(2, len(camera_names))):
                self.cameras.item(i).setCheckState(Qt.CheckState.Checked)
        self.cameras.blockSignals(False)
        selected = self.checked_cameras()
        if selected != self.board.selected:
            self.board.select(selected)
        if not self.board.reference_ms and timeline.bounds():
            self.board.seek(float(self.settings.get("reference_ms") or timeline.bounds()[0]))

    def refresh_records(self, *_):
        device = self.devices.currentData()
        previous = self.current_row["asset_id"] if self.current_row else self.settings.get("current_asset")
        self.records.blockSignals(True)
        self.records.clear()
        seen = set()
        selected = None
        candidates = sorted([r for r in self.rows if r["kind"] == "imu" and r["state"] == "ready"
                             and r["metadata"].get("device", "未知设备") == device],
                            key=lambda r: (r["metadata"].get("create_time_ms", 0), r["path"]))
        for row in candidates:
            if row["asset_id"] in seen:
                continue
            seen.add(row["asset_id"])
            item = QListWidgetItem(Path(row["path"]).stem)
            item.setToolTip(row["path"] + "\ncreate_time 为设备采集起点；update_time 仅为服务器收包时间")
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.records.addItem(item)
            if row["asset_id"] == previous:
                selected = item
        if selected:
            self.records.setCurrentItem(selected)
        self.records.blockSignals(False)
        if selected and self.work is None:
            self.select_record(selected)
        elif selected and self.work and not self.source_available:
            row = selected.data(Qt.ItemDataRole.UserRole)
            try:
                stamp = file_stamp(self.catalog.source_path(row["path"]))
                if stamp == row["stamp"]:
                    self.current_row, self.current_stamp, self.source_available = row, stamp, True
            except OSError:
                pass
        elif (self.work is None or self.current_row and self.current_row["metadata"].get("device") != device) and self.records.count():
            self.records.setCurrentRow(0)

    def checked_cameras(self):
        return [self.cameras.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.cameras.count())
                if self.cameras.item(i).checkState() == Qt.CheckState.Checked]

    def select_cameras(self, *_):
        selected = self.checked_cameras()
        if len(selected) > 8:
            self.tell("一次最多显示 8 路。请取消一个视角，再勾选新的。")
            self.cameras.blockSignals(True)
            for i in range(self.cameras.count()):
                item = self.cameras.item(i)
                if item.data(Qt.ItemDataRole.UserRole) not in selected[:8]:
                    item.setCheckState(Qt.CheckState.Unchecked)
            self.cameras.blockSignals(False)
            selected = selected[:8]
        self.board.select(selected)
        self.dirty = True

    def set_layout(self, index):
        self.board.columns = index if 1 <= index <= 4 else 0
        self.board.layout_mode = "main_aux" if index == 5 else "grid"
        self.board.relayout()

    def select_record(self, item, *_, follow=False):
        if not item or not self.catalog:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        if self.current_row and row["asset_id"] == self.current_row["asset_id"] and self.motion:
            if not follow and self._continuation_pending:
                self.load_generation += 1
                self._continuation_pending = None
            return
        # On first load no work has been opened yet. Saving the empty board here
        # would overwrite the project's persisted camera selection before it is
        # restored by refresh_lists (e.g. eight saved views become two defaults).
        if self.work is not None:
            self.save_current()
            if self.dirty and not self.catalog.readonly:
                return
        if not follow:
            self.board.play(False)
        self.load_generation += 1
        generation = self.load_generation
        self._continuation_pending = row["asset_id"] if follow else None
        root, asset_id = self.catalog.root, row["asset_id"]
        self.tell("正在后台读取九轴数据，保留真实采样时间及缺口…")

        def read():
            try:
                path = root / row["path"]
                before = file_stamp(path)
                if before != row["stamp"]:
                    raise ValueError("九轴文件在读取时变化，请等复制完成后刷新")
                cached = self.motion_cache.get(asset_id)
                motion = cached or load_motion_json(path)
                if file_stamp(path) != before:
                    raise ValueError("九轴文件在读取时变化，请等复制完成后刷新")
                if not self._closed:
                    self.motionReady.emit((generation, row, motion, before, follow))
            except Exception as exc:
                if not self._closed:
                    self.motionReady.emit((generation, row, None, None, follow, str(exc)))

        threading.Thread(target=read, daemon=True, name="motion-loader").start()

    def _motion_loaded(self, result):
        generation, row, motion, stamp = result[:4]
        follow = bool(result[4]) if len(result) > 4 else False
        if generation != self.load_generation or not self.catalog:
            return
        self._continuation_pending = None
        if motion is None:
            self.tell(result[5])
            self.refresh_records()
            return
        try:
            if file_stamp(self.catalog.source_path(row["path"])) != stamp:
                raise ValueError("九轴文件在读取时变化，请等复制完成后刷新")
        except (OSError, ValueError) as exc:
            self.tell(str(exc))
            self.refresh_records()
            return
        if follow:
            # The movie keeps running while IMU decoding happens in the worker.
            # Revalidate at delivery time: the clock/cow/target may have changed.
            target, _ = continuation_target([row], self.work.asset_id,
                self.current_row["metadata"].get("device"), self.work.project.cow_id,
                self.board.reference_ms, lambda asset: read_json(self.catalog.work_path(asset), None))
            if target is None:
                self.tell("下一份九轴已读取，但当前时刻或牛号不再匹配；保留视频位置，未强行切换。")
                self.refresh_records()
                return
            self.save_current()
            if self.dirty and not self.catalog.readonly:
                self.board.play(False)
                return
        self.current_row, self.motion, self.current_stamp = row, motion, stamp
        self.source_available = True
        self.motion_cache[row["asset_id"]] = motion
        while len(self.motion_cache) > 2:
            self.motion_cache.popitem(last=False)
        raw = read_json(self.catalog.work_path(row["asset_id"]), None)
        self.work = SessionWork.from_dict(raw) if raw else SessionWork(row["asset_id"])
        if not follow:
            profile = self.settings.get("device_profiles", {}).get(str(motion.device), {})
            preferences = profile.get("views", self.settings.get("device_views", {}).get(str(motion.device)))
            order = profile.get("order", [])
            if order:
                self.cameras.blockSignals(True)
                items = [self.cameras.takeItem(0) for _ in range(self.cameras.count())]
                items.sort(key=lambda item: (item.text() not in order, order.index(item.text()) if item.text() in order else item.text()))
                for item in items:
                    self.cameras.addItem(item)
                self.cameras.blockSignals(False)
            if preferences is not None:
                self.cameras.blockSignals(True)
                for i in range(self.cameras.count()):
                    item = self.cameras.item(i)
                    item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in preferences else Qt.CheckState.Unchecked)
                self.cameras.blockSignals(False)
                self.board.select(self.checked_cameras()[:8])
            self.layout_choice.setCurrentIndex(profile.get("layout", self.settings.get("layout", 0)))
            self.board.two_view_ratio = profile.get("two_view_ratio", 50)
            self.board.set_main(profile.get("main", self.settings.get("main_camera", self.board.main_camera)))
        self.work.project.source.update({"name": Path(row["path"]).name, "path": row["path"], "asset_id": row["asset_id"],
                                         "device": motion.device, "uid": motion.uid, "durationMs": motion.duration_ms,
                                         "createTimeMs": motion.create_time_ms,
                                         "create_time_semantics": "device_acquisition_start",
                                         "capture_timing": motion.capture_timing()})
        if not self.work.clock.anchors:
            self.work.clock = ClockMap.from_capture(motion, self.settings.get("timezone_offset_minutes", 480))
        elif self.work.clock.basis != "manual":
            origin = ClockMap.from_capture(motion, self.settings.get("timezone_offset_minutes", 480))
            if origin.map(0) != self.work.clock.map(0) or origin.basis != self.work.clock.basis:
                self.work.set_clock(origin)
        self.cow.setText(self.work.project.cow_id)
        self.plot.set_data([PlotSeries(**series) for series in motion.plot_series()], motion.duration_ms)
        self.plot.set_view(0, min(motion.duration_ms, 120000))
        self.imu_position.setMaximum(motion.duration_ms / 1000)
        self.imu_ms = float(self.work.progress.get("imu_ms", 0))
        if follow:
            self.imu_ms = self.work.clock.map(self.board.reference_ms, inverse=True)
        self.selection = None
        self._set_imu(self.imu_ms)
        self.link.setChecked(bool(self.work.clock.anchors))
        if self.active_event:
            if self.active_event["cow_id"] != self.work.project.cow_id:
                self.tell("当前动作属于另一牛号，已保留起点；请先结束该动作再切换标注对象。")
            else:
                self.active_event["assets"].add(row["asset_id"])
        self.refresh_events()
        self.update_alignment_text()
        if follow:
            self.tell("已自动续接下一份已校准九轴；视频位置、倍率和视角保持不变。")
        elif self.work.clock.anchors:
            self.board.seek(self.work.clock.map(self.imu_ms))
        if self.worker:
            self.worker.request("priority", [r["path"] for r in self.rows if r["kind"] == "video"
                                             and Path(row["path"]).stem[:10] in r["path"]])
        self.dirty = True
        self.update_coverage(force=True)

    def set_cow(self):
        if self.work and not self.catalog.readonly:
            value = self.cow.text().strip()
            if value != self.work.project.cow_id:
                self.work.checkpoint()
                self.work.project.cow_id = value
                for event in self.work.project.events:
                    if event.extras.get("confirmation") == "confirmed":
                        event.extras["confirmation"] = "needs_review"
                self.dirty = True
                self.save_current()

    def _set_imu(self, value):
        if not self.motion:
            return
        index = self.motion.nearest_sample_index(value)
        self.imu_ms = float(self.motion.times_ms[index])
        self.imu_position.setValue(self.imu_ms / 1000)
        self.plot.set_playhead(self.imu_ms)

    def seek_imu(self, value):
        self._set_imu(value)
        if self.linked and self.work and self.work.clock.anchors:
            self.board.seek(self.work.clock.map(self.imu_ms))
        self.dirty = True

    def toggle_link(self, enabled):
        if not enabled and self._continuation_pending:
            self.load_generation += 1
            self._continuation_pending = None
            self.refresh_records()
        if enabled and (not self.work or not self.work.clock.anchors):
            self.link.blockSignals(True)
            self.link.setChecked(False)
            self.link.blockSignals(False)
            self.tell("尚无同步点。分别拖动九轴与视频，找到对应位置后点“钉住对应点”。")
            self.linked = False
        else:
            self.linked = enabled

    def pin(self):
        if not self.writable_work():
            return
        evidence = self.board.evidence()
        current = next((e for e in evidence if e["camera"] == self.board.main_camera and e["frame_ready"]), None)
        if not current:
            self.tell("主视角实际画面尚未到位，请暂停并等待，再钉住对应点。")
            return
        try:
            self.work.calibrate(self.imu_ms, current["reference_ms"], current)
            self.link.setChecked(True)
            self.update_alignment_text()
            self.refresh_events()
            self.dirty = True
            self.save_current()
            self.tell("对应点已保存。请在较远处再次核对并钉住；切视角或换小视频不会重新建立同步。")
        except ValueError as exc:
            self.tell(str(exc))

    def edit_mapping(self):
        if not self.writable_work():
            return
        dialog = MappingDialog(self.work.clock, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.work.set_clock(dialog.value)
            self.update_alignment_text()
            self.refresh_events()
            self.dirty = True
            self.save_current()

    def edit_camera_mapping(self):
        if not self.catalog or self.catalog.readonly or not self.board.main_camera:
            return
        camera = self.board.main_camera
        maps = self.settings.setdefault("camera_maps", {})
        dialog = MappingDialog(ClockMap.from_dict(maps.get(camera, {})), self, camera=True)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings.setdefault("camera_mapping_history", []).append({"camera": camera, "mapping": maps.get(camera, {})})
            maps[camera] = dialog.value.to_dict()
            if self.work:
                for event in self.work.project.events:
                    if any(e.get("camera") == camera for e in event.extras.get("video_evidence", [])):
                        event.extras["confirmation"] = "needs_review"
            self.refresh_lists()
            self.board.seek(self.board.reference_ms)
            self.dirty = True
            self.save_current()
            self.tell("相机同步修订已保存。参考时间未变，相关已确认标注需重新复核。")

    def update_alignment_text(self):
        if not self.work:
            return
        n = len(self.work.clock.anchors)
        if self.work.clock.basis != "manual":
            self.alignment_label.setText("已按设备采集时间自动联动录像；相机时钟偏差尚未人工核对，不会自动确认标签或硬拼下一份九轴。"
                                         + (" 旧协议首帧起点为估计。" if self.work.clock.basis == "legacy_estimate" else ""))
        else:
            self.alignment_label.setText(f"人工同步锚点 {n} · {self.work.clock.quality(self.imu_ms)} · 版本 {self.work.clock.revision[:8]}；保留原有校准")

    def video_time_changed(self, value):
        if not self.wall_input.hasFocus():
            self.wall_input.setText(wall_text(value))
        bounds = self.board.timeline.bounds()
        if bounds and not self.video_slider.isSliderDown():
            fraction = max(0.0, min(1.0, (value - bounds[0]) / max(1, bounds[1] - bounds[0])))
            self.video_slider.setValue(round(fraction * 1000000))
        if self.linked and self.work and self.work.clock.anchors:
            candidate = self.work.clock.map(value, inverse=True)
            if 0 <= candidate <= self.motion.duration_ms:
                self._set_imu(candidate)
            else:
                if candidate > self.motion.duration_ms and self.board.playing:
                    self.continue_motion(value)
                else:
                    self.alignment_label.setText("当前录像时间超出这份九轴记录；可继续浏览/记视频草稿，或切换下一份记录")
        self.update_coverage()
        self.dirty = True

    def _cached_work(self, asset_id, catalog=None):
        path = (catalog or self.catalog).work_path(asset_id)
        cache_key = str(path)
        try:
            stamp = file_stamp(path)
        except OSError:
            return None
        previous = self._work_cache.get(cache_key)
        if previous is None or previous[0] != stamp:
            raw = read_json(path, None)
            # Continuation needs only identity/alignment, not every annotation.
            brief = {"project": {"cow_id": raw.get("project", {}).get("cow_id")},
                     "clock": raw.get("clock", {})} if raw else None
            previous = (stamp, brief)
            self._work_cache[cache_key] = previous
        return previous[1]

    def continue_motion(self, reference_ms):
        if self._continuation_pending or self._continuation_second == int(reference_ms / 1000):
            return
        self._continuation_second = int(reference_ms / 1000)
        self.alignment_label.setText("本份九轴已结束；正在后台查找已校准的下一份，视频保持当前位置继续播放。")
        # Large projects may have thousands of saved calibration files. Their
        # stat/read/clock lookup must not block the Qt/VLC presentation thread.
        self._continuation_pending = "search"
        generation, catalog = self.load_generation, self.catalog
        rows, asset = list(self.rows), self.work.asset_id
        device, cow = self.current_row["metadata"].get("device"), self.work.project.cow_id

        def search():
            try:
                row, reason = continuation_target(rows, asset, device, cow, reference_ms,
                    lambda target: self._cached_work(target, catalog) if generation == self.load_generation else None)
                if not self._closed:
                    self.continuationReady.emit((generation, catalog, asset, cow, row, reason))
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                if not self._closed:
                    self.continuationReady.emit((generation, catalog, asset, cow, None, str(exc)))

        threading.Thread(target=search, daemon=True, name="calibrated-imu-search").start()

    def _continuation_ready(self, result):
        generation, catalog, asset, cow, row, reason = result
        if generation != self.load_generation or catalog is not self.catalog:
            return
        self._continuation_pending = None
        if not self.work or self.work.asset_id != asset or self.work.project.cow_id != cow:
            return
        if row is None:
            message = ("存在多份重叠的已校准九轴，请选择正确记录。" if reason == "ambiguous" else
                       "本份九轴已结束；下一份的牛号/同步尚未确认或当前处于数据缺口。视频可继续，九轴未强行拼接。")
            self.alignment_label.setText(message)
            return
        for i in range(self.records.count()):
            item = self.records.item(i)
            if item.data(Qt.ItemDataRole.UserRole)["asset_id"] == row["asset_id"]:
                self.records.blockSignals(True)
                self.records.setCurrentItem(item)
                self.records.blockSignals(False)
                self.select_record(item, follow=True)
                break

    def update_coverage(self, *, force=False):
        if not self.catalog:
            return
        second = int(self.board.reference_ms / 1000)
        if not force and second == self._coverage_second:
            return
        self._coverage_second = second
        aligned = bool(self.work and self.work.clock.anchors
                       and self.work.clock.quality(self.work.clock.map(self.board.reference_ms, inverse=True)) == "interpolated")
        status = video_coverage(self.board.timeline, self.rows, self.board.reference_ms,
                               self.board.selected, scan_complete=self._last_scan_complete, aligned=aligned)
        messages = {
            "covered": "当前参考时刻有录像覆盖；确认真值仍需核对牛号、同步和画面。",
            "other_views": "当前所选视角没有覆盖，但其他视角有录像：",
            "indexing": "录像仍在索引或目录核对中，暂不能判定后续没有录像。",
            "no_videos": "当前工程没有可用录像；九轴仍可浏览，但不能确认录像真值。",
            "unresolved_video": "仍有录像时间待核验或文件异常，暂不能判定后续没有录像。",
            "alignment_unknown": "当前未找到候选录像，但九轴同步尚未确认；请先核对对应点，不能据此跳过后续数据。",
            "gap": "当前时刻处于录像缺口，后面仍有录像；下一覆盖：",
            "exhausted": "按当前时间校准，工程内后续无录像覆盖；后续九轴暂不具备录像真值。新增或核验录像后会重新检查。",
        }
        message = messages[status.code]
        if status.code == "other_views":
            message += "、".join(status.cameras)
        elif status.next_ms is not None:
            message += wall_text(status.next_ms)
        self.coverage_label.setText(message)

    def slider_seek(self):
        bounds = self.board.timeline.bounds()
        if bounds:
            self.board.seek(bounds[0] + self.video_slider.value() / 1000000 * (bounds[1] - bounds[0]))

    def jump_wall(self):
        try:
            self.board.seek(wall_ms(self.wall_input.text()))
        except ValueError:
            self.tell("时间格式应为 2026-08-03 12:44:58，也接受 2026-08-03 12_44_58")

    def toggle_play(self):
        self.board.play(not self.board.playing)

    def toggle_compatibility(self, enabled):
        self.board.compatibility = enabled
        if self.catalog:
            self.settings["compatibility"] = enabled
            if self.board.playing:
                self.board.seek(self.board.reference_ms)
            self.dirty = True
        self.tell("兼容缓存已开启：只处理当前工作素材，不重编码，容量最多 8 GiB；精确暂停仍回到原片。" if enabled else "当前使用原始录像播放。异常监控 PS 跳转不流畅时，可勾选兼容缓存。")

    def playback_changed(self, playing):
        self.play_button.setText("暂停" if playing else "播放")
        if self.worker:
            self.worker.playback_busy.set() if playing else self.worker.playback_busy.clear()

    def writable_work(self):
        if not self.work or not self.catalog or self.catalog.readonly:
            self.tell("请先选择九轴记录；只读工程不能修改人工成果。")
            return False
        return True

    def select_range(self, start, end):
        self.selection = sorted((start, end))
        self.event_status.setText(f"已选九轴区间 {self.selection[0] / 1000:.3f}–{self.selection[1] / 1000:.3f} 秒")

    def evidence(self):
        evidence = self.board.evidence()
        for item in evidence:
            item["camera_mapping_revision"] = self.settings.get("camera_maps", {}).get(item["camera"], {}).get("revision", "uncalibrated")
            row = next((r for r in self.rows if r["asset_id"] == item["asset_id"]), None)
            item["video_revision"] = self.video_revision(row) if row else None
        return evidence

    @staticmethod
    def video_revision(row):
        return hashlib.sha256(json.dumps(row["metadata"].get("intervals", []), sort_keys=True).encode()).hexdigest()

    def validate_evidence(self, evidence, *, allow_archived=False):
        usable = [e for e in evidence if e.get("frame_ready") and e.get("verified_interval")]
        if not usable:
            return False
        for item in usable:
            revision = self.settings.get("camera_maps", {}).get(item.get("camera"), {}).get("revision", "uncalibrated")
            if item.get("camera_mapping_revision") != revision:
                return False
            candidates = [r for r in self.rows if r["asset_id"] == item.get("asset_id") and r["state"] in {"ready", "review"}]
            valid = False
            for row in candidates:
                try:
                    valid |= file_stamp(self.catalog.source_path(row["path"])) == row["stamp"] and self.video_revision(row) == item.get("video_revision")
                except OSError:
                    pass
            if not valid and allow_archived:
                from .archive import archived_asset
                # Only previously confirmed training exports may use an audited
                # offline source. This never authorizes confirming a new draft.
                matching = [r for r in self.rows if r["asset_id"] == item.get("asset_id")]
                missing = bool(matching) and all(not self.catalog.source_path(r["path"]).exists() for r in matching)
                valid = (missing and archived_asset(self.settings.get("video_archive", {}), item["asset_id"])
                         and any(self.video_revision(r) == item.get("video_revision") for r in matching))
            if not valid:
                return False
        return True

    def mark(self, index):
        if not self.writable_work():
            return
        label = self.work.project.labels[index]
        if label.code == "SYNC_ANCHOR":
            self.pin()
            return
        evidence = self.evidence()
        main = next((e for e in evidence if e["camera"] == self.board.main_camera and e["frame_ready"]), None)
        if not main:
            self.tell("请等主视角画面到位后记录动作，避免把加载中的旧画面当成证据。")
            return
        value = main["reference_ms"]
        if label.is_point:
            self.work.add_draft(index, value, None, evidence)
        elif self.active_event is None:
            self.active_event = {"label": index, "start": value, "evidence": evidence,
                                 "group_id": uuid.uuid4().hex, "assets": {self.work.asset_id},
                                 "cow_id": self.work.project.cow_id}
            self.event_status.setText(f"正在记录：{label.name} · 起点 {wall_text(value)} · 换小视频不会中断，再按同一键结束")
            self.dirty = True
            self.save_current()
            return
        else:
            if self.active_event["label"] != index:
                self.tell("请先用原标签键结束正在记录的动作，再开始另一个区间。点事件可单独记录。")
                return
            active = self.active_event
            if active["cow_id"] != self.work.project.cow_id:
                self.tell("正在记录的动作牛号与当前记录不一致，请切回原记录结束。")
                return
            try:
                self.snapshot_writer.flush()
            except (OSError, ValueError) as exc:
                self.dirty = True
                self.tell("保存失败，内存中的成果仍保留，请勿关闭：" + str(exc))
                return
            for asset_id in active["assets"]:
                target = self.work if asset_id == self.work.asset_id else SessionWork.from_dict(read_json(self.catalog.work_path(asset_id)))
                target.add_draft(index, active["start"], value, active["evidence"] + evidence, group_id=active["group_id"])
                atomic_json(self.catalog.work_path(asset_id), target.to_dict())
            self.active_event = None
            self.event_status.setText("动作已保存为视频草稿；同步核对后可确认对应九轴范围。")
        self.refresh_events()
        self.dirty = True
        self.save_current()

    def mark_selection(self):
        if not self.writable_work() or not self.selection:
            return
        if not self.work.clock.anchors:
            self.tell("九轴所选区间尚无视频对应关系，请先钉住同步点；也可以直接记录视频动作草稿。")
            return
        start, end = (self.work.clock.map(value) for value in self.selection)
        self.work.add_draft(self.labels.currentIndex(), start, end, self.evidence())
        self.refresh_events()
        self.dirty = True
        self.save_current()

    def refresh_events(self):
        if not self.work:
            self.events.setRowCount(0)
            return
        entries = [("draft", draft) for draft in self.work.drafts if draft.get("confirmation") != "confirmed"]
        entries += [("event", event) for event in self.work.project.events]
        self.events.setRowCount(len(entries))
        for i, (kind, entry) in enumerate(entries):
            if kind == "draft":
                label = self.work.project.labels[entry["label_index"]]
                values = ["视频草稿", label.name, wall_text(entry["reference_start"]),
                          wall_text(entry["reference_end"]) if entry["reference_end"] is not None else "点事件", "九轴待确认", entry.get("note", "")]
                identifier = entry["id"]
            else:
                label = self.work.project.labels[entry.li]
                values = ["九轴标注", label.name, f"{entry.t0 / 1000:.3f}",
                          f"{entry.t1 / 1000:.3f}" if entry.t1 is not None else "点事件",
                          entry.extras.get("confirmation", "legacy_unreviewed"), entry.note]
                identifier = entry.id
            for j, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                item.setData(Qt.ItemDataRole.UserRole, (kind, identifier))
                self.events.setItem(i, j, item)
        self.plot.set_events([label.to_dict() for label in self.work.project.labels], [e.to_dict() for e in self.work.project.events])

    def selected_entry(self):
        item = self.events.item(self.events.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def confirm_selected(self):
        if not self.writable_work():
            return
        selected = self.selected_entry()
        if not selected:
            self.tell("请先在表格中选择一条视频草稿或待复核标注")
            return
        kind, identifier = selected
        try:
            if kind == "event":
                event = next(e for e in self.work.project.events if e.id == identifier)
                identifier = event.extras.get("draft_id")
                if identifier is None:
                    raise ValueError("旧标注没有视频草稿锚点，请先回看并建立视频草稿，不能直接当成已校准真值")
            self.check_active_sources()
            confirmed = self.work.confirm_draft(identifier, self.motion.duration_ms, source_available=self.source_available,
                                                evidence_validator=self.validate_evidence)
            self.refresh_events()
            self.dirty = True
            self.save_current()
            self.tell("已确认九轴真值，保留视频资产、真实样本范围和同步版本。")
            if self.isVisible():
                self.capture_evidence(event=confirmed)
        except (ValueError, StopIteration) as exc:
            self.tell(str(exc))

    def review_selected(self, *_, at_end=False):
        if not self.work:
            return
        selected = self.selected_entry()
        if not selected:
            return
        kind, identifier = selected
        self.board.play(False)
        if kind == "draft":
            draft = next(d for d in self.work.drafts if d["id"] == identifier)
            self.board.seek(draft["reference_end"] if at_end and draft["reference_end"] is not None else draft["reference_start"])
        else:
            event = next(e for e in self.work.project.events if e.id == identifier)
            self.plot.focus_event(event.id)
            self.seek_imu(event.t1 if at_end and event.t1 is not None else event.t0)

    def edit_selected(self):
        if not self.writable_work() or not self.selected_entry():
            return
        kind, identifier = self.selected_entry()
        entry = next(d for d in self.work.drafts if d["id"] == identifier) if kind == "draft" else next(e for e in self.work.project.events if e.id == identifier)
        index = entry["label_index"] if kind == "draft" else entry.li
        start, end = (entry["reference_start"], entry["reference_end"]) if kind == "draft" else (entry.t0, entry.t1)
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑标注（修改后需复核）")
        layout = QVBoxLayout(dialog)
        labels = QComboBox()
        for label in self.work.project.labels:
            labels.addItem(label.name)
        labels.setCurrentIndex(index)
        layout.addWidget(labels)
        layout.addWidget(QLabel("视频草稿填日期时间；九轴标注填相对秒。点事件不填结束。"))
        begin = QLineEdit(wall_text(start) if kind == "draft" else str(start / 1000))
        finish = QLineEdit((wall_text(end) if kind == "draft" else str(end / 1000)) if end is not None else "")
        note = QLineEdit(entry.get("note", "") if kind == "draft" else entry.note)
        note.setPlaceholderText("备注")
        for control in (begin, finish, note):
            layout.addWidget(control)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(dialog.accept)
        box.rejected.connect(dialog.reject)
        layout.addWidget(box)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            parse = wall_ms if kind == "draft" else lambda value: float(value) * 1000
            start = parse(begin.text())
            end = None if self.work.project.labels[labels.currentIndex()].is_point else parse(finish.text())
            if kind == "draft":
                self.work.checkpoint()
                if end is not None and start > end:
                    start, end = end, start
                entry.update(label_index=labels.currentIndex(), reference_start=start, reference_end=end,
                             note=note.text(), video_evidence=[], confirmation="video_draft")
            else:
                self.work.edit_event(identifier, start, end, self.motion.duration_ms, label_index=labels.currentIndex(), note=note.text())
            self.dirty = True
            self.refresh_events()
            self.save_current()
        except ValueError as exc:
            self.tell(str(exc))

    def update_evidence(self):
        if not self.writable_work() or not self.selected_entry():
            return
        kind, identifier = self.selected_entry()
        if kind == "event":
            event = next(e for e in self.work.project.events if e.id == identifier)
            identifier = event.extras.get("draft_id")
        draft = next((d for d in self.work.drafts if d["id"] == identifier), None)
        fresh = self.evidence()
        if not draft or not self.validate_evidence(fresh):
            self.tell("请回看对应动作并等画面到位；旧标注需先建立视频草稿。")
            return
        self.work.checkpoint()
        draft["video_evidence"] = [e for e in draft["video_evidence"] if self.validate_evidence([e])] + fresh
        if kind == "event":
            event.extras["confirmation"] = "needs_review"
        self.dirty = True
        self.save_current()
        self.tell("已补充当前画面证据；区间动作请核对起止两端后再确认。")

    def capture_evidence(self, *, event=None):
        if not self.writable_work():
            return
        if event is None:
            selected = self.selected_entry()
            if not selected or selected[0] != "event":
                self.tell("请先选择一条已确认标注；视频草稿需先核对同步并确认。")
                return
            event = next(e for e in self.work.project.events if e.id == selected[1])
        if event.extras.get("confirmation") != "confirmed":
            self.tell("该标签尚未确认或需要复核；请先完成视频真值核对。")
            return
        from .evidence_ui import CaptureDialog
        if self._capture_dialog is not None:
            if self._capture_dialog.future is not None and not self._capture_dialog.future.done():
                self.tell("上一组截图还在处理，请等待完成。")
                return
            self._capture_dialog.close()
            self._capture_dialog.deleteLater()
        self._capture_dialog = CaptureDialog(self, event)
        self._capture_dialog.show()

    def verify_video_archive(self):
        if not self.catalog or self.catalog.readonly:
            self.tell("请先打开可写数据工程。")
            return
        from .evidence_ui import ArchiveDialog
        if self._archive_dialog is not None:
            if self._archive_dialog.future is not None and not self._archive_dialog.future.done():
                self._archive_dialog.raise_()
                return
            self._archive_dialog.close()
            self._archive_dialog.deleteLater()
        self.save_current()
        self.board.play(False)
        self._archive_dialog = ArchiveDialog(self)
        self._archive_dialog.show()

    def select_plot_event(self, identifier):
        for i in range(self.events.rowCount()):
            if self.events.item(i, 0).data(Qt.ItemDataRole.UserRole) == ("event", identifier):
                self.events.selectRow(i)
                break

    def edit_plot_event(self, identifier, start, end):
        if not self.writable_work():
            return
        event = next((e for e in self.work.project.events if e.id == identifier), None)
        if event:
            self.work.edit_event(identifier, start, end, self.motion.duration_ms)
            self.refresh_events()
            self.dirty = True
            self.save_current()

    def delete_selected(self):
        if not self.writable_work() or not self.selected_entry():
            return
        if QMessageBox.question(self, "删除标注", "删除所选条目？可以撤销，保存前一版本也会保留在备份中。") != QMessageBox.StandardButton.Yes:
            return
        kind, identifier = self.selected_entry()
        self.work.checkpoint()
        if kind == "draft":
            self.work.drafts = [d for d in self.work.drafts if d["id"] != identifier]
        else:
            removed = next(e for e in self.work.project.events if e.id == identifier)
            self.work.drafts = [d for d in self.work.drafts if d["id"] != removed.extras.get("draft_id")]
            self.work.project.events = [e for e in self.work.project.events if e.id != identifier]
        self.refresh_events()
        self.dirty = True
        self.save_current()

    def undo(self, redo=False):
        if self.writable_work() and self.work.undo_once(redo=redo):
            self.refresh_events()
            self.update_alignment_text()
            self.dirty = True
            self.save_current()

    def check_active_sources(self):
        if not self.catalog:
            return
        if self.current_row and self.source_available:
            try:
                unchanged = file_stamp(self.catalog.source_path(self.current_row["path"])) == self.current_stamp
            except OSError:
                unchanged = False
            if not unchanged:
                self.source_available = False
                self.board.play(False)
                self.tell("当前九轴源文件已变化/移走，已暂停同步；缓存波形和人工成果保留，等待刷新确认新版本。")
        by_path = {r["path"]: r for r in self.rows}
        for tile in self.board.tiles.values():
            if not tile.interval:
                continue
            row = by_path.get(tile.interval.path)
            try:
                unchanged = row and row["asset_id"] == tile.asset_id and row["state"] in {"ready", "review"} and file_stamp(self.catalog.source_path(row["path"])) == row["stamp"]
            except OSError:
                unchanged = False
            if not unchanged:
                self.board.blocked_assets.add(tile.asset_id)
                self.board._pause_tile(tile)
                tile.pending = None
                tile.interval = None
                tile.surface.hide()
                tile.stack.hide()
                tile.status("源录像已变化/移走，暂停本路；原标注保留")
                self.tell("检测到正在使用的录像变化，已暂停对应画面；请刷新素材确认，其他视角仍可使用。")

    def save_current(self, *_, background=False):
        if not self.catalog or self.catalog.readonly:
            return
        try:
            if background:
                if not self.snapshot_writer.poll():
                    return
            else:
                self.snapshot_writer.flush()
            snapshot = []
            if self.work:
                self.work.progress.update(imu_ms=self.imu_ms, reference_ms=self.board.reference_ms)
                snapshot.append((self.catalog.work_path(self.work.asset_id), copy.deepcopy(self.work.to_dict())))
            self.settings.update(device=self.devices.currentData(), current_asset=self.work.asset_id if self.work else None,
                                 reference_ms=self.board.reference_ms, selected_cameras=self.board.selected,
                                 camera_order=[self.cameras.item(i).text() for i in range(self.cameras.count())],
                                 layout=self.layout_choice.currentIndex(), strict_sync=self.strict.isChecked(),
                                 main_camera=self.board.main_camera)
            device = self.current_row["metadata"].get("device") if self.current_row else self.devices.currentData()
            self.settings.setdefault("device_views", {})[str(device)] = list(self.board.selected)
            self.settings.setdefault("device_profiles", {})[str(device)] = {
                "views": list(self.board.selected), "main": self.board.main_camera,
                "order": [self.cameras.item(i).text() for i in range(self.cameras.count())],
                "layout": self.layout_choice.currentIndex(), "two_view_ratio": self.board.two_view_ratio}
            self.settings["active_event"] = {**self.active_event, "assets": sorted(self.active_event["assets"])} if self.active_event else None
            snapshot.append((self.catalog.meta / "project.json", copy.deepcopy(self.settings)))
            if background:
                self.snapshot_writer.submit(snapshot)
            else:
                self.snapshot_writer.write(snapshot)
            self.dirty = False
        except (OSError, ValueError) as exc:
            self.dirty = True
            self.tell("保存失败，内存中的成果仍保留，请勿关闭：" + str(exc))

    def auto_save(self):
        try:
            available = self.snapshot_writer.poll()
        except (OSError, ValueError) as exc:
            self.dirty = True
            self.tell("保存失败，内存中的成果仍保留，请勿关闭：" + str(exc))
            return
        if self.dirty and available:
            self.save_current(background=True)

    def source_manager(self):
        if not self.catalog:
            self.tell("请先打开数据工程")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("素材索引 · 重名按批次/内容身份区分")
        dialog.resize(1150, 650)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(len(self.rows), 5)
        table.setHorizontalHeaderLabels(["相对路径", "类型", "状态", "开始时间 / 设备", "说明"])
        table.setColumnWidth(0, 360)
        table.horizontalHeader().setStretchLastSection(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        snapshot = list(self.rows)
        for i, row in enumerate(snapshot):
            values = [row["path"], row["kind"], row["state"], row["metadata"].get("start_display") or row["metadata"].get("device", ""),
                      row["error"] or row["metadata"].get("reason") or "; ".join(row["metadata"].get("warnings", []))]
            for j, value in enumerate(values):
                table.setItem(i, j, QTableWidgetItem(str(value)))
        layout.addWidget(table)
        buttons = QHBoxLayout()
        self._button("核验所选视频时间 / 框选 ROI", lambda: self.edit_source(snapshot[table.currentRow()]) if table.currentRow() >= 0 else None, buttons)
        self._button("重新建立所选视频索引", lambda: self.worker.request("recheck", snapshot[table.currentRow()]["path"]) if self.worker and table.currentRow() >= 0 else None, buttons)
        layout.addLayout(buttons)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(dialog.reject)
        layout.addWidget(box)
        dialog.exec()

    def edit_source(self, row):
        if row["kind"] != "video" or row["state"] not in {"ready", "review"} or self.catalog.readonly:
            self.tell("请先等待视频通过可读性检查，再核验时间")
            return
        dialog = SourceTimeDialog(self.catalog, row, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        metadata = copy.deepcopy(row["metadata"])
        metadata["manual_readings"] = dialog.readings
        metadata["roi"] = dialog.canvas.roi
        self.settings.setdefault("camera_overrides", {})[row["asset_id"]] = dialog.camera.text().strip()
        if dialog.readings:
            readings = sorted(dialog.readings, key=lambda r: r["media_ms"])
            intervals = []
            if len(readings) == 1:
                start = readings[0]["wall_ms"] - readings[0]["media_ms"]
                intervals.append({"wall_start": start, "wall_end": start + metadata["duration_ms"], "media_start": 0,
                                  "media_end": metadata["duration_ms"], "verified": False, "warnings": ["单个人工读数，只可粗定位"]})
            else:
                for left, right in zip(readings, readings[1:]):
                    intervals.append({"wall_start": left["wall_ms"], "wall_end": right["wall_ms"],
                                      "media_start": left["media_ms"], "media_end": right["media_ms"],
                                      "verified": True, "warnings": ["人工确认两端；中间仍应抽查"]})
            metadata["intervals"] = intervals
            metadata["needs_review"] = len(readings) < 2
        self.catalog.update_metadata(row["asset_id"], metadata)
        # Manual work is also outside the rebuildable DB.
        atomic_json(self.catalog.meta / "video_corrections" / (row["asset_id"] + ".json"),
                    {"asset_id": row["asset_id"], "readings": dialog.readings, "roi": dialog.canvas.roi,
                     "camera": dialog.camera.text().strip(), "intervals": metadata.get("intervals", [])})
        self.save_current()
        self.scan_completed(None)

    def new_batch(self):
        if not self.catalog or self.catalog.readonly:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择工程中的录像设备目录", str(self.catalog.root))
        if not folder:
            return
        camera = Path(folder).resolve()
        if not camera.is_relative_to(self.catalog.root) or camera.is_relative_to(self.catalog.meta):
            self.tell("请在当前数据工程中选择设备目录")
            return
        name, ok = QInputDialog.getText(self, "新增拷贝批次", "批次名，例如 2026-08-08_第二次导出；若重名会自动加编号")
        if ok:
            try:
                path = unique_batch(camera, name)
                self.tell(f"已创建空批次：{path}。请把本次完整导出内容复制进去，保留原文件名及配套文件，不覆盖旧批次。")
            except (OSError, ValueError) as exc:
                self.tell(str(exc))

    def open_candidates(self):
        if not self.writable_work():
            return
        from .candidate_window import CandidateWindow
        try:
            if self._candidate_window is None:
                self._candidate_window = CandidateWindow(self)
            self._candidate_window.refresh_results()
            self._candidate_window.show()
            self._candidate_window.raise_()
        except (OSError, ValueError) as exc:
            self.tell(str(exc))

    def export_work(self, *, snippet=False):
        if not self.work or not self.motion or not self.catalog or self._export_running:
            return
        if snippet and not self.selection:
            self.tell("请先在九轴波形上拖选要保存的片段")
            return
        from .label_file import build_label_file, save_label_file
        suffix = ".片段.标注.json" if snippet else ".标注.json"
        range_name = f"_{min(self.selection) / 1000:.3f}-{max(self.selection) / 1000:.3f}s" if snippet else ""
        name = self.motion.source_path.stem + "_" + self.work.asset_id[:8] + range_name + suffix
        path, _ = QFileDialog.getSaveFileName(self, "保存一个标注文件（可放在任意位置）", name, "JSON (*.json)")
        if not path:
            return
        work = SessionWork.from_dict(copy.deepcopy(self.work.to_dict()))
        motion, root, evidence_root = self.motion, self.catalog.root, self.catalog.meta
        rows, settings = copy.deepcopy(self.rows), copy.deepcopy(self.settings)
        settings["selected_cameras"] = self.checked_cameras()
        selection = copy.deepcopy(self.selection) if snippet else None
        protected = [self.catalog.source_path(r["path"]) for r in self.rows]
        protected.append(self.catalog.work_path(self.work.asset_id))
        self._export_running = True
        self.tell("正在后台保存单个标注文件；原始九轴和录像不会改写…")

        def save():
            try:
                document = build_label_file(work, motion, root, rows, settings, selection=selection)
                save_label_file(path, document, protected=protected, evidence_root=evidence_root)
                message = "标注已保存：" + path + "。已留存的截图一并复制到同级“证据”文件夹；移动成果时请一起带走。"
            except (OSError, ValueError, KeyError, TypeError) as exc:
                message = "标注导出失败：" + str(exc)
            if not self._closed:
                self.exportReady.emit(message)

        threading.Thread(target=save, name="annotation-export", daemon=False).start()

    def _export_finished(self, message):
        self._export_running = False
        self.tell(message)

    def open_history(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开历史标注回看", filter="JSON (*.json)")
        if not path:
            return
        from .history_window import HistoryWindow
        # Do not overlay history playback on eight actively decoding streams.
        self.board.play(False)
        self._history_windows = [w for w in self._history_windows if not w.disposed]
        if self._history_windows:
            window = self._history_windows[0]
            if window.future is not None:
                self.tell("历史回看仍在核对来源，请等待完成")
                return
            window.path = Path(path)
            window.setWindowTitle("COWMATA · 历史标注回看（只读） · " + window.path.name)
            window.begin_load(self.catalog.root if self.catalog else None)
        else:
            window = HistoryWindow(path, self.catalog.root if self.catalog else None, reusable=True)
            self._history_windows.append(window)
        window.show()
        window.raise_()

    def export_training(self):
        if not self.work or not self.catalog:
            return
        directory = QFileDialog.getExistingDirectory(self, "选择导出位置")
        if not directory:
            return
        try:
            self.check_active_sources()
            project = self.work.training_project(source_available=self.source_available,
                                                 evidence_validator=lambda e: self.validate_evidence(e, allow_archived=True))
            output = unique_batch(Path(directory), "COWMATA_" + self.work.asset_id[:8])
            atomic_json(output / "全部人工成果.json", self.work.to_dict())
            save_project(self.work.project, output / "兼容单视频工程.json")
            export_events_csv(project, output / "已确认事件.csv", relative_timestamps_ms=self.motion.times_ms)
            with (output / "参考时间与视频证据.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["event_id", "cow_id", "imu_asset_sha256", "imu_start_ms", "imu_end_ms", "reference_start", "reference_end", "mapping_revision", "video_evidence_json"])
                for event in project.events:
                    writer.writerow([event.id, project.cow_id, self.work.asset_id, event.t0, event.t1,
                                     wall_text(self.work.clock.map(event.t0)), wall_text(self.work.clock.map(event.t1)) if event.t1 is not None else "",
                                     self.work.clock.revision, json.dumps(event.extras.get("video_evidence", []), ensure_ascii=False)])
            export_sample_multihot_csv(project, output / "逐样本标签.csv", self.motion.times_ms)
            export_boris_csv(project, output / "BORIS.csv")
            export_meta_json(project, output / "训练元数据.json", data_meta=self.motion.quality_report())
            self.tell(f"已导出到 {output}。未确认/未观看样本不能作为负样本；逐样本表保留 reviewed_any 标识。")
        except (OSError, ValueError, TypeError) as exc:
            self.tell("人工成果可保留；训练真值导出被阻止或未完成：" + str(exc))

    def import_legacy(self):
        if not self.writable_work():
            return
        path, _ = QFileDialog.getOpenFileName(self, "导入旧单视频标注工程", filter="JSON (*.json)")
        if path:
            try:
                legacy = load_project(path)
                if QMessageBox.question(self, "核对旧工程来源", "导入会替换当前记录的标签工程并清空同步关系；请确认旧工程确实对应当前九轴记录。当前成果会保留备份且可撤销，继续？") != QMessageBox.StandardButton.Yes:
                    return
                self.work.checkpoint()
                source = dict(self.work.project.source)
                for event in legacy.events:
                    event.extras["confirmation"] = "legacy_unreviewed"
                self.work.project = legacy
                self.work.project.extras["legacy_source"] = dict(legacy.source)
                self.work.project.source = source
                self.work.project.source["asset_id"] = self.work.asset_id
                self.work.drafts = []
                self.work.clock = ClockMap()
                self.refresh_events()
                self.dirty = True
                self.save_current()
                self.tell("旧工程已导入，未自动继承不明同步关系；请核对源记录、牛号与视频后确认。原文件未改。")
            except (OSError, ValueError) as exc:
                self.tell(str(exc))

    def open_legacy(self):
        from cowmata_tailring.app.basic_window import MainWindow as LegacyWindow
        window = LegacyWindow()
        self._legacy_windows.append(window)
        window.show()

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def set_two_view_ratio(self):
        ratio, ok = QInputDialog.getInt(self, "双画面主视角宽度", "主视角宽度百分比（两路横排时生效）", self.board.two_view_ratio, 20, 80, 5)
        if ok:
            self.board.two_view_ratio = ratio
            self.board.relayout()
            self.dirty = True
            self.save_current()

    def toggle_decode(self):
        self.settings["software_decode"] = not self.settings.get("software_decode", False)
        self.save_current()
        self.tell("解码偏好已保存，请重新打开工程后生效；原视频不转码。")

    def diagnostics(self):
        values = sorted(x["seconds"] for x in self.board.latencies)
        p95 = values[min(len(values) - 1, int(len(values) * .95))] if values else None
        text = f"当前显示 {len(self.board.selected)} 路；已分配播放器 {sum(t.engine is not None for t in self.board.pool)} / 9\n"
        text += f"已确认定位 {len(values)} 次；P95 {p95:.3f} 秒\n" if p95 is not None else "尚无已确认的画面定位统计\n"
        text += "画面准备需同时满足视频输出、实际显示帧增长及解码时钟接近。八路原画性能受显卡、硬盘和倍率影响。"
        text += "\nDecode preference: " + ("CPU" if self.board.software_decode else "GPU auto / driver fallback")
        text += "\nOriginal-frame RAM cache: " + str(self.board.frame_cache.stats())
        QMessageBox.information(self, "性能与索引诊断", text)
        if self.catalog and not self.catalog.readonly:
            atomic_json(self.catalog.meta / "playback_metrics.json", self.board.latencies)

    def closeEvent(self, event):
        for dialog in (self._capture_dialog, self._archive_dialog):
            if dialog is not None and dialog.future is not None and not dialog.future.done():
                event.ignore()
                self.tell("证据图片或归档核验正在处理，请完成或取消后再关闭。")
                return
        if self._candidate_window is not None and self._candidate_window.running:
            self._candidate_window.cancel()
            self.tell("正在取消后台预测，结束后自动关闭；人工成果将正常保存。")
            event.ignore()
            QTimer.singleShot(300, self.close)
            return
        if self._export_running:
            event.ignore()
            self.tell("标注文件正在保存，请等待完成后再关闭。")
            return
        self.save_current()
        if self.dirty and self.catalog and not self.catalog.readonly:
            event.ignore()
            self.tell("人工成果尚未成功保存，请先处理保存错误再关闭。")
            return
        self._closed = True
        if self._candidate_window is not None:
            self._candidate_window.cancel()
            self._candidate_window.timer.stop()
        self.snapshot_writer.close()
        for history in self._history_windows:
            history.dispose()
        self.load_generation += 1
        self.save_timer.stop()
        self.source_timer.stop()
        self.board.close()
        if self.worker:
            self.worker.cancel()
        event.accept()
