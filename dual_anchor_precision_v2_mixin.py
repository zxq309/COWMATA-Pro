from __future__ import annotations

import json
import os
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QLabel

import annotation_core
from precision_timeline_widgets_v2 import (
    PrecisionSignalPlotWidget,
    PrecisionVideoTimelineWidget,
)
from ui_helpers import format_relative


class DualAnchorPrecisionV2Mixin:
    """Independent paused anchors, confirmed pairing and safe 10x playback."""

    VIEW_SCALE_PRESETS = (
        ("1 分钟", 60_000.0),
        ("2 分钟", 120_000.0),
        ("3 分钟", 180_000.0),
        ("4 分钟", 240_000.0),
        ("5 分钟", 300_000.0),
        ("10 分钟", 600_000.0),
    )
    VIEW_SCALE_ALL = -1.0
    VIEW_SCALE_CUSTOM = -2.0

    def __init__(self) -> None:
        self._timelines_linked = False
        self._source_switch_active = False
        self._source_switch_kind = ""
        self._source_switch_snapshot: dict[str, object] = {}
        self._source_switch_mapping_committed = False
        self._pending_data_anchor_ms: float | None = None
        self._pending_video_anchor_ms: float | None = None
        self._independent_video_selection_active = False
        self._play_requested_after_anchor_confirmation = False
        super().__init__()
        self._sync_pin_button()
        self._install_precision_timelines()
        self._install_extended_rates()
        self._install_view_scale_selector()

    # ------------------------------------------------------------------
    # Precision widgets and data timing
    # ------------------------------------------------------------------

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
        # The main data panel is reserved for the eleven sensor channels.
        # Annotation labels and events remain available in the editor below,
        # but no longer consume fixed-height rows inside this plot.
        self.plot.set_lanes_visible(False)

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
        video_layout.replaceWidget(old_video_timeline, video_timeline)
        old_video_timeline.hide()
        old_video_timeline.deleteLater()
        self.video_timeline = video_timeline
        # Keep the release build's 90 ms debounce.  It is essential for
        # dragging through long-GOP HEVC without creating a seek storm.
        self.video_timeline.seekRequested.connect(
            self._queue_video_timeline_seek
        )
        if self.media is not None:
            self.video_timeline.set_duration(self.media.duration_ms())

    # ------------------------------------------------------------------
    # View scale quick selector (1/2/3/4/5/10 分钟 / 全部)
    # ------------------------------------------------------------------

    def _install_view_scale_selector(self) -> None:
        nav_layout = self.full_view_btn.parentWidget().layout()
        combo = QComboBox()
        for text, span_ms in self.VIEW_SCALE_PRESETS:
            combo.addItem(text, span_ms)
        combo.addItem("全部", self.VIEW_SCALE_ALL)
        combo.addItem("自定义", self.VIEW_SCALE_CUSTOM)
        combo.setCurrentIndex(combo.findData(self.VIEW_SCALE_ALL))
        combo.setMaximumWidth(96)
        combo.setToolTip(
            "时间尺度：一键切换九轴图窗口宽度，以当前播放头为中心"
        )
        label = QLabel("时间尺度")
        nav_layout.insertWidget(3, label)
        nav_layout.insertWidget(4, combo)
        self.view_scale_combo = combo
        combo.activated.connect(self._view_scale_chosen)
        self.plot.viewChanged.connect(self._view_scale_sync)

    def _view_scale_chosen(self, index: int) -> None:
        combo = self.view_scale_combo
        span = float(combo.itemData(index))
        if span == self.VIEW_SCALE_CUSTOM:
            # “自定义”仅作状态显示，不改变视图。
            self._view_scale_sync(*self.plot.view_range)
            return
        if span < 0:
            self.plot.show_all()
            return
        duration = float(getattr(self.plot, "_duration_ms", 0.0) or 0.0)
        if duration <= 1.0:
            return
        span = min(span, duration)
        center = float(getattr(self.plot, "_playhead_ms", 0.0) or 0.0)
        self.plot.set_view(
            center - span * 0.35, center + span * 0.65, True
        )

    def _view_scale_sync(self, t0: float, t1: float) -> None:
        combo = getattr(self, "view_scale_combo", None)
        if combo is None:
            return
        span = float(t1) - float(t0)
        duration = float(getattr(self.plot, "_duration_ms", 0.0) or 0.0)
        target = combo.findData(self.VIEW_SCALE_CUSTOM)
        if duration > 1.0 and span >= duration * 0.999:
            target = combo.findData(self.VIEW_SCALE_ALL)
        else:
            for _text, preset in self.VIEW_SCALE_PRESETS:
                if abs(span - preset) <= preset * 0.02:
                    target = combo.findData(preset)
                    break
        combo.blockSignals(True)
        combo.setCurrentIndex(target)
        combo.blockSignals(False)

    def _display_motion_data(self) -> None:
        super()._display_motion_data()
        combo = getattr(self, "view_scale_combo", None)
        if combo is not None:
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(self.VIEW_SCALE_ALL))
            combo.blockSignals(False)
        if self.data is None:
            return
        self.plot.set_time_reference(
            self.data.times_ms,
            self.data_create_time_ms,
            getattr(self.data, "timestamp_mode", ""),
            getattr(self.data, "gap_threshold_ms", 100.0),
        )

    # ------------------------------------------------------------------
    # Playback rate
    # ------------------------------------------------------------------

    def _install_extended_rates(self) -> None:
        for rate in (6.0, 8.0, 10.0):
            if self.rate_combo.findData(rate) < 0:
                self.rate_combo.addItem(f"{rate:g}×", rate)
        self.rate_combo.setToolTip(
            "播放倍速：0.25×–10×；实际流畅度取决于解码性能"
        )

    def _apply_rate(self, _index: int | None = None) -> None:
        if self.media is None or not self.video_path:
            return
        desired = float(self.rate_combo.currentData() or 1.0)
        try:
            accepted = bool(self.media.set_rate(desired))
            actual = float(self.media.get_rate())
        except Exception:
            accepted = False
            actual = 1.0
        accepted = (
            accepted
            and actual > 0
            and abs(actual - desired) <= max(0.05, desired * 0.02)
        )
        if accepted:
            return

        available = [
            float(self.rate_combo.itemData(index))
            for index in range(self.rate_combo.count())
            if self.rate_combo.itemData(index) is not None
        ]
        fallback = (
            min(available, key=lambda value: abs(value - actual))
            if available and actual > 0
            else 1.0
        )
        index = self.rate_combo.findData(fallback)
        self.rate_combo.blockSignals(True)
        try:
            if index >= 0:
                self.rate_combo.setCurrentIndex(index)
        finally:
            self.rate_combo.blockSignals(False)
        try:
            self.media.set_rate(fallback)
        except Exception:
            pass
        self.statusBar().showMessage(
            f"当前视频不接受 {desired:g}×，已恢复为 {fallback:g}×",
            5000,
        )

    def _smooth_playhead_tick(self) -> None:
        if self._source_switch_active or (
            self.video_path and not self._timelines_are_linked()
        ):
            return
        # SmoothPlayheadMixin follows the actual playing state and uses VLC's
        # sparse time reports only for calibration.  The former 400 ms media-
        # time ceiling made 1x freeze after 400 ms and 10x after only 40 ms.
        super()._smooth_playhead_tick()

    # ------------------------------------------------------------------
    # Pending dual-anchor state
    # ------------------------------------------------------------------

    def _reset_pending_pair(self, cancel_queued_seek: bool = False) -> None:
        self._pending_data_anchor_ms = None
        self._pending_video_anchor_ms = None
        self._independent_video_selection_active = False
        self._play_requested_after_anchor_confirmation = False
        if cancel_queued_seek:
            if hasattr(self, "_video_seek_timer"):
                self._video_seek_timer.stop()
            if hasattr(self, "_pending_video_seek_ms"):
                self._pending_video_seek_ms = None
            if hasattr(self, "_clear_ui_seek"):
                self._clear_ui_seek()

    def _sync_pin_button(self) -> None:
        button = getattr(self, "pin_btn", None)
        if button is None:
            return
        linked = bool(getattr(self, "_timelines_linked", False))
        button.blockSignals(True)
        button.setChecked(linked)
        button.setText(
            "✓ 已钉住（点击解除）"
            if linked
            else "钉住 当前帧=播放头"
        )
        button.blockSignals(False)
        button.setStyleSheet(
            "background:#dff4e8;color:#11643a;font-weight:600;"
            if linked
            else "background:#fff4d8;color:#8a5200;font-weight:600;"
        )

    def _set_timelines_linked(self, linked: bool) -> None:
        self._timelines_linked = bool(linked)
        self._sync_pin_button()
        if linked:
            self._reset_pending_pair()
        if hasattr(self, "align_status"):
            self._update_alignment_status()
        if hasattr(self, "play_btn"):
            self._refresh_enabled()

    def _begin_source_switch(self, kind: str) -> None:
        was_playing = self._is_user_playing()
        self._source_switch_active = True
        self._source_switch_kind = str(kind)
        self._source_switch_mapping_committed = False
        self._source_switch_snapshot = {
            "linked": bool(self._timelines_linked),
            "data_ms": float(self.playhead_ms),
            "view": tuple(self.plot.view_range),
            "data_duration_ms": float(self.data_duration_ms),
            "video_ms": (
                float(self.media.get_time_ms())
                if self.media is not None and self.video_path
                else 0.0
            ),
            "video_start_wall_ms": self.video_start_wall_ms,
            "align_method": self.align_method,
            "was_playing": was_playing,
        }
        if was_playing and self.media is not None:
            self.media.pause(True)
            self._source_switch_snapshot["video_ms"] = float(
                self.media.get_time_ms()
            )
        if hasattr(self, "pin_btn"):
            self._refresh_enabled()

    def _cancel_source_switch(self) -> None:
        snapshot = dict(self._source_switch_snapshot)
        saved_wall = snapshot.get("video_start_wall_ms")
        self.video_start_wall_ms = (
            None if saved_wall is None else int(saved_wall)
        )
        self.align_method = str(snapshot.get("align_method", self.align_method))
        self._source_switch_active = False
        self._source_switch_kind = ""
        self._source_switch_mapping_committed = False
        self._source_switch_snapshot = {}
        if self.data is not None:
            self._set_playhead_visual(
                float(snapshot.get("data_ms", self.playhead_ms))
            )
            view = snapshot.get("view")
            if isinstance(view, (tuple, list)) and len(view) == 2:
                self.plot.set_view(float(view[0]), float(view[1]))
        if self.media is not None and self.video_path:
            self.media.set_time_ms(float(snapshot.get("video_ms", 0.0)))
        self._set_timelines_linked(bool(snapshot.get("linked", False)))
        if (
            snapshot.get("was_playing")
            and self.media is not None
            and self.video_path
            and not self.media.is_playing()
        ):
            self._apply_rate()
            self.media.play()

    def _complete_video_source_switch(self) -> None:
        if not (
            self._source_switch_active
            and self._source_switch_kind == "video"
            and self._source_switch_mapping_committed
        ):
            return
        snapshot = dict(self._source_switch_snapshot)
        data_ms = float(snapshot.get("data_ms", self.playhead_ms))
        view = snapshot.get("view", self.plot.view_range)
        self._source_switch_active = False
        self._source_switch_kind = ""
        self._source_switch_mapping_committed = False
        self._source_switch_snapshot = {}
        self._set_playhead_visual(data_ms)
        if isinstance(view, (tuple, list)) and len(view) == 2:
            self.plot.set_view(float(view[0]), float(view[1]))
        self._set_timelines_linked(bool(snapshot.get("linked", False)))
        self._update_alignment_status()
        self.statusBar().showMessage(
            "视频分段续接完成：九轴位置保持在 "
            + format_relative(data_ms),
            5000,
        )

    def _restore_new_json_view(self, snapshot: dict[str, object]) -> None:
        view = snapshot.get("view")
        if not isinstance(view, (tuple, list)) or len(view) != 2:
            return
        old_duration = float(snapshot.get("data_duration_ms", 0.0) or 0.0)
        span = max(1.0, float(view[1]) - float(view[0]))
        if old_duration > 1.0 and span >= old_duration * 0.999:
            self.plot.show_all()
            return
        self.plot.set_view(0.0, min(float(self.data_duration_ms), span))

    def open_json(self, path: str | None = None) -> None:
        previous_path = self.data_path
        previous_data = self.data
        switching = bool(
            not self._restoring_project
            and previous_data is not None
            and self.media is not None
            and self.video_path
        )
        if switching:
            self._begin_source_switch("json")
        try:
            super().open_json(path)
        except Exception:
            if switching:
                self._cancel_source_switch()
            raise
        changed = self.data is not None and self.data is not previous_data
        changed_path = bool(
            self.data_path
            and previous_path
            and os.path.normcase(os.path.abspath(self.data_path))
            != os.path.normcase(os.path.abspath(previous_path))
        )
        if not changed:
            if switching:
                self._cancel_source_switch()
            return
        self._reset_pending_pair(cancel_queued_seek=True)
        if self._restoring_project:
            return
        if not switching or not changed_path:
            self._source_switch_active = False
            self._source_switch_kind = ""
            self._source_switch_snapshot = {}
            self._source_switch_mapping_committed = False
            self._set_timelines_linked(
                self.video_start_wall_ms is not None
                and self.align_method not in {"none", "default"}
            )
            return

        snapshot = dict(self._source_switch_snapshot)
        video_ms = float(snapshot.get("video_ms", 0.0))
        self.video_start_wall_ms = int(
            round(self.data_create_time_ms - video_ms)
        )
        self.align_method = "data_continuation"
        self._set_playhead_visual(0.0)
        self.video_timeline.set_position(video_ms)
        self._restore_new_json_view(snapshot)
        self._source_switch_mapping_committed = True
        self._source_switch_active = False
        self._source_switch_kind = ""
        self._source_switch_snapshot = {}
        self._source_switch_mapping_committed = False
        self._set_timelines_linked(bool(snapshot.get("linked", False)))
        if self.media is not None:
            self.media.set_time_ms(video_ms)
        self._update_alignment_status()
        self.statusBar().showMessage(
            "JSON 续接完成：视频保持在 " + format_relative(video_ms),
            5000,
        )

    def open_video(self, path: str | None = None) -> None:
        previous_path = self.video_path
        previous_current = (
            self.media.current_path if self.media is not None else ""
        )
        previous_primed = getattr(self, "_media_primed", False)
        switching = bool(
            not self._restoring_project
            and self.data is not None
            and (previous_current or previous_path)
        )
        if switching:
            self._begin_source_switch("video")
        requested = (
            os.path.abspath(os.fspath(path))
            if isinstance(path, (str, os.PathLike))
            else None
        )
        try:
            super().open_video(path)
        except Exception:
            if switching:
                self._cancel_source_switch()
            raise
        current = self.media.current_path if self.media is not None else ""
        opened = (
            bool(current)
            and bool(self.video_path)
            and os.path.normcase(os.path.abspath(current))
            == os.path.normcase(os.path.abspath(self.video_path))
        )
        if requested is not None:
            opened = (
                opened
                and os.path.normcase(os.path.abspath(current))
                == os.path.normcase(requested)
            )
        reopened_same = (
            bool(current)
            and current == previous_current
            and previous_primed
            and not getattr(self, "_media_primed", False)
        )
        if opened and (
            requested is not None
            or current != previous_current
            or reopened_same
        ):
            self._reset_pending_pair(cancel_queued_seek=True)

        previous_media = previous_current or previous_path
        switched_segment = (
            opened
            and bool(previous_media)
            and os.path.normcase(os.path.abspath(current))
            != os.path.normcase(os.path.abspath(previous_media))
        )
        if (
            switched_segment
            and self.data is not None
            and not self._restoring_project
        ):
            snapshot = dict(self._source_switch_snapshot)
            preserved_playhead = float(
                snapshot.get("data_ms", self.playhead_ms)
            )
            preserved_view = snapshot.get("view", self.plot.view_range)
            self.video_start_wall_ms = int(
                round(self.data_create_time_ms + preserved_playhead)
            )
            self.align_method = "continuation"
            self._source_switch_mapping_committed = True
            self._timelines_linked = bool(snapshot.get("linked", False))
            self._sync_pin_button()
            self._reset_pending_pair(cancel_queued_seek=True)
            self._update_alignment_status()
            self.set_playhead(preserved_playhead, seek_video=True)
            if (
                isinstance(preserved_view, (tuple, list))
                and len(preserved_view) == 2
            ):
                self.plot.set_view(
                    float(preserved_view[0]), float(preserved_view[1])
                )
            self.statusBar().showMessage(
                "正在续接下一段视频：九轴冻结在 "
                + format_relative(preserved_playhead),
                5000,
            )
            if getattr(self, "_media_primed", False):
                self._complete_video_source_switch()
            return

        if switching:
            self._cancel_source_switch()
        if opened and not self._restoring_project:
            self._set_timelines_linked(
                self.video_start_wall_ms is not None
                and self.align_method not in {"none", "default"}
            )

    def load_project_dialog(self, path: str | None = None) -> None:
        previous_project = self._project_path
        previous_data = self.data
        super().load_project_dialog(path)
        if self._project_path and (
            self._project_path != previous_project
            or self.data is not previous_data
        ):
            self._reset_pending_pair(cancel_queued_seek=True)
            try:
                raw = json.loads(
                    Path(self._project_path).read_text(encoding="utf-8-sig")
                )
            except (OSError, ValueError, TypeError):
                raw = {}
            align = raw.get("align", {}) if isinstance(raw, dict) else {}
            ui = raw.get("ui", {}) if isinstance(raw, dict) else {}
            if not isinstance(align, dict):
                align = {}
            if not isinstance(ui, dict):
                ui = {}
            saved_linked = align.get("locked", ui.get("timelinesLinked"))
            if saved_linked is None:
                saved_linked = (
                    self.video_start_wall_ms is not None
                    and self.align_method not in {"none", "default"}
                )
            self._timelines_linked = bool(saved_linked)
            if not self._timelines_linked:
                data_anchor = ui.get("dataAnchorMs")
                video_anchor = ui.get("videoAnchorMs")
                self._pending_data_anchor_ms = (
                    None if data_anchor is None else float(data_anchor)
                )
                self._pending_video_anchor_ms = (
                    None if video_anchor is None else float(video_anchor)
                )
                self._independent_video_selection_active = bool(
                    self._pending_video_anchor_ms is not None
                )
            self._sync_pin_button()
            self._update_alignment_status()
            self._refresh_enabled()
            if not self._timelines_linked:
                if self._pending_data_anchor_ms is not None:
                    self._set_playhead_visual(self._pending_data_anchor_ms)
                if (
                    self.media is not None
                    and self._pending_video_anchor_ms is not None
                ):
                    self.media.pause(True)
                    self.media.set_time_ms(self._pending_video_anchor_ms)
                    self.video_timeline.set_position(
                        self._pending_video_anchor_ms
                    )
            else:
                self.set_playhead(self.playhead_ms, seek_video=True)

    def _is_user_playing(self) -> bool:
        return bool(
            self._media_primed
            and self.media is not None
            and self.media.is_playing()
        )

    def _timelines_are_linked(self) -> bool:
        """Return whether paused timeline gestures must stay synchronized."""

        return bool(
            self._timelines_linked
            and not self._source_switch_active
            and self.data is not None
            and self.media is not None
            and self.video_path
            and self.video_start_wall_ms is not None
        )

    def set_playhead(self, value_ms: float, seek_video: bool = True) -> None:
        may_seek_video = bool(
            self._timelines_are_linked()
            or (
                self._source_switch_active
                and self._source_switch_kind == "video"
                and self._source_switch_mapping_committed
            )
        )
        super().set_playhead(
            value_ms, seek_video=bool(seek_video and may_seek_video)
        )

    def _seek_video_to_playhead(self) -> None:
        if self._source_switch_active and not (
            self._source_switch_kind == "video"
            and self._source_switch_mapping_committed
        ):
            return
        if self.video_path and not (
            self._timelines_are_linked()
            or (
                self._source_switch_active
                and self._source_switch_mapping_committed
            )
        ):
            return
        super()._seek_video_to_playhead()

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
        suffix = "；调整完成后点击“钉住”"
        if getattr(self, "_ui_pending_seek_video_ms", None) is not None:
            suffix = "；等待视频定位确认后点击“钉住”"
        self.align_status.setText(
            "待配对：九轴 "
            + format_relative(data_ms)
            + " ↔ 视频 "
            + format_relative(video_ms)
            + suffix
        )
        self.align_status.setStyleSheet("color:#a15c00;")
        self.statusBar().showMessage(
            "未钉住：九轴和视频可独立调整，确认后点击“钉住”",
            5000,
        )

    # ------------------------------------------------------------------
    # Independent paused selection
    # ------------------------------------------------------------------

    def _data_timeline_selected(self, data_ms: float) -> None:
        if self.data is None:
            return
        data_ms = self.plot.snap_time(data_ms)
        if self._timelines_are_linked():
            self._reset_pending_pair()
            self.set_playhead(data_ms, seek_video=True)
            return
        if self._is_user_playing() and self.media is not None:
            self.media.pause(True)
        self._pending_data_anchor_ms = float(data_ms)
        self._set_playhead_visual(float(data_ms))
        self._show_pending_pair()

    def _queue_video_timeline_seek(self, video_ms: float) -> None:
        if self.media is None or not self.video_path:
            return
        if self._timelines_are_linked():
            self._reset_pending_pair()
            super()._queue_video_timeline_seek(video_ms)
            return

        value = float(video_ms)
        self._pending_video_anchor_ms = value
        self._independent_video_selection_active = True
        # A newer drag position supersedes an older seek confirmation.
        self._clear_ui_seek()
        self.video_timeline.set_position(value)
        self.video_clock_label.setText(
            "视频 " + format_relative(value)
        )
        self.wall_clock_label.setText("画面时间：待与九轴起点配对")
        self.video_status.setText(
            f"{Path(self.video_path).name} · 已选择独立视频起点"
        )
        super()._queue_video_timeline_seek(value)
        self._show_pending_pair()

    def _video_timeline_selected(self, video_ms: float) -> None:
        """Compatibility alias used by earlier tests and integrations."""

        self._queue_video_timeline_seek(video_ms)

    def _seek_from_video_timeline(self, video_ms: float) -> None:
        if self.media is None or not self.video_path:
            return
        if self._timelines_are_linked():
            self._reset_pending_pair()
            super()._seek_from_video_timeline(video_ms)
            return

        value = float(video_ms)
        self._pending_video_anchor_ms = value
        self._independent_video_selection_active = True
        self._arm_ui_seek(value)
        self.video_timeline.set_position(value)
        self.video_clock_label.setText(
            "视频 " + format_relative(value)
        )
        self.wall_clock_label.setText("画面时间：待与九轴起点配对")
        self.video_status.setText(
            f"{Path(self.video_path).name} · 正在确认独立视频起点"
        )
        self.media.pause(True)
        accepted = bool(self.media.set_time_ms(value))
        if not accepted:
            actual = float(self.media.get_time_ms())
            self._clear_ui_seek()
            self._pending_video_anchor_ms = actual
            self.video_timeline.set_position(actual)
            self.statusBar().showMessage(
                "视频定位请求未被接受，将使用播放器实际位置配对",
                5000,
            )
        self._show_pending_pair()

    def _on_media_time(self, video_time_ms: int) -> None:
        if self._source_switch_active:
            self.video_timeline.set_position(video_time_ms)
            self.video_clock_label.setText(
                "视频 " + format_relative(video_time_ms)
            )
            self.wall_clock_label.setText("画面时间：数据源切换中")
            return
        if (
            self.video_path
            and self.data is not None
            and not self._timelines_are_linked()
        ):
            pending = self._ui_pending_seek_video_ms
            if pending is not None:
                confirmed = (
                    abs(float(video_time_ms) - pending)
                    <= self.SEEK_CONFIRM_TOLERANCE_MS
                )
                if not confirmed and time.monotonic() < self._ui_seek_deadline:
                    return
                if not confirmed:
                    self.statusBar().showMessage(
                        "视频定位超时，将按播放器实际位置配对",
                        5000,
                    )
                self._clear_ui_seek()
            self._pending_video_anchor_ms = float(video_time_ms)
            self.video_timeline.set_position(video_time_ms)
            self.video_clock_label.setText(
                "视频 " + format_relative(video_time_ms)
            )
            self.wall_clock_label.setText(
                "画面时间：未钉住，待与九轴位置配对"
            )
            self.video_status.setText(
                f"{Path(self.video_path).name} · 校准模式"
            )
            if not self._is_user_playing():
                self._show_pending_pair()
            return
        super()._on_media_time(video_time_ms)

    def _finish_media_priming(self) -> None:
        super()._finish_media_priming()
        if (
            self._source_switch_active
            and self._source_switch_kind == "video"
            and self._source_switch_mapping_committed
        ):
            QTimer.singleShot(0, self._complete_video_source_switch)

    # ------------------------------------------------------------------
    # Pair once, then reuse the existing shared-clock playback chain
    # ------------------------------------------------------------------

    def _flush_pending_video_selection(self) -> None:
        if not hasattr(self, "_video_seek_timer"):
            return
        self._video_seek_timer.stop()
        value = self._pending_video_seek_ms
        self._pending_video_seek_ms = None
        if value is not None:
            self._seek_from_video_timeline(float(value))

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
            round(self.data_create_time_ms + data_ms - video_ms)
        )
        self.align_method = "pin"
        self._clear_ui_seek()
        self._independent_video_selection_active = False
        self._pending_data_anchor_ms = None
        self._pending_video_anchor_ms = None
        self._set_playhead_visual(data_ms)
        self.video_timeline.set_position(video_ms)
        self._timelines_linked = True
        self._sync_pin_button()
        self._update_alignment_status()
        self.statusBar().showMessage(
            "已按所选九轴起点与视频起点自动配对",
            5000,
        )
        return True

    def _can_start_from_current_pair(self) -> bool:
        limits = self._shared_video_limits()
        if limits is None or limits[1] - limits[0] < 1.0:
            return False
        current_video = (
            float(self.media.get_time_ms())
            if self.media is not None
            else self.video_timeline._position_ms
        )
        if current_video >= limits[1] - 1.0:
            if self.media is not None:
                self.media.pause(True)
            self.play_btn.setText("▶ 播放")
            self.video_status.setText(
                "所选起点位于共同覆盖区间末端，已阻止跳回开头"
            )
            return False
        return True

    def _continue_play_after_anchor_confirmation(self) -> None:
        paired = self._pair_selected_anchors()
        if paired and not self._can_start_from_current_pair():
            return
        super().toggle_play()

    def toggle_play(self) -> None:
        if self._source_switch_active:
            self.statusBar().showMessage(
                "数据源仍在切换，请等待完成后再播放", 3000
            )
            return
        if (
            self.data is not None
            and self.media is not None
            and self.video_path
            and not self._timelines_are_linked()
        ):
            if self.media.is_playing():
                self.media.pause(True)
                return
            self._flush_pending_video_selection()
            if self._ui_pending_seek_video_ms is not None:
                self.statusBar().showMessage(
                    "视频定位仍在确认，请稍候再播放", 3000
                )
                return
            self._apply_rate()
            self.media.play()
            self.video_status.setText(
                f"{Path(self.video_path).name} · 校准模式，仅播放视频"
            )
            self.statusBar().showMessage(
                "未钉住：视频独立播放，正式标注已暂停", 4000
            )
            return
        if self._is_user_playing():
            super().toggle_play()
            return
        if self._play_requested_after_anchor_confirmation:
            self._play_requested_after_anchor_confirmation = False
            self.play_btn.setText("▶ 播放")
            self.statusBar().showMessage("已取消等待定位后的播放", 3000)
            return

        self._flush_pending_video_selection()
        if (
            self._independent_video_selection_active
            and self._ui_pending_seek_video_ms is not None
        ):
            self._play_requested_after_anchor_confirmation = True
            self.play_btn.setText("⏳ 定位后播放")
            self.video_status.setText(
                f"{Path(self.video_path).name} · 确认视频起点后自动播放"
            )
            return

        super().toggle_play()

    # ------------------------------------------------------------------
    # Persistence and explicit alignment actions
    # ------------------------------------------------------------------

    def _project_model(self):
        if (
            getattr(self, "_pending_video_seek_ms", None) is not None
            or getattr(self, "_ui_pending_seek_video_ms", None) is not None
        ):
            raise RuntimeError(
                "视频位置仍在定位确认中，请稍候再保存或导出"
            )
        project = super()._project_model()
        project.align["locked"] = bool(self._timelines_are_linked())
        ui = project.extras.setdefault("ui", {})
        if not isinstance(ui, dict):
            ui = {}
            project.extras["ui"] = ui
        ui["timelinesLinked"] = bool(self._timelines_are_linked())
        ui["dataAnchorMs"] = (
            self._pending_data_anchor_ms
            if self._pending_data_anchor_ms is not None
            else self.playhead_ms
        )
        ui["videoAnchorMs"] = (
            self._pending_video_anchor_ms
            if self._pending_video_anchor_ms is not None
            else (
                self.media.get_time_ms()
                if self.media is not None and self.video_path
                else None
            )
        )
        return project

    def pin_alignment(self, *_args) -> None:
        button = getattr(self, "pin_btn", None)
        requested = (
            bool(button.isChecked())
            if button is not None
            else not bool(self._timelines_linked)
        )
        if not requested:
            if getattr(self, "pending_intervals", {}):
                self._sync_pin_button()
                self.statusBar().showMessage(
                    "请先结束或取消正在标注的区间，再解除钉住", 5000
                )
                return
            if self.media is not None and self.media.is_playing():
                self.media.pause(True)
            self._pending_data_anchor_ms = float(self.playhead_ms)
            self._pending_video_anchor_ms = (
                float(self.media.get_time_ms())
                if self.media is not None and self.video_path
                else None
            )
            self._independent_video_selection_active = bool(
                self._pending_video_anchor_ms is not None
            )
            self._set_timelines_linked(False)
            self._show_pending_pair()
            autosave = getattr(self, "_autosave", None)
            if callable(autosave):
                autosave()
            return
        if self.media is None or self.data is None or not self.video_path:
            self._timelines_linked = False
            self._sync_pin_button()
            self.statusBar().showMessage("请先打开九轴 JSON 和视频", 3000)
            return
        self._flush_pending_video_selection()
        if getattr(self, "_ui_pending_seek_video_ms", None) is not None:
            self._timelines_linked = False
            self._sync_pin_button()
            self.statusBar().showMessage(
                "视频定位仍在确认，请等待画面到位后再点击“钉住”",
                5000,
            )
            return
        data_ms = float(
            self._pending_data_anchor_ms
            if self._pending_data_anchor_ms is not None
            else self.playhead_ms
        )
        video_ms = self._current_video_position()
        self.video_start_wall_ms = int(
            round(self.data_create_time_ms + data_ms - video_ms)
        )
        self.align_method = "pin"
        self._set_playhead_visual(data_ms)
        self.video_timeline.set_position(video_ms)
        self._set_timelines_linked(True)
        self.statusBar().showMessage(
            "已钉住：九轴 "
            + format_relative(data_ms)
            + " ↔ 视频 "
            + format_relative(video_ms),
            5000,
        )
        autosave = getattr(self, "_autosave", None)
        if callable(autosave):
            autosave()

    def align_from_filename(self, silent: bool = False) -> None:
        super().align_from_filename(silent)
        if (
            self.align_method == "filename"
            and not self._restoring_project
            and not self._source_switch_active
        ):
            self._reset_pending_pair()
            self._set_timelines_linked(True)
            self._seek_video_to_playhead()

    def align_from_corner(self) -> None:
        super().align_from_corner()
        if self.align_method == "corner":
            self._reset_pending_pair()
            self._set_timelines_linked(True)
            if self.media is not None:
                self._on_media_time(int(self.media.get_time_ms()))

    def nudge_alignment(self, delta_ms: float) -> None:
        if (
            self.media is not None
            and self.video_path
            and not self._timelines_are_linked()
        ):
            if self.media.is_playing():
                self.media.pause(True)
            duration = float(self.media.duration_ms())
            target = float(self.media.get_time_ms()) + float(delta_ms)
            target = max(0.0, min(target, duration if duration > 0 else target))
            self._queue_video_timeline_seek(target)
            self.statusBar().showMessage(
                "未钉住：仅微调视频位置，确认后点击“钉住”", 4000
            )
            return
        super().nudge_alignment(delta_ms)
        if self.align_method == "manual":
            self._reset_pending_pair()
            self._set_timelines_linked(True)

    def _annotation_blocked_by_alignment(self) -> bool:
        return bool(
            self.data is not None
            and self.video_path
            and not self._timelines_are_linked()
        )

    def _label_shortcut(self, label_index: int) -> None:
        if self._annotation_blocked_by_alignment():
            self.statusBar().showMessage(
                "当前未钉住，不能开始标注；完成校准后请重新钉住",
                5000,
            )
            return
        super()._label_shortcut(label_index)

    def _add_event(
        self, label_index: int, start_ms: float, end_ms: float | None
    ) -> None:
        if self._annotation_blocked_by_alignment():
            self.statusBar().showMessage(
                "当前未钉住，正式标注已暂停；完成校准后请重新钉住",
                5000,
            )
            return
        super()._add_event(label_index, start_ms, end_ms)

    def _refresh_enabled(self) -> None:
        super()._refresh_enabled()
        if not hasattr(self, "pin_btn"):
            return
        has_pair = bool(
            self.data is not None and self.media is not None and self.video_path
        )
        self.pin_btn.setEnabled(has_pair and not self._source_switch_active)
        for button, _delta in getattr(self, "nudge_buttons", []):
            button.setEnabled(has_pair and not self._source_switch_active)
        calibration = self._annotation_blocked_by_alignment()
        self.label_list.setEnabled(not calibration)

    def _update_alignment_status(self) -> None:
        super()._update_alignment_status()
        self._sync_pin_button()
        if not self.video_path or self.data is None:
            return
        if self._source_switch_active:
            label = "视频" if self._source_switch_kind == "video" else "JSON"
            self.align_status.setText(f"正在切换{label} · 时间轴暂时冻结")
            self.align_status.setStyleSheet("color:#8a5200;font-weight:600;")
            return
        if not self._timelines_are_linked():
            previous = self.align_status.text()
            self.align_status.setText("未钉住（校准模式） · " + previous)
            self.align_status.setStyleSheet("color:#a15c00;font-weight:600;")

    def _release_validation_issues(
        self, project: annotation_core.Project
    ) -> list[annotation_core.ValidationIssue]:
        issues = list(super()._release_validation_issues(project))
        if self.video_path and not bool(project.align.get("locked", False)):
            issues.append(
                annotation_core.ValidationIssue(
                    "error",
                    "timelines_unlocked",
                    "视频与九轴当前未钉住；请完成校准并重新钉住后再导出",
                )
            )
        return issues
