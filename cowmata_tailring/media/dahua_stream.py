from __future__ import annotations

import bisect
import ctypes
import os
import threading
from pathlib import Path

from cowmata_tailring.media.dahua_duration import DahuaDurationIndex

DahuaOpenCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_uint64),
)
DahuaReadCallback = ctypes.CFUNCTYPE(
    ctypes.c_ssize_t,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
)
DahuaSeekCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_uint64,
)
DahuaCloseCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


def _encode_mpeg_timestamp(value: int | float, prefix: int) -> bytes:
    timestamp = max(0, int(round(float(value)))) & ((1 << 33) - 1)
    return bytes(
        (
            (int(prefix) << 4) | (((timestamp >> 30) & 0x07) << 1) | 1,
            (timestamp >> 22) & 0xFF,
            (((timestamp >> 15) & 0x7F) << 1) | 1,
            (timestamp >> 7) & 0xFF,
            ((timestamp & 0x7F) << 1) | 1,
        )
    )


class DahuaPlaybackStream:
    """Expose a timestamp-normalized virtual slice of a Dahua MPEG-PS file."""

    PRE_ROLL_MS = 150.0
    TARGET_HOLD_MS = 250.0
    TIMESTAMP_BASE_TICKS = 90_000
    SEGMENT_DURATION_MS = 60_000

    def __init__(
        self,
        index: DahuaDurationIndex,
        target_ms: int | float,
    ) -> None:
        if not index.has_seek_index:
            raise ValueError("Dahua playback requires a keyframe index")
        self.index = index
        self.path = str(Path(index.source_path).resolve())
        self.target_ms = min(
            float(index.duration_ms),
            max(0.0, float(target_ms)),
        )
        self.anchor = index.seek_point_at_or_before(self.target_ms)
        self.anchor_ms = float(self.anchor.time_ms)
        self.base_offset = max(0, int(self.anchor.byte_offset))
        seek_times = [point.time_ms for point in index.seek_points]
        end_index = bisect.bisect_left(
            seek_times,
            self.target_ms + self.SEGMENT_DURATION_MS,
        )
        if end_index < len(index.seek_points):
            end_point = index.seek_points[end_index]
            self.end_offset = max(self.base_offset, int(end_point.byte_offset))
            self.segment_end_ms = float(end_point.time_ms)
        else:
            self.end_offset = int(index.source_size)
            self.segment_end_ms = float(index.duration_ms)
        self.virtual_size = max(0, self.end_offset - self.base_offset)
        has_preroll = self.target_ms > self.anchor_ms + 1.0
        self.pre_roll_ms = self.PRE_ROLL_MS if has_preroll else 0.0
        self.target_hold_ms = self.TARGET_HOLD_MS if has_preroll else 0.0

        self._timestamp_points = index.timestamp_points
        self._timestamp_offsets = tuple(point.byte_offset for point in self._timestamp_points)
        self._frame_delay_ticks = max(
            1,
            int(round(index.packets.frame_duration_ms * 90.0)),
        )
        self._fd = -1
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self.open_callback = DahuaOpenCallback(self._open)
        self.read_callback = DahuaReadCallback(self._read)
        # Every public seek reopens at an indexed keyframe. Exposing an inner
        # seek callback lets avformat perform a second timestamp-based seek,
        # which is exactly what is broken in these recordings.
        self.seek_callback = DahuaSeekCallback()
        self.close_callback = DahuaCloseCallback(self._close)

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    @property
    def ready_raw_time_ms(self) -> float:
        return self.pre_roll_ms

    def close(self) -> None:
        self._close(0)

    def public_time_ms(self, raw_time_ms: int | float) -> int:
        raw = max(0.0, float(raw_time_ms))
        if self.pre_roll_ms > 0.0 and raw < self.pre_roll_ms:
            fraction = raw / self.pre_roll_ms
            logical = self.anchor_ms + (self.target_ms - self.anchor_ms) * fraction
        elif raw < self.pre_roll_ms + self.target_hold_ms:
            logical = self.target_ms
        else:
            logical = self.target_ms + (raw - self.pre_roll_ms - self.target_hold_ms)
        return max(0, min(int(self.index.duration_ms), int(round(logical))))

    def patch_bytes(self, physical_start: int, payload: bytes) -> bytes:
        if not payload or not self._timestamp_points:
            return payload
        data = bytearray(payload)
        physical_end = int(physical_start) + len(data)
        point_index = bisect.bisect_left(
            self._timestamp_offsets,
            int(physical_start) - 10,
        )
        while point_index < len(self._timestamp_points):
            point = self._timestamp_points[point_index]
            if point.byte_offset >= physical_end:
                break
            output_ms = self._output_time_ms(float(point.time_ms))
            dts_ticks = self.TIMESTAMP_BASE_TICKS + int(round(output_ms * 90.0))
            pts_ticks = dts_ticks + (self._frame_delay_ticks if point.is_video else 0)
            replacements = [
                (
                    point.byte_offset,
                    _encode_mpeg_timestamp(
                        pts_ticks,
                        3 if point.has_dts else 2,
                    ),
                )
            ]
            if point.has_dts:
                replacements.append(
                    (
                        point.byte_offset + 5,
                        _encode_mpeg_timestamp(dts_ticks, 1),
                    )
                )
            for replacement_start, replacement in replacements:
                overlap_start = max(int(physical_start), replacement_start)
                overlap_end = min(
                    physical_end,
                    replacement_start + len(replacement),
                )
                if overlap_start < overlap_end:
                    data[
                        overlap_start - int(physical_start) : overlap_end - int(physical_start)
                    ] = replacement[
                        overlap_start - replacement_start : overlap_end - replacement_start
                    ]
            point_index += 1
        return bytes(data)

    def _output_time_ms(self, logical_time_ms: float) -> float:
        if logical_time_ms < self.target_ms and self.pre_roll_ms > 0.0:
            return (
                self.pre_roll_ms
                * max(
                    0.0,
                    logical_time_ms - self.anchor_ms,
                )
                / max(1.0, self.target_ms - self.anchor_ms)
            )
        if logical_time_ms > self.target_ms and self.target_hold_ms > 0.0:
            return self.pre_roll_ms + self.target_hold_ms + logical_time_ms - self.target_ms
        return self.pre_roll_ms + max(0.0, logical_time_ms - self.target_ms)

    def _open(
        self,
        opaque: int,
        data_pointer: ctypes.Array[ctypes.c_void_p],
        size_pointer: ctypes.Array[ctypes.c_uint64],
    ) -> int:
        del opaque
        try:
            with self._lock:
                self._close_file()
                flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
                self._fd = os.open(self.path, flags)
                os.lseek(self._fd, self.base_offset, os.SEEK_SET)
                data_pointer[0] = None
                size_pointer[0] = self.virtual_size
                self._closed.clear()
            return 0
        except OSError:
            self._close_file()
            return -1

    def _read(
        self,
        _data: int,
        buffer: int,
        length: int,
    ) -> int:
        try:
            with self._lock:
                if self._fd < 0:
                    return -1
                physical_start = os.lseek(self._fd, 0, os.SEEK_CUR)
                remaining = max(
                    0,
                    self.end_offset - physical_start,
                )
                payload = os.read(self._fd, min(int(length), remaining))
                patched = self.patch_bytes(physical_start, payload)
            if patched:
                ctypes.memmove(buffer, patched, len(patched))
            return len(patched)
        except (OSError, ValueError):
            return -1

    def _seek(self, _data: int, offset: int) -> int:
        try:
            with self._lock:
                if self._fd < 0:
                    return -1
                virtual_offset = min(
                    max(0, int(offset)),
                    self.virtual_size,
                )
                os.lseek(
                    self._fd,
                    self.base_offset + virtual_offset,
                    os.SEEK_SET,
                )
            return 0
        except OSError:
            return -1

    def _close(self, _data: int) -> None:
        with self._lock:
            self._close_file()
            self._closed.set()

    def _close_file(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1


__all__ = [
    "DahuaCloseCallback",
    "DahuaOpenCallback",
    "DahuaPlaybackStream",
    "DahuaReadCallback",
    "DahuaSeekCallback",
]
