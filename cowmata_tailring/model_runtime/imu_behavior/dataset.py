from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import Dataset

from .io import FeatureCache
from .labels import SessionAnnotations, dense_targets, load_annotations
from .schema import ModelConfig, NormalizationStats, SessionRecord


@dataclass
class PreparedSession:
    record: SessionRecord
    feature_path: Path
    time_path: Path
    length: int
    annotations: SessionAnnotations | None


def fit_robust_normalizer(
    records: Sequence[SessionRecord],
    cache: FeatureCache,
    max_samples_per_session: int = 20_000,
    max_total_samples: int = 2_000_000,
    progress: Callable[[str], None] | None = None,
) -> NormalizationStats:
    """Fit median/MAD using train sessions only and bounded deterministic subsamples."""
    samples: list[np.ndarray] = []
    total = 0
    for index, record in enumerate(records):
        _, features, _ = cache.load(record.imu_path)
        take = min(max_samples_per_session, len(features), max_total_samples - total)
        if take <= 0:
            break
        positions = np.linspace(0, len(features) - 1, num=take, dtype=np.int64)
        samples.append(np.asarray(features[positions], dtype=np.float32))
        total += take
        if progress:
            progress(f"拟合训练集归一化统计 {index + 1}/{len(records)}")
    if not samples:
        raise ValueError("没有可用于归一化的训练样本")
    stacked = np.concatenate(samples, axis=0)
    center = np.median(stacked, axis=0).astype(np.float32)
    mad = np.median(np.abs(stacked - center), axis=0).astype(np.float32)
    scale = 1.4826 * mad
    fallback = np.std(stacked, axis=0).astype(np.float32)
    scale = np.where(scale > 1e-5, scale, np.maximum(fallback, 1.0))
    # The gap flag is already in [0, 1] and must retain its literal meaning.
    center[-1], scale[-1] = 0.0, 1.0
    return NormalizationStats(center=center.tolist(), scale=scale.tolist())


def normalize_features(features: np.ndarray, stats: NormalizationStats) -> np.ndarray:
    center = np.asarray(stats.center, dtype=np.float32)
    scale = np.asarray(stats.scale, dtype=np.float32)
    if features.shape[-1] != center.size:
        raise ValueError(f"特征通道 {features.shape[-1]} 与模型归一化通道 {center.size} 不一致")
    normalized = (features.astype(np.float32, copy=False) - center) / scale
    np.clip(normalized[:, :-1], -12.0, 12.0, out=normalized[:, :-1])
    return normalized


class _ArrayLoaderMixin:
    _local_arrays: OrderedDict
    _array_cache_size: int = 6

    def _arrays(self, state: PreparedSession) -> tuple[np.ndarray, np.ndarray]:
        key = str(state.feature_path)
        if key in self._local_arrays:
            arrays = self._local_arrays.pop(key)
            self._local_arrays[key] = arrays
            return arrays
        features = np.load(state.feature_path, mmap_mode="r", allow_pickle=False)
        times = np.load(state.time_path, mmap_mode="r", allow_pickle=False)
        self._local_arrays[key] = (features, times)
        while len(self._local_arrays) > self._array_cache_size:
            self._local_arrays.popitem(last=False)
        return features, times

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_local_arrays"] = OrderedDict()
        return state


def prepare_sessions(
    records: Sequence[SessionRecord],
    cache: FeatureCache,
    require_annotations: bool,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[PreparedSession], list[dict[str, str]]]:
    prepared: list[PreparedSession] = []
    rejected: list[dict[str, str]] = []
    for index, record in enumerate(records):
        try:
            feature_path, time_path, metadata = cache.ensure(
                record.imu_path,
                (lambda message: progress(f"{index + 1}/{len(records)} {message}")) if progress else None,
            )
            annotations = None
            if require_annotations:
                if record.annotation_path is None:
                    raise FileNotFoundError("缺少标注路径")
                annotations = load_annotations(record.annotation_path)
            prepared.append(
                PreparedSession(
                    record=record,
                    feature_path=feature_path,
                    time_path=time_path,
                    length=int(metadata["regular_frames"]),
                    annotations=annotations,
                )
            )
        except Exception as exc:
            rejected.append({"path": str(record.imu_path), "reason": str(exc)})
        if progress:
            progress(f"已准备 {index + 1}/{len(records)} 个 {records[index].split} 会话")
    return prepared, rejected


def chunk_starts(length: int, chunk: int, step: int) -> list[int]:
    if length <= chunk:
        return [0]
    starts = list(range(0, length - chunk + 1, step))
    # Round upward so the tail is covered while every start stays grid-aligned.
    final = int(np.ceil((length - chunk) / step) * step)
    if starts[-1] != final:
        starts.append(final)
    return sorted(set(starts))


class DenseChunkDataset(_ArrayLoaderMixin, Dataset):
    def __init__(
        self,
        sessions: Sequence[PreparedSession],
        config: ModelConfig,
        stats: NormalizationStats,
        training: bool,
        event_negative_scope: str = "full_session",
    ):
        self.sessions = list(sessions)
        self.config = config
        self.stats = stats
        self.training = training
        self.event_negative_scope = event_negative_scope
        self._local_arrays = OrderedDict()
        step = config.chunk_samples if not training else max(
            config.output_stride,
            int(config.chunk_samples * (1.0 - config.chunk_overlap)) // config.output_stride
            * config.output_stride,
        )
        self.items: list[tuple[int, int]] = []
        self.sample_weights: list[float] = []
        rare_boost = {
            "DEFECATION": 5.0,
            "URINATION": 2.0,
            "LYING_DOWN": 2.0,
            "STANDING_UP": 2.0,
            "TAIL_RAISED": 3.0,
            "TAIL_WAGGING": 8.0,
        }
        for session_index, state in enumerate(self.sessions):
            _, times = self._arrays(state)
            for start in chunk_starts(state.length, config.chunk_samples, step):
                self.items.append((session_index, start))
                end_index = min(state.length - 1, start + config.chunk_samples - 1)
                start_ms, end_ms = float(times[start]), float(times[end_index])
                weight = 1.0
                if state.annotations:
                    if any(item.overlaps(start_ms, end_ms) for item in state.annotations.body_intervals):
                        weight += 0.5
                    for interval in state.annotations.event_intervals:
                        if interval.overlaps(start_ms, end_ms):
                            weight += rare_boost.get(interval.code, 1.0)
                self.sample_weights.append(min(weight, 16.0))
        self._local_arrays.clear()

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> dict[str, torch.Tensor | str]:
        session_index, start = self.items[index]
        state = self.sessions[session_index]
        features, times = self._arrays(state)
        chunk = self.config.chunk_samples
        end = min(start + chunk, state.length)
        actual = end - start
        raw = np.zeros((chunk, features.shape[1]), dtype=np.float32)
        raw[:, :-1] = np.asarray(self.stats.center[:-1], dtype=np.float32)
        raw[:, -1] = 1.0
        raw[:actual] = np.asarray(features[start:end], dtype=np.float32)
        x = normalize_features(raw, self.stats)

        if self.training:
            gain = np.random.uniform(0.92, 1.08, size=(1, 3)).astype(np.float32)
            for triad, triad_start in enumerate((0, 3, 6)):
                x[:, triad_start : triad_start + 3] *= gain[:, triad]
                x[:, 9 + triad] *= gain[:, triad]
            x[:, :-1] += np.random.normal(0.0, 0.01, size=x[:, :-1].shape).astype(np.float32)

        output_count = chunk // self.config.output_stride
        center_indices = start + self.config.output_stride // 2 + np.arange(output_count) * self.config.output_stride
        valid = center_indices < end
        output_times = np.empty(output_count, dtype=np.int64)
        if valid.any():
            output_times[valid] = np.asarray(times[center_indices[valid]], dtype=np.int64)
            output_times[~valid] = output_times[valid][-1] + (
                np.arange(1, (~valid).sum() + 1) * int(round(1000 / self.config.output_hz))
            )
        else:
            output_times[:] = 0
        if state.annotations is None:
            raise RuntimeError("监督数据集会话没有 annotations")
        targets = dense_targets(output_times, state.annotations, self.event_negative_scope)
        targets["body_target"][~valid] = -100
        targets["body_weight"][~valid] = 0.0
        targets["event_mask"][:, ~valid] = 0.0
        targets["event_weight"][:, ~valid] = 0.0

        return {
            "x": torch.from_numpy(np.ascontiguousarray(x.T)),
            "body_target": torch.from_numpy(targets["body_target"]),
            "body_weight": torch.from_numpy(targets["body_weight"]),
            "event_target": torch.from_numpy(targets["event_target"]),
            "event_mask": torch.from_numpy(targets["event_mask"]),
            "event_weight": torch.from_numpy(targets["event_weight"]),
            "session_key": state.record.key,
        }


class SSLChunkDataset(_ArrayLoaderMixin, Dataset):
    def __init__(
        self,
        sessions: Sequence[PreparedSession],
        config: ModelConfig,
        stats: NormalizationStats,
    ):
        self.sessions = list(sessions)
        self.config = config
        self.stats = stats
        self._local_arrays = OrderedDict()
        self.items: list[tuple[int, int]] = []
        step = config.chunk_samples
        for session_index, state in enumerate(self.sessions):
            for start in chunk_starts(state.length, config.chunk_samples, step):
                self.items.append((session_index, start))

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        session_index, start = self.items[index]
        state = self.sessions[session_index]
        features, _ = self._arrays(state)
        chunk = self.config.chunk_samples
        end = min(start + chunk, state.length)
        actual = end - start
        raw = np.zeros((chunk, features.shape[1]), dtype=np.float32)
        raw[:, :-1] = np.asarray(self.stats.center[:-1], dtype=np.float32)
        raw[:, -1] = 1.0
        raw[:actual] = np.asarray(features[start:end], dtype=np.float32)
        x = normalize_features(raw, self.stats)
        valid = np.zeros(chunk, dtype=np.float32)
        valid[:actual] = 1.0 - raw[:actual, -1]
        return {
            "x": torch.from_numpy(np.ascontiguousarray(x.T)),
            "valid": torch.from_numpy(valid),
        }
