from __future__ import annotations

import hashlib
import json
import os
import statistics
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

CACHE_SCHEMA = 2
MIN_DISCONTINUITY_MS = 300_000.0
FRAME_DISCONTINUITY_MULTIPLIER = 100.0
# Hikvision recorder blocks in the supplied data set end with an 8-17 second
# fragment after a 13-21 hour PTS jump.  Those packets are an orphan recorder
# tail, not a continuation of the main recording.  Keep their raw bounds for
# diagnostics, but never expose them as annotatable/playable time.
MIN_ISOLATED_SEGMENT_MS = 30_000.0


class TimelineProbeError(RuntimeError):
    """Raised when a packet timeline cannot be read safely."""


@dataclass(frozen=True)
class PacketTimestamp:
    pts_ms: float
    duration_ms: float
    position: int | None = None


@dataclass(frozen=True)
class TimelineSegment:
    raw_start_ms: float
    raw_end_ms: float
    public_start_ms: float
    public_end_ms: float

    @property
    def duration_ms(self) -> float:
        return max(0.0, self.public_end_ms - self.public_start_ms)

    def to_dict(self) -> dict[str, float]:
        return {
            "rawStartMs": self.raw_start_ms,
            "rawEndMs": self.raw_end_ms,
            "publicStartMs": self.public_start_ms,
            "publicEndMs": self.public_end_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> TimelineSegment:
        return cls(
            raw_start_ms=float(value["rawStartMs"]),
            raw_end_ms=float(value["rawEndMs"]),
            public_start_ms=float(value["publicStartMs"]),
            public_end_ms=float(value["publicEndMs"]),
        )


def _normalise_timeline_segments(
    raw_segments: Iterable[TimelineSegment],
    frame_duration_ms: float,
) -> tuple[TimelineSegment, ...]:
    """Rebuild public ranges and suppress isolated post-gap recorder tails.

    Re-normalising cached schema-2 entries is intentional.  Older caches used
    a five-second cutoff and therefore exposed the 8-17 second tail present in
    the affected Hikvision files.  Loading those caches through this function
    migrates their behaviour in memory without deleting or rewriting them.
    """

    minimum_frame_ms = max(0.001, float(frame_duration_ms))
    public_cursor_ms = 0.0
    segments: list[TimelineSegment] = []
    for segment_index, raw_segment in enumerate(raw_segments):
        raw_start_ms = float(raw_segment.raw_start_ms)
        raw_segment_duration_ms = max(
            minimum_frame_ms,
            float(raw_segment.raw_end_ms) - raw_start_ms,
        )
        duration_ms = (
            0.0
            if segment_index > 0
            and raw_segment_duration_ms < MIN_ISOLATED_SEGMENT_MS
            else raw_segment_duration_ms
        )
        segments.append(
            TimelineSegment(
                raw_start_ms=raw_start_ms,
                raw_end_ms=raw_start_ms + raw_segment_duration_ms,
                public_start_ms=public_cursor_ms,
                public_end_ms=public_cursor_ms + duration_ms,
            )
        )
        public_cursor_ms += duration_ms
    return tuple(segments)


@dataclass(frozen=True)
class TimelineDiscontinuity:
    packet_index: int
    previous_pts_ms: float
    current_pts_ms: float
    delta_ms: float
    position: int | None = None

    def to_dict(self) -> dict[str, float | int | None]:
        return {
            "packetIndex": self.packet_index,
            "previousPtsMs": self.previous_pts_ms,
            "currentPtsMs": self.current_pts_ms,
            "deltaMs": self.delta_ms,
            "position": self.position,
        }

    @classmethod
    def from_dict(
        cls, value: dict[str, object]
    ) -> TimelineDiscontinuity:
        position = value.get("position")
        return cls(
            packet_index=int(value["packetIndex"]),
            previous_pts_ms=float(value["previousPtsMs"]),
            current_pts_ms=float(value["currentPtsMs"]),
            delta_ms=float(value["deltaMs"]),
            position=None if position is None else int(position),
        )


@dataclass(frozen=True)
class MediaTimelineIndex:
    source_path: str
    source_size: int
    source_mtime_ns: int
    first_pts_ms: float
    frame_duration_ms: float
    segments: tuple[TimelineSegment, ...]
    discontinuities: tuple[TimelineDiscontinuity, ...]

    @property
    def is_corrected(self) -> bool:
        return bool(self.discontinuities and len(self.segments) > 1)

    @property
    def duration_ms(self) -> float:
        if not self.segments:
            return 0.0
        return max(0.0, self.segments[-1].public_end_ms)

    @property
    def raw_duration_ms(self) -> float:
        if not self.segments:
            return 0.0
        return max(segment.raw_end_ms for segment in self.segments)

    @property
    def removed_gap_ms(self) -> float:
        return max(0.0, self.raw_duration_ms - self.duration_ms)

    @property
    def last_playable_segment_index(self) -> int | None:
        """Return the final segment exposed on the public timeline."""

        for index in range(len(self.segments) - 1, -1, -1):
            if self.segments[index].duration_ms > 0.0:
                return index
        return None

    @property
    def has_ignored_trailing_tail(self) -> bool:
        """Whether raw packets remain after the final playable segment."""

        index = self.last_playable_segment_index
        return bool(index is not None and index < len(self.segments) - 1)

    @property
    def playable_raw_end_ms(self) -> float:
        """Exclusive raw end of the final segment that should be displayed."""

        index = self.last_playable_segment_index
        if index is None:
            return 0.0
        return max(0.0, self.segments[index].raw_end_ms)

    @property
    def playable_last_frame_raw_ms(self) -> float:
        """Raw timestamp of the final displayable video frame."""

        index = self.last_playable_segment_index
        if index is None:
            return 0.0
        segment = self.segments[index]
        return max(
            segment.raw_start_ms,
            segment.raw_end_ms - max(0.001, self.frame_duration_ms),
        )

    def public_to_raw_ms(self, value_ms: float) -> tuple[float, int]:
        """Map the continuous UI time to libVLC's original packet time."""

        if not self.segments:
            return max(0.0, float(value_ms)), 0
        value = min(max(0.0, float(value_ms)), self.duration_ms)
        for index, segment in enumerate(self.segments):
            if value <= segment.public_end_ms or index == len(self.segments) - 1:
                offset = min(
                    max(0.0, value - segment.public_start_ms),
                    segment.duration_ms,
                )
                return segment.raw_start_ms + offset, index
        last = self.segments[-1]
        return last.raw_end_ms, len(self.segments) - 1

    def raw_to_public_ms(
        self,
        value_ms: float,
        preferred_segment: int | None = None,
    ) -> tuple[float, int]:
        """Map libVLC time to the continuous time shown by the tool."""

        if not self.segments:
            return max(0.0, float(value_ms)), 0
        value = float(value_ms)
        if preferred_segment is not None and 0 <= preferred_segment < len(
            self.segments
        ):
            preferred = self.segments[preferred_segment]
            if preferred.raw_start_ms <= value <= preferred.raw_end_ms:
                offset = min(
                    max(0.0, value - preferred.raw_start_ms),
                    preferred.duration_ms,
                )
                return preferred.public_start_ms + offset, preferred_segment

        candidates = [
            (index, segment)
            for index, segment in enumerate(self.segments)
            if segment.raw_start_ms <= value <= segment.raw_end_ms
        ]
        if candidates:
            index, segment = candidates[0]
            offset = min(
                max(0.0, value - segment.raw_start_ms),
                segment.duration_ms,
            )
            return segment.public_start_ms + offset, index

        boundaries: list[tuple[float, float, int]] = []
        for index, segment in enumerate(self.segments):
            boundaries.append(
                (abs(value - segment.raw_start_ms), segment.public_start_ms, index)
            )
            boundaries.append(
                (abs(value - segment.raw_end_ms), segment.public_end_ms, index)
            )
        _distance, public_value, index = min(boundaries, key=lambda item: item[0])
        return min(max(0.0, public_value), self.duration_ms), index

    def raw_gap_active(self, value_ms: float) -> bool:
        """Return whether a raw decoder clock is inside a removed PTS gap.

        A corrected timeline intentionally removes these gaps from the public
        clock. While the decoder traverses one, no video frame exists to
        display, so a presentation clock must hold at the nearest segment
        boundary instead of extrapolating through the missing interval.
        """

        if len(self.segments) < 2:
            return False
        value = float(value_ms)
        return any(
            previous.raw_end_ms < value < current.raw_start_ms
            for previous, current in zip(self.segments, self.segments[1:])
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": CACHE_SCHEMA,
            "source": {
                "path": self.source_path,
                "size": self.source_size,
                "mtimeNs": self.source_mtime_ns,
            },
            "firstPtsMs": self.first_pts_ms,
            "frameDurationMs": self.frame_duration_ms,
            "segments": [segment.to_dict() for segment in self.segments],
            "discontinuities": [
                discontinuity.to_dict()
                for discontinuity in self.discontinuities
            ],
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> MediaTimelineIndex:
        if int(value.get("schema", 0)) != CACHE_SCHEMA:
            raise ValueError("unsupported timeline cache schema")
        source = value["source"]
        if not isinstance(source, dict):
            raise ValueError("invalid timeline cache source")
        raw_segments = value.get("segments", [])
        raw_discontinuities = value.get("discontinuities", [])
        if not isinstance(raw_segments, list) or not isinstance(
            raw_discontinuities, list
        ):
            raise ValueError("invalid timeline cache entries")
        frame_duration_ms = float(value["frameDurationMs"])
        parsed_segments = tuple(
            TimelineSegment.from_dict(item)
            for item in raw_segments
            if isinstance(item, dict)
        )
        return cls(
            source_path=str(source["path"]),
            source_size=int(source["size"]),
            source_mtime_ns=int(source["mtimeNs"]),
            first_pts_ms=float(value["firstPtsMs"]),
            frame_duration_ms=frame_duration_ms,
            segments=_normalise_timeline_segments(
                parsed_segments,
                frame_duration_ms,
            ),
            discontinuities=tuple(
                TimelineDiscontinuity.from_dict(item)
                for item in raw_discontinuities
                if isinstance(item, dict)
            ),
        )


def _source_identity(path: str | os.PathLike[str]) -> tuple[str, int, int]:
    source = Path(path).resolve()
    stat = source.stat()
    return str(source), int(stat.st_size), int(stat.st_mtime_ns)


def timeline_cache_path(
    cache_directory: str | os.PathLike[str],
    source_path: str | os.PathLike[str],
) -> Path:
    source, size, mtime_ns = _source_identity(source_path)
    digest = hashlib.sha1(
        f"{source}|{size}|{mtime_ns}".encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:20]
    return Path(cache_directory) / f"{digest}.timeline.json"


def load_timeline_cache(
    cache_directory: str | os.PathLike[str],
    source_path: str | os.PathLike[str],
) -> MediaTimelineIndex | None:
    source, size, mtime_ns = _source_identity(source_path)
    path = timeline_cache_path(cache_directory, source)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        index = MediaTimelineIndex.from_dict(payload)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if (
        os.path.normcase(index.source_path) != os.path.normcase(source)
        or index.source_size != size
        or index.source_mtime_ns != mtime_ns
    ):
        return None
    return index


def save_timeline_cache(
    cache_directory: str | os.PathLike[str],
    index: MediaTimelineIndex,
) -> Path:
    directory = Path(cache_directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = timeline_cache_path(directory, index.source_path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(index.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def parse_ffprobe_packets(lines: Iterable[str]) -> list[PacketTimestamp]:
    packets: list[PacketTimestamp] = []
    for line in lines:
        values: dict[str, str] = {}
        for field in line.strip().split("|"):
            key, separator, value = field.partition("=")
            if separator:
                values[key] = value
        raw_pts = values.get("pts_time")
        if not raw_pts or raw_pts == "N/A":
            raw_pts = values.get("dts_time")
        if not raw_pts or raw_pts == "N/A":
            continue
        try:
            pts_ms = float(raw_pts) * 1000.0
            duration_ms = float(values.get("duration_time", "0")) * 1000.0
        except ValueError:
            continue
        raw_position = values.get("pos")
        try:
            position = None if raw_position in (None, "N/A") else int(raw_position)
        except ValueError:
            position = None
        packets.append(
            PacketTimestamp(
                pts_ms=pts_ms,
                duration_ms=max(0.0, duration_ms),
                position=position,
            )
        )
    return packets


def build_timeline_index(
    source_path: str | os.PathLike[str],
    packets: Iterable[PacketTimestamp],
) -> MediaTimelineIndex:
    packet_list = list(packets)
    if not packet_list:
        raise TimelineProbeError("视频流没有可用的PTS/DTS时间戳")

    source, size, mtime_ns = _source_identity(source_path)
    positive_durations = [
        packet.duration_ms
        for packet in packet_list
        if 0.0 < packet.duration_ms <= 10_000.0
    ]
    if positive_durations:
        frame_duration_ms = float(statistics.median(positive_durations))
    else:
        ordinary_deltas = [
            current.pts_ms - previous.pts_ms
            for previous, current in zip(packet_list, packet_list[1:])
            if 0.0 < current.pts_ms - previous.pts_ms <= 1_000.0
        ]
        frame_duration_ms = (
            float(statistics.median(ordinary_deltas))
            if ordinary_deltas
            else 40.0
        )
    discontinuity_threshold_ms = max(
        MIN_DISCONTINUITY_MS,
        frame_duration_ms * FRAME_DISCONTINUITY_MULTIPLIER,
    )

    first_pts_ms = packet_list[0].pts_ms
    segment_start_ms = first_pts_ms
    segment_max_pts_ms = first_pts_ms
    segment_end_duration_ms = (
        packet_list[0].duration_ms or frame_duration_ms
    )
    raw_segments: list[tuple[float, float]] = []
    discontinuities: list[TimelineDiscontinuity] = []
    previous = packet_list[0]

    for packet_index, packet in enumerate(packet_list[1:], start=1):
        delta_ms = packet.pts_ms - previous.pts_ms
        if delta_ms > discontinuity_threshold_ms:
            raw_segments.append(
                (
                    segment_start_ms - first_pts_ms,
                    segment_max_pts_ms
                    + segment_end_duration_ms
                    - first_pts_ms,
                )
            )
            discontinuities.append(
                TimelineDiscontinuity(
                    packet_index=packet_index,
                    previous_pts_ms=previous.pts_ms,
                    current_pts_ms=packet.pts_ms,
                    delta_ms=delta_ms,
                    position=packet.position,
                )
            )
            segment_start_ms = packet.pts_ms
            segment_max_pts_ms = packet.pts_ms
            segment_end_duration_ms = packet.duration_ms or frame_duration_ms
        elif packet.pts_ms >= segment_max_pts_ms:
            segment_max_pts_ms = packet.pts_ms
            segment_end_duration_ms = packet.duration_ms or frame_duration_ms
        previous = packet

    raw_segments.append(
        (
            segment_start_ms - first_pts_ms,
            segment_max_pts_ms + segment_end_duration_ms - first_pts_ms,
        )
    )

    segments = _normalise_timeline_segments(
        (
            TimelineSegment(
                raw_start_ms=raw_start_ms,
                raw_end_ms=raw_end_ms,
                public_start_ms=0.0,
                public_end_ms=0.0,
            )
            for raw_start_ms, raw_end_ms in raw_segments
        ),
        frame_duration_ms,
    )

    return MediaTimelineIndex(
        source_path=source,
        source_size=size,
        source_mtime_ns=mtime_ns,
        first_pts_ms=first_pts_ms,
        frame_duration_ms=frame_duration_ms,
        segments=segments,
        discontinuities=tuple(discontinuities),
    )


def probe_media_timeline(
    source_path: str | os.PathLike[str],
    ffprobe_path: str | os.PathLike[str],
    *,
    timeout_seconds: float = 120.0,
) -> MediaTimelineIndex:
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
        os.fspath(source_path),
    ]
    creation_flags = (
        int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if os.name == "nt"
        else 0
    )
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TimelineProbeError(f"FFprobe时间轴扫描失败：{exc}") from exc
    if process.returncode != 0:
        detail = process.stderr.strip() or f"退出码 {process.returncode}"
        raise TimelineProbeError(f"FFprobe时间轴扫描失败：{detail}")
    packets = parse_ffprobe_packets(process.stdout.splitlines())
    return build_timeline_index(source_path, packets)


__all__ = [
    "MediaTimelineIndex",
    "PacketTimestamp",
    "TimelineDiscontinuity",
    "TimelineProbeError",
    "TimelineSegment",
    "build_timeline_index",
    "load_timeline_cache",
    "parse_ffprobe_packets",
    "probe_media_timeline",
    "save_timeline_cache",
    "timeline_cache_path",
]
