from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QProcess, QStandardPaths
from PySide6.QtWidgets import QMessageBox, QProgressDialog

from ffmpeg_tools import (
    FFmpegToolError,
    cache_target,
    probe_media,
    streamcopy_command,
)


class SafeFFmpegMixin:
    """Crash-safe, no-transcode FFmpeg fallback for the release window."""

    def __init__(self) -> None:
        self._ffmpeg_partial: Path | None = None
        self._ffmpeg_saved_alignment: tuple[int | None, str] | None = None
        super().__init__()

    @staticmethod
    def _valid_cached_media(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        try:
            info = probe_media(path)
        except FFmpegToolError:
            return False
        return any(
            stream.get("codec_type") == "video"
            for stream in info.get("streams", [])
        )

    def _open_repackaged_preserving_alignment(self, path: Path) -> None:
        saved = self._ffmpeg_saved_alignment or (
            self.video_start_wall_ms,
            self.align_method,
        )
        self.open_video(str(path))
        if saved[0] is not None:
            self.video_start_wall_ms = saved[0]
            self.align_method = saved[1]
            self._update_alignment_status()
            self._seek_video_to_playhead()

    def _ffmpeg_diagnose(self) -> None:
        process = self._ffmpeg_process
        if (
            process is not None
            and process.state() != QProcess.ProcessState.NotRunning
        ):
            self.statusBar().showMessage("无损封装仍在进行，请稍候。", 3000)
            return
        if not self.video_path:
            self._show_error("请先打开需要诊断的视频。")
            return
        try:
            info = probe_media(self.video_path)
        except FFmpegToolError as exc:
            self._show_error(str(exc))
            return

        fmt = info.get("format", {})
        streams = info.get("streams", [])
        summary = [
            f"容器：{fmt.get('format_long_name') or fmt.get('format_name', '未知')}",
            f"时长：{fmt.get('duration', '未知')} 秒",
        ]
        for stream in streams:
            summary.append(
                f"{stream.get('codec_type', 'stream')}："
                f"{stream.get('codec_name', '未知')} "
                f"{stream.get('profile', '')}"
            )
        answer = QMessageBox.question(
            self,
            "FFmpeg 诊断",
            "\n".join(summary)
            + "\n\n是否只换成 MKV 封装后重新打开？\n"
            "该操作使用 -c copy，不重新编码、不损失画质，也不会修改原文件。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        cache_dir = Path(
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.CacheLocation
            )
        ) / "media"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg_target = cache_target(self.video_path, cache_dir)
        self._ffmpeg_saved_alignment = (
            self.video_start_wall_ms,
            self.align_method,
        )
        if self._valid_cached_media(self._ffmpeg_target):
            self._open_repackaged_preserving_alignment(self._ffmpeg_target)
            return
        if self._ffmpeg_target.exists():
            self._ffmpeg_target.unlink(missing_ok=True)

        # Keep a Matroska suffix so FFmpeg can infer the muxer.  Only a
        # successfully completed file is atomically promoted to the cache.
        self._ffmpeg_partial = self._ffmpeg_target.with_suffix(
            ".partial.mkv"
        )
        self._ffmpeg_partial.unlink(missing_ok=True)
        command = streamcopy_command(self.video_path, self._ffmpeg_partial)
        self._ffmpeg_process = QProcess(self)
        self._ffmpeg_progress = QProgressDialog(
            "正在无损更换封装…", "取消", 0, 0, self
        )
        self._ffmpeg_progress.setWindowTitle("FFmpeg")
        self._ffmpeg_progress.canceled.connect(self._ffmpeg_process.kill)
        self._ffmpeg_process.finished.connect(self._ffmpeg_finished)
        self.ffmpeg_btn.setEnabled(False)
        self._ffmpeg_process.start(command[0], command[1:])
        self._ffmpeg_progress.show()

    def _ffmpeg_finished(
        self, exit_code: int, _exit_status: QProcess.ExitStatus
    ) -> None:
        self.ffmpeg_btn.setEnabled(True)
        if self._ffmpeg_progress is not None:
            self._ffmpeg_progress.close()

        partial = self._ffmpeg_partial
        target = self._ffmpeg_target
        if (
            exit_code == 0
            and partial is not None
            and target is not None
            and partial.is_file()
            and partial.stat().st_size > 0
        ):
            try:
                os.replace(partial, target)
                if not self._valid_cached_media(target):
                    raise FFmpegToolError("换封装后的文件未检测到有效视频流")
                self._open_repackaged_preserving_alignment(target)
                self.statusBar().showMessage(
                    f"已无损封装并打开：{target}", 8000
                )
                return
            except (OSError, FFmpegToolError) as exc:
                target.unlink(missing_ok=True)
                detail = str(exc)
            else:
                detail = ""
        else:
            detail = ""

        if self._ffmpeg_process is not None:
            stderr = bytes(
                self._ffmpeg_process.readAllStandardError()
            ).decode("utf-8", errors="replace")
            detail = (detail + "\n" + stderr[-2000:]).strip()
        if partial is not None:
            partial.unlink(missing_ok=True)
        self._show_error("FFmpeg 无损封装失败。\n" + detail)

    def closeEvent(self, event) -> None:  # noqa: N802
        process = self._ffmpeg_process
        if (
            process is not None
            and process.state() != QProcess.ProcessState.NotRunning
        ):
            process.kill()
            process.waitForFinished(2000)
        if self._ffmpeg_partial is not None:
            self._ffmpeg_partial.unlink(missing_ok=True)
        super().closeEvent(event)
