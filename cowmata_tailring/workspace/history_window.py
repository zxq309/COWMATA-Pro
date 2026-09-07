"""Independent read-only history viewer. Never imports over active human work."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from cowmata_tailring.ui.widgets import PlotSeries

from .catalog import file_stamp
from .clocks import wall_text
from .evidence import context_matches
from .evidence_ui import EvidenceGallery
from .label_file import contained, load_history
from .materials import GLASS_STYLE, FrostedCanvas, apply_mica
from .presentation import PresentationVideoBoard
from .signal_panel import SignalPanel
from .theme import STYLE


class HistoryWindow(QMainWindow):
    def __init__(self, path, root=None, *, board_factory=PresentationVideoBoard, reusable=False):
        super().__init__()
        self.path = Path(path)
        self.data = None
        self.closed = False
        self.disposed = False
        self.reusable = reusable
        self.cancellation = threading.Event()
        self.loader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="annotation-history")
        self.future = None
        self.cache = tempfile.TemporaryDirectory(prefix="cowmata-history-")
        self.setWindowTitle("COWMATA · 历史标注回看（只读） · " + self.path.name)
        self.resize(1400, 900)
        self.setStyleSheet(STYLE + GLASS_STYLE)
        canvas = FrostedCanvas()
        outer = QVBoxLayout(canvas)
        bar = QHBoxLayout()
        title = QLabel("历史回看 · 原始标注不会被改写")
        title.setObjectName("sectionTitle")
        bar.addWidget(title, 1)
        self.relink = QPushButton("重新选择数据工程…")
        self.relink.clicked.connect(self.choose_root)
        bar.addWidget(self.relink)
        self.archive_link = QPushButton("连接归档录像…")
        self.archive_link.clicked.connect(self.choose_archive)
        bar.addWidget(self.archive_link)
        self.media_mode = QComboBox()
        self.media_mode.addItems(["原录像", "留存证据图"])
        self.media_mode.currentIndexChanged.connect(self.switch_media)
        bar.addWidget(self.media_mode)
        self.view = QComboBox()
        self.view.addItems(["主画面 + 辅画面", "自动网格"])
        self.view.currentIndexChanged.connect(lambda i: self.board.set_presentation("B" if i else "A"))
        bar.addWidget(self.view)
        self.policy = QComboBox()
        self.policy.addItems(["八路全速", "主路优先 · 辅路预览"])
        self.policy.currentIndexChanged.connect(lambda i: self.board.set_policy("balanced" if i else "full"))
        bar.addWidget(self.policy)
        outer.addLayout(bar)
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        outer.addWidget(self.banner)
        horizontal = QSplitter(Qt.Orientation.Horizontal)
        side = QWidget()
        sidebar = QVBoxLayout(side)
        sidebar.addWidget(QLabel("标注与视频草稿"))
        self.events = QListWidget()
        self.events.setWordWrap(True)
        self.events.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.events.currentItemChanged.connect(self.review_item)
        sidebar.addWidget(self.events, 2)
        sidebar.addWidget(QLabel("勾选 1–8 个视角"))
        self.cameras = QListWidget()
        self.cameras.itemChanged.connect(self.select_cameras)
        sidebar.addWidget(self.cameras, 1)
        side.setMinimumWidth(240)
        horizontal.addWidget(side)
        panes = QSplitter(Qt.Orientation.Vertical)
        self.board = board_factory()
        self.board.notice.connect(self.statusBar().showMessage)
        self.board.policyChanged.connect(lambda policy: self.policy.setCurrentIndex(1 if policy == "balanced" else 0))
        self.board.timeChanged.connect(self.video_time)
        self.board.playbackChanged.connect(lambda playing: self.play_button.setText("暂停" if playing else "播放"))
        self.media_pages = QStackedWidget()
        self.media_pages.addWidget(self.board)
        self.evidence_gallery = EvidenceGallery()
        self.media_pages.addWidget(self.evidence_gallery)
        panes.addWidget(self.media_pages)
        self.plot = SignalPanel()
        self.plot.wave.event_editable = False
        self.plot.track.setToolTip("只读回看：单击标签定位，不能拖动修改历史边界")
        self.plot.seekRequested.connect(self.seek)
        self.plot.eventSelected.connect(self.review_event)
        panes.addWidget(self.plot)
        panes.setSizes([570, 250])
        horizontal.addWidget(panes)
        horizontal.setSizes([280, 1100])
        outer.addWidget(horizontal, 1)
        transport = QHBoxLayout()
        self.play_button = QPushButton("播放")
        self.play_button.clicked.connect(self.toggle_play)
        transport.addWidget(self.play_button)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 10000)
        self.slider.sliderReleased.connect(self.slider_seek)
        transport.addWidget(self.slider, 1)
        self.position = QLabel("")
        transport.addWidget(self.position)
        outer.addLayout(transport)
        self.setCentralWidget(canvas)
        QShortcut(QKeySequence("Space"), self, activated=self.toggle_play)
        QShortcut(QKeySequence("Esc"), self, activated=lambda: self.board.enlarge(None))
        self.poll = QTimer(self)
        self.poll.setInterval(100)
        self.poll.timeout.connect(self.poll_load)
        self.poll.start()
        self.source_check = QTimer(self)
        self.source_check.setInterval(2000)
        self.source_check.timeout.connect(self.check_sources)
        self.source_check.start()
        self.begin_load(root)
        apply_mica(int(self.winId()))

    def begin_load(self, root):
        self.closed = False
        self.cancellation = threading.Event()
        self.source_check.start()
        self.board.play(False)
        self.board.select([])
        self.data = None
        self.evidence_gallery.set_bundle(None)
        self.plot.clear_data()
        self.events.clear()
        self.cameras.clear()
        self.banner.setText("正在后台核对标注来源与录像身份…")
        self.play_button.setEnabled(False)
        self.relink.setEnabled(False)
        self.archive_link.setEnabled(False)
        self.future = self.loader.submit(load_history, self.path, root, cancelled=self.cancellation.is_set)

    def choose_root(self):
        root = QFileDialog.getExistingDirectory(self, "选择包含原始九轴与录像的数据工程")
        if root:
            self.begin_load(root)

    def choose_archive(self):
        hint = self.data.document.get("video", {}).get("archive", {}).get("archive_root_hint", "") if self.data else ""
        root = QFileDialog.getExistingDirectory(self, "选择录像归档目录（按内容身份核验，不改写归档）", hint)
        if root:
            self.begin_load(root)

    def switch_media(self, index):
        if not hasattr(self, "media_pages"):
            return
        self.board.play(False)
        self.media_pages.setCurrentIndex(index)
        self.play_button.setEnabled(index == 0 and bool(self.data and self.data.timeline.intervals))

    def poll_load(self):
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        self.relink.setEnabled(True)
        self.archive_link.setEnabled(True)
        try:
            data = future.result()
            if not self.closed:
                self.apply_data(data)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            self.banner.setText("历史标注读取失败：" + str(exc))

    def apply_data(self, data):
        self.data = data
        self.banner.setText("；".join(data.warnings) or "只读回看：标签位置采用原始九轴时间，录像使用已保存的校准版本。")
        if data.motion:
            self.plot.set_data([PlotSeries(**s) for s in data.motion.plot_series()], data.motion.duration_ms)
            self.plot.set_view(*self.bounds())
        self.plot.set_events([label.to_dict() for label in data.work.project.labels], [e.to_dict() for e in data.work.project.events])
        for event in data.work.project.events:
            label = data.work.project.labels[event.li].name if 0 <= event.li < len(data.work.project.labels) else str(event.li)
            ending = "" if event.t1 is None else f" – {event.t1 / 1000:.3f}s"
            item = QListWidgetItem(f"{label} · {event.t0 / 1000:.3f}s{ending}\n{event.extras.get('confirmation', 'legacy_unreviewed')} · {event.note}")
            item.setData(Qt.ItemDataRole.UserRole, ("event", event.id))
            item.setToolTip(item.text())
            self.events.addItem(item)
        for draft in data.work.drafts:
            if draft.get("confirmation") == "confirmed":
                continue
            item = QListWidgetItem("视频草稿 · " + wall_text(draft["reference_start"]) + " · " + draft.get("note", ""))
            item.setData(Qt.ItemDataRole.UserRole, ("draft", draft["id"]))
            self.events.addItem(item)
        if data.root:
            facade = SimpleNamespace(root=data.root, meta=Path(self.cache.name), readonly=True,
                                     source_path=lambda relative: contained(data.root, relative))
            self.board.configure(facade, data.rows, data.timeline)
        self.cameras.blockSignals(True)
        preferred = data.document.get("video", {}).get("selected_cameras", []) or data.timeline.cameras[:8]
        selected = 0
        for camera in data.timeline.cameras:
            item = QListWidgetItem(camera)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = camera in preferred and selected < 8
            selected += int(checked)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.cameras.addItem(item)
        self.cameras.blockSignals(False)
        self.select_cameras()
        self.play_button.setEnabled(bool(data.timeline.intervals))
        self.seek(self.bounds()[0])
        self.media_mode.setCurrentIndex(0 if data.timeline.intervals else 1)
        self.switch_media(self.media_mode.currentIndex())
        if self.events.count():
            self.events.setCurrentRow(0)

    def bounds(self):
        view = self.data.document["view"]
        return float(view["start_ms"]), float(view["end_ms"])

    def select_cameras(self, changed=None):
        checked = [self.cameras.item(i).text() for i in range(self.cameras.count()) if self.cameras.item(i).checkState() == Qt.CheckState.Checked]
        if len(checked) > 8:
            if changed:
                self.cameras.blockSignals(True)
                changed.setCheckState(Qt.CheckState.Unchecked)
                self.cameras.blockSignals(False)
            self.statusBar().showMessage("一次请选择 1–8 个视角")
            return
        self.board.select(checked)

    def seek(self, when):
        if not self.data:
            return
        lo, hi = self.bounds()
        when = max(lo, min(hi, float(when)))
        self.update_imu(when)
        clock = self.data.work.clock
        if clock.anchors:
            self.board.seek(clock.map(when))
        else:
            self.statusBar().showMessage("没有九轴校准锚点；不会用文件名或服务器时间自动对齐视频。")

    def update_imu(self, when):
        lo, hi = self.bounds()
        self.plot.set_playhead(when)
        if not self.slider.isSliderDown():
            self.slider.setValue(round(10000 * (when - lo) / max(1, hi - lo)))
        quality = self.data.work.clock.quality(when)
        label = {"interpolated": "已校准范围", "estimated": "未校准", "single_anchor": "单点粗对齐",
                 "unconfirmed": "未确认区间", "extrapolated": "超出校准范围",
                 "device_clock": "设备时钟候选定位", "legacy_estimate": "旧协议估计时间"}.get(quality, "未校准")
        self.position.setText(f"{when / 1000:.3f}s · {label}")

    def video_time(self, when):
        if not self.data or not self.data.work.clock.anchors:
            return
        imu = self.data.work.clock.map(when, inverse=True)
        lo, hi = self.bounds()
        if self.board.playing and imu >= hi:
            self.board.play(False)
            self.board.seek(self.data.work.clock.map(hi))
            self.statusBar().showMessage("已到本标注或九轴片段结束处")
            return
        self.update_imu(max(lo, min(hi, imu)))

    def slider_seek(self):
        if self.data:
            lo, hi = self.bounds()
            self.seek(lo + self.slider.value() / 10000 * (hi - lo))

    def check_sources(self):
        if not self.data or not self.data.root:
            return
        changed = []
        for tile in self.board.tiles.values():
            interval = tile.interval
            if not interval or interval.asset_id in self.board.blocked_assets:
                continue
            try:
                if file_stamp(contained(self.data.root, interval.path)) == self.board.source_stamps.get(interval.path):
                    continue
            except OSError:
                pass
            self.board.blocked_assets.add(interval.asset_id)
            changed.append(interval.path)
        if changed:
            self.board.play(False)
            self.board.seek(self.board.reference_ms)
            self.banner.setText("录像缺失或已变化：" + "、".join(changed))

    def toggle_play(self):
        if self.data and self.data.timeline.intervals and self.media_mode.currentIndex() == 0:
            self.board.play(not self.board.playing)

    def review_event(self, identifier):
        if self.data:
            event = next((e for e in self.data.work.project.events if e.id == identifier), None)
            if event:
                bundle = event.extras.get("screenshots")
                self.evidence_gallery.set_bundle(bundle, self.path.parent)
                if bundle and not context_matches(bundle, self.data.work, event):
                    self.statusBar().showMessage("证据图对应旧标签/同步版本，保留用于追溯，需重新核对。")
                self.board.play(False)
                self.plot.set_selected_event(event.id)
                self.seek(event.t0)

    def review_item(self, item, previous=None):
        if not item or not self.data:
            return
        kind, identifier = item.data(Qt.ItemDataRole.UserRole)
        if kind == "event":
            self.review_event(identifier)
        else:
            self.evidence_gallery.set_bundle(None)
            draft = next(d for d in self.data.work.drafts if d["id"] == identifier)
            self.board.play(False)
            self.board.seek(draft["reference_start"])

    def closeEvent(self, event):
        self.closed = True
        self.cancellation.set()
        if self.reusable:
            # Embedded surveillance decoders cannot always be safely released
            # on the GUI thread. Retain ONE bounded pool for subsequent history
            # files instead of allocating nine more after each close/reopen.
            self.source_check.stop()
            self.board.play(False)
            self.board.select([])
            event.ignore()
            self.hide()
            return
        self.disposed = True
        self.poll.stop()
        self.source_check.stop()
        self.board.close()
        self.loader.shutdown(wait=False, cancel_futures=True)
        # VLC may retain cache handles until process teardown on surveillance
        # PS. No source files or human work are touched by this temporary cache.
        try:
            self.cache.cleanup()
        except OSError:
            pass
        event.accept()

    def dispose(self):
        if not self.disposed:
            self.reusable = False
            self.close()
