from __future__ import annotations

from cowmata_tailring.ui.i18n import t


class PlaybackGuardMixin:
    """Never start playback when video and sensor data do not overlap."""

    def toggle_play(self) -> None:
        if (
            self.media is not None
            and self.video_path
            and self.data is not None
        ):
            if self.video_start_wall_ms is None:
                self.media.pause(True)
                self.play_btn.setText(t("▶ 播放"))
                self.video_status.setText(t("请先完成视频与九轴时间对齐"))
                return
            limits = self._shared_video_limits()
            if limits is None or limits[1] - limits[0] < 1.0:
                self.media.pause(True)
                self.play_btn.setText(t("▶ 播放"))
                self.video_status.setText(t("数据与视频没有共同覆盖时间，已阻止播放"))
                return
        super().toggle_play()
