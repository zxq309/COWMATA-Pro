from __future__ import annotations

from PySide6.QtCore import QProcess

from cowmata_tailring.app._layer_ffmpeg import MainWindow as ExistingWindow
from cowmata_tailring.app.mixins.production_safety import ProductionSafetyMixin
from cowmata_tailring.media.mixins.safe_ffmpeg import SafeFFmpegMixin
from cowmata_tailring.ui.i18n import t


class MainWindow(
    ProductionSafetyMixin,
    SafeFFmpegMixin,
    ExistingWindow,
):
    """Audited release window used by the authoritative desktop entry."""

    def __init__(self) -> None:
        self._release_closing = False
        super().__init__()
        self.setWindowTitle(t("牛尾环九轴视频标注工作台 · 桌面正式版"))

    def _ffmpeg_finished(
        self, exit_code: int, exit_status: QProcess.ExitStatus
    ) -> None:
        if self._release_closing:
            if self._ffmpeg_progress is not None:
                self._ffmpeg_progress.close()
            if self._ffmpeg_partial is not None:
                self._ffmpeg_partial.unlink(missing_ok=True)
            return
        super()._ffmpeg_finished(exit_code, exit_status)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._release_closing = True
        super().closeEvent(event)
