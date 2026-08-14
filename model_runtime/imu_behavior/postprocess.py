from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from .schema import BODY_CLASSES, EVENT_CLASSES, LABEL_LAYER, LABEL_ZH


POSTURE_CODES = ("UPRIGHT", "LYING")
OUTPUT_BODY_CODES = ("STANDING", "LYING", "WALKING")
RESEARCH_EVENT_CODES = {"DEFECATION", "TAIL_RAISED", "TAIL_WAGGING"}

DEFAULT_POSTPROCESS = {
    # The animal is standing while the tail ring is fitted and recording starts.
    # This is the known physical initial condition documented by the project.
    "state_machine": {
        "initial_state": "UPRIGHT",
        "confirm_points": 4,
        "min_dwell_points": 4,
        "margin": 0.10,
        "transition_event_weight": 0.50,
        "max_sequence_gap_ms": 750,
    },
    "walking_threshold": 0.55,
    "walking_hysteresis_margin": 0.12,
    "walking_min_duration_s": 1.0,
    "walking_merge_gap_s": 0.75,
    "event_min_duration_s": {
        "DEFECATION": 2.0,
        "URINATION": 2.0,
        "LYING_DOWN": 1.0,
        "STANDING_UP": 1.0,
        "TAIL_RAISED": 1.0,
        "TAIL_WAGGING": 0.5,
    },
    "event_merge_gap_s": 1.0,
    "hysteresis_margin": 0.15,
}


def _runs(labels: np.ndarray) -> list[tuple[int, int, int]]:
    if labels.size == 0:
        return []
    changes = np.flatnonzero(np.r_[True, labels[1:] != labels[:-1], True])
    return [
        (int(labels[start]), int(start), int(end))
        for start, end in zip(changes[:-1], changes[1:])
    ]


def clean_short_body_runs(
    labels: np.ndarray,
    probability: np.ndarray,
    minimum_steps: int,
) -> np.ndarray:
    """Compatibility helper retained for historical callers and tests."""

    cleaned = labels.copy()
    for _ in range(4):
        changed = False
        runs = _runs(cleaned)
        for run_index, (_, start, end) in enumerate(runs):
            if end - start >= minimum_steps or len(runs) == 1:
                continue
            candidates: list[int] = []
            if run_index > 0:
                candidates.append(runs[run_index - 1][0])
            if run_index + 1 < len(runs):
                candidates.append(runs[run_index + 1][0])
            if candidates:
                replacement = max(
                    candidates,
                    key=lambda candidate: float(
                        probability[start:end, candidate].mean()
                    ),
                )
                cleaned[start:end] = replacement
                changed = True
        if not changed:
            break
    return cleaned


def hysteresis_binary(probability: np.ndarray, high: float, low: float) -> np.ndarray:
    active = np.zeros(probability.size, dtype=bool)
    state = False
    for index, value in enumerate(probability):
        if not state and value >= high:
            state = True
        elif state and value < low:
            state = False
        active[index] = state
    return active


def merge_and_filter_binary(
    active: np.ndarray,
    minimum_steps: int,
    merge_gap_steps: int,
) -> np.ndarray:
    output = active.copy()
    positive_runs = [
        (start, end)
        for label, start, end in _runs(output.astype(np.int8))
        if label == 1
    ]
    for (_, left_end), (right_start, _) in zip(
        positive_runs[:-1], positive_runs[1:]
    ):
        if right_start - left_end <= merge_gap_steps:
            output[left_end:right_start] = True
    for label, start, end in _runs(output.astype(np.int8)):
        if label == 1 and end - start < minimum_steps:
            output[start:end] = False
    return output


def run_posture_state_machine(
    times_ms: np.ndarray,
    posture_probability: np.ndarray,
    standing_up_probability: np.ndarray,
    lying_down_probability: np.ndarray,
    settings: Mapping[str, object] | None = None,
) -> np.ndarray:
    """Return 0=UPRIGHT/1=LYING using only current and historical evidence."""

    config = {**DEFAULT_POSTPROCESS["state_machine"], **(settings or {})}
    times = np.asarray(times_ms, dtype=np.int64)
    posture = np.asarray(posture_probability, dtype=np.float64)
    if posture.shape != (times.size, 2):
        raise ValueError(
            "posture_probability must have shape (N, 2) in UPRIGHT/LYING order"
        )
    standing_up = np.asarray(standing_up_probability, dtype=np.float64)
    lying_down = np.asarray(lying_down_probability, dtype=np.float64)
    if standing_up.shape != (times.size,) or lying_down.shape != (times.size,):
        raise ValueError("transition probability arrays must each have shape (N,)")

    initial_state = 1 if str(config["initial_state"]).upper() == "LYING" else 0
    confirm_points = max(1, int(config["confirm_points"]))
    min_dwell = max(0, int(config["min_dwell_points"]))
    margin = float(config["margin"])
    event_weight = float(config["transition_event_weight"])
    max_gap = int(config["max_sequence_gap_ms"])
    states = np.empty(times.size, dtype=np.int8)
    state = initial_state
    candidate = -1
    candidate_count = 0
    dwell = min_dwell

    for index in range(times.size):
        if index == 0 or times[index] - times[index - 1] > max_gap:
            state = initial_state
            candidate = -1
            candidate_count = 0
            dwell = min_dwell
            states[index] = state
            continue

        to_lying = posture[index, 1] + event_weight * lying_down[index]
        to_upright = posture[index, 0] + event_weight * standing_up[index]
        proposed = state
        if state == 0 and to_lying > to_upright + margin:
            proposed = 1
        elif state == 1 and to_upright > to_lying + margin:
            proposed = 0

        if proposed == state:
            candidate = -1
            candidate_count = 0
        else:
            candidate_count = candidate_count + 1 if candidate == proposed else 1
            candidate = proposed
            if dwell >= min_dwell and candidate_count >= confirm_points:
                state = proposed
                dwell = 0
                candidate = -1
                candidate_count = 0
        dwell += 1
        states[index] = state
    return states


def _wall_time(create_time_ms: int | float | None, relative_ms: int) -> str:
    if not create_time_ms:
        return ""
    base = datetime.fromtimestamp(
        float(create_time_ms) / 1000.0, tz=ZoneInfo("Asia/Shanghai")
    )
    return (base + timedelta(milliseconds=relative_ms)).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3]


def _interval_row(
    code: str,
    start_index: int,
    end_index: int,
    times_ms: np.ndarray,
    class_probability: np.ndarray,
    step_ms: int,
    create_time_ms: int | float | None,
    **metadata: object,
) -> dict[str, object]:
    start_ms = max(0, int(round(times_ms[start_index] - step_ms / 2)))
    end_ms = int(round(times_ms[end_index - 1] + step_ms / 2))
    selected = class_probability[start_index:end_index]
    return {
        "layer": LABEL_LAYER[code],
        "code": code,
        "label": LABEL_ZH[code],
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_s": round((end_ms - start_ms) / 1000.0, 3),
        "confidence_mean": round(float(selected.mean()), 6),
        "confidence_max": round(float(selected.max()), 6),
        "start_wall_bj": _wall_time(create_time_ms, start_ms),
        "end_wall_bj": _wall_time(create_time_ms, end_ms),
        **metadata,
    }


def _threshold_map(
    event_codes: Sequence[str],
    event_thresholds: Sequence[float] | Mapping[str, float],
) -> dict[str, float]:
    if isinstance(event_thresholds, Mapping):
        return {code: float(event_thresholds.get(code, 0.5)) for code in event_codes}
    values = list(event_thresholds)
    if len(values) != len(event_codes):
        raise ValueError("event threshold count does not match event code count")
    return {code: float(values[index]) for index, code in enumerate(event_codes)}


def postprocess_hierarchical_predictions(
    times_ms: np.ndarray,
    posture_probability: np.ndarray,
    walking_probability: np.ndarray,
    event_probability: np.ndarray,
    event_codes: Sequence[str],
    event_thresholds: Sequence[float] | Mapping[str, float],
    output_hz: float,
    create_time_ms: int | float | None = None,
    settings: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """Build the product timeline from posture, walking and independent events."""

    times = np.asarray(times_ms, dtype=np.int64)
    posture = np.asarray(posture_probability, dtype=np.float64)
    walking = np.asarray(walking_probability, dtype=np.float64).reshape(-1)
    events = np.asarray(event_probability, dtype=np.float64)
    codes = tuple(str(code) for code in event_codes)
    if posture.shape != (times.size, 2):
        raise ValueError("posture_probability shape does not match output times")
    if walking.shape != (times.size,):
        raise ValueError("walking_probability shape does not match output times")
    if events.shape != (times.size, len(codes)):
        raise ValueError("event_probability shape does not match output times/codes")
    if times.size == 0:
        return []

    config = {**DEFAULT_POSTPROCESS, **(settings or {})}
    state_config = {
        **DEFAULT_POSTPROCESS["state_machine"],
        **dict(config.get("state_machine", {}) or {}),
    }
    minimum_by_event = {
        **DEFAULT_POSTPROCESS["event_min_duration_s"],
        **dict(config.get("event_min_duration_s", {}) or {}),
    }
    thresholds = _threshold_map(codes, event_thresholds)
    event_index = {code: index for index, code in enumerate(codes)}
    zeros = np.zeros(times.size, dtype=np.float64)
    standing_up = (
        events[:, event_index["STANDING_UP"]]
        if "STANDING_UP" in event_index
        else zeros
    )
    lying_down = (
        events[:, event_index["LYING_DOWN"]]
        if "LYING_DOWN" in event_index
        else zeros
    )
    states = run_posture_state_machine(
        times, posture, standing_up, lying_down, state_config
    )

    walking_high = float(config["walking_threshold"])
    walking_low = max(
        0.02, walking_high - float(config["walking_hysteresis_margin"])
    )
    walking_active = hysteresis_binary(walking, walking_high, walking_low)
    walking_active = merge_and_filter_binary(
        walking_active,
        max(1, int(round(float(config["walking_min_duration_s"]) * output_hz))),
        max(0, int(round(float(config["walking_merge_gap_s"]) * output_hz))),
    )
    walking_active &= states == 0

    # The annotation protocol remains mutually exclusive. Internally walking is
    # an independent upright sub-state; at export it overlays STANDING.
    body_labels = states.copy()
    body_labels[walking_active] = 2
    body_confidence = np.column_stack((posture[:, 0], posture[:, 1], walking))
    step_ms = int(round(1000.0 / output_hz))
    rows: list[dict[str, object]] = []
    for class_index, start, end in _runs(body_labels):
        code = OUTPUT_BODY_CODES[class_index]
        confidence = body_confidence[:, class_index]
        rows.append(
            _interval_row(
                code,
                start,
                end,
                times,
                confidence,
                step_ms,
                create_time_ms,
                candidate_type="continuous_state",
                review_priority="standard",
                model_support="formal",
                review_recommended=bool(float(confidence[start:end].mean()) >= 0.60),
                source_head=("locomotion" if code == "WALKING" else "posture_state_machine"),
                decision_threshold=(walking_high if code == "WALKING" else 0.5),
            )
        )

    margin = float(config["hysteresis_margin"])
    merge_steps = max(
        0, int(round(float(config["event_merge_gap_s"]) * output_hz))
    )
    for class_index, code in enumerate(codes):
        high = thresholds[code]
        low = max(0.02, high - margin)
        active = hysteresis_binary(events[:, class_index], high, low)
        active = merge_and_filter_binary(
            active,
            max(
                1,
                int(
                    round(float(minimum_by_event.get(code, 1.0)) * output_hz)
                ),
            ),
            merge_steps,
        )
        support = "research" if code in RESEARCH_EVENT_CODES else "formal"
        for label, start, end in _runs(active.astype(np.int8)):
            if label != 1:
                continue
            mean_score = float(events[start:end, class_index].mean())
            priority = (
                "research"
                if support == "research"
                else "high_score"
                if mean_score >= min(0.99, high + 0.15)
                else "uncertain"
            )
            rows.append(
                _interval_row(
                    code,
                    start,
                    end,
                    times,
                    events[:, class_index],
                    step_ms,
                    create_time_ms,
                    candidate_type="event_candidate",
                    review_priority=priority,
                    model_support=support,
                    review_recommended=support == "formal",
                    source_head=f"event:{code}",
                    decision_threshold=high,
                    uncertainty=round(abs(mean_score - high), 6),
                )
            )
    return sorted(
        rows,
        key=lambda row: (
            int(row["start_ms"]),
            str(row["layer"]),
            str(row["code"]),
        ),
    )


def postprocess_body_event_predictions(
    times_ms: np.ndarray,
    body_probability: np.ndarray,
    event_probability: np.ndarray,
    body_codes: Sequence[str],
    event_codes: Sequence[str],
    event_thresholds: Sequence[float] | Mapping[str, float],
    output_hz: float,
    create_time_ms: int | float | None = None,
    settings: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """Adapt a historical body/event classifier to the current label contract.

    FEEDING is deliberately mapped to UPRIGHT evidence and is never emitted.
    WALKING is reused as an independent upright sub-state. This keeps the
    deployed checkpoint useful while making its product output match the current
    causal multi-task design.
    """

    body = np.asarray(body_probability, dtype=np.float64)
    codes = tuple(str(code) for code in body_codes)
    if body.ndim != 2 or body.shape[1] != len(codes):
        raise ValueError("legacy body probability shape is incompatible")
    body_index = {code: index for index, code in enumerate(codes)}
    if not {"LYING", "WALKING"}.issubset(body_index):
        raise ValueError("legacy body codes must include LYING and WALKING")
    lying = np.clip(body[:, body_index["LYING"]], 0.0, 1.0)
    upright = np.clip(1.0 - lying, 0.0, 1.0)
    walking = np.clip(body[:, body_index["WALKING"]], 0.0, 1.0)
    return postprocess_hierarchical_predictions(
        times_ms,
        np.column_stack((upright, lying)),
        walking,
        event_probability,
        event_codes,
        event_thresholds,
        output_hz,
        create_time_ms,
        settings,
    )


def postprocess_predictions(
    times_ms: np.ndarray,
    body_probability: np.ndarray,
    event_probability: np.ndarray,
    event_thresholds: Sequence[float] | Mapping[str, float],
    output_hz: float,
    create_time_ms: int | float | None = None,
    settings: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """Compatibility wrapper for the bundled ``HierarchicalMSResTCN``."""

    return postprocess_body_event_predictions(
        times_ms,
        body_probability,
        event_probability,
        BODY_CLASSES,
        EVENT_CLASSES,
        event_thresholds,
        output_hz,
        create_time_ms,
        settings,
    )
