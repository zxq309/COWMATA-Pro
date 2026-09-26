"""Geometry-only presentation of the existing playback board.

Native video surfaces keep their parents and handles across every layout.
Nothing in this module opens media, seeks, or changes the reference clock.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QPushButton,
    QScrollBar,
    QVBoxLayout,
    QWidget,
)

from .adaptive_playback import AdaptiveVideoBoard


class PresentationVideoBoard(AdaptiveVideoBoard):
    focusRequested = Signal(str)

    def __init__(self, parent=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.presentation = "A"
        self.single_camera_only = False
        self.observation_ratio = 75
        self.aux_scroll = QScrollBar(Qt.Orientation.Vertical, self)
        self.aux_scroll.valueChanged.connect(self.relayout)
        self.empty = QLabel("打开工程后，在素材面板勾选视角", self)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("color:#a4b8b8; background:#17282c; border-radius:12px; font-size:16px")
        self._pending_tile_shows = []
        self._tile_show_queued = False
        self._spare_surface_queued = False

    def set_presentation(self, mode):
        self.presentation = mode
        self.relayout()

    def set_single_camera_only(self, enabled):
        self.single_camera_only = bool(enabled)
        if enabled:
            self.set_policy("focus")
        self.seek(self.reference_ms)
        self.relayout()

    def set_policy(self, policy):
        super().set_policy("focus" if getattr(self, "single_camera_only", False) else policy)

    def is_preview(self, camera):
        if getattr(self, "single_camera_only", False) and camera != self.main_camera:
            return True  # Hidden views are not current evidence, even paused.
        return super().is_preview(camera)

    def _position(self, camera, tile, *, force=False):
        if self.single_camera_only and camera != self.main_camera:
            self._pause_tile(tile)
            tile.pending = None
            tile.ready = False
            tile.precise_ms = None
            tile.actual_ms = None
            tile._preview_only = True
            tile.stack.hide()
            return  # No background preview decode in algorithm inspection.
        super()._position(camera, tile, force=force)

    def set_main(self, camera):
        previous = self.main_camera
        super().set_main(camera)
        if self.single_camera_only and previous != self.main_camera:
            self.seek(self.reference_ms)

    def _preview_position(self, camera, tile):
        if self.single_camera_only and camera != self.main_camera:
            self._position(camera, tile)
            return
        super()._preview_position(camera, tile)

    def enlarge(self, camera=None):
        if self.presentation == "C" and camera:
            self.focusRequested.emit(camera)
            return
        super().enlarge(camera)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.relayout()

    def relayout(self):
        if getattr(self, "_closing", False):
            return
        if getattr(self, "playing", False):
            # A layout choice and main-camera change can arrive in one UI turn.
            # Apply their final geometry once, not two costly native resizes.
            if not getattr(self, "_live_layout_queued", False):
                self._live_layout_queued = True
                QTimer.singleShot(0, self._flush_live_layout)
            return
        self._apply_layout()

    def _flush_live_layout(self):
        self._live_layout_queued = False
        if not getattr(self, "_closing", False):
            self._apply_layout()

    def _apply_layout(self):
        if getattr(self, "_laying_out", False):
            self._layout_pending = True
            return
        if getattr(self, "_closing", False):
            return
        self._laying_out = True
        self._layout_pending = False
        updates = self.updatesEnabled()
        # Parent repaint suppression also affects embedded native surfaces.
        # Full-playback tests found long stalls when toggling it around GPU
        # layout changes. Keep live full renderers enabled; batch other layouts.
        batch = not getattr(self, "playing", False)
        if batch:
            self.setUpdatesEnabled(False)
        try:
            self._relayout_geometry()
        finally:
            if batch:
                self.setUpdatesEnabled(updates)
            self._laying_out = False
        if self._layout_pending:
            QTimer.singleShot(0, self.relayout)

    def _relayout_geometry(self):
        if not hasattr(self, "aux_scroll"):
            return
        while self.grid.count():
            self.grid.takeAt(0)
        w, h, gap = self.width(), self.height(), 8
        self.empty.setGeometry(self.rect())
        self.empty.setVisible(not self.selected)
        visible = set()
        expanded = self.expanded if self.expanded in self.selected else None
        if self.single_camera_only and self.main_camera in self.selected:
            expanded = self.main_camera
        if self.presentation == "C" and self.main_camera in self.selected:
            expanded = self.main_camera
        scrolling = False
        if expanded:
            positions = {expanded: (0, 0, w, h)}
        elif self.presentation == "A" and len(self.selected) > 1:
            # Auxiliary views scroll independently; the main view never shrinks
            # to accommodate eight minimum-height native video surfaces.
            fraction = self.observation_ratio / 100 if len(self.selected) == 2 else .75
            aux_width = max(170, int(w * (1 - fraction)))
            main_width = max(1, w - aux_width - gap)
            positions = {self.main_camera: (0, 0, main_width, h)}
            auxiliary = [c for c in self.selected if c != self.main_camera]
            tile_h = max(130, int((aux_width - 14) * 9 / 16) + 50)
            content_h = len(auxiliary) * (tile_h + gap) - gap
            self.aux_scroll.blockSignals(True)
            self.aux_scroll.setRange(0, max(0, content_h - h))
            self.aux_scroll.setPageStep(h)
            self.aux_scroll.blockSignals(False)
            scrolling = content_h > h
            self.aux_scroll.setVisible(scrolling)
            self.aux_scroll.setGeometry(w - 12, 0, 12, h)
            for index, camera in enumerate(auxiliary):
                positions[camera] = (main_width + gap, index * (tile_h + gap) - self.aux_scroll.value(),
                                     aux_width - (16 if scrolling else 0), tile_h)
        else:
            # The old grid-column preference is intentionally not inherited.
            # B promises a complete grid; eight rows would clip native surfaces.
            count = max(1, len(self.selected))
            def picture_area(columns):
                rows = math.ceil(count / columns)
                width = max(1, (w-gap*(columns-1))/columns)
                height = max(1, (h-gap*(rows-1))/rows-69)
                return min(width, height*16/9)**2*9/16
            columns = max(range(1, count+1), key=picture_area)
            rows = max(1, math.ceil(len(self.selected) / columns))
            cell_w, cell_h = (w - gap * (columns - 1)) // columns, (h - gap * (rows - 1)) // rows
            positions = {camera: ((i % columns) * (cell_w + gap), (i // columns) * (cell_h + gap),
                                   cell_w, cell_h) for i, camera in enumerate(self.selected)}
        # Hiding and showing a pressed scrollbar loses Qt's mouse grab. Keep
        # it visible for the whole gesture; only layout-mode changes hide it.
        self.aux_scroll.setVisible(scrolling)
        pending_shows = []
        for camera, geometry in positions.items():
            tile = self.tiles[camera]
            visible.add(tile)
            tile.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
            tile.setMinimumSize(0, 0)
            tile.title.setFixedHeight(25)
            tile.zoom.setFixedHeight(25)
            tile.message.setFixedHeight(19)
            tile.message.setWordWrap(False)
            tile.message.setToolTip(tile.message.text())
            main = camera == self.main_camera
            style = ("QFrame {background:#17282c; border:1px solid " +
                              ("#8add66" if main else "#30464b") + "; border-radius:8px} "
                              "QPushButton {color:#d8e9e9; background:#20383d; border:0; padding:1px 6px; border-radius:5px} "
                              "QLabel {border:0; font-size:11px}")
            if tile.styleSheet() != style:
                tile.setStyleSheet(style)
            tile.setGeometry(*geometry)
            if tile.isHidden():
                pending_shows.append(tile)
        for tile in self.pool:
            if tile not in visible:
                tile.hide()
        # QWidget.show creates native child windows on Windows. Eight cold
        # surfaces took over one second in a single history-load callback.
        # Keep geometry synchronous, but yield to input between native shows.
        self._pending_tile_shows = pending_shows
        if pending_shows and not self._tile_show_queued:
            self._tile_show_queued = True
            QTimer.singleShot(0, self._show_next_tile)
        elif not pending_shows:
            self._queue_spare_surface()
        self.aux_scroll.raise_()

    def _show_next_tile(self):
        self._tile_show_queued = False
        if getattr(self, "_closing", False):
            self._pending_tile_shows.clear()
            return
        if self._pending_tile_shows:
            self._pending_tile_shows.pop(0).show()
        # A newer layout replaces this queue, so hidden/removed views cannot
        # be resurrected by an earlier selection's deferred callback.
        if self._pending_tile_shows and not self._tile_show_queued:
            self._tile_show_queued = True
            QTimer.singleShot(0, self._show_next_tile)
        elif not self._pending_tile_shows:
            self._queue_spare_surface()

    def _queue_spare_surface(self):
        if (self._spare_surface_queued or self._closing or self.playing or self._pending_tile_shows
                or not self.catalog or not self.selected or not self.isVisible()):
            return
        self._spare_surface_queued = True
        QTimer.singleShot(0, self._prepare_spare_surface)

    def _prepare_spare_surface(self):
        self._spare_surface_queued = False
        # Recheck the current layout, playback and lifetime; an earlier queued
        # callback must not allocate after closing or while playback is busy.
        if (self._closing or self.playing or self._pending_tile_shows
                or not self.catalog or not self.selected or not self.isVisible()):
            return
        if self.prewarm is None:
            self.prewarm = self._idle_tile()
        # Prepare the spare HWND before playback so first creation is not paid
        # in the prewarm callback. Only create its handle on this paused UI turn;
        # the existing prewarm algorithm still decides when to open a decoder.
        if not self.prewarm.surface.testAttribute(Qt.WidgetAttribute.WA_WState_Created):
            self.prewarm.surface.winId()


class DragHeader(QLabel):
    def __init__(self, stage):
        super().__init__("录像 · 拖动标题移动画中画")
        self.stage = stage
        self.origin = None
        self.setFixedHeight(26)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.stage.pip_active():
            self.origin = event.globalPosition().toPoint() - self.stage.video.pos()
            # Grab the pointer so the drag survives fast moves that leave the
            # 26px header; without the grab the drag silently breaks and the
            # PiP appears immovable.
            self.grabMouse()

    def mouseMoveEvent(self, event):
        if self.origin is not None:
            pos = event.globalPosition().toPoint() - self.origin
            self.stage.move_pip(pos)

    def mouseReleaseEvent(self, event):
        self.origin = None
        if self.mouseGrabber() is self:
            self.releaseMouse()

    def mouseDoubleClickEvent(self, event):
        if self.stage.mode == "C" and self.stage.wave_window is None:
            self.stage.toggle_video_focus()


class WaveformWindow(QWidget):
    """Separate top-level window for the waveform, e.g. on a second monitor.

    The waveform is plain Qt painting, so it can move between windows; the
    native video surfaces never change parent (see module docstring). While
    this window is active, the main window's labelling and playback hotkeys
    keep working through mirrored shortcuts.
    """

    def __init__(self, stage, owner):
        super().__init__(owner, Qt.WindowType.Window)
        self.stage, self.owner = stage, owner
        self.setWindowTitle("波形 · 分屏（关闭此窗口即合并回主界面）")
        self.setMinimumSize(640, 360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.mirrors = []

    def mirror_shortcuts(self):
        """Mirror every hotkey of the main window, rebuilt on each activation."""
        for item in self.mirrors:
            if isinstance(item, QAction):
                self.removeAction(item)
            else:
                item.setEnabled(False)
                item.deleteLater()
        self.mirrors = []
        window_context = Qt.ShortcutContext.WindowShortcut
        for action in self.owner.findChildren(QAction):
            if action.shortcut().isEmpty() or action.shortcutContext() != window_context:
                continue
            if any(self.owns_hotkey(item) for item in action.associatedObjects()):
                self.addAction(action)
                self.mirrors.append(action)
        for shortcut in self.owner.findChildren(QShortcut):
            if (shortcut.key().isEmpty() or not shortcut.isEnabled()
                    or shortcut.context() != window_context or not self.owns_hotkey(shortcut.parent())):
                continue
            proxy = QShortcut(QKeySequence(shortcut.key()), self)
            proxy.setAutoRepeat(shortcut.autoRepeat())
            proxy.activated.connect(shortcut.activated.emit)
            self.mirrors.append(proxy)

    def owns_hotkey(self, widget):
        """True for main-window hotkeys, not those of other child windows."""
        if not isinstance(widget, QWidget) or widget is self or self.isAncestorOf(widget):
            return False
        while isinstance(widget, QMenu) and widget.parentWidget() is not None:
            widget = widget.parentWidget()  # popup menus are their own window
        return widget.window() is self.owner

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.mirror_shortcuts()

    def closeEvent(self, event):
        # The window is reused, never destroyed on close: the waveform goes
        # back to the main stage and this shell is only hidden.
        event.ignore()
        self.stage.dock_waveform()


class WorkspaceStage(QWidget):
    """Stable-parent video + waveform area, including a draggable PiP.

    Operators identify the cow's field mark in the video while locking the
    feature interval in the waveform, so the video can grow: 放大视频 keeps a
    waveform strip under a window-wide video (F11 makes it full screen), and
    波形分屏 moves the waveform to its own window so the video fills the stage.
    """
    FOCUS_WAVE_RATIO = .32

    def __init__(self, board, signal_panel, parent=None):
        super().__init__(parent)
        self.mode = "A"
        self.pip_scale = .34
        self.pip_position = (1.0, 0.0)
        self.wave_ratio = .55
        self.video_focus = False
        self.wave_window = None
        self._wave_shell = None
        self.signal_panel = signal_panel
        signal_panel.setParent(self)
        self.video = QFrame(self)
        self.video.setObjectName("videoCard")
        layout = QVBoxLayout(self.video)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.drag_header = DragHeader(self)
        header = QHBoxLayout()
        header.setSpacing(4)
        header.addWidget(self.drag_header, 1)
        self.focus_button = QPushButton("放大视频")
        self.focus_button.setCheckable(True)
        self.focus_button.setToolTip("视频铺满窗口宽度，下方保留波形（双击标题同样切换；F11 全屏）")
        self.focus_button.clicked.connect(self.toggle_video_focus)
        self.split_button = QPushButton("波形分屏")
        self.split_button.setCheckable(True)
        self.split_button.setToolTip("把波形移到独立窗口（可拖到第二块屏幕），主窗口只显示视频；关闭该窗口即合并回来")
        self.split_button.clicked.connect(self.toggle_waveform_window)
        for button in (self.focus_button, self.split_button):
            button.setFixedHeight(24)
            header.addWidget(button)
        self.header_buttons = (self.focus_button, self.split_button)
        layout.addLayout(header)
        layout.addWidget(board, 1)
        self.board = board
        self.setMinimumSize(540, 390)

    def pip_active(self):
        return self.mode == "C" and not self.video_focus and self.wave_window is None

    def toggle_video_focus(self, *_):
        self.video_focus = not self.video_focus
        self.arrange()

    def toggle_waveform_window(self, *_):
        if self.wave_window is None:
            self.undock_waveform()
        else:
            self.dock_waveform()

    def undock_waveform(self):
        if self.wave_window is not None:
            self.wave_window.raise_()
            self.wave_window.activateWindow()
            return
        owner = self.window()
        window = self._wave_shell
        if window is None or window.owner is not owner:
            window = self._wave_shell = WaveformWindow(self, owner)
        self.signal_panel.setParent(window)
        window.layout().addWidget(self.signal_panel)
        self.signal_panel.show()
        self.wave_window = window
        # Prefer a second monitor; otherwise open beside the main window.
        here = owner.screen() if owner is not None else None
        others = [s for s in QGuiApplication.screens() if s is not here]
        if others:
            window.setGeometry(others[0].availableGeometry())
            window.showMaximized()
        else:
            geometry = owner.geometry() if owner is not None else None
            if geometry is not None:
                window.resize(max(800, geometry.width() // 2), max(420, geometry.height() // 2))
            window.show()
        window.mirror_shortcuts()
        self.arrange()

    def dock_waveform(self):
        window, self.wave_window = self.wave_window, None
        if window is None:
            return
        window.layout().removeWidget(self.signal_panel)
        self.signal_panel.setParent(self)
        self.signal_panel.show()
        window.hide()
        self.arrange()

    def set_mode(self, mode):
        if mode not in {"A", "B", "C"}:
            raise ValueError("Invalid presentation")
        self.mode = mode
        self.board.set_presentation(mode)
        self.arrange()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.arrange()

    def arrange(self):
        w, h = self.width(), self.height()
        detached = self.wave_window is not None
        self.focus_button.setChecked(self.video_focus)
        self.split_button.setChecked(detached)
        show_header = self.mode == "C" or detached
        self.drag_header.setVisible(show_header)
        self.focus_button.setVisible(self.mode == "C" and not detached)
        self.split_button.setVisible(show_header)
        self.drag_header.setText("录像 · 波形已分屏到独立窗口" if detached else
                                 "录像 · 放大查看（双击标题恢复画中画）" if self.video_focus else
                                 "录像 · 拖动标题移动画中画，双击放大")
        if detached:
            self.video.setGeometry(0, 0, w, h)
            self.video.show()
            return
        if self.mode == "C" and self.video_focus:
            wave_h = max(200, int(h * self.FOCUS_WAVE_RATIO))
            self.video.setGeometry(0, 0, w, max(120, h - wave_h - 6))
            self.signal_panel.setGeometry(0, h - wave_h, w, wave_h)
        elif self.mode == "C":
            self.signal_panel.setGeometry(0, 0, w, h)
            pw = min(w, max(260, int(w * self.pip_scale)))
            self.pip_top = self.signal_panel.toolbar.sizeHint().height() + 5
            self.pip_bottom = self.signal_panel.scroll.height() + 5
            ph = min(h - self.pip_top - self.pip_bottom, int(pw * 9 / 16) + 88)
            self.video.setGeometry(int((w - pw) * self.pip_position[0]),
                                   self.pip_top + int((h - ph - self.pip_top - self.pip_bottom) * self.pip_position[1]), pw, ph)
            self.video.raise_()
        else:
            available = max(120, h - 150)
            wave_h = min(available, max(240, int(h * self.wave_ratio)))
            self.video.setGeometry(0, 0, w, h - wave_h - 8)
            self.signal_panel.setGeometry(0, h - wave_h, w, wave_h)
        self.signal_panel.show()
        self.video.show()

    def move_pip(self, pos: QPoint):
        xspan = max(1, self.width() - self.video.width())
        top = getattr(self, "pip_top", 0)
        bottom = getattr(self, "pip_bottom", 0)
        yspan = max(1, self.height() - self.video.height() - top - bottom)
        before = self.video.geometry()
        self.pip_position = (max(0, min(1, pos.x() / xspan)), max(0, min(1, (pos.y() - top) / yspan)))
        self.arrange()
        if self.pip_active() and self.video.geometry() != before:
            # Moving native video children can copy stale border pixels into
            # the backing store. Recompose the exposed waveform from its cache;
            # update() coalesces drag events and does not rebuild the curves.
            self.signal_panel.wave.update()
