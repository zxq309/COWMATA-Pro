from __future__ import annotations

import os
from pathlib import Path

from cowmata_tailring.ui.helpers import format_relative, format_wall
from cowmata_tailring.ui.i18n import t


class EndedReplayMixin:
    """Recover a finished VLC stream before seeking or replaying it."""

    def __init__(self) -> None:
        self._media_reached_end = False
        self._ended_replay_target_ms: float | None = None
        super().__init__()

    def _media_is_ended(self) -> bool:
        status = (
            getattr(self.media, "current_status", "")
            if self.media is not None
            else ""
        )
        return bool(self._media_reached_end or status == "ended")

    def _clear_media_pending_seek(self) -> None:
        if self.media is None:
            return
        clear = getattr(self.media, "clear_pending_seek", None)
        if callable(clear):
            clear()

    def open_video(self, path: str | None = None) -> None:
        super().open_video(path)
        if self.media is None or not self.video_path:
            return
        current = os.path.normcase(
            os.path.abspath(self.media.current_path or "")
        )
        expected = os.path.normcase(os.path.abspath(self.video_path))
        if current == expected:
            self._media_reached_end = False
            self._ended_replay_target_ms = None

    def _on_media_ended(self) -> None:
        super()._on_media_ended()
        self._media_reached_end = True
        self._ended_replay_target_ms = None
        self._clear_media_pending_seek()
        reset_pair = getattr(self, "_reset_pending_pair", None)
        if callable(reset_pair):
            reset_pair(cancel_queued_seek=True)
        self.play_btn.setText(t("▶ 播放"))
        if self.video_path:
            self.video_status.setText(
                f"{Path(self.video_path).name} · 播放结束，再按播放从头重播"
            )

    def _select_ended_replay_target(self, video_ms: float) -> None:
        if self.media is None:
            return
        duration = float(self.media.duration_ms())
        upper = duration if duration > 0 else max(0.0, float(video_ms))
        target = min(max(0.0, float(video_ms)), upper)
        self._ended_replay_target_ms = target
        self.video_timeline.set_position(target)
        self.video_clock_label.setText("视频 " + format_relative(target))

        if self.video_start_wall_ms is not None:
            wall = self.video_start_wall_ms + target
            self.wall_clock_label.setText("画面时间 " + format_wall(wall))
            linked = getattr(self, "_timelines_are_linked", None)
            may_sync_data = not callable(linked) or bool(linked())
            if self.data is not None and may_sync_data:
                data_ms = wall - self.data_create_time_ms
                if 0.0 <= data_ms <= self.data_duration_ms:
                    self._set_playhead_visual(data_ms)

        self.play_btn.setText(t("▶ 播放"))
        self.video_status.setText(
            f"{Path(self.video_path).name} · 已选择重播位置，按播放继续"
        )

    def _queue_video_timeline_seek(self, video_ms: float) -> None:
        if self._media_is_ended():
            self._select_ended_replay_target(video_ms)
            return
        super()._queue_video_timeline_seek(video_ms)

    def _seek_from_video_timeline(self, video_ms: float) -> None:
        if self._media_is_ended():
            self._select_ended_replay_target(video_ms)
            return
        super()._seek_from_video_timeline(video_ms)

    def _restart_ended_media(self) -> None:
        if self.media is None or not self.video_path:
            return
        duration = float(self.media.duration_ms())
        target = self._ended_replay_target_ms
        if target is None or (duration > 0 and target >= duration - 1.0):
            target = 0.0

        # Discard both alignment-level and engine-level waits from the old
        # media generation before reopening the same Hikvision stream.
        reset_pair = getattr(self, "_reset_pending_pair", None)
        if callable(reset_pair):
            reset_pair(cancel_queued_seek=True)
        self._clear_media_pending_seek()
        self._select_ended_replay_target(target)

        replay = getattr(self.media, "replay_from_ms", None)
        if not callable(replay) or not replay(target):
            self._media_reached_end = True
            self.play_btn.setText(t("▶ 播放"))
            self.video_status.setText(
                f"{Path(self.video_path).name} · 重播启动失败"
            )
            return

        self._media_reached_end = False
        self._ended_replay_target_ms = None
        self._apply_rate()
        self.media.set_volume(self.volume_slider.value())
        self.media.mute(self.mute_check.isChecked())
        self.play_btn.setText(t("⏳ 正在重播"))
        self.video_status.setText(
            f"{Path(self.video_path).name} · 正在从 "
            f"{format_relative(target)} 重播"
        )

    def toggle_play(self) -> None:
        if self._media_is_ended():
            self._restart_ended_media()
            return
        super().toggle_play()
