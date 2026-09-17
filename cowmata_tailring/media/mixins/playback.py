from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog

from cowmata_tailring.ui.helpers import BEIJING, format_relative, format_wall, parse_video_filename
from cowmata_tailring.ui.i18n import t


class PlaybackMixin:
    """Native libVLC playback and wall-clock synchronization handlers."""

    def open_video(self, path: str | None = None) -> None:
        if self.media is None:
            self._show_error("原生播放器不可用，请检查 VLC 安装。")
            return
        if path is None or isinstance(path, bool):
            start = self.settings.value(
                "last_video_dir",
                "G:\\" if Path("G:\\").exists() else str(Path.cwd()),
            )
            path, _ = QFileDialog.getOpenFileName(
                self,
                "打开视频或录像块",
                str(start),
                "视频文件 (*.mp4 *.mkv *.avi *.mov *.ts *.m2ts *.ps *.mpeg "
                "*.h265 *.hevc);;所有文件 (*.*)",
            )
        if not path:
            return
        path = os.path.abspath(path)
        if not self.media.open(path):
            self._show_error(self.media.last_error or "视频无法打开。")
            return
        self.video_path = path
        self.settings.setValue("last_video_dir", str(Path(path).parent))
        self.video_status.setText(f"正在读取 {Path(path).name}")
        self._autopause_on_load = True
        self.media.set_volume(self.volume_slider.value())
        self.media.mute(self.mute_check.isChecked())
        self.media.play()
        self._apply_rate()

        parsed = parse_video_filename(path, self.data_create_time_ms or None)
        if parsed is not None:
            self.video_start_wall_ms = parsed
            self.align_method = "filename"
        elif self.data is not None and not self._restoring_project:
            self.video_start_wall_ms = self.data_create_time_ms
            self.align_method = "default"
        self._update_alignment_status()
        self._refresh_enabled()

    def _on_media_status(self, status: str) -> None:
        labels = {
            "ready": "已打开",
            "opening": "正在打开",
            "buffering": "缓冲",
            "playing": "播放中",
            "paused": "已暂停",
            "stopped": "已停止",
            "ended": "播放结束",
            "error": "播放错误",
        }
        name = Path(self.video_path).name if self.video_path else ""
        text = labels.get(status, status)
        self.video_status.setText(f"{name} · {text}" if name else text)

    def _on_media_duration(self, duration_ms: int) -> None:
        if duration_ms <= 0:
            return
        if self._autopause_on_load and self.media is not None:
            self._autopause_on_load = False
            self._schedule_load_autopause()
        self._refresh_enabled()

    def _schedule_load_autopause(self) -> None:
        """Pause ordinary players shortly after their duration becomes ready."""

        QTimer.singleShot(80, lambda: self.media and self.media.pause(True))

    def _on_media_time(self, video_time_ms: int) -> None:
        self.video_clock_label.setText(
            f"视频 {format_relative(video_time_ms)}"
        )
        if self.video_start_wall_ms is None:
            return
        wall = self.video_start_wall_ms + video_time_ms
        self.wall_clock_label.setText(f"画面时间 {format_wall(wall)}")
        if self.data is not None:
            self._set_playhead_visual(wall - self.data_create_time_ms)

    def _on_playing_changed(self, playing: bool) -> None:
        self.play_btn.setText("⏸ 暂停" if playing else "▶ 播放")

    def _on_media_ended(self) -> None:
        self.play_btn.setText(t("▶ 播放"))

    def toggle_play(self) -> None:
        if self.media is None or not self.video_path:
            return
        if self.media.is_playing():
            prepare_pause = getattr(
                self,
                "_prepare_playhead_for_user_pause",
                None,
            )
            if callable(prepare_pause):
                prepare_pause()
            else:
                refresh_playhead = getattr(
                    self,
                    "_refresh_playhead_for_user_action",
                    None,
                )
                if callable(refresh_playhead):
                    refresh_playhead()
            self.media.pause(True)
            return
        coverage = self._video_coverage()
        if coverage is not None and not (
            coverage[0] <= self.playhead_ms < coverage[1]
        ):
            self.set_playhead(coverage[0])
        # Pausing does not change the decoder position.  Seeking here used to
        # arm the long-GOP confirmation watchdog on every ordinary resume;
        # the watchdog then pulled the decoder back to the same timestamp
        # every 400 ms.  Real position changes already seek at their source
        # (timeline drag, alignment, source continuation or ended replay).
        self._apply_rate()
        self.media.play()

    def previous_frame(self) -> None:
        if self.media is None or not self.video_path:
            return
        self.media.pause(True)
        self.media.set_time_ms(max(0, self.media.get_time_ms() - 100))
        QTimer.singleShot(40, self.media.frame_step)

    def next_frame(self) -> None:
        if self.media is None or not self.video_path:
            return
        self.media.pause(True)
        self.media.frame_step()

    def _apply_rate(self, _index: int | None = None) -> None:
        if self.media is not None and self.video_path:
            self.media.set_rate(float(self.rate_combo.currentData() or 1.0))

    def _apply_mute(self, checked: bool) -> None:
        if self.media is not None:
            self.media.mute(checked)

    def _apply_volume(self, value: int) -> None:
        if self.media is not None:
            self.media.set_volume(value)

    def set_playhead(self, value_ms: float, seek_video: bool = True) -> None:
        upper = self.data_duration_ms
        if upper > 0:
            value_ms = float(np.clip(value_ms, 0.0, upper))
        else:
            value_ms = max(0.0, float(value_ms))
        self._set_playhead_visual(value_ms)
        if seek_video:
            self._seek_video_to_playhead()

    def _set_playhead_visual(self, value_ms: float) -> None:
        upper = self.data_duration_ms
        if upper > 0:
            value_ms = float(np.clip(value_ms, 0.0, upper))
        self.playhead_ms = value_ms
        self.playhead_label.setText(format_relative(value_ms))
        self.plot.set_playhead(value_ms)
        self.plot.center_on(value_ms)

    def _seek_video_to_playhead(self) -> None:
        if (
            self.media is None
            or not self.video_path
            or self.data is None
            or self.video_start_wall_ms is None
        ):
            return
        target = (
            self.data_create_time_ms
            + self.playhead_ms
            - self.video_start_wall_ms
        )
        duration = self.media.duration_ms()
        if 0 <= target <= duration:
            self.media.set_time_ms(target)

    def _video_coverage(self) -> tuple[float, float] | None:
        if (
            self.media is None
            or self.data is None
            or self.video_start_wall_ms is None
        ):
            return None
        offset = self.video_start_wall_ms - self.data_create_time_ms
        start = max(0.0, float(offset))
        end = min(
            self.data_duration_ms,
            float(offset + self.media.duration_ms()),
        )
        return (start, end) if end > start else None

    def align_from_filename(self, silent: bool = False) -> None:
        if not self.video_path or self.data is None:
            if not silent:
                self._show_error("请先打开九轴 JSON 和视频。")
            return
        value = parse_video_filename(
            self.video_path, self.data_create_time_ms
        )
        if value is None:
            if not silent:
                self._show_error(
                    "视频文件名不含日期时间。海康 hiv 文件请使用“钉住”"
                    "或输入画面角标时间。"
                )
            return
        self.video_start_wall_ms = value
        self.align_method = "filename"
        self._update_alignment_status()
        self._seek_video_to_playhead()

    def pin_alignment(self) -> None:
        if self.media is None or self.data is None or not self.video_path:
            return
        self.video_start_wall_ms = int(
            self.data_create_time_ms
            + self.playhead_ms
            - self.media.get_time_ms()
        )
        self.align_method = "pin"
        self._update_alignment_status()

    def align_from_corner(self) -> None:
        if self.media is None or self.data is None or not self.video_path:
            return
        match = re.fullmatch(
            r"(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?",
            self.corner_time_edit.text().strip(),
        )
        if not match:
            self._show_error("角标时间格式应为 HH:MM:SS.mmm。")
            return
        hour, minute, second = map(int, match.groups()[:3])
        milliseconds = int((match.group(4) or "0").ljust(3, "0"))
        base = datetime.fromtimestamp(
            self.data_create_time_ms / 1000.0, BEIJING
        )
        corner = base.replace(
            hour=hour,
            minute=minute,
            second=second,
            microsecond=milliseconds * 1000,
        )
        self.video_start_wall_ms = (
            int(corner.timestamp() * 1000) - self.media.get_time_ms()
        )
        self.align_method = "corner"
        self._update_alignment_status()

    def nudge_alignment(self, delta_ms: float) -> None:
        if self.video_start_wall_ms is None:
            return
        # The button sign describes the video's movement on screen.  Because
        # video_ms = data_wall_ms - video_start_wall_ms, moving the video
        # forward requires decreasing its wall-clock origin.
        self.video_start_wall_ms -= int(delta_ms)
        self.align_method = "manual"
        self._update_alignment_status()
        self._seek_video_to_playhead()

    def _update_alignment_status(self) -> None:
        if self.video_start_wall_ms is None:
            self.align_status.setText(t("尚未对齐"))
            self.align_status.setStyleSheet("color:#b42318;")
            return
        labels = {
            "filename": "文件名",
            "pin": "人工钉住",
            "corner": "画面角标",
            "manual": "人工微调",
            "continuation": "分段续接",
            "segment_clock": "乐橙分段时钟",
            "data_continuation": "JSON 续接",
            "default": "零偏移待校准",
        }
        self.align_status.setText(
            f"{labels.get(self.align_method, self.align_method)} · "
            f"视频0秒={format_wall(self.video_start_wall_ms)}"
        )
        color = "#b42318" if self.align_method == "default" else "#18794e"
        self.align_status.setStyleSheet(f"color:{color};")
