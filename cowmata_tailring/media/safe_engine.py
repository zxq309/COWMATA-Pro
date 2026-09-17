from __future__ import annotations

import ctypes
import os
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths, Signal

from cowmata_tailring.media.dahua_duration import (
    DahuaDurationIndex,
    DahuaDurationProbeError,
    is_dahua_program_stream,
    load_duration_cache,
    probe_dahua_duration,
    probe_dahua_program_stream_duration,
    save_duration_cache,
)
from cowmata_tailring.media.dahua_stream import (
    DahuaCloseCallback,
    DahuaOpenCallback,
    DahuaPlaybackStream,
    DahuaReadCallback,
    DahuaSeekCallback,
)
from cowmata_tailring.media.engine import MediaEngine
from cowmata_tailring.media.ffmpeg_tools import FFmpegToolError, find_ffmpeg
from cowmata_tailring.media.seek_barrier import PlaybackSeekBarrier
from cowmata_tailring.media.timeline import (
    MediaTimelineIndex,
    TimelineProbeError,
    load_timeline_cache,
    probe_media_timeline,
    save_timeline_cache,
)

TIMELINE_DURATION_TOLERANCE_MS = 2_000
DAHUA_STREAM_RELEASE_GRACE_S = 0.25


def _continuous_packet_timeline_overrides_player(
    player_duration_ms: int | float,
    index: MediaTimelineIndex | None,
) -> bool:
    """Return whether a continuous packet clock must replace VLC's clock."""

    if index is None or index.is_corrected:
        return False
    player_duration = max(0, int(round(float(player_duration_ms))))
    packet_duration = max(0, int(round(float(index.duration_ms))))
    return bool(
        player_duration > 0
        and packet_duration
        > player_duration + TIMELINE_DURATION_TOLERANCE_MS
    )


def _effective_timeline_duration_ms(
    player_duration_ms: int | float,
    index: MediaTimelineIndex | None,
) -> int:
    """Choose the safe UI duration from VLC metadata and packet timing."""

    player_duration = max(0, int(round(float(player_duration_ms))))
    if index is None:
        return player_duration
    packet_duration = max(0, int(round(float(index.duration_ms))))
    if packet_duration <= 0:
        return player_duration
    if index.is_corrected:
        return packet_duration
    if player_duration <= 0:
        return packet_duration
    # Duration, clock scaling and seek mode must make the same decision.  A
    # small metadata rounding difference stays on VLC's clock; otherwise the
    # UI would advertise a final 1-2 seconds that no seek can ever reach.
    if _continuous_packet_timeline_overrides_player(player_duration, index):
        return packet_duration
    return player_duration


def _uses_fractional_timeline_seek(
    player_duration_ms: int | float,
    index: MediaTimelineIndex | None,
) -> bool:
    """Return whether millisecond seeks are bounded by bad VLC metadata."""

    return _continuous_packet_timeline_overrides_player(
        player_duration_ms,
        index,
    )


def _fractional_seek_clock(
    target_ms: int | float,
    player_duration_ms: int | float,
    index_duration_ms: int | float,
) -> tuple[float, float]:
    """Map a true timeline target to VLC position and logical offset."""

    packet_duration = max(1.0, float(index_duration_ms))
    player_duration = max(0.0, float(player_duration_ms))
    target = min(packet_duration, max(0.0, float(target_ms)))
    fraction = min(1.0, max(0.0, target / packet_duration))
    raw_anchor_ms = player_duration * fraction
    return fraction, target - raw_anchor_ms


def _ignored_tail_stop_target_ms(
    index: MediaTimelineIndex | None,
) -> int | None:
    """Return the last valid raw frame when a recorder tail was suppressed."""

    if index is None or not index.has_ignored_trailing_tail:
        return None
    return max(0, int(round(index.playable_last_frame_raw_ms)))


def _timeline_transport_blocked(
    validation_required: bool,
    index: MediaTimelineIndex | None,
) -> bool:
    """Keep unvalidated recorder timestamps out of UI transport controls."""

    return bool(validation_required and index is None)


class SafeMediaEngine(MediaEngine):
    """Compatibility and lifecycle fixes for the installed libVLC 3.x."""

    timeline_correction_changed = Signal(int, int, int)
    timeline_analysis_message = Signal(str, int)
    _timeline_probe_completed = Signal(int, str, object, str)
    _dahua_duration_probe_completed = Signal(int, str, object, str)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Choose avformat per file instead of forcing it for every container.
        kwargs["force_avformat"] = False
        super().__init__(*args, **kwargs)
        self._lib.libvlc_media_player_set_time.restype = None
        self._lib.libvlc_media_new_callbacks.argtypes = [
            ctypes.c_void_p,
            DahuaOpenCallback,
            DahuaReadCallback,
            DahuaSeekCallback,
            DahuaCloseCallback,
            ctypes.c_void_p,
        ]
        self._lib.libvlc_media_new_callbacks.restype = ctypes.c_void_p
        self._pending_seek_ms: int | None = None
        self._seek_barrier = PlaybackSeekBarrier()
        self._requested_rate = 1.0
        self._closing_async = False
        self._release_thread: threading.Thread | None = None
        self._timeline_index: MediaTimelineIndex | None = None
        self._dahua_duration_index: DahuaDurationIndex | None = None
        self._dahua_stream: DahuaPlaybackStream | None = None
        self._retired_dahua_streams: list[DahuaPlaybackStream] = []
        self._dahua_pause_when_ready = False
        self._timeline_segment_hint = 0
        self._timeline_probe_generation = 0
        self._timeline_probe_thread: threading.Thread | None = None
        self._timeline_probe_lock = threading.Lock()
        self._timeline_probe_pending = False
        self._timeline_validation_required = False
        self._timeline_validation_failed = False
        self._timeline_validation_error = ""
        self._normalized_seek_time_offset_ms = 0.0
        self._timeline_duration_overrides_player = False
        self._timeline_gap_active = False
        self._timeline_logical_end_reached = False
        self._pause_requested = False
        self._timeline_probe_completed.connect(
            self._finish_timeline_probe
        )
        self._dahua_duration_probe_completed.connect(
            self._finish_dahua_duration_probe
        )

    @staticmethod
    def _is_hikvision_program_stream(path: str) -> bool:
        try:
            with open(path, "rb") as source:
                header = source.read(12)
        except OSError:
            return False
        return header.startswith(b"\x00\x00\x01\xba")

    def open(self, path: str | os.PathLike[str]) -> bool:
        normalized = os.path.abspath(os.fspath(path))
        old_dahua_stream = self._dahua_stream
        if old_dahua_stream is not None:
            if not self._prepare_dahua_stream_release(old_dahua_stream):
                return self._fail(
                    "乐橙旧播放流未能安全结束，已取消切换以避免播放器崩溃"
                )
            self._retired_dahua_streams.append(old_dahua_stream)
            self._dahua_stream = None
        self._timeline_probe_generation += 1
        self._timeline_index = None
        self._dahua_duration_index = None
        self._timeline_segment_hint = 0
        self._timeline_probe_pending = False
        self._timeline_validation_required = False
        self._timeline_validation_failed = False
        self._timeline_validation_error = ""
        self._normalized_seek_time_offset_ms = 0.0
        self._timeline_duration_overrides_player = False
        self._timeline_gap_active = False
        self._timeline_logical_end_reached = False
        self._pause_requested = False
        self._dahua_pause_when_ready = False
        self._force_avformat = self._is_hikvision_program_stream(normalized)
        dahua_program_stream = is_dahua_program_stream(normalized)
        self._timeline_validation_required = bool(
            self._force_avformat and not dahua_program_stream
        )
        self._pending_seek_ms = None
        self._seek_barrier.reset()
        if self._player and self._media:
            if old_dahua_stream is None:
                self._prepare_standard_media_release()
            # Detach the old media without waiting synchronously for the
            # Hikvision decoder's occasionally slow stop path.
            self._lib.libvlc_media_player_set_pause(self._player, 1)
            self._lib.libvlc_media_player_set_media(self._player, None)
        opened = super().open(normalized)
        if opened and dahua_program_stream:
            self._start_dahua_duration_probe(normalized)
        elif opened and self._force_avformat:
            self._start_timeline_probe(normalized)
        return opened

    @staticmethod
    def _timeline_cache_directory() -> Path:
        root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.CacheLocation
        )
        if not root:
            root = str(Path.home() / ".bovine-motion-workbench" / "cache")
        return Path(root) / "video-timeline"

    @staticmethod
    def _dahua_duration_cache_directory() -> Path:
        root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.CacheLocation
        )
        if not root:
            root = str(Path.home() / ".bovine-motion-workbench" / "cache")
        return Path(root) / "dahua-duration"

    def _start_dahua_duration_probe(self, path: str) -> None:
        generation = self._timeline_probe_generation
        cache_directory = self._dahua_duration_cache_directory()
        try:
            cached = load_duration_cache(cache_directory, path)
        except OSError:
            cached = None
        if cached is not None:
            self._apply_dahua_duration_index(cached)
            return

        self._timeline_probe_pending = True
        self.timeline_analysis_message.emit("正在校验乐橙视频时长…", 0)

        def probe() -> None:
            index: DahuaDurationIndex | None = None
            error = ""
            try:
                with self._timeline_probe_lock:
                    if (
                        self._closed
                        or generation != self._timeline_probe_generation
                    ):
                        return
                    try:
                        _ffmpeg, ffprobe = find_ffmpeg()
                    except FFmpegToolError:
                        index = probe_dahua_program_stream_duration(path)
                    else:
                        index = probe_dahua_duration(path, ffprobe)
                    try:
                        save_duration_cache(cache_directory, index)
                    except (OSError, ValueError):
                        pass
            except (
                DahuaDurationProbeError,
                FFmpegToolError,
                OSError,
            ) as exc:
                error = str(exc)
            self._dahua_duration_probe_completed.emit(
                generation, path, index, error
            )

        self._timeline_probe_thread = threading.Thread(
            target=probe,
            name="dahua-duration-probe",
            daemon=True,
        )
        self._timeline_probe_thread.start()

    def _finish_dahua_duration_probe(
        self,
        generation: int,
        path: str,
        index: object,
        error: str,
    ) -> None:
        if (
            self._closed
            or generation != self._timeline_probe_generation
            or os.path.normcase(path) != os.path.normcase(self._path)
        ):
            return
        self._timeline_probe_pending = False
        if isinstance(index, DahuaDurationIndex):
            self._apply_dahua_duration_index(index)
            if index.basis.startswith("program_scan"):
                self.timeline_analysis_message.emit(
                    "FFprobe不可用，已使用内置MPEG-PS扫描校正乐橙视频",
                    6000,
                )
        elif error:
            self.timeline_analysis_message.emit(
                "乐橙视频时长校验失败，暂时使用播放器原始时长",
                6000,
            )

    def _apply_dahua_duration_index(self, index: DahuaDurationIndex) -> None:
        player_duration = super().duration_ms()
        previous_status = self.current_status
        resume_playback = bool(
            previous_status in {"opening", "buffering", "playing"}
            and not self._pause_requested
        )
        pending_target = self._pending_seek_ms
        current_target = max(0, int(MediaEngine.get_time_ms(self)))
        target = current_target if pending_target is None else pending_target
        self._dahua_duration_index = index
        self._timeline_index = None
        self._timeline_duration_overrides_player = False
        self._last_duration = -1
        if index.has_seek_index:
            if self._replace_dahua_stream(target):
                self._pending_seek_ms = None
                if resume_playback:
                    MediaEngine.play(self)
                elif previous_status == "paused" or pending_target is not None:
                    self._dahua_pause_when_ready = True
                    self._pause_requested = True
                    self._seek_barrier.request(target, playing=False)
                    MediaEngine.play(self)
        self.timeline_analysis_message.emit(
            f"乐橙视频时长和跳转索引已校正：{player_duration} ms → {index.duration_ms} ms",
            8000,
        )

    def _replace_dahua_stream(self, target_ms: int | float) -> bool:
        index = self._dahua_duration_index
        if index is None or not index.has_seek_index or not self._player:
            return False
        try:
            if index.basis == 'native_ps':
                from .native_stream import NativePlaybackStream
                stream = NativePlaybackStream(index, target_ms)
            else:
                stream = DahuaPlaybackStream(index, target_ms)
        except (OSError, ValueError) as exc:
            return self._fail(f"乐橙视频跳转流创建失败：{exc}")

        media = self._lib.libvlc_media_new_callbacks(
            self._instance,
            stream.open_callback,
            stream.read_callback,
            stream.seek_callback,
            stream.close_callback,
            None,
        )
        if not media:
            return self._fail(self._format_error("乐橙视频跳转流创建失败"))
        self._lib.libvlc_media_add_option(media, b":demux=avformat")
        self._lib.libvlc_media_add_option(media, b":file-caching=300")

        old_media = self._media
        old_stream = self._dahua_stream
        if old_stream is not None and not self._prepare_dahua_stream_release(
            old_stream
        ):
            self._lib.libvlc_media_release(media)
            stream.close()
            return self._fail(
                "乐橙旧播放流未能安全结束，已取消跳转以避免播放器崩溃"
            )
        if old_stream is None and old_media is not None:
            self._prepare_standard_media_release()
        self._lib.libvlc_media_player_set_pause(self._player, 1)
        self._lib.libvlc_media_player_set_media(self._player, None)
        if old_media:
            self._lib.libvlc_media_release(old_media)
        if old_stream is not None:
            self._retired_dahua_streams.append(old_stream)

        self._lib.libvlc_media_player_set_media(self._player, media)
        self._media = media
        self._dahua_stream = stream
        self._dahua_pause_when_ready = False
        self._last_error = ""
        self._reset_poll_cache()
        self._set_status("ready")
        return True

    def _prepare_dahua_stream_release(
        self,
        stream: DahuaPlaybackStream,
    ) -> bool:
        """Quiesce callback-backed VLC media before detaching it."""

        stream.close()
        if not self._player:
            return True
        result = self._lib.libvlc_media_player_play(self._player)
        if result != 0:
            return False
        # libVLC reads the short virtual segment ahead, so an input EOF cannot
        # prove that the decoder has left the ctypes callback media. Resuming
        # the closed stream and holding this measured quiescence window lets
        # the demux/decoder stack unwind before its media pointer is replaced.
        time.sleep(DAHUA_STREAM_RELEASE_GRACE_S)
        return True

    def _prepare_standard_media_release(self) -> None:
        """Let a regular VLC decoder settle before replacing its media."""

        if not self._player:
            return
        state_code = int(
            self._lib.libvlc_media_player_get_state(self._player)
        )
        if state_code not in {1, 2, 3, 4}:  # opening/buffering/playing/paused
            return
        self._lib.libvlc_media_player_set_pause(self._player, 1)
        time.sleep(DAHUA_STREAM_RELEASE_GRACE_S)

    def _start_dahua_seek(self, target_ms: int, *, resume: bool) -> bool:
        if not self._replace_dahua_stream(target_ms):
            return False
        self._pending_seek_ms = None
        self._dahua_pause_when_ready = not resume
        self._pause_requested = not resume
        self._seek_barrier.request(target_ms, playing=resume)
        if MediaEngine.play(self):
            return True
        self._dahua_pause_when_ready = False
        return False

    def _start_timeline_probe(self, path: str) -> None:
        generation = self._timeline_probe_generation
        cache_directory = self._timeline_cache_directory()
        try:
            cached = load_timeline_cache(cache_directory, path)
        except OSError:
            cached = None
        if cached is not None:
            self._apply_timeline_index(cached)
            return

        self._timeline_probe_pending = True
        self.timeline_analysis_message.emit("正在校验视频时间轴…", 0)

        def probe() -> None:
            index: MediaTimelineIndex | None = None
            error = ""
            try:
                with self._timeline_probe_lock:
                    if (
                        self._closed
                        or generation != self._timeline_probe_generation
                    ):
                        return
                    _ffmpeg, ffprobe = find_ffmpeg()
                    index = probe_media_timeline(path, ffprobe)
                    try:
                        save_timeline_cache(cache_directory, index)
                    except OSError:
                        pass
            except (FFmpegToolError, OSError, TimelineProbeError) as exc:
                error = str(exc)
            except Exception as exc:  # Thread boundary: never leave validation pending.
                error = f"{type(exc).__name__}: {exc}"
            self._timeline_probe_completed.emit(
                generation, path, index, error
            )

        self._timeline_probe_thread = threading.Thread(
            target=probe,
            name="video-timeline-probe",
            daemon=True,
        )
        self._timeline_probe_thread.start()

    def _finish_timeline_probe(
        self,
        generation: int,
        path: str,
        index: object,
        error: str,
    ) -> None:
        if (
            self._closed
            or generation != self._timeline_probe_generation
            or os.path.normcase(path) != os.path.normcase(self._path)
        ):
            return
        if isinstance(index, MediaTimelineIndex):
            self._timeline_probe_pending = False
            self._apply_timeline_index(index)
            if (
                not index.is_corrected
                and not self._timeline_duration_overrides_player
            ):
                self.timeline_analysis_message.emit("视频时间轴校验正常", 2500)
        else:
            self._reject_unvalidated_timeline(
                error or "时间轴扫描没有返回可用索引"
            )

    def _reject_unvalidated_timeline(self, error: str) -> None:
        """Freeze transport instead of exposing a known-untrusted VLC clock."""

        self._timeline_probe_pending = False
        self._timeline_validation_failed = True
        self._timeline_validation_error = error
        self._pending_seek_ms = None
        self._seek_barrier.reset()
        self._pause_requested = True
        if self._player:
            self._lib.libvlc_media_player_set_pause(self._player, 1)
        self._last_duration = -1
        self.timeline_analysis_message.emit(
            "海康视频时间轴校验失败，已禁用时长和跳转；"
            "请检查 FFprobe 或视频文件",
            0,
        )

    def _apply_timeline_index(self, index: MediaTimelineIndex) -> None:
        player_duration = super().duration_ms()
        self._timeline_index = index
        self._timeline_validation_failed = False
        self._timeline_validation_error = ""
        self._timeline_segment_hint = 0
        self._install_ignored_tail_stop_option(index)
        self._refresh_timeline_duration_mode(player_duration)
        if (
            not index.is_corrected
            and not self._timeline_duration_overrides_player
        ):
            return
        self._last_time = -1
        self._last_duration = -1
        if self._timeline_duration_overrides_player:
            packet_duration = int(round(index.duration_ms))
            self.timeline_analysis_message.emit(
                "播放器时长读取偏短，已按完整视频时间轴校正："
                f"{player_duration} ms → {packet_duration} ms",
                8000,
            )
            return
        raw_duration = int(round(index.raw_duration_ms))
        corrected_duration = int(round(index.duration_ms))
        self.timeline_analysis_message.emit(
            "检测到异常时间戳，已启用连续校正时间轴", 8000
        )
        self.timeline_correction_changed.emit(
            raw_duration,
            corrected_duration,
            len(index.discontinuities),
        )

    def _install_ignored_tail_stop_option(
        self, index: MediaTimelineIndex
    ) -> None:
        """Tell VLC to finish before an isolated post-gap recorder tail."""

        if not index.has_ignored_trailing_tail or not self._media:
            return
        stop_seconds = max(0.0, index.playable_raw_end_ms / 1000.0)
        option = f":stop-time={stop_seconds:.6f}".encode("ascii")
        self._lib.libvlc_media_add_option(self._media, option)

    def _refresh_timeline_duration_mode(
        self, player_duration_ms: int | float | None = None
    ) -> None:
        """Re-evaluate the packet/player duration relationship.

        VLC often reports length 0 while the media is opening. The packet
        probe can finish during that window, so deciding the seek/clock mode
        only once would permanently leave a short-player stream unscaled.
        """

        index = self._timeline_index
        if index is None:
            self._timeline_duration_overrides_player = False
            return
        player_duration = (
            super().duration_ms()
            if player_duration_ms is None
            else player_duration_ms
        )
        if float(player_duration) <= 0.0:
            # Keep a previously selected mode through transient zero metadata
            # while VLC is reopening or refreshing the stream.
            return
        self._timeline_duration_overrides_player = (
            _uses_fractional_timeline_seek(player_duration, index)
        )

    def _public_to_raw_time_ms(self, value_ms: int | float) -> int:
        index = self._timeline_index
        if index is None or not index.is_corrected:
            return max(0, int(round(value_ms)))
        raw, segment = index.public_to_raw_ms(float(value_ms))
        self._timeline_segment_hint = segment
        return max(0, int(round(raw)))

    def _raw_to_public_time_ms(self, value_ms: int | float) -> int:
        index = self._timeline_index
        self._refresh_timeline_duration_mode()
        if index is None or not index.is_corrected:
            self._timeline_gap_active = False
            if self._timeline_duration_overrides_player:
                scale = self._timeline_public_clock_scale()
                if scale > 0.0:
                    logical = float(value_ms) * scale
                    if index is not None:
                        logical = min(logical, float(index.duration_ms))
                    return max(0, int(round(logical)))
            return max(
                0,
                int(
                    round(
                        float(value_ms)
                        + self._normalized_seek_time_offset_ms
                    )
                ),
            )
        self._timeline_gap_active = index.raw_gap_active(float(value_ms))
        public, segment = index.raw_to_public_ms(
            float(value_ms), self._timeline_segment_hint
        )
        self._timeline_segment_hint = segment
        return max(0, int(round(public)))

    def _timeline_public_clock_scale(self) -> float:
        """Map VLC's short container clock to the packet-time clock.

        Some Hikvision files contain the complete packet timeline but expose
        a much shorter libVLC length. Seeking was already expressed as a
        fraction of the packet timeline; playback samples must use that same
        fraction continuously or the UI clock drifts after every seek.
        """

        index = self._timeline_index
        self._refresh_timeline_duration_mode()
        if index is None or index.is_corrected:
            return 1.0
        player_duration = max(0.0, float(self.player_duration_ms()))
        packet_duration = max(0.0, float(index.duration_ms))
        if not self._timeline_duration_overrides_player:
            return 1.0
        if player_duration <= 0.0 or packet_duration <= 0.0:
            return 1.0
        return packet_duration / player_duration

    def public_clock_speed_ms_per_s(self) -> float:
        """Return the public timeline speed used by every UI consumer.

        libVLC advances its own clock at ``1000 * rate`` milliseconds per
        second.  When a continuous packet timeline is longer than VLC's
        container clock, public time uses the same duration ratio as seeking.
        Exposing the combined speed prevents the smooth playhead from running
        on a second, slower clock.
        """

        rate = max(0.05, float(self.get_rate()))
        if self._dahua_stream is not None:
            return 1000.0 * rate
        return 1000.0 * rate * self._timeline_public_clock_scale()

    @property
    def timeline_gap_active(self) -> bool:
        """Whether the decoder currently sits inside a removed PTS gap."""

        return bool(self._timeline_gap_active)

    def get_time_ms(self) -> int:
        if self._dahua_stream is not None:
            raw_time_ms = MediaEngine.get_time_ms(self)
            return self._dahua_stream.public_time_ms(raw_time_ms)
        if (
            self._timeline_logical_end_reached
            and self._timeline_index is not None
        ):
            return max(0, int(round(self._timeline_index.duration_ms)))
        raw_time_ms = super().get_time_ms()
        return self._raw_to_public_time_ms(raw_time_ms)

    def duration_ms(self) -> int:
        if self._dahua_duration_index is not None:
            return max(0, int(self._dahua_duration_index.duration_ms))
        if _timeline_transport_blocked(
            self._timeline_validation_required,
            self._timeline_index,
        ):
            return 0
        player_duration = super().duration_ms()
        self._refresh_timeline_duration_mode(player_duration)
        return _effective_timeline_duration_ms(
            player_duration,
            self._timeline_index,
        )

    def is_seekable(self) -> bool:
        if _timeline_transport_blocked(
            self._timeline_validation_required,
            self._timeline_index,
        ):
            return False
        if (
            self._dahua_duration_index is not None
            and self._dahua_duration_index.has_seek_index
        ):
            return True
        return super().is_seekable()

    def defers_timeline_seek_while_dragging(self) -> bool:
        """Return whether a seek should be committed only on mouse release."""

        return bool(
            self._dahua_duration_index is not None
            and self._dahua_duration_index.has_seek_index
        )

    def player_duration_ms(self) -> int:
        """Return VLC's container duration before packet-time correction."""

        return super().duration_ms()

    def _apply_decoder_seek(self, target_ms: int) -> None:
        if (
            self._dahua_duration_index is not None
            and self._dahua_duration_index.has_seek_index
        ):
            self._start_dahua_seek(
                target_ms,
                resume=bool(self.is_playing() and not self._pause_requested),
            )
            return
        index = self._timeline_index
        player_duration = super().duration_ms()
        self._refresh_timeline_duration_mode(player_duration)
        if _uses_fractional_timeline_seek(player_duration, index):
            assert index is not None
            fraction, _offset_ms = _fractional_seek_clock(
                target_ms,
                player_duration,
                index.duration_ms,
            )
            # The public clock is scaled continuously in
            # ``_raw_to_public_time_ms``. The old additive offset corrected a
            # single seek but made playback drift at 1x afterwards.
            self._normalized_seek_time_offset_ms = 0.0
            self._lib.libvlc_media_player_set_position(
                self._player,
                ctypes.c_float(fraction),
            )
            return

        self._normalized_seek_time_offset_ms = 0.0
        raw_target = self._public_to_raw_time_ms(target_ms)
        self._lib.libvlc_media_player_set_time(self._player, raw_target)

    def stop(self) -> None:
        if self._closed or not self._player:
            return
        # A paused decoder is enough for normal UI operation. Full stop and
        # release happen asynchronously during close.
        self._lib.libvlc_media_player_set_pause(self._player, 1)
        self.poll()

    def pause(self, paused: bool = True) -> None:
        # libVLC changes into the paused state asynchronously.  Remember the
        # requested state so a seek issued immediately after Pause is queued
        # until that transition is real; otherwise the late pause completion
        # can put the decoder back on the frame from before the seek.
        self._pause_requested = bool(paused)
        super().pause(paused)

    def set_time_ms(self, value: int | float) -> bool:
        self._ensure_media()
        if self._timeline_validation_failed:
            return False
        requested_target = max(0, int(round(value)))
        probe_pending = bool(
            self._timeline_probe_pending and self._timeline_index is None
        )
        target = requested_target
        duration = self.duration_ms()
        if duration > 0 and not probe_pending:
            target = min(target, duration)
        playing = self.is_playing()
        pause_pending = bool(self._pause_requested)
        if (
            self._dahua_duration_index is not None
            and self._dahua_duration_index.has_seek_index
        ):
            return self._start_dahua_seek(
                target,
                resume=bool(playing and not pause_pending),
            )
        self._seek_barrier.request(
            target,
            playing=bool(playing and not pause_pending),
            now=time.monotonic(),
        )
        if (
            pause_pending
            or probe_pending
            or not self.is_seekable()
            or self.current_status not in {"playing", "paused"}
        ):
            self._pending_seek_ms = target
            return True
        self._apply_decoder_seek(target)
        self._pending_seek_ms = None
        return True

    def clear_pending_seek(self) -> None:
        """Discard a seek queued for an earlier media state."""

        self._pending_seek_ms = None
        self._seek_barrier.reset()

    def seek_target_is_ready(self, target_ms: int | float) -> bool:
        return self._seek_barrier.target_is_ready(float(target_ms))

    def seek_confirmation_pending(self) -> bool:
        return self._seek_barrier.playback_confirmation_pending()

    def seek_confirmation_serial(self) -> int:
        return int(self._seek_barrier.confirmation_serial)

    def cancel_seek_confirmation(self) -> None:
        self._pending_seek_ms = None
        self._seek_barrier.reset()

    def replay_from_ms(self, value: int | float = 0) -> bool:
        """Reopen an ended stream and start it from ``value`` milliseconds."""

        if (
            self._dahua_duration_index is not None
            and self._dahua_duration_index.has_seek_index
        ):
            target = max(0, int(round(value)))
            target = min(
                target,
                max(0, int(self._dahua_duration_index.duration_ms) - 1),
            )
            return self._start_dahua_seek(target, resume=True)
        path = self._path
        if not path:
            return self._fail("No media is available for replay")
        target = max(0, int(round(value)))
        duration = self.duration_ms()
        if duration > 0:
            target = min(target, max(0, duration - 1))
        if not self.open(path):
            return False
        # The reopened stream is initially ready/opening, so the normal poll
        # loop applies this target as soon as VLC becomes seekable again.
        self._pending_seek_ms = target
        barrier = getattr(self, "_seek_barrier", None)
        if barrier is not None:
            barrier.request(target, playing=False)
        return self.play()

    def play(self) -> bool:
        if self._timeline_validation_failed:
            return False
        self._pause_requested = False
        if (
            self._dahua_duration_index is not None
            and self._dahua_duration_index.has_seek_index
        ):
            self._dahua_pause_when_ready = False
            self._seek_barrier.playback_started()
            if self._dahua_stream is None:
                if not self._replace_dahua_stream(self.get_time_ms()):
                    return False
            return MediaEngine.play(self)
        target = self._seek_barrier.playback_started()
        if target is not None:
            # Apply once after VLC enters a genuinely seekable playback state.
            # This replaces the old decoder timestamp before it can reach UI.
            self._pending_seek_ms = int(round(target))
        return super().play()

    def set_rate(self, rate: float) -> bool:
        self._requested_rate = float(rate)
        return super().set_rate(rate)

    def _apply_pending_seek_if_ready(
        self,
        *,
        state_code: int,
        seekable: bool,
    ) -> bool:
        if (
            self._pending_seek_ms is None
            or self._timeline_probe_pending
            or _timeline_transport_blocked(
                self._timeline_validation_required,
                self._timeline_index,
            )
            or not seekable
            or state_code not in {3, 4}  # playing / paused
            or (self._pause_requested and state_code != 4)
        ):
            return False

        target = self._pending_seek_ms
        duration = self.duration_ms()
        if duration > 0:
            target = min(target, duration)
        if target != self._pending_seek_ms:
            self._seek_barrier.request(
                target,
                playing=state_code == 3,
                now=time.monotonic(),
            )
        self._pending_seek_ms = None
        if state_code == 4:
            self._pause_requested = False
        self._apply_decoder_seek(target)
        return True

    def poll(self) -> dict[str, Any]:
        if self._timeline_logical_end_reached:
            return self._ignored_tail_end_snapshot()

        state_code = (
            int(self._lib.libvlc_media_player_get_state(self._player))
            if self._player
            else 0
        )
        if (
            state_code == 6  # ended
            and self._dahua_stream is not None
            and self._dahua_duration_index is not None
            and self._dahua_stream.segment_end_ms
            < float(self._dahua_duration_index.duration_ms) - 1.0
        ):
            next_target = int(round(self._dahua_stream.segment_end_ms))
            self._start_dahua_seek(next_target, resume=True)
            state_code = int(
                self._lib.libvlc_media_player_get_state(self._player)
            )
        if (
            self._dahua_pause_when_ready
            and self._dahua_stream is not None
            and state_code == 3  # playing
        ):
            raw_time_ms = MediaEngine.get_time_ms(self)
            if raw_time_ms >= self._dahua_stream.ready_raw_time_ms:
                self._lib.libvlc_media_player_set_pause(self._player, 1)
                self._dahua_pause_when_ready = False
        if (
            self._pending_seek_ms is not None
            and self._player
            and not self._timeline_probe_pending
        ):
            seekable = bool(
                self._lib.libvlc_media_player_is_seekable(self._player)
            )
            self._apply_pending_seek_if_ready(
                state_code=state_code,
                seekable=seekable,
            )
        if self._ignored_tail_playback_end_due(state_code):
            return self._finish_ignored_tail_playback()
        snapshot = super().poll()
        if (
            snapshot.get("status") == "ended"
            and self._timeline_index is not None
            and self._timeline_index.has_ignored_trailing_tail
        ):
            self._latch_observed_ignored_tail_end(snapshot)
        if snapshot.get("status") in {
            "paused",
            "stopped",
            "ended",
            "error",
        }:
            self._pause_requested = False
        # MediaEngine only calls _accept_time_update when the integer VLC
        # timestamp changes.  Observe every poll as well: this lets a frame
        # held steadily at the requested target finish its short decoder-
        # settle period, and still guarantees that a frozen stale clock times
        # out instead of blocking both UI timelines forever.
        before_serial = self._seek_barrier.confirmation_serial
        accepted = self._seek_barrier.observe(
            float(snapshot.get("time_ms", 0)),
            playing=bool(snapshot.get("playing", False)),
            rate=float(snapshot.get("rate", 1.0)),
            now=time.monotonic(),
        )
        if (
            accepted
            and self._seek_barrier.confirmation_serial > before_serial
        ):
            confirmed_time = int(round(float(snapshot.get("time_ms", 0))))
            if confirmed_time != self._last_time:
                self._last_time = confirmed_time
            self.time_changed.emit(confirmed_time)
        if snapshot.get("status") == "playing":
            current = float(snapshot.get("rate", 1.0))
            if abs(current - self._requested_rate) > 1e-3:
                super().set_rate(self._requested_rate)
        if self._retired_dahua_streams:
            self._retired_dahua_streams = [
                stream
                for stream in self._retired_dahua_streams
                if not stream.is_closed
            ]
        return snapshot

    def _ignored_tail_playback_end_due(self, state_code: int) -> bool:
        """Return whether playback reached the main recording's last frame."""

        target = _ignored_tail_stop_target_ms(self._timeline_index)
        if (
            target is None
            or state_code != 3  # playing
            or self._timeline_probe_pending
            or self._pending_seek_ms is not None
            or self._seek_barrier.playback_confirmation_pending()
        ):
            return False
        raw_time_ms = max(0, int(MediaEngine.get_time_ms(self)))
        return raw_time_ms >= target

    def _finish_ignored_tail_playback(self) -> dict[str, Any]:
        """Pause on the last real frame and publish one logical end event."""

        index = self._timeline_index
        target = _ignored_tail_stop_target_ms(index)
        if index is None or target is None:
            return super().poll()

        self._timeline_logical_end_reached = True
        self._timeline_gap_active = False
        self._pending_seek_ms = None
        self._pause_requested = False
        self._seek_barrier.reset()

        # Seeking first avoids the late-pause race that can restore an older
        # decoder frame.  It also returns to the final main-recording frame if
        # a high playback rate skipped directly into the orphan tail.
        self._lib.libvlc_media_player_set_time(self._player, target)
        self._lib.libvlc_media_player_set_pause(self._player, 1)

        endpoint = max(0, int(round(index.duration_ms)))
        if endpoint != self._last_time:
            self._last_time = endpoint
            self.time_changed.emit(endpoint)
        if endpoint != self._last_duration:
            self._last_duration = endpoint
            self.duration_changed.emit(endpoint)
        if self._last_playing:
            self._last_playing = False
            self.playing_changed.emit(False)
        if self._last_status != "ended":
            self._last_status = "ended"
            self.status_changed.emit("ended")
            self.ended.emit()
        return self._ignored_tail_end_snapshot()

    def _latch_observed_ignored_tail_end(
        self, snapshot: dict[str, Any]
    ) -> None:
        """Stabilize clocks when VLC honored the per-media stop option."""

        index = self._timeline_index
        if index is None:
            return
        self._timeline_logical_end_reached = True
        self._timeline_gap_active = False
        endpoint = max(0, int(round(index.duration_ms)))
        if endpoint != self._last_time:
            self._last_time = endpoint
            self.time_changed.emit(endpoint)
        snapshot["status"] = "ended"
        snapshot["time_ms"] = endpoint
        snapshot["duration_ms"] = endpoint
        snapshot["playing"] = False

    def _ignored_tail_end_snapshot(self) -> dict[str, Any]:
        index = self._timeline_index
        endpoint = (
            max(0, int(round(index.duration_ms)))
            if index is not None
            else max(0, int(self._last_time))
        )
        return {
            "status": "ended",
            "time_ms": endpoint,
            "duration_ms": endpoint,
            "playing": False,
            "seekable": bool(self._last_seekable),
            "rate": self.get_rate(),
            "muted": self.is_muted(),
            "volume": self.volume(),
            "error": self._last_error,
            "path": self._path,
        }

    def _accept_time_update(
        self, current_time_ms: int, playing: bool
    ) -> bool:
        return self._seek_barrier.observe(
            float(current_time_ms),
            playing=bool(playing),
            rate=self.get_rate(),
            now=time.monotonic(),
        )

    def close(self) -> None:
        if self._closed or self._closing_async:
            return
        if self._dahua_stream is not None:
            self._prepare_dahua_stream_release(self._dahua_stream)
        elif self._media is not None:
            self._prepare_standard_media_release()
        self._closing_async = True
        self._poll_timer.stop()
        if self._player:
            self._lib.libvlc_media_player_set_hwnd(
                self._player, ctypes.c_void_p(0)
            )

        lib = self._lib
        player = self._player
        media = self._media
        instance = self._instance
        dll_handle = self._dll_directory_handle
        dahua_streams = tuple(
            stream
            for stream in [
                self._dahua_stream,
                *self._retired_dahua_streams,
            ]
            if stream is not None
        )
        self._player = None
        self._media = None
        self._instance = None
        self._dll_directory_handle = None
        self._dahua_stream = None
        self._retired_dahua_streams = []
        self._closed = True

        def release() -> None:
            try:
                if player:
                    lib.libvlc_media_player_stop(player)
                    lib.libvlc_media_player_set_media(player, None)
                if media:
                    lib.libvlc_media_release(media)
                if player:
                    lib.libvlc_media_player_release(player)
                if instance:
                    lib.libvlc_release(instance)
            finally:
                for stream in dahua_streams:
                    stream.close()
                if dll_handle is not None:
                    try:
                        dll_handle.close()
                    except Exception:
                        pass

        self._release_thread = threading.Thread(
            target=release,
            name="libvlc-release",
            daemon=True,
        )
        self._release_thread.start()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
