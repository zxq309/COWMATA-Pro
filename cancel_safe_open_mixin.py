from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import QFileDialog


class CancelSafeOpenMixin:
    """Do not reset a working player when the open dialog is cancelled."""

    def open_video(self, path: str | None = None) -> None:
        if path is None or isinstance(path, bool):
            start = self.settings.value(
                "last_video_dir",
                "G:\\" if Path("G:\\").exists() else str(Path.cwd()),
            )
            selected, _ = QFileDialog.getOpenFileName(
                self,
                "打开视频或录像块",
                str(start),
                "视频文件 (*.mp4 *.mkv *.avi *.mov *.ts *.m2ts *.ps *.mpeg "
                "*.h265 *.hevc);;所有文件 (*.*)",
            )
            path = selected
        if not path:
            return
        normalized = os.path.abspath(path)
        if not Path(normalized).is_file():
            self._show_error(f"视频文件不存在：\n{normalized}")
            return

        state = (
            self._media_primed,
            self._queued_play_after_prime,
            self._prime_pending_seek_ms,
        )
        super().open_video(normalized)
        opened = (
            os.path.abspath(self.video_path or "") == normalized
            and self.media is not None
            and os.path.abspath(self.media.current_path or "")
            == normalized
        )
        if not opened:
            (
                self._media_primed,
                self._queued_play_after_prime,
                self._prime_pending_seek_ms,
            ) = state
