from __future__ import annotations

import copy
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QStandardPaths, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
)

import annotation_core
import data_core
from ffmpeg_tools import FFmpegToolError, probe_media
from ui_helpers import parse_video_filename


class ProductionSafetyMixin:
    """Data-safety, history and synchronization fixes for the release UI."""

    # v2 does not prescribe a universal minimum duration. Intervals are
    # bounded by actual video/JSON time only.
    MIN_INTERVAL_MS = 0.0
    MAX_VISIBLE_LABELS = 128

    def __init__(self) -> None:
        self._undo_stack: annotation_core.UndoStack[dict[str, Any]] = (
            annotation_core.UndoStack(limit=100)
        )
        self._history_suspended = True
        self._drag_history_snapshot: dict[str, Any] | None = None
        self._pending_video_seek_ms: float | None = None
        super().__init__()

        self._video_seek_timer = QTimer(self)
        self._video_seek_timer.setSingleShot(True)
        self._video_seek_timer.setInterval(90)
        self._video_seek_timer.timeout.connect(
            self._flush_video_timeline_seek
        )
        try:
            self.video_timeline.seekRequested.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.video_timeline.seekRequested.connect(
            self._queue_video_timeline_seek
        )
        self._history_suspended = False
        self._rebuild_shortcuts()

    # ------------------------------------------------------------------
    # Annotation history
    # ------------------------------------------------------------------

    def _rebuild_shortcuts(self) -> None:
        super()._rebuild_shortcuts()
        for sequence, handler in (
            ("Ctrl+Z", self.undo_annotation),
            ("Ctrl+Y", self.redo_annotation),
            ("Ctrl+Shift+Z", self.redo_annotation),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)
            self._shortcut_objects.append(shortcut)

    def _annotation_snapshot(self) -> dict[str, Any]:
        return {
            "labels": copy.deepcopy(self.labels),
            "events": copy.deepcopy(self.events),
            "next_event_id": int(self.next_event_id),
            "selected_label": int(self.selected_label),
            "selected_event_id": self.selected_event_id,
            "pending_intervals": copy.deepcopy(self.pending_intervals),
        }

    def _checkpoint_if_changed(self, before: dict[str, Any]) -> None:
        if (
            not self._history_suspended
            and before != self._annotation_snapshot()
        ):
            self._undo_stack.push(before)

    def _restore_annotation_snapshot(
        self, snapshot: dict[str, Any]
    ) -> None:
        previous = self._history_suspended
        self._history_suspended = True
        try:
            self.labels = copy.deepcopy(snapshot["labels"])
            self.events = copy.deepcopy(snapshot["events"])
            self.next_event_id = int(snapshot["next_event_id"])
            self.selected_label = int(snapshot["selected_label"])
            self.selected_event_id = snapshot["selected_event_id"]
            self.pending_intervals = copy.deepcopy(
                snapshot["pending_intervals"]
            )
            self._refresh_labels()
            self._refresh_events()
            self._rebuild_shortcuts()
            self._autosave()
        finally:
            self._history_suspended = previous

    def undo_annotation(self) -> None:
        snapshot = self._undo_stack.undo(self._annotation_snapshot())
        if snapshot is None:
            self.statusBar().showMessage("没有可撤销的标注操作", 2000)
            return
        self._restore_annotation_snapshot(snapshot)
        self.statusBar().showMessage("已撤销标注操作", 2000)

    def redo_annotation(self) -> None:
        snapshot = self._undo_stack.redo(self._annotation_snapshot())
        if snapshot is None:
            self.statusBar().showMessage("没有可重做的标注操作", 2000)
            return
        self._restore_annotation_snapshot(snapshot)
        self.statusBar().showMessage("已重做标注操作", 2000)

    def add_label(self) -> None:
        if len(self.labels) >= self.MAX_VISIBLE_LABELS:
            self._show_error(
                "标签数量已达到当前上限，请先编辑或删除现有标签。"
            )
            return
        before = self._annotation_snapshot()
        super().add_label()
        self._checkpoint_if_changed(before)

    def edit_label(self) -> None:
        before = self._annotation_snapshot()
        super().edit_label()
        self._checkpoint_if_changed(before)

    def delete_label(self) -> None:
        before = self._annotation_snapshot()
        super().delete_label()
        self._checkpoint_if_changed(before)
        if before != self._annotation_snapshot():
            self._autosave()

    def _normalise_interval(
        self, start_ms: float, end_ms: float
    ) -> tuple[float, float]:
        duration = max(0.0, float(self.data_duration_ms))
        start = float(np.clip(start_ms, 0.0, duration))
        end = float(np.clip(end_ms, 0.0, duration))
        if end < start:
            start, end = end, start
        return start, end

    def _add_event(
        self, label_index: int, start_ms: float, end_ms: float | None
    ) -> None:
        before = self._annotation_snapshot()
        if end_ms is not None:
            start_ms, end_ms = self._normalise_interval(
                start_ms, end_ms
            )
        super()._add_event(label_index, start_ms, end_ms)
        self._checkpoint_if_changed(before)

    def _event_double_clicked(self, row: int, column: int) -> None:
        before = self._annotation_snapshot()
        super()._event_double_clicked(row, column)
        self._checkpoint_if_changed(before)

    def delete_selected_event(self) -> None:
        before = self._annotation_snapshot()
        super().delete_selected_event()
        self._checkpoint_if_changed(before)

    def clear_events(self) -> None:
        before = self._annotation_snapshot()
        super().clear_events()
        self._checkpoint_if_changed(before)

    def _select_event_by_id(self, event_id: int) -> None:
        if getattr(self.plot, "_event_drag", None) is not None:
            self._drag_history_snapshot = self._annotation_snapshot()
        super()._select_event_by_id(event_id)

    def _event_changed_on_plot(
        self, event_id: int, start_ms: float, end_ms: Any
    ) -> None:
        event = next(
            (
                item
                for item in self.events
                if int(item.get("id", -1)) == int(event_id)
            ),
            None,
        )
        if event is not None and event.get("t1") is not None:
            event["t0"], event["t1"] = self._normalise_interval(
                float(event.get("t0", start_ms)),
                float(event["t1"]),
            )
        before = self._drag_history_snapshot
        self._drag_history_snapshot = None
        super()._event_changed_on_plot(event_id, start_ms, end_ms)
        updated = next(
            (
                item for item in self.events
                if int(item.get("id", -1)) == int(event_id)
            ),
            None,
        )
        if updated is not None:
            updated["reviewed_range"] = {
                "start": float(updated.get("t0", start_ms)),
                "end": updated.get("t1"),
            }
            times = np.asarray(getattr(self.data, "times_ms", []), dtype=float)
            if times.size:
                updated["json_sample_start"] = int(
                    np.searchsorted(times, float(updated["t0"]), side="left")
                )
                updated["json_sample_end"] = (
                    None if updated.get("t1") is None else int(
                        np.searchsorted(times, float(updated["t1"]), side="left")
                    )
                )
            self._autosave()
        if before is not None:
            self._checkpoint_if_changed(before)

    # ------------------------------------------------------------------
    # Real data reload and order-independent alignment
    # ------------------------------------------------------------------

    @staticmethod
    def _load_motion(path: str, scale: int):
        loader = getattr(data_core, "load_motion_json", None) or getattr(
            data_core, "load_json", None
        )
        if loader is None:
            raise RuntimeError("data_core.py 缺少九轴 JSON 读取函数")
        try:
            return loader(path, acc_scale=scale)
        except TypeError:
            return loader(path, scale)

    def open_json(self, path: str | None = None) -> None:
        previous_path = self.data_path
        super().open_json(path)
        if not self.data_path or self.data_path == previous_path:
            return
        if not self._restoring_project:
            self._undo_stack.clear()
        if self.video_path:
            self.video_start_wall_ms = None
            self.align_method = "none"
            self.align_from_filename(silent=True)
            if self.video_start_wall_ms is None:
                # This mapping only enables shared playback; red UI text and
                # export validation keep it explicitly "not calibrated".
                self.video_start_wall_ms = self.data_create_time_ms
                self.align_method = "default"
            self._update_alignment_status()
            self._seek_video_to_playhead()

    def _reload_scale(self, _text: str) -> None:
        if not self.data_path or self._restoring_project:
            return
        old_playhead = self.playhead_ms
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            parsed = self._load_motion(
                self.data_path, int(self.acc_scale_combo.currentText())
            )
            self.data = parsed
            self._display_motion_data()
            self.set_playhead(old_playhead)
            self._refresh_events()
            self._autosave()
        except Exception as exc:
            self._show_error(f"量程重载失败：\n{exc}")
        finally:
            QApplication.restoreOverrideCursor()

    # ------------------------------------------------------------------
    # Shared playback boundaries and seek throttling
    # ------------------------------------------------------------------

    def _shared_video_limits(self) -> tuple[float, float] | None:
        if (
            self.media is None
            or self.data is None
            or self.video_start_wall_ms is None
        ):
            return None
        media_duration = float(self.media.duration_ms())
        if media_duration <= 0:
            return None
        start = max(
            0.0,
            float(self.data_create_time_ms - self.video_start_wall_ms),
        )
        end = min(
            media_duration,
            float(
                self.data_create_time_ms
                + self.data_duration_ms
                - self.video_start_wall_ms
            ),
        )
        return (start, end) if end >= start else None

    def _seek_video_to_playhead(self) -> None:
        if (
            self.media is None
            or not self.video_path
            or self.data is None
            or self.video_start_wall_ms is None
        ):
            return
        target = (
            self.data_create_time_ms
            + self.playhead_ms
            - self.video_start_wall_ms
        )
        limits = self._shared_video_limits()
        if (
            limits is not None
            and limits[0] <= target <= limits[1]
        ):
            self.media.set_time_ms(target)
            return
        self.media.pause(True)
        self.video_status.setText(
            f"{Path(self.video_path).name} · 当前九轴时刻无对应视频"
        )

    def _queue_video_timeline_seek(self, video_ms: float) -> None:
        self._pending_video_seek_ms = float(video_ms)
        self._video_seek_timer.start()

    def _flush_video_timeline_seek(self) -> None:
        value = self._pending_video_seek_ms
        self._pending_video_seek_ms = None
        if value is not None:
            self._seek_from_video_timeline(value)

    def _seek_from_video_timeline(self, video_ms: float) -> None:
        limits = self._shared_video_limits()
        if limits is not None:
            video_ms = float(np.clip(video_ms, limits[0], limits[1]))
        elif self.data is not None and self.video_start_wall_ms is not None:
            if self.media is not None:
                self.media.pause(True)
            self.video_status.setText("数据与视频没有重叠时间")
            return
        super()._seek_from_video_timeline(video_ms)

    def _on_media_time(self, video_time_ms: int) -> None:
        limits = self._shared_video_limits()
        if limits is not None and not (
            limits[0] - 30.0 <= video_time_ms <= limits[1] + 30.0
        ):
            clamped = int(round(np.clip(video_time_ms, *limits)))
            if self.media is not None:
                self.media.pause(True)
                self.media.set_time_ms(clamped)
            video_time_ms = clamped
            self.statusBar().showMessage(
                "已到达视频与九轴数据的共同覆盖边界", 3000
            )
        super()._on_media_time(video_time_ms)

    # ------------------------------------------------------------------
    # Atomic project I/O and source-identity checks
    # ------------------------------------------------------------------

    def save_project_dialog(self, path: str | None = None) -> None:
        if path is None or isinstance(path, bool):
            suggestion = (
                str(Path(self.data_path).with_suffix(".annotation.json"))
                if self.data_path
                else "annotation_project.json"
            )
            path, _ = QFileDialog.getSaveFileName(
                self, "保存标注工程", suggestion, "工程 JSON (*.json)"
            )
        if not path:
            return
        try:
            annotation_core.save_project(self._project_model(), path)
            self._project_path = os.path.abspath(path)
            self.statusBar().showMessage(f"工程已保存：{path}", 5000)
        except Exception as exc:
            self._show_error(f"工程保存失败：\n{exc}")

    def _autosave(self) -> None:
        if self.data is None:
            return
        try:
            folder = Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppDataLocation
                )
            )
            folder.mkdir(parents=True, exist_ok=True)
            key = f"{self.data_device}_{self.data_create_time_ms}"
            annotation_core.save_project(
                self._project_model(), folder / f"autosave_{key}.json"
            )
        except Exception:
            pass

    @staticmethod
    def _resolve_saved_path(value: Any, project_path: Path) -> Path | None:
        text = str(value or "").strip()
        if not text:
            return None
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = project_path.parent / candidate
        return candidate.resolve()

    def _choose_missing_data(
        self, candidate: Path | None, project_path: Path
    ) -> Path | None:
        if candidate is not None and candidate.is_file():
            return candidate
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "工程中的九轴 JSON 路径失效，请重新关联",
            str(project_path.parent),
            "JSON 文件 (*.json);;所有文件 (*.*)",
        )
        return Path(selected).resolve() if selected else None

    def _choose_missing_video(
        self, candidate: Path | None, project_path: Path
    ) -> Path | None:
        if candidate is not None and candidate.is_file():
            return candidate
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "工程中的视频路径失效，请重新关联",
            str(project_path.parent),
            "视频文件 (*.mp4 *.mkv *.avi *.mov *.ts *.m2ts *.ps *.mpeg "
            "*.h265 *.hevc);;所有文件 (*.*)",
        )
        return Path(selected).resolve() if selected else None

    def _clear_loaded_video(self) -> None:
        if self.media is not None:
            try:
                self.media.pause(True)
                if self.media._player:
                    self.media._lib.libvlc_media_player_set_media(
                        self.media._player, None
                    )
                self.media._release_media()
                self.media._path = ""
                self.media._reset_poll_cache()
            except Exception:
                pass
        self.video_path = ""
        self.video_start_wall_ms = None
        self.align_method = "none"
        self.video_timeline.set_duration(0)
        self.video_timeline.set_position(0)
        self.video_status.setText("工程未关联视频")

    def load_project_dialog(self, path: str | None = None) -> None:
        if path is None or isinstance(path, bool):
            selected, _ = QFileDialog.getOpenFileName(
                self,
                "载入标注工程",
                str(Path.cwd()),
                "工程 JSON (*.json)",
            )
            path = selected
        if not path:
            return
        project_path = Path(path).resolve()
        try:
            project = annotation_core.load_project(project_path)
            raw = project.to_dict()
        except Exception as exc:
            self._show_error(f"工程载入失败：\n{exc}")
            return

        data_path = self._choose_missing_data(
            self._resolve_saved_path(
                project.source.get("path"), project_path
            ),
            project_path,
        )
        if data_path is None:
            self.statusBar().showMessage("已取消载入；当前工程未改变", 3000)
            return
        scale = int(project.source.get("accScale", 4096) or 4096)
        try:
            parsed = self._load_motion(str(data_path), scale)
        except Exception as exc:
            self._show_error(
                "重新关联的九轴 JSON 无法解析，当前工程未改变：\n"
                + str(exc)
            )
            return

        expected_device = str(project.source.get("device", "")).strip()
        actual_device = str(getattr(parsed, "device", "")).strip()
        if expected_device and actual_device != expected_device:
            self._show_error(
                "工程来源校验失败，当前工程未改变。\n"
                f"期望设备：{expected_device}\n实际设备：{actual_device}"
            )
            return
        expected_create = project.source.get("createTime")
        if expected_create not in (None, ""):
            try:
                expected_create_ms = int(float(expected_create))
            except (TypeError, ValueError):
                expected_create_ms = 0
            actual_create_ms = int(getattr(parsed, "create_time_ms", 0))
            if expected_create_ms and actual_create_ms != expected_create_ms:
                self._show_error(
                    "工程来源时间校验失败，当前工程未改变。\n"
                    f"期望：{expected_create_ms}\n实际：{actual_create_ms}"
                )
                return

        video_value = raw.get("videoPath")
        video_candidate = self._resolve_saved_path(
            video_value, project_path
        )
        expects_video = bool(video_value or project.videoName)
        video_path: Path | None = None
        if expects_video:
            if (
                video_candidate is None
                and project.videoName
                and (project_path.parent / project.videoName).is_file()
            ):
                video_candidate = (
                    project_path.parent / project.videoName
                ).resolve()
            video_path = self._choose_missing_video(
                video_candidate, project_path
            )
            if video_path is None:
                self.statusBar().showMessage(
                    "已取消载入；当前工程未改变", 3000
                )
                return
            try:
                media_info = probe_media(video_path)
                if not any(
                    stream.get("codec_type") == "video"
                    for stream in media_info.get("streams", [])
                ):
                    raise FFmpegToolError("文件中未检测到视频流")
            except FFmpegToolError as exc:
                self._show_error(
                    "重新关联的视频无效，当前工程未改变：\n"
                    + str(exc)
                )
                return

        previous_restore = self._restoring_project
        previous_history = self._history_suspended
        self._restoring_project = True
        self._history_suspended = True
        try:
            self.labels = [label.to_dict() for label in project.labels]
            self.events = [event.to_dict() for event in project.events]
            self.next_event_id = max(
                (int(event["id"]) for event in self.events), default=0
            ) + 1
            self.selected_label = 0
            self.selected_event_id = None
            self.pending_intervals.clear()
            self.annotator_edit.setText(project.annotator)
            self.protocol_edit.setText(project.protocol)
            self.cow_id_edit.setText(project.cow_id)

            combo_index = self.acc_scale_combo.findText(str(scale))
            self.acc_scale_combo.blockSignals(True)
            if combo_index >= 0:
                self.acc_scale_combo.setCurrentIndex(combo_index)
            self.acc_scale_combo.blockSignals(False)

            self.data = parsed
            self.data_path = str(data_path)
            self.settings.setValue(
                "last_data_dir", str(data_path.parent)
            )
            self._display_motion_data()

            if video_path is not None:
                self.open_video(str(video_path))
                if os.path.abspath(self.video_path) != os.path.abspath(
                    str(video_path)
                ):
                    raise RuntimeError("视频播放器未能载入工程视频")
            else:
                self._clear_loaded_video()

            saved_start = project.align.get("videoStartWall")
            self.video_start_wall_ms = (
                None if saved_start is None else int(saved_start)
            )
            self.align_method = str(
                project.align.get("method", "none")
            )
            if video_path is not None and self.video_start_wall_ms is None:
                parsed_name_time = parse_video_filename(
                    str(video_path), self.data_create_time_ms
                )
                if parsed_name_time is not None:
                    self.video_start_wall_ms = parsed_name_time
                    self.align_method = "filename"
                else:
                    self.video_start_wall_ms = self.data_create_time_ms
                    self.align_method = "default"

            self._project_path = str(project_path)
            self._refresh_labels()
            self._refresh_events()
            ui = raw.get("ui", {})
            if not isinstance(ui, dict):
                ui = {}
            view = ui.get("view")
            if isinstance(view, list) and len(view) == 2:
                self.plot.set_view(float(view[0]), float(view[1]))
            rate = float(ui.get("rate", 1.0) or 1.0)
            rate_index = self.rate_combo.findData(rate)
            if rate_index >= 0:
                self.rate_combo.setCurrentIndex(rate_index)
            self.set_playhead(float(ui.get("playheadMs", 0.0)))
            self._update_alignment_status()
            self._refresh_enabled()
        except Exception as exc:
            self._show_error(
                "工程提交失败。来源文件已通过预检，但界面载入未完成：\n"
                + str(exc)
            )
            return
        finally:
            self._restoring_project = previous_restore
            self._history_suspended = previous_history

        self._undo_stack.clear()
        self._autosave()
        self.statusBar().showMessage(f"工程已载入：{project_path}", 5000)

    # ------------------------------------------------------------------
    # Validation-aware exports
    # ------------------------------------------------------------------

    def _release_validation_issues(
        self, project: annotation_core.Project
    ) -> list[annotation_core.ValidationIssue]:
        issues = list(
            annotation_core.validate_project(
                project,
                duration_ms=self.data_duration_ms or None,
                require_alignment=True,
            )
        )
        if self.video_path and self.align_method == "default":
            issues.append(
                annotation_core.ValidationIssue(
                    "warning",
                    "uncalibrated_default_alignment",
                    "海康视频当前仅按零偏移临时同步，尚未用画面角标或人工钉住校准",
                )
            )
        return issues

    def _allow_export(
        self, project: annotation_core.Project
    ) -> bool:
        issues = self._release_validation_issues(project)
        errors = [issue for issue in issues if issue.severity == "error"]
        warnings = [
            issue for issue in issues if issue.severity != "error"
        ]
        if errors:
            self._show_error(
                "存在结构性错误，已阻止导出：\n"
                + "\n".join(f"• {issue.message}" for issue in errors)
            )
            return False
        if not warnings:
            return True
        summary = "\n".join(f"• {issue.message}" for issue in warnings)
        return (
            QMessageBox.warning(
                self,
                "导出检查",
                summary + "\n\n仍然导出？",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def export_event_csv(self) -> None:
        if self.data is None or not self.events:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出事件 CSV",
            str(Path(self.data_path).with_suffix(".events.csv")),
            "CSV (*.csv)",
        )
        if not path:
            return
        project = self._project_model()
        if not self._allow_export(project):
            return
        try:
            annotation_core.export_events_csv(
                project,
                path,
                relative_timestamps_ms=getattr(
                    self.data, "times_ms", None
                ),
            )
            meta_path = str(Path(path).with_suffix("")) + "_meta.json"
            annotation_core.export_meta_json(
                project,
                meta_path,
                data_meta={
                    "sampling_hz": getattr(
                        self.data, "sample_rate_hz", ""
                    ),
                    "gap_count": getattr(self.data, "gap_count", ""),
                    "resting_g": (
                        self.static_g
                        if math.isfinite(self.static_g)
                        else ""
                    ),
                },
            )
            self.statusBar().showMessage(f"已导出：{path}", 5000)
        except Exception as exc:
            self._show_error(f"CSV 导出失败：\n{exc}")

    def export_boris_csv(self) -> None:
        if not self.events:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出聚合/BORIS CSV",
            str(Path(self.data_path).with_suffix(".boris.csv"))
            if self.data_path
            else "events_boris.csv",
            "CSV (*.csv)",
        )
        if not path:
            return
        project = self._project_model()
        if not self._allow_export(project):
            return
        try:
            annotation_core.export_boris_csv(project, path)
            self.statusBar().showMessage(f"已导出：{path}", 5000)
        except Exception as exc:
            self._show_error(f"聚合 CSV 导出失败：\n{exc}")
