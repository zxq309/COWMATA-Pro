from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.special import expit, logsumexp

from .schema import EVENT_CLASSES


def _positive_interval_count(
    target: np.ndarray,
    valid: np.ndarray,
    session_keys: Sequence[str] | None,
) -> int:
    if session_keys is None:
        values = target[valid].astype(bool)
        return int(np.sum(values & ~np.r_[False, values[:-1]])) if values.size else 0
    keys = np.asarray(session_keys, dtype=object)
    count = 0
    for key in dict.fromkeys(keys.tolist()):
        selected = (keys == key) & valid
        values = target[selected].astype(bool)
        if values.size:
            count += int(np.sum(values & ~np.r_[False, values[:-1]]))
    return count


def _fit_scalar_temperature(objective) -> tuple[float, float, float]:
    before = float(objective(0.0))
    result = minimize_scalar(objective, bounds=(np.log(0.25), np.log(4.0)), method="bounded")
    temperature = float(np.exp(result.x)) if result.success else 1.0
    after = float(objective(np.log(temperature)))
    return temperature, before, after


def fit_temperatures(
    body_logits: np.ndarray,
    event_logits: np.ndarray,
    body_target: np.ndarray,
    body_weight: np.ndarray,
    event_target: np.ndarray,
    event_mask: np.ndarray,
    min_event_positives: int = 5,
    min_event_intervals: int = 3,
    session_keys: Sequence[str] | None = None,
) -> dict:
    body_valid = (body_target >= 0) & (body_weight > 0)
    if body_valid.sum() >= 20:
        logits = body_logits[body_valid].astype(np.float64)
        target = body_target[body_valid].astype(np.int64)

        def body_objective(log_temperature: float) -> float:
            scaled = logits / np.exp(log_temperature)
            return float(np.mean(logsumexp(scaled, axis=1) - scaled[np.arange(target.size), target]))

        body_temperature, body_before, body_after = _fit_scalar_temperature(body_objective)
    else:
        body_temperature, body_before, body_after = 1.0, float("nan"), float("nan")

    event_temperatures: list[float] = []
    event_status: dict[str, dict] = {}
    for class_index, code in enumerate(EVENT_CLASSES):
        valid = event_mask[:, class_index] > 0
        logits = event_logits[valid, class_index].astype(np.float64)
        target = event_target[valid, class_index].astype(np.float64)
        positives, negatives = int(target.sum()), int(target.size - target.sum())
        positive_intervals = _positive_interval_count(
            event_target[:, class_index], valid, session_keys
        )
        if (
            positives >= min_event_positives
            and negatives >= min_event_positives
            and positive_intervals >= min_event_intervals
        ):

            def event_objective(log_temperature: float) -> float:
                scaled = logits / np.exp(log_temperature)
                return float(np.mean(np.logaddexp(0.0, scaled) - target * scaled))

            temperature, before, after = _fit_scalar_temperature(event_objective)
            status = "fitted"
        else:
            temperature, before, after = 1.0, float("nan"), float("nan")
            status = "insufficient_validation_support"
        event_temperatures.append(temperature)
        event_status[code] = {
            "temperature": temperature,
            "status": status,
            "positive_points": positives,
            "positive_intervals": positive_intervals,
            "negative_points": negatives,
            "nll_before": before,
            "nll_after": after,
        }
    return {
        "body_temperature": body_temperature,
        "body_nll_before": body_before,
        "body_nll_after": body_after,
        "event_temperatures": event_temperatures,
        "event_details": event_status,
    }


def apply_calibration(
    body_logits: np.ndarray,
    event_logits: np.ndarray,
    calibration: dict | None,
) -> tuple[np.ndarray, np.ndarray]:
    calibration = calibration or {}
    body_temperature = max(float(calibration.get("body_temperature", 1.0)), 1e-3)
    event_temperatures = np.asarray(
        calibration.get("event_temperatures", [1.0] * event_logits.shape[1]),
        dtype=np.float64,
    )
    event_temperatures = np.maximum(event_temperatures, 1e-3)
    scaled_body = body_logits.astype(np.float64) / body_temperature
    scaled_body -= scaled_body.max(axis=1, keepdims=True)
    body_probability = np.exp(scaled_body)
    body_probability /= body_probability.sum(axis=1, keepdims=True)
    event_probability = expit(event_logits.astype(np.float64) / event_temperatures[None, :])
    return body_probability.astype(np.float32), event_probability.astype(np.float32)


def tune_event_thresholds(
    event_probability: np.ndarray,
    event_target: np.ndarray,
    event_mask: np.ndarray,
    minimum_positives: int = 2,
    minimum_intervals: int = 3,
    session_keys: Sequence[str] | None = None,
) -> tuple[list[float], dict]:
    thresholds: list[float] = []
    details: dict = {}
    grid = np.arange(0.10, 0.91, 0.025)
    for class_index, code in enumerate(EVENT_CLASSES):
        valid = event_mask[:, class_index] > 0
        target = event_target[valid, class_index].astype(bool)
        probability = event_probability[valid, class_index]
        positives = int(target.sum())
        positive_intervals = _positive_interval_count(
            event_target[:, class_index], valid, session_keys
        )
        if (
            positives < minimum_positives
            or (~target).sum() < minimum_positives
            or positive_intervals < minimum_intervals
        ):
            best_threshold, best_f1, status = 0.5, float("nan"), "insufficient_validation_support"
        else:
            scores = []
            for threshold in grid:
                prediction = probability >= threshold
                tp = int((prediction & target).sum())
                fp = int((prediction & ~target).sum())
                fn = int((~prediction & target).sum())
                f1 = 2 * tp / max(1, 2 * tp + fp + fn)
                scores.append((f1, -abs(float(threshold) - 0.5), float(threshold)))
            best_f1, _, best_threshold = max(scores)
            status = "fitted"
        thresholds.append(float(best_threshold))
        details[code] = {
            "threshold": float(best_threshold),
            "validation_f1": float(best_f1),
            "positive_points": positives,
            "positive_intervals": positive_intervals,
            "status": status,
        }
    return thresholds, details
