from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QProcess, QStandardPaths
from PySide6.QtWidgets import QMessageBox, QProgressDialog, QPushButton

from cowmata_tailring.app._layer_stable import MainWindow as StableWindow
from cowmata_tailring.media.ffmpeg_tools import (
    FFmpegToolError,
    cache_target,
    probe_media,
    streamcopy_command,
)
from cowmata_tailring.ui.i18n import t


class MainWindow(StableWindow):
    """Adds explicit FFmpeg diagnosis and no-transcode fallback packaging."""

    def __init__(self) -> None:
        super().__init__()
        self.ffmpeg_btn = QPushButton("FFmpeg 诊断 / 无损封装")
        utility_layout = getattr(self, "video_utility_layout", None)
        if utility_layout is not None:
            utility_layout.addWidget(self.ffmpeg_btn)
        else:
            self.video_surface.parentWidget().layout().insertWidget(
                4, self.ffmpeg_btn
            )
        self.ffmpeg_btn.clicked.connect(self._ffmpeg_diagnose)
        self._ffmpeg_process: QProcess | None = None
        self._ffmpeg_progress: QProgressDialog | None = None
        self._ffmpeg_target: Path | None = None

    def _ffmpeg_diagnose(self) -> None:
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
            t("FFmpeg 诊断"),
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
        if self._ffmpeg_target.is_file():
            self.open_video(str(self._ffmpeg_target))
            return
        command = streamcopy_command(self.video_path, self._ffmpeg_target)
        self._ffmpeg_process = QProcess(self)
        self._ffmpeg_progress = QProgressDialog(
            "正在无损更换封装…", "取消", 0, 0, self
        )
        self._ffmpeg_progress.setWindowTitle("FFmpeg")
        self._ffmpeg_progress.canceled.connect(self._ffmpeg_process.kill)
        self._ffmpeg_process.finished.connect(self._ffmpeg_finished)
        self._ffmpeg_process.start(command[0], command[1:])
        self._ffmpeg_progress.show()

    def _ffmpeg_finished(
        self, exit_code: int, _exit_status: QProcess.ExitStatus
    ) -> None:
        if self._ffmpeg_progress is not None:
            self._ffmpeg_progress.close()
        if (
            exit_code == 0
            and self._ffmpeg_target is not None
            and self._ffmpeg_target.is_file()
        ):
            self.open_video(str(self._ffmpeg_target))
            self.statusBar().showMessage(
                f"已无损封装并打开：{self._ffmpeg_target}", 8000
            )
            return
        detail = ""
        if self._ffmpeg_process is not None:
            detail = bytes(
                self._ffmpeg_process.readAllStandardError()
            ).decode("utf-8", errors="replace")
        self._show_error(
            "FFmpeg 无损封装失败。\n" + detail[-2000:]
        )
