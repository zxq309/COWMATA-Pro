from __future__ import annotations

import math
from typing import Sequence

import torch
import torch.nn.functional as F


def make_ssl_mask(
    x: torch.Tensor,
    valid: torch.Tensor,
    mask_ratio: float = 0.30,
    channel_mask_probability: float = 0.12,
    min_block: int = 25,
    max_block: int = 250,
) -> torch.Tensor:
    """Block-and-channel mask for the 12 physical channels; gap flag is excluded."""
    batch, _, length = x.shape
    # Vectorized multi-scale block masks avoid thousands of GPU-synchronizing
    # scalar `.item()` calls per batch.  Bernoulli seeds are dilated by max-pool;
    # the seed probability is chosen so each scale covers a known expected ratio.
    block_sizes = (
        max(3, int(min_block) | 1),
        max(3, int(max_block) | 1),
    )
    per_scale_ratio = 1.0 - math.sqrt(max(0.0, 1.0 - mask_ratio))
    flattened_masks: list[torch.Tensor] = []
    for block in block_sizes:
        seed_probability = min(1.0, -math.log(max(1e-6, 1.0 - per_scale_ratio)) / block)
        seeds = (
            torch.rand((batch * 12, 1, length), device=x.device) < seed_probability
        ).float()
        expanded = F.max_pool1d(seeds, kernel_size=block, stride=1, padding=block // 2)
        flattened_masks.append(expanded > 0)
    mask = torch.logical_or(*flattened_masks).reshape(batch, 12, length)
    whole_channel = (
        torch.rand((batch, 12, 1), device=x.device) < channel_mask_probability
    )
    mask |= whole_channel
    mask &= valid[:, None, :].bool()
    return mask


def masked_reconstruction_loss(
    model,
    x: torch.Tensor,
    valid: torch.Tensor,
    mask_ratio: float = 0.30,
) -> torch.Tensor:
    mask = make_ssl_mask(x, valid, mask_ratio=mask_ratio)
    corrupted = x.clone()
    corrupted[:, :12] = corrupted[:, :12].masked_fill(mask, 0.0)
    prediction = model.reconstruct(corrupted)
    element_loss = F.huber_loss(prediction, x[:, :12], reduction="none", delta=1.0)
    denominator = mask.sum().clamp_min(1)
    return (element_loss * mask).sum() / denominator


def _body_loss(
    logits: torch.Tensor,
    target: torch.Tensor,
    weight: torch.Tensor,
    class_weight: torch.Tensor | None = None,
) -> torch.Tensor:
    valid = (target >= 0) & (weight > 0)
    if not valid.any():
        return logits.sum() * 0.0
    point_loss = F.cross_entropy(
        logits, target, weight=class_weight, ignore_index=-100, reduction="none"
    )
    return (point_loss * weight * valid).sum() / (weight * valid).sum().clamp_min(1.0)


def _event_focal_loss(
    logits: torch.Tensor,
    target: torch.Tensor,
    mask: torch.Tensor,
    weight: torch.Tensor,
    pos_weight: torch.Tensor,
    gamma: float,
) -> torch.Tensor:
    class_weight = 1.0 + target * (pos_weight[None, :, None] - 1.0)
    bce = F.binary_cross_entropy_with_logits(logits, target, reduction="none")
    probability = torch.sigmoid(logits)
    probability_true = target * probability + (1.0 - target) * (1.0 - probability)
    focal = (1.0 - probability_true).pow(gamma)
    numerator = (bce * focal * class_weight * mask * weight).sum()
    denominator = (mask * weight).sum().clamp_min(1.0)
    return numerator / denominator


def temporal_smoothness_loss(
    body_logits: torch.Tensor,
    event_logits: torch.Tensor,
    body_weight: torch.Tensor,
    event_mask: torch.Tensor,
    tau: float = 4.0,
) -> torch.Tensor:
    body_log_probability = torch.log_softmax(body_logits, dim=1)
    event_log_probability = F.logsigmoid(event_logits)
    body_delta = torch.diff(body_log_probability, dim=-1).abs().clamp_max(tau).pow(2)
    event_delta = torch.diff(event_log_probability, dim=-1).abs().clamp_max(tau).pow(2)
    body_pairs = ((body_weight[:, 1:] > 0) & (body_weight[:, :-1] > 0)).float()
    event_pairs = event_mask[:, :, 1:] * event_mask[:, :, :-1]
    body_term = (body_delta * body_pairs[:, None, :]).sum() / (
        body_pairs.sum() * body_logits.shape[1]
    ).clamp_min(1.0)
    event_term = (event_delta * event_pairs).sum() / event_pairs.sum().clamp_min(1.0)
    return body_term + event_term


def supervised_loss(
    outputs: dict,
    batch: dict[str, torch.Tensor],
    event_pos_weight: torch.Tensor,
    body_class_weight: torch.Tensor | None = None,
    focal_gamma: float = 2.0,
    smoothness_weight: float = 0.05,
) -> dict[str, torch.Tensor]:
    body_stages: Sequence[torch.Tensor] = outputs["body_stages"]
    event_stages: Sequence[torch.Tensor] = outputs["event_stages"]
    stage_weights = [0.5] * (len(body_stages) - 1) + [1.0]
    body_total = body_stages[-1].sum() * 0.0
    event_total = event_stages[-1].sum() * 0.0
    for stage_weight, body_logits, event_logits in zip(
        stage_weights, body_stages, event_stages
    ):
        body_total = body_total + stage_weight * _body_loss(
            body_logits,
            batch["body_target"],
            batch["body_weight"],
            body_class_weight,
        )
        event_total = event_total + stage_weight * _event_focal_loss(
            event_logits,
            batch["event_target"],
            batch["event_mask"],
            batch["event_weight"],
            event_pos_weight,
            focal_gamma,
        )
    smooth = temporal_smoothness_loss(
        body_stages[-1],
        event_stages[-1],
        batch["body_weight"],
        batch["event_mask"],
    )
    total = body_total + event_total + smoothness_weight * smooth
    return {"total": total, "body": body_total, "event": event_total, "smooth": smooth}
