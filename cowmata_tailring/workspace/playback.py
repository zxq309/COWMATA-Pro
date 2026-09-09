from __future__ import annotations

import ctypes
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from cowmata_tailring.media.stable_engine import StableMediaEngine
from cowmata_tailring.media.timeline import MediaTimelineIndex

from .catalog import file_stamp
from .clocks import VideoTimeline, wall_text
from .compatibility import CompatibilityCache
from .frame_cache import FrameCache


class MediaStats(ctypes.Structure):
    _fields_ = [("read_bytes", ctypes.c_int), ("input_bitrate", ctypes.c_float),
                ("demux_read_bytes", ctypes.c_int), ("demux_bitrate", ctypes.c_float),
                ("demux_corrupted", ctypes.c_int), ("demux_discontinuity", ctypes.c_int),
                ("decoded_video", ctypes.c_int), ("decoded_audio", ctypes.c_int),
                ("displayed_pictures", ctypes.c_int), ("lost_pictures", ctypes.c_int),
                ("played_abuffers", ctypes.c_int), ("lost_abuffers", ctypes.c_int),
                ("sent_packets", ctypes.c_int), ("sent_bytes", ctypes.c_int), ("send_bitrate", ctypes.c_float)]


class WorkspaceEngine(StableMediaEngine):
    def __init__(self, widget, *, metadata, cache, parent=None, software=False, no_audio=False):
        self.metadata_provider = metadata
        self.project_cache = cache
        # Keep GPU decoding automatic, but use the bundled D3D9 presentation
        # backend for embedded multiview HWNDs. The D3D11 presentation path
        # showed intermittent long layout/reopen stalls in native stress tests.
        # Auto CPU thread counts grow with every view and inflate the hardware
        # frame pool too. Bound each decoder; dedicated GPU decoding stays on.
        options = ["--vout=direct3d9", "--avcodec-threads=2"]
        if software:
            options.append("--avcodec-hw=none")
        if no_audio:
            options.append("--no-audio")
        super().__init__(widget, parent=parent, instance_options=options)
        self._lib.libvlc_media_get_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(MediaStats)]
        self._lib.libvlc_media_get_stats.restype = ctypes.c_int
        for name in ("libvlc_video_set_mouse_input", "libvlc_video_set_key_input"):
            method = getattr(self._lib, name)
            method.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            method.restype = None
            method(self._player, 0)

    def _timeline_cache_directory(self):
        return self.project_cache / "packet-timelines"

    def _dahua_duration_cache_directory(self):
        return self.project_cache / "dahua-timelines"

    def _start_timeline_probe(self, path):
        value = self.metadata_provider(path)
        if value and value.get("timeline"):
            index = MediaTimelineIndex.from_dict(value["timeline"])
            if index.native and index.native.get("timestamp_data"):
                from cowmata_tailring.media.native_ps import playback_index
                self._timeline_validation_required = False
                self._apply_dahua_duration_index(playback_index(path,index.native))
                return
            stat = Path(path).stat()
            index = replace(index, source_path=str(Path(path).resolve()), source_size=stat.st_size,
                            source_mtime_ns=stat.st_mtime_ns)
            self._apply_timeline_index(index)
        else:
            super()._start_timeline_probe(path)

    def stats(self):
        value = MediaStats()
        if self._media and self._lib.libvlc_media_get_stats(self._media, ctypes.byref(value)):
            return value
        return None


class VideoSurface(QWidget):
    doubleClicked = Signal()
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setStyleSheet("background: #070b10")
        self.setMinimumSize(100, 70)

    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit()

    def mousePressEvent(self, event):
        self.clicked.emit()


class PausedFrame(QLabel):
    doubleClicked = Signal()
    clicked = Signal()

    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit()

    def mousePressEvent(self, event):
        self.clicked.emit()


class ClipSeekSlider(QSlider):
    """Click or drag anywhere; decoding happens only when the gesture ends."""

    def _at_pointer(self, event):
        margin = self.style().pixelMetric(QStyle.PixelMetric.PM_SliderLength) / 2
        fraction = max(0, min(1, (event.position().x() - margin) / max(1, self.width() - 2 * margin)))
        self.setSliderPosition(round(self.minimum() + fraction * (self.maximum() - self.minimum())))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self._at_pointer(event)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self._at_pointer(event)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self._at_pointer(event)
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class VideoTile(QFrame):
    activated = Signal(object)
    enlarged = Signal(object)
    transportRequested = Signal(object, str, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.camera = ""
        self.asset_id = None
        self.interval = None
        self.engine = None
        self.pending = None
        self.actual_ms = None
        self.ready = False
        self.last_seek_at = 0.0
        self.last_media_ms = None
        self.last_motion_at = 0.0
        self.frame_baseline = 0
        self.last_picture_count = 0
        self.precise_ms = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        header = QHBoxLayout()
        self.title = QPushButton("视角")
        self.title.clicked.connect(lambda: self.activated.emit(self))
        header.addWidget(self.title, 1)
        layout.addLayout(header)
        self.surface = VideoSurface(self)
        self.surface.clicked.connect(lambda: self.activated.emit(self))
        self.surface.doubleClicked.connect(lambda: self.enlarged.emit(self))
        self.frame_view = PausedFrame()
        self.frame_view.doubleClicked.connect(lambda: self.enlarged.emit(self))
        self.frame_view.clicked.connect(lambda: self.activated.emit(self))
        self.frame_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frame_view.setMinimumSize(100, 70)
        self.frame_image = None
        self.stack = QStackedWidget()
        self.stack.addWidget(self.surface)
        self.stack.addWidget(self.frame_view)
        layout.addWidget(self.stack, 1)
        self.overlay = QFrame(self.stack)
        self.overlay.setObjectName("videoTransport")
        # A native sibling stays above VLC's embedded native video surface.
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.overlay.setStyleSheet("""
            QFrame#videoTransport { background: rgba(15, 24, 36, 220); border: 1px solid #60778b; border-radius: 9px; }
            QFrame#videoTransport QToolButton { color: #ffffff; background: transparent; border: 0; border-radius: 5px; padding: 0; margin: 0; min-height: 0; }
            QFrame#videoTransport QToolButton:hover { background: #367ca2; }
            QFrame#videoTransport QComboBox { color: #ffffff; background: #263c50; border: 1px solid #60778b; border-radius: 4px; }
        """)
        bar = QHBoxLayout(self.overlay)
        bar.setContentsMargins(5, 4, 5, 4)
        bar.setSpacing(2)
        def control(icon, help_text, callback):
            button = QToolButton(self.overlay)
            button.setIcon(self.control_icon(icon))
            button.setIconSize(QSize(18, 18))
            button.setFixedSize(28, 28)
            button.setToolTip(help_text)
            button.setAccessibleName(help_text)
            button.clicked.connect(callback)
            bar.addWidget(button)
            return button
        self.back = control(QStyle.StandardPixmap.SP_MediaSeekBackward, "后退 5 秒 · 统一标注时间轴",
                            lambda: self.transportRequested.emit(self, "seek", -5000))
        self.play_control = control(QStyle.StandardPixmap.SP_MediaPlay, "播放此视角 · 其他视角暂停",
                                   lambda: self.transportRequested.emit(self, "play", 0))
        self.forward = control(QStyle.StandardPixmap.SP_MediaSeekForward, "前进 5 秒 · 统一标注时间轴",
                               lambda: self.transportRequested.emit(self, "seek", 5000))
        self.speed_control = QComboBox(self.overlay)
        self.speed_control.addItems(["0.25×", "0.5×", "1×", "2×", "4×"])
        self.speed_control.setCurrentIndex(2)
        self.speed_control.setFixedWidth(65)
        self.speed_control.setToolTip("播放倍率 · 所有视角与九轴共用时间轴")
        self.speed_control.currentIndexChanged.connect(
            lambda i: self.transportRequested.emit(self, "rate", [.25, .5, 1, 2, 4][i]))
        bar.addWidget(self.speed_control)
        self.zoom = control(QStyle.StandardPixmap.SP_TitleBarMaxButton, "放大此视角 / 恢复布局",
                            lambda: self.enlarged.emit(self))
        self.overlay.hide()
        self._control_state = None
        self.message = QLabel("等待选择视角")
        # Avoid repeated native video resize/reflow on every clock update.
        self.message.setWordWrap(False)
        self.message.setMinimumWidth(0)
        self.message.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.message.setFixedHeight(22)
        layout.addWidget(self.message)
        timeline_bar = QHBoxLayout()
        timeline_bar.setContentsMargins(3, 0, 3, 0)
        self.seek_slider = ClipSeekSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000000)
        self.seek_slider.setEnabled(False)
        self.seek_slider.setToolTip("拖动到本段录像的位置；松开后定位，九轴和其他视角一起联动")
        self.seek_slider.setAccessibleName("本视角录像进度")
        self.seek_clock = QLabel("--:-- / --:--")
        self.seek_clock.setStyleSheet("color:#b8cecc; font-size:11px")
        self.seek_slider.sliderReleased.connect(self._commit_seek)
        self.seek_slider.sliderMoved.connect(self._preview_seek)
        self.seek_slider.actionTriggered.connect(self._seek_action)
        self.seek_bounds = None
        timeline_bar.addWidget(self.seek_slider, 1)
        timeline_bar.addWidget(self.seek_clock)
        layout.addLayout(timeline_bar)

    @staticmethod
    def _duration_text(ms):
        seconds = max(0, int(ms / 1000))
        return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"

    def _preview_seek(self, value):
        if self.seek_bounds:
            duration = self.seek_bounds[1] - self.seek_bounds[0]
            self.seek_clock.setText(f"{self._duration_text(duration * value / 1000000)} / {self._duration_text(duration)}")

    def _commit_seek(self):
        if self.seek_bounds:
            start, end = self.seek_bounds
            target = start + (end - start) * self.seek_slider.sliderPosition() / 1000000
            self.transportRequested.emit(self, "seek_absolute", min(target, end - 1))

    def _seek_action(self, action):
        if not self.seek_slider.isSliderDown():
            # Keyboard/page actions commit once after Qt updates the position.
            QTimer.singleShot(0, self._commit_seek)

    def update_seek(self, start, end, reference_ms):
        if self.seek_slider.isSliderDown():
            return  # Freeze this clip's bounds until the gesture is committed.
        self.seek_bounds = (start, end) if end > start else None
        self.seek_slider.setEnabled(self.seek_bounds is not None)
        if self.seek_bounds:
            fraction = max(0, min(1, (reference_ms - start) / (end - start)))
            self.seek_slider.setValue(round(fraction * 1000000))
            self._preview_seek(self.seek_slider.value())

    def status(self, text, *, good=False):
        if self.message.text() != text:
            self.message.setText(text)
            self.message.setToolTip(text)
        style = "color: #9cdbac" if good else "color: #f2bc74"
        if self.message.styleSheet() != style:
            self.message.setStyleSheet(style)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.scale_frame()

    def control_icon(self, icon):
        pixmap = self.style().standardIcon(icon).pixmap(18, 18)
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor("#f1faff"))
        painter.end()
        return QIcon(pixmap)

    def update_controls(self, playing, rate, expanded):
        state = (playing, rate, expanded)
        if state != self._control_state:
            self._control_state = state
            self.play_control.setIcon(self.control_icon(
                QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay))
            self.play_control.setToolTip("暂停标注时间轴" if playing else "播放此视角 · 其他视角暂停")
            self.play_control.setAccessibleName(self.play_control.toolTip())
            self.zoom.setIcon(self.control_icon(
                QStyle.StandardPixmap.SP_TitleBarNormalButton if expanded else QStyle.StandardPixmap.SP_TitleBarMaxButton))
            self.speed_control.blockSignals(True)
            self.speed_control.setCurrentIndex([.25, .5, 1, 2, 4].index(rate))
            self.speed_control.blockSignals(False)
        hovered = self.stack.isVisible() and self.stack.rect().contains(self.stack.mapFromGlobal(QCursor.pos()))
        if hovered or self.speed_control.view().isVisible():
            width = self.overlay.sizeHint().width()
            self.overlay.setGeometry(max(0, (self.stack.width()-width)//2), max(0, self.stack.height()-43), width, 38)
            self.overlay.show()
            self.overlay.raise_()
        else:
            self.overlay.hide()

    def scale_frame(self):
        # Native playback leaves the last precise frame cached. Resizing eight
        # live views must not resample these hidden full-resolution pixmaps.
        if self.frame_image is not None and self.stack.currentWidget() is self.frame_view:
            self.frame_view.setPixmap(QPixmap.fromImage(self.frame_image).scaled(self.stack.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))


class VideoBoard(QWidget):
    timeChanged = Signal(float)
    playbackChanged = Signal(bool)
    rateChanged = Signal(float)
    mainChanged = Signal(str)
    notice = Signal(str)
    metricsChanged = Signal(object)
    preciseReady = Signal(object)
    compatibilityReady = Signal(object)

    def __init__(self, parent=None, *, engine_factory=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setSpacing(4)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.pool = []
        self.tiles = {}
        self.selected = []
        self.main_camera = ""
        self.expanded = None
        self.columns = 0
        self.layout_mode = "grid"
        self.two_view_ratio = 50
        self.timeline = VideoTimeline([])
        self.catalog = None
        self.metadata = {}
        self.source_stamps = {}
        self.blocked_assets = set()
        self.reference_ms = 0.0
        self.playing = False
        self.rate = 1.0
        self.strict_sync = False
        self._held = False
        self.software_decode = False
        self.compatibility = False
        self.compatibility_cache = None
        self.generation = 0
        self.last_tick = time.perf_counter()
        if engine_factory is None:
            from .threaded_engine import ThreadedWorkspaceEngine
            engine_factory = partial(ThreadedWorkspaceEngine, backend_factory=WorkspaceEngine)
        self.engine_factory = engine_factory
        self.prewarm = None
        self.latencies = []
        self.frame_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="precise-frame")
        self.frame_jobs = []
        self.frame_cache = FrameCache()
        self.preciseReady.connect(self._precise_ready)
        self.compatibilityReady.connect(self._compatibility_ready)
        self._closing = False
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.control_timer = QTimer(self)
        self.control_timer.timeout.connect(self._update_controls)
        self.control_timer.start(120)

    def configure(self, catalog, rows, timeline):
        if self.catalog is not catalog:
            self.compatibility_cache = CompatibilityCache(catalog.meta, readonly=catalog.readonly)
        self.catalog = catalog
        self.timeline = timeline
        # Only reconciliation can make a changed source usable again.
        for row in rows:
            if row["asset_id"] in self.blocked_assets and row["state"] in {"ready", "review"}:
                try:
                    if file_stamp(catalog.source_path(row["path"])) == row["stamp"]:
                        self.blocked_assets.discard(row["asset_id"])
                except OSError:
                    pass
        self.source_stamps = {r["path"]: r["stamp"] for r in rows if r["state"] in {"ready", "review"}}
        self.metadata = {str(catalog.source_path(row["path"])): row["metadata"] for row in rows
                         if row["state"] in {"ready", "review"}}
        for tile in self.pool:
            if tile.engine:
                tile.engine.project_cache = catalog.meta / "cache"

    def _new_tile(self):
        if len(self.pool) >= 9:
            raise RuntimeError("已达到 8 路显示 + 1 路预热的解码器上限")
        tile = VideoTile(self)
        tile.activated.connect(lambda t: self.set_main(t.camera))
        tile.enlarged.connect(lambda t: self.enlarge(t.camera))
        tile.transportRequested.connect(self.transport)
        self.pool.append(tile)
        tile.hide()
        return tile

    def _update_controls(self):
        for camera, tile in self.tiles.items():
            preview = hasattr(self, "is_preview") and self.is_preview(camera)
            tile.update_controls(self.playing and not preview, self.rate, self.expanded == camera)
            spans = [s for s in self.timeline._groups.get(camera, []) if s.asset_id == tile.asset_id]
            if spans:
                start = self.timeline.reference_time(camera, min(s.wall_start for s in spans))
                end = self.timeline.reference_time(camera, max(s.wall_end for s in spans))
                tile.update_seek(start, end, self.reference_ms)
            else:
                tile.update_seek(0, 0, 0)

    def transport(self, tile, command, value):
        if tile.camera not in self.selected:
            return
        was_active = tile.camera == self.main_camera
        self.set_main(tile.camera)
        if command == "play":
            # A tile play command explicitly asks for exclusive playback.
            if hasattr(self, "set_policy"):
                self.set_policy("focus")
            self.play(not (self.playing and was_active))
        elif command == "seek":
            self.seek(self.reference_ms + value)
        elif command == "seek_absolute":
            self.seek(value)
        elif command == "rate":
            self.set_rate(value)
        self._update_controls()

    def _idle_tile(self):
        active = set(self.tiles.values())
        return next((t for t in self.pool if t not in active and t is not self.prewarm), None) or self._new_tile()

    def select(self, cameras):
        cameras = list(dict.fromkeys(cameras))
        if not 0 <= len(cameras) <= 8:
            raise ValueError("一次请选择 1–8 个视角")
        for camera in list(self.tiles):
            if camera not in cameras:
                tile = self.tiles.pop(camera)
                self._pause_tile(tile)
                tile.pending = None
                tile.hide()
        for camera in cameras:
            if camera not in self.tiles:
                tile = self._idle_tile()
                tile.camera = camera
                tile.title.setText(camera)
                self.tiles[camera] = tile
        self.selected = cameras
        if self.main_camera not in cameras:
            self.main_camera = cameras[0] if cameras else ""
        if self.expanded not in cameras:
            self.expanded = None
        self.relayout()
        self._apply_audio()
        self.seek(self.reference_ms)

    def set_main(self, camera):
        if camera in self.selected and camera != self.main_camera:
            self.main_camera = camera
            self._apply_audio()
            self.mainChanged.emit(camera)
            self.relayout()

    def _apply_audio(self):
        for tile in self.pool:
            if tile.engine:
                tile.engine.set_volume(80 if tile.camera == self.main_camera and tile in self.tiles.values() else 0)

    def enlarge(self, camera=None):
        self.expanded = None if camera is None or self.expanded == camera else camera
        if camera:
            self.set_main(camera)
        self.relayout()

    def relayout(self):
        # Remove layout items only; native widgets never change parents/handles.
        while self.grid.count():
            self.grid.takeAt(0)
        for tile in self.pool:
            tile.hide()
        columns = self.columns or (1 if len(self.selected) == 1 else 2 if len(self.selected) <= 4 else 3 if len(self.selected) <= 6 else 4)
        for column in range(8):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        if len(self.selected) == 2 and columns >= 2:
            main_column = self.selected.index(self.main_camera)
            self.grid.setColumnStretch(main_column, self.two_view_ratio)
            self.grid.setColumnStretch(1 - main_column, 100 - self.two_view_ratio)
        for i, camera in enumerate(self.selected):
            tile = self.tiles[camera]
            if self.expanded and camera != self.expanded:
                continue
            if self.expanded:
                self.grid.addWidget(tile, 0, 0)
            elif self.layout_mode == "main_aux" and len(self.selected) > 2:
                if camera == self.main_camera:
                    self.grid.addWidget(tile, 0, 0, max(2, len(self.selected) - 1), 2)
                else:
                    side_index = [c for c in self.selected if c != self.main_camera].index(camera)
                    self.grid.addWidget(tile, side_index, 2)
            else:
                self.grid.addWidget(tile, i // columns, i % columns)
            tile.setStyleSheet("QFrame {background:#111824; border: 1px solid " + ("#52a9e8" if camera == self.main_camera else "#354354") + ";}")
            tile.show()

    def seek(self, reference_ms):
        self.generation += 1
        for job in self.frame_jobs:
            job.cancel()
        self.frame_jobs = []
        self.reference_ms = float(reference_ms)
        self.last_tick = time.perf_counter()
        for camera, tile in sorted(self.tiles.items(), key=lambda item: item[0] != self.main_camera):
            self._position(camera, tile, force=True)
        self.timeChanged.emit(self.reference_ms)

    def play(self, enabled=True):
        if enabled and not any(tile.interval for tile in self.tiles.values()):
            enabled = False
            self.notice.emit("当前时刻的录像尚未就绪，请等待检索出画面后再播放")
        previous = self.playing
        self.playing = bool(enabled)
        if not self.playing:
            self._held = False
        self.last_tick = time.perf_counter()
        for tile in self.tiles.values():
            if tile.engine and tile.asset_id and tile.ready:
                preview = hasattr(self, "is_preview") and self.is_preview(tile.camera)
                tile.engine.pause(not self.playing or preview)
        self.playbackChanged.emit(self.playing)
        if previous != self.playing:
            self.seek(self.reference_ms)

    def set_rate(self, rate):
        if rate not in {.25, .5, 1, 2, 4}:
            raise ValueError("播放倍率不支持")
        self.rate = rate
        for tile in self.pool:
            if tile.engine:
                tile.engine.set_rate(rate)
        self.rateChanged.emit(rate)

    def _pause_tile(self, tile):
        if tile.engine and tile.asset_id:
            try:
                tile.engine.pause(True)
            except RuntimeError:
                pass
        tile.ready = False

    def _position(self, camera, tile, *, force=False):
        match = self.timeline.locate(camera, self.reference_ms, prefer=tile.asset_id)
        if match and match[0].asset_id in self.blocked_assets:
            match = None
        if match is None:
            self._pause_tile(tile)
            tile.pending = None
            tile.interval = None
            tile.surface.hide()
            tile.stack.hide()
            tile.status("此时刻无录像覆盖 · 不会用上一帧冒充现场")
            return
        interval, target = match
        if tile.asset_id != interval.asset_id and self.prewarm and self.prewarm.asset_id == interval.asset_id and self.prewarm.ready:
            old = tile
            tile = self.prewarm
            self.prewarm = old
            tile.camera = camera
            tile.title.setText(camera)
            self.tiles[camera] = tile
            self._pause_tile(old)
            old.pending = None
            self.relayout()
            self._apply_audio()
            force = True  # Resume/confirm the prepared renderer at the handoff.
        had_interval = tile.interval is not None
        tile.interval = interval
        if tile.asset_id != interval.asset_id or force or not had_interval:
            self._request(tile, interval, target)

    def _request(self, tile, interval, target, *, queued_start=None):
        if not self.catalog:
            return
        path = self.catalog.source_path(interval.path)
        try:
            if interval.asset_id in self.blocked_assets or not path.is_file() or file_stamp(path) != self.source_stamps.get(interval.path):
                self.blocked_assets.add(interval.asset_id)
                raise OSError("录像文件已移走或正在更换，请刷新素材")
            source_path = path
            metadata = self.metadata.get(str(source_path), {})
            if not self.playing and tile is not self.prewarm:
                # Paused review does not need eight live VLC decoders merely
                # to display eight still images. Keep the original PTS proof.
                tile.asset_id = interval.asset_id
                tile.ready = False
                tile.last_seek_at = queued_start or time.perf_counter()
                tile.pending = {"target": target, "generation": self.generation, "start": tile.last_seek_at,
                                "asset": interval.asset_id, "cold": tile.engine is None, "phase": "exact_frame"}
                if tile.engine:
                    tile.engine.pause(True)
                tile.stack.hide()
                self._precise_request(tile, source_path, target)
                return
            native = metadata.get("timeline", {}).get("native")
            if (self.compatibility or native) and self.playing and metadata.get("format") == "mpeg":
                cached = self.compatibility_cache.cached(interval.asset_id)
                if cached is None:
                    self._compatibility_request(tile, interval, target, source_path, metadata)
                    return
                path = cached.resolve()
                self.metadata[str(path)] = self.compatibility_cache.playback_metadata(interval.asset_id, metadata)
            if tile.engine is None:
                tile.engine = self.engine_factory(tile.surface, metadata=lambda p: self.metadata.get(str(Path(p))),
                                                  cache=self.catalog.meta / "cache", parent=tile,
                                                  software=self.software_decode)
                tile.engine.error_occurred.connect(lambda s, t=tile: t.status("播放失败：" + s))
            new_asset = tile.asset_id != interval.asset_id or Path(tile.engine._path or ".") != path or tile.engine.current_status in {"ended", "error", "stopped"}
            if new_asset:
                if not tile.engine.open(path):
                    raise RuntimeError(tile.engine.last_error)
                tile.asset_id = interval.asset_id
            else:
                tile.engine.clear_pending_seek()
            tile.engine.set_volume(80 if tile.camera == self.main_camera else 0)
            tile.engine.set_rate(1.0)
            stats = tile.engine.stats()
            baseline = stats.displayed_pictures if stats else 0
            tile.surface.show()
            tile.stack.show()
            tile.engine.play()
            tile.ready = False
            tile.last_seek_at = queued_start or time.perf_counter()
            tile.pending = {"target": target, "generation": self.generation,
                            "start": tile.last_seek_at, "baseline": baseline,
                            "asset": interval.asset_id, "cold": new_asset,
                            "phase": "priming", "attempts": 0}
            tile.status("正在准备真实画面…")
            if not self.playing and tile is not self.prewarm:
                tile.pending["phase"] = "exact_frame"
                self._precise_request(tile, source_path, target)
            else:
                tile.precise_ms = None
                tile.stack.setCurrentWidget(tile.surface)
        except (OSError, RuntimeError) as exc:
            tile.pending = None
            tile.ready = False
            tile.surface.hide()
            tile.stack.hide()
            tile.status(str(exc))
            self.notice.emit(str(exc))

    def _observe(self, tile, now):
        if not tile.engine or not tile.interval:
            return
        current = tile.engine.get_time_ms()
        stats = tile.engine.stats()
        pending = tile.pending
        if pending:
            if pending["asset"] != tile.asset_id or pending["generation"] != self.generation:
                tile.pending = None
                return
            if pending["phase"] in {"exact_frame", "compat_cache"}:
                return
            if pending["phase"] == "priming" and stats and stats.displayed_pictures > 0 and tile.engine.video_output_count() and tile.engine.is_seekable():
                if self.playing and tile is not self.prewarm and tile.camera != self.main_camera:
                    latest = self.timeline.locate(tile.camera, self.reference_ms, prefer=tile.asset_id)
                    if latest and latest[0].asset_id == tile.asset_id:
                        pending["target"] = latest[1]
                pending["baseline"] = stats.displayed_pictures
                tile.engine.pause(False)
                preroll = min(10000, pending["target"]) if tile.engine._force_avformat and not tile.engine._dahua_duration_index else 0
                tile.engine.set_time_ms(max(0, pending["target"] - preroll))
                # Native/Dahua byte seeks replace libVLC media, whose picture
                # counters restart at zero. Comparing to the previous media's
                # cumulative counter can make a warm seek wait forever.
                if tile.engine._dahua_duration_index:
                    pending["baseline"] = 0
                tile.engine.set_rate(4.0 if preroll > 1500 else 1.0)
                pending["phase"] = "preroll" if preroll > 1500 else "seeking"
                pending["seek_at"] = now
                # current/stats above belong to the media BEFORE this seek.
                # Only a later observation can certify the new actual frame.
                return
            if pending["phase"] == "preroll" and current >= pending["target"] - 1800:
                tile.engine.set_rate(1.0)
                pending["phase"] = "seeking"
            # Stop the decoder near the requested frame first. libVLC publishes
            # statistics less frequently than its playback clock; requiring a
            # fresh statistics tick before pausing overshoots every cold seek.
            if pending["phase"] == "seeking" and not self.playing:
                if abs(current - pending["target"]) <= 450 and now - pending["seek_at"] > .15:
                    tile.engine.pause(True)
                    pending["phase"] = "verifying_frame"
                elif current > pending["target"] + 450 and now - pending["seek_at"] > .8 and pending["attempts"] < 3:
                    pending["attempts"] += 1
                    tile.engine.set_time_ms(max(0, pending["target"] - 400 - 250 * pending["attempts"]))
                    pending["seek_at"] = now
            # Vout + displayed-picture progress + decoder time must all agree.
            visible = bool(stats and (stats.displayed_pictures > pending["baseline"] or tile is self.prewarm and stats.decoded_video > 3) and tile.engine.video_output_count())
            near = (-150 <= current - pending["target"] <= 1800) if self.playing else abs(current - pending["target"]) <= 650
            if pending["phase"] in {"seeking", "verifying_frame"} and visible and near:
                tile.pending = None
                tile.ready = True
                tile.actual_ms = self.timeline.reference_time(tile.camera, tile.interval.wall_at(current))
                if self.playing and tile.camera == self.main_camera and tile is not self.prewarm:
                    self.reference_ms = tile.actual_ms
                tile.last_motion_at = now
                tile.last_media_ms = current
                tile.engine.set_rate(self.rate)
                tile.engine.pause(not self.playing or self._held or tile is self.prewarm)
                self.latencies.append({"camera": tile.camera, "cold": pending["cold"],
                                       "seconds": now - pending["start"], "error_ms": current - pending["target"]})
                self.latencies = self.latencies[-2000:]
            elif now - pending["start"] > 25:
                self._pause_tile(tile)
                tile.pending = None
                tile.surface.hide()
                tile.stack.hide()
                tile.status("定位超时，画面未确认到位 · 请重试或减少同时播放路数")
            return
        if not tile.ready:
            if self.playing and stats and stats.displayed_pictures > tile.last_picture_count:
                tile.ready = True
                tile.last_motion_at = now
            else:
                if self.playing and now - tile.last_seek_at > 4:
                    match = self.timeline.locate(tile.camera, self.reference_ms, prefer=tile.asset_id)
                    if match and match[0].asset_id not in self.blocked_assets:
                        self._request(tile, *match)
                return
        if not self.playing and tile.precise_ms is not None:
            return
        if tile is self.prewarm:
            return
        if self._held:
            tile.status("画面已准备，等待其他有效视角到位…")
            tile.last_motion_at = now
            return
        if tile.last_media_ms != current or stats and stats.displayed_pictures != tile.last_picture_count:
            tile.last_media_ms, tile.last_motion_at = current, now
        if stats:
            tile.last_picture_count = stats.displayed_pictures
        tile.actual_ms = self.timeline.reference_time(tile.camera, tile.interval.wall_at(current))
        drift = tile.actual_ms - self.reference_ms
        if self.playing and now - tile.last_motion_at > 2.5:
            tile.status("画面暂未推进 / 缓冲中；当前不是同步实况")
            tile.ready = False
            return
        if self.playing and tile.camera != self.main_camera and abs(drift) > 900 * max(1, self.rate) and now - tile.last_seek_at > 3:
            match = self.timeline.locate(tile.camera, self.reference_ms, prefer=tile.asset_id)
            if match:
                self._request(tile, *match)
                return
        quality = "已抽检" if tile.interval.verified else "时间待复核"
        tile.status(f"{wall_text(tile.actual_ms)} · Δ{drift / 1000:+.2f}s · {quality}", good=abs(drift) < 1000)

    def tick(self):
        now = time.perf_counter()
        elapsed = min(now - self.last_tick, .5)
        self.last_tick = now
        if self.playing:
            valid_tiles = self.synchronised_tiles()
            main = self.tiles.get(self.main_camera)
            blocked = bool((self.strict_sync and any(not t.ready for t in valid_tiles)) or (main and main.pending))
            if blocked != self._held:
                self._held = blocked
                for t in valid_tiles:
                    if t.ready and t.engine:
                        t.engine.pause(blocked)
            if not blocked:
                if main and main.ready and main.engine and main.interval:
                    # The actual main decoder drives annotation time. A 4x
                    # request is not proof of 4x throughput; chasing a synthetic
                    # clock caused repeated seeks and visible playback stalls.
                    current = main.engine.get_time_ms()
                    self.reference_ms = self.timeline.reference_time(main.camera, main.interval.wall_at(current))
                    if main.engine.current_status == "ended" and current >= main.interval.media_end - 1000:
                        self.reference_ms = self.timeline.reference_time(main.camera, main.interval.wall_end) + 1
                else:
                    self.reference_ms += elapsed * 1000 * self.rate
            for camera, tile in list(self.tiles.items()):
                self._position(camera, tile)
            if not any(t.interval for t in self.tiles.values()) and self.tiles:
                self.play(False)
                self.notice.emit("所选视角当前没有已定位的录像画面；已暂停，可检查其他视角或跳到下一覆盖时段")
            self.timeChanged.emit(self.reference_ms)
        for tile in list(self.tiles.values()):
            self._observe(tile, now)
        if self.prewarm:
            self._observe(self.prewarm, now)
        if self.playing:
            self._prepare_next()

    def _prepare_next(self):
        # Exactly one shared spare decoder, nearest boundary first.
        candidates = []
        for camera, tile in self.prewarm_candidates():
            if not tile.interval or not tile.ready:
                continue
            end = self.timeline.reference_time(camera, tile.interval.wall_end)
            if 0 < end - self.reference_ms < 6000 * self.rate:
                match = self.timeline.locate(camera, end + 1)
                if match and match[0].asset_id != tile.asset_id:
                    candidates.append((end, camera, match))
        if not candidates:
            return
        _, camera, (interval, target) = min(candidates, key=lambda c: c[0])
        if self.prewarm and self.prewarm.asset_id == interval.asset_id:
            return
        if self.prewarm is None:
            self.prewarm = self._idle_tile()
        spare = self.prewarm
        spare.camera, spare.interval = camera, interval
        self._request(spare, interval, target)
        if spare.engine:
            spare.engine.set_volume(0)
        spare.hide()

    def synchronised_tiles(self):
        return [t for t in self.tiles.values() if t.interval]

    def prewarm_candidates(self):
        return self.tiles.items()

    def next_coverage(self):
        starts = [self.timeline.next_start(c, self.reference_ms) for c in self.selected]
        valid = [s for s in starts if s is not None]
        if valid:
            self.seek(min(valid))
        else:
            self.notice.emit("后面没有已索引的所选视角录像")

    def step(self, direction=1):
        self.play(False)
        tile = self.tiles.get(self.main_camera)
        frame = 40.0
        if tile and tile.interval and self.catalog:
            metadata = self.metadata.get(str(self.catalog.source_path(tile.interval.path)), {})
            frame = float(metadata.get("timeline", {}).get("frameDurationMs", 40))
        self.seek(self.reference_ms + direction * frame)

    def evidence(self):
        return [{"camera": camera, "asset_id": tile.asset_id,
                 "media_ms": tile.precise_ms if tile.precise_ms is not None and not self.playing else tile.engine.get_time_ms() if tile.engine and tile.asset_id else None,
                 "frame_source": "ffmpeg_pts" if tile.precise_ms is not None and not self.playing else "native_playback_clock",
                 "reference_ms": tile.actual_ms, "frame_ready": tile.ready and tile.pending is None,
                 "verified_interval": bool(tile.interval and tile.interval.verified)}
                for camera, tile in self.tiles.items() if tile.interval]

    def _precise_request(self, tile, path, target):
        generation, asset_id = self.generation, tile.asset_id
        metadata = self.metadata.get(str(path), {})
        timeline = MediaTimelineIndex.from_dict(metadata["timeline"]) if metadata.get("timeline") else None
        stamp = file_stamp(path)
        # Source identity, current stamp, request, and packet-clock definition
        # must all match. A cache hit never supplies a different frame's PTS.
        key = (str(path), asset_id, stamp, float(target), repr(metadata.get("timeline")))
        cached = self.frame_cache.get(key)
        if cached:
            actual, image = cached
            self.preciseReady.emit((generation, asset_id, tile, actual, image, None))
            return
        tile.status("正在提取精确暂停帧（原片 PTS，最多两路后台解码）…")

        def read():
            if self._closing or generation != self.generation:
                return
            try:
                from .probe import extract_frame
                frame, actual = extract_frame(path, target, timeline, cancelled=lambda: self._closing or generation != self.generation,
                                              image_codec="bmp")
                data = frame.tobytes("raw", "RGB")
                result = QImage(data, frame.width, frame.height, frame.width * 3, QImage.Format.Format_RGB888).copy()
                if file_stamp(path) != stamp:
                    raise OSError("原片在抽帧期间发生变化，请刷新索引")
                if not self._closing:
                    self.frame_cache.put(key, actual, result)
                    self.preciseReady.emit((generation, asset_id, tile, actual, result, None))
            except Exception as exc:
                if not self._closing:
                    self.preciseReady.emit((generation, asset_id, tile, None, None, str(exc)))

        self.frame_jobs.append(self.frame_pool.submit(read))

    def _compatibility_request(self, tile, interval, target, path, metadata):
        self._pause_tile(tile)
        tile.asset_id = interval.asset_id
        tile.stack.hide()
        generation, started = self.generation, time.perf_counter()
        tile.pending = {"asset": interval.asset_id, "generation": generation, "target": target,
                        "phase": "compat_cache", "start": started}
        tile.status("正在准备本段流畅播放 · 无重编码 · 首次完成后复用")
        cache = self.compatibility_cache
        pinned = [t.engine._path for t in self.pool if t.engine and t.engine._path]

        def build():
            if self._closing or self.generation != generation:
                return
            error = None
            try:
                cache.build(interval.asset_id, path, metadata,
                            cancelled=lambda: self._closing or self.generation != generation, pinned=pinned)
            except Exception as exc:
                error = str(exc)
            if not self._closing:
                self.compatibilityReady.emit((generation, tile, interval, target, started, error))

        self.frame_jobs.append(self.frame_pool.submit(build))

    def _compatibility_ready(self, result):
        generation, tile, interval, target, started, error = result
        if self._closing or generation != self.generation or tile.asset_id != interval.asset_id:
            return
        if error:
            tile.pending = None
            tile.status("兼容缓存未启用：" + error)
            self.notice.emit(error)
            return
        self._request(tile, interval, target, queued_start=started)

    def _precise_ready(self, result):
        generation, asset_id, tile, actual, image, error = result
        if generation != self.generation or tile.asset_id != asset_id or not tile.pending or self.playing or self._closing:
            return
        if error:
            tile.pending = None
            self._pause_tile(tile)
            tile.surface.hide()
            tile.stack.hide()
            tile.status("精确帧读取失败：" + error)
            return
        pending = tile.pending
        tile.pending = None
        if tile.engine:
            tile.engine.pause(True)
        tile.precise_ms = actual
        tile.actual_ms = self.timeline.reference_time(tile.camera, tile.interval.wall_at(actual))
        tile.frame_image = image
        tile.stack.setCurrentWidget(tile.frame_view)
        tile.stack.show()
        tile.scale_frame()
        tile.ready = True
        tile.status(f"{wall_text(tile.actual_ms)} · 精确暂停帧 PTS {actual / 1000:.3f}s · 原片", good=True)
        self.latencies.append({"camera": tile.camera, "cold": pending["cold"], "mode": "precise_pause",
                               "seconds": time.perf_counter() - pending["start"], "error_ms": actual - pending["target"]})

    def close(self):
        self._closing = True
        self.timer.stop()
        self.control_timer.stop()
        self.playing = False
        self.frame_pool.shutdown(wait=False, cancel_futures=True)
        self.frame_cache.clear()
        for tile in self.pool:
            if tile.engine:
                tile.engine.close()
        return super().close()
