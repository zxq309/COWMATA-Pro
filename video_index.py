from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ffmpeg_tools import probe_media
from ui_helpers import parse_video_filename


VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".ts",
    ".m2ts",
    ".ps",
    ".mpeg",
    ".mpg",
    ".h265",
    ".hevc",
}


@dataclass(frozen=True, slots=True)
class VideoIndexEntry:
    path: str
    start_wall_ms: int
    duration_ms: float
    size: int
    mtime_ns: int

    @property
    def end_wall_ms(self) -> float:
        return float(self.start_wall_ms) + float(self.duration_ms)


@dataclass(frozen=True, slots=True)
class VideoIndexBuildResult:
    root: str
    entries: tuple[VideoIndexEntry, ...]
    scanned: int
    missing_time: int
    failures: tuple[str, ...]


def _duration_ms(info: dict[str, Any]) -> float:
    candidates: list[float] = []
    raw_format = info.get("format", {})
    if isinstance(raw_format, dict):
        try:
            candidates.append(float(raw_format.get("duration", 0.0)) * 1000.0)
        except (TypeError, ValueError):
            pass
    raw_streams = info.get("streams", [])
    if isinstance(raw_streams, list):
        for stream in raw_streams:
            if not isinstance(stream, dict):
                continue
            try:
                candidates.append(float(stream.get("duration", 0.0)) * 1000.0)
            except (TypeError, ValueError):
                pass
    return max((value for value in candidates if value > 0.0), default=0.0)


def _read_duration_cache(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}
    records = payload.get("files", {}) if isinstance(payload, dict) else {}
    return records if isinstance(records, dict) else {}


def _write_duration_cache(
    path: Path | None, records: dict[str, dict[str, Any]]
) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"version": 1, "files": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def iter_video_files(root: str | os.PathLike[str]) -> list[Path]:
    folder = Path(root).resolve()
    return sorted(
        (
            path
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        ),
        key=lambda path: os.path.normcase(str(path)),
    )


def build_video_index(
    root: str | os.PathLike[str],
    data_epoch_ms: int | None,
    *,
    cache_path: str | os.PathLike[str] | None = None,
    probe: Callable[[str | os.PathLike[str]], dict[str, Any]] = probe_media,
    progress: Callable[[int, int, str], None] | None = None,
) -> VideoIndexBuildResult:
    """Index videos whose recording start can be read from the filename."""

    folder = Path(root).resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"视频目录不存在：{folder}")

    files = iter_video_files(folder)
    cache_file = Path(cache_path) if cache_path is not None else None
    cache = _read_duration_cache(cache_file)
    entries: list[VideoIndexEntry] = []
    failures: list[str] = []
    missing_time = 0

    for index, path in enumerate(files, 1):
        if progress is not None:
            progress(index, len(files), path.name)
        start_wall_ms = parse_video_filename(str(path), data_epoch_ms)
        if start_wall_ms is None:
            missing_time += 1
            continue
        try:
            stat = path.stat()
            key = os.path.normcase(str(path.resolve()))
            record = cache.get(key, {})
            duration = 0.0
            if (
                isinstance(record, dict)
                and int(record.get("size", -1)) == stat.st_size
                and int(record.get("mtime_ns", -1)) == stat.st_mtime_ns
            ):
                duration = float(record.get("duration_ms", 0.0))
            if duration <= 0.0:
                duration = _duration_ms(probe(path))
                if duration <= 0.0:
                    raise ValueError("无法取得有效视频时长")
                cache[key] = {
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "duration_ms": duration,
                }
            entries.append(
                VideoIndexEntry(
                    path=str(path),
                    start_wall_ms=int(start_wall_ms),
                    duration_ms=float(duration),
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                )
            )
        except Exception as exc:
            failures.append(f"{path.name}：{exc}")

    try:
        _write_duration_cache(cache_file, cache)
    except OSError:
        # A cache failure must not invalidate an otherwise usable index.
        pass

    entries.sort(key=lambda item: (item.start_wall_ms, item.path))
    return VideoIndexBuildResult(
        root=str(folder),
        entries=tuple(entries),
        scanned=len(files),
        missing_time=missing_time,
        failures=tuple(failures),
    )


def matching_videos(
    entries: Iterable[VideoIndexEntry],
    event_start_wall_ms: float,
    event_end_wall_ms: float | None = None,
) -> list[VideoIndexEntry]:
    """Return exact time-covering videos, preferring coverage of event start."""

    event_start = float(event_start_wall_ms)
    event_end = event_start if event_end_wall_ms is None else float(event_end_wall_ms)
    if event_end < event_start:
        event_start, event_end = event_end, event_start

    ranked: list[tuple[int, float, str, VideoIndexEntry]] = []
    for entry in entries:
        video_start = float(entry.start_wall_ms)
        video_end = entry.end_wall_ms
        covers_start = video_start <= event_start < video_end
        overlaps = event_end > video_start and event_start < video_end
        if not covers_start and not overlaps:
            continue
        ranked.append(
            (
                0 if covers_start else 1,
                abs(event_start - video_start),
                os.path.normcase(entry.path),
                entry,
            )
        )
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked]


def review_target_ms(
    data_start_wall_ms: float,
    event_start_relative_ms: float,
    video: VideoIndexEntry,
    *,
    preroll_ms: float = 5000.0,
    data_duration_ms: float | None = None,
) -> float:
    """Choose a safe data-relative preview point inside both data and video."""

    desired_wall = float(data_start_wall_ms) + float(event_start_relative_ms) - float(
        preroll_ms
    )
    target_wall = max(float(video.start_wall_ms), desired_wall)
    target = max(0.0, target_wall - float(data_start_wall_ms))
    if data_duration_ms is not None and data_duration_ms > 0.0:
        target = min(target, float(data_duration_ms))
    return target
