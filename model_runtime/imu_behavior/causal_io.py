"""Gap-safe V2 preprocessing for current causal checkpoints."""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np


FRAME_DTYPE = np.dtype(
    [("elapsed_ms", "<u4"), ("values", "<i2", (9,))], align=False
)
FRAME_BYTES = 22
TARGET_DT_MS = 20
GAP_THRESHOLD_MS = 40
MIN_SEGMENT_SAMPLES = 256
MAG_RAW_TO_IMU = np.asarray(
    [[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]],
    dtype=np.float32,
)
DEFAULT_CALIBRATION: dict[str, object] = {
    "acc_divisor": 4096.0,
    "acc_bias_counts": [0.0, 0.0, 0.0],
    "gyro_divisor": 32.0,
    "gyro_bias_counts": [0.0, 0.0, 0.0],
    "mag_divisor": 1000.0,
}


def _phase_score(payload: bytes, offset: int, max_frames: int = 8000) -> float:
    count = (len(payload) - offset) // FRAME_BYTES
    if count < 10:
        return float("-inf")
    sample_count = min(count, max_frames)
    frames = np.frombuffer(
        memoryview(payload)[offset : offset + sample_count * FRAME_BYTES],
        dtype=FRAME_DTYPE,
        count=sample_count,
    )
    time_ms = frames["elapsed_ms"].astype(np.int64)
    dt = np.diff(time_ms)
    positive = float(np.mean(dt > 0))
    nominal = float(np.mean((dt >= 10) & (dt <= 40)))
    plausible = float(np.mean((dt >= 1) & (dt <= 1000)))
    positive_dt = dt[dt > 0]
    cadence = (
        float(np.exp(-abs(float(np.median(positive_dt)) - 20.0) / 20.0))
        if positive_dt.size
        else 0.0
    )
    return 0.55 * nominal + 0.25 * positive + 0.10 * plausible + 0.10 * cadence


def _first_plausible_frame(time_ms: np.ndarray, run: int = 8) -> int:
    if time_ms.size <= run:
        return 0
    dt = np.diff(time_ms.astype(np.int64))
    good = ((dt >= 5) & (dt <= 100)).astype(np.int16)
    hits = np.convolve(good, np.ones(run, dtype=np.int16), mode="valid")
    matched = np.flatnonzero(hits == run)
    return int(matched[0]) if matched.size else 0


def decode_v2_payload(payload: bytes) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    if len(payload) < FRAME_BYTES * 10:
        raise ValueError("V2 IMU 数据太短")
    scores = np.asarray([_phase_score(payload, offset) for offset in range(FRAME_BYTES)])
    best_offset = int(np.argmax(scores))
    best_score = float(scores[best_offset])
    if not np.isfinite(best_score) or best_score < 0.80:
        raise ValueError(f"无法恢复可信的 V2 帧相位（score={best_score:.3f}）")
    frame_count = (len(payload) - best_offset) // FRAME_BYTES
    trailing = (len(payload) - best_offset) % FRAME_BYTES
    frames = np.frombuffer(
        memoryview(payload)[best_offset : best_offset + frame_count * FRAME_BYTES],
        dtype=FRAME_DTYPE,
        count=frame_count,
    )
    time_ms = frames["elapsed_ms"].astype(np.int64)
    leading = _first_plausible_frame(time_ms)
    if leading:
        frames = frames[leading:]
        time_ms = time_ms[leading:]
    return (
        time_ms.copy(),
        np.asarray(frames["values"], dtype=np.int16).copy(),
        {
            "phase_score": best_score,
            "dropped_prefix_bytes": best_offset + leading * FRAME_BYTES,
            "dropped_trailing_bytes": trailing,
            "source_frames": int(frames.size),
        },
    )


def _calibration_candidate(
    source: object,
    *,
    device: str,
    session_id: str,
) -> Mapping[str, object] | None:
    if not isinstance(source, Mapping):
        return None
    if all(key in source for key in ("acc_divisor", "gyro_divisor", "mag_divisor")):
        return source
    devices = source.get("devices")
    if isinstance(devices, Mapping) and isinstance(devices.get(device), Mapping):
        return devices[device]
    sessions = source.get("sessions")
    if isinstance(sessions, list):
        for item in sessions:
            if not isinstance(item, Mapping):
                continue
            same_device = str(item.get("device_mac", item.get("device", ""))) == device
            same_session = str(item.get("session_id", "")) == session_id
            if same_device and same_session:
                return item
    default = source.get("default")
    return default if isinstance(default, Mapping) else None


def resolve_calibration(
    checkpoint: Mapping[str, object],
    *,
    device: str,
    session_id: str,
) -> tuple[dict[str, object], str]:
    for key in ("sensor_calibration", "calibration_manifest"):
        candidate = _calibration_candidate(
            checkpoint.get(key), device=device, session_id=session_id
        )
        if candidate is not None:
            return {**DEFAULT_CALIBRATION, **dict(candidate)}, f"checkpoint:{key}"
    return dict(DEFAULT_CALIBRATION), "standard_device_defaults"


def _as_vector(value: object, name: str) -> np.ndarray:
    result = np.asarray(value, dtype=np.float32)
    if result.shape != (3,):
        raise ValueError(f"{name} 必须包含 3 个数值")
    return result


def physical_values(raw_values: np.ndarray, calibration: Mapping[str, object]) -> np.ndarray:
    acc_divisor = float(calibration["acc_divisor"])
    gyro_divisor = float(calibration["gyro_divisor"])
    mag_divisor = float(calibration["mag_divisor"])
    if min(acc_divisor, gyro_divisor, mag_divisor) <= 0:
        raise ValueError("传感器灵敏度除数必须为正数")
    values = raw_values.astype(np.float32, copy=False)
    acc = (
        values[:, 0:3]
        - _as_vector(calibration.get("acc_bias_counts", [0.0] * 3), "acc_bias_counts")
    ) / acc_divisor
    gyro = (
        values[:, 3:6]
        - _as_vector(calibration.get("gyro_bias_counts", [0.0] * 3), "gyro_bias_counts")
    ) / gyro_divisor
    mag = (values[:, 6:9] @ MAG_RAW_TO_IMU.T) / mag_divisor
    return np.concatenate((acc, gyro, mag), axis=1).astype(np.float32, copy=False)


def _contiguous_slices(time_ms: np.ndarray) -> list[slice]:
    dt = np.diff(np.asarray(time_ms, dtype=np.int64))
    boundaries = np.flatnonzero((dt <= 0) | (dt > GAP_THRESHOLD_MS)) + 1
    starts = np.concatenate(([0], boundaries))
    stops = np.concatenate((boundaries, [time_ms.size]))
    return [
        slice(int(start), int(stop))
        for start, stop in zip(starts, stops)
        if stop > start
    ]


def _quality_flag(raw_time: np.ndarray, grid: np.ndarray) -> np.ndarray:
    right = np.searchsorted(raw_time, grid, side="left")
    right = np.clip(right, 1, len(raw_time) - 1)
    local_dt = raw_time[right] - raw_time[right - 1]
    return (np.abs(local_dt - TARGET_DT_MS) > 5).astype(np.float32)


def resample_segments(
    elapsed_ms: np.ndarray,
    physical: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, list[tuple[int, int]]]:
    feature_chunks: list[np.ndarray] = []
    time_chunks: list[np.ndarray] = []
    ranges: list[tuple[int, int]] = []
    offset = 0
    for run in _contiguous_slices(elapsed_ms):
        raw_time = elapsed_ms[run].astype(np.float64)
        raw_values = physical[run]
        if raw_time.size < MIN_SEGMENT_SAMPLES:
            continue
        start = int(math.ceil(raw_time[0] / TARGET_DT_MS) * TARGET_DT_MS)
        stop = int(math.floor(raw_time[-1] / TARGET_DT_MS) * TARGET_DT_MS)
        if stop - start < (MIN_SEGMENT_SAMPLES - 1) * TARGET_DT_MS:
            continue
        grid = np.arange(start, stop + 1, TARGET_DT_MS, dtype=np.int64)
        channels = np.column_stack(
            [
                np.interp(grid, raw_time, raw_values[:, index])
                for index in range(raw_values.shape[1])
            ]
        ).astype(np.float32)
        magnitudes = np.column_stack(
            (
                np.linalg.norm(channels[:, 0:3], axis=1),
                np.linalg.norm(channels[:, 3:6], axis=1),
                np.linalg.norm(channels[:, 6:9], axis=1),
            )
        ).astype(np.float32)
        quality = _quality_flag(raw_time, grid)[:, None]
        features = np.concatenate((channels, magnitudes, quality), axis=1).astype(
            np.float32
        )
        feature_chunks.append(features)
        time_chunks.append(grid)
        ranges.append((offset, offset + len(grid)))
        offset += len(grid)
    if not feature_chunks:
        raise ValueError("没有长度达到 5.12 秒的连续 V2 IMU 片段")
    return np.concatenate(time_chunks), np.concatenate(feature_chunks), ranges


def load_causal_features(
    path: Path,
    checkpoint: Mapping[str, object],
) -> tuple[np.ndarray, np.ndarray, list[tuple[int, int]], dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        document = json.load(handle)
    if int(document.get("version", 2)) != 2:
        raise ValueError("新版因果模型当前只支持含设备时间戳的 V2 九轴 JSON")
    encoded = document.get("imu")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("JSON 缺少非空 imu Base64 字段")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except ValueError:
        payload = base64.b64decode("".join(encoded.split()), validate=True)
    elapsed_ms, raw_values, diagnostics = decode_v2_payload(payload)
    device = str(
        document.get("device")
        or document.get("device_id")
        or document.get("mac")
        or ""
    )
    calibration, calibration_source = resolve_calibration(
        checkpoint, device=device, session_id=path.stem
    )
    times, features, ranges = resample_segments(
        elapsed_ms, physical_values(raw_values, calibration)
    )
    metadata: dict[str, Any] = {
        **diagnostics,
        "create_time": document.get("create_time"),
        "device": device,
        "uid": document.get("uid", ""),
        "version": 2,
        "regular_frames": int(len(times)),
        "segments": len(ranges),
        "calibration_source": calibration_source,
        "calibration_warning": (
            "checkpoint 未包含该设备的逐会话校准，使用标准 4096/32/1000 灵敏度；"
            "新设备结果必须人工复核。"
            if calibration_source == "standard_device_defaults"
            else ""
        ),
    }
    return times, features, ranges, metadata
