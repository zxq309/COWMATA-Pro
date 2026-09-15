"""Independent history review and in-place annotation editor. Never imports over active human work."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
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
from .dataset_access import DatasetLease
from .evidence import context_matches
from .evidence_ui import EvidenceGallery
from .label_file import load_history, read_label_file
from .materials import GLASS_STYLE, FrostedCanvas, apply_mica
from .presentation import PresentationVideoBoard
from .signal_panel import SignalPanel, TimePositionSpinBox, reference_text
from .theme import STYLE


class HistoryWindow(QMainWindow):
    saved = Signal(object)
    def __init__(self, path, root=None, *, board_factory=PresentationVideoBoard, reusable=False):
        super().__init__()
        from .review_store import resolve_label_path
        self.path = resolve_label_path(path)
        self.review_dirty = False
        self.before_save = lambda: None
        self.data = None
        self.closed = False
        self.disposed = False
        self.reusable = reusable
        self.cancellation = threading.Event()
        self.loader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="annotation-history")
        self.future = None
        self.source_leases = []
        self.cache = tempfile.TemporaryDirectory(prefix="cowmata-history-")
        self.setWindowTitle("COWMATA Pro™ · 复核与修改 · " + self.path.name)
        self.resize(1400, 900)
        self.setStyleSheet(STYLE + GLASS_STYLE)
        canvas = FrostedCanvas()
        outer = QVBoxLayout(canvas)
        bar = QHBoxLayout()
        title = QLabel("复核已有标签 · 修改写回原文件")
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
        editing = QHBoxLayout()
        self.edit_button = QPushButton('修改所选标签')
        self.edit_button.clicked.connect(self.edit_existing)
        self.save_button = QPushButton('保存修改到原文件')
        self.save_button.clicked.connect(self.save_changes)
        self.save_button.setEnabled(False)
        editing.addWidget(self.edit_button)
        editing.addWidget(self.save_button)
        editing.addWidget(QLabel('复核只修改现有记录，不创建新标签；保存前保留修订备份。'), 1)
        outer.addLayout(editing)
        QShortcut(QKeySequence('Ctrl+S'), self, activated=self.save_changes)
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
        self.plot.track.setToolTip("单击标签定位；点击修改所选标签调整类型和边界")
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
        if not self.confirm_pending():
            return False
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
        cancellation = self.cancellation
        path = self.path

        def guarded_load():
            document=read_label_file(path)
            from .review_store import local_source_root
            local_root=local_source_root(path, document)
            hint = document["source"].get("project_root_hint", "")
            selected = local_root or root or (hint if hint and Path(hint).is_dir() else None)
            selected_roots=[selected] if selected else []
            archive=document.get('video',{}).get('archive',{}).get('archive_root_hint')
            if root is None and archive and Path(archive).is_dir():
                selected_roots.append(archive)
            selected_roots.extend(r['external_source'] for r in document.get('video',{}).get('rows',[]) if r.get('external_source'))
            lease = DatasetLease(selected_roots, kind="review") if selected_roots else None
            if lease:
                self.source_leases.append(lease)
            try:
                return load_history(path, root, cancelled=cancellation.is_set)
            except Exception:
                if lease:
                    lease.close()
                raise
        self.future = self.loader.submit(guarded_load)
        return True

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
        self.loaded_sha = data.document.get('_review_sha256')
        self.review_dirty = False
        self.edit_button.setEnabled(bool(data.work.project.events or data.work.drafts))
        self.save_button.setEnabled(False)
        self.events.clear()
        self.cameras.clear()
        self.plot.set_clock(data.work.clock)
        self.banner.setText("；".join(data.warnings) or "标签位置采用原始信号时间；修改后可保存到原标签文件。")
        if data.motion:
            self.plot.set_data([PlotSeries(**s) for s in data.motion.plot_series()], data.motion.duration_ms)
            self.plot.set_view(*self.bounds())
        self.plot.set_events([label.to_dict() for label in data.work.project.labels], [e.to_dict() for e in data.work.project.events])
        clock = data.work.clock
        for event in data.work.project.events:
            label = data.work.project.labels[event.li].name if 0 <= event.li < len(data.work.project.labels) else str(event.li)
            start = reference_text(clock, event.t0, True) if clock.anchors else f"相对 {event.t0 / 1000:.3f} 秒"
            ending = ""
            if event.t1 is not None:
                end = reference_text(clock, event.t1, True) if clock.anchors else f"相对 {event.t1 / 1000:.3f} 秒"
                ending = f" – {end}"
            item = QListWidgetItem(f"{label} · {start}{ending}\n{event.extras.get('confirmation', 'legacy_unreviewed')} · {event.note}")
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
                                     source_path=data.source_path)
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
        label = {"interpolated": "已校准范围", "estimated": "未校准", "single_anchor": "单点粗对齐", "offset": "已一次对齐 · 固定时间差",
                 "unconfirmed": "未确认区间", "extrapolated": "超出校准范围",
                 "device_clock": "设备时钟候选定位", "legacy_estimate": "旧协议估计时间"}.get(quality, "未校准")
        clock = self.data.work.clock
        position = reference_text(clock, when, True) if clock.anchors else f"{when / 1000:.3f}s"
        self.position.setText(f"{position} · {label}")

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
                if file_stamp(self.data.source_path(interval.path)) == self.board.source_stamps.get(interval.path):
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

    def edit_existing(self):
        item = self.events.currentItem()
        if self.data is None or item is None:
            self.statusBar().showMessage('请先选择一条已有标签')
            return
        kind, identifier = item.data(Qt.ItemDataRole.UserRole)
        work = self.data.work
        record = next((e for e in work.project.events if e.id == identifier), None) if kind == 'event' else next((d for d in work.drafts if d['id'] == identifier), None)
        if record is None:
            return
        if kind == 'draft' and not work.clock.anchors:
            self.statusBar().showMessage('视频草稿缺少时间映射，请先连接原工程核对同步')
            return
        index = record.li if kind == 'event' else record['label_index']
        start = record.t0 if kind == 'event' else record['reference_start']
        end = record.t1 if kind == 'event' else record['reference_end']
        if kind == 'draft':
            start = work.clock.inverse(start)
            end = work.clock.inverse(end) if end is not None else None
        dialog = QDialog(self)
        dialog.setWindowTitle('修改已有标签')
        form = QFormLayout(dialog)
        labels = QComboBox()
        for label in work.project.labels:
            labels.addItem(label.name)
        labels.setCurrentIndex(index)
        form.addRow('标签', labels)
        fields = []
        for title, value in [('开始（秒）', start), ('结束（秒）', end if end is not None else start)]:
            field = TimePositionSpinBox()
            field.set_clock(work.clock)
            field.setDecimals(3)
            field.setRange(0, max(self.bounds()[1]/1000, float(value)/1000, 1))
            field.setValue(float(value)/1000)
            form.addRow(title, field)
            fields.append(field)
        note = QLineEdit(record.note if kind == 'event' else record.get('note',''))
        form.addRow('备注', note)
        def label_changed():
            fields[1].setEnabled(work.project.labels[labels.currentIndex()].type != 'point')
        labels.currentIndexChanged.connect(label_changed)
        label_changed()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_start = fields[0].value()*1000
        new_end = fields[1].value()*1000 if fields[1].isEnabled() else None
        if new_end is not None and new_end < new_start:
            QMessageBox.warning(self, '无法修改', '结束时间不能早于开始时间')
            return
        work.checkpoint()
        if kind == 'event':
            record.li, record.t0, record.t1, record.note = labels.currentIndex(), new_start, new_end, note.text()
            record.extras['confirmation'] = 'needs_review'
        else:
            record.update(label_index=labels.currentIndex(), label_code=work.project.labels[labels.currentIndex()].code, reference_start=work.clock.map(new_start), reference_end=work.clock.map(new_end) if new_end is not None else None, note=note.text(), confirmation='needs_review')
        self.plot.set_events([label.to_dict() for label in work.project.labels], [e.to_dict() for e in work.project.events])
        item.setText(work.project.labels[labels.currentIndex()].name + f' · {new_start/1000:.3f} s' + (' · 点事件' if new_end is None else f' – {new_end/1000:.3f} s'))
        self.review_dirty = True
        self.save_button.setEnabled(True)
        self.statusBar().showMessage('已修改当前记录；点击保存修改到原文件')

    def save_changes(self):
        if not self.review_dirty or self.data is None:
            return True
        from .review_store import save_review
        try:
            self.before_save()
            self.loaded_sha = save_review(self.path, self.data.work, expected_sha=self.loaded_sha)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '无法保存复核', str(exc))
            return False
        self.review_dirty = False
        self.save_button.setEnabled(False)
        self.saved.emit(self.path)
        self.statusBar().showMessage('修改已写回原标签文件，原始信号未改动')
        return True

    def confirm_pending(self):
        if not self.review_dirty:
            return True
        answer = QMessageBox.question(self, '保存复核修改', '是否把复核修改保存到原标签文件？',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Cancel or answer == QMessageBox.StandardButton.Save and not self.save_changes():
            return False
        self.review_dirty = False
        return True

    def closeEvent(self, event):
        if not self.confirm_pending():
            event.ignore()
            return
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
        def release_sources():
            self.loader.shutdown(wait=True, cancel_futures=True)
            self.board.frame_pool.shutdown(wait=True, cancel_futures=True)
            for tile in self.board.pool:
                if tile.engine and getattr(tile.engine, "thread_owner", None):
                    tile.engine.thread_owner.wait()
            for lease in self.source_leases:
                lease.close()
            self.source_leases.clear()
        threading.Thread(target=release_sources, name="history-release", daemon=True).start()
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
