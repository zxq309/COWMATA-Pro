from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path

import numpy as np

from .schema import FEATURE_NAMES, PREPROCESSING_VERSION, SessionRecord

IMU_DTYPE = np.dtype([("time_ms", "<u4"), ("axes", "<i2", (9,))])
MAX_SESSION_DURATION_MS = 12 * 60 * 60 * 1000


def discover_labeled_sessions(root: Path, split: str) -> list[SessionRecord]:
    imu_root = root / split / "imu"
    csv_root = root / split / "csv"
    if not imu_root.is_dir() or not csv_root.is_dir():
        raise FileNotFoundError(f"缺少 {split} 数据目录: {imu_root} 或 {csv_root}")
    records: list[SessionRecord] = []
    for imu_path in sorted(imu_root.rglob("*.json")):
        rel = imu_path.relative_to(imu_root)
        ann_path = csv_root / rel.parent / f"{imu_path.stem}.events.csv"
        if not ann_path.is_file():
            raise FileNotFoundError(f"IMU 缺少对应标注 CSV: {imu_path} -> {ann_path}")
        device_folder = rel.parts[-2] if len(rel.parts) >= 2 else "unknown"
        device_id, cow_id = _parse_device_folder(device_folder)
        records.append(
            SessionRecord(
                imu_path=imu_path,
                annotation_path=ann_path,
                split=split,
                session_id=imu_path.stem,
                device_id=device_id,
                cow_id=cow_id,
            )
        )
    return records


def discover_unlabelled_sessions(root: Path) -> list[SessionRecord]:
    imu_root = root / "untrained_imu"
    if not imu_root.is_dir():
        return []
    records: list[SessionRecord] = []
    for imu_path in sorted(imu_root.rglob("*.json")):
        rel = imu_path.relative_to(imu_root)
        device_folder = rel.parts[-2] if len(rel.parts) >= 2 else "unknown"
        device_id, cow_id = _parse_device_folder(device_folder)
        records.append(
            SessionRecord(
                imu_path=imu_path,
                split="unlabelled",
                session_id=imu_path.stem,
                device_id=device_id,
                cow_id=cow_id,
            )
        )
    return records


def _parse_device_folder(value: str) -> tuple[str, str]:
    pieces = value.split("-", 1)
    return pieces[0], pieces[1] if len(pieces) == 2 else ""


def read_imu_json(path: Path) -> tuple[np.ndarray, np.ndarray, dict]:
    """Decode the exact little-endian <I9h payload used by this dataset."""
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    encoded = payload.get("imu")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError(f"{path}: 缺少非空 imu Base64 字段")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError(f"{path}: imu Base64 解码失败: {exc}") from exc
    if len(raw) % IMU_DTYPE.itemsize:
        raise ValueError(
            f"{path}: 解码后 {len(raw)} 字节不能被帧长 {IMU_DTYPE.itemsize} 整除"
        )
    frames = np.frombuffer(raw, dtype=IMU_DTYPE)
    if frames.size < 2:
        raise ValueError(f"{path}: 有效 IMU 帧不足 2 条")
    times = frames["time_ms"].astype(np.int64)
    axes = frames["axes"].astype(np.float32)
    raw_span = int(times.max()) - int(times.min())
    if raw_span > MAX_SESSION_DURATION_MS:
        raise ValueError(
            f"{path}: 相对时间跨度 {raw_span / 3_600_000:.3f} h 超过单会话安全上限 "
            f"{MAX_SESSION_DURATION_MS / 3_600_000:.0f} h，疑似时间戳字节损坏"
        )
    finite = np.isfinite(axes).all(axis=1)
    times, axes = times[finite], axes[finite]
    order = np.argsort(times, kind="stable")
    times, axes = times[order], axes[order]
    keep = np.r_[True, np.diff(times) > 0]
    times, axes = times[keep], axes[keep]
    if times.size < 2:
        raise ValueError(f"{path}: 去重后有效 IMU 帧不足 2 条")
    metadata = {
        "create_time": payload.get("create_time"),
        "device": payload.get("device", ""),
        "uid": payload.get("uid", ""),
        "version": payload.get("version"),
        "source_frames": int(frames.size),
    }
    return times, axes, metadata


def regularize_imu(
    times_ms: np.ndarray,
    axes: np.ndarray,
    sample_hz: float = 50.0,
    gap_threshold_ms: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Interpolate onto a stable grid and retain a gap indicator as channel 13."""
    step_ms = 1000.0 / sample_hz
    if abs(step_ms - round(step_ms)) > 1e-6:
        raise ValueError("当前实现要求采样间隔为整数毫秒")
    step = int(round(step_ms))
    start = int(np.ceil(times_ms[0] / step) * step)
    end = int(np.floor(times_ms[-1] / step) * step)
    if end <= start:
        raise ValueError("IMU 时间跨度太短，无法重采样")
    grid = np.arange(start, end + 1, step, dtype=np.int64)
    maximum_frames = int(MAX_SESSION_DURATION_MS / step) + 1
    if grid.size > maximum_frames:
        raise ValueError(
            f"重采样将产生 {grid.size:,} 帧，超过安全上限 {maximum_frames:,}"
        )
    out_axes = np.empty((grid.size, axes.shape[1]), dtype=np.float32)
    source_t = times_ms.astype(np.float64, copy=False)
    target_t = grid.astype(np.float64, copy=False)
    for channel in range(axes.shape[1]):
        out_axes[:, channel] = np.interp(target_t, source_t, axes[:, channel]).astype(
            np.float32
        )

    right = np.searchsorted(times_ms, grid, side="left")
    left = np.clip(right - 1, 0, times_ms.size - 1)
    right_clip = np.clip(right, 0, times_ms.size - 1)
    nearest_distance = np.minimum(
        np.abs(grid - times_ms[left]), np.abs(times_ms[right_clip] - grid)
    )
    threshold = gap_threshold_ms if gap_threshold_ms is not None else 1.75 * step
    gap_mask = nearest_distance > threshold
    source_gaps = np.diff(times_ms)
    large_gap_indices = np.flatnonzero(source_gaps > 2.5 * step)
    for idx in large_gap_indices:
        gap_mask |= (grid > times_ms[idx]) & (grid < times_ms[idx + 1])

    # Do not create a physically implausible smooth ramp across missing spans.
    # Fill flagged locations from the nearest observed frame and let gap_mask tell
    # the network that those values are imputed.
    if gap_mask.any():
        choose_right = np.abs(times_ms[right_clip] - grid) < np.abs(grid - times_ms[left])
        nearest_source = np.where(choose_right, right_clip, left)
        out_axes[gap_mask] = axes[nearest_source[gap_mask]]

    triad_norms = np.stack(
        [np.linalg.norm(out_axes[:, start_idx : start_idx + 3], axis=1) for start_idx in (0, 3, 6)],
        axis=1,
    ).astype(np.float32)
    features = np.concatenate(
        [out_axes, triad_norms, gap_mask.astype(np.float32)[:, None]], axis=1
    )
    if features.shape[1] != len(FEATURE_NAMES):
        raise AssertionError("特征通道数与 schema 不一致")
    return grid, features


def read_and_regularize(path: Path, sample_hz: float = 50.0) -> tuple[np.ndarray, np.ndarray, dict]:
    times, axes, metadata = read_imu_json(path)
    grid, features = regularize_imu(times, axes, sample_hz=sample_hz)
    metadata.update(
        {
            "grid_start_ms": int(grid[0]),
            "grid_end_ms": int(grid[-1]),
            "regular_frames": int(grid.size),
            "sample_hz": sample_hz,
        }
    )
    return grid, features, metadata


class FeatureCache:
    """Content-versioned `.npy` cache; arrays remain memory-mappable."""

    CACHE_VERSION = PREPROCESSING_VERSION

    def __init__(self, directory: Path, sample_hz: float = 50.0):
        self.directory = directory
        self.sample_hz = sample_hz
        self.directory.mkdir(parents=True, exist_ok=True)

    def _key(self, source: Path) -> str:
        stat = source.stat()
        token = (
            f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
            f"{self.CACHE_VERSION}|{self.sample_hz}"
        )
        return hashlib.sha1(token.encode("utf-8")).hexdigest()

    def paths(self, source: Path) -> tuple[Path, Path, Path]:
        key = self._key(source)
        return (
            self.directory / f"{key}.features.npy",
            self.directory / f"{key}.times.npy",
            self.directory / f"{key}.meta.json",
        )

    def ensure(
        self,
        source: Path,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[Path, Path, dict]:
        feature_path, time_path, meta_path = self.paths(source)
        if feature_path.is_file() and time_path.is_file() and meta_path.is_file():
            with meta_path.open("r", encoding="utf-8") as handle:
                return feature_path, time_path, json.load(handle)
        if progress:
            progress(f"解码并缓存 {source.name}")
        times, features, metadata = read_and_regularize(source, self.sample_hz)
        temp_suffix = f".{os.getpid()}.tmp"
        feature_tmp = feature_path.with_name(feature_path.name + temp_suffix)
        time_tmp = time_path.with_name(time_path.name + temp_suffix)
        meta_tmp = meta_path.with_name(meta_path.name + temp_suffix)
        with feature_tmp.open("wb") as handle:
            np.save(handle, features, allow_pickle=False)
        with time_tmp.open("wb") as handle:
            np.save(handle, times, allow_pickle=False)
        with meta_tmp.open("w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
        os.replace(feature_tmp, feature_path)
        os.replace(time_tmp, time_path)
        os.replace(meta_tmp, meta_path)
        return feature_path, time_path, metadata

    def load(self, source: Path) -> tuple[np.ndarray, np.ndarray, dict]:
        feature_path, time_path, metadata = self.ensure(source)
        features = np.load(feature_path, mmap_mode="r", allow_pickle=False)
        times = np.load(time_path, mmap_mode="r", allow_pickle=False)
        return times, features, metadata


def read_annotation_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def safe_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def iter_valid_sessions(
    records: Iterable[SessionRecord], cache: FeatureCache
) -> tuple[list[SessionRecord], list[tuple[SessionRecord, str]]]:
    valid: list[SessionRecord] = []
    rejected: list[tuple[SessionRecord, str]] = []
    for record in records:
        try:
            cache.ensure(record.imu_path)
            valid.append(record)
        except Exception as exc:
            rejected.append((record, str(exc)))
    return valid, rejected
