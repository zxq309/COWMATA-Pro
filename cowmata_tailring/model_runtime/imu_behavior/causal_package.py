"""Adapter for the delivered 2026-08-15 causal prediction package.

The reference predictor consumes the calibrated ``features.npy`` arrays from
the package catalog.  This module resolves those arrays for an opened raw JSON
and converts the reference dense probabilities into the interval contract used
by the annotation UI.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from .postprocess import clean_short_body_runs
from .schema import LABEL_LAYER, LABEL_ZH

PACKAGE_DIRECTORY_NAME = "牛尾环IMU_20260815_整理包"
CATALOG_RELATIVE_PATH = Path("01_关键训练数据") / "ssl_cache" / "cache_catalog.json"
EVENT_POSTPROCESS: dict[str, dict[str, int]] = {
    "STANDING_UP": {"min_ms": 500, "merge_gap_ms": 1500},
    "LYING_DOWN": {"min_ms": 500, "merge_gap_ms": 1500},
    "URINATION": {"min_ms": 1500, "merge_gap_ms": 3000},
    "DEFECATION": {"min_ms": 1500, "merge_gap_ms": 3000},
    "TAIL_RAISED": {"min_ms": 1000, "merge_gap_ms": 2000},
    "TAIL_WAGGING": {"min_ms": 500, "merge_gap_ms": 1000},
}
RESEARCH_EVENT_CODES = {"DEFECATION", "TAIL_RAISED", "TAIL_WAGGING"}
BODY_CODES = ("STANDING", "LYING", "WALKING")


def _candidate_package_roots(checkpoint_path: Path) -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("CATTLE_IMU_PACKAGE_ROOT", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    resolved = checkpoint_path.resolve()
    candidates.extend(resolved.parents)
    # Normal workspace layout: package and annotation tool are sibling folders.
    try:
        workspace = Path(__file__).resolve().parents[3]
        candidates.append(workspace / PACKAGE_DIRECTORY_NAME)
    except IndexError:
        pass
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.resolve(strict=False)))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def find_package_root(checkpoint_path: Path) -> Path | None:
    for candidate in _candidate_package_roots(checkpoint_path):
        if (candidate / CATALOG_RELATIVE_PATH).is_file():
            return candidate.resolve()
    return None


@lru_cache(maxsize=4)
def _catalog_sessions(catalog_path: str, modified_ns: int) -> tuple[dict[str, Any], ...]:
    del modified_ns  # Included in the cache key so catalog replacements are re-read.
    with Path(catalog_path).open("r", encoding="utf-8-sig") as handle:
        document = json.load(handle)
    sessions = document.get("sessions")
    if not isinstance(sessions, list):
        raise ValueError(f"新版预测目录中的 catalog 缺少 sessions: {catalog_path}")
    return tuple(dict(item) for item in sessions if isinstance(item, Mapping))


def read_v2_identity(imu_path: Path) -> dict[str, Any]:
    with imu_path.open("r", encoding="utf-8-sig") as handle:
        document = json.load(handle)
    if int(document.get("version", 2)) != 2:
        raise ValueError("新版因果模型当前只支持含设备时间戳的 V2 九轴 JSON")
    device = str(
        document.get("device")
        or document.get("device_id")
        or document.get("mac")
        or ""
    ).strip()
    if not device:
        raise ValueError("九轴 JSON 缺少 device/device_id/mac，无法匹配训练缓存")
    return {
        "device": device,
        "session_id": imu_path.stem,
        "create_time": document.get("create_time"),
        "uid": document.get("uid", ""),
    }


def _resolve_cache_path(package_root: Path, value: object) -> Path:
    source = Path(str(value).replace("\\", "/"))
    if source.is_absolute() and source.is_dir():
        return source
    candidate = package_root / source
    if candidate.is_dir():
        return candidate
    parts = source.parts
    if "01_关键训练数据" in parts:
        relative = Path(*parts[parts.index("01_关键训练数据") :])
        candidate = package_root / relative
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"catalog 指向的训练缓存不存在: {value}")


def load_package_features(
    checkpoint_path: Path,
    imu_path: Path,
) -> tuple[np.ndarray, dict[str, Any]] | None:
    """Return the exact calibrated cache for ``imu_path`` when delivered."""

    package_root = find_package_root(checkpoint_path)
    if package_root is None:
        return None
    identity = read_v2_identity(imu_path)
    catalog_path = package_root / CATALOG_RELATIVE_PATH
    sessions = _catalog_sessions(
        str(catalog_path), catalog_path.stat().st_mtime_ns
    )
    matched = next(
        (
            item
            for item in sessions
            if str(item.get("device_mac", "")).casefold()
            == str(identity["device"]).casefold()
            and str(item.get("session_id", "")) == str(identity["session_id"])
        ),
        None,
    )
    if matched is None:
        return None
    segments = matched.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("匹配到的训练缓存缺少连续片段时间信息")
    cache_directory = _resolve_cache_path(package_root, matched.get("cache_path", ""))
    features_path = cache_directory / "features.npy"
    if not features_path.is_file():
        raise FileNotFoundError(f"匹配到的训练特征不存在: {features_path}")
    features = np.load(features_path, mmap_mode="r")
    if features.ndim != 2 or features.shape[1] != 13:
        raise ValueError(
            f"训练缓存必须是 (T, 13)，实际为 {tuple(features.shape)}: {features_path}"
        )
    expected_frames = int(matched.get("frames", len(features)))
    if len(features) != expected_frames:
        raise ValueError(
            f"训练缓存帧数与 catalog 不一致: {len(features)} != {expected_frames}"
        )
    metadata = {
        **identity,
        "package_root": str(package_root),
        "catalog_path": str(catalog_path),
        "features_path": str(features_path),
        "device_key": str(matched.get("device_key", "")),
        "cow_id": str(matched.get("cow_id", "")),
        "segments_detail": [dict(item) for item in segments],
        "segments": len(segments),
        "calibration_source": "delivered_training_cache",
        "calibration_warning": "",
    }
    return features, metadata


def frame_times_ms(indices: np.ndarray, segments: Sequence[Mapping[str, object]]) -> np.ndarray:
    """Match the reference ``predict_continuous.py`` frame-to-time mapping."""

    requested = np.asarray(indices, dtype=np.int64)
    output = np.full(requested.shape, -1, dtype=np.int64)
    for segment in segments:
        start_index = int(segment["start_index"])
        stop_index = int(segment["stop_index"])
        selected = (requested >= start_index) & (requested < stop_index)
        if not np.any(selected):
            continue
        start_ms = float(segment["start_ms"])
        stop_ms = float(segment["stop_ms"])
        scale = (stop_ms - start_ms) / max(stop_index - start_index, 1)
        output[selected] = np.rint(
            start_ms + (requested[selected] - start_index) * scale
        ).astype(np.int64)
    if np.any(output < 0):
        raise ValueError("catalog segments 未覆盖全部模型输出帧")
    return output


def _runs(labels: np.ndarray) -> list[tuple[int, int, int]]:
    if labels.size == 0:
        return []
    changes = np.flatnonzero(np.r_[True, labels[1:] != labels[:-1], True])
    return [
        (int(labels[start]), int(start), int(stop))
        for start, stop in zip(changes[:-1], changes[1:])
    ]


def _wall_time(create_time_ms: int | float | None, relative_ms: int) -> str:
    if not create_time_ms:
        return ""
    base = datetime.fromtimestamp(
        float(create_time_ms) / 1000.0, tz=ZoneInfo("Asia/Shanghai")
    )
    return (base + timedelta(milliseconds=relative_ms)).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3]


def _interval_row(
    code: str,
    start_ms: int,
    end_ms: int,
    scores: np.ndarray,
    create_time_ms: int | float | None,
    **metadata: object,
) -> dict[str, object]:
    selected = np.asarray(scores, dtype=np.float64)
    return {
        "layer": LABEL_LAYER[code],
        "code": code,
        "label": LABEL_ZH[code],
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "duration_s": round((end_ms - start_ms) / 1000.0, 3),
        "confidence_mean": round(float(selected.mean()), 6),
        "confidence_max": round(float(selected.max()), 6),
        "start_wall_bj": _wall_time(create_time_ms, start_ms),
        "end_wall_bj": _wall_time(create_time_ms, end_ms),
        **metadata,
    }


def _body_intervals(
    times_ms: np.ndarray,
    posture_probability: np.ndarray,
    walking_probability: np.ndarray,
    walking_threshold: float,
    create_time_ms: int | float | None,
    step_ms: int,
) -> list[dict[str, object]]:
    posture = np.asarray(posture_probability, dtype=np.float64)
    walking = np.asarray(walking_probability, dtype=np.float64).reshape(-1)
    labels = np.where(
        np.argmax(posture, axis=1) == 1,
        1,
        np.where(walking >= walking_threshold, 2, 0),
    ).astype(np.int8)
    confidence = np.column_stack((posture[:, 0], posture[:, 1], walking))
    # The reference defines dense display labels but not interval smoothing.
    # Remove only isolated sub-second flashes before presenting intervals.
    labels = clean_short_body_runs(labels, confidence, minimum_steps=2)
    rows: list[dict[str, object]] = []
    for class_index, start, stop in _runs(labels):
        code = BODY_CODES[class_index]
        selected = confidence[start:stop, class_index]
        start_ms = int(times_ms[start])
        end_ms = int(times_ms[stop - 1] + step_ms)
        rows.append(
            _interval_row(
                code,
                start_ms,
                end_ms,
                selected,
                create_time_ms,
                candidate_type="continuous_state",
                review_priority="standard",
                model_support="formal",
                review_recommended=bool(float(selected.mean()) >= 0.60),
                source_head=("locomotion" if code == "WALKING" else "posture"),
                decision_threshold=(walking_threshold if code == "WALKING" else 0.5),
                uncertainty=round(1.0 - float(selected.mean()), 6),
            )
        )
    return rows


def _event_intervals(
    times_ms: np.ndarray,
    event_probability: np.ndarray,
    event_codes: Sequence[str],
    thresholds: Mapping[str, float],
    create_time_ms: int | float | None,
    step_ms: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    events = np.asarray(event_probability, dtype=np.float64)
    for class_index, code in enumerate(event_codes):
        if code not in EVENT_POSTPROCESS:
            continue
        threshold = float(thresholds.get(code, 0.5))
        config = EVENT_POSTPROCESS[code]
        scores = events[:, class_index]
        selected = np.flatnonzero(scores >= threshold)
        if selected.size == 0:
            continue
        groups: list[np.ndarray] = []
        group_start = 0
        for position in range(1, len(selected)):
            if (
                times_ms[selected[position]] - times_ms[selected[position - 1]]
                > config["merge_gap_ms"]
            ):
                groups.append(selected[group_start:position])
                group_start = position
        groups.append(selected[group_start:])
        for group in groups:
            start_ms = int(times_ms[group[0]])
            end_ms = int(times_ms[group[-1]] + step_ms)
            if end_ms - start_ms < config["min_ms"]:
                continue
            candidate_scores = scores[group]
            support = "research" if code in RESEARCH_EVENT_CODES else "formal"
            mean_score = float(candidate_scores.mean())
            priority = (
                "research"
                if support == "research"
                else "high_score"
                if mean_score >= min(0.99, threshold + 0.15)
                else "uncertain"
            )
            rows.append(
                _interval_row(
                    code,
                    start_ms,
                    end_ms,
                    candidate_scores,
                    create_time_ms,
                    candidate_type="event_candidate",
                    review_priority=priority,
                    model_support=support,
                    review_recommended=(support == "formal"),
                    source_head=f"event:{code}",
                    decision_threshold=threshold,
                    uncertainty=round(abs(mean_score - threshold), 6),
                )
            )
    return rows


def package_prediction_intervals(
    times_ms: np.ndarray,
    posture_probability: np.ndarray,
    walking_probability: np.ndarray,
    event_probability: np.ndarray,
    event_codes: Sequence[str],
    thresholds: Mapping[str, float],
    output_hz: float,
    create_time_ms: int | float | None = None,
) -> list[dict[str, object]]:
    times = np.asarray(times_ms, dtype=np.int64)
    posture = np.asarray(posture_probability, dtype=np.float64)
    walking = np.asarray(walking_probability, dtype=np.float64).reshape(-1)
    events = np.asarray(event_probability, dtype=np.float64)
    codes = tuple(str(code) for code in event_codes)
    if times.size == 0:
        return []
    if posture.shape != (times.size, 2):
        raise ValueError("posture_probability shape 与输出时间不一致")
    if walking.shape != (times.size,):
        raise ValueError("walking_probability shape 与输出时间不一致")
    if events.shape != (times.size, len(codes)):
        raise ValueError("event_probability shape 与事件代码不一致")
    step_ms = int(round(1000.0 / output_hz))
    walking_threshold = float(thresholds.get("WALKING", 0.5))
    rows = _body_intervals(
        times,
        posture,
        walking,
        walking_threshold,
        create_time_ms,
        step_ms,
    )
    rows.extend(
        _event_intervals(
            times,
            events,
            codes,
            thresholds,
            create_time_ms,
            step_ms,
        )
    )
    rows.sort(key=lambda row: (int(row["start_ms"]), str(row["layer"]), str(row["code"])))
    return rows
