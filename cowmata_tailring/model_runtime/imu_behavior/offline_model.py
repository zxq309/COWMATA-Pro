"""Inference runtime for the 2026-08-16 ``OfflineMultiTaskTCN`` contract."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import torch
from torch import nn

from .schema import EVENT_CLASSES

try:
    from pytorch_tcn import TCN
except ImportError as exc:  # pragma: no cover - user-facing dependency error
    raise ImportError(
        "20260816 深度模型需要 pytorch-tcn。请运行“安装依赖”脚本后重试。"
    ) from exc


DEFAULT_EVENT_CONTEXT_SECONDS: dict[str, float] = {
    "STANDING_UP": 8.0,
    "LYING_DOWN": 8.0,
    "URINATION": 20.0,
    "DEFECATION": 30.0,
    "TAIL_RAISED": 15.0,
    "TAIL_WAGGING": 3.0,
}


class DilatedTCNEncoder(nn.Module):
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
            causal=False,
            use_norm="weight_norm",
            activation="relu",
            kernel_initializer="xavier_uniform",
            use_skip_connections=True,
            input_shape="NCL",
        )

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return self.network(inputs)


class CenteredScaleHead(nn.Module):
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
        self.context_samples = int(context_samples) | 1
        self.padding = self.context_samples // 2
        self.temporal = nn.Conv1d(
            channels,
            channels,
            kernel_size=self.context_samples,
            groups=channels,
            bias=False,
            padding=self.padding,
        )
        self.activation = nn.GELU()
        self.dropout = nn.Dropout(dropout)
        self.output = nn.Conv1d(channels, outputs, kernel_size=1)

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        return self.output(
            self.dropout(self.activation(self.temporal(features)))
        )

    def forward_center(self, features: torch.Tensor) -> torch.Tensor:
        length = features.shape[-1]
        center = length // 2
        half = self.padding
        if center - half >= 0 and center + half + 1 <= length:
            patch = features[..., center - half : center + half + 1]
            value = nn.functional.conv1d(
                patch,
                self.temporal.weight,
                self.temporal.bias,
                groups=self.temporal.groups,
            )
        else:
            value = self.temporal(features)[..., center : center + 1]
        return self.output(self.dropout(self.activation(value)))


class OfflineMultiTaskTCN(nn.Module):
    """Non-causal model whose decision timestamp is the window centre."""

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
        self.encoder = DilatedTCNEncoder(
            in_channels, width=width, dropout=dropout
        )
        contexts = dict(DEFAULT_EVENT_CONTEXT_SECONDS)
        if event_context_seconds is not None:
            contexts.update(
                {str(key): float(value) for key, value in event_context_seconds.items()}
            )
        self.posture_head = CenteredScaleHead(
            width, 2, round(20.0 * self.sample_rate_hz), dropout
        )
        self.locomotion_head = CenteredScaleHead(
            width, 1, round(4.0 * self.sample_rate_hz), dropout
        )
        self.event_heads = nn.ModuleDict(
            {
                code: CenteredScaleHead(
                    width,
                    1,
                    max(1, round(contexts[code] * self.sample_rate_hz)),
                    dropout,
                )
                for code in self.event_codes
            }
        )

    def forward(self, inputs: torch.Tensor) -> dict[str, torch.Tensor]:
        features = self.encoder(inputs)
        return {
            "posture_logits": self.posture_head.forward_center(features)[..., 0],
            "locomotion_logits": self.locomotion_head.forward_center(features)[
                ..., 0
            ],
            "event_logits": torch.cat(
                [
                    self.event_heads[code].forward_center(features)
                    for code in self.event_codes
                ],
                dim=1,
            )[..., 0],
        }

    def forward_posture_locomotion(
        self, inputs: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Skip unused event heads in the deployed hybrid predictor."""

        features = self.encoder(inputs)
        posture = self.posture_head.forward_center(features)[..., 0]
        locomotion = self.locomotion_head.forward_center(features)[..., 0]
        return posture, locomotion
