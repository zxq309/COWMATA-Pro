"""Wall-clock values are naive camera calendar milliseconds, never OS timezone.

Sensor timer, reference wall clock and each camera clock remain distinct.
Changing a viewport or source does not create an alignment anchor.
"""
from __future__ import annotations

import bisect
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

EPOCH = datetime(1970, 1, 1)


def wall_ms(value: str | datetime) -> float:
    if isinstance(value, str):
        value = value.strip()
        value = re.sub(r"(\d{2})_(\d{2})_(\d{2})", r"\1:\2:\3", value)
        value = datetime.fromisoformat(value)
    if value.tzinfo is not None:
        raise ValueError("现场时间需使用无时区的相机日历读数")
    return (value - EPOCH).total_seconds() * 1000


def wall_text(value: float, *, filename: bool = False) -> str:
    dt = EPOCH + timedelta(milliseconds=value)
    return dt.strftime("%Y-%m-%d %H_%M_%S" if filename else "%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class Anchor:
    source_ms: float
    reference_ms: float
    evidence: dict = field(default_factory=dict)


@dataclass
class ClockMap:
    anchors: list[Anchor] = field(default_factory=list)
    revision: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Breaks are source-clock ranges explicitly declared unconfirmed.
    breaks: list[tuple[float, float]] = field(default_factory=list)
    basis: str = "manual"

    def __post_init__(self):
        self.anchors.sort(key=lambda a: a.source_ms)
        for a in self.anchors:
            if not math.isfinite(a.source_ms) or not math.isfinite(a.reference_ms):
                raise ValueError("锚点必须是有效时间")
        for left, right in zip(self.anchors, self.anchors[1:]):
            if right.source_ms <= left.source_ms or right.reference_ms <= left.reference_ms:
                raise ValueError("锚点必须在两个时钟中都递增；跳变应分段而不能倒序连接")
        for left, right in self.breaks:
            if not math.isfinite(left + right) or left >= right:
                raise ValueError("未确认区间的结束必须晚于开始")

    @classmethod
    def from_dict(cls, data: dict):
        return cls([Anchor(**a) for a in data.get("anchors", [])],
                   data.get("revision") or uuid.uuid4().hex,
                   [tuple(b) for b in data.get("breaks", [])], data.get("basis", "manual"))

    def to_dict(self):
        return {"anchors": [asdict(a) for a in self.anchors], "revision": self.revision,
                "breaks": self.breaks, "basis": self.basis}

    @classmethod
    def from_capture(cls, motion, timezone_offset_minutes=480):
        # Epoch is UTC; the video index stores local, naive calendar milliseconds.
        offset = int(timezone_offset_minutes)
        if not -840 <= offset <= 840:
            raise ValueError("Invalid project timezone offset")
        return cls([Anchor(0, motion.epoch_at(0) + offset * 60000,
                           {"capture_timing": motion.capture_timing(), "timezone_offset_minutes": offset})],
                   basis="device_clock" if motion.version == 2 else "legacy_estimate")

    def with_anchor(self, source_ms: float, reference_ms: float, evidence: dict):
        # An automatic origin is not a human observation; do not combine it
        # with one human pin and accidentally certify a two-point calibration.
        remaining = [a for a in self.anchors if abs(a.source_ms - source_ms) > 0.001] if self.basis == "manual" else []
        return ClockMap(remaining + [Anchor(source_ms, reference_ms, evidence)], breaks=self.breaks.copy())

    def map(self, value: float, *, inverse: bool = False) -> float:
        if not math.isfinite(value) or not self.anchors:
            raise ValueError("没有可用的人工校准锚点")
        x = [a.reference_ms if inverse else a.source_ms for a in self.anchors]
        y = [a.source_ms if inverse else a.reference_ms for a in self.anchors]
        if len(x) == 1:
            return value + y[0] - x[0]
        i = min(max(bisect.bisect_right(x, value) - 1, 0), len(x) - 2)
        return y[i] + (value - x[i]) * (y[i + 1] - y[i]) / (x[i + 1] - x[i])

    def quality(self, source_ms: float) -> str:
        if not self.anchors:
            return "estimated"
        if any(a <= source_ms < b for a, b in self.breaks):
            return "unconfirmed"
        if self.basis != "manual":
            return self.basis
        if len(self.anchors) == 1:
            return "single_anchor"
        if self.anchors[0].source_ms <= source_ms <= self.anchors[-1].source_ms:
            return "interpolated"
        return "extrapolated"


@dataclass(frozen=True)
class VideoInterval:
    asset_id: str
    path: str
    camera: str
    wall_start: float
    wall_end: float
    media_start: float
    media_end: float
    verified: bool = False
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        if not all(math.isfinite(v) for v in (self.wall_start, self.wall_end, self.media_start, self.media_end)):
            raise ValueError("录像区间含无效时间")
        if self.wall_end <= self.wall_start or self.media_end <= self.media_start:
            raise ValueError("录像区间必须递增")

    def media_at(self, wall: float) -> float:
        return self.media_start + (wall - self.wall_start) * (self.media_end - self.media_start) / (self.wall_end - self.wall_start)

    def wall_at(self, media: float) -> float:
        return self.wall_start + (media - self.media_start) * (self.wall_end - self.wall_start) / (self.media_end - self.media_start)


class VideoTimeline:
    def __init__(self, intervals: list[VideoInterval], camera_maps: dict[str, ClockMap] | None = None):
        self.intervals = sorted(intervals, key=lambda s: (s.camera, s.wall_start, s.path))
        self.camera_maps = camera_maps or {}
        self._groups: dict[str, list[VideoInterval]] = {}
        for interval in self.intervals:
            self._groups.setdefault(interval.camera, []).append(interval)
        self._starts = {k: [v.wall_start for v in values] for k, values in self._groups.items()}
        # Prefix max-end handles nested overlaps; one bisect alone misses coverage.
        self._maxends = {}
        for camera, values in self._groups.items():
            end = -math.inf
            ends = []
            for value in values:
                end = max(end, value.wall_end)
                ends.append(end)
            self._maxends[camera] = ends

    @property
    def cameras(self):
        return list(self._groups)

    def camera_time(self, camera: str, reference_ms: float) -> float:
        mapping = self.camera_maps.get(camera)
        return mapping.map(reference_ms, inverse=True) if mapping and mapping.anchors else reference_ms

    def reference_time(self, camera: str, camera_ms: float) -> float:
        mapping = self.camera_maps.get(camera)
        return mapping.map(camera_ms) if mapping and mapping.anchors else camera_ms

    def locate(self, camera: str, reference_ms: float, prefer: str | None = None):
        values = self._groups.get(camera, [])
        wall = self.camera_time(camera, reference_ms)
        i = bisect.bisect_right(self._starts.get(camera, []), wall) - 1
        candidates = []
        while i >= 0 and self._maxends[camera][i] > wall:
            item = values[i]
            if item.wall_start <= wall < item.wall_end:
                candidates.append(item)
            i -= 1
        if not candidates:
            return None
        # Hold current source across overlaps; otherwise trusted, newest start,
        # deterministic path. Never concatenate overlapping duplicates.
        interval = min(candidates, key=lambda s: (s.asset_id != prefer, not s.verified, -s.wall_start, s.path))
        return interval, interval.media_at(wall)

    def next_start(self, camera: str, reference_ms: float) -> float | None:
        wall = self.camera_time(camera, reference_ms)
        i = bisect.bisect_right(self._starts.get(camera, []), wall)
        values = self._groups.get(camera, [])
        return self.reference_time(camera, values[i].wall_start) if i < len(values) else None

    def bounds(self) -> tuple[float, float] | None:
        if not self.intervals:
            return None
        return (min(self.reference_time(s.camera, s.wall_start) for s in self.intervals),
                max(self.reference_time(s.camera, s.wall_end) for s in self.intervals))


def intervals_from_rows(rows: list[dict], camera_overrides: dict[str, str] | None = None):
    from .demand import camera_name
    intervals = []
    overrides = camera_overrides or {}
    seen = set()
    for row in rows:
        if row["state"] not in {"ready", "review"} or not row["asset_id"]:
            continue
        metadata = row["metadata"]
        camera = camera_name(row, overrides)
        if (row["asset_id"], camera) in seen:
            continue
        seen.add((row["asset_id"], camera))
        for item in metadata.get("intervals", []):
            intervals.append(VideoInterval(row["asset_id"], row["path"], camera,
                                           item["wall_start"], item["wall_end"],
                                           item["media_start"], item["media_end"],
                                           item.get("verified", False), tuple(item.get("warnings", []))))
    return intervals
