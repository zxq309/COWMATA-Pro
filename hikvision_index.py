from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

try:
    import cv2
except (ImportError, OSError):
    cv2 = None

from ffmpeg_tools import find_ffmpeg
from media_timeline import probe_media_timeline
from ui_helpers import BEIJING


HIKVISION_NAME = re.compile(r"^hiv\d{5}\.mp4$", re.IGNORECASE)


class HikvisionIndexError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HikvisionSegment:
    start_wall_ms: int
    duration_ms: float
    playback_offset_ms: float
    channel: str
    width: int
    height: int
    ocr_confidence: float
    ocr_text: str


def is_hikvision_storage_directory(
    root: str | os.PathLike[str], files: list[Path]
) -> bool:
    folder = Path(root)
    named = [path for path in files if HIKVISION_NAME.fullmatch(path.name)]
    return bool(named) and (
        (folder / "HIKWS").is_file()
        or any(folder.glob("backupinfo*.bin"))
        or len(named) >= max(2, len(files) // 2)
    )


def is_recorded_hikvision_block(path: str | os.PathLike[str]) -> bool:
    try:
        with Path(path).open("rb") as stream:
            return stream.read(4) == b"\x00\x00\x01\xba"
    except OSError:
        return False


def parse_osd_datetime_text(text: str) -> datetime | None:
    """Parse noisy Hikvision OSD OCR such as `...日116:58:06`."""

    normalized = str(text).translate(
        str.maketrans({"：": ":", "－": "-", "—": "-", "／": "/"})
    )
    date_match = re.search(
        r"(20\d{2})\D{0,4}(\d{1,2})\D{0,4}(\d{1,2})", normalized
    )
    if date_match is None:
        return None
    year, month, day = (int(value) for value in date_match.groups())
    tail = normalized[date_match.end() :]
    time_matches = list(
        re.finditer(r"(\d{1,4})\D+(\d{1,2})\D+(\d{1,2})", tail)
    )
    if not time_matches:
        return None
    raw_hour, raw_minute, raw_second = time_matches[-1].groups()
    # The outlined high-resolution font is occasionally read as `116` for
    # `16`; the real hour is always the final one or two digits.
    hour = int(raw_hour[-2:])
    minute = int(raw_minute)
    second = int(raw_second)
    try:
        return datetime(
            year, month, day, hour, minute, second, tzinfo=BEIJING
        )
    except ValueError:
        return None


def create_ocr_engine():
    if cv2 is None:
        raise HikvisionIndexError(
            "检测到海康裸录像目录，但图像解码库不可用。请运行“安装依赖”后重试。"
        )
    try:
        from rapidocr_onnxruntime import RapidOCR
    except (ImportError, OSError) as exc:
        raise HikvisionIndexError(
            "检测到海康裸录像目录，但OCR运行库不可用。请运行“安装依赖”后重试。"
        ) from exc
    try:
        return RapidOCR()
    except Exception as exc:
        raise HikvisionIndexError(f"海康角标OCR初始化失败：{exc}") from exc


def _timestamp_crops(image: np.ndarray) -> list[np.ndarray]:
    height, width = image.shape[:2]
    if width >= 1920:
        reference = (
            (0, 45, 850, 150),
            (0, 55, 900, 150),
            (0, 35, 1000, 165),
        )
        scale_x = width / 2560.0
        scale_y = height / 1440.0
    else:
        reference = (
            (0, 10, 650, 80),
            (0, 20, 650, 75),
            (0, 0, 700, 90),
        )
        scale_x = width / 1280.0
        scale_y = height / 720.0
    crops: list[np.ndarray] = []
    for x0, y0, x1, y1 in reference:
        left = max(0, int(round(x0 * scale_x)))
        top = max(0, int(round(y0 * scale_y)))
        right = min(width, int(round(x1 * scale_x)))
        bottom = min(height, int(round(y1 * scale_y)))
        if right > left and bottom > top:
            crops.append(image[top:bottom, left:right])
    return crops


def _recognition_values(result: Any) -> list[tuple[str, float]]:
    values: list[tuple[str, float]] = []
    if not isinstance(result, list):
        return values
    for item in result:
        if not isinstance(item, (list, tuple)):
            continue
        if len(item) == 2 and isinstance(item[0], str):
            text, score = item
        elif len(item) >= 3 and isinstance(item[1], str):
            text, score = item[1], item[2]
        else:
            continue
        try:
            values.append((str(text), float(score)))
        except (TypeError, ValueError):
            continue
    return values


def recognize_osd_datetime(
    image: np.ndarray,
    engine,
    expected_epoch_ms: int | None = None,
    minimum_wall_ms: int | None = None,
) -> tuple[datetime, float, str]:
    candidates: list[tuple[float, datetime, str]] = []
    crops = _timestamp_crops(image)

    def collect(values: list[tuple[str, float]]) -> None:
        combined = "".join(text for text, _score in values)
        if combined:
            values = values + [
                (
                    combined,
                    sum(score for _text, score in values) / len(values),
                )
            ]
        for text, score in values:
            parsed = parse_osd_datetime_text(text)
            if parsed is None:
                continue
            adjusted_score = score
            if expected_epoch_ms:
                distance_days = abs(
                    parsed.timestamp() - expected_epoch_ms / 1000.0
                ) / 86_400.0
                if distance_days > 45.0:
                    adjusted_score -= min(0.5, distance_days / 365.0)
            candidates.append((adjusted_score, parsed, text))

    def plausible_candidates() -> list[tuple[float, datetime, str]]:
        if minimum_wall_ms is None:
            return list(candidates)
        return [
            item
            for item in candidates
            if item[1].timestamp() * 1000.0 >= minimum_wall_ms - 120_000
        ]

    first_result, _elapsed = engine(
        crops[0], use_det=False, use_cls=False, use_rec=True
    )
    collect(_recognition_values(first_result))
    fast = plausible_candidates()
    if fast and max(item[0] for item in fast) >= 0.84:
        score, parsed, text = max(fast, key=lambda item: item[0])
        return parsed, max(0.0, score), text

    # The low-resolution channel is detected more reliably as two adjacent
    # text boxes (date + weekday/time) than as one recognition-only line.
    if image.shape[1] < 1920 and (
        not fast or max(item[0] for item in fast) < 0.84
    ):
        detected, _elapsed = engine(
            crops[0], use_det=True, use_cls=False, use_rec=True
        )
        collect(_recognition_values(detected))
        fast = plausible_candidates()
        if fast:
            score, parsed, text = max(fast, key=lambda item: item[0])
            return parsed, max(0.0, score), text

    for crop in crops[1:]:
        result, _elapsed = engine(
            crop, use_det=False, use_cls=False, use_rec=True
        )
        collect(_recognition_values(result))
    needs_detector = not candidates
    if candidates and minimum_wall_ms is not None:
        newest = max(item[1].timestamp() * 1000.0 for item in candidates)
        needs_detector = newest < minimum_wall_ms - 120_000
    if needs_detector:
        for crop in crops:
            result, _elapsed = engine(
                crop, use_det=True, use_cls=False, use_rec=True
            )
            collect(_recognition_values(result))
            if not isinstance(result, list) or not candidates:
                continue
            dated = max(candidates, key=lambda item: item[0])[1]
            positioned: list[tuple[float, str, float]] = []
            for item in result:
                if not isinstance(item, (list, tuple)) or len(item) < 3:
                    continue
                box, text, raw_score = item[0], item[1], item[2]
                try:
                    center_x = sum(float(point[0]) for point in box) / len(box)
                    score = float(raw_score)
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                positioned.append((center_x, str(text), score))
            positioned.sort(key=lambda item: item[0])
            groups: list[str] = []
            scores: list[float] = []
            for center_x, text, score in positioned:
                if center_x < crop.shape[1] * 0.58:
                    continue
                values = re.findall(r"\d+", text)
                if values:
                    groups.extend(values)
                    scores.append(score)
            if len(groups) >= 3:
                try:
                    hybrid = dated.replace(
                        hour=int(groups[0][-2:]),
                        minute=int(groups[1][-2:]),
                        second=int(groups[-1][-2:]),
                    )
                except ValueError:
                    continue
                candidates.append(
                    (
                        sum(scores) / len(scores),
                        hybrid,
                        "detector:" + "|".join(groups),
                    )
                )
    if not candidates:
        raise HikvisionIndexError("无法识别画面左上角的日期时间")
    plausible = plausible_candidates()
    if minimum_wall_ms is not None:
        if not plausible:
            raise HikvisionIndexError(
                "角标时间早于同通道上一录像段，拒绝写入不可靠索引"
            )
    score, parsed, text = max(plausible, key=lambda item: item[0])
    return parsed, max(0.0, score), text


def recognize_channel(image: np.ndarray, engine) -> str:
    height, width = image.shape[:2]
    crop = image[
        int(height * 0.78) : height,
        int(width * 0.48) : width,
    ]
    result, _elapsed = engine(crop, use_det=True, use_cls=False, use_rec=True)
    values = sorted(_recognition_values(result), key=lambda item: item[1], reverse=True)
    for text, _score in values:
        match = re.search(r"通道\s*([0-9]{1,3})", text)
        if match:
            return f"通道{int(match.group(1))}"
    return f"{width}x{height}"


def extract_frame(
    path: str | os.PathLike[str],
    ffmpeg_path: str | os.PathLike[str],
    seek_ms: float = 0.0,
) -> np.ndarray:
    if cv2 is None:
        raise HikvisionIndexError(
            "海康录像图像解码库不可用，请运行“安装依赖”后重试"
        )
    command = [
        os.fspath(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "mpeg",
        "-i",
        os.fspath(path),
    ]
    if seek_ms > 0.0:
        command.extend(("-ss", f"{seek_ms / 1000.0:.3f}"))
    command.extend(("-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"))
    creation_flags = (
        int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if os.name == "nt"
        else 0
    )
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            timeout=90,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HikvisionIndexError(f"无法抽取海康录像帧：{exc}") from exc
    if process.returncode != 0 or not process.stdout:
        details = process.stderr.decode("utf-8", errors="replace").strip()
        raise HikvisionIndexError(details or "海康录像没有可解码画面")
    encoded = np.frombuffer(process.stdout, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise HikvisionIndexError("海康录像首帧解码失败")
    return image


def index_hikvision_file(
    path: str | os.PathLike[str],
    engine,
    expected_epoch_ms: int | None = None,
    minimum_wall_by_channel: dict[str, int] | None = None,
    channel_by_resolution: dict[tuple[int, int], str] | None = None,
) -> list[HikvisionSegment]:
    ffmpeg, ffprobe = find_ffmpeg()
    timeline = probe_media_timeline(path, ffprobe, timeout_seconds=180.0)
    usable = [
        segment
        for segment in timeline.segments
        if segment.public_end_ms - segment.public_start_ms >= 1.0
    ]
    if not usable:
        raise HikvisionIndexError("录像块没有可复核的连续画面")

    first_image = extract_frame(path, ffmpeg, usable[0].raw_start_ms)
    resolution = (int(first_image.shape[1]), int(first_image.shape[0]))
    channel = (
        channel_by_resolution.get(resolution, "")
        if channel_by_resolution is not None
        else ""
    )
    if not channel:
        channel = recognize_channel(first_image, engine)
        if channel_by_resolution is not None:
            channel_by_resolution[resolution] = channel
    results: list[HikvisionSegment] = []
    for index, segment in enumerate(usable):
        image = (
            first_image
            if index == 0
            else extract_frame(path, ffmpeg, segment.raw_start_ms + 250.0)
        )
        try:
            timestamp, confidence, text = recognize_osd_datetime(
                image,
                engine,
                expected_epoch_ms,
                (
                    minimum_wall_by_channel.get(channel)
                    if minimum_wall_by_channel is not None
                    else None
                ),
            )
        except HikvisionIndexError:
            retry = extract_frame(path, ffmpeg, segment.raw_start_ms + 1000.0)
            timestamp, confidence, text = recognize_osd_datetime(
                retry,
                engine,
                expected_epoch_ms,
                (
                    minimum_wall_by_channel.get(channel)
                    if minimum_wall_by_channel is not None
                    else None
                ),
            )
        results.append(
            HikvisionSegment(
                start_wall_ms=int(round(timestamp.timestamp() * 1000.0)),
                duration_ms=float(
                    segment.public_end_ms - segment.public_start_ms
                ),
                playback_offset_ms=float(segment.public_start_ms),
                channel=channel,
                width=int(image.shape[1]),
                height=int(image.shape[0]),
                ocr_confidence=float(confidence),
                ocr_text=text,
            )
        )
        if minimum_wall_by_channel is not None:
            value = int(round(timestamp.timestamp() * 1000.0))
            minimum_wall_by_channel[channel] = max(
                value, minimum_wall_by_channel.get(channel, value)
            )
    return results


__all__ = [
    "HIKVISION_NAME",
    "HikvisionIndexError",
    "HikvisionSegment",
    "create_ocr_engine",
    "index_hikvision_file",
    "is_hikvision_storage_directory",
    "is_recorded_hikvision_block",
    "parse_osd_datetime_text",
    "recognize_channel",
    "recognize_osd_datetime",
]
