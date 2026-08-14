from __future__ import annotations

from cancel_safe_open_mixin import CancelSafeOpenMixin
from desktop_release_window import MainWindow as DesktopReleaseWindow
from ffmpeg_process_guard_mixin import FFmpegProcessGuardMixin
from playback_guard_mixin import PlaybackGuardMixin
from priming_playback_mixin import PrimingPlaybackMixin
from seek_watchdog_mixin import SeekWatchdogMixin
from transactional_project_mixin import TransactionalProjectMixin


class MainWindow(
    TransactionalProjectMixin,
    CancelSafeOpenMixin,
    PrimingPlaybackMixin,
    PlaybackGuardMixin,
    SeekWatchdogMixin,
    FFmpegProcessGuardMixin,
    DesktopReleaseWindow,
):
    """Single audited class used by the user-facing launcher."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("牛尾环九轴视频标注工作台 · 正式桌面版")
