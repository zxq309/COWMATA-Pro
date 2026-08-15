from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ffmpeg_tools import probe_media
from hikvision_index import (
    create_ocr_engine,
    index_hikvision_file,
    is_hikvision_storage_directory,
    is_recorded_hikvision_block,
)
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


class VideoIndexCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VideoIndexEntry:
    path: str
    start_wall_ms: int
    duration_ms: float
    size: int
    mtime_ns: int
    playback_offset_ms: float = 0.0
    channel: str = ""
    width: int = 0
    height: int = 0
    time_source: str = "filename"
    time_confidence: float = 1.0

    @property
    def end_wall_ms(self) -> float:
        return float(self.start_wall_ms) + float(self.duration_ms)

    @property
    def effective_video_start_wall_ms(self) -> float:
        return float(self.start_wall_ms) - float(self.playback_offset_ms)


@dataclass(frozen=True, slots=True)
class VideoIndexBuildResult:
    root: str
    entries: tuple[VideoIndexEntry, ...]
    scanned: int
    missing_time: int
    failures: tuple[str, ...]
    index_kind: str = "filename"
    channels: tuple[str, ...] = ()


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
        json.dumps({"version": 2, "files": records}, ensure_ascii=False, indent=2),
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
    cancelled: Callable[[], bool] | None = None,
) -> VideoIndexBuildResult:
    """Index ordinary timestamped videos or raw Hikvision recording blocks."""

    folder = Path(root).resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"视频目录不存在：{folder}")

    files = iter_video_files(folder)
    cache_file = Path(cache_path) if cache_path is not None else None
    cache = _read_duration_cache(cache_file)
    entries: list[VideoIndexEntry] = []
    failures: list[str] = []
    missing_time = 0
    hikvision_mode = is_hikvision_storage_directory(folder, files)
    ocr_engine = None
    minimum_wall_by_channel: dict[str, int] = {}
    channel_by_resolution: dict[tuple[int, int], str] = {}

    for index, path in enumerate(files, 1):
        if cancelled is not None and cancelled():
            try:
                _write_duration_cache(cache_file, cache)
            except OSError:
                pass
            raise VideoIndexCancelled("用户取消了视频目录索引")
        if progress is not None:
            progress(index, len(files), path.name)
        try:
            stat = path.stat()
            key = os.path.normcase(str(path.resolve()))
            record = cache.get(key, {})
            record_matches = bool(
                isinstance(record, dict)
                and int(record.get("size", -1)) == stat.st_size
                and int(record.get("mtime_ns", -1)) == stat.st_mtime_ns
            )
            if hikvision_mode:
                if not is_recorded_hikvision_block(path):
                    missing_time += 1
                    continue
                raw_segments = (
                    record.get("hikvision_segments", [])
                    if record_matches
                    and int(record.get("hikvision_ocr_version", 0)) == 1
                    else []
                )
                if not isinstance(raw_segments, list) or not raw_segments:
                    if ocr_engine is None:
                        ocr_engine = create_ocr_engine()
                    segments = index_hikvision_file(
                        path,
                        ocr_engine,
                        data_epoch_ms,
                        minimum_wall_by_channel,
                        channel_by_resolution,
                    )
                    raw_segments = [
                        {
                            "start_wall_ms": item.start_wall_ms,
                            "duration_ms": item.duration_ms,
                            "playback_offset_ms": item.playback_offset_ms,
                            "channel": item.channel,
                            "width": item.width,
                            "height": item.height,
                            "ocr_confidence": item.ocr_confidence,
                            "ocr_text": item.ocr_text,
                        }
                        for item in segments
                    ]
                    cache[key] = {
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "hikvision_ocr_version": 1,
                        "hikvision_segments": raw_segments,
                    }
                    if index % 5 == 0:
                        try:
                            _write_duration_cache(cache_file, cache)
                        except OSError:
                            pass
                for raw in raw_segments:
                    if not isinstance(raw, dict):
                        continue
                    channel = str(raw.get("channel", ""))
                    start = int(raw["start_wall_ms"])
                    width = int(raw.get("width", 0))
                    height = int(raw.get("height", 0))
                    if channel and width > 0 and height > 0:
                        channel_by_resolution.setdefault(
                            (width, height), channel
                        )
                    minimum_wall_by_channel[channel] = max(
                        start, minimum_wall_by_channel.get(channel, start)
                    )
                    entries.append(
                        VideoIndexEntry(
                            path=str(path),
                            start_wall_ms=start,
                            duration_ms=float(raw["duration_ms"]),
                            size=stat.st_size,
                            mtime_ns=stat.st_mtime_ns,
                            playback_offset_ms=float(
                                raw.get("playback_offset_ms", 0.0)
                            ),
                            channel=channel,
                            width=width,
                            height=height,
                            time_source="hikvision_osd",
                            time_confidence=float(
                                raw.get("ocr_confidence", 0.0)
                            ),
                        )
                    )
                continue

            start_wall_ms = parse_video_filename(str(path), data_epoch_ms)
            if start_wall_ms is None:
                missing_time += 1
                continue
            duration = 0.0
            if record_matches:
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
        index_kind="hikvision_osd" if hikvision_mode else "filename",
        channels=tuple(sorted({entry.channel for entry in entries if entry.channel})),
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

    ranked: list[tuple[int, float, int, str, VideoIndexEntry]] = []
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
                -(entry.width * entry.height),
                os.path.normcase(entry.path),
                entry,
            )
        )
    ranked.sort(key=lambda item: item[:3])
    return [item[4] for item in ranked]


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
