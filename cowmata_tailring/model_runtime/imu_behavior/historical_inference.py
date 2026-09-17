"""Inference adapter for delivered LOCO ``MultiTaskResTCN`` checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import torch

from .causal_io import load_causal_features
from .checkpoint import inspect_checkpoint
from .hardware import autocast_dtype, detect_runtime
from .historical_model import HistoricalMultiTaskResTCN
from .postprocess import postprocess_body_event_predictions

ProgressCallback = Callable[[int, str], None]
WINDOW_SAMPLES = 256
WINDOW_LEFT = WINDOW_SAMPLES // 2
WINDOW_RIGHT = WINDOW_SAMPLES - WINDOW_LEFT
OUTPUT_STRIDE = 25


def _emit(callback: ProgressCallback | None, percent: int, message: str) -> None:
    if callback:
        callback(max(0, min(100, int(percent))), message)


def _normalization(checkpoint: dict[str, object]) -> tuple[np.ndarray, np.ndarray]:
    fold = checkpoint.get("fold")
    normalization = fold.get("normalization") if isinstance(fold, dict) else None
    if not isinstance(normalization, dict):
        raise ValueError("历史 LOCO checkpoint 缺少 fold.normalization")
    mean = np.asarray(normalization.get("mean"), dtype=np.float32)
    std = np.maximum(
        np.asarray(normalization.get("std"), dtype=np.float32), np.float32(1e-6)
    )
    if mean.shape != (13,) or std.shape != (13,):
        raise ValueError("历史 LOCO 归一化统计必须各含 13 个通道")
    return mean, std


def _feature_indices(
    checkpoint: dict[str, object], in_channels: int
) -> np.ndarray:
    source = checkpoint.get("feature_indices")
    if source is None:
        indices = np.arange(in_channels, dtype=np.int64)
    else:
        indices = np.asarray(source, dtype=np.int64)
    if indices.shape != (in_channels,) or np.any((indices < 0) | (indices >= 13)):
        raise ValueError("历史 LOCO feature_indices 与模型输入不兼容")
    return indices


def _window_centers(segments: list[tuple[int, int]]) -> np.ndarray:
    values: list[np.ndarray] = []
    for start, stop in segments:
        first = start + WINDOW_LEFT
        last = stop - WINDOW_RIGHT
        if last >= first:
            values.append(
                np.arange(first, last + 1, OUTPUT_STRIDE, dtype=np.int64)
            )
    if not values:
        raise ValueError("没有长度足够生成 5.12 秒中心窗的连续 IMU 片段")
    return np.concatenate(values)


@torch.inference_mode()
def predict_historical_loco_imu(
    checkpoint_path: Path,
    imu_path: Path,
    *,
    device: str = "auto",
    batch_size: int | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    runtime = detect_runtime(requested_device=device)
    torch_device = torch.device(runtime.device)
    _emit(progress, 2, "正在安全加载历史 LOCO 权重")
    resolved, checkpoint = inspect_checkpoint(checkpoint_path)
    if checkpoint.get("model_class") != "MultiTaskResTCN":
        raise ValueError("所选文件不是历史 MultiTaskResTCN checkpoint")
    state = checkpoint.get("model_state")
    if not isinstance(state, dict) or "encoder.0.weight" not in state:
        raise ValueError("历史 LOCO checkpoint 缺少完整 model_state")
    in_channels = int(state["encoder.0.weight"].shape[1])
    model = HistoricalMultiTaskResTCN(in_channels=in_channels).to(torch_device)
    model.load_state_dict(state, strict=True)
    model.eval()

    _emit(progress, 7, "按历史训练口径解码、校准并分段重采样 V2 IMU")
    times, features, segments, metadata = load_causal_features(
        imu_path, checkpoint
    )
    mean, std = _normalization(checkpoint)
    indices = _feature_indices(checkpoint, in_channels)
    centers = _window_centers(segments)
    inference_batch = int(
        batch_size or (512 if torch_device.type == "cuda" else 128)
    )
    amp_enabled = runtime.amp and torch_device.type == "cuda"
    body_rows: list[np.ndarray] = []
    event_rows: list[np.ndarray] = []
    offsets = np.arange(-WINDOW_LEFT, WINDOW_RIGHT, dtype=np.int64)
    _emit(
        progress,
        12,
        f"开始在 {runtime.device_name} 上推理 {len(centers):,} 个历史中心窗",
    )
    for batch_start in range(0, len(centers), inference_batch):
        selected_centers = centers[batch_start : batch_start + inference_batch]
        window_indices = selected_centers[:, None] + offsets[None, :]
        raw = np.asarray(features[window_indices], dtype=np.float32)
        normalized = ((raw - mean) / std)[:, :, indices]
        inputs = torch.from_numpy(
            np.ascontiguousarray(normalized.transpose(0, 2, 1))
        ).to(torch_device, non_blocking=runtime.pin_memory)
        with torch.autocast(
            device_type=torch_device.type,
            dtype=autocast_dtype(runtime),
            enabled=amp_enabled,
        ):
            output = model(inputs)
        body_rows.append(
            torch.softmax(output["body_logits"], dim=1).float().cpu().numpy()
        )
        event_rows.append(
            torch.sigmoid(output["event_logits"]).float().cpu().numpy()
        )
        completed = min(len(centers), batch_start + len(selected_centers))
        _emit(
            progress,
            12 + int(72 * completed / len(centers)),
            f"历史 LOCO 推理 {completed:,}/{len(centers):,} 窗",
        )

    body_probability = np.concatenate(body_rows)
    event_probability = np.concatenate(event_rows)
    thresholds_source = checkpoint.get("thresholds")
    thresholds = {
        code: float(thresholds_source.get(code, 0.5))
        if isinstance(thresholds_source, dict)
        else 0.5
        for code in model.EVENT_CODES
    }
    threshold_adjustments: dict[str, dict[str, float]] = {}
    # Some validation-only folds selected a near-zero TAIL_WAGGING threshold,
    # yielding hundreds of unusable proposals per hour. The model card marks it
    # non-deployable, so the review UI uses a conservative research-candidate floor.
    if thresholds["TAIL_WAGGING"] < 0.50:
        threshold_adjustments["TAIL_WAGGING"] = {
            "checkpoint": thresholds["TAIL_WAGGING"],
            "review_floor": 0.50,
        }
        thresholds["TAIL_WAGGING"] = 0.50
    _emit(progress, 88, "正在执行新版站卧状态机与事件候选后处理")
    dense_times = times[centers]
    predictions = postprocess_body_event_predictions(
        dense_times,
        body_probability,
        event_probability,
        model.BODY_CODES,
        model.EVENT_CODES,
        thresholds,
        50.0 / OUTPUT_STRIDE,
        metadata.get("create_time"),
        {"walking_threshold": 0.50},
    )
    warnings = [
        "所选权重是历史固定中心窗 LOCO 模型（约 2.56 秒未来信息），仅用于离线辅助复核。"
    ]
    calibration_warning = str(metadata.get("calibration_warning", ""))
    if calibration_warning:
        warnings.append(calibration_warning)
    if threshold_adjustments:
        warnings.append(
            "TAIL_WAGGING 的验证阈值过低，复核候选已使用 0.50 安全下限。"
        )
    result: dict[str, object] = {
        "model": str(resolved),
        "model_class": "MultiTaskResTCN",
        "algorithm": "historical_loco_center_window_hierarchical_adapter_v2",
        "model_semantics": (
            "historical four-class centered-window evidence mapped to posture + "
            "walking + independent events + state machine"
        ),
        "imu_file": str(imu_path),
        "device": runtime.device_name,
        "sample_hz": 50.0,
        "output_hz": 50.0 / OUTPUT_STRIDE,
        "duration_s": round((times[-1] - times[0]) / 1000.0, 3),
        "prediction_intervals": predictions,
        "ground_truth_intervals": [],
        "metrics": None,
        "thresholds": thresholds,
        "threshold_adjustments": threshold_adjustments,
        "preprocessing": {
            "feature_mode": checkpoint.get("feature_mode", "raw13"),
            "feature_indices": indices.tolist(),
            "window_samples": WINDOW_SAMPLES,
            "window_alignment": "centered",
            "segments": int(metadata["segments"]),
            "calibration_source": metadata["calibration_source"],
            "warning": calibration_warning,
        },
        "fold": {
            "fold": checkpoint.get("fold", {}).get("fold")
            if isinstance(checkpoint.get("fold"), dict)
            else None,
            "test_cow": checkpoint.get("fold", {}).get("test_cow")
            if isinstance(checkpoint.get("fold"), dict)
            else None,
        },
        "warnings": warnings,
        "_arrays": {
            "times_ms": dense_times,
            "body_probability": body_probability,
            "event_probability": event_probability,
        },
    }
    _emit(progress, 100, f"完成：得到 {len(predictions)} 个分层预测区间")
    return result
