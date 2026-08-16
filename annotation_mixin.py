from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QListWidgetItem,
    QMessageBox,
    QTableWidgetItem,
)

import annotation_core
from defaults import DEFAULT_LABELS
from ui_helpers import LabelDialog, as_dict, format_relative, format_wall


LAYER_NAMES = {
    "body_state": "身体行为",
    "tail_action": "尾部动作",
    "posture_transition": "姿态转换",
    "objective_event": "客观事件",
    "calving_process": "分娩过程",
    "sync_anchor": "同步锚点",
}
MAIN_OPERATION_CODES = {
    "STANDING", "LYING", "WALKING",
    "STRAINING_ONSET", "STRAINING_BOUT",
    "AMNIOTIC_SAC_FIRST_VISIBLE", "FETAL_PART_FIRST_VISIBLE",
    "CALF_FULLY_EXPELLED", "FETAL_MEMBRANES_FULLY_EXPELLED",
    "TAIL_RAISED", "TAIL_WAGGING", "STANDING_UP", "LYING_DOWN",
    "URINATION", "DEFECATION",
}


class AnnotationMixin:
    """Labels, events, project persistence, exports, IRR and shortcuts."""

    def _rebuild_shortcuts(self) -> None:
        for shortcut in self._shortcut_objects:
            shortcut.setParent(None)
            shortcut.deleteLater()
        self._shortcut_objects.clear()

        fixed = [
            ("Space", self.toggle_play),
            ("Ctrl+S", self.save_project_dialog),
            ("Delete", self.delete_selected_event),
            ("Backspace", self.delete_selected_event),
            ("[", self.previous_frame),
            ("]", self.next_frame),
            ("F", self.plot.show_all),
            ("N", lambda: self._jump_activity(1)),
            ("P", lambda: self._jump_activity(-1)),
            ("Left", lambda: self.set_playhead(self.playhead_ms - 100)),
            ("Right", lambda: self.set_playhead(self.playhead_ms + 100)),
            ("Esc", self._cancel_pending),
        ]
        for sequence, handler in fixed:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)
            self._shortcut_objects.append(shortcut)
        for index, label in enumerate(self.labels):
            key = str(label.get("key", "")).strip()
            if not key:
                continue
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(
                lambda label_index=index: self._label_shortcut(label_index)
            )
            self._shortcut_objects.append(shortcut)

    def _visible_label_indices(self) -> list[int]:
        return [
            index for index, label in enumerate(self.labels)
            if str(label.get("layer", "")) != "sync_anchor"
        ]

    def _set_visible_label(self, label_index: int) -> None:
        for row in range(self.label_list.count()):
            item = self.label_list.item(row)
            value = item.data(256)
            if value is not None and int(value) == int(label_index):
                self.label_list.setCurrentRow(row)
                return

    def _select_visible_label(self, row: int) -> None:
        if row < 0:
            return
        item = self.label_list.item(row)
        if item is None or item.data(256) is None:
            return
        self._select_label(int(item.data(256)))

    def _refresh_labels(self) -> None:
        visible = self._visible_label_indices()
        current = self.selected_label if self.selected_label in visible else (
            visible[0] if visible else -1
        )
        self.label_list.blockSignals(True)
        self.label_list.clear()
        for index in visible:
            label = self.labels[index]
            item = QListWidgetItem(
                f"{label.get('key', '')}  {label.get('name', '')}"
            )
            item.setData(256, index)
            item.setForeground(QColor(str(label.get("color", "#4f8cff"))))
            item.setToolTip(
                f"{LAYER_NAMES.get(str(label.get('layer', '')), label.get('layer', ''))}\n"
                f"{label.get('def', '')}"
            )
            self.label_list.addItem(item)
        self.label_list.blockSignals(False)
        if visible:
            self.selected_label = current
            self._set_visible_label(current)
            self._select_label(current)
        else:
            self.label_definition.setText("")
        self.plot.set_events(self.labels, self.events)

    def _select_label(self, index: int) -> None:
        if not (0 <= index < len(self.labels)):
            return
        self.selected_label = index
        label = self.labels[index]
        self.label_definition.setText(
            f"[{LAYER_NAMES.get(str(label.get('layer', '')), label.get('layer', ''))}] "
            f"{label.get('name', '')} / {label.get('en', '')}："
            f"{label.get('def', '')}"
        )

    def add_label(self) -> None:
        dialog = LabelDialog(parent=self)
        if dialog.exec():
            self.labels.append(dialog.result_value())
            self.selected_label = len(self.labels) - 1
            self._refresh_labels()
            self._rebuild_shortcuts()
            self._autosave()

    def edit_label(self) -> None:
        if not (0 <= self.selected_label < len(self.labels)):
            return
        dialog = LabelDialog(self.labels[self.selected_label], self)
        if dialog.exec():
            self.labels[self.selected_label] = dialog.result_value()
            self._refresh_labels()
            self._refresh_events()
            self._rebuild_shortcuts()
            self._autosave()

    def delete_label(self) -> None:
        if not (0 <= self.selected_label < len(self.labels)):
            return
        if any(event.get("li") == self.selected_label for event in self.events):
            self._show_error("该标签仍有标注事件，请先删除或改用其他标签。")
            return
        index = self.selected_label
        self.labels.pop(index)
        for event in self.events:
            if int(event.get("li", -1)) > index:
                event["li"] = int(event["li"]) - 1
        self.selected_label = min(index, len(self.labels) - 1)
        self._refresh_labels()
        self._refresh_events()
        self._rebuild_shortcuts()

    def _range_selected(self, start_ms: float, end_ms: float) -> None:
        if self.data is None or not self.labels:
            return
        label = self.labels[self.selected_label]
        if label.get("type") == "point":
            start_ms = (start_ms + end_ms) / 2
            end: float | None = None
        else:
            end = end_ms
        self._add_event(self.selected_label, start_ms, end)

    def _add_event(
        self, label_index: int, start_ms: float, end_ms: float | None
    ) -> None:
        preserve_playhead = float(self.playhead_ms)
        label = self.labels[label_index] if 0 <= label_index < len(self.labels) else {}
        code = str(label.get("code", label.get("name", "")))
        event = {
            "id": self.next_event_id,
            "li": int(label_index),
            "label_code": code,
            "layer": str(label.get("layer", "objective_event")),
            "t0": float(start_ms),
            "t1": None if end_ms is None else float(end_ms),
            "note": "",
            "ev": str(self.evidence_combo.currentData() or "both"),
            "ctx": str(self.context_combo.currentData() or ""),
            "reviewed_range": {
                "start": float(start_ms),
                "end": None if end_ms is None else float(end_ms),
            },
        }
        if self.data is not None:
            times = np.asarray(getattr(self.data, "times_ms", []), dtype=float)
            if times.size:
                event["json_sample_start"] = int(np.searchsorted(times, start_ms, side="left"))
                event["json_sample_end"] = (
                    None if end_ms is None
                    else int(np.searchsorted(times, end_ms, side="left"))
                )
        self.next_event_id += 1
        self.events.append(event)
        self.events.sort(key=lambda item: (float(item["t0"]), int(item["id"])))
        self.selected_event_id = int(event["id"])
        self._refresh_events()
        self._reflect_event_meta(event)
        self.set_playhead(preserve_playhead, seek_video=False)
        self._autosave()

    def _label_shortcut(self, label_index: int) -> None:
        if self.data is None or not (0 <= label_index < len(self.labels)):
            return
        self.selected_label = label_index
        self._set_visible_label(label_index)
        label = self.labels[label_index]
        code = str(label.get("code", label.get("name", "")))
        if label.get("type") == "point":
            self._add_event(label_index, self.playhead_ms, None)
            return

        if str(label.get("layer", "")) == "body_state":
            active_body = [
                (index, start)
                for index, start in self.pending_intervals.items()
                if str(self.labels[index].get("layer", "")) == "body_state"
            ]
            if label_index in self.pending_intervals:
                start = self.pending_intervals.pop(label_index)
                if self.playhead_ms > start:
                    self._add_event(label_index, start, self.playhead_ms)
                else:
                    self.statusBar().showMessage("同一时刻未生成空区间", 2500)
                return
            for previous_index, start in active_body:
                self.pending_intervals.pop(previous_index, None)
                if self.playhead_ms > start:
                    self._add_event(previous_index, start, self.playhead_ms)
            self.pending_intervals[label_index] = self.playhead_ms
            self.statusBar().showMessage(
                f"{label.get('name')}：已开始；切换身体行为会自动结束上一段",
                4000,
            )
            return
        if label_index not in self.pending_intervals:
            self.pending_intervals[label_index] = self.playhead_ms
            self.statusBar().showMessage(
                f"{label.get('name')}：已记录开始，再按 {label.get('key')} 结束"
            )
        else:
            start = self.pending_intervals.pop(label_index)
            self._add_event(
                label_index,
                min(start, self.playhead_ms),
                max(start, self.playhead_ms),
            )

    def _cancel_pending(self) -> None:
        self.pending_intervals.clear()
        self.statusBar().showMessage("已取消待闭合区间")

    def _refresh_events(self) -> None:
        self.event_table.blockSignals(True)
        self.event_table.setRowCount(len(self.events))
        select_row = -1
        record_start_ms = int(
            getattr(self, "data_create_time_ms", 0) or 0
        )

        def display_event_time(relative_ms: float) -> str:
            if record_start_ms:
                return format_wall(record_start_ms + relative_ms)
            return format_relative(relative_ms)

        for row, event in enumerate(self.events):
            label_index = int(event.get("li", -1))
            label = (
                self.labels[label_index]
                if 0 <= label_index < len(self.labels)
                else {}
            )
            start = float(event.get("t0", 0))
            end = event.get("t1")
            duration = (
                ""
                if end is None
                else format_relative(float(end) - start)
            )
            values = [
                str(event.get("id", row + 1)),
                LAYER_NAMES.get(
                    str(label.get("layer", "")), str(label.get("layer", ""))
                ),
                str(label.get("name", f"#{label_index}")),
                display_event_time(start),
                "点" if end is None else display_event_time(float(end)),
                duration,
                str(event.get("note", "")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(256, int(event.get("id", row + 1)))
                self.event_table.setItem(row, column, item)
            if int(event.get("id", -1)) == self.selected_event_id:
                select_row = row
        if select_row >= 0:
            # Refreshing the table selects the current event for display only.
            # Keep signals blocked so this programmatic selection cannot be
            # mistaken for a user click and seek both timelines back to t0.
            self.event_table.selectRow(select_row)
        self.event_table.blockSignals(False)
        self.event_count_label.setText(f"{len(self.events)} 条")
        self.plot.set_events(self.labels, self.events)
        self._sync_plot_event_selection()
        self._refresh_enabled()

    def _sync_plot_event_selection(self) -> None:
        """Keep manual and model event selection identical on the waveform."""

        set_selected_event = getattr(self.plot, "set_selected_event", None)
        if callable(set_selected_event):
            set_selected_event(self.selected_event_id)

    def _event_selected(self) -> None:
        rows = self.event_table.selectionModel().selectedRows()
        if not rows:
            self.selected_event_id = None
            self._sync_plot_event_selection()
            return
        row = rows[0].row()
        item = self.event_table.item(row, 0)
        if item is None:
            return
        self.selected_event_id = int(item.data(256))
        self._sync_plot_event_selection()
        event = next(
            (e for e in self.events if int(e["id"]) == self.selected_event_id),
            None,
        )
        if event:
            self.set_playhead(float(event["t0"]))
            self._reflect_event_meta(event)

    def _reflect_event_meta(self, event: dict[str, Any]) -> None:
        """Echo the selected event's metadata to the editor controls."""

        for combo, value in (
            (self.evidence_combo, str(event.get("ev", "both"))),
        ):
            index = combo.findData(value)
            if index >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)
        self.reason_edit.blockSignals(True)
        self.reason_edit.setText(str(event.get("note", "")))
        self.reason_edit.blockSignals(False)

    def _annotation_meta_changed(self) -> None:
        """Apply metadata edits to the selected event."""

        if self.selected_event_id is None or self._restoring_project:
            return
        event = next(
            (
                e
                for e in self.events
                if int(e.get("id", -1)) == self.selected_event_id
            ),
            None,
        )
        if event is None:
            return
        event["ev"] = str(self.evidence_combo.currentData() or "both")
        event["ctx"] = str(self.context_combo.currentData() or "")
        event["note"] = str(self.reason_edit.text()).strip()
        event["reviewed_range"] = {
            "start": float(event.get("t0", 0.0)),
            "end": event.get("t1"),
        }
        self._refresh_events()
        self._autosave()

    def _event_double_clicked(self, row: int, _column: int) -> None:
        if not (0 <= row < len(self.events)):
            return
        event_id = int(self.event_table.item(row, 0).data(256))
        event = next(
            (e for e in self.events if int(e["id"]) == event_id), None
        )
        if event is None:
            return
        note, ok = QInputDialog.getMultiLineText(
            self, "事件备注", "备注", str(event.get("note", ""))
        )
        if ok:
            event["note"] = note
            self.reason_edit.blockSignals(True)
            self.reason_edit.setText(note)
            self.reason_edit.blockSignals(False)
            self._refresh_events()
            self._autosave()

    def delete_selected_event(self) -> None:
        if self.selected_event_id is None:
            return
        self.events = [
            event
            for event in self.events
            if int(event.get("id", -1)) != self.selected_event_id
        ]
        self.selected_event_id = None
        self._refresh_events()
        self._autosave()

    def clear_events(self) -> None:
        if not self.events:
            return
        if (
            QMessageBox.question(self, "清空标注", "确定删除全部标注事件？")
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.events.clear()
        self.selected_event_id = None
        self.pending_intervals.clear()
        self._refresh_events()
        self._autosave()

    def _project_model(self) -> annotation_core.Project:
        source = {
            "name": Path(self.data_path).stem if self.data_path else "",
            "path": self.data_path,
            "device": self.data_device,
            "createTime": self.data_create_time_ms,
            "dataVersion": getattr(self.data, "version", 0) if self.data else 0,
            "accScale": int(self.acc_scale_combo.currentText()),
            "durationMs": self.data_duration_ms,
            "sampleCount": int(
                getattr(self.data, "sample_count", 0) if self.data else 0
            ),
        }
        align = {
            "method": self.align_method,
            "videoStartWall": self.video_start_wall_ms,
            "offsetMs": (
                None
                if self.video_start_wall_ms is None
                else self.video_start_wall_ms - self.data_create_time_ms
            ),
        }
        payload = {
            "_type": "bovine-annotation-project",
            "version": 4,
            "annotator": self.annotator_edit.text().strip(),
            "protocol": self.protocol_edit.text().strip(),
            "cow_id": self.cow_id_edit.text().strip(),
            "source": source,
            "align": align,
            "videoName": Path(self.video_path).name if self.video_path else "",
            "videoPath": self.video_path,
            "labels": self.labels,
            "events": self.events,
            "ui": {
                "playheadMs": self.playhead_ms,
                "view": list(self.plot.view_range),
                "rate": float(self.rate_combo.currentData() or 1.0),
            },
        }
        return annotation_core.Project.from_dict(payload)

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
            project = self._project_model()
            payload = project.to_dict()
            payload["videoPath"] = self.video_path
            payload["ui"] = {
                "playheadMs": self.playhead_ms,
                "view": list(self.plot.view_range),
                "rate": float(self.rate_combo.currentData() or 1.0),
            }
            Path(path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._project_path = os.path.abspath(path)
            self.statusBar().showMessage(f"工程已保存：{path}", 5000)
        except Exception as exc:
            self._show_error(f"工程保存失败：\n{exc}")

    def load_project_dialog(self, path: str | None = None) -> None:
        if path is None or isinstance(path, bool):
            path, _ = QFileDialog.getOpenFileName(
                self, "载入标注工程", str(Path.cwd()), "工程 JSON (*.json)"
            )
        if not path:
            return
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            project = annotation_core.Project.from_dict(raw)
        except Exception as exc:
            self._show_error(f"工程载入失败：\n{exc}")
            return
        self._restoring_project = True
        try:
            self.labels = [label.to_dict() for label in project.labels]
            self.events = [event.to_dict() for event in project.events]
            self.next_event_id = max(
                (int(event["id"]) for event in self.events), default=0
            ) + 1
            self.pending_intervals.clear()
            self.annotator_edit.setText(project.annotator)
            self.protocol_edit.setText(project.protocol)
            self.cow_id_edit.setText(project.cow_id)
            scale = str(project.source.get("accScale", 4096))
            index = self.acc_scale_combo.findText(scale)
            if index >= 0:
                self.acc_scale_combo.setCurrentIndex(index)
            data_path = str(project.source.get("path", ""))
            video_path = str(raw.get("videoPath", ""))
            if data_path and Path(data_path).is_file():
                self.open_json(data_path)
            if video_path and Path(video_path).is_file():
                self.open_video(video_path)
            self.video_start_wall_ms = project.align.get("videoStartWall")
            self.align_method = str(project.align.get("method", "none"))
            self._project_path = os.path.abspath(path)
            self._refresh_labels()
            self._refresh_events()
            ui = raw.get("ui", {})
            self.set_playhead(float(ui.get("playheadMs", 0)))
            view = ui.get("view")
            if isinstance(view, list) and len(view) == 2:
                self.plot.set_view(float(view[0]), float(view[1]))
            self._update_alignment_status()
        finally:
            self._restoring_project = False

    def _validation_summary(
        self, project: annotation_core.Project
    ) -> str:
        issues = annotation_core.validate_project(
            project,
            duration_ms=self.data_duration_ms or None,
            require_alignment=True,
        )
        if not issues:
            return ""
        return "\n".join(
            f"[{'错误' if issue.severity == 'error' else '提示'}] "
            f"{issue.message}"
            for issue in issues
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
        summary = self._validation_summary(project)
        if summary and QMessageBox.warning(
            self,
            "导出检查",
            summary + "\n\n仍然导出？",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        times = getattr(self.data, "times_ms", None)
        try:
            annotation_core.export_events_csv(
                project, path, relative_timestamps_ms=times
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
                    "resting_g": getattr(self.data, "static_g", ""),
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
        if path:
            try:
                annotation_core.export_boris_csv(
                    self._project_model(), path
                )
            except Exception as exc:
                self._show_error(f"聚合 CSV 导出失败：\n{exc}")

    def export_training_csv(self) -> None:
        if self.data is None or not self.events:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出训练样本 CSV",
            str(Path(self.data_path).with_suffix(".samples.csv"))
            if self.data_path else "training_samples.csv",
            "CSV (*.csv)",
        )
        if not path:
            return
        project = self._project_model()
        issues = annotation_core.validate_project(
            project,
            duration_ms=self.data_duration_ms or None,
            require_alignment=True,
            require_cow_id=True,
        )
        errors = [issue.message for issue in issues if issue.is_error]
        if errors:
            self._show_error(
                "存在结构性问题，已阻止训练样本导出：\n"
                + "\n".join(f"• {message}" for message in errors)
            )
            return
        times = getattr(self.data, "times_ms", None)
        if times is None or len(times) == 0:
            self._show_error("当前九轴数据没有真实 JSON 样本时间")
            return
        try:
            annotation_core.export_sample_multihot_csv(project, path, times)
            self.statusBar().showMessage(f"已导出训练样本：{path}", 5000)
        except Exception as exc:
            self._show_error(f"训练样本导出失败：\n{exc}")

    def compare_irr(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择两份标注工程",
            str(Path.cwd()),
            "工程 JSON (*.json)",
        )
        if len(paths) != 2:
            return
        try:
            a = annotation_core.load_project(paths[0])
            b = annotation_core.load_project(paths[1])
            report = annotation_core.compare_projects(a, b)
        except Exception as exc:
            self._show_error(f"IRR 计算失败：\n{exc}")
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "保存 IRR 报告", "irr_report.csv", "CSV (*.csv)"
        )
        if target:
            annotation_core.export_irr_csv(report, target)
        QMessageBox.information(
            self,
            "IRR",
            f"micro-F1：{report.micro_f1:.3f}\n"
            f"macro-κ：{report.macro_kappa:.3f}\n"
            f"匹配事件：{report.matched_total}",
        )

    def _jump_activity(self, direction: int) -> None:
        if self.data is None:
            return
        times = getattr(self.data, "activity_times_ms", None)
        values = getattr(self.data, "activity_values", None)
        if times is None or values is None:
            activity = getattr(self.data, "activity", None)
            if activity is None:
                return
            values = np.asarray(activity)
            times = np.linspace(0, self.data_duration_ms, len(values))
        times = np.asarray(times, dtype=float)
        values = np.asarray(values, dtype=float)
        if times.size == 0:
            return
        threshold = np.nanpercentile(values, 80)
        candidates = times[values >= threshold]
        if direction > 0:
            future = candidates[candidates > self.playhead_ms + 1]
            target = future[0] if future.size else candidates[0]
        else:
            past = candidates[candidates < self.playhead_ms - 1]
            target = past[-1] if past.size else candidates[-1]
        self.set_playhead(float(target))

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
            path = folder / f"autosave_{key}.json"
            payload = self._project_model().to_dict()
            payload["videoPath"] = self.video_path
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            # Autosave must never interrupt annotation.
            pass

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "错误", str(message))

