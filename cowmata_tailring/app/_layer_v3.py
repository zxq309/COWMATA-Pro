from __future__ import annotations

from cowmata_tailring.annotation.mixins.transactional_project import TransactionalProjectMixin
from cowmata_tailring.app._layer_release import MainWindow as DesktopReleaseWindow
from cowmata_tailring.app.mixins.cancel_safe_open import CancelSafeOpenMixin
from cowmata_tailring.app.mixins.timer_shutdown import TimerShutdownMixin
from cowmata_tailring.media.mixins.ffmpeg_process_guard import FFmpegProcessGuardMixin
from cowmata_tailring.media.mixins.playback_guard import PlaybackGuardMixin
from cowmata_tailring.media.mixins.priming_playback import PrimingPlaybackMixin
from cowmata_tailring.media.mixins.seek_watchdog import SeekWatchdogMixin
from cowmata_tailring.media.mixins.strict_seek import StrictSeekMixin
from cowmata_tailring.ui.i18n import t


class MainWindow(
    TimerShutdownMixin,
    TransactionalProjectMixin,
    CancelSafeOpenMixin,
    PrimingPlaybackMixin,
    PlaybackGuardMixin,
    SeekWatchdogMixin,
    StrictSeekMixin,
    FFmpegProcessGuardMixin,
    DesktopReleaseWindow,
):
    """User-facing native desktop window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(t("牛尾环九轴视频标注工作台 · 正式桌面版"))
