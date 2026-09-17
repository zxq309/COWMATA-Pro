from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .io import read_annotation_rows, safe_float
from .schema import BODY_CLASSES, EVENT_CLASSES, LABEL_LAYER, LABEL_ZH

BODY_TO_INDEX = {code: index for index, code in enumerate(BODY_CLASSES)}
EVENT_TO_INDEX = {code: index for index, code in enumerate(EVENT_CLASSES)}


@dataclass(frozen=True)
class AnnotationInterval:
    code: str
    layer: str
    start_ms: float
    end_ms: float
    weight: float
    confidence_tier: str
    uncertain_start_min_ms: float
    uncertain_start_max_ms: float
    uncertain_end_min_ms: float
    uncertain_end_max_ms: float

    def overlaps(self, start_ms: float, end_ms: float) -> bool:
        return self.end_ms > start_ms and self.start_ms < end_ms


@dataclass
class SessionAnnotations:
    intervals: list[AnnotationInterval]
    source: Path

    @property
    def body_intervals(self) -> list[AnnotationInterval]:
        return [item for item in self.intervals if item.code in BODY_TO_INDEX]

    @property
    def event_intervals(self) -> list[AnnotationInterval]:
        return [item for item in self.intervals if item.code in EVENT_TO_INDEX]


CONFIDENCE_WEIGHTS = {
    "A_dual_consensus_iou_ge_0.70": 1.00,
    "B_dual_consensus_iou_0.30_to_0.70": 0.82,
    "C_dual_consensus_strong_containment": 0.72,
    "single_source_scoped_layer": 0.62,
    "single_source_unique_event": 0.55,
}


def confidence_weight(row: dict[str, str]) -> float:
    tier = row.get("confidence_tier", "")
    if tier in CONFIDENCE_WEIGHTS:
        return CONFIDENCE_WEIGHTS[tier]
    source_count = int(safe_float(row.get("source_count"), 1.0))
    agreement = safe_float(row.get("agreement_iou"), -1.0)
    if source_count >= 2 and agreement >= 0:
        return float(np.clip(0.65 + 0.35 * agreement, 0.65, 1.0))
    return 0.65


def load_annotations(path: Path) -> SessionAnnotations:
    intervals: list[AnnotationInterval] = []
    for row in read_annotation_rows(path):
        code = (row.get("code") or "").strip().upper()
        if code not in BODY_TO_INDEX and code not in EVENT_TO_INDEX:
            continue
        start = safe_float(row.get("reviewed_start_ms"), safe_float(row.get("t_start_rel_ms")))
        end = safe_float(row.get("reviewed_end_ms"), safe_float(row.get("t_end_rel_ms")))
        if end <= start:
            continue
        intervals.append(
            AnnotationInterval(
                code=code,
                layer=(row.get("layer") or LABEL_LAYER[code]).strip(),
                start_ms=start,
                end_ms=end,
                weight=confidence_weight(row),
                confidence_tier=(row.get("confidence_tier") or "legacy_single_source"),
                uncertain_start_min_ms=safe_float(row.get("uncertain_start_min_ms"), start),
                uncertain_start_max_ms=safe_float(row.get("uncertain_start_max_ms"), start),
                uncertain_end_min_ms=safe_float(row.get("uncertain_end_min_ms"), end),
                uncertain_end_max_ms=safe_float(row.get("uncertain_end_max_ms"), end),
            )
        )
    return SessionAnnotations(intervals=intervals, source=path)


def dense_targets(
    output_times_ms: np.ndarray,
    annotations: SessionAnnotations,
    event_negative_scope: str = "full_session",
) -> dict[str, np.ndarray]:
    """Build hierarchical targets without inventing a body-state background class."""
    count = output_times_ms.size
    body_target = np.full(count, -100, dtype=np.int64)
    body_weight = np.zeros(count, dtype=np.float32)

    # Low-confidence intervals are written first so stronger consensus wins conflicts.
    for interval in sorted(annotations.body_intervals, key=lambda item: item.weight):
        active = (output_times_ms >= interval.start_ms) & (output_times_ms < interval.end_ms)
        body_target[active] = BODY_TO_INDEX[interval.code]
        body_weight[active] = interval.weight

    event_target = np.zeros((len(EVENT_CLASSES), count), dtype=np.float32)
    if event_negative_scope == "full_session":
        event_mask = np.ones_like(event_target, dtype=np.float32)
    elif event_negative_scope == "annotated_body":
        event_mask = np.broadcast_to((body_target >= 0)[None, :], event_target.shape).copy().astype(
            np.float32
        )
    else:
        raise ValueError(f"未知 event_negative_scope: {event_negative_scope}")
    event_weight = event_mask.copy()

    for interval in sorted(annotations.event_intervals, key=lambda item: item.weight):
        event_index = EVENT_TO_INDEX[interval.code]
        active = (output_times_ms >= interval.start_ms) & (output_times_ms < interval.end_ms)
        event_target[event_index, active] = 1.0
        event_weight[event_index, active] = interval.weight

    # Disagreement bands are excluded from the corresponding task loss.
    for interval in annotations.intervals:
        uncertain = (
            (
                (output_times_ms >= min(interval.uncertain_start_min_ms, interval.uncertain_start_max_ms))
                & (output_times_ms <= max(interval.uncertain_start_min_ms, interval.uncertain_start_max_ms))
            )
            | (
                (output_times_ms >= min(interval.uncertain_end_min_ms, interval.uncertain_end_max_ms))
                & (output_times_ms <= max(interval.uncertain_end_min_ms, interval.uncertain_end_max_ms))
            )
        )
        if interval.code in BODY_TO_INDEX:
            body_target[uncertain] = -100
            body_weight[uncertain] = 0.0
        else:
            event_index = EVENT_TO_INDEX[interval.code]
            event_mask[event_index, uncertain] = 0.0
            event_weight[event_index, uncertain] = 0.0

    return {
        "body_target": body_target,
        "body_weight": body_weight,
        "event_target": event_target,
        "event_mask": event_mask,
        "event_weight": event_weight,
    }


def annotation_rows_for_display(path: Path) -> list[dict[str, object]]:
    annotations = load_annotations(path)
    rows: list[dict[str, object]] = []
    for interval in sorted(annotations.intervals, key=lambda item: (item.start_ms, item.layer)):
        rows.append(
            {
                "layer": interval.layer,
                "code": interval.code,
                "label": LABEL_ZH.get(interval.code, interval.code),
                "start_ms": int(round(interval.start_ms)),
                "end_ms": int(round(interval.end_ms)),
                "duration_s": round((interval.end_ms - interval.start_ms) / 1000.0, 3),
                "confidence": round(interval.weight, 4),
                "source": interval.confidence_tier,
            }
        )
    return rows

