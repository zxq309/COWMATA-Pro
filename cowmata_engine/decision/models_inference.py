"""Inference-only model decoder for the 4.3.4 calving package.

This module deliberately contains no fit/train code. JSON model documents are
validated and scored using the same numerical paths as the client runtime.
"""
from __future__ import annotations

import base64
import warnings

import numpy as np


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -40, 40)))


def _logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def prep_apply(prep, x, *, scale=False, indicators=True):
    median = np.asarray(prep["median"])
    missing = ~np.isfinite(x)
    filled = np.where(missing, median, x)
    if scale:
        filled = (filled - np.asarray(prep["mean"])) / np.asarray(prep["std"])
    return np.column_stack([filled, missing.astype(float)]) if indicators else filled


def _tree_predict(doc, z):
    left, right = np.asarray(doc["left"]), np.asarray(doc["right"])
    feature, threshold = np.asarray(doc["feature"]), np.asarray(doc["threshold"])
    value = np.asarray(doc["value"], dtype=float)
    n = len(left)
    inner = left >= 0
    if (n == 0 or any(len(a) != n for a in (right, feature, threshold, value))
            or np.any(left[inner] <= np.flatnonzero(inner))
            or np.any(right[inner] <= np.flatnonzero(inner))
            or np.any(left[inner] >= n) or np.any(right[inner] >= n)
            or np.any(feature[inner] >= z.shape[1]) or np.any(feature[inner] < 0)):
        raise ValueError("决策树结构无效")
    node = np.zeros(len(z), dtype=int)
    while True:
        active = np.flatnonzero(left[node] >= 0)
        if not len(active):
            break
        current = node[active]
        node[active] = np.where(z[active, feature[current]] <= threshold[current], left[current], right[current])
    return value[node]


def _expert_raw(doc, x):
    total = np.zeros(len(x))
    weight_sum = np.zeros(len(x))
    for index, direction, weight, _, scale in doc["priors"]:
        z = x[:, index] / scale
        ok = np.isfinite(z)
        signal = np.abs(z) if direction == 0 else direction * z
        total += np.where(ok, weight * np.clip(signal, 0, 8), 0.0)
        weight_sum += np.where(ok, weight, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(weight_sum > 0, total / np.maximum(weight_sum, 1e-9), 0.0)


def _deviation_raw(doc, x):
    idx = [i for i, _, _, _, _ in doc["priors"]]
    scales = np.asarray([scale for *_, scale in doc["priors"]], dtype=float)
    if not idx:
        return np.zeros(len(x))
    z = x[:, idx] / scales
    ok = np.isfinite(z)
    with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        value = np.sqrt(np.nanmean(np.where(ok, np.clip(z, -10, 10) ** 2, np.nan), axis=1))
    return np.where(np.isfinite(value), value, 0.0)


def predict_model(doc, x):
    x = np.asarray(x, dtype=float)
    if x.ndim != 2 or x.shape[1] != len(doc["columns"]):
        raise ValueError("决策模型输入列数不一致")
    algorithm = doc["algorithm"]
    if algorithm in ("expert_rules", "baseline_deviation"):
        raw = (_expert_raw if algorithm == "expert_rules" else _deviation_raw)(doc, x)
        a, b = doc["platt"]
        return _sigmoid(a * raw + b)
    if algorithm == "logistic":
        z = prep_apply(doc["prep"], x, scale=True)
        return _sigmoid(z @ np.asarray(doc["coef"]) + doc["intercept"])
    if algorithm in ("decision_tree", "random_forest", "extra_trees"):
        z = prep_apply(doc["prep"], x)
        return np.mean([_tree_predict(t, z) for t in doc["trees"]], axis=0)
    if algorithm == "adaboost":
        z = prep_apply(doc["prep"], x)
        votes = np.zeros(len(z))
        for tree, weight in zip(doc["trees"], doc["weights"]):
            votes += weight * np.where(_tree_predict(tree, z) >= 0.5, 1.0, -1.0)
        return _sigmoid(2.0 * votes / max(sum(doc["weights"]), 1e-9) * 2.0)
    if algorithm == "xgboost":
        import xgboost as xgb
        booster = xgb.Booster(params=dict(nthread=2))
        booster.load_model(bytearray(base64.b64decode(doc["booster"])))
        return booster.predict(xgb.DMatrix(x, missing=np.nan))
    if algorithm == "stacking":
        meta = np.column_stack([predict_model(base, x) for base in doc["bases"]])
        combined = _sigmoid(_logit(meta) @ np.asarray(doc["meta"]["coef"]) + doc["meta"]["intercept"])
        return np.interp(combined, doc["isotonic"]["x"], doc["isotonic"]["y"])
    raise ValueError(f"未知决策算法：{algorithm}")


def predict_time_to_event(doc, x):
    import xgboost as xgb
    booster = xgb.Booster(params=dict(nthread=2))
    booster.load_model(bytearray(base64.b64decode(doc["booster"])))
    raw = np.asarray(booster.predict(xgb.DMatrix(np.asarray(x, dtype=float), missing=np.nan)))
    raw = np.sort(raw.reshape(len(x), -1), axis=1)
    q = float(doc.get("conformal_log", 0.0))
    raw[:, 0] -= q
    raw[:, -1] += q
    return np.clip(np.expm1(raw), 0.0, doc.get("max_hours", 240.0))
