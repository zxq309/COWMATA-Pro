"""Pure LYING_DOWN training wrapper for decoded arrays."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from sklearn.ensemble import ExtraTreesClassifier

from .liedown import ALGORITHM, DEFAULT_PARAMS, MODEL_FEATURES, candidates, event_features, prepare
from .models import export_forest


def build_training_example(times_ms, acc_g, gyro_dps, events=()):
    sig = prepare(np.asarray(times_ms), np.asarray(acc_g), np.asarray(gyro_dps))
    peaks = candidates(sig, DEFAULT_PARAMS["min_angle_deg"], DEFAULT_PARAMS["min_sep_s"])
    X = event_features(sig, peaks)
    labels = np.zeros(len(peaks), dtype=np.int64)
    for i, peak in enumerate(peaks):
        t = float(peak) * 1000.0 / 50.0
        for event in events:
            a, b = float(event.get("start_ms", event.get("t0", 0))), float(event.get("end_ms", event.get("t1", event.get("t0", 0))))
            if a <= t <= b:
                labels[i] = 1 if event.get("code", "LYING_DOWN") == "LYING_DOWN" else 0
                break
    return X, labels


def train_arrays(X, y, *, output_path=None, threshold=.3, random_state=38,
                 n_estimators=128, min_samples_leaf=8):
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=np.int64)
    if X.ndim != 2 or X.shape[1] != len(MODEL_FEATURES) or len(X) != len(y):
        raise ValueError("X/y shape does not match LYING_DOWN features")
    if set(np.unique(y)) != {0, 1}:
        raise ValueError("LYING_DOWN training needs both classes")
    median = np.nan_to_num(np.nanmedian(X, axis=0), nan=0.0)
    model = ExtraTreesClassifier(n_estimators=int(n_estimators), min_samples_leaf=int(min_samples_leaf),
                                 class_weight="balanced", random_state=int(random_state), n_jobs=-1)
    model.fit(np.where(np.isfinite(X), X, median), y)
    payload = export_forest(model, list(MODEL_FEATURES), median)
    payload.update(code="LYING_DOWN", algorithm=ALGORITHM, threshold=float(threshold),
                   params=dict(DEFAULT_PARAMS), training=dict(rows=int(len(y)), positives=int(y.sum()),
                   random_state=int(random_state)))
    if output_path is not None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


train = train_arrays
__all__ = ["build_training_example", "train_arrays", "train"]
