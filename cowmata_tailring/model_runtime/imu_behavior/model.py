from __future__ import annotations

import torch
from torch import nn

from .schema import ModelConfig


def _group_count(channels: int) -> int:
    for groups in (16, 8, 4, 2):
        if channels % groups == 0:
            return groups
    return 1


class MultiScaleResidualBlock(nn.Module):
    def __init__(self, channels: int, dilation: int, dropout: float):
        super().__init__()
        groups = _group_count(channels)
        self.norm = nn.GroupNorm(groups, channels)
        self.activation = nn.SiLU()
        self.branch3 = nn.Conv1d(
            channels,
            channels,
            kernel_size=3,
            padding=dilation,
            dilation=dilation,
            groups=channels,
            bias=False,
        )
        self.branch7 = nn.Conv1d(
            channels,
            channels,
            kernel_size=7,
            padding=3 * dilation,
            dilation=dilation,
            groups=channels,
            bias=False,
        )
        self.fuse = nn.Sequential(
            nn.Conv1d(2 * channels, channels, kernel_size=1, bias=False),
            nn.GroupNorm(groups, channels),
            nn.SiLU(),
            nn.Dropout(dropout),
            nn.Conv1d(channels, channels, kernel_size=1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        normalized = self.activation(self.norm(x))
        combined = torch.cat([self.branch3(normalized), self.branch7(normalized)], dim=1)
        return x + self.fuse(combined)


class RefinementStage(nn.Module):
    def __init__(self, in_channels: int, config: ModelConfig):
        super().__init__()
        self.stem = nn.Conv1d(in_channels, config.width, kernel_size=1)
        self.blocks = nn.Sequential(
            *[
                MultiScaleResidualBlock(config.width, dilation, config.dropout)
                for dilation in config.refinement_dilations
            ]
        )
        self.body_head = nn.Conv1d(config.width, config.body_classes, kernel_size=1)
        self.event_head = nn.Conv1d(config.width, config.event_classes, kernel_size=1)

    def forward(self, probabilities: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        features = self.blocks(self.stem(probabilities))
        return self.body_head(features), self.event_head(features)


class HierarchicalMSResTCN(nn.Module):
    """Multi-scale TCN with hierarchical mutually-exclusive and multi-label heads."""

    def __init__(self, config: ModelConfig):
        super().__init__()
        self.config = config
        groups = _group_count(config.width)
        self.stem = nn.Sequential(
            nn.Conv1d(config.input_channels, config.width, kernel_size=7, padding=3),
            nn.GroupNorm(groups, config.width),
            nn.SiLU(),
        )
        self.encoder = nn.Sequential(
            *[
                MultiScaleResidualBlock(config.width, dilation, config.dropout)
                for dilation in config.dilations
            ]
        )
        self.pool = nn.AvgPool1d(
            kernel_size=config.output_stride,
            stride=config.output_stride,
        )
        self.body_head = nn.Conv1d(config.width, config.body_classes, kernel_size=1)
        self.event_head = nn.Conv1d(config.width, config.event_classes, kernel_size=1)
        self.refinement = RefinementStage(
            config.body_classes + config.event_classes,
            config,
        )
        self.reconstruction_head = nn.Sequential(
            nn.GroupNorm(groups, config.width),
            nn.SiLU(),
            nn.Conv1d(config.width, 12, kernel_size=1),
        )
        self._initialize()

    def _initialize(self) -> None:
        for module in self.modules():
            if isinstance(module, nn.Conv1d):
                nn.init.kaiming_normal_(module.weight, nonlinearity="relu")
                if module.bias is not None:
                    nn.init.zeros_(module.bias)

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        return self.encoder(self.stem(x))

    def reconstruct(self, x: torch.Tensor) -> torch.Tensor:
        return self.reconstruction_head(self.encode(x))

    def forward(self, x: torch.Tensor) -> dict[str, list[torch.Tensor] | torch.Tensor]:
        high_resolution = self.encode(x)
        features = self.pool(high_resolution)
        body_first = self.body_head(features)
        event_first = self.event_head(features)
        probabilities = torch.cat(
            [torch.softmax(body_first, dim=1), torch.sigmoid(event_first)], dim=1
        )
        body_final, event_final = self.refinement(probabilities)
        return {
            "body_stages": [body_first, body_final],
            "event_stages": [event_first, event_final],
            "features": features,
        }

