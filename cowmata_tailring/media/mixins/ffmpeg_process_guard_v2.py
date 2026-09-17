from __future__ import annotations

from PySide6.QtCore import QProcess


class FFmpegProcessGuardV2Mixin:
    """Catch both synchronous and asynchronous FFmpeg start failures."""

    def __init__(self) -> None:
        self._ffmpeg_failed_process_handled: QProcess | None = None
        super().__init__()

    def _ffmpeg_diagnose(self) -> None:
        super()._ffmpeg_diagnose()
        process = self._ffmpeg_process
        if (
            process is not None
            and process.error() == QProcess.ProcessError.FailedToStart
        ):
            # On Windows FailedToStart can be emitted synchronously by
            # start(), before the lower guard has connected its signal.
            self._ffmpeg_process_error(
                QProcess.ProcessError.FailedToStart
            )

    def _ffmpeg_process_error(
        self, error: QProcess.ProcessError
    ) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            process = self._ffmpeg_process
            if (
                process is not None
                and process is self._ffmpeg_failed_process_handled
            ):
                return
            self._ffmpeg_failed_process_handled = process
        super()._ffmpeg_process_error(error)
