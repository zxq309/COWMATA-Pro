"""Training core for the stand-up detector.

The package accepts decoded arrays and writes nothing unless output_dir is supplied.
Research data, caches and figures stay outside this package.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from .detector import INV_IDX, MODEL_SCHEMA, lr_predict, refine_boundaries
from .features import FEATURE_NAMES, candidate_features, record_reference
from .signal import DEFAULT_PARAMS, candidates, preprocess

ALGORITHM_VERSION = "standup-tailring-2"


def build_training_example(times_ms, acc_g, gyro_dps, events=()):
    """Decode one record into candidate features and interval labels.

    events is an iterable of mappings with code (STANDING_UP or LYING_DOWN), t0
    and t1 milliseconds. Candidate extraction is deterministic and shared with
    inference.
    """
    sig = preprocess(np.asarray(times_ms), np.asarray(acc_g), np.asarray(gyro_dps))
    cands, _, _ = candidates(sig, DEFAULT_PARAMS)
    ref = record_reference(sig)
    kept, rows, bounds, labels = [], [], [], []
    truth = list(events)
    for candidate in cands:
        feat = candidate_features(sig, candidate, ref)
        if feat is None:
            continue
        label = 0
        for event in truth:
            if min(float(event.get("t1", event["t0"])), candidate["t1"]) - max(float(event["t0"]), candidate["t0"]) > 0:
                label = 1 if event.get("code") == "STANDING_UP" else 2 if event.get("code") == "LYING_DOWN" else 0
                if label:
                    break
        kept.append(candidate)
        rows.append(feat)
        bounds.append(refine_boundaries(sig, candidate))
        labels.append(label)
    X = np.asarray(rows, dtype=float).reshape((-1, len(FEATURE_NAMES))) if rows else np.zeros((0, len(FEATURE_NAMES)))
    return dict(X=X, y=np.asarray(labels, dtype=np.int64), candidates=kept,
                bounds=np.asarray(bounds, dtype=float).reshape((-1, 2)) if bounds else np.zeros((0, 2)),
                signal_quality=sig.get("quality", {}))


def fit_linear(X, y):
    """Fit the detector's rotation-invariant 3-class logistic model."""
    X, y = _checked_arrays(X, y)
    if len(np.unique(y)) < 2:
        raise ValueError("stand-up training needs at least two classes")
    med = np.nan_to_num(np.nanmedian(X, axis=0))
    x = np.where(np.isfinite(X), X, med)[:, INV_IDX]
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    scale = StandardScaler().fit(x)
    clf = LogisticRegression(C=0.5, max_iter=5000, class_weight="balanced", multi_class="auto").fit(scale.transform(x), y)
    return dict(median=med[INV_IDX].tolist(), mean=scale.mean_.tolist(), scale=scale.scale_.tolist(),
                coef=clf.coef_.tolist(), intercept=clf.intercept_.tolist(),
                features=[FEATURE_NAMES[i] for i in INV_IDX], classes=clf.classes_.tolist())


def _checked_arrays(X, y):
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=np.int64)
    if X.ndim != 2 or X.shape[1] != len(FEATURE_NAMES) or len(X) != len(y):
        raise ValueError("X must have shape (n, len(FEATURE_NAMES)) and match y")
    if len(X) == 0:
        raise ValueError("empty stand-up training data")
    return X, y


def validate_model(model, X, y):
    """Return deterministic in-sample sanity metrics; use group CV upstream."""
    X, y = _checked_arrays(X, y)
    p = lr_predict(model, X[:, INV_IDX])
    pred = np.argmax(p, axis=1)
    return dict(samples=int(len(y)), accuracy=float(np.mean(pred == y)),
                classes=[int(v) for v in sorted(set(y.tolist()))],
                score_sha256=hashlib.sha256(p.astype(np.float32).tobytes()).hexdigest())


def train_arrays(X, y, *, output_dir=None, version=None, threshold=0.5, decoding="viterbi"):
    """Train a portable linear model and optionally export a detector manifest."""
    model = fit_linear(X, y)
    metrics = validate_model(model, X, y)
    manifest = dict(schema=MODEL_SCHEMA, version=version or ALGORITHM_VERSION,
                    algorithm=ALGORITHM_VERSION, code="STANDING_UP", label="起立过程",
                    candidate_params=dict(DEFAULT_PARAMS), decoding=decoding,
                    threshold=float(threshold), viterbi_floor=float(threshold),
                    lr=model, cnn_weight=0.0, cnn_file=None, metrics=metrics,
                    training=dict(samples=int(len(y)), feature_count=len(FEATURE_NAMES)))
    if output_dir is not None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "model.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
