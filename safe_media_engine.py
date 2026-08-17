from __future__ import annotations

import ctypes
import os
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths, Signal

from ffmpeg_tools import FFmpegToolError, find_ffmpeg
from media_engine import MediaEngine
from media_timeline import (
    MediaTimelineIndex,
    TimelineProbeError,
    load_timeline_cache,
    probe_media_timeline,
    save_timeline_cache,
)
from seek_barrier import PlaybackSeekBarrier


TIMELINE_DURATION_TOLERANCE_MS = 2_000


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
    # A continuous packet index is authoritative when VLC under-reports the
    # stream.  Keep the player value when it is longer, which is safer for an
    # unusual stream containing a backwards timestamp reset.
    return max(player_duration, packet_duration)


def _uses_fractional_timeline_seek(
    player_duration_ms: int | float,
    index: MediaTimelineIndex | None,
) -> bool:
    """Return whether millisecond seeks are bounded by bad VLC metadata."""

    if index is None or index.is_corrected:
        return False
    player_duration = max(0, int(round(float(player_duration_ms))))
    packet_duration = max(0, int(round(float(index.duration_ms))))
    return bool(
        player_duration > 0
        and packet_duration
        > player_duration + TIMELINE_DURATION_TOLERANCE_MS
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


class SafeMediaEngine(MediaEngine):
    """Compatibility and lifecycle fixes for the installed libVLC 3.x."""

    timeline_correction_changed = Signal(int, int, int)
    timeline_analysis_message = Signal(str, int)
    _timeline_probe_completed = Signal(int, str, object, str)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Choose avformat per file instead of forcing it for every container.
        kwargs["force_avformat"] = False
        super().__init__(*args, **kwargs)
        self._lib.libvlc_media_player_set_time.restype = None
        self._pending_seek_ms: int | None = None
        self._seek_barrier = PlaybackSeekBarrier()
        self._requested_rate = 1.0
        self._closing_async = False
        self._release_thread: threading.Thread | None = None
        self._timeline_index: MediaTimelineIndex | None = None
        self._timeline_segment_hint = 0
        self._timeline_probe_generation = 0
        self._timeline_probe_thread: threading.Thread | None = None
        self._timeline_probe_lock = threading.Lock()
        self._timeline_probe_pending = False
        self._normalized_seek_time_offset_ms = 0.0
        self._timeline_duration_overrides_player = False
        self._timeline_probe_completed.connect(
            self._finish_timeline_probe
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
        self._timeline_probe_generation += 1
        self._timeline_index = None
        self._timeline_segment_hint = 0
        self._timeline_probe_pending = False
        self._normalized_seek_time_offset_ms = 0.0
        self._timeline_duration_overrides_player = False
        self._force_avformat = self._is_hikvision_program_stream(normalized)
        self._pending_seek_ms = None
        self._seek_barrier.reset()
        if self._player and self._media:
            # Detach the old media without waiting synchronously for the
            # Hikvision decoder's occasionally slow stop path.
            self._lib.libvlc_media_player_set_pause(self._player, 1)
            self._lib.libvlc_media_player_set_media(self._player, None)
        opened = super().open(normalized)
        if opened and self._force_avformat:
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
        elif error:
            self._timeline_probe_pending = False
            self.timeline_analysis_message.emit(
                "视频时间轴校验失败，暂时使用播放器原始时长", 6000
            )

    def _apply_timeline_index(self, index: MediaTimelineIndex) -> None:
        player_duration = super().duration_ms()
        self._timeline_index = index
        self._timeline_segment_hint = 0
        self._timeline_duration_overrides_player = (
            _uses_fractional_timeline_seek(player_duration, index)
        )
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

    def _public_to_raw_time_ms(self, value_ms: int | float) -> int:
        index = self._timeline_index
        if index is None or not index.is_corrected:
            return max(0, int(round(value_ms)))
        raw, segment = index.public_to_raw_ms(float(value_ms))
        self._timeline_segment_hint = segment
        return max(0, int(round(raw)))

    def _raw_to_public_time_ms(self, value_ms: int | float) -> int:
        index = self._timeline_index
        if index is None or not index.is_corrected:
            return max(
                0,
                int(
                    round(
                        float(value_ms)
                        + self._normalized_seek_time_offset_ms
                    )
                ),
            )
        public, segment = index.raw_to_public_ms(
            float(value_ms), self._timeline_segment_hint
        )
        self._timeline_segment_hint = segment
        return max(0, int(round(public)))

    def get_time_ms(self) -> int:
        raw_time_ms = super().get_time_ms()
        return self._raw_to_public_time_ms(raw_time_ms)

    def duration_ms(self) -> int:
        return _effective_timeline_duration_ms(
            super().duration_ms(),
            self._timeline_index,
        )

    def player_duration_ms(self) -> int:
        """Return VLC's container duration before packet-time correction."""

        return super().duration_ms()

    def _apply_decoder_seek(self, target_ms: int) -> None:
        index = self._timeline_index
        player_duration = super().duration_ms()
        if _uses_fractional_timeline_seek(player_duration, index):
            assert index is not None
            fraction, offset_ms = _fractional_seek_clock(
                target_ms,
                player_duration,
                index.duration_ms,
            )
            self._normalized_seek_time_offset_ms = offset_ms
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

    def set_time_ms(self, value: int | float) -> bool:
        self._ensure_media()
        requested_target = max(0, int(round(value)))
        probe_pending = bool(
            self._timeline_probe_pending and self._timeline_index is None
        )
        target = requested_target
        duration = self.duration_ms()
        if duration > 0 and not probe_pending:
            target = min(target, duration)
        playing = self.is_playing()
        self._seek_barrier.request(
            target, playing=playing, now=time.monotonic()
        )
        if (
            probe_pending
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
        target = self._seek_barrier.playback_started()
        if target is not None:
            # Apply once after VLC enters a genuinely seekable playback state.
            # This replaces the old decoder timestamp before it can reach UI.
            self._pending_seek_ms = int(round(target))
        return super().play()

    def set_rate(self, rate: float) -> bool:
        self._requested_rate = float(rate)
        return super().set_rate(rate)

    def poll(self) -> dict[str, Any]:
        if (
            self._pending_seek_ms is not None
            and self._player
            and not self._timeline_probe_pending
        ):
            state_code = int(
                self._lib.libvlc_media_player_get_state(self._player)
            )
            seekable = bool(
                self._lib.libvlc_media_player_is_seekable(self._player)
            )
            if seekable and state_code in {3, 4}:  # playing / paused
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
                self._apply_decoder_seek(target)
        snapshot = super().poll()
        # MediaEngine only calls _accept_time_update when the integer VLC
        # timestamp changes.  Maintain the deadline here as well so a frozen
        # decoder clock cannot leave playback and both UI timelines blocked.
        self._seek_barrier.expire(time.monotonic())
        if snapshot.get("status") == "playing":
            current = float(snapshot.get("rate", 1.0))
            if abs(current - self._requested_rate) > 1e-3:
                super().set_rate(self._requested_rate)
        return snapshot

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
        self._player = None
        self._media = None
        self._instance = None
        self._dll_directory_handle = None
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
