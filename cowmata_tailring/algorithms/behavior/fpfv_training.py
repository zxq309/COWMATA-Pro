"""FPFV training, validation and inference adapters.

This module has no dataset paths and does no work at import time. Callers pass
continuous per-device feature series and event dictionaries.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import json
import numpy as np

from .. import fpfv

CODE = fpfv.CODE
ALGORITHM_VERSION = fpfv.ALGORITHM_VERSION
FEATURE_VERSION = fpfv.FEATURE_VERSION
DEFAULT_WINDOW_MS = 45 * 60 * 1000.0
DEFAULT_EXCLUDE_BEFORE_MS = 8 * 60 * 1000.0
DEFAULT_EXCLUDE_AFTER_MS = 10 * 60 * 1000.0
HGB_DEFAULTS = dict(learning_rate=.05, max_leaf_nodes=15, min_samples_leaf=40,
                    l2_regularization=5.0, max_iter=300, max_features=.7,
                    early_stopping=False, random_state=38)


def prepare_sample(series, events=(), *, group=None, positive_window_ms=DEFAULT_WINDOW_MS,
                   exclude_before_ms=DEFAULT_EXCLUDE_BEFORE_MS,
                   exclude_after_ms=DEFAULT_EXCLUDE_AFTER_MS):
    """Convert one device series and its event list into trainable rows."""
    X, t_ms, names = fpfv.feature_matrix(series)
    t_ms = np.asarray(t_ms, float)
    y = np.zeros(len(t_ms), dtype=np.int8)
    keep = np.ones(len(t_ms), dtype=bool)
    used = []
    for event in events:
        if event.get("code", CODE) != CODE:
            continue
        point = float(event["t_ms"])
        pos = (t_ms >= point) & (t_ms <= point + float(positive_window_ms))
        y[pos] = 1
        ambiguous = ((t_ms >= point - float(exclude_before_ms)) & (t_ms < point))
        ambiguous |= ((t_ms > point + float(positive_window_ms)) &
                      (t_ms <= point + float(positive_window_ms) + float(exclude_after_ms)))
        keep &= ~ambiguous
        used.append(point)
    keep |= y == 1
    return dict(X=X, y=y, keep=keep, group=group, times_ms=t_ms,
                feature_names=names, events=used)


def _rows(samples):
    xs, ys, gs = [], [], []
    names = None
    for i, sample in enumerate(samples):
        item = (prepare_sample(sample["series"], sample.get("events", ()),
                                group=sample.get("group", i))
                if "series" in sample else sample)
        x = np.asarray(item.get("X", item.get("features")), float)
        y = np.asarray(item.get("y", item.get("labels")), int)
        keep = np.asarray(item.get("keep", np.ones(len(y), bool)), bool)
        if x.ndim != 2 or y.ndim != 1 or len(x) != len(y) or len(keep) != len(y):
            raise ValueError("Samples need matching X/features, y/labels and keep rows")
        x, y = x[keep], y[keep]
        if not np.isin(y, (0, 1)).all():
            raise ValueError("FPFV labels must be binary 0/1")
        xs.append(x); ys.append(y)
        gs.extend([item.get("group", i)] * len(y))
        names = names or item.get("feature_names")
    if not xs:
        raise ValueError("At least one FPFV sample is required")
    return np.vstack(xs), np.concatenate(ys), np.asarray(gs, dtype=object), names


def train_model(samples, *, feature_names=None, output_path=None, params=None,
                version=None):
    """Fit a numeric HistGradientBoosting model and optionally save JSON."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    X, y, _, inferred_names = _rows(samples)
    if np.unique(y).size != 2:
        raise ValueError("Both positive and background FPFV rows are required")
    cfg = dict(HGB_DEFAULTS)
    if params:
        cfg.update(params)
    model = HistGradientBoostingClassifier(**cfg)
    median = np.nan_to_num(np.nanmedian(X, axis=0), nan=0.0)
    Xi = np.where(np.isfinite(X), X, median)
    weight = np.where(y == 1, 1.0 / max(int(y.sum()), 1),
                      4.0 / max(int((y == 0).sum()), 1))
    weight /= np.mean(weight)
    model.fit(Xi, y, sample_weight=weight)
    names = list(feature_names or inferred_names or [f"f{i}" for i in range(X.shape[1])])
    payload = fpfv.export_hgb(model, names)
    payload.update(code=CODE, title="胎儿首个部位首次可见", version=version or
                   datetime.now(timezone.utc).strftime("fpfv-%Y%m%d-%H%M"),
                   algorithm=ALGORITHM_VERSION, feature_version=FEATURE_VERSION,
                   decoder=dict(fpfv.DEFAULTS), training=dict(params=cfg, rows=int(len(y)),
                   positives=int(y.sum()), negatives=int((y == 0).sum())),
                   requires_video_confirmation=True)
    if output_path is not None:
        save_model(payload, output_path)
    return payload


def predict_scores(model, series):
    X, times_ms, names = fpfv.feature_matrix(series)
    if list(names) != list(model.get("features", ())):
        raise ValueError("FPFV feature order does not match model")
    return fpfv.predict_gbdt(model, X), times_ms


def predict(model, series, *, decoder=None):
    scores, times_ms = predict_scores(model, series)
    params = dict(model.get("decoder", {}))
    if decoder:
        params.update(decoder)
    return dict(scores=scores, times_ms=times_ms,
                events=fpfv.decode(times_ms, scores, **params),
                feature_version=FEATURE_VERSION)


def validate_grouped(samples, *, feature_names=None, decoder=None, **fit_kwargs):
    """Leave-one-device/group-out validation with held-out data excluded from fit."""
    expanded = []
    for i, sample in enumerate(samples):
        expanded.append(prepare_sample(sample["series"], sample.get("events", ()),
                                       group=sample.get("group", i))
                       if "series" in sample else sample)
    groups = [s.get("group", i) for i, s in enumerate(expanded)]
    unique = list(dict.fromkeys(groups))
    if len(unique) < 2:
        raise ValueError("At least two validation groups are required")
    results = []
    for group in unique:
        train = [s for s, g in zip(expanded, groups) if g != group]
        test = [s for s, g in zip(expanded, groups) if g == group]
        payload = train_model(train, feature_names=feature_names, **fit_kwargs)
        for sample in test:
            if "series" in sample:
                pred = predict(payload, sample["series"], decoder=decoder)
                results.append(dict(group=str(group), events=len(pred["events"]),
                                    rows=len(pred["scores"])))
            else:
                X = np.asarray(sample.get("X", sample.get("features")), float)
                p = fpfv.predict_gbdt(payload, X)
                results.append(dict(group=str(group), events=0, rows=len(p),
                                    mean_score=float(np.nanmean(p))))
    return dict(protocol="leave-one-group-out", folds=results,
                groups=len(unique), rows=sum(r["rows"] for r in results))


def load_model(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != fpfv.MODEL_SCHEMA or doc.get("code") != CODE:
        raise ValueError("Incompatible FPFV model")
    return doc


def save_model(model, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


train = train_model
validate = validate_grouped
