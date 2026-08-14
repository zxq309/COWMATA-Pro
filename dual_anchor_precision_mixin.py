from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QTimer

from precision_timeline_widgets import (
    PrecisionSignalPlotWidget,
    PrecisionVideoTimelineWidget,
)
from ui_helpers import format_relative


class DualAnchorPrecisionMixin:
    """Independent paused selection, automatic pairing and 10x playback."""

    def __init__(self) -> None:
        self._pending_data_anchor_ms: float | None = None
        self._pending_video_anchor_ms: float | None = None
        self._independent_video_selection_active = False
        super().__init__()
        self._install_precision_timelines()
        self._install_extended_rates()

    def _install_precision_timelines(self) -> None:
        old_plot = self.plot
        plot_parent = old_plot.parentWidget()
        plot_layout = plot_parent.layout()
        plot = PrecisionSignalPlotWidget(plot_parent)
        plot_layout.replaceWidget(old_plot, plot)
        old_plot.hide()
        old_plot.deleteLater()
        self.plot = plot
        self.ui.plot = plot

        try:
            self.auto_y_check.toggled.disconnect()
        except RuntimeError:
            pass
        try:
            self.full_view_btn.clicked.disconnect()
        except RuntimeError:
            pass
        self.auto_y_check.toggled.connect(self.plot.set_auto_y)
        self.full_view_btn.clicked.connect(self.plot.show_all)
        self.plot.seekRequested.connect(self._data_timeline_selected)
        self.plot.rangeSelected.connect(self._range_selected)
        self.plot.eventSelected.connect(self._select_event_by_id)
        self.plot.eventChanged.connect(self._event_changed_on_plot)
        self.plot.set_events(self.labels, self.events)

        old_video_timeline = self.video_timeline
        video_parent = old_video_timeline.parentWidget()
        video_layout = video_parent.layout()
        video_timeline = PrecisionVideoTimelineWidget(video_parent)
        video_layout.replaceWidget(
            old_video_timeline, video_timeline
        )
        old_video_timeline.hide()
        old_video_timeline.deleteLater()
        self.video_timeline = video_timeline
        self.video_timeline.seekRequested.connect(
            self._video_timeline_selected
        )
        if self.media is not None:
            self.video_timeline.set_duration(
                self.media.duration_ms()
            )

    def _install_extended_rates(self) -> None:
        for rate in (6.0, 8.0, 10.0):
            if self.rate_combo.findData(rate) < 0:
                self.rate_combo.addItem(f"{rate:g}×", rate)
        self.rate_combo.setToolTip(
            "播放倍速：0.25×–10×；实际流畅度取决于解码性能"
        )

    def _display_motion_data(self) -> None:
        super()._display_motion_data()
        if hasattr(self.plot, "configure_time_context"):
            self.plot.configure_time_context(
                self.data_create_time_ms
            )

    def _reset_pending_pair(self) -> None:
        self._pending_data_anchor_ms = None
        self._pending_video_anchor_ms = None
        self._independent_video_selection_active = False

    def open_json(self, path: str | None = None) -> None:
        previous = self.data_path
        super().open_json(path)
        if self.data_path and self.data_path != previous:
            self._reset_pending_pair()

    def open_video(self, path: str | None = None) -> None:
        previous = self.video_path
        super().open_video(path)
        if self.video_path and self.video_path != previous:
            self._reset_pending_pair()

    def load_project_dialog(self, path: str | None = None) -> None:
        previous = self._project_path
        super().load_project_dialog(path)
        if self._project_path and self._project_path != previous:
            self._reset_pending_pair()

    def _is_user_playing(self) -> bool:
        return bool(
            self._media_primed
            and self.media is not None
            and self.media.is_playing()
        )

    def _current_video_position(self) -> float:
        if self._pending_video_anchor_ms is not None:
            return float(self._pending_video_anchor_ms)
        if self.media is not None:
            return float(self.media.get_time_ms())
        return float(self.video_timeline._position_ms)

    def _show_pending_pair(self) -> None:
        if self.data is None or not self.video_path:
            return
        data_ms = (
            self._pending_data_anchor_ms
            if self._pending_data_anchor_ms is not None
            else self.playhead_ms
        )
        video_ms = self._current_video_position()
        self.align_status.setText(
            "待配对：九轴 "
            + format_relative(data_ms)
            + " ↔ 视频 "
            + format_relative(video_ms)
            + "；按播放"
        )
        self.align_status.setStyleSheet("color:#a15c00;")
        self.statusBar().showMessage(
            "已分别选择起点；按播放后自动对齐并同步前进",
            5000,
        )

    def _data_timeline_selected(self, data_ms: float) -> None:
        if self.data is None:
            return
        if hasattr(self.plot, "snap_time"):
            data_ms = self.plot.snap_time(data_ms)
        if self._is_user_playing():
            self._reset_pending_pair()
            self.set_playhead(data_ms, seek_video=True)
            return
        self._pending_data_anchor_ms = float(data_ms)
        self._set_playhead_visual(float(data_ms))
        self._show_pending_pair()

    def _video_timeline_selected(self, video_ms: float) -> None:
        if self.media is None or not self.video_path:
            return
        if self._is_user_playing():
            self._reset_pending_pair()
            super()._seek_from_video_timeline(video_ms)
            return

        self._pending_video_anchor_ms = float(video_ms)
        self._independent_video_selection_active = True
        self._clear_ui_seek()
        self._arm_ui_seek(float(video_ms))
        self.video_timeline.set_position(video_ms)
        self.video_clock_label.setText(
            "视频 " + format_relative(video_ms)
        )
        self.wall_clock_label.setText("画面时间：待与九轴起点配对")
        self.video_status.setText(
            f"{Path(self.video_path).name} · 正在定位独立视频起点"
        )
        self.media.set_time_ms(video_ms)
        self._show_pending_pair()

    def _on_media_time(self, video_time_ms: int) -> None:
        if (
            self._independent_video_selection_active
            and not self._is_user_playing()
        ):
            pending = self._ui_pending_seek_video_ms
            if pending is not None:
                if (
                    abs(float(video_time_ms) - pending)
                    > self.SEEK_CONFIRM_TOLERANCE_MS
                    and time.monotonic() < self._ui_seek_deadline
                ):
                    return
                self._clear_ui_seek()
            self._pending_video_anchor_ms = float(video_time_ms)
            self.video_timeline.set_position(video_time_ms)
            self.video_clock_label.setText(
                "视频 " + format_relative(video_time_ms)
            )
            self.wall_clock_label.setText(
                "画面时间：待与九轴起点配对"
            )
            self.video_status.setText(
                f"{Path(self.video_path).name} · 视频起点已选择"
            )
            self._show_pending_pair()
            return
        super()._on_media_time(video_time_ms)

    def _pair_selected_anchors(self) -> bool:
        if (
            self.data is None
            or self.media is None
            or not self.video_path
            or (
                self._pending_data_anchor_ms is None
                and self._pending_video_anchor_ms is None
            )
        ):
            return False

        data_ms = float(
            self._pending_data_anchor_ms
            if self._pending_data_anchor_ms is not None
            else self.playhead_ms
        )
        video_ms = self._current_video_position()
        self.video_start_wall_ms = int(
            round(
                self.data_create_time_ms
                + data_ms
                - video_ms
            )
        )
        self.align_method = "pin"
        self._clear_ui_seek()
        self._independent_video_selection_active = False
        self._pending_data_anchor_ms = None
        self._pending_video_anchor_ms = None
        self._set_playhead_visual(data_ms)
        self.video_timeline.set_position(video_ms)
        self._update_alignment_status()
        self.statusBar().showMessage(
            "已按所选九轴起点与视频起点自动配对",
            5000,
        )
        return True

    def toggle_play(self) -> None:
        if not self._is_user_playing():
            self._pair_selected_anchors()
        super().toggle_play()

    def pin_alignment(self) -> None:
        super().pin_alignment()
        self._reset_pending_pair()

    def align_from_filename(self, silent: bool = False) -> None:
        super().align_from_filename(silent)
        if self.align_method == "filename":
            self._reset_pending_pair()

    def align_from_corner(self) -> None:
        super().align_from_corner()
        if self.align_method == "corner":
            self._reset_pending_pair()

    def nudge_alignment(self, delta_ms: float) -> None:
        super().nudge_alignment(delta_ms)
        if self.align_method == "manual":
            self._reset_pending_pair()
