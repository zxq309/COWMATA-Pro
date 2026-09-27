"""Tail-ring lying-down (卧倒过程, LYING_DOWN) detector.

Physical regularity found in COWMATA_Behavior_Dataset (tail-mounted 9-axis ring, 50 Hz):

* The ring's Y axis runs along the tail and keeps carrying most of gravity (y ~ -0.9 g).
* Standing: the tail hangs behind the pin bones, the dorsal Z axis tilts up (g_z ~ +0.37,
  |roll| ~ 17 deg).  Lying: the hindquarters drop and the tail rolls onto either side
  (g_z ~ 0, |roll| ~ 95 deg, sign of X = left / right recumbency).
* A lying-down therefore shows, in order: a short gyroscope burst (median 28 dps, 2-35 s),
  a lasting gravity-direction change (median 39 deg) with a Z drop (97% of events) and a
  new orientation that is retained for minutes, while a standing-up is the mirror image.

The detector follows the published two-layer pattern (posture state + transition event,
Achour et al. 2019; minimum bout rules as in Ledgerwood 2010 / Kok 2015):

1. accelerometer offset self-calibration (a faulty unit had a +1.95 g Z bias);
2. 10 Hz gravity / gyro-energy series, orientation-step candidates (>= 8 deg);
3. a per-second posture model (standing vs lying) gives context before and after;
4. an event forest scores each candidate from physics features + posture context;
5. non-maximum suppression and learned boundary corrections produce proposed intervals.

Models are numeric forests (no pickle), evaluated with :func:`models.predict_forest`.
"""
from __future__ import annotations

import numpy as np

ALGORITHM = "liedown-tailring-1"
CODE = "LYING_DOWN"
HZ = 10
STILL_DPS = 4.0

EVENT_FEATURES = [
    "ang_s", "ang_l", "dz_s", "dz_l", "dy_l", "droll_abs", "roll_pre_abs", "roll_post_abs",
    "xz_pre", "xz_post", "z_pre", "z_post", "y_pre", "y_post",
    "e_pre", "e_core", "e_post", "e_post_long", "e_peak", "burst_ratio", "settle_ratio",
    "med_pre", "med_post", "act_pre", "act_post", "noise_pre", "noise_post",
    "retain_post", "retain_pre", "ang_ref_pre", "ang_ref_post", "ref_gap", "trans_s",
    "still_pre60", "still_post60", "still_pre180", "still_post180", "p25_pre60", "p25_post60",
    "p50_pre60", "p50_post60", "jitter_ratio", "still_gain", "ang_60", "e_pre60", "e_post60",
    "tex_pre", "tex_post", "tex_gain",
]
CONTEXT_FEATURES = ["pl_pre", "pl_post", "pl_pre_long", "pl_post_long", "pl_delta", "pl_delta_long"]
MODEL_FEATURES = EVENT_FEATURES + CONTEXT_FEATURES
POSTURE_FEATURES = [f"{k}_{w}" for w in (10, 60) for k in ("x", "y", "z", "absx", "absroll", "xz")] + [
    "still60", "e60", "tex60", "ostd60"]
CONTEXT_WINDOWS = {"pl_pre": (-60, -5), "pl_post": (5, 60), "pl_pre_long": (-300, -5),
                   "pl_post_long": (5, 300)}
DEFAULT_PARAMS = dict(min_angle_deg=8.0, min_sep_s=8.0, nms_s=20.0, onset_bias_s=0.0, offset_bias_s=0.0)


# ----------------------------------------------------------------------------- signal layer
def calibrate_accel(acc_g, gyro_dps, tol=0.07):
    """Remove a constant accelerometer offset when quiet samples are far from 1 g."""
    acc = np.asarray(acc_g, dtype=np.float64)
    gn = np.linalg.norm(np.asarray(gyro_dps, dtype=np.float64), axis=1)
    quiet = np.flatnonzero((gn < 2) & np.isfinite(acc).all(1))
    info = dict(quiet_norm=None, offset=[0.0, 0.0, 0.0], corrected=False)
    if quiet.size < 500:
        return acc, info
    a = acc[quiet[:: max(1, quiet.size // 20000)]]
    norm = float(np.median(np.linalg.norm(a, axis=1)))
    info["quiet_norm"] = norm
    if abs(norm - 1) <= tol:
        return acc, info
    from scipy.optimize import least_squares
    x0 = np.median(a, 0) * (1 - 1 / max(norm, 1e-6))
    fit = least_squares(lambda p: np.r_[np.linalg.norm(a - p, axis=1) - 1.0, 0.01 * p], x0,
                        loss="soft_l1", f_scale=0.02)
    residual = float(np.median(np.abs(np.linalg.norm(a - fit.x, axis=1) - 1)))
    if residual < 0.03:
        info.update(offset=[float(v) for v in fit.x], corrected=True, residual=residual)
        return acc - fit.x, info
    return acc, info


def _mov(x, lo, hi, min_frac=0.5):
    """nan-aware mean of x[i+lo:i+hi] for each i (window in samples)."""
    x = np.asarray(x, dtype=np.float64)
    one = x.ndim == 1
    if one:
        x = x[:, None]
    n = len(x)
    finite = np.isfinite(x)
    c = np.vstack([np.zeros((1, x.shape[1])), np.cumsum(np.where(finite, x, 0), 0)])
    k = np.vstack([np.zeros((1, x.shape[1])), np.cumsum(finite, 0)])
    a = np.clip(np.arange(n) + lo, 0, n)
    b = np.clip(np.arange(n) + hi, 0, n)
    count = k[b] - k[a]
    out = np.where(count > min_frac * (hi - lo), (c[b] - c[a]) / np.maximum(count, 1), np.nan)
    return out[:, 0] if one else out


def _unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-9)


def _angle(a, b):
    return np.degrees(np.arccos(np.clip(np.sum(_unit(a) * _unit(b), -1), -1, 1)))


def _roll(v):
    return np.degrees(np.arctan2(v[..., 0], v[..., 2]))


def prepare(times_ms, acc_g, gyro_dps):
    """Resample one record to the 10 Hz analysis grid; sensor gaps stay NaN."""
    t = np.asarray(times_ms, dtype=np.float64)
    acc = np.asarray(acc_g, dtype=np.float64)
    gyro = np.asarray(gyro_dps, dtype=np.float64)
    if t.ndim != 1 or len(t) < 2 or acc.shape != (len(t), 3) or gyro.shape != acc.shape:
        raise ValueError("Expected timestamped three-axis acceleration (g) and angular velocity (dps)")
    if not np.isfinite(t).all() or np.any(np.diff(t) < 0) or t[0] < 0:
        raise ValueError("Nonmonotonic sensor time axis")
    acc, calib = calibrate_accel(acc, gyro)
    ok = np.isfinite(acc).all(1) & np.isfinite(gyro).all(1)
    b = np.floor(t[ok] / (1000 / HZ)).astype(np.int64)
    n = int(np.floor(t[-1] / (1000 / HZ))) + 1
    count = np.bincount(b, minlength=n).astype(np.float64)
    A = np.stack([np.bincount(b, acc[ok, i], n) for i in range(3)], 1) / np.maximum(count, 1)[:, None]
    G = np.bincount(b, np.linalg.norm(gyro[ok], axis=1), n) / np.maximum(count, 1)
    valid = count >= 2
    A[~valid] = np.nan
    G[~valid] = np.nan
    g1 = _unit(_mov(A, -5, 5))
    dev = np.linalg.norm(A - _mov(A, -5, 5), axis=1)
    signals = dict(A=A, G=G, valid=valid, g1=g1, E=_mov(G, -10, 10), n=n,
                   tex=np.sqrt(_mov(dev ** 2, -10, 10)), calib=calib)
    act = _mov(G, -300, 300)
    usable = np.isfinite(act) & np.isfinite(g1).all(1)
    if usable.sum() > 600:
        signals["ref"] = _unit(np.nanmedian(g1[usable & (act >= np.nanpercentile(act[usable], 80))], 0))
    else:
        signals["ref"] = np.array([0.0, -0.85, 0.5])
    return signals


def candidates(s, min_angle_deg=8.0, min_sep_s=8.0):
    from scipy.signal import find_peaks
    pre = _mov(s["g1"], -12 * HZ, -2 * HZ)
    post = _mov(s["g1"], 2 * HZ, 12 * HZ)
    ang = _angle(pre, post)
    ang[~np.isfinite(ang)] = 0
    peaks, _ = find_peaks(ang, height=min_angle_deg, distance=max(1, int(min_sep_s * HZ)))
    return peaks


def _stat(x, i, lo, hi, fn=np.nanmean):
    a, b = max(0, i + lo), min(len(x), i + hi)
    seg = x[a:b]
    if seg.size == 0 or not np.isfinite(seg).any():
        return np.full(x.shape[1], np.nan) if x.ndim > 1 else np.nan
    return fn(seg, axis=0) if seg.ndim > 1 else fn(seg)


def event_features(s, peaks):
    g1, G, E, ref, tex = s["g1"], s["G"], s["E"], s["ref"], s["tex"]
    rows = []
    for i in np.asarray(peaks, dtype=int):
        med = np.nanmedian
        pre_s, post_s = _unit(_stat(g1, i, -12 * HZ, -2 * HZ, med)), _unit(_stat(g1, i, 2 * HZ, 12 * HZ, med))
        pre_l, post_l = _unit(_stat(g1, i, -30 * HZ, -3 * HZ, med)), _unit(_stat(g1, i, 3 * HZ, 30 * HZ, med))
        post_ll, pre_ll = _unit(_stat(g1, i, 30 * HZ, 90 * HZ, med)), _unit(_stat(g1, i, -90 * HZ, -30 * HZ, med))
        e_pre, e_core = _stat(G, i, -30 * HZ, -3 * HZ), _stat(G, i, -3 * HZ, 3 * HZ)
        e_post, e_post_long = _stat(G, i, 3 * HZ, 30 * HZ), _stat(G, i, 3 * HZ, 120 * HZ)
        gpre, gpost = G[max(0, i - 30 * HZ): max(0, i - 3 * HZ)], G[i + 3 * HZ: i + 30 * HZ]
        npre, npost = _stat(g1, i, -30 * HZ, -3 * HZ, np.nanstd), _stat(g1, i, 3 * HZ, 30 * HZ, np.nanstd)
        d = post_s - pre_s
        seg = g1[max(0, i - 20 * HZ): i + 20 * HZ]
        trans = np.nan
        if np.isfinite(d).all() and np.linalg.norm(d) > 1e-6 and len(seg):
            p = (seg - pre_s) @ d / (d @ d)
            c = min(20 * HZ, i)
            left, right = np.flatnonzero(p[:c] <= 0.1), np.flatnonzero(p[c:] >= 0.9)
            if left.size and right.size:
                trans = (c + right[0] - left[-1]) / HZ
        with np.errstate(all="ignore"):
            f = dict(
                ang_s=_angle(pre_s, post_s), ang_l=_angle(pre_l, post_l),
                dz_s=post_s[2] - pre_s[2], dz_l=post_l[2] - pre_l[2], dy_l=post_l[1] - pre_l[1],
                droll_abs=abs(((_roll(post_l) - _roll(pre_l)) + 180) % 360 - 180),
                roll_pre_abs=abs(_roll(pre_l)), roll_post_abs=abs(_roll(post_l)),
                xz_pre=np.hypot(pre_l[0], pre_l[2]), xz_post=np.hypot(post_l[0], post_l[2]),
                z_pre=pre_l[2], z_post=post_l[2], y_pre=pre_l[1], y_post=post_l[1],
                e_pre=e_pre, e_core=e_core, e_post=e_post, e_post_long=e_post_long,
                e_peak=_stat(E, i, -8 * HZ, 8 * HZ, np.nanmax),
                burst_ratio=np.log((e_core + 1) / (np.fmax(e_pre, e_post) + 1)),
                settle_ratio=np.log((e_post + 1) / (e_pre + 1)),
                med_pre=np.nanmedian(gpre) if np.isfinite(gpre).any() else np.nan,
                med_post=np.nanmedian(gpost) if np.isfinite(gpost).any() else np.nan,
                act_pre=np.nanmean(gpre > 15) if gpre.size else np.nan,
                act_post=np.nanmean(gpost > 15) if gpost.size else np.nan,
                noise_pre=np.nansum(npre), noise_post=np.nansum(npost),
                retain_post=_angle(post_l, post_ll), retain_pre=_angle(pre_l, pre_ll),
                ang_ref_pre=_angle(pre_l, ref), ang_ref_post=_angle(post_l, ref), trans_s=trans,
            )
            f["ref_gap"] = f["ang_ref_post"] - f["ang_ref_pre"]
            for w in (60, 180):
                a = G[max(0, i - w * HZ): max(0, i - 3 * HZ)]
                b = G[i + 3 * HZ: i + w * HZ]
                a, b = a[np.isfinite(a)], b[np.isfinite(b)]
                f[f"still_pre{w}"] = float(np.mean(a < STILL_DPS)) if a.size > 50 else np.nan
                f[f"still_post{w}"] = float(np.mean(b < STILL_DPS)) if b.size > 50 else np.nan
                if w == 60:
                    for q, name in ((25, "p25"), (50, "p50")):
                        f[f"{name}_pre60"] = np.percentile(a, q) if a.size > 50 else np.nan
                        f[f"{name}_post60"] = np.percentile(b, q) if b.size > 50 else np.nan
                    f["e_pre60"] = float(np.mean(a)) if a.size > 50 else np.nan
                    f["e_post60"] = float(np.mean(b)) if b.size > 50 else np.nan
                    ta, tb = tex[max(0, i - w * HZ): max(0, i - 3 * HZ)], tex[i + 3 * HZ: i + w * HZ]
                    f["tex_pre"] = np.nanmedian(ta) if np.isfinite(ta).sum() > 50 else np.nan
                    f["tex_post"] = np.nanmedian(tb) if np.isfinite(tb).sum() > 50 else np.nan
            f["jitter_ratio"] = np.log((f["p50_post60"] + 1) / (f["p50_pre60"] + 1))
            f["still_gain"] = f["still_post60"] - f["still_pre60"]
            f["tex_gain"] = np.log((f["tex_post"] + 1e-3) / (f["tex_pre"] + 1e-3))
            f["ang_60"] = _angle(_unit(_stat(g1, i, -60 * HZ, -3 * HZ, med)), _unit(_stat(g1, i, 3 * HZ, 60 * HZ, med)))
        rows.append([float(f[k]) for k in EVENT_FEATURES])
    return np.asarray(rows, dtype=np.float64).reshape(len(rows), len(EVENT_FEATURES))


def boundaries(s, i, max_s=25):
    """Transition start/end (grid indices) from the orientation-progress curve."""
    g1 = s["g1"]
    pre_s = _unit(_stat(g1, i, -12 * HZ, -2 * HZ, np.nanmedian))
    post_s = _unit(_stat(g1, i, 2 * HZ, 12 * HZ, np.nanmedian))
    d = post_s - pre_s
    lo, hi = max(0, i - max_s * HZ), min(s["n"], i + max_s * HZ)
    if not np.isfinite(d).all():
        return max(lo, i - 2 * HZ), min(hi - 1, i + 2 * HZ)
    with np.errstate(invalid="ignore"):
        p = (g1[lo:hi] - pre_s) @ d / max(float(d @ d), 1e-9)
    c = i - lo
    left = np.flatnonzero(~(p[:c] > 0.05))
    right = np.flatnonzero(~(p[c:] < 0.95))
    return lo + (int(left[-1]) if left.size else 0), i + (int(right[0]) if right.size else hi - 1 - i)


# ----------------------------------------------------------------------------- posture layer
def posture_features(s, grid_index):
    g1, G, tex = s["g1"], s["G"], s["tex"]
    idx = np.asarray(grid_index, dtype=int)
    cols = []
    for w in (10, 60):
        m = _unit(_mov(g1, -w * HZ, w * HZ))[idx]
        cols += [m[:, 0], m[:, 1], m[:, 2], np.abs(m[:, 0]), np.abs(_roll(m)), np.hypot(m[:, 0], m[:, 2])]
    still = _mov(np.where(np.isfinite(G), (G < STILL_DPS).astype(float), np.nan), -300, 300)[idx]
    mean_g = _mov(g1, -300, 300)
    spread = _mov(np.sum((g1 - mean_g) ** 2, 1), -300, 300)[idx]
    cols += [still, _mov(G, -300, 300)[idx], _mov(tex, -300, 300)[idx], np.sqrt(np.maximum(spread, 0))]
    return np.column_stack(cols)


def posture_seconds(s):
    n_s = s["n"] // HZ
    return posture_features(s, np.arange(n_s) * HZ + HZ // 2)


def posture_labels(events, n_seconds, guard_s=5):
    """Per-second standing(0)/lying(1)/unknown(-1) implied by alternating LD/SU intervals (ms)."""
    ev = sorted([e for e in events if e["code"] in ("LYING_DOWN", "STANDING_UP")], key=lambda e: e["start_ms"])
    merged = []
    for e in ev:
        if merged and merged[-1]["code"] == e["code"] and e["start_ms"] <= merged[-1]["end_ms"] + 1000:
            merged[-1]["end_ms"] = max(merged[-1]["end_ms"], e["end_ms"])
            continue
        merged.append(dict(e))
    labels = np.full(n_seconds, -1, dtype=np.int8)
    if not merged:
        return labels
    bounds = [0.0] + [x for e in merged for x in (e["start_ms"] / 1000, e["end_ms"] / 1000)] + [float(n_seconds)]
    states = [0 if merged[0]["code"] == "LYING_DOWN" else 1]
    for k, e in enumerate(merged):
        post = 1 if e["code"] == "LYING_DOWN" else 0
        nxt = merged[k + 1]["code"] if k + 1 < len(merged) else None
        states.append(post if nxt is None or (nxt == "LYING_DOWN") == (post == 0) else -1)
    for k, state in enumerate(states):
        a = bounds[2 * k] + (guard_s if k > 0 else 0)
        b = bounds[2 * k + 1] - (guard_s if k < len(merged) else 0)
        if b > a:
            labels[int(np.ceil(a)):int(np.floor(b))] = state
    return labels


def posture_context(p_lying, times_s):
    p = np.asarray(p_lying, dtype=np.float64)
    out = np.full((len(times_s), len(CONTEXT_FEATURES)), np.nan)
    for j, t in enumerate(times_s):
        for k, (name, (lo, hi)) in enumerate(CONTEXT_WINDOWS.items()):
            a, b = int(max(0, t + lo)), int(min(len(p), t + hi))
            v = p[a:b]
            v = v[np.isfinite(v)]
            out[j, k] = v.mean() if v.size > 3 else np.nan
    out[:, 4] = out[:, 1] - out[:, 0]
    out[:, 5] = out[:, 3] - out[:, 2]
    return out


# ----------------------------------------------------------------------------- inference
def _forest(model, x):
    from .models import predict_forest
    return predict_forest(model, x)


def law_score(x):
    """Model-free fallback: the four universal conditions, as a 0..1 score."""
    f = dict(zip(MODEL_FEATURES, np.asarray(x, dtype=np.float64).T))
    conditions = [f["ang_l"] >= 10, f["dz_l"] <= -0.03, f["e_core"] >= 18, f["burst_ratio"] >= 0]
    return np.mean(np.vstack([np.nan_to_num(c, nan=0).astype(float) for c in conditions]), 0)


def analyse(times_ms, acc_g, gyro_dps, model=None):
    """Return signals, candidate grid indices, feature matrix and posture probabilities."""
    s = prepare(times_ms, acc_g, gyro_dps)
    params = {**DEFAULT_PARAMS, **((model or {}).get("params") or {})}
    peaks = candidates(s, params["min_angle_deg"], params["min_sep_s"])
    x = event_features(s, peaks)
    p_lying = None
    if model is not None and model.get("posture"):
        px = posture_seconds(s)
        p_lying = _forest(model["posture"], px)
        p_lying[~np.isfinite(px).all(1)] = np.nan
        ctx = posture_context(p_lying, peaks / HZ)
    else:
        ctx = np.full((len(peaks), len(CONTEXT_FEATURES)), np.nan)
    return s, peaks, np.hstack([x, ctx]), p_lying, params


def detect(times_ms, acc_g, gyro_dps, model=None, *, threshold=None, duration_ms=None):
    """Detect lying-down transitions in one record; returns candidate dicts (ms, parent IMU clock)."""
    s, peaks, x, p_lying, params = analyse(times_ms, acc_g, gyro_dps, model)
    if len(peaks) == 0:
        return []
    if model is not None:
        score = _forest(model, x)
        thr = float(model.get("threshold", 0.3) if threshold is None else threshold)
        method = model.get("algorithm", ALGORITHM)
    else:
        score = law_score(x)
        thr = 1.0 if threshold is None else float(threshold)
        method = ALGORITHM + ":law-only"
    duration_ms = float(times_ms[-1]) if duration_ms is None else float(duration_ms)
    order = np.argsort(-score)
    taken = []
    for j in order:
        if score[j] < thr:
            break
        if all(abs(peaks[j] - peaks[k]) > params["nms_s"] * HZ for k in taken):
            taken.append(j)
    events = []
    name = {k: i for i, k in enumerate(MODEL_FEATURES)}
    for j in sorted(taken, key=lambda k: peaks[k]):
        i = int(peaks[j])
        a, b = boundaries(s, i)
        start = max(0.0, a / HZ * 1000 - params["onset_bias_s"] * 1000)
        end = min(duration_ms, b / HZ * 1000 - params["offset_bias_s"] * 1000)
        point = min(max(i / HZ * 1000, start), end) if end >= start else start
        end = max(end, point)
        row = x[j]
        events.append(dict(
            code=CODE, start_ms=float(start), end_ms=float(end), point_ms=float(point),
            score=float(score[j]), time_semantics="proposed_interval", requires_video_confirmation=False,
            available_ms=float(min(end + 300000, duration_ms)), review_status="pending", method=method,
            evidence=dict(
                orientation_change_deg=_r(row[name["ang_l"]]), z_change_g=_r(row[name["dz_l"]]),
                roll_change_deg=_r(row[name["droll_abs"]]), burst_gyro_dps=_r(row[name["e_core"]]),
                lying_prob_before=_r(row[name["pl_pre"]]), lying_prob_after=_r(row[name["pl_post"]]),
                accel_offset_corrected=bool(s["calib"]["corrected"]))))
    return events


def _r(v, nd=3):
    v = float(v)
    return round(v, nd) if np.isfinite(v) else None


def detect_motion_file(path, model=None, **kw):
    """Decode a COWMATA motion JSON with the application parser and detect lying-down events."""
    from cowmata_tailring.annotation.data import GRAVITY_MS2, load_motion_json
    motion = load_motion_json(path)
    acc = np.column_stack([motion.channels[k] for k in ("ax", "ay", "az")]) / GRAVITY_MS2
    gyro = np.column_stack([motion.channels[k] for k in ("gx", "gy", "gz")])
    return detect(motion.times_ms, acc, gyro, model, duration_ms=motion.duration_ms, **kw)