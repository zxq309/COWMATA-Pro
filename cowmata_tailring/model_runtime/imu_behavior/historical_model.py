"""Exact reader for the delivered historical ``MultiTaskResTCN`` weights."""

from __future__ import annotations

import torch
from torch import nn


class HistoricalResidualBlock(nn.Module):
    """Matches the legacy block retained in the delivered SSL reader."""

    def __init__(self, channels: int, dilation: int, dropout: float) -> None:
        super().__init__()
        padding = 2 * dilation
        self.block = nn.Sequential(
            nn.Conv1d(
                channels,
                channels,
                5,
                padding=padding,
                dilation=dilation,
                bias=False,
            ),
            nn.BatchNorm1d(channels),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Conv1d(
                channels,
                channels,
                5,
                padding=padding,
                dilation=dilation,
                bias=False,
            ),
            nn.BatchNorm1d(channels),
        )
        self.activation = nn.GELU()

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return self.activation(inputs + self.block(inputs))


class HistoricalMultiTaskResTCN(nn.Module):
    """Fixed 256-sample centered-window model used by ``models_codex``."""

    BODY_CODES = ("STANDING", "LYING", "WALKING", "FEEDING")
    EVENT_CODES = (
        "STANDING_UP",
        "LYING_DOWN",
        "URINATION",
        "DEFECATION",
        "TAIL_RAISED",
        "TAIL_WAGGING",
    )

    def __init__(
        self,
        in_channels: int,
        width: int = 64,
        dropout: float = 0.10,
    ) -> None:
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Conv1d(
                in_channels, width, 9, stride=2, padding=4, bias=False
            ),
            nn.BatchNorm1d(width),
            nn.GELU(),
            HistoricalResidualBlock(width, 1, dropout),
            nn.Conv1d(width, width, 5, stride=2, padding=2, bias=False),
            nn.BatchNorm1d(width),
            nn.GELU(),
            HistoricalResidualBlock(width, 2, dropout),
            HistoricalResidualBlock(width, 4, dropout),
            nn.AdaptiveAvgPool1d(1),
            nn.Flatten(),
        )
        self.shared = nn.Sequential(
            nn.Dropout(dropout),
            nn.Linear(width, width),
            nn.GELU(),
        )
        self.body_head = nn.Linear(width, len(self.BODY_CODES))
        self.event_head = nn.Linear(width, len(self.EVENT_CODES))

    def forward(self, inputs: torch.Tensor) -> dict[str, torch.Tensor]:
        features = self.shared(self.encoder(inputs))
        return {
            "body_logits": self.body_head(features),
            "event_logits": self.event_head(features),
        }
