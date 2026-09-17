"""Runtime copy of the current causal multi-task model contract.

The temporal operators are provided by the MIT-licensed ``pytorch-tcn``
package, matching ``02_训练代码与过程/src/cattle_imu/model.py``.  This module is
imported lazily so the delivered legacy checkpoint can still run when an older
installation has not installed the new optional dependency yet.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import torch
from torch import nn

from .schema import EVENT_CLASSES

try:
    from pytorch_tcn import TCN, TemporalConv1d
except ImportError as exc:  # pragma: no cover - dependency error is user-facing
    raise ImportError(
        "该权重是新版因果模型，需要 pytorch-tcn。请运行“安装依赖”脚本后重试。"
    ) from exc


DEFAULT_EVENT_CONTEXT_SECONDS: dict[str, float] = {
    "STANDING_UP": 8.0,
    "LYING_DOWN": 8.0,
    "URINATION": 20.0,
    "DEFECATION": 30.0,
    "TAIL_RAISED": 15.0,
    "TAIL_WAGGING": 3.0,
}


class CausalTCNEncoder(nn.Module):
    def __init__(
        self,
        in_channels: int,
        *,
        width: int = 64,
        dropout: float = 0.10,
        dilations: Iterable[int] = (1, 2, 4, 8, 16, 32, 64, 128),
    ) -> None:
        super().__init__()
        dilation_list = [int(value) for value in dilations]
        if not dilation_list or min(dilation_list) <= 0:
            raise ValueError("dilations must contain positive integers")
        self.width = int(width)
        self.network = TCN(
            num_inputs=int(in_channels),
            num_channels=[self.width] * len(dilation_list),
            kernel_size=5,
            dilations=dilation_list,
            dropout=float(dropout),
            causal=True,
            use_norm="weight_norm",
            activation="relu",
            kernel_initializer="xavier_uniform",
            use_skip_connections=True,
            input_shape="NCL",
        )

    def forward(self, inputs: torch.Tensor, *, inference: bool = False) -> torch.Tensor:
        return self.network(inputs, inference=inference)

    def reset_buffers(self) -> None:
        self.network.reset_buffers()


class CausalScaleHead(nn.Module):
    def __init__(
        self,
        channels: int,
        outputs: int,
        context_samples: int,
        dropout: float,
    ) -> None:
        super().__init__()
        if context_samples <= 0:
            raise ValueError("context_samples must be positive")
        self.context_samples = int(context_samples)
        self.temporal = TemporalConv1d(
            channels,
            channels,
            kernel_size=self.context_samples,
            groups=channels,
            bias=False,
            causal=True,
        )
        self.activation = nn.GELU()
        self.dropout = nn.Dropout(dropout)
        self.output = nn.Conv1d(channels, outputs, kernel_size=1)

    def forward(self, features: torch.Tensor, *, inference: bool = False) -> torch.Tensor:
        value = self.temporal(features, inference=inference)
        return self.output(self.dropout(self.activation(value)))

    def reset_buffers(self) -> None:
        self.temporal.reset_buffer()


class CausalMultiTaskTCN(nn.Module):
    """Shared causal encoder with posture, walking and independent event heads."""

    def __init__(
        self,
        in_channels: int = 8,
        *,
        event_codes: Iterable[str] = EVENT_CLASSES,
        sample_rate_hz: int = 50,
        width: int = 64,
        dropout: float = 0.10,
        event_context_seconds: Mapping[str, float] | None = None,
    ) -> None:
        super().__init__()
        self.event_codes = tuple(str(code) for code in event_codes)
        unknown = sorted(set(self.event_codes) - set(EVENT_CLASSES))
        if unknown:
            raise ValueError(f"unknown event codes: {unknown}")
        self.sample_rate_hz = int(sample_rate_hz)
        self.encoder = CausalTCNEncoder(
            in_channels, width=width, dropout=dropout
        )
        contexts = dict(DEFAULT_EVENT_CONTEXT_SECONDS)
        if event_context_seconds is not None:
            contexts.update(
                {str(key): float(value) for key, value in event_context_seconds.items()}
            )
        self.posture_head = CausalScaleHead(
            width, 2, round(20.0 * self.sample_rate_hz), dropout
        )
        self.locomotion_head = CausalScaleHead(
            width, 1, round(4.0 * self.sample_rate_hz), dropout
        )
        self.event_heads = nn.ModuleDict(
            {
                code: CausalScaleHead(
                    width,
                    1,
                    max(1, round(contexts[code] * self.sample_rate_hz)),
                    dropout,
                )
                for code in self.event_codes
            }
        )

    def forward_dense(
        self,
        inputs: torch.Tensor,
        *,
        inference: bool = False,
    ) -> dict[str, torch.Tensor]:
        features = self.encoder(inputs, inference=inference)
        event_logits = torch.cat(
            [
                self.event_heads[code](features, inference=inference)
                for code in self.event_codes
            ],
            dim=1,
        )
        return {
            "posture_logits": self.posture_head(features, inference=inference),
            "locomotion_logits": self.locomotion_head(
                features, inference=inference
            ),
            "event_logits": event_logits,
        }

    def forward(self, inputs: torch.Tensor) -> dict[str, torch.Tensor]:
        dense = self.forward_dense(inputs)
        return {name: value[..., -1] for name, value in dense.items()}

    def reset_stream(self) -> None:
        self.encoder.reset_buffers()
        self.posture_head.reset_buffers()
        self.locomotion_head.reset_buffers()
        for head in self.event_heads.values():
            head.reset_buffers()
