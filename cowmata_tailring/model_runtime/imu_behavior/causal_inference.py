"""Inference aligned with the delivered 2026-08-15 causal predictor."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import torch

from .causal_io import load_causal_features
from .causal_model import CausalMultiTaskTCN
from .causal_package import (
    frame_times_ms,
    load_package_features,
    package_prediction_intervals,
)
from .checkpoint import resolve_checkpoint
from .hardware import autocast_dtype, detect_runtime

ProgressCallback = Callable[[int, str], None]


def _emit(callback: ProgressCallback | None, percent: int, message: str) -> None:
    if callback:
        callback(max(0, min(100, int(percent))), message)


def _safe_load(path: Path, device: torch.device) -> dict[str, object]:
    resolved = resolve_checkpoint(path)
    try:
        payload = torch.load(resolved, map_location=device, weights_only=True)
    except Exception as exc:
        raise ValueError(f"无法安全加载因果模型 {resolved}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("model_class") != "CausalMultiTaskTCN":
        raise ValueError("所选权重不是 CausalMultiTaskTCN checkpoint")
    payload["checkpoint_path"] = str(resolved)
    return payload


@torch.inference_mode()
def predict_causal_imu(
    checkpoint_path: Path,
    imu_path: Path,
    *,
    device: str = "auto",
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    runtime = detect_runtime(requested_device=device)
    torch_device = torch.device(runtime.device)
    _emit(progress, 2, "正在安全加载新版因果多任务模型")
    checkpoint = _safe_load(checkpoint_path, torch_device)
    kwargs = checkpoint.get("model_kwargs")
    if not isinstance(kwargs, dict):
        raise ValueError("因果 checkpoint 缺少 model_kwargs")
    model = CausalMultiTaskTCN(**kwargs).to(torch_device)
    state = checkpoint.get("model_state")
    if not isinstance(state, dict):
        raise ValueError("因果 checkpoint 缺少 model_state")
    model.load_state_dict(state, strict=True)
    model.eval()

    _emit(progress, 7, "正在匹配新版整理包中的训练口径特征缓存")
    cached = load_package_features(Path(checkpoint["checkpoint_path"]), imu_path)
    source_times: np.ndarray | None = None
    if cached is not None:
        features, metadata = cached
        segment_details = metadata["segments_detail"]

        def output_time(indices: np.ndarray) -> np.ndarray:
            return frame_times_ms(indices, segment_details)

        _emit(progress, 9, "已匹配训练时校准的 features.npy")
    else:
        _emit(progress, 8, "训练缓存未匹配，检查 checkpoint 内嵌设备校准")
        source_times, features, _segments, metadata = load_causal_features(
            imu_path, checkpoint
        )
        if metadata.get("calibration_source") == "standard_device_defaults":
            raise ValueError(
                "该九轴 JSON 不在新版整理包的训练缓存中，且 checkpoint 未包含设备校准。"
                "为避免生成失真的高置信度候选，正式预测已停止。请补充校准清单或"
                "先按新版训练流程生成 features.npy。"
            )

        def output_time(indices: np.ndarray) -> np.ndarray:
            assert source_times is not None
            return source_times[indices]

        metadata["features_path"] = ""
    statistics = checkpoint.get("feature_statistics")
    if not isinstance(statistics, dict):
        raise ValueError("因果 checkpoint 缺少 feature_statistics")
    mean = np.asarray(statistics.get("mean"), dtype=np.float32)
    std = np.maximum(
        np.asarray(statistics.get("std"), dtype=np.float32), np.float32(1e-6)
    )
    if mean.shape != (13,) or std.shape != (13,):
        raise ValueError("checkpoint 归一化统计必须各含 13 个缓存通道")
    feature_indices = np.asarray(checkpoint.get("feature_indices"), dtype=np.int64)
    if feature_indices.ndim != 1 or feature_indices.size != int(kwargs["in_channels"]):
        raise ValueError("checkpoint feature_indices 与模型输入通道不一致")
    if np.any((feature_indices < 0) | (feature_indices >= 12)):
        raise ValueError("checkpoint feature_indices 含非法或 timing-quality 通道")

    output_stride = max(1, int(checkpoint.get("output_stride", 25)))
    raw_block = max(output_stride, int(checkpoint.get("stream_block_samples", 500)))
    block_samples = max(output_stride, raw_block // output_stride * output_stride)
    total_frames = int(len(features))
    if total_frames <= 0:
        raise ValueError("训练特征为空，无法执行模型预测")
    processed_frames = 0
    output_times: list[np.ndarray] = []
    posture_rows: list[np.ndarray] = []
    walking_rows: list[np.ndarray] = []
    event_rows: list[np.ndarray] = []
    amp_enabled = runtime.amp and torch_device.type == "cuda"
    _emit(
        progress,
        12,
        f"开始在 {runtime.device_name} 上按新版连续会话流式推理",
    )
    # The reference predictor clears the TCN buffers once per complete session,
    # then feeds every 500-sample block without resetting at minute boundaries.
    model.reset_stream()
    for start in range(0, total_frames, block_samples):
        stop = min(start + block_samples, total_frames)
        raw = np.asarray(features[start:stop], dtype=np.float32)
        normalized = ((raw - mean) / std)[:, feature_indices]
        block = np.ascontiguousarray(normalized.T, dtype=np.float32)
        inputs = torch.from_numpy(block).unsqueeze(0).to(
            torch_device, non_blocking=runtime.pin_memory
        )
        with torch.autocast(
            device_type=torch_device.type,
            dtype=autocast_dtype(runtime),
            enabled=amp_enabled,
        ):
            output = model.forward_dense(inputs, inference=True)
        local = np.arange(0, stop - start, output_stride, dtype=np.int64)
        selected = torch.as_tensor(local, device=torch_device, dtype=torch.long)
        posture = (
            torch.softmax(output["posture_logits"], dim=1)[0]
            .index_select(1, selected)
            .float()
            .cpu()
            .numpy()
            .T
        )
        walking = (
            torch.sigmoid(output["locomotion_logits"])[0, 0]
            .index_select(0, selected)
            .float()
            .cpu()
            .numpy()
        )
        events = (
            torch.sigmoid(output["event_logits"])[0]
            .index_select(1, selected)
            .float()
            .cpu()
            .numpy()
            .T
        )
        output_times.append(output_time(start + local))
        posture_rows.append(posture)
        walking_rows.append(walking)
        event_rows.append(events)
        processed_frames += stop - start
        _emit(
            progress,
            12 + int(72 * processed_frames / max(total_frames, 1)),
            f"新版因果推理 {processed_frames:,}/{total_frames:,} 帧",
        )

    dense_times = np.concatenate(output_times)
    posture_probability = np.concatenate(posture_rows)
    walking_probability = np.concatenate(walking_rows)
    event_probability = np.concatenate(event_rows)
    event_codes = tuple(str(code) for code in model.event_codes)
    thresholds_source = checkpoint.get("thresholds")
    thresholds = {
        "WALKING": float(thresholds_source.get("WALKING", 0.5))
        if isinstance(thresholds_source, dict)
        else 0.5,
        **(
            {code: float(thresholds_source.get(code, 0.5)) for code in event_codes}
            if isinstance(thresholds_source, dict)
            else {code: 0.5 for code in event_codes}
        ),
    }
    output_hz = float(kwargs.get("sample_rate_hz", 50)) / output_stride
    _emit(progress, 88, "正在按新版接口生成身体状态与事件候选")
    predictions = package_prediction_intervals(
        dense_times,
        posture_probability,
        walking_probability,
        event_probability,
        event_codes,
        thresholds,
        output_hz,
        metadata.get("create_time"),
    )
    warning = str(metadata.get("calibration_warning", ""))
    duration_start = int(dense_times[0])
    duration_stop = int(dense_times[-1]) + int(round(1000.0 / output_hz))
    result: dict[str, object] = {
        "model": checkpoint["checkpoint_path"],
        "model_class": "CausalMultiTaskTCN",
        "algorithm": "causal_multitask_tcn_package_20260815_v1",
        "model_semantics": (
            "package-aligned posture + independent walking + sigmoid event candidates"
        ),
        "imu_file": str(imu_path),
        "device": runtime.device_name,
        "sample_hz": float(kwargs.get("sample_rate_hz", 50)),
        "output_hz": output_hz,
        "duration_s": round((duration_stop - duration_start) / 1000.0, 3),
        "prediction_intervals": predictions,
        "ground_truth_intervals": [],
        "metrics": None,
        "thresholds": thresholds,
        "preprocessing": {
            "segments": int(metadata["segments"]),
            "calibration_source": metadata["calibration_source"],
            "feature_source": str(metadata.get("features_path", "")),
            "catalog": str(metadata.get("catalog_path", "")),
            "warning": warning,
        },
        "warnings": [warning] if warning else [],
        "_arrays": {
            "times_ms": dense_times,
            "posture_probability": posture_probability,
            "walking_probability": walking_probability,
            "event_probability": event_probability,
        },
    }
    _emit(progress, 100, f"完成：得到 {len(predictions)} 个分层预测区间")
    return result
