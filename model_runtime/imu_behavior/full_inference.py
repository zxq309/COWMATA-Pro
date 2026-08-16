"""Production adapter for the 2026-08-16 offline TCN + GBDT model package."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from .causal_io import DEFAULT_CALIBRATION, load_causal_features, resolve_calibration
from .checkpoint import inspect_checkpoint
from .full_features import (
    SessionReference,
    gravity_split,
    segment_features,
    session_reference,
)
from .postprocess import postprocess_predict_full_guide


ProgressCallback = Callable[[int, str], None]

GBDT_FILENAME = "gbdt_full.joblib"
DEEP_FILENAME = "best.pt"
CONFIG_FILENAMES = ("inference_config.json", "model_manifest.json")
FULL_EVENT_CODES = (
    "STANDING_UP",
    "LYING_DOWN",
    "URINATION",
    "DEFECATION",
    "TAIL_RAISED",
    "TAIL_WAGGING",
)
REQUIRED_GBDT_TASKS = ("POSTURE_LYING", "WALKING", *FULL_EVENT_CODES)
ALGORITHM_VERSION = "predict_full_guide_20260816_v1"

DEFAULT_EVENT_THRESHOLDS = {code: 0.5 for code in FULL_EVENT_CODES}
# All production devices share the same sensor contract.  MAC addresses are
# metadata only and never select preprocessing parameters.  The centred
# session reference below still compensates for the ring's mounting angle.
UNIFIED_SENSOR_CALIBRATION: dict[str, object] = dict(DEFAULT_CALIBRATION)
GUIDE_POSTPROCESS: dict[str, object] = {
    "contract": "PredictFull_使用指南.md",
    "event_threshold": 0.5,
    "event_split_gap_ms": 5_000,
    "event_min_duration_s": None,
    "event_hysteresis": False,
    "posture_state_machine": False,
    "body_display_adapter": {
        "posture": "argmax(UPRIGHT, LYING)",
        "walking_threshold": 0.5,
        "walking_requires_upright": True,
    },
}


@dataclass(frozen=True)
class FullModelPackageInfo:
    directory: Path
    gbdt_path: Path
    deep_path: Path | None
    config_path: Path | None
    deep_status: str
    deep_model_class: str | None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        return {
            key: str(value) if isinstance(value, Path) else value
            for key, value in payload.items()
        }


def _emit(
    callback: ProgressCallback | None,
    percent: int,
    message: str,
) -> None:
    if callback:
        callback(max(0, min(100, int(percent))), message)


def resolve_full_model_dir(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_file():
        candidate = candidate.parent
    return candidate.resolve(strict=False)


def is_full_model_package(path: str | Path) -> bool:
    directory = resolve_full_model_dir(path)
    return directory.is_dir() and (directory / GBDT_FILENAME).is_file()


def _read_package_config(directory: Path) -> tuple[dict[str, Any], Path | None]:
    for filename in CONFIG_FILENAMES:
        path = directory / filename
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                document = json.load(handle)
        except Exception as exc:
            raise ValueError(f"模型包配置无法解析：{path}: {exc}") from exc
        if not isinstance(document, dict):
            raise ValueError(f"模型包配置顶层必须是对象：{path}")
        return dict(document), path
    return {}, None


def _validate_offline_checkpoint(payload: Mapping[str, object], path: Path) -> None:
    model_class = str(payload.get("model_class", ""))
    if model_class != "OfflineMultiTaskTCN":
        raise ValueError(
            f"{path} 的模型类型是 {model_class or '无法识别'}，"
            "20260816 模型包只接受 OfflineMultiTaskTCN"
        )
    required = {
        "model_state",
        "model_kwargs",
        "feature_indices",
        "feature_statistics",
        "context_samples",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"{path} 缺少 checkpoint 字段：{missing}")
    kwargs = payload.get("model_kwargs")
    if not isinstance(kwargs, Mapping):
        raise ValueError("OfflineMultiTaskTCN checkpoint 的 model_kwargs 无效")
    input_channels = int(kwargs.get("in_channels", 0))
    indices = np.asarray(payload.get("feature_indices"), dtype=np.int64)
    if indices.ndim != 1 or indices.size != input_channels:
        raise ValueError("checkpoint feature_indices 与输入通道数不一致")
    if np.any((indices < 0) | (indices >= 12)):
        raise ValueError("checkpoint feature_indices 包含非法或质量标记通道")
    statistics = payload.get("feature_statistics")
    if not isinstance(statistics, Mapping):
        raise ValueError("checkpoint feature_statistics 无效")
    mean = np.asarray(statistics.get("mean"), dtype=np.float32)
    std = np.asarray(statistics.get("std"), dtype=np.float32)
    if mean.shape != (13,) or std.shape != (13,):
        raise ValueError("checkpoint 归一化 mean/std 必须各含 13 项")
    context = int(payload.get("context_samples", 0))
    if context <= 0 or context > 200_000:
        raise ValueError(f"checkpoint context_samples 非法：{context}")


def inspect_full_model_package(path: str | Path) -> FullModelPackageInfo:
    """Validate the file-level package and safely inspect optional ``best.pt``."""

    directory = resolve_full_model_dir(path)
    if not directory.is_dir():
        raise FileNotFoundError(f"模型包目录不存在：{directory}")
    gbdt_path = directory / GBDT_FILENAME
    if not gbdt_path.is_file() or gbdt_path.stat().st_size <= 0:
        raise FileNotFoundError(f"模型包缺少 {GBDT_FILENAME}：{directory}")
    _, config_path = _read_package_config(directory)
    deep_path = directory / DEEP_FILENAME
    if not deep_path.is_file():
        return FullModelPackageInfo(
            directory=directory,
            gbdt_path=gbdt_path,
            deep_path=None,
            config_path=config_path,
            deep_status="waiting_for_training",
            deep_model_class=None,
        )
    resolved, payload = inspect_checkpoint(deep_path)
    _validate_offline_checkpoint(payload, resolved)
    return FullModelPackageInfo(
        directory=directory,
        gbdt_path=gbdt_path,
        deep_path=resolved,
        config_path=config_path,
        deep_status="ready",
        deep_model_class="OfflineMultiTaskTCN",
    )


def _load_gbdt_bundle(path: Path) -> dict[str, Any]:
    try:
        import joblib
        import xgboost  # required while unpickling XGBClassifier
    except ImportError as exc:
        raise ImportError(
            "20260816 GBDT 模型需要 joblib 和 xgboost 3.2。"
            "请运行“安装依赖”脚本后重试。"
        ) from exc
    if str(getattr(xgboost, "__version__", "")) != "3.2.0":
        raise RuntimeError(
            "gbdt_full.joblib 由 xgboost 3.2.0 序列化，当前环境是 "
            f"{getattr(xgboost, '__version__', '未知版本')}。"
            "请运行“安装依赖”脚本安装精确版本后重试。"
        )
    try:
        bundle = joblib.load(path)
    except Exception as exc:
        raise ValueError(
            f"无法加载 {path}。请确认模型来自可信的 20260816 训练流程，"
            f"并安装兼容的 xgboost：{exc}"
        ) from exc
    if not isinstance(bundle, dict):
        raise ValueError("gbdt_full.joblib 顶层必须是字典")
    models = bundle.get("models")
    features = bundle.get("features")
    if not isinstance(models, Mapping):
        raise ValueError("gbdt_full.joblib 缺少 models 字典")
    if not isinstance(features, Sequence) or isinstance(features, (str, bytes)):
        raise ValueError("gbdt_full.joblib 缺少 features 列表")
    feature_names = [str(value) for value in features]
    if len(feature_names) != 104 or len(set(feature_names)) != 104:
        raise ValueError(
            f"GBDT 特征契约不兼容：预期 104 个唯一特征，实际 {len(feature_names)}"
        )
    missing = [code for code in REQUIRED_GBDT_TASKS if code not in models]
    if missing:
        raise ValueError(f"GBDT 模型包缺少任务：{missing}")
    return {**bundle, "models": dict(models), "features": feature_names}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _merge_mapping(base: dict[str, object], update: Mapping[str, object]) -> None:
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(base.get(str(key)), Mapping):
            nested = dict(base[str(key)])  # type: ignore[arg-type]
            _merge_mapping(nested, value)
            base[str(key)] = nested
        else:
            base[str(key)] = value


def _effective_config(
    bundle: Mapping[str, object],
    file_config: Mapping[str, object],
) -> dict[str, object]:
    result: dict[str, object] = {}
    embedded = bundle.get("inference_config")
    if isinstance(embedded, Mapping):
        _merge_mapping(result, embedded)
    _merge_mapping(result, file_config)
    return result


def _sensor_calibration_contract(
    bundle: Mapping[str, object],
    config: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    """Resolve one global sensor contract without branching on device MAC."""

    result = deepcopy(UNIFIED_SENSOR_CALIBRATION)
    source_name = "tool:unified_sensor_defaults"
    candidates = (
        (bundle.get("sensor_calibration"), "model:sensor_calibration"),
        (config.get("sensor_calibration"), "inference_config:sensor_calibration"),
    )
    for source, name in candidates:
        if not isinstance(source, Mapping):
            continue
        # Older configuration files may wrap a global value in ``default``.
        # Device/MAC tables are deliberately ignored because production
        # devices do not carry individual calibration parameters.
        candidate = source.get("default")
        if not isinstance(candidate, Mapping):
            candidate = source
        updated = False
        for key in DEFAULT_CALIBRATION:
            if key in candidate:
                result[key] = deepcopy(candidate[key])
                updated = True
        if updated:
            source_name = name
    return result, source_name


def _read_source_document(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8-sig") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("原始 IMU JSON 顶层必须是对象")
    if int(document.get("version", 2)) != 2:
        raise ValueError("PredictFull 指南适配当前只支持 V2 九轴 JSON")
    # Cache lookup needs only session identity. Do not retain the large Base64
    # payload while the models are running.
    document.pop("imu", None)
    return document


def _guide_cache_roots(model_directory: Path) -> list[Path]:
    """Locate the two cache roots in the same order as predict_full.py."""

    anchors = [model_directory, *model_directory.parents]
    bundled_root = Path(__file__).resolve().parents[2] / "预测" / "20260816"
    anchors.append(bundled_root)
    data_roots: list[Path] = []
    seen: set[str] = set()
    for anchor in anchors:
        candidate = anchor / "01_关键训练数据"
        key = str(candidate.resolve(strict=False)).casefold()
        if key in seen or not candidate.is_dir():
            continue
        seen.add(key)
        data_roots.append(candidate)
    roots: list[Path] = []
    for data_root in data_roots:
        roots.extend(
            (
                data_root / "supervised_cache" / "session_cache",
                data_root / "ssl_cache" / "session_cache",
            )
        )
    return [root for root in roots if root.is_dir()]


def _find_guide_cache(
    model_directory: Path,
    source: Path,
    document: Mapping[str, object],
) -> Path | None:
    device = str(
        document.get("device")
        or document.get("device_id")
        or document.get("mac")
        or ""
    ).strip()
    if not device:
        return None
    prefix = re.sub(
        r"[^A-Za-z0-9]+", "_", f"{device}_{source.stem}"
    ).strip("_")
    for root in _guide_cache_roots(model_directory):
        matches = sorted(
            path
            for path in root.glob(f"{prefix}_*")
            if path.is_dir() and (path / "features.npy").is_file()
        )
        if len(matches) > 1:
            raise ValueError(
                f"PredictFull 指南缓存匹配不唯一：{prefix}，请保留一个 cache_key"
            )
        if matches:
            return matches[0]
    return None


def _load_guide_cache(
    cache_directory: Path,
    document: Mapping[str, object],
) -> tuple[np.ndarray, np.ndarray, list[tuple[int, int]], dict[str, object]]:
    """Load ``features.npy`` and segment times exactly like predict_full.py."""

    features = np.load(cache_directory / "features.npy", mmap_mode="r")
    if features.ndim != 2 or features.shape[1] != 13:
        raise ValueError(
            f"PredictFull 缓存必须是 N×13，实际为 {tuple(features.shape)}"
        )
    metadata_path = cache_directory / "metadata.json"
    if metadata_path.is_file():
        with metadata_path.open("r", encoding="utf-8-sig") as handle:
            cache_metadata = json.load(handle)
        raw_segments = cache_metadata.get("segments", [])
        segments_with_time = [
            (
                int(item["start_index"]),
                int(item["stop_index"]),
                int(item.get("start_ms", int(item["start_index"]) * 20)),
            )
            for item in raw_segments
        ]
    else:
        segments_with_time = [(0, len(features), 0)]
    if not segments_with_time:
        raise ValueError(f"PredictFull 缓存没有有效 segments：{cache_directory}")

    times = np.empty(len(features), dtype=np.int64)
    segments: list[tuple[int, int]] = []
    for start, stop, start_ms in segments_with_time:
        if not (0 <= start < stop <= len(features)):
            raise ValueError(
                f"PredictFull 缓存 segment 越界：[{start}, {stop})/{len(features)}"
            )
        times[start:stop] = start_ms + np.arange(stop - start, dtype=np.int64) * 20
        segments.append((start, stop))

    device = str(
        document.get("device")
        or document.get("device_id")
        or document.get("mac")
        or ""
    )
    metadata: dict[str, object] = {
        "create_time": document.get("create_time"),
        "device": device,
        "uid": document.get("uid", ""),
        "version": 2,
        "regular_frames": int(len(features)),
        "segments": len(segments),
        "calibration_source": "predict_full:preprocessed_cache",
        "calibration_warning": "",
        "guide_cache_key": cache_directory.name,
        "guide_cache_path": str(cache_directory.resolve()),
    }
    return times, features, segments, metadata


def _extract_feature_table(
    features: np.ndarray,
    times_ms: np.ndarray,
    segments: Sequence[tuple[int, int]],
    progress: ProgressCallback | None,
) -> tuple[
    pd.DataFrame,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    SessionReference,
]:
    """Apply the exact centred 104-feature transform without crossing gaps."""

    static_parts: list[np.ndarray] = []
    motion_parts: list[np.ndarray] = []
    for start, stop in segments:
        block = np.asarray(features[start:stop, 0:3], dtype=np.float64)
        if block.shape[0] < 32:
            continue
        static, dynamic = gravity_split(block, causal=False)
        static_parts.append(static[::10])
        motion_parts.append(np.linalg.norm(dynamic, axis=1)[::10])
    if not static_parts:
        raise ValueError("连续 IMU 片段过短，无法建立逐会话姿态参考")
    reference = session_reference(
        np.concatenate(static_parts), np.concatenate(motion_parts)
    )

    frames: list[pd.DataFrame] = []
    centers: list[np.ndarray] = []
    center_times: list[np.ndarray] = []
    center_segment_starts: list[np.ndarray] = []
    center_segment_stops: list[np.ndarray] = []
    segment_count = max(1, len(segments))
    for index, (start, stop) in enumerate(segments):
        local = np.arange(0, stop - start, 25, dtype=np.int64)
        if local.size == 0:
            continue
        block = np.asarray(features[start:stop], dtype=np.float32)
        frames.append(
            segment_features(
                block,
                local,
                causal=False,
                reference=reference,
            )
        )
        global_centers = local + int(start)
        centers.append(global_centers)
        center_times.append(times_ms[global_centers])
        center_segment_starts.append(
            np.full(local.size, int(start), dtype=np.int64)
        )
        center_segment_stops.append(
            np.full(local.size, int(stop), dtype=np.int64)
        )
        _emit(
            progress,
            16 + int(24 * (index + 1) / segment_count),
            f"正在提取新版 104 维特征：{index + 1}/{segment_count} 段",
        )
    if not frames:
        raise ValueError("没有可预测的 2 Hz 决策点")
    return (
        pd.concat(frames, ignore_index=True),
        np.concatenate(center_times).astype(np.int64, copy=False),
        np.concatenate(centers).astype(np.int64, copy=False),
        np.concatenate(center_segment_starts),
        np.concatenate(center_segment_stops),
        reference,
    )


def _prediction_vector(model: object, values: np.ndarray, task: str) -> np.ndarray:
    predict = getattr(model, "predict_proba", None)
    if not callable(predict):
        raise ValueError(f"GBDT 任务 {task} 不提供 predict_proba")
    probability = np.asarray(predict(values), dtype=np.float64)
    if probability.ndim == 2 and probability.shape[1] == 2:
        probability = probability[:, 1]
    probability = probability.reshape(-1)
    if probability.size != values.shape[0]:
        raise ValueError(f"GBDT 任务 {task} 输出长度不匹配")
    if not np.isfinite(probability).all():
        raise ValueError(f"GBDT 任务 {task} 输出包含非有限数")
    return np.clip(probability, 0.0, 1.0).astype(np.float32)


def _gbdt_probabilities(
    bundle: Mapping[str, object],
    frame: pd.DataFrame,
    progress: ProgressCallback | None,
) -> dict[str, np.ndarray]:
    feature_names = [str(value) for value in bundle["features"]]  # type: ignore[index]
    missing = [name for name in feature_names if name not in frame.columns]
    if missing:
        raise ValueError(f"运行时 104 维特征缺少列：{missing[:8]}")
    values = frame[feature_names].to_numpy(np.float32)
    values = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)
    models = bundle["models"]
    assert isinstance(models, Mapping)
    result: dict[str, np.ndarray] = {}
    for index, task in enumerate(REQUIRED_GBDT_TASKS):
        result[task] = _prediction_vector(models[task], values, task)
        _emit(
            progress,
            42 + int(16 * (index + 1) / len(REQUIRED_GBDT_TASKS)),
            f"正在运行 GBDT：{index + 1}/{len(REQUIRED_GBDT_TASKS)} 个任务",
        )
    return result


def _centered_context_bounds(
    center: int,
    segment_start: int,
    segment_stop: int,
    context_samples: int,
) -> tuple[int, int, int]:
    half = int(context_samples) // 2
    ideal_start = int(center) - half
    start = max(int(segment_start), ideal_start)
    stop = min(int(segment_stop), ideal_start + int(context_samples))
    return start, stop, start - ideal_start


def _deep_probabilities(
    deep_path: Path,
    features: np.ndarray,
    centers: np.ndarray,
    segment_starts: np.ndarray,
    segment_stops: np.ndarray,
    progress: ProgressCallback | None,
    *,
    device: str,
    batch_size: int | None,
) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    import torch

    from .hardware import detect_runtime
    from .offline_model import OfflineMultiTaskTCN

    resolved, checkpoint = inspect_checkpoint(deep_path)
    _validate_offline_checkpoint(checkpoint, resolved)
    kwargs_source = checkpoint["model_kwargs"]
    assert isinstance(kwargs_source, Mapping)
    allowed = {
        "in_channels",
        "event_codes",
        "sample_rate_hz",
        "width",
        "dropout",
        "event_context_seconds",
    }
    kwargs = {
        str(key): value
        for key, value in kwargs_source.items()
        if str(key) in allowed
    }
    model = OfflineMultiTaskTCN(**kwargs)
    model.load_state_dict(checkpoint["model_state"], strict=True)  # type: ignore[arg-type]
    runtime = detect_runtime(requested_device=device)
    torch_device = torch.device(runtime.device)
    model.to(torch_device).eval()

    statistics = checkpoint["feature_statistics"]
    assert isinstance(statistics, Mapping)
    mean = np.asarray(statistics["mean"], dtype=np.float32)
    std = np.maximum(
        np.asarray(statistics["std"], dtype=np.float32), np.float32(1e-6)
    )
    indices = np.asarray(checkpoint["feature_indices"], dtype=np.int64)
    context = int(checkpoint["context_samples"])
    # PredictFull_使用指南.md fixes B=64.  An explicit smaller value and the
    # OOM fallback are retained only so the desktop tool can recover safely.
    effective_batch = int(batch_size or 64)
    effective_batch = max(1, effective_batch)
    posture_parts: list[np.ndarray] = []
    walking_parts: list[np.ndarray] = []
    completed = 0
    with torch.inference_mode():
        while completed < len(centers):
            current_batch = min(effective_batch, len(centers) - completed)
            stop_index = completed + current_batch
            windows = np.zeros(
                (current_batch, len(indices), context), dtype=np.float32
            )
            for local_index, row_index in enumerate(range(completed, stop_index)):
                start, stop, destination = _centered_context_bounds(
                    int(centers[row_index]),
                    int(segment_starts[row_index]),
                    int(segment_stops[row_index]),
                    context,
                )
                normalized = (
                    np.asarray(features[start:stop], dtype=np.float32) - mean
                ) / std
                selected = normalized[:, indices].T
                windows[
                    local_index,
                    :,
                    destination : destination + selected.shape[1],
                ] = selected
            inputs = torch.from_numpy(windows).to(
                torch_device, non_blocking=runtime.pin_memory
            )
            try:
                output = model(inputs)
                posture_logits = output["posture_logits"]
                walking_logits = output["locomotion_logits"]
            except RuntimeError as exc:
                if (
                    torch_device.type == "cuda"
                    and effective_batch > 1
                    and "out of memory" in str(exc).lower()
                ):
                    effective_batch = max(1, effective_batch // 2)
                    torch.cuda.empty_cache()
                    _emit(
                        progress,
                        60 + int(30 * completed / max(1, len(centers))),
                        f"显存不足，自动把深度批量降为 {effective_batch}",
                    )
                    continue
                raise
            posture_parts.append(
                torch.softmax(posture_logits, dim=1).float().cpu().numpy()
            )
            walking_parts.append(
                torch.sigmoid(walking_logits).float().cpu().numpy().reshape(-1)
            )
            completed = stop_index
            _emit(
                progress,
                60 + int(30 * completed / max(1, len(centers))),
                f"正在运行离线深度姿态模型：{completed:,}/{len(centers):,} 点",
            )
    thresholds = checkpoint.get("thresholds")
    return (
        np.concatenate(posture_parts).astype(np.float32, copy=False),
        np.concatenate(walking_parts).astype(np.float32, copy=False),
        {
            "path": str(resolved),
            "device": runtime.device_name,
            "batch_size": effective_batch,
            "best_epoch": checkpoint.get("best_epoch", checkpoint.get("epoch")),
            "thresholds": dict(thresholds) if isinstance(thresholds, Mapping) else {},
            "precision": "float32_predict_full_guide",
        },
    )


def predict_full_imu(
    model_package: str | Path,
    imu_path: str | Path,
    *,
    device: str = "auto",
    batch_size: int | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    """Predict all nine tool labels from a raw V2 JSON recording."""

    package = inspect_full_model_package(model_package)
    source = Path(imu_path).resolve()
    _emit(progress, 2, "正在加载 20260816 模型包")
    bundle = _load_gbdt_bundle(package.gbdt_path)
    file_config, _ = _read_package_config(package.directory)
    config = _effective_config(bundle, file_config)

    document = _read_source_document(source)
    guide_cache = _find_guide_cache(package.directory, source, document)
    applied_calibration: dict[str, object] | None
    if guide_cache is not None:
        _emit(progress, 8, f"正在按 PredictFull 指南加载 cache_key：{guide_cache.name}")
        times, base_features, segments, metadata = _load_guide_cache(
            guide_cache, document
        )
        applied_calibration = None
        preprocessing_contract = "predict_full_guide_preprocessed_cache_v1"
    else:
        calibration_source, calibration_source_name = _sensor_calibration_contract(
            bundle, config
        )
        _emit(
            progress,
            8,
            "新会话无 cache_key，正在按指南第 6 节生成 50 Hz/13 通道输入",
        )
        times, base_features, segments, metadata = load_causal_features(
            source,
            {"sensor_calibration": calibration_source},
        )
        applied_calibration, _ = resolve_calibration(
            {"sensor_calibration": calibration_source},
            device=str(metadata.get("device", "")),
            session_id=source.stem,
        )
        metadata["calibration_source"] = calibration_source_name
        metadata["calibration_warning"] = ""
        preprocessing_contract = "predict_full_guide_raw_cache_adapter_v1"
    (
        feature_frame,
        dense_times,
        centers,
        segment_starts,
        segment_stops,
        reference,
    ) = _extract_feature_table(base_features, times, segments, progress)
    task_probability = _gbdt_probabilities(bundle, feature_frame, progress)
    event_probability = np.column_stack(
        [task_probability[code] for code in FULL_EVENT_CODES]
    ).astype(np.float32, copy=False)

    warnings: list[str] = []
    deep_metadata: dict[str, object] | None = None
    if package.deep_path is not None:
        _emit(progress, 60, "检测到 best.pt，切换为深度姿态/行走")
        posture_probability, walking_probability, deep_metadata = (
            _deep_probabilities(
                package.deep_path,
                base_features,
                centers,
                segment_starts,
                segment_stops,
                progress,
                device=device,
                batch_size=batch_size,
            )
        )
        body_support = "formal"
        posture_source = "offline_tcn:posture"
        walking_source = "offline_tcn:locomotion"
        model_status = "hybrid_ready"
        model_class = "OfflineMultiTaskTCN+FullGBDT"
    else:
        lying = task_probability["POSTURE_LYING"]
        posture_probability = np.column_stack((1.0 - lying, lying)).astype(
            np.float32, copy=False
        )
        walking_probability = task_probability["WALKING"]
        body_support = "provisional"
        posture_source = "gbdt:posture"
        walking_source = "gbdt:walking"
        model_status = "gbdt_fallback_waiting_for_best_pt"
        model_class = "FullGBDT"
        warnings.append(
            "深度模型仍在训练，当前站立/躺卧/行走使用 GBDT 回退；"
            "把训练完成的 best.pt 放入模型包目录后会自动切换。"
        )

    event_thresholds = dict(DEFAULT_EVENT_THRESHOLDS)
    walking_threshold = 0.5
    settings = deepcopy(GUIDE_POSTPROCESS)
    _emit(progress, 92, "正在按 PredictFull 指南生成 0.5 阈值候选区间")
    intervals = postprocess_predict_full_guide(
        dense_times,
        posture_probability,
        walking_probability,
        event_probability,
        FULL_EVENT_CODES,
        metadata.get("create_time"),
        body_model_support=body_support,
        posture_source_head=posture_source,
        walking_source_head=walking_source,
    )

    gbdt_hash = _sha256(package.gbdt_path)
    deep_hash = _sha256(package.deep_path) if package.deep_path is not None else ""
    fingerprint = hashlib.sha256(
        f"{ALGORITHM_VERSION}|{gbdt_hash}|{deep_hash}|"
        f"{metadata.get('guide_cache_key', '')}|"
        f"{json.dumps(applied_calibration, sort_keys=True)}|"
        f"{json.dumps(event_thresholds, sort_keys=True)}|"
        f"{walking_threshold}".encode("utf-8")
    ).hexdigest()
    if guide_cache is None:
        warnings.append(
            "当前会话不在 PredictFull 训练缓存中；工具已按指南第 6 节从原始 "
            "JSON 生成 50 Hz/13 通道输入后预测。"
        )
    warnings.append("甩尾仅有 4 个事件/1 头牛，按指南必须逐条人工复核。")
    result: dict[str, object] = {
        "model": str(package.directory),
        "model_class": model_class,
        "model_status": model_status,
        "model_fingerprint": fingerprint,
        "model_hashes": {
            "gbdt_sha256": gbdt_hash,
            "deep_sha256": deep_hash or None,
        },
        "algorithm": ALGORITHM_VERSION,
        "model_semantics": (
            "PredictFull guide: offline deep posture/walking when best.pt is "
            "present; GBDT six-event heads; fixed 0.5 event candidates"
        ),
        "imu_file": str(source),
        "device": (
            str(deep_metadata.get("device"))
            if deep_metadata is not None
            else "CPU (XGBoost)"
        ),
        "sample_hz": 50.0,
        "output_hz": 2.0,
        "duration_s": round(len(base_features) / 50.0, 3),
        "timeline_span_s": round(
            (int(times[-1]) - int(times[0]) + 20) / 1000.0, 3
        ),
        "prediction_intervals": intervals,
        "ground_truth_intervals": [],
        "metrics": None,
        "thresholds": {
            "WALKING": walking_threshold,
            **event_thresholds,
        },
        "postprocess": settings,
        "package": package.to_dict(),
        "deep_runtime": deep_metadata,
        "preprocessing": {
            "contract": preprocessing_contract,
            "segments": len(segments),
            "regular_frames": int(len(base_features)),
            "dense_points": int(len(dense_times)),
            "calibration_source": metadata["calibration_source"],
            "sensor_calibration": applied_calibration,
            "guide_cache_key": metadata.get("guide_cache_key"),
            "guide_cache_path": metadata.get("guide_cache_path"),
            "session_reference": reference.to_dict(),
            "phase_score": metadata.get("phase_score"),
            "dropped_prefix_bytes": metadata.get("dropped_prefix_bytes", 0),
            "dropped_trailing_bytes": metadata.get("dropped_trailing_bytes", 0),
        },
        "warnings": warnings,
        "_arrays": {
            "center_index": centers,
            "times_ms": dense_times,
            "posture_probability": posture_probability,
            "walking_probability": walking_probability,
            "event_probability": event_probability,
            "event_codes": list(FULL_EVENT_CODES),
        },
    }
    _emit(progress, 100, f"完成：按 PredictFull 指南生成 {len(intervals)} 个区间")
    return result
