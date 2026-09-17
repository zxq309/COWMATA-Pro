from __future__ import annotations

import os
import random
from dataclasses import asdict, dataclass

import numpy as np
import torch


@dataclass
class RuntimeConfig:
    device: str
    device_name: str
    amp: bool
    amp_dtype: str
    batch_size: int
    num_workers: int
    supervised_epochs: int
    pretrain_epochs: int
    pin_memory: bool

    def to_dict(self) -> dict:
        return asdict(self)


def detect_runtime(
    requested_device: str = "auto",
    batch_size: int | None = None,
    num_workers: int | None = None,
    epochs: int | None = None,
    pretrain_epochs: int | None = None,
) -> RuntimeConfig:
    if requested_device == "auto":
        if torch.cuda.is_available():
            device = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
    else:
        device = requested_device

    if device.startswith("cuda"):
        index = torch.cuda.current_device()
        props = torch.cuda.get_device_properties(index)
        memory_gb = props.total_memory / 1024**3
        auto_batch = 8 if memory_gb < 10 else 16 if memory_gb < 18 else 24
        name = props.name
        amp_dtype = "bfloat16" if torch.cuda.is_bf16_supported() else "float16"
        amp = True
        auto_epochs, auto_pretrain = 45, 8
        auto_workers = min(4, max(1, (os.cpu_count() or 4) // 4))
        pin_memory = True
    elif device == "mps":
        auto_batch, name = 4, "Apple Metal"
        amp, amp_dtype = False, "float32"
        auto_epochs, auto_pretrain = 35, 4
        auto_workers, pin_memory = 2, False
    else:
        auto_batch, name = 2, "CPU"
        amp, amp_dtype = False, "float32"
        auto_epochs, auto_pretrain = 25, 0
        auto_workers = min(2, max(0, (os.cpu_count() or 2) // 4))
        pin_memory = False
    return RuntimeConfig(
        device=device,
        device_name=name,
        amp=amp,
        amp_dtype=amp_dtype,
        batch_size=batch_size or auto_batch,
        num_workers=auto_workers if num_workers is None else num_workers,
        supervised_epochs=epochs if epochs is not None else auto_epochs,
        pretrain_epochs=pretrain_epochs if pretrain_epochs is not None else auto_pretrain,
        pin_memory=pin_memory,
    )


def seed_everything(seed: int = 2026) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = True


def autocast_dtype(runtime: RuntimeConfig) -> torch.dtype:
    return torch.bfloat16 if runtime.amp_dtype == "bfloat16" else torch.float16
