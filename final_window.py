from __future__ import annotations

import os

from complete_window import MainWindow as FunctionalWindow
from safe_media_engine import SafeMediaEngine
from ui_helpers import format_relative


class MainWindow(FunctionalWindow):
    """Production window using the non-blocking VLC compatibility layer."""

    def __init__(self) -> None:
        previous = os.environ.get("BOVINE_NO_MEDIA")
        os.environ["BOVINE_NO_MEDIA"] = "1"
        try:
            super().__init__()
        finally:
            if previous is None:
                os.environ.pop("BOVINE_NO_MEDIA", None)
            else:
                os.environ["BOVINE_NO_MEDIA"] = previous

        self.media = SafeMediaEngine(self.video_surface, parent=self)
        self.media.status_changed.connect(self._on_media_status)
        self.media.error_occurred.connect(self._show_error)
        self.media.time_changed.connect(self._on_media_time)
        self.media.duration_changed.connect(self._on_media_duration)
        self.media.playing_changed.connect(self._on_playing_changed)
        self.media.ended.connect(self._on_media_ended)
        self.media.timeline_correction_changed.connect(
            self._on_timeline_correction
        )
        self.media.timeline_analysis_message.connect(
            self._on_timeline_analysis_message
        )
        self._refresh_enabled()

    def _on_timeline_correction(
        self, raw_duration_ms: int, corrected_duration_ms: int, gaps: int
    ) -> None:
        self.statusBar().showMessage(
            "检测到视频时间戳异常："
            f"{format_relative(raw_duration_ms)} → "
            f"{format_relative(corrected_duration_ms)}，"
            f"已折叠 {gaps} 处异常空洞",
            10_000,
        )

    def _on_timeline_analysis_message(
        self, message: str, timeout_ms: int
    ) -> None:
        self.statusBar().showMessage(message, max(0, int(timeout_ms)))

    def closeEvent(self, event) -> None:  # noqa: N802
        self._autosave()
        if self.media is not None:
            self.media.close()
        event.accept()
