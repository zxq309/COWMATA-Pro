from __future__ import annotations

import time

from PySide6.QtCore import QTimer

from cowmata_tailring.ui.i18n import t


class SeekWatchdogMixin:
    """Confirm or expire one seek even if libVLC emits no time signal."""

    def __init__(self) -> None:
        super().__init__()
        self._seek_watchdog = QTimer(self)
        self._seek_watchdog.setInterval(400)
        self._seek_watchdog.timeout.connect(self._check_pending_seek)

    def _arm_ui_seek(self, target_ms: float) -> None:
        super()._arm_ui_seek(target_ms)
        self._seek_watchdog.start()

    def _clear_ui_seek(self) -> None:
        super()._clear_ui_seek()
        if hasattr(self, "_seek_watchdog"):
            self._seek_watchdog.stop()

    def _check_pending_seek(self) -> None:
        pending = self._ui_pending_seek_video_ms
        if pending is None:
            self._seek_watchdog.stop()
            return
        now = time.monotonic()
        actual = self.media.get_time_ms() if self.media is not None else 0
        tolerance = float(
            getattr(self, "SEEK_CONFIRM_TOLERANCE_MS", 250.0)
        )
        if abs(float(actual) - float(pending)) <= tolerance:
            # MediaEngine only emits time_changed when the public VLC time
            # changes.  Seeking to the currently displayed value therefore
            # needs an explicit confirmation check.
            self._on_media_time(int(actual))
            return
        if now >= self._ui_seek_deadline:
            self._clear_ui_seek()
            cancel = (
                getattr(self.media, "cancel_seek_confirmation", None)
                if self.media is not None
                else None
            )
            if callable(cancel):
                cancel()
            self.statusBar().showMessage(
                t("视频定位未在 8 秒内确认，已恢复播放器实际位置"),
                5000,
            )
            reset_clock = getattr(self, "_reset_smooth_clock", None)
            present_position = getattr(
                self, "_present_smooth_position", None
            )
            if callable(reset_clock) and callable(present_position):
                reset_clock(float(actual), now)
                present_position(float(actual))
            else:
                self._on_media_time(int(actual))
