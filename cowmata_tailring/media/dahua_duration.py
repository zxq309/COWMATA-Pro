from __future__ import annotations

import base64
import bisect
import hashlib
import json
import mmap
import os
import re
import statistics
import subprocess
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

CACHE_SCHEMA = 3
MAX_ADJACENT_DURATION_MS = 6 * 60 * 60 * 1000
MIN_ADJACENT_DURATION_MS = 1_000
MIN_COMPLETE_PACKET_COVERAGE = 0.90
DURATION_MATCH_ABSOLUTE_MS = 5_000
DURATION_MATCH_RELATIVE = 0.08
DEFAULT_FFPROBE_TIMEOUT_SECONDS = 30.0
FORWARD_OUTLIER_MS = 300_000.0
BACKWARD_RESET_MS = -1_000.0

_START_CODE = b"\x00\x00\x01"
_PACK_START = _START_CODE + b"\xba"
_VIDEO_START = _START_CODE + b"\xe0"


class DahuaDurationProbeError(RuntimeError):
    """Raised when a Dahua/Imou media index cannot be built."""


@dataclass(frozen=True)
class DahuaPacketSummary:
    packet_count: int
    frame_duration_ms: float
    maximum_position: int
    packet_coverage: float
    backward_resets: int
    forward_outliers: int

    @property
    def estimated_duration_ms(self) -> float:
        return max(
            0.0,
            float(self.packet_count) * float(self.frame_duration_ms),
        )

    def to_dict(self) -> dict[str, float | int]:
        return {
            "count": self.packet_count,
            "frameDurationMs": self.frame_duration_ms,
            "maximumPosition": self.maximum_position,
            "coverage": self.packet_coverage,
            "backwardResets": self.backward_resets,
            "forwardOutliers": self.forward_outliers,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> DahuaPacketSummary:
        return cls(
            packet_count=int(value["count"]),
            frame_duration_ms=float(value["frameDurationMs"]),
            maximum_position=int(value["maximumPosition"]),
            packet_coverage=float(value["coverage"]),
            backward_resets=int(value["backwardResets"]),
            forward_outliers=int(value["forwardOutliers"]),
        )


@dataclass(frozen=True)
class DahuaSeekPoint:
    time_ms: int
    byte_offset: int


@dataclass(frozen=True)
class DahuaTimestampPoint:
    time_ms: int
    byte_offset: int
    has_dts: bool
    is_video: bool


@dataclass(frozen=True)
class DahuaProgramScan:
    frame_count: int
    keyframes: tuple[tuple[int, int], ...]
    timestamps: tuple[tuple[int, float, bool, bool], ...]
    frame_duration_ms: float = 0.0


def _append_varint(payload: bytearray, value: int) -> None:
    remaining = max(0, int(value))
    while remaining >= 0x80:
        payload.append((remaining & 0x7F) | 0x80)
        remaining >>= 7
    payload.append(remaining)


def _read_varint(payload: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(payload) and shift <= 35:
        byte = payload[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid Dahua timestamp varint")


def _encode_timestamp_points(points: tuple[DahuaTimestampPoint, ...]) -> str:
    payload = bytearray()
    _append_varint(payload, len(points))
    previous_offset = 0
    previous_time = 0
    for point in points:
        flags = int(point.has_dts) | (int(point.is_video) << 1)
        byte_offset = int(point.byte_offset)
        time_ms = int(point.time_ms)
        if byte_offset < previous_offset or time_ms < previous_time:
            raise ValueError("Dahua timestamp index is not monotonic")
        _append_varint(payload, byte_offset - previous_offset)
        _append_varint(payload, time_ms - previous_time)
        payload.append(flags)
        previous_offset = byte_offset
        previous_time = time_ms
    compressed = zlib.compress(bytes(payload), level=9)
    return base64.b64encode(compressed).decode("ascii")


def _decode_timestamp_points(value: object) -> tuple[DahuaTimestampPoint, ...]:
    if not isinstance(value, str):
        raise ValueError("invalid Dahua timestamp index")
    try:
        payload = zlib.decompress(base64.b64decode(value, validate=True))
    except (ValueError, zlib.error) as exc:
        raise ValueError("invalid Dahua timestamp index") from exc
    count, offset = _read_varint(payload, 0)
    points: list[DahuaTimestampPoint] = []
    byte_offset = 0
    time_ms = 0
    for _ in range(count):
        offset_delta, offset = _read_varint(payload, offset)
        time_delta, offset = _read_varint(payload, offset)
        if offset >= len(payload):
            raise ValueError("truncated Dahua timestamp index")
        flags = payload[offset]
        offset += 1
        byte_offset += offset_delta
        time_ms += time_delta
        points.append(
            DahuaTimestampPoint(
                time_ms=int(time_ms),
                byte_offset=int(byte_offset),
                has_dts=bool(flags & 1),
                is_video=bool(flags & 2),
            )
        )
    if offset != len(payload):
        raise ValueError("invalid Dahua timestamp index tail")
    return tuple(points)


@dataclass(frozen=True)
class DahuaDurationIndex:
    source_path: str
    source_size: int
    source_mtime_ns: int
    duration_ms: int
    basis: str
    next_path: str | None
    next_size: int | None
    next_mtime_ns: int | None
    packets: DahuaPacketSummary
    frame_count: int = 0
    seek_points: tuple[DahuaSeekPoint, ...] = ()
    timestamp_points: tuple[DahuaTimestampPoint, ...] = ()

    @property
    def has_seek_index(self) -> bool:
        return bool(self.frame_count > 0 and self.seek_points)

    def seek_point_at_or_before(self, time_ms: int | float) -> DahuaSeekPoint:
        if not self.seek_points:
            return DahuaSeekPoint(time_ms=0, byte_offset=0)
        target = min(float(self.duration_ms), max(0.0, float(time_ms)))
        times = [point.time_ms for point in self.seek_points]
        index = max(0, bisect.bisect_right(times, target) - 1)
        return self.seek_points[index]

    def to_dict(self) -> dict[str, object]:
        next_source = None
        if self.next_path is not None:
            next_source = {
                "path": self.next_path,
                "size": self.next_size,
                "mtimeNs": self.next_mtime_ns,
            }
        seek_index: dict[str, object] | None = None
        if self.has_seek_index:
            seek_index = {
                "frameCount": self.frame_count,
                "keyframes": [[point.time_ms, point.byte_offset] for point in self.seek_points],
                "timestamps": _encode_timestamp_points(self.timestamp_points),
            }
        return {
            "schema": CACHE_SCHEMA,
            "source": {
                "path": self.source_path,
                "size": self.source_size,
                "mtimeNs": self.source_mtime_ns,
            },
            "durationMs": self.duration_ms,
            "basis": self.basis,
            "nextSource": next_source,
            "packets": self.packets.to_dict(),
            "seekIndex": seek_index,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> DahuaDurationIndex:
        if int(value.get("schema", 0)) != CACHE_SCHEMA:
            raise ValueError("unsupported Dahua duration cache schema")
        source = value["source"]
        packets = value["packets"]
        next_source = value.get("nextSource")
        seek_index = value.get("seekIndex")
        if not isinstance(source, dict) or not isinstance(packets, dict):
            raise ValueError("invalid Dahua duration cache")
        if next_source is not None and not isinstance(next_source, dict):
            raise ValueError("invalid Dahua next-source cache")

        frame_count = 0
        seek_points: tuple[DahuaSeekPoint, ...] = ()
        timestamp_points: tuple[DahuaTimestampPoint, ...] = ()
        if seek_index is not None:
            if not isinstance(seek_index, dict):
                raise ValueError("invalid Dahua seek index")
            raw_keyframes = seek_index.get("keyframes")
            if not isinstance(raw_keyframes, list):
                raise ValueError("invalid Dahua keyframe index")
            frame_count = int(seek_index["frameCount"])
            seek_points = tuple(
                DahuaSeekPoint(time_ms=int(item[0]), byte_offset=int(item[1]))
                for item in raw_keyframes
                if isinstance(item, list) and len(item) == 2
            )
            timestamp_points = _decode_timestamp_points(seek_index.get("timestamps"))
            if frame_count <= 0 or not seek_points:
                raise ValueError("empty Dahua seek index")

        return cls(
            source_path=str(source["path"]),
            source_size=int(source["size"]),
            source_mtime_ns=int(source["mtimeNs"]),
            duration_ms=int(value["durationMs"]),
            basis=str(value["basis"]),
            next_path=(None if next_source is None else str(next_source["path"])),
            next_size=(None if next_source is None else int(next_source["size"])),
            next_mtime_ns=(None if next_source is None else int(next_source["mtimeNs"])),
            packets=DahuaPacketSummary.from_dict(packets),
            frame_count=frame_count,
            seek_points=seek_points,
            timestamp_points=timestamp_points,
        )


def _source_identity(
    path: str | os.PathLike[str],
) -> tuple[str, int, int]:
    source = Path(path).resolve()
    stat = source.stat()
    return str(source), int(stat.st_size), int(stat.st_mtime_ns)


def is_dahua_program_stream(path: str | os.PathLike[str]) -> bool:
    """Recognise Dahua/Imou MPEG-PS without trusting the file extension."""

    try:
        with open(path, "rb") as source:
            header = source.read(4_096)
    except OSError:
        return False
    return bool(header.startswith(_PACK_START) and (b"DHAV" in header or b"DHES" in header))


def find_next_recording(
    path: str | os.PathLike[str],
) -> Path | None:
    """Return the exact next numbered Dahua/Imou recording, if present."""

    source = Path(path).resolve()
    match = re.fullmatch(r"(.*?)(\d+)(\.[^.]+)", source.name)
    if match is None:
        return None
    prefix, digits, suffix = match.groups()
    next_name = f"{prefix}{int(digits) + 1:0{len(digits)}d}{suffix}"
    candidate = source.with_name(next_name)
    if not candidate.is_file() or not is_dahua_program_stream(candidate):
        return None
    return candidate


def parse_ffprobe_packet_summary(
    lines: Iterable[str],
    source_size: int,
) -> DahuaPacketSummary:
    timestamps_ms: list[float] = []
    durations_ms: list[float] = []
    maximum_position = 0

    for line in lines:
        values: dict[str, str] = {}
        for field in line.strip().split("|"):
            key, separator, value = field.partition("=")
            if separator:
                values[key] = value

        raw_clock = values.get("dts_time")
        if raw_clock in (None, "N/A"):
            raw_clock = values.get("pts_time")
        if raw_clock not in (None, "N/A"):
            try:
                timestamps_ms.append(float(raw_clock) * 1000.0)
            except ValueError:
                pass

        raw_duration = values.get("duration_time")
        if raw_duration not in (None, "N/A"):
            try:
                duration_ms = float(raw_duration) * 1000.0
            except ValueError:
                duration_ms = 0.0
            if 0.0 < duration_ms <= 1_000.0:
                durations_ms.append(duration_ms)

        raw_position = values.get("pos")
        if raw_position not in (None, "N/A"):
            try:
                maximum_position = max(maximum_position, int(raw_position))
            except ValueError:
                pass

    deltas = [current - previous for previous, current in zip(timestamps_ms, timestamps_ms[1:])]
    frame_duration_ms = float(statistics.median(durations_ms)) if durations_ms else 0.0
    coverage = min(1.0, max(0.0, maximum_position / float(source_size))) if source_size > 0 else 0.0
    return DahuaPacketSummary(
        packet_count=len(timestamps_ms),
        frame_duration_ms=frame_duration_ms,
        maximum_position=maximum_position,
        packet_coverage=coverage,
        backward_resets=sum(delta < BACKWARD_RESET_MS for delta in deltas),
        forward_outliers=sum(delta > FORWARD_OUTLIER_MS for delta in deltas),
    )


def _pes_payload_bounds(
    data: mmap.mmap,
    position: int,
) -> tuple[int, int] | None:
    if position + 9 > len(data):
        return None
    packet_length = int.from_bytes(data[position + 4 : position + 6], "big")
    packet_end = position + 6 + packet_length
    payload_start = position + 9 + int(data[position + 8])
    if packet_length <= 0 or payload_start > packet_end or packet_end > len(data):
        return None
    return payload_start, packet_end


def _timestamp_flags(data: mmap.mmap, position: int) -> int:
    if position + 9 > len(data):
        return 0
    return (int(data[position + 7]) >> 6) & 0x03


def _decode_mpeg_timestamp(data: mmap.mmap, position: int) -> int | None:
    if position < 0 or position + 5 > len(data):
        return None
    value = data[position : position + 5]
    if not (value[0] & 1 and value[2] & 1 and value[4] & 1):
        return None
    return (
        ((value[0] >> 1) & 0x07) << 30
        | value[1] << 22
        | ((value[2] >> 1) & 0x7F) << 15
        | value[3] << 7
        | ((value[4] >> 1) & 0x7F)
    )


def _infer_frame_duration_ms(
    samples: Iterable[tuple[float, int]],
) -> float:
    total_ticks = 0.0
    total_frames = 0.0
    valid_intervals = 0
    sample_list = list(samples)
    for (previous_frame, previous_ticks), (current_frame, current_ticks) in zip(
        sample_list,
        sample_list[1:],
    ):
        frame_delta = float(current_frame) - float(previous_frame)
        tick_delta = int(current_ticks) - int(previous_ticks)
        if frame_delta <= 0.0 or tick_delta <= 0:
            continue
        duration_ms = tick_delta / (90.0 * frame_delta)
        if not 5.0 <= duration_ms <= 250.0:
            continue
        total_ticks += tick_delta
        total_frames += frame_delta
        valid_intervals += 1
    if valid_intervals < 2 or total_frames <= 0.0:
        return 0.0
    return total_ticks / (90.0 * total_frames)


def scan_dahua_program_stream(
    source_path: str | os.PathLike[str],
) -> DahuaProgramScan:
    """Scan HEVC access units and timestamp fields without decoding frames."""

    source = Path(source_path).resolve()
    frame_positions: list[int] = []
    keyframes: list[tuple[int, int]] = []
    video_timestamps: list[tuple[int, float, bool, bool]] = []
    video_clock_samples: list[tuple[float, int]] = []

    with source.open("rb") as handle:
        data = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            position = 0
            while True:
                position = data.find(_VIDEO_START, position)
                if position < 0:
                    break
                bounds = _pes_payload_bounds(data, position)
                if bounds is None:
                    position += len(_VIDEO_START)
                    continue
                payload_start, packet_end = bounds
                first_frame_index = len(frame_positions)
                cursor = payload_start
                while True:
                    marker = data.find(_START_CODE, cursor, packet_end)
                    if marker < 0 or marker + 6 >= packet_end:
                        break
                    nal_header = marker + len(_START_CODE)
                    nal_type = (int(data[nal_header]) >> 1) & 0x3F
                    first_slice = bool(int(data[nal_header + 2]) & 0x80)
                    if nal_type <= 31 and first_slice:
                        frame_index = len(frame_positions)
                        frame_positions.append(marker)
                        if 16 <= nal_type <= 23:
                            pack_position = data.rfind(
                                _PACK_START,
                                max(0, position - 131_072),
                                position + 1,
                            )
                            if pack_position >= 0:
                                keyframes.append((frame_index, pack_position))
                    cursor = nal_header + 1

                flags = _timestamp_flags(data, position)
                if flags in {2, 3}:
                    coordinate = float(
                        first_frame_index
                        if len(frame_positions) > first_frame_index
                        else max(0, len(frame_positions) - 1)
                    )
                    video_timestamps.append((position + 9, coordinate, flags == 3, True))
                    clock_ticks = _decode_mpeg_timestamp(data, position + 9)
                    if clock_ticks is not None:
                        video_clock_samples.append((coordinate, clock_ticks))
                position = packet_end

            audio_positions: list[tuple[int, bool]] = []
            position = 0
            while True:
                position = data.find(_START_CODE, position)
                if position < 0 or position + 9 > len(data):
                    break
                stream_id = int(data[position + 3])
                if 0xC0 <= stream_id <= 0xDF:
                    bounds = _pes_payload_bounds(data, position)
                    flags = _timestamp_flags(data, position)
                    if bounds is not None and flags in {2, 3}:
                        audio_positions.append((position + 9, flags == 3))
                position += len(_START_CODE) + 1

            timestamps = list(video_timestamps)
            for byte_offset, has_dts in audio_positions:
                if frame_positions:
                    frame_index = max(
                        0,
                        bisect.bisect_right(frame_positions, byte_offset) - 1,
                    )
                    coordinate = float(frame_index)
                    if frame_index + 1 < len(frame_positions):
                        start = frame_positions[frame_index]
                        end = frame_positions[frame_index + 1]
                        coordinate += min(
                            1.0,
                            max(0.0, (byte_offset - start) / max(1, end - start)),
                        )
                else:
                    coordinate = 0.0
                timestamps.append((byte_offset, coordinate, has_dts, False))
        finally:
            data.close()

    timestamps.sort(key=lambda item: item[0])
    deduplicated_keyframes: list[tuple[int, int]] = []
    for point in keyframes:
        if not deduplicated_keyframes or point[1] != deduplicated_keyframes[-1][1]:
            deduplicated_keyframes.append(point)
    return DahuaProgramScan(
        frame_count=len(frame_positions),
        keyframes=tuple(deduplicated_keyframes),
        timestamps=tuple(timestamps),
        frame_duration_ms=_infer_frame_duration_ms(video_clock_samples),
    )


def _adjacent_duration_ms(source: Path, next_source: Path) -> int:
    delta_ns = next_source.stat().st_mtime_ns - source.stat().st_mtime_ns
    duration_ms = int(round(delta_ns / 1_000_000.0))
    if not MIN_ADJACENT_DURATION_MS <= duration_ms <= MAX_ADJACENT_DURATION_MS:
        return 0
    return duration_ms


def build_dahua_duration_index(
    source_path: str | os.PathLike[str],
    packets: DahuaPacketSummary,
    program_scan: DahuaProgramScan | None = None,
) -> DahuaDurationIndex:
    source = Path(source_path).resolve()
    source_value, source_size, source_mtime_ns = _source_identity(source)
    next_source = find_next_recording(source)
    adjacent_duration_ms = (
        _adjacent_duration_ms(source, next_source) if next_source is not None else 0
    )
    duration_frame_count = packets.packet_count
    if program_scan is not None and program_scan.frame_count > 0:
        duration_frame_count = program_scan.frame_count
    packet_duration_ms = int(round(duration_frame_count * packets.frame_duration_ms))

    basis = "packet_scan"
    duration_ms = packet_duration_ms
    if adjacent_duration_ms > 0:
        tolerance_ms = max(
            DURATION_MATCH_ABSOLUTE_MS,
            int(round(adjacent_duration_ms * DURATION_MATCH_RELATIVE)),
        )
        packet_matches = bool(
            packet_duration_ms > 0
            and abs(packet_duration_ms - adjacent_duration_ms) <= tolerance_ms
        )
        packet_scan_is_partial = bool(
            packets.packet_coverage < MIN_COMPLETE_PACKET_COVERAGE and program_scan is None
        )
        # File mtimes are recording metadata only while the files remain on
        # the recorder. Copy tools can replace them with per-file copy times,
        # so they must never override a complete scan of the file contents.
        if (
            packet_duration_ms <= 0
            or packet_matches
            or packet_scan_is_partial
        ):
            duration_ms = adjacent_duration_ms
            basis = "adjacent_file_validated" if packet_matches else "adjacent_file"

    if duration_ms <= 0:
        raise DahuaDurationProbeError("乐橙录像没有可用的相邻文件时间或视频帧时长")

    next_path: str | None = None
    next_size: int | None = None
    next_mtime_ns: int | None = None
    if next_source is not None:
        next_path, next_size, next_mtime_ns = _source_identity(next_source)

    frame_count = 0
    seek_points: tuple[DahuaSeekPoint, ...] = ()
    timestamp_points: tuple[DahuaTimestampPoint, ...] = ()
    if program_scan is not None and program_scan.frame_count > 0:
        frame_count = program_scan.frame_count
        scale = float(duration_ms) / float(frame_count)
        seek_points = tuple(
            DahuaSeekPoint(
                time_ms=max(0, int(round(frame_index * scale))),
                byte_offset=max(0, int(byte_offset)),
            )
            for frame_index, byte_offset in program_scan.keyframes
        )
        built_timestamps: list[DahuaTimestampPoint] = []
        previous_time_ms = 0
        for (
            byte_offset,
            frame_coordinate,
            has_dts,
            is_video,
        ) in sorted(program_scan.timestamps, key=lambda item: item[0]):
            # A damaged video PES can carry a timestamp without a complete
            # first-slice NAL. Its frame coordinate may therefore trail an
            # audio timestamp that physically precedes it. Keep the virtual
            # stream clock monotonic in byte order so the decoder never sees
            # a synthetic backwards jump.
            time_ms = max(
                previous_time_ms,
                int(round(frame_coordinate * scale)),
            )
            built_timestamps.append(
                DahuaTimestampPoint(
                    time_ms=time_ms,
                    byte_offset=max(0, int(byte_offset)),
                    has_dts=bool(has_dts),
                    is_video=bool(is_video),
                )
            )
            previous_time_ms = time_ms
        timestamp_points = tuple(built_timestamps)

    return DahuaDurationIndex(
        source_path=source_value,
        source_size=source_size,
        source_mtime_ns=source_mtime_ns,
        duration_ms=duration_ms,
        basis=basis,
        next_path=next_path,
        next_size=next_size,
        next_mtime_ns=next_mtime_ns,
        packets=packets,
        frame_count=frame_count,
        seek_points=seek_points,
        timestamp_points=timestamp_points,
    )


def probe_dahua_duration(
    source_path: str | os.PathLike[str],
    ffprobe_path: str | os.PathLike[str],
    *,
    timeout_seconds: float = DEFAULT_FFPROBE_TIMEOUT_SECONDS,
) -> DahuaDurationIndex:
    source = Path(source_path).resolve()
    if not is_dahua_program_stream(source):
        raise DahuaDurationProbeError("文件不是乐橙/Dahua MPEG-PS录像")

    command = [
        os.fspath(ffprobe_path),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,dts_time,duration_time,pos",
        "-of",
        "compact=p=0:nk=0",
        os.fspath(source),
    ]
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return probe_dahua_program_stream_duration(source)
    if process.returncode != 0:
        return probe_dahua_program_stream_duration(source)
    packets = parse_ffprobe_packet_summary(
        process.stdout.splitlines(),
        source.stat().st_size,
    )
    program_scan = scan_dahua_program_stream(source)
    if packets.packet_count <= 0 or packets.frame_duration_ms <= 0.0:
        return _build_dahua_program_stream_duration(source, program_scan)
    return build_dahua_duration_index(source, packets, program_scan)


def _build_dahua_program_stream_duration(
    source: Path,
    program_scan: DahuaProgramScan,
) -> DahuaDurationIndex:
    if program_scan.frame_count <= 0:
        raise DahuaDurationProbeError("乐橙内置扫描没有找到视频帧")
    if program_scan.frame_duration_ms <= 0.0:
        raise DahuaDurationProbeError("乐橙内置扫描无法确定视频帧率")
    source_size = source.stat().st_size
    packets = DahuaPacketSummary(
        packet_count=program_scan.frame_count,
        frame_duration_ms=program_scan.frame_duration_ms,
        maximum_position=source_size,
        packet_coverage=1.0,
        backward_resets=0,
        forward_outliers=0,
    )
    index = build_dahua_duration_index(source, packets, program_scan)
    basis = (
        "program_scan_adjacent_validated"
        if index.basis == "adjacent_file_validated"
        else "program_scan"
    )
    return replace(index, basis=basis)


def probe_dahua_program_stream_duration(
    source_path: str | os.PathLike[str],
) -> DahuaDurationIndex:
    source = Path(source_path).resolve()
    if not is_dahua_program_stream(source):
        raise DahuaDurationProbeError("文件不是乐橙/Dahua MPEG-PS录像")
    return _build_dahua_program_stream_duration(
        source,
        scan_dahua_program_stream(source),
    )


def duration_cache_path(
    cache_directory: str | os.PathLike[str],
    source_path: str | os.PathLike[str],
) -> Path:
    source, size, mtime_ns = _source_identity(source_path)
    digest = hashlib.sha1(
        f"{source}|{size}|{mtime_ns}".encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:20]
    return Path(cache_directory) / f"{digest}.dahua-duration.json"


def _legacy_cache_requires_reprobe(index: DahuaDurationIndex) -> bool:
    if index.basis != "adjacent_file":
        return False
    frame_count = index.frame_count
    if (
        frame_count <= 0
        and index.packets.packet_coverage >= MIN_COMPLETE_PACKET_COVERAGE
    ):
        frame_count = index.packets.packet_count
    if frame_count <= 0:
        return False
    scanned_duration_ms = int(
        round(frame_count * index.packets.frame_duration_ms)
    )
    tolerance_ms = max(
        DURATION_MATCH_ABSOLUTE_MS,
        int(round(index.duration_ms * DURATION_MATCH_RELATIVE)),
    )
    return abs(scanned_duration_ms - index.duration_ms) > tolerance_ms


def load_duration_cache(
    cache_directory: str | os.PathLike[str],
    source_path: str | os.PathLike[str],
) -> DahuaDurationIndex | None:
    source, size, mtime_ns = _source_identity(source_path)
    path = duration_cache_path(cache_directory, source)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        legacy_schema = int(payload.get("schema", 0)) == 2
        if legacy_schema:
            payload = dict(payload)
            payload["schema"] = CACHE_SCHEMA
        index = DahuaDurationIndex.from_dict(payload)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if (
        os.path.normcase(index.source_path) != os.path.normcase(source)
        or index.source_size != size
        or index.source_mtime_ns != mtime_ns
    ):
        return None

    current_next = find_next_recording(source)
    if current_next is None:
        if index.next_path is not None:
            return None
    else:
        if index.next_path is None:
            return None
        next_path, next_size, next_mtime_ns = _source_identity(current_next)
        if (
            os.path.normcase(index.next_path) != os.path.normcase(next_path)
            or index.next_size != next_size
            or index.next_mtime_ns != next_mtime_ns
        ):
            return None

    if legacy_schema:
        if _legacy_cache_requires_reprobe(index):
            return None
        try:
            save_duration_cache(cache_directory, index)
        except OSError:
            pass
    return index


def save_duration_cache(
    cache_directory: str | os.PathLike[str],
    index: DahuaDurationIndex,
) -> Path:
    directory = Path(cache_directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = duration_cache_path(directory, index.source_path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(index.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


__all__ = [
    "DahuaDurationIndex",
    "DahuaDurationProbeError",
    "DahuaPacketSummary",
    "DahuaProgramScan",
    "DahuaSeekPoint",
    "DahuaTimestampPoint",
    "build_dahua_duration_index",
    "duration_cache_path",
    "find_next_recording",
    "is_dahua_program_stream",
    "load_duration_cache",
    "parse_ffprobe_packet_summary",
    "probe_dahua_duration",
    "probe_dahua_program_stream_duration",
    "save_duration_cache",
    "scan_dahua_program_stream",
]
