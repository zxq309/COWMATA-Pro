"""Calf fully expelled (CFE) behavior algorithm.

Pure, path-independent numeric implementation. Research scripts and outputs
stay outside the product source tree. Public functions accept explicit arrays,
records and model payloads.
"""
from __future__ import annotations

from pathlib import Path
import json
import numpy as np

from ..models import export_forest, predict_forest, score_events

CODE = "CALF_FULLY_EXPELLED"
ALGORITHM_VERSION = "cfe-multiscale-1"
FEATURE_VERSION = "cfe-context-1"
EDGES = (-1800, -900, -300, -120, -60, -20, 0, 20, 60, 120, 300, 600, 1200)
SIGNALS = ("gyro", "dyn", "turn", "magturn", "active", "elev")


def _csum(x):
    x = np.asarray(x, float)
    ok = np.isfinite(x)
    return np.r_[0.0, np.cumsum(np.where(ok, x, 0.0))], np.r_[0, np.cumsum(ok)]


def _wmean(cs, cn, lo, hi, n):
    i = np.arange(n)
    a, b = np.clip(i + lo, 0, n), np.clip(i + hi, 0, n)
    count = cn[b] - cn[a]
    width = max(1, hi - lo)
    return np.divide(cs[b] - cs[a], count, out=np.full(n, np.nan), where=count > 0), count / float(width)


def base_signals(acc, gyr=None, mag=None, gyro_energy=None, acc_energy=None):
    """Return rotation-invariant 1 Hz signals from already resampled arrays."""
    acc = np.asarray(acc, float)
    if acc.ndim != 2 or acc.shape[1] != 3:
        raise ValueError("acc must have shape (n, 3)")
    n = len(acc)
    gyr = np.zeros_like(acc) if gyr is None else np.asarray(gyr, float)
    mag = np.asarray(acc if mag is None else mag, float)
    if gyr.shape != acc.shape or mag.shape != acc.shape:
        raise ValueError("gyr and mag must have shape (n, 3)")
    u = acc / np.maximum(np.linalg.norm(acc, axis=1, keepdims=True), 1e-6)
    mu = mag / np.maximum(np.linalg.norm(mag, axis=1, keepdims=True), 1e-6)
    turn = np.full(n, np.nan)
    mturn = np.full(n, np.nan)
    if n > 1:
        turn[1:] = np.degrees(np.arccos(np.clip(np.sum(u[1:] * u[:-1], 1), -1, 1)))
        mturn[1:] = np.degrees(np.arccos(np.clip(np.sum(mu[1:] * mu[:-1], 1), -1, 1)))
    hang = np.array([0.0, -1.0, 0.0])
    ok = np.isfinite(u).all(1)
    if ok.sum() > 600 and np.nanmedian(-u[ok, 1]) < .5:
        ref = np.nanmedian(u[ok], 0)
        hang = ref / max(np.linalg.norm(ref), 1e-6)
    elev = np.degrees(np.arccos(np.clip(u @ hang, -1, 1)))
    if gyro_energy is None:
        gyro_energy = np.linalg.norm(gyr, axis=1)
    if acc_energy is None:
        acc_energy = np.linalg.norm(acc - np.nanmedian(acc, axis=0), axis=1)
    gyro_energy, acc_energy = np.asarray(gyro_energy, float), np.asarray(acc_energy, float)
    if gyro_energy.shape != (n,) or acc_energy.shape != (n,):
        raise ValueError("energy signals must have shape (n,)")
    dyn = np.maximum(acc_energy, 0)
    return dict(elev=elev, gyro=np.log10(np.maximum(gyro_energy, 0) + 1),
                dyn=np.log10(dyn + .01), turn=np.minimum(turn, 90.0),
                magturn=np.minimum(mturn, 90.0),
                active=np.where(np.isfinite(dyn), (dyn > .1).astype(float), np.nan),
                u=u, mu=mu)


def context_features(sig):
    """Build the research feature law without filesystem dependency."""
    missing = set(SIGNALS) - set(sig)
    if missing:
        raise ValueError("Missing signals: " + ", ".join(sorted(missing)))
    n = len(np.asarray(sig["gyro"]))
    if n == 0:
        return np.empty((0, 0), np.float32), []
    cols = {}
    wins = list(zip(EDGES[:-1], EDGES[1:]))
    for key in SIGNALS:
        cs, cn = _csum(sig[key])
        cs2, _ = _csum(np.asarray(sig[key], float) ** 2)
        for lo, hi in wins:
            m, cov = _wmean(cs, cn, lo, hi, n)
            cols[f"{key}_{lo}_{hi}"] = m
            if key == "gyro":
                cols[f"cov_{lo}_{hi}"] = cov
            if key in ("gyro", "dyn", "elev"):
                m2, _ = _wmean(cs2, cn, lo, hi, n)
                cols[f"{key}_sd_{lo}_{hi}"] = np.sqrt(np.maximum(0, m2 - m * m))
    for key in ("gyro", "dyn", "active", "elev"):
        cs, cn = _csum(sig[key])
        base, _ = _wmean(cs, cn, -21600, -1800, n)
        for lo, hi in ((-300, -20), (20, 120), (120, 600), (300, 1200)):
            m, _ = _wmean(cs, cn, lo, hi, n)
            cols[f"rel_{key}_{lo}_{hi}"] = m - base
    for key in ("gyro", "dyn", "active", "elev"):
        g = lambda lo, hi: cols[f"{key}_{lo}_{hi}"]
        cols[key + "_post_minus_pre"] = np.nanmean([g(120, 300), g(300, 600)], axis=0) - np.nanmean([g(-300, -120), g(-120, -60)], axis=0)
        cols[key + "_rest_minus_pre"] = np.nanmean([g(20, 60), g(60, 120)], axis=0) - np.nanmean([g(-120, -60), g(-60, -20)], axis=0)
        cols[key + "_rest_minus_post"] = np.nanmean([g(20, 60), g(60, 120)], axis=0) - np.nanmean([g(120, 300), g(300, 600)], axis=0)
    for name, vec in (("u", np.asarray(sig["u"], float)), ("mu", np.asarray(sig["mu"], float))):
        comps = []
        for lo, hi in ((-300, -20), (20, 120), (120, 600), (600, 1200)):
            means = []
            for j in range(3):
                cs, cn = _csum(vec[:, j])
                means.append(_wmean(cs, cn, lo, hi, n)[0])
            comps.append(np.column_stack(means))
        def angle(a, b):
            den = np.maximum(np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1), 1e-9)
            return np.degrees(np.arccos(np.clip(np.sum(a * b, 1) / den, -1, 1)))
        cols[name + "_shift_pre_rest"] = angle(comps[0], comps[1])
        cols[name + "_shift_pre_post"] = angle(comps[0], comps[2])
        cols[name + "_shift_rest_post"] = angle(comps[1], comps[2])
        cols[name + "_shift_pre_late"] = angle(comps[0], comps[3])
    x = np.asarray(sig["u"], float)
    def mvec(lo, hi):
        vals = []
        for j in range(3):
            cs, cn = _csum(x[:, j])
            vals.append(_wmean(cs, cn, lo, hi, n)[0])
        return np.column_stack(vals)
    for w in (5, 10, 20, 60):
        a, b = mvec(-w, -1), mvec(1, w)
        den = np.maximum(np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1), 1e-9)
        cols[f"step_{w}"] = np.degrees(np.arccos(np.clip(np.sum(a*b, 1) / den, -1, 1)))
    after, before = mvec(2, 60), mvec(-60, -2)
    cols["ori_noise_post60"] = 1 - np.linalg.norm(after, axis=1)
    cols["ori_noise_pre60"] = 1 - np.linalg.norm(before, axis=1)
    act = np.asarray(sig["active"], float)
    onset = np.full(n, np.nan)
    if n > 1:
        onset[1:] = np.where(np.isfinite(act[1:]) & np.isfinite(act[:-1]), ((act[1:] > .5) & (act[:-1] < .5)).astype(float), np.nan)
    cs, cn = _csum(onset)
    for lo, hi in ((-7200, -3600), (-3600, -1800), (-1800, -600), (-600, -60), (60, 600)):
        cols[f"burst_rate_{lo}_{hi}"] = _wmean(cs, cn, lo, hi, n)[0] * 60
    cs, cn = _csum(sig["elev"])
    cols["tail_drop_600"] = _wmean(cs, cn, -600, -30, n)[0] - _wmean(cs, cn, 60, 600, n)[0]
    cols["tail_drop_1800"] = _wmean(cs, cn, -1800, -60, n)[0] - _wmean(cs, cn, 60, 1800, n)[0]
    cols["tail_drop_120"] = _wmean(cs, cn, -120, -5, n)[0] - _wmean(cs, cn, 5, 120, n)[0]
    names = list(cols)
    return np.column_stack([cols[k] for k in names]).astype(np.float32), names


def feature_matrix(series):
    if "signals" in series:
        series = series["signals"]
    return context_features(series)


def _coerce_training_samples(samples):
    xs, ys, groups = [], [], []
    for i, sample in enumerate(samples):
        x = np.asarray(sample["X"] if "X" in sample else sample["features"], float)
        y = np.asarray(sample["y"] if "y" in sample else sample["labels"], int)
        if x.ndim != 2 or y.ndim != 1 or len(x) != len(y):
            raise ValueError("Each sample needs matching X/features and y/labels")
        if not np.isin(y, (0, 1)).all():
            raise ValueError("CFE labels must be binary 0/1")
        xs.append(x); ys.append(y); groups.extend([sample.get("group", i)] * len(y))
    if not xs:
        raise ValueError("At least one sample is required")
    return np.vstack(xs), np.concatenate(ys), np.asarray(groups, object)


def train_model(samples, *, feature_names=None, output_path=None, n_estimators=128,
                random_state=38, max_depth=8, min_samples_leaf=10):
    from sklearn.ensemble import ExtraTreesClassifier
    X, y, _ = _coerce_training_samples(samples)
    if np.unique(y).size != 2:
        raise ValueError("Both positive and background rows are required")
    median = np.nan_to_num(np.nanmedian(X, axis=0), nan=0.0)
    model = ExtraTreesClassifier(n_estimators=int(n_estimators), max_depth=int(max_depth),
                                 min_samples_leaf=int(min_samples_leaf), max_features=.3,
                                 n_jobs=-1, random_state=int(random_state))
    model.fit(np.where(np.isfinite(X), X, median), y)
    payload = export_forest(model, feature_names or [f"f{i}" for i in range(X.shape[1])], median)
    payload.update(code=CODE, title="犊牛全部娩出", algorithm=ALGORITHM_VERSION,
                   feature_version=FEATURE_VERSION,
                   training=dict(rows=int(len(y)), positives=int(y.sum()), negatives=int((y == 0).sum()),
                                 random_state=int(random_state)))
    if output_path is not None:
        save_model(payload, output_path)
    return payload


def predict_scores(model, X):
    if hasattr(model, "predict_proba"):
        return model.predict_proba(np.asarray(X, float))[:, 1]
    return predict_forest(model, np.asarray(X, float))


def predict(model, X, times_ms=None, *, threshold=.5, duration_ms=None):
    scores = np.asarray(predict_scores(model, X), float)
    if times_ms is None:
        return scores
    t = np.asarray(times_ms, float)
    if len(t) != len(scores):
        raise ValueError("times_ms and X must have equal length")
    duration_ms = float(duration_ms if duration_ms is not None else (t[-1] - t[0] if len(t) else 0.0))
    events = score_events(scores, np.isfinite(scores), code=CODE, threshold=float(threshold), duration_ms=duration_ms)
    offset = float(t[0]) if len(t) else 0.0
    for e in events:
        e["point_ms"] += offset; e["start_ms"] += offset; e["end_ms"] += offset
    return dict(scores=scores, events=events)


def validate_grouped(samples, *, feature_names=None, threshold=.5, **fit_kwargs):
    from sklearn.model_selection import LeaveOneGroupOut
    X, y, groups = _coerce_training_samples(samples)
    if len(np.unique(groups)) < 2:
        raise ValueError("At least two groups are required")
    rows = []
    for train_idx, test_idx in LeaveOneGroupOut().split(X, y, groups):
        payload = train_model([{"X": X[train_idx], "y": y[train_idx]}],
                              feature_names=feature_names, **fit_kwargs)
        p = predict_scores(payload, X[test_idx])
        rows.append(dict(group=str(groups[test_idx[0]]), rows=int(len(test_idx)),
                         positives=int(y[test_idx].sum()), mean_score=float(np.mean(p)),
                         hit_rate=float(np.mean((p >= threshold) == y[test_idx]))))
    return dict(protocol="leave-one-group-out", folds=rows, rows=int(len(y)),
                groups=int(len(np.unique(groups))))


def load_model(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != "numeric-forest-1" or doc.get("code") != CODE:
        raise ValueError("Incompatible CFE model")
    return doc


def save_model(model, path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
