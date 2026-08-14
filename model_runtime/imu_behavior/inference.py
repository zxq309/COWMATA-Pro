from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np
import torch

from .calibration import apply_calibration
from .checkpoint import inspect_checkpoint, load_checkpoint
from .dataset import chunk_starts, normalize_features
from .hardware import autocast_dtype, detect_runtime
from .io import read_and_regularize
from .labels import annotation_rows_for_display, dense_targets, load_annotations
from .metrics import evaluate_probabilities
from .postprocess import DEFAULT_POSTPROCESS, postprocess_predictions
from .schema import EVENT_CLASSES, NormalizationStats


ProgressCallback = Callable[[int, str], None]


def _emit(callback: ProgressCallback | None, percent: int, message: str) -> None:
    if callback:
        callback(max(0, min(100, int(percent))), message)


def predict_imu(
    checkpoint_path: Path,
    imu_path: Path,
    annotation_path: Path | None = None,
    device: str = "auto",
    batch_size: int | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    resolved, inspected = inspect_checkpoint(checkpoint_path)
    if inspected.get("model_class") == "CausalMultiTaskTCN":
        from .causal_inference import predict_causal_imu

        if annotation_path is not None:
            raise ValueError("新版因果模型的工具内验证比较尚未开放，请使用训练代码评估")
        return predict_causal_imu(
            resolved,
            imu_path,
            device=device,
            progress=progress,
        )
    if inspected.get("model_class") == "MultiTaskResTCN":
        from .historical_inference import predict_historical_loco_imu

        if annotation_path is not None:
            raise ValueError("历史 LOCO 模型请使用交付的预测 CSV/训练代码进行评估")
        return predict_historical_loco_imu(
            resolved,
            imu_path,
            device=device,
            batch_size=batch_size,
            progress=progress,
        )
    if inspected.get("model_class") in {"SSLTemporalEncoder", "LegacySSLTemporalEncoder"}:
        raise ValueError(
            "所选文件是自监督 SSL 表征权重，只有编码器、没有行为分类头，"
            "不能直接用于自动标注。请选择 loco_v1、loco_v2_domain_robust "
            "目录中的 best.pt，或新版 CausalMultiTaskTCN checkpoint。"
        )

    runtime = detect_runtime(requested_device=device, batch_size=batch_size)
    torch_device = torch.device(runtime.device)
    _emit(progress, 2, "正在安全加载模型")
    model, payload = load_checkpoint(checkpoint_path, torch_device)
    config = model.config
    stats = NormalizationStats.from_dict(payload["normalization"])

    _emit(progress, 8, "正在解码 Base64 IMU 并重采样到 50 Hz")
    grid_times, features, imu_metadata = read_and_regularize(imu_path, config.sample_hz)
    chunk = config.chunk_samples
    step = max(config.output_stride, chunk // 2 // config.output_stride * config.output_stride)
    starts = chunk_starts(len(features), chunk, step)
    local_outputs = chunk // config.output_stride
    center_offset = config.output_stride // 2
    output_count = max(0, (len(features) - 1 - center_offset) // config.output_stride + 1)
    if output_count <= 0:
        raise ValueError("IMU 太短，无法生成模型输出")
    body_accumulator = np.zeros((output_count, config.body_classes), dtype=np.float64)
    event_accumulator = np.zeros((output_count, config.event_classes), dtype=np.float64)
    weight_accumulator = np.zeros(output_count, dtype=np.float64)
    local_weight = np.hanning(local_outputs).astype(np.float64) + 0.10

    model.eval()
    inference_batch = runtime.batch_size
    amp_enabled = runtime.amp and torch_device.type == "cuda"
    _emit(progress, 12, f"开始在 {runtime.device_name} 上推理，共 {len(starts)} 个窗口")
    with torch.inference_mode():
        for batch_start in range(0, len(starts), inference_batch):
            selected_starts = starts[batch_start : batch_start + inference_batch]
            batch_chunks: list[np.ndarray] = []
            for start in selected_starts:
                end = min(start + chunk, len(features))
                actual = max(0, end - start)
                raw = np.zeros((chunk, features.shape[1]), dtype=np.float32)
                raw[:, :-1] = np.asarray(stats.center[:-1], dtype=np.float32)
                raw[:, -1] = 1.0
                if actual:
                    raw[:actual] = features[start:end]
                batch_chunks.append(normalize_features(raw, stats).T)
            tensor = torch.from_numpy(np.stack(batch_chunks)).to(
                torch_device, non_blocking=runtime.pin_memory
            )
            with torch.autocast(
                device_type=torch_device.type,
                dtype=autocast_dtype(runtime),
                enabled=amp_enabled,
            ):
                outputs = model(tensor)
            body_batch = outputs["body_stages"][-1].float().cpu().numpy().transpose(0, 2, 1)
            event_batch = outputs["event_stages"][-1].float().cpu().numpy().transpose(0, 2, 1)
            for local_batch_index, start in enumerate(selected_starts):
                global_start = start // config.output_stride
                global_indices = global_start + np.arange(local_outputs)
                raw_centers = start + center_offset + np.arange(local_outputs) * config.output_stride
                valid = (global_indices < output_count) & (raw_centers < len(features))
                indices = global_indices[valid]
                weights = local_weight[valid]
                body_accumulator[indices] += body_batch[local_batch_index, valid] * weights[:, None]
                event_accumulator[indices] += event_batch[local_batch_index, valid] * weights[:, None]
                weight_accumulator[indices] += weights
            completed = min(len(starts), batch_start + len(selected_starts))
            _emit(progress, 12 + int(72 * completed / len(starts)), f"模型推理 {completed}/{len(starts)}")

    if np.any(weight_accumulator <= 0):
        missing = int((weight_accumulator <= 0).sum())
        raise RuntimeError(f"滑窗聚合后仍有 {missing} 个时间点未覆盖")
    body_logits = (body_accumulator / weight_accumulator[:, None]).astype(np.float32)
    event_logits = (event_accumulator / weight_accumulator[:, None]).astype(np.float32)
    output_indices = center_offset + np.arange(output_count) * config.output_stride
    output_times = grid_times[output_indices]
    body_probability, event_probability = apply_calibration(
        body_logits, event_logits, payload.get("calibration")
    )
    postprocess = {**DEFAULT_POSTPROCESS, **payload.get("postprocess", {})}
    thresholds = postprocess.get("event_thresholds", [0.5] * len(EVENT_CLASSES))
    _emit(progress, 88, "正在执行迟滞阈值与最短持续时间后处理")
    predictions = postprocess_predictions(
        output_times,
        body_probability,
        event_probability,
        thresholds,
        config.output_hz,
        imu_metadata.get("create_time"),
        postprocess,
    )

    result: dict = {
        "model": payload["checkpoint_path"],
        "model_class": payload.get("architecture", "HierarchicalMSResTCN"),
        "algorithm": "legacy_evidence_with_hierarchical_state_adapter_v2",
        "model_semantics": (
            "legacy four-class evidence mapped to upright/lying + independent "
            "walking + sigmoid events + state machine; FEEDING is not emitted"
        ),
        "imu_file": str(imu_path),
        "device": runtime.device_name,
        "sample_hz": config.sample_hz,
        "output_hz": config.output_hz,
        "duration_s": round((grid_times[-1] - grid_times[0]) / 1000.0, 3),
        "prediction_intervals": predictions,
        "ground_truth_intervals": [],
        "metrics": None,
        "calibration": payload.get("calibration", {}),
        "thresholds": list(map(float, thresholds)),
        "warnings": [
            "当前默认权重是历史证据模型；输出已按新分层口径适配，但并非新因果模型重训结果。"
        ],
        "_arrays": {
            "times_ms": output_times,
            "body_probability": body_probability,
            "event_probability": event_probability,
        },
    }
    if annotation_path is not None:
        _emit(progress, 92, "正在与验证标注逐点比较")
        annotations = load_annotations(annotation_path)
        event_negative_scope = payload.get("training_metadata", {}).get(
            "event_negative_scope", "full_session"
        )
        targets = dense_targets(
            output_times, annotations, event_negative_scope=event_negative_scope
        )
        session_key = [imu_path.stem] * output_count
        result["ground_truth_intervals"] = annotation_rows_for_display(annotation_path)
        result["metrics"] = evaluate_probabilities(
            body_probability,
            event_probability,
            targets["body_target"],
            targets["body_weight"],
            targets["event_target"].T,
            targets["event_mask"].T,
            thresholds,
            session_key,
        )
        result["event_negative_scope"] = event_negative_scope
    _emit(progress, 100, f"完成：得到 {len(predictions)} 个预测区间")
    return result
