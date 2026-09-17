from __future__ import annotations

from pathlib import Path

import torch
from torch.torch_version import TorchVersion

from .model import HierarchicalMSResTCN
from .schema import (
    BODY_CLASSES,
    EVENT_CLASSES,
    MODEL_FORMAT_VERSION,
    PREPROCESSING_VERSION,
    ModelConfig,
    NormalizationStats,
)


def checkpoint_payload(
    model: HierarchicalMSResTCN,
    normalization: NormalizationStats,
    calibration: dict | None = None,
    postprocess: dict | None = None,
    training_metadata: dict | None = None,
) -> dict:
    return {
        "format_version": MODEL_FORMAT_VERSION,
        "preprocessing_version": PREPROCESSING_VERSION,
        "architecture": "HierarchicalMSResTCN",
        "model_config": model.config.to_dict(),
        "model_state": model.state_dict(),
        "normalization": normalization.to_dict(),
        "body_classes": list(BODY_CLASSES),
        "event_classes": list(EVENT_CLASSES),
        "calibration": calibration or {},
        "postprocess": postprocess or {},
        "training_metadata": training_metadata or {},
    }


def save_checkpoint(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    torch.save(payload, temporary)
    temporary.replace(path)


def resolve_checkpoint(path: Path) -> Path:
    if path.is_dir():
        for name in ("best_model.pt", "last_model.pt"):
            candidate = path / name
            if candidate.is_file():
                return candidate
        candidates = sorted(path.glob("*.pt"))
        if candidates:
            return candidates[0]
        raise FileNotFoundError(f"目录内没有 .pt 模型: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"模型文件不存在: {path}")
    return path


def inspect_checkpoint(path: Path) -> tuple[Path, dict]:
    """Safely read a checkpoint payload before choosing a runtime adapter."""

    resolved = resolve_checkpoint(path)
    try:
        # Historical LOCO checkpoints store the producing PyTorch version as a
        # TorchVersion (a string subclass). It is data-only and safe to allow.
        with torch.serialization.safe_globals([TorchVersion]):
            payload = torch.load(resolved, map_location="cpu", weights_only=True)
    except Exception as exc:
        raise ValueError(f"无法安全读取模型 {resolved}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"模型 {resolved} 的顶层内容不是字典")
    return resolved, payload


def load_checkpoint(path: Path, device: str | torch.device = "cpu") -> tuple[HierarchicalMSResTCN, dict]:
    resolved = resolve_checkpoint(path)
    try:
        payload = torch.load(resolved, map_location=device, weights_only=True)
    except Exception as exc:
        raise ValueError(f"无法安全加载模型 {resolved}: {exc}") from exc
    if payload.get("format_version") != MODEL_FORMAT_VERSION:
        raise ValueError(
            f"模型格式版本 {payload.get('format_version')} 与程序版本 {MODEL_FORMAT_VERSION} 不兼容"
        )
    if payload.get("preprocessing_version") != PREPROCESSING_VERSION:
        raise ValueError(
            f"模型预处理版本 {payload.get('preprocessing_version')} 与程序 {PREPROCESSING_VERSION} 不兼容"
        )
    if tuple(payload.get("body_classes", [])) != BODY_CLASSES:
        raise ValueError("模型主体状态标签顺序不兼容")
    if tuple(payload.get("event_classes", [])) != EVENT_CLASSES:
        raise ValueError("模型事件标签顺序不兼容")
    config = ModelConfig.from_dict(payload["model_config"])
    model = HierarchicalMSResTCN(config)
    model.load_state_dict(payload["model_state"], strict=True)
    model.to(device)
    model.eval()
    payload["checkpoint_path"] = str(resolved)
    return model, payload
