from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QProcess


class FFmpegProcessGuardMixin:
    """Recover the FFmpeg UI if Windows refuses to start the process."""

    def __init__(self) -> None:
        self._guarded_ffmpeg_process: QProcess | None = None
        super().__init__()

    def _ffmpeg_diagnose(self) -> None:
        super()._ffmpeg_diagnose()
        process = self._ffmpeg_process
        if (
            process is not None
            and process is not self._guarded_ffmpeg_process
        ):
            self._guarded_ffmpeg_process = process
            process.errorOccurred.connect(self._ffmpeg_process_error)

    def _ffmpeg_process_error(
        self, error: QProcess.ProcessError
    ) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        self.ffmpeg_btn.setEnabled(True)
        if self._ffmpeg_progress is not None:
            self._ffmpeg_progress.close()
        partial = self._ffmpeg_partial
        if partial is not None:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
        if not getattr(self, "_release_closing", False):
            self._show_error(
                "FFmpeg 无法启动。请检查 "
                r"F:\Applications\ffmpeg-8.1.2-full_build"
            )

    def closeEvent(self, event) -> None:  # noqa: N802
        partial: Path | None = self._ffmpeg_partial
        # The lower close handler kills and waits for QProcess.  Clearing this
        # attribute prevents an unlink attempt while Windows may still hold
        # the file handle.
        self._ffmpeg_partial = None
        super().closeEvent(event)
        if (
            partial is not None
            and (
                self._ffmpeg_process is None
                or self._ffmpeg_process.state()
                == QProcess.ProcessState.NotRunning
            )
        ):
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                # It remains a .partial.mkv and can never be mistaken for a
                # completed cache; the next run will remove it.
                pass
