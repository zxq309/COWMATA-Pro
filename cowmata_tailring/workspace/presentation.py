"""Geometry-only presentation of the existing playback board.

Native video surfaces keep their parents and handles across every layout.
Nothing in this module opens media, seeks, or changes the reference clock.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLayout, QScrollBar, QVBoxLayout, QWidget

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
        self.aux_scroll.hide()
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
            columns = min(len(self.selected), 2 if len(self.selected) <= 4 else 4) or 1
            rows = max(1, math.ceil(len(self.selected) / columns))
            cell_w, cell_h = (w - gap * (columns - 1)) // columns, (h - gap * (rows - 1)) // rows
            positions = {camera: ((i % columns) * (cell_w + gap), (i // columns) * (cell_h + gap),
                                   cell_w, cell_h) for i, camera in enumerate(self.selected)}
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


class DragHeader(QLabel):
    def __init__(self, stage):
        super().__init__("录像 · 拖动标题移动画中画")
        self.stage = stage
        self.origin = None
        self.setFixedHeight(26)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.stage.mode == "C":
            self.origin = event.globalPosition().toPoint() - self.stage.video.pos()

    def mouseMoveEvent(self, event):
        if self.origin is not None:
            pos = event.globalPosition().toPoint() - self.origin
            self.stage.move_pip(pos)

    def mouseReleaseEvent(self, event):
        self.origin = None


class WorkspaceStage(QWidget):
    """Stable-parent video + waveform area, including a draggable PiP."""
    def __init__(self, board, signal_panel, parent=None):
        super().__init__(parent)
        self.mode = "A"
        self.pip_scale = .34
        self.pip_position = (1.0, 0.0)
        self.wave_ratio = .32
        self.signal_panel = signal_panel
        signal_panel.setParent(self)
        self.video = QFrame(self)
        self.video.setObjectName("videoCard")
        layout = QVBoxLayout(self.video)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.drag_header = DragHeader(self)
        header = QHBoxLayout()
        header.addWidget(self.drag_header)
        layout.addLayout(header)
        layout.addWidget(board, 1)
        self.board = board
        self.setMinimumSize(540, 390)

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
        self.drag_header.setVisible(self.mode == "C")
        if self.mode == "C":
            self.signal_panel.setGeometry(0, 0, w, h)
            pw = min(w, max(260, int(w * self.pip_scale)))
            self.pip_top = self.signal_panel.toolbar.sizeHint().height() + 5
            self.pip_bottom = self.signal_panel.scroll.height() + 5
            ph = min(h - self.pip_top - self.pip_bottom, int(pw * 9 / 16) + 88)
            self.video.setGeometry(int((w - pw) * self.pip_position[0]),
                                   self.pip_top + int((h - ph - self.pip_top - self.pip_bottom) * self.pip_position[1]), pw, ph)
            self.video.raise_()
        else:
            wave_h = min(h - 150, max(172, int(h * self.wave_ratio)))
            self.video.setGeometry(0, 0, w, h - wave_h - 8)
            self.signal_panel.setGeometry(0, h - wave_h, w, wave_h)
        self.signal_panel.show()
        self.video.show()

    def move_pip(self, pos: QPoint):
        xspan = max(1, self.width() - self.video.width())
        top = getattr(self, "pip_top", 0)
        bottom = getattr(self, "pip_bottom", 0)
        yspan = max(1, self.height() - self.video.height() - top - bottom)
        self.pip_position = (max(0, min(1, pos.x() / xspan)), max(0, min(1, (pos.y() - top) / yspan)))
        self.arrange()
