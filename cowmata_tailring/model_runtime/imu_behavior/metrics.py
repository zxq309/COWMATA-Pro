from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

import numpy as np
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_recall_fscore_support,
)

from .schema import BODY_CLASSES, EVENT_CLASSES


def expected_calibration_error(
    confidence: np.ndarray, correct: np.ndarray, bins: int = 15
) -> float:
    if confidence.size == 0:
        return float("nan")
    edges = np.linspace(0.0, 1.0, bins + 1)
    error = 0.0
    for lower, upper in zip(edges[:-1], edges[1:]):
        selected = (confidence > lower) & (confidence <= upper)
        if selected.any():
            error += selected.mean() * abs(confidence[selected].mean() - correct[selected].mean())
    return float(error)


def multiclass_brier(probability: np.ndarray, target: np.ndarray) -> float:
    one_hot = np.eye(probability.shape[1], dtype=np.float64)[target]
    return float(np.mean(np.sum((probability - one_hot) ** 2, axis=1)))


def _binary_runs(values: np.ndarray, valid: np.ndarray) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, (active, usable) in enumerate(zip(values.astype(bool), valid.astype(bool))):
        if active and usable and start is None:
            start = index
        if start is not None and (not active or not usable):
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(values)))
    return runs


def segment_counts(
    prediction: np.ndarray,
    target: np.ndarray,
    valid: np.ndarray,
    session_keys: Sequence[str],
    iou_threshold: float = 0.30,
) -> tuple[int, int, int]:
    true_positive = false_positive = false_negative = 0
    keys = np.asarray(session_keys, dtype=object)
    for key in dict.fromkeys(keys.tolist()):
        selected = keys == key
        pred_runs = _binary_runs(prediction[selected], valid[selected])
        true_runs = _binary_runs(target[selected], valid[selected])
        candidates: list[tuple[float, int, int]] = []
        for pred_index, (pred_start, pred_end) in enumerate(pred_runs):
            for true_index, (true_start, true_end) in enumerate(true_runs):
                intersection = max(0, min(pred_end, true_end) - max(pred_start, true_start))
                union = max(pred_end, true_end) - min(pred_start, true_start)
                iou = intersection / union if union else 0.0
                if iou >= iou_threshold:
                    candidates.append((iou, pred_index, true_index))
        matched_pred: set[int] = set()
        matched_true: set[int] = set()
        for _, pred_index, true_index in sorted(candidates, reverse=True):
            if pred_index not in matched_pred and true_index not in matched_true:
                matched_pred.add(pred_index)
                matched_true.add(true_index)
        true_positive += len(matched_pred)
        false_positive += len(pred_runs) - len(matched_pred)
        false_negative += len(true_runs) - len(matched_true)
    return true_positive, false_positive, false_negative


def segment_statistics(
    prediction: np.ndarray,
    target: np.ndarray,
    valid: np.ndarray,
    session_keys: Sequence[str],
    iou_threshold: float,
    step_seconds: float = 0.5,
) -> dict:
    true_positive = false_positive = false_negative = 0
    matched_ious: list[float] = []
    start_errors: list[float] = []
    end_errors: list[float] = []
    keys = np.asarray(session_keys, dtype=object)
    for key in dict.fromkeys(keys.tolist()):
        selected = keys == key
        pred_runs = _binary_runs(prediction[selected], valid[selected])
        true_runs = _binary_runs(target[selected], valid[selected])
        candidates: list[tuple[float, int, int]] = []
        for pred_index, (pred_start, pred_end) in enumerate(pred_runs):
            for true_index, (true_start, true_end) in enumerate(true_runs):
                intersection = max(0, min(pred_end, true_end) - max(pred_start, true_start))
                union = max(pred_end, true_end) - min(pred_start, true_start)
                iou = intersection / union if union else 0.0
                if iou >= iou_threshold:
                    candidates.append((iou, pred_index, true_index))
        matched_pred: set[int] = set()
        matched_true: set[int] = set()
        for iou, pred_index, true_index in sorted(candidates, reverse=True):
            if pred_index in matched_pred or true_index in matched_true:
                continue
            matched_pred.add(pred_index)
            matched_true.add(true_index)
            matched_ious.append(float(iou))
            pred_start, pred_end = pred_runs[pred_index]
            true_start, true_end = true_runs[true_index]
            start_errors.append(abs(pred_start - true_start) * step_seconds)
            end_errors.append(abs(pred_end - true_end) * step_seconds)
        true_positive += len(matched_pred)
        false_positive += len(pred_runs) - len(matched_pred)
        false_negative += len(true_runs) - len(matched_true)
    return {
        "tp": true_positive,
        "fp": false_positive,
        "fn": false_negative,
        "f1": float(
            2 * true_positive / max(1, 2 * true_positive + false_positive + false_negative)
        ),
        "mean_matched_iou": float(np.mean(matched_ious)) if matched_ious else float("nan"),
        "start_mae_s": float(np.mean(start_errors)) if start_errors else float("nan"),
        "end_mae_s": float(np.mean(end_errors)) if end_errors else float("nan"),
    }


def evaluate_probabilities(
    body_probability: np.ndarray,
    event_probability: np.ndarray,
    body_target: np.ndarray,
    body_weight: np.ndarray,
    event_target: np.ndarray,
    event_mask: np.ndarray,
    event_thresholds: Sequence[float] | None = None,
    session_keys: Sequence[str] | None = None,
) -> dict:
    thresholds = np.asarray(event_thresholds or [0.5] * len(EVENT_CLASSES), dtype=np.float64)
    body_valid = (body_target >= 0) & (body_weight > 0)
    result: dict = {"body": {}, "events": {}, "summary": {}}
    if body_valid.any():
        body_true = body_target[body_valid]
        body_pred = body_probability[body_valid].argmax(axis=1)
        confidence = body_probability[body_valid].max(axis=1)
        result["body"] = {
            "valid_points": int(body_valid.sum()),
            "accuracy": float(accuracy_score(body_true, body_pred)),
            "balanced_accuracy": float(balanced_accuracy_score(body_true, body_pred)),
            "macro_f1": float(f1_score(body_true, body_pred, labels=range(len(BODY_CLASSES)), average="macro", zero_division=0)),
            "ece_15": expected_calibration_error(confidence, body_pred == body_true),
            "brier": multiclass_brier(body_probability[body_valid], body_true),
            "confusion_matrix": confusion_matrix(
                body_true, body_pred, labels=range(len(BODY_CLASSES))
            ).tolist(),
            "class_order": list(BODY_CLASSES),
        }
        precision, recall, f1, support = precision_recall_fscore_support(
            body_true,
            body_pred,
            labels=range(len(BODY_CLASSES)),
            zero_division=0,
        )
        result["body"]["per_class"] = {
            code: {
                "precision": float(precision[index]),
                "recall": float(recall[index]),
                "f1": float(f1[index]),
                "support_points": int(support[index]),
            }
            for index, code in enumerate(BODY_CLASSES)
        }
    else:
        result["body"] = {"valid_points": 0, "macro_f1": float("nan")}

    event_f1_values: list[float] = []
    event_ap_values: list[float] = []
    for class_index, code in enumerate(EVENT_CLASSES):
        valid = event_mask[:, class_index] > 0
        true = event_target[valid, class_index].astype(np.int64)
        probability = event_probability[valid, class_index]
        predicted = (probability >= thresholds[class_index]).astype(np.int64)
        positives = int(true.sum())
        negatives = int(true.size - positives)
        if true.size:
            precision, recall, f1, _ = precision_recall_fscore_support(
                true, predicted, average="binary", zero_division=0
            )
            brier = brier_score_loss(true, probability)
            ece = expected_calibration_error(
                np.where(predicted == 1, probability, 1.0 - probability), predicted == true
            )
        else:
            precision = recall = f1 = brier = ece = float("nan")
        ap = (
            float(average_precision_score(true, probability))
            if positives > 0 and negatives > 0
            else float("nan")
        )
        class_result = {
            "valid_points": int(true.size),
            "positive_points": positives,
            "negative_points": negatives,
            "threshold": float(thresholds[class_index]),
            "precision": float(precision),
            "recall": float(recall),
            "f1": float(f1),
            "average_precision": ap,
            "brier": float(brier),
            "ece_15": float(ece),
        }
        if session_keys is not None and true.size:
            selected_keys = np.asarray(session_keys, dtype=object)[valid]
            class_result["segment_metrics"] = {
                f"iou_{threshold:.2f}": segment_statistics(
                    predicted,
                    true,
                    np.ones_like(true, dtype=bool),
                    selected_keys,
                    threshold,
                )
                for threshold in (0.10, 0.25, 0.50)
            }
        result["events"][code] = class_result
        if np.isfinite(f1):
            event_f1_values.append(float(f1))
        if np.isfinite(ap):
            event_ap_values.append(ap)

    result["summary"] = {
        "body_macro_f1": float(result["body"].get("macro_f1", float("nan"))),
        "event_macro_f1": float(np.mean(event_f1_values)) if event_f1_values else float("nan"),
        "event_macro_average_precision": float(np.mean(event_ap_values))
        if event_ap_values
        else float("nan"),
    }
    return result


def metrics_by_device(
    body_probability: np.ndarray,
    event_probability: np.ndarray,
    body_target: np.ndarray,
    body_weight: np.ndarray,
    event_target: np.ndarray,
    event_mask: np.ndarray,
    thresholds: Sequence[float],
    session_keys: Sequence[str],
) -> dict[str, dict]:
    keys = np.asarray(session_keys, dtype=object)
    device_to_indices: defaultdict[str, list[int]] = defaultdict(list)
    for index, key in enumerate(keys):
        device_to_indices[str(key).split("/", 1)[0]].append(index)
    output: dict[str, dict] = {}
    for device, indices in device_to_indices.items():
        selected = np.asarray(indices, dtype=np.int64)
        output[device] = evaluate_probabilities(
            body_probability[selected],
            event_probability[selected],
            body_target[selected],
            body_weight[selected],
            event_target[selected],
            event_mask[selected],
            thresholds,
            keys[selected].tolist(),
        )["summary"]
    return output
