from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QFileDialog, QMessageBox

import annotation_core
from ffmpeg_tools import FFmpegToolError, find_ffmpeg, probe_media
from media_timeline import (
    MediaTimelineIndex,
    TimelineProbeError,
    load_timeline_cache,
    probe_media_timeline,
    save_timeline_cache,
)


VIDEO_IDENTITY_DURATION_TOLERANCE_MS = 2_000


def _identity_duration_value(identity: dict[str, Any], key: str) -> int:
    try:
        value = int(round(float(identity.get(key, 0) or 0)))
    except (OverflowError, TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _expected_video_durations(identity: dict[str, Any]) -> tuple[int, ...]:
    values: list[int] = []
    for key in ("durationMs", "rawDurationMs", "continuousDurationMs"):
        value = _identity_duration_value(identity, key)
        if value > 0 and value not in values:
            values.append(value)
    return tuple(values)


def _video_identity_duration_matches(
    identity: dict[str, Any],
    raw_duration_ms: int,
    timeline_index: MediaTimelineIndex | None = None,
) -> bool:
    """Accept either the container duration or the corrected UI timeline."""

    expected = _expected_video_durations(identity)
    if not expected:
        return True
    actual_raw = [int(raw_duration_ms)] if raw_duration_ms > 0 else []
    if timeline_index is not None:
        rounded_raw = int(round(float(timeline_index.raw_duration_ms)))
        if rounded_raw > 0 and rounded_raw not in actual_raw:
            actual_raw.append(rounded_raw)
    expected_raw = _identity_duration_value(identity, "rawDurationMs")
    schema = _identity_duration_value(identity, "schema")
    duration_basis = str(identity.get("durationBasis", "") or "")
    public_duration_is_explicit = bool(
        schema >= 2 or duration_basis == "player_public_timeline"
    )
    if expected_raw and actual_raw and not public_duration_is_explicit:
        return any(
            abs(expected_raw - actual_value)
            <= VIDEO_IDENTITY_DURATION_TOLERANCE_MS
            for actual_value in actual_raw
        )

    actual = list(actual_raw)
    if timeline_index is not None:
        rounded_continuous = int(round(float(timeline_index.duration_ms)))
        if rounded_continuous > 0 and rounded_continuous not in actual:
            actual.append(rounded_continuous)
    if not actual:
        return True
    return any(
        abs(expected_value - actual_value)
        <= VIDEO_IDENTITY_DURATION_TOLERANCE_MS
        for expected_value in expected
        for actual_value in actual
    )


class TransactionalProjectMixin:
    """Preflight and roll back project loads as one logical transaction."""

    def __init__(self) -> None:
        self._transaction_video_override: Path | None = None
        super().__init__()

    def _project_model(self) -> annotation_core.Project:
        project = super()._project_model()
        if self.video_path and Path(self.video_path).is_file():
            path = Path(self.video_path)
            reported_duration = 0
            if self.media is not None:
                player_duration = getattr(
                    self.media,
                    "player_duration_ms",
                    None,
                )
                reported_duration = (
                    player_duration()
                    if callable(player_duration)
                    else self.media.duration_ms()
                )
            if reported_duration <= 0 and self.media is not None:
                reported_duration = self.media.duration_ms()
            identity: dict[str, Any] = {
                "schema": 2,
                "name": path.name,
                "size": path.stat().st_size,
                "durationMs": reported_duration,
                "durationBasis": "player_public_timeline",
            }
            timeline_index = (
                getattr(self.media, "_timeline_index", None)
                if self.media is not None
                else None
            )
            if (
                isinstance(timeline_index, MediaTimelineIndex)
                and os.path.normcase(
                    os.path.abspath(timeline_index.source_path)
                )
                == os.path.normcase(os.path.abspath(path))
            ):
                identity.update(
                    {
                        "rawDurationMs": int(
                            round(timeline_index.raw_duration_ms)
                        ),
                        "continuousDurationMs": int(
                            round(timeline_index.duration_ms)
                        ),
                        "timelineCorrected": bool(
                            timeline_index.is_corrected
                        ),
                    }
                )
            project.extras["videoIdentity"] = identity
        return project

    def _release_validation_issues(
        self, project: annotation_core.Project
    ) -> list[annotation_core.ValidationIssue]:
        issues = list(super()._release_validation_issues(project))
        return issues

    def _event_changed_on_plot(
        self, event_id: int, start_ms: float, end_ms: Any
    ) -> None:
        before = self._drag_history_snapshot
        if before is not None:
            old = next(
                (
                    item
                    for item in before["events"]
                    if int(item.get("id", -1)) == int(event_id)
                ),
                None,
            )
            current = next(
                (
                    item
                    for item in self.events
                    if int(item.get("id", -1)) == int(event_id)
                ),
                None,
            )
            if (
                old is not None
                and current is not None
                and float(old.get("t0", 0.0))
                == float(current.get("t0", 0.0))
                and old.get("t1") == current.get("t1")
            ):
                self._drag_history_snapshot = None
        super()._event_changed_on_plot(event_id, start_ms, end_ms)

    def _capture_session(self) -> dict[str, Any]:
        return {
            "data": self.data,
            "data_path": self.data_path,
            "labels": copy.deepcopy(self.labels),
            "events": copy.deepcopy(self.events),
            "next_event_id": self.next_event_id,
            "selected_label": self.selected_label,
            "selected_event_id": self.selected_event_id,
            "pending_intervals": copy.deepcopy(self.pending_intervals),
            "annotator": self.annotator_edit.text(),
            "protocol": self.protocol_edit.text(),
            "cow_id": self.cow_id_edit.text(),
            "scale": self.acc_scale_combo.currentText(),
            "video_path": self.video_path,
            "video_start_wall_ms": self.video_start_wall_ms,
            "align_method": self.align_method,
            "playhead_ms": self.playhead_ms,
            "view": tuple(self.plot.view_range),
            "rate": float(self.rate_combo.currentData() or 1.0),
            "project_path": self._project_path,
        }

    def _session_signature(self) -> tuple[Any, ...]:
        return (
            id(self.data),
            self.data_path,
            self.video_path,
            repr(self.labels),
            repr(self.events),
            self._project_path,
        )

    def _restore_session(self, snapshot: dict[str, Any]) -> None:
        previous_restore = self._restoring_project
        previous_history = self._history_suspended
        self._restoring_project = True
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
            self.annotator_edit.setText(snapshot["annotator"])
            self.protocol_edit.setText(snapshot["protocol"])
            self.cow_id_edit.setText(snapshot.get("cow_id", ""))

            self.acc_scale_combo.blockSignals(True)
            scale_index = self.acc_scale_combo.findText(snapshot["scale"])
            if scale_index >= 0:
                self.acc_scale_combo.setCurrentIndex(scale_index)
            self.acc_scale_combo.blockSignals(False)

            self.data = snapshot["data"]
            self.data_path = snapshot["data_path"]
            if self.data is not None:
                self._display_motion_data()
            else:
                self.plot.clear_data()
                self.data_title.setText("尚未打开九轴 JSON")

            old_video = snapshot["video_path"]
            current_media = (
                self.media.current_path if self.media is not None else ""
            )
            if old_video:
                if os.path.abspath(current_media or "") != os.path.abspath(
                    old_video
                ):
                    self.open_video(old_video)
                self.video_path = old_video
            elif self.video_path:
                self._clear_loaded_video()

            self.video_start_wall_ms = snapshot[
                "video_start_wall_ms"
            ]
            self.align_method = snapshot["align_method"]
            self._project_path = snapshot["project_path"]
            self._refresh_labels()
            self._refresh_events()
            view = snapshot["view"]
            self.plot.set_view(float(view[0]), float(view[1]))
            rate_index = self.rate_combo.findData(snapshot["rate"])
            if rate_index >= 0:
                self.rate_combo.setCurrentIndex(rate_index)
            self.set_playhead(float(snapshot["playhead_ms"]))
            self._update_alignment_status()
            self._refresh_enabled()
        finally:
            self._restoring_project = previous_restore
            self._history_suspended = previous_history

    @staticmethod
    def _media_duration_from_probe(info: dict[str, Any]) -> int:
        try:
            return int(round(float(info.get("format", {}).get("duration")) * 1000))
        except (TypeError, ValueError):
            return 0

    def _timeline_cache_directory_for_identity(self) -> Path:
        getter = (
            getattr(self.media, "_timeline_cache_directory", None)
            if self.media is not None
            else None
        )
        if callable(getter):
            try:
                return Path(getter())
            except (OSError, TypeError, ValueError):
                pass
        root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.CacheLocation
        )
        if not root:
            root = str(Path.home() / ".bovine-motion-workbench" / "cache")
        return Path(root) / "video-timeline"

    def _timeline_index_for_identity(
        self, candidate: Path
    ) -> MediaTimelineIndex | None:
        active = (
            getattr(self.media, "_timeline_index", None)
            if self.media is not None
            else None
        )
        if isinstance(active, MediaTimelineIndex):
            try:
                same_source = (
                    os.path.normcase(os.path.abspath(active.source_path))
                    == os.path.normcase(os.path.abspath(candidate))
                    and active.source_size == candidate.stat().st_size
                    and active.source_mtime_ns == candidate.stat().st_mtime_ns
                )
            except OSError:
                same_source = False
            if same_source:
                return active

        cache_directory = self._timeline_cache_directory_for_identity()
        try:
            cached = load_timeline_cache(cache_directory, candidate)
        except OSError:
            cached = None
        if cached is not None:
            return cached

        self.statusBar().showMessage(
            "检测到监控视频原始时长异常，正在核对连续时间轴…",
            0,
        )
        try:
            _ffmpeg, ffprobe = find_ffmpeg()
            index = probe_media_timeline(
                candidate,
                ffprobe,
                timeout_seconds=60.0,
            )
        except (FFmpegToolError, TimelineProbeError, OSError):
            return None
        try:
            save_timeline_cache(cache_directory, index)
        except OSError:
            pass
        return index

    def _confirm_video_identity(
        self,
        project: annotation_core.Project,
        raw: dict[str, Any],
        project_path: Path,
    ) -> bool:
        video_value = raw.get("videoPath")
        candidate = self._resolve_saved_path(video_value, project_path)
        expects_video = bool(video_value or project.videoName)
        if not expects_video:
            self._transaction_video_override = None
            return True

        relinked = candidate is None or not candidate.is_file()
        if relinked:
            selected, _ = QFileDialog.getOpenFileName(
                self,
                "工程中的视频路径失效，请重新关联",
                str(project_path.parent),
                "视频文件 (*.mp4 *.mkv *.avi *.mov *.ts *.m2ts *.ps "
                "*.mpeg *.h265 *.hevc);;所有文件 (*.*)",
            )
            if not selected:
                return False
            candidate = Path(selected).resolve()
        assert candidate is not None

        try:
            info = probe_media(candidate)
        except FFmpegToolError as exc:
            self._show_error(f"视频身份校验失败：\n{exc}")
            return False
        if not any(
            stream.get("codec_type") == "video"
            for stream in info.get("streams", [])
        ):
            self._show_error("视频身份校验失败：文件中没有视频流")
            return False

        expected = raw.get("videoIdentity")
        if isinstance(expected, dict) and expected:
            mismatches: list[str] = []
            expected_name = str(expected.get("name", ""))
            if expected_name and candidate.name != expected_name:
                mismatches.append(
                    f"文件名应为 {expected_name}，实际为 {candidate.name}"
                )
            expected_size = int(expected.get("size", 0) or 0)
            if expected_size and candidate.stat().st_size != expected_size:
                mismatches.append(
                    f"文件大小应为 {expected_size}，实际为 "
                    f"{candidate.stat().st_size}"
                )
            expected_durations = _expected_video_durations(expected)
            actual_duration = self._media_duration_from_probe(info)
            if (
                not mismatches
                and expected_durations
                and actual_duration
                and not _video_identity_duration_matches(
                    expected,
                    actual_duration,
                )
            ):
                # Schema-2 projects deliberately record both VLC's public
                # duration and the packet timeline duration.  Depending on
                # the container/driver, FFprobe may report either one during
                # a later relink.  Reuse or rebuild the timeline index before
                # rejecting a project instead of treating those two valid
                # views of the same file as a conflict.
                timeline_index = self._timeline_index_for_identity(candidate)
                if not _video_identity_duration_matches(
                    expected,
                    actual_duration,
                    timeline_index,
                ):
                    expected_text = "/".join(
                        str(value) for value in expected_durations
                    )
                    actual_text = f"原始 {actual_duration} ms"
                    if timeline_index is not None:
                        actual_text += (
                            f"，连续 {int(round(timeline_index.duration_ms))} ms"
                        )
                    mismatches.append(
                        f"时长应约为 {expected_text} ms，实际为 "
                        + actual_text
                    )
            if mismatches:
                self._show_error(
                    "重新关联的视频与工程记录不一致，已取消载入：\n"
                    + "\n".join(f"• {item}" for item in mismatches)
                )
                return False
        else:
            name_mismatch = (
                bool(project.videoName)
                and candidate.name != project.videoName
            )
            if relinked or name_mismatch:
                detail = (
                    f"工程记录：{project.videoName or '未记录文件名'}\n"
                    f"当前选择：{candidate.name}\n"
                    f"大小：{candidate.stat().st_size} 字节\n"
                    f"时长：{self._media_duration_from_probe(info)} ms\n\n"
                    "旧工程没有保存视频指纹，确认这是同一段录像？"
                )
                if (
                    QMessageBox.warning(
                        self,
                        "确认重新关联视频",
                        detail,
                        QMessageBox.StandardButton.Yes
                        | QMessageBox.StandardButton.No,
                    )
                    != QMessageBox.StandardButton.Yes
                ):
                    return False

        self._transaction_video_override = candidate
        return True

    def _choose_missing_video(
        self, candidate: Path | None, project_path: Path
    ) -> Path | None:
        if self._transaction_video_override is not None:
            return self._transaction_video_override
        return super()._choose_missing_video(candidate, project_path)

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

        if len(project.labels) > self.MAX_VISIBLE_LABELS:
            self._show_error(
                f"工程包含 {len(project.labels)} 个标签，当前正式版最多"
                f"显示 {self.MAX_VISIBLE_LABELS} 个，已取消载入。"
            )
            return
        if not self._confirm_video_identity(
            project, raw, project_path
        ):
            self._transaction_video_override = None
            return

        snapshot = self._capture_session()
        signature = self._session_signature()
        messages: list[str] = []
        had_override = "_show_error" in self.__dict__
        previous_override = self.__dict__.get("_show_error")
        original_show_error = self._show_error

        def tracked_error(message: str) -> None:
            messages.append(str(message))
            original_show_error(message)

        self._show_error = tracked_error
        try:
            super().load_project_dialog(str(project_path))
        finally:
            if had_override:
                self._show_error = previous_override
            else:
                del self.__dict__["_show_error"]
            self._transaction_video_override = None

        changed = self._session_signature() != signature
        committed = (
            bool(self._project_path)
            and os.path.abspath(self._project_path)
            == os.path.abspath(str(project_path))
        )
        if changed and (messages or not committed):
            self._restore_session(snapshot)
            self.statusBar().showMessage(
                "工程载入失败，已完整恢复原会话", 5000
            )
