from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QFileDialog

import annotation_core


class ReleaseHardeningMixin:
    """Final safety rules for media switching, alignment and rollback."""

    # A 100 ms keyboard seek must never be confirmed by the old frame.
    SEEK_CONFIRM_TOLERANCE_MS = 50.0

    def __init__(self) -> None:
        self._session_restore_incomplete = False
        self._session_restore_failure = ""
        super().__init__()

    def open_video(self, path: str | None = None) -> None:
        # Resolve the dialog before lower mixins reset priming or seek state.
        if path is None or isinstance(path, bool):
            start = self.settings.value(
                "last_video_dir",
                "G:\\" if Path("G:\\").exists() else str(Path.cwd()),
            )
            selected, _ = QFileDialog.getOpenFileName(
                self,
                "打开视频或录像块",
                str(start),
                "视频文件 (*.mp4 *.mkv *.avi *.mov *.ts *.m2ts *.ps "
                "*.mpeg *.h265 *.hevc);;所有文件 (*.*)",
            )
            if not selected:
                return
            path = selected

        normalized = os.path.abspath(path)
        super().open_video(normalized)
        opened = (
            self.media is not None
            and os.path.abspath(self.video_path or "") == normalized
            and os.path.abspath(self.media.current_path or "")
            == normalized
        )
        if opened:
            # A pending seek belongs to the previous media generation.
            self._clear_ui_seek()
            self._session_restore_incomplete = False
            self._session_restore_failure = ""

    def align_from_corner(self) -> None:
        previous = (self.video_start_wall_ms, self.align_method)
        super().align_from_corner()
        changed = (
            self.align_method == "corner"
            and (self.video_start_wall_ms, self.align_method) != previous
        )
        if not changed or self.media is None:
            return

        self._clear_ui_seek()
        limits = self._shared_video_limits()
        if limits is None or limits[1] - limits[0] < 1.0:
            self.media.pause(True)
            self.play_btn.setText("▶ 播放")
            self.video_status.setText(
                "角标校准后视频与九轴数据没有共同覆盖时间，已暂停"
            )
            return

        # Keep the actual video frame as the clock source and immediately
        # refresh both timelines under the new wall-clock mapping.
        actual = int(self.media.get_time_ms())
        self._on_media_time(actual)

    def _restore_session(self, snapshot: dict[str, Any]) -> None:
        try:
            super()._restore_session(snapshot)
        except Exception as exc:
            self._session_restore_failure = (
                f"恢复原会话时发生异常：{exc}"
            )
            self._session_restore_incomplete = True
            try:
                self._clear_loaded_video()
            except Exception:
                self.video_path = ""
                self.video_start_wall_ms = None
                self.align_method = "none"
            raise RuntimeError(self._session_restore_failure) from exc

        expected = str(snapshot.get("video_path") or "")
        actual = (
            self.media.current_path
            if self.media is not None
            else ""
        )
        matches = (
            bool(expected)
            and bool(actual)
            and os.path.normcase(os.path.abspath(expected))
            == os.path.normcase(os.path.abspath(actual))
        )
        if expected and not matches:
            self._session_restore_failure = (
                "原会话的视频未能重新打开；为避免路径与实际画面错配，"
                "当前视频已清空"
            )
            self._session_restore_incomplete = True
            self._clear_loaded_video()
            raise RuntimeError(self._session_restore_failure)
        if not expected and actual:
            self._session_restore_failure = (
                "原会话应为无视频状态，但播放器仍有关联媒体；"
                "当前视频已清空"
            )
            self._session_restore_incomplete = True
            self._clear_loaded_video()
            raise RuntimeError(self._session_restore_failure)

    def load_project_dialog(self, path: str | None = None) -> None:
        before_project = self._project_path
        try:
            super().load_project_dialog(path)
        except Exception as exc:
            self._session_restore_incomplete = True
            detail = self._session_restore_failure or str(exc)
            try:
                self._clear_loaded_video()
            except Exception:
                self.video_path = ""
                self.video_start_wall_ms = None
                self.align_method = "none"
            self._show_error(
                "工程载入失败，原会话的视频未能安全恢复。\n"
                "当前视频已清空，重新打开正确视频后方可导出。\n\n"
                + detail
            )
            self.statusBar().showMessage(
                "工程载入失败；视频恢复不完整，已阻止导出",
                8000,
            )
            return

        if self._project_path and self._project_path != before_project:
            self._session_restore_incomplete = False
            self._session_restore_failure = ""

    def _release_validation_issues(
        self, project: annotation_core.Project
    ) -> list[annotation_core.ValidationIssue]:
        issues = list(super()._release_validation_issues(project))
        if self._session_restore_incomplete:
            issues.append(
                annotation_core.ValidationIssue(
                    "error",
                    "session_restore_incomplete",
                    "上次工程回滚未能恢复原视频，请重新打开正确视频",
                )
            )
        return issues
