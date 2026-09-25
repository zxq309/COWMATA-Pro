"""FETAL_PART_FIRST_VISIBLE (胎儿首个部位首次可见) — tail-ring IMU standard algorithm.

Law (规律), established on the COWMATA_Behavior_Dataset (16 video-timed + 9 coarse calvings,
~3100 h of background; see docs/FPFV_ALGORITHM.md):

  * Tail axis: a hanging tail puts gravity on sensor -Y, so tail elevation
    ``elev = arccos(-u_y)`` is invariant to the ring turning around the tail.
  * Hours before the event the tail-raise frequency climbs (elev crossing 45 deg:
    ~51 /h at the event vs ~4 /h background) together with restlessness.
  * The fetal part becomes visible at the start of stage II: the tail is then *held*
    lifted (30-85 deg, or >=20 deg above the cow's own 24 h carriage) with rhythmic
    straining lifts, until the calf is expelled (median +12 min, 2-46 min).

Pipeline: per-second summaries -> causal + mirrored-future multi-scale features ->
gradient-boosted stage-II state probability (numeric JSON trees, no pickle) ->
episode decoding -> onset (rising edge) = approximate FPFV point, one per 12 h.
Outputs always require video confirmation.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .fpfv_features import FEATURE_VERSION, features_bidir

CODE = "FETAL_PART_FIRST_VISIBLE"
ALGORITHM_VERSION = "fpfv-stage2-onset-1"
MODEL_SCHEMA = "numeric-gbdt-1"
MIN_MS = 60_000.0
DROP_FEATURES = ("temp", "base_elev_24h")      # absolute, device-specific values
DEFAULTS = dict(threshold=0.5, onset_frac=0.7, smooth_s=120, min_episode_ms=120_000,
                merge_ms=20 * MIN_MS, separation_ms=12 * 3600_000.0, search_back_ms=30 * MIN_MS)


# ----------------------------------------------------------------------------- signals
def second_summary(times_ms, acc_g, gyro_dps, jerk_source=None):
    """Per-second mean/sd summaries of one record (times relative to record start)."""
    t = np.asarray(times_ms, float)
    acc, gyr = np.asarray(acc_g, float), np.asarray(gyro_dps, float)
    ok = (np.linalg.norm(acc, axis=1) > .01) & (np.max(np.abs(gyr), axis=1) < 1023.5) & np.isfinite(acc).all(1)
    sec = np.floor(t / 1000).astype(int)
    n = int(sec[-1]) + 1 if len(sec) else 0
    cnt = np.bincount(sec[ok], minlength=n).astype(float)

    def mean_sd(x):
        mu = np.stack([np.bincount(sec[ok], x[ok, j], minlength=n) for j in range(x.shape[1])], 1) / np.maximum(cnt, 1)[:, None]
        sq = np.stack([np.bincount(sec[ok], x[ok, j] ** 2, minlength=n) for j in range(x.shape[1])], 1) / np.maximum(cnt, 1)[:, None]
        return mu, np.sqrt(np.maximum(sq - mu * mu, 0))

    am, asd = mean_sd(acc)
    _, gsd = mean_sd(gyr)
    jerk = np.r_[0, np.linalg.norm(np.diff(acc, axis=0), axis=1)]
    jm, _ = mean_sd(jerk[:, None])
    return dict(coverage=cnt / 50., acc_mean=am, acc_sd=asd, gyr_sd=gsd, jerk=jm[:, 0])


def summarize_motion(motion):
    """Per-second summary + temperature from a parsed ``MotionRecord``."""
    from cowmata_tailring.annotation.data import GRAVITY_MS2
    acc = np.column_stack([motion.channels[k] for k in ("ax", "ay", "az")]) / GRAVITY_MS2
    gyr = np.column_stack([motion.channels[k] for k in ("gx", "gy", "gz")])
    s = second_summary(motion.times_ms, acc, gyr)
    s.update(epoch0_ms=float(motion.epoch_at(0)), temp_c=np.asarray(motion.temperature, float),
             temp_ms=np.asarray(motion.temperature_times_ms, float))
    return s


def continuous(parts):
    """Merge per-record summaries of ONE device onto a common epoch-second grid (NaN = not recorded)."""
    keys = ("coverage", "acc_mean", "acc_sd", "gyr_sd", "jerk")
    parts = [p for p in parts if len(p["coverage"])]
    if not parts:
        raise ValueError("No motion samples")
    start = int(math.floor(min(p["epoch0_ms"] for p in parts) / 1000))
    stop = int(math.ceil(max(p["epoch0_ms"] + len(p["coverage"]) * 1000 for p in parts) / 1000))
    if stop - start > 60 * 86400:
        raise ValueError("A device series longer than 60 days must be split")
    n = stop - start
    out = {k: np.full((n,) + np.shape(parts[0][k])[1:], np.nan, np.float32) for k in keys}
    tt, tv = [], []
    for p in parts:
        s = int(round(p["epoch0_ms"] / 1000)) - start
        m = len(p["coverage"])
        lo, hi = max(0, s), min(n, s + m)
        for k in keys:
            out[k][lo:hi] = np.asarray(p[k])[lo - s:hi - s]
        tt.append(np.asarray(p["temp_ms"]) + p["epoch0_ms"])
        tv.append(np.asarray(p["temp_c"]))
    valid = np.nan_to_num(out["coverage"]) >= .8
    for k in keys:
        out[k][~valid] = np.nan
    out["valid"], out["epoch_s"] = valid, np.arange(start, stop, dtype=np.float64)
    tt, tv = np.concatenate(tt), np.concatenate(tv)
    o = np.argsort(tt)
    out["temp_ms"], out["temp_c"] = tt[o], tv[o]
    return out


def feature_matrix(series):
    X, t_s, names = features_bidir(series)
    keep = [i for i, n in enumerate(names) if n not in DROP_FEATURES]
    return X[:, keep], t_s * 1000.0, [names[i] for i in keep]


# ----------------------------------------------------------------------------- numeric GBDT
def export_hgb(model, feature_names):
    """Export a fitted sklearn HistGradientBoostingClassifier as numeric JSON trees."""
    if list(model.classes_) != [0, 1]:
        raise ValueError("Binary model with both classes required")
    trees = []
    for it in model._predictors:
        nodes = it[0].nodes
        trees.append(dict(feature=nodes["feature_idx"].tolist(), threshold=nodes["num_threshold"].tolist(),
                          nan_left=nodes["missing_go_to_left"].astype(int).tolist(),
                          left=nodes["left"].tolist(), right=nodes["right"].tolist(),
                          leaf=nodes["is_leaf"].astype(int).tolist(), value=nodes["value"].tolist()))
    return dict(schema=MODEL_SCHEMA, features=list(feature_names),
                baseline=float(np.ravel(model._baseline_prediction)[0]), trees=trees)


def predict_gbdt(model, X):
    if model.get("schema") != MODEL_SCHEMA:
        raise ValueError("Incompatible FPFV model schema")
    X = np.asarray(X, np.float64)
    if X.ndim != 2 or X.shape[1] != len(model["features"]) or not 0 < len(model["trees"]) <= 2000:
        raise ValueError("Feature schema does not match the FPFV model")
    raw = np.full(len(X), model["baseline"])
    rows = np.arange(len(X))
    for tr in model["trees"]:
        f, th = np.asarray(tr["feature"]), np.asarray(tr["threshold"], float)
        nl, lf = np.asarray(tr["nan_left"], bool), np.asarray(tr["leaf"], bool)
        left, right, val = np.asarray(tr["left"]), np.asarray(tr["right"]), np.asarray(tr["value"], float)
        n = len(f)
        if n > 20000 or np.any(left[~lf] <= np.flatnonzero(~lf)) or np.any(right[~lf] <= np.flatnonzero(~lf)) \
                or np.any(left[~lf] >= n) or np.any(right[~lf] >= n) or np.any(f[~lf] >= X.shape[1]):
            raise ValueError("Invalid or cyclic tree")
        node = np.zeros(len(X), int)
        active = ~lf[node]
        while active.any():
            i = rows[active]
            c = node[i]
            x = X[i, f[c]]
            go_left = np.where(np.isnan(x), nl[c], x <= th[c])
            node[i] = np.where(go_left, left[c], right[c])
            active = ~lf[node]
        raw += val[node]
    return 1.0 / (1.0 + np.exp(-raw))


def model_file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_model(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("code") != CODE or doc.get("feature_version") != FEATURE_VERSION:
        raise ValueError("FPFV model does not match this feature version")
    return doc


# ----------------------------------------------------------------------------- decoding
def centered_smooth(score, t_ms, win_s=120):
    s = pd.Series(np.asarray(score, float), index=pd.to_datetime(np.asarray(t_ms), unit="ms"))
    fwd = s.rolling(f"{max(1, win_s // 2)}s", min_periods=1).mean()
    bwd = s[::-1].rolling(f"{max(1, win_s // 2)}s", min_periods=1).mean()[::-1]
    return ((fwd + bwd) / 2).to_numpy()


def stage2_episodes(t, p, threshold, min_len_ms, merge_ms):
    idx = np.flatnonzero(p >= threshold)
    if not len(idx):
        return []
    br = np.flatnonzero(np.diff(t[idx]) > merge_ms)
    starts, ends = np.r_[idx[0], idx[br + 1]], np.r_[idx[br], idx[-1]]
    return [(int(a), int(b)) for a, b in zip(starts, ends) if t[b] - t[a] >= min_len_ms]


def decode(t_ms, p, **params):
    """Stage-II probability (smoothed) -> FPFV candidates, strongest first, one per separation window."""
    c = {**DEFAULTS, **{k: v for k, v in params.items() if v is not None}}
    t = np.asarray(t_ms, float)
    out = []
    for a, b in stage2_episodes(t, p, c["threshold"], c["min_episode_ms"], c["merge_ms"]):
        seg = p[a:b + 1]
        peak = float(pd.Series(seg).rolling(30, min_periods=1).mean().max())
        lo = int(np.searchsorted(t, t[a] - c["search_back_ms"]))
        level = c["onset_frac"] * max(float(seg.max()), c["threshold"])
        rise = lo + int(np.argmax(p[lo:b + 1] >= level))
        out.append(dict(point_ms=float(t[rise]), episode_start_ms=float(t[a]), episode_end_ms=float(t[b]),
                        score=peak))
    out.sort(key=lambda r: -r["score"])
    kept = []
    for r in out:
        if all(abs(r["point_ms"] - k["point_ms"]) >= c["separation_ms"] for k in kept):
            kept.append(r)
    return sorted(kept, key=lambda r: r["point_ms"])


def detect_series(series, model, **params):
    """Run the standard algorithm on one device's continuous per-second series (epoch based)."""
    X, t_ms, names = feature_matrix(series)
    if names != model["features"]:
        raise ValueError("Feature order does not match the FPFV model")
    if not len(X):
        return [], dict(t_ms=t_ms, p=np.zeros(0))
    raw = predict_gbdt(model, X)
    c = {**DEFAULTS, **model.get("decoder", {}), **{k: v for k, v in params.items() if v is not None}}
    p = centered_smooth(raw, t_ms, c["smooth_s"])
    events = []
    for r in decode(t_ms, p, **c):
        events.append(dict(code=CODE, point_epoch_ms=r["point_ms"], start_epoch_ms=r["episode_start_ms"],
                           end_epoch_ms=r["episode_end_ms"], score=r["score"],
                           time_semantics="approximate_point", requires_video_confirmation=True,
                           review_status="pending", algorithm=ALGORITHM_VERSION,
                           model_version=model.get("version"), score_is_probability=False))
    return events, dict(t_ms=t_ms, p=p)


def detect_records(motions, model, **params):
    """``motions``: parsed MotionRecord objects of ONE device (any order, may have gaps)."""
    return detect_series(continuous([summarize_motion(m) for m in motions]), model, **params)
