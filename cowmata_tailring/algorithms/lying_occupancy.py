"""Tail-ring lying occupancy (躺卧占比) — posture state synchronised with stand-up / lie-down.

Algorithm ``lying-occupancy-2`` replaces the transition-only reconstruction of
:mod:`cowmata_tailring.algorithms.posture` (4.3.2), which left every period without an
observed transition "unknown" and therefore produced lying fractions on only part of the day.

Layers (all numpy; models are plain JSON numbers, never pickles):

1. Per-second summary of the 50 Hz ring (the same summary as the shared behaviour cache):
   mean acceleration per axis, angular-rate RMS, dynamic acceleration, coverage.
2. Device offset: a constant accelerometer bias is removed by a sphere fit on quiet seconds
   (faulty rings carried up to +1.95 g on Z).
3. Standing reference: median gravity direction during sustained activity of the same cow
   (walking and feeding only happen standing), re-estimated every hour from the preceding 24 h.
4. Posture layer: per-second P(lying) from rotation-invariant physics — angle to the standing
   reference, roll onto the X axis (lying) versus rotation towards Z (tail raising), stillness,
   and the orientation of tail-down seconds only (robust to tail raising during labour).
5. Transition layer (起立过程 / 卧倒过程 regularities): orientation-step candidates with a gyro
   burst; P(STANDING_UP) and P(LYING_DOWN) from a 3-class model.  Z drops at 97 % of lie-downs
   and rises at 98-99 % of stand-ups, so direction errors are rare.
6. State layer: two-state hidden Markov model.  The state may change essentially only where a
   transition candidate says so; emissions are tempered posture log-odds.  A causal forward
   filter runs over the whole series; every 10-min window is smoothed with a fixed lag, so a
   window only uses samples up to ``end + LOOKAHEAD_MS`` (lag 150 s + candidate context 12 s +
   posture context 121 s + posture features 600 s < 900 s).
7. Wear check: seconds where the ring is off the tail (sustained elevation > 75 deg or a
   motionless ring) are excluded instead of being read as lying.
"""
from __future__ import annotations

import numpy as np

ALGORITHM = "lying-occupancy-2"
LOOKAHEAD_MS = 900_000          # posture features <= +600 s; smoothing lag + transition context <= +900 s
SMOOTH_LAG_S = 150
REF_HISTORY_S = 86_400
BLOCK_S = 3_600
STILL_DPS = 4.0

DEFAULT_HMM = dict(kappa=0.35, leak=1e-6, prior_lying=0.5, p_floor=0.02, transition_gain=1.0,
                   transition_window_s=12, reset_gap_s=900, min_bout_s=60)
FALLBACK_REFERENCE = (0.0, -0.9, 0.37)


# ----------------------------------------------------------------------------- per-second summary

def summarize_motion(times_ms, acc_g, gyro_dps, *, sample_rate_hz=50.0):
    """Per-second means/SDs from one decoded motion record (record-relative seconds)."""
    t = np.asarray(times_ms, dtype=np.float64)
    acc = np.asarray(acc_g, dtype=np.float64)
    gyr = np.asarray(gyro_dps, dtype=np.float64)
    if t.ndim != 1 or acc.shape != (len(t), 3) or gyr.shape != acc.shape:
        raise ValueError("Expected timestamped three-axis acceleration (g) and angular rate (dps)")
    ok = (np.linalg.norm(acc, axis=1) > .01) & (np.max(np.abs(gyr), axis=1) < 1023.5)
    ok &= np.isfinite(acc).all(1) & np.isfinite(gyr).all(1) & np.isfinite(t) & (t >= 0)
    sec = np.floor(np.where(ok, t, 0) / 1000).astype(np.int64)
    n = int(sec[ok].max()) + 1 if ok.any() else 0
    cnt = np.bincount(sec[ok], minlength=n).astype(np.float64)

    def mean_sd(x):
        mu = np.stack([np.bincount(sec[ok], x[ok, j], minlength=n) for j in range(3)], 1) / np.maximum(cnt, 1)[:, None]
        sq = np.stack([np.bincount(sec[ok], x[ok, j] ** 2, minlength=n) for j in range(3)], 1) / np.maximum(cnt, 1)[:, None]
        return mu, np.sqrt(np.maximum(sq - mu * mu, 0))

    am, asd = mean_sd(acc)
    gm, gsd = mean_sd(gyr)
    return dict(coverage=cnt / float(sample_rate_hz), acc_mean=am, acc_sd=asd, gyr_mean=gm, gyr_sd=gsd)


def summarize_motion_file(path):
    """Decode one COWMATA motion JSON and return (epoch0_ms, summary, meta)."""
    import json
    from pathlib import Path

    from cowmata_tailring.annotation.data import GRAVITY_MS2, parse_motion_object

    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    m = parse_motion_object(doc, source_path=path)
    acc = np.column_stack([m.channels[k] for k in ("ax", "ay", "az")]) / GRAVITY_MS2
    gyr = np.column_stack([m.channels[k] for k in ("gx", "gy", "gz")])
    sec = summarize_motion(m.times_ms, acc, gyr, sample_rate_hz=m.sample_rate_hz or 50.0)
    meta = dict(source=str(path), update_time_ms=m.update_time_ms, duration_ms=m.duration_ms)
    return float(m.epoch_at(0)), sec, meta


# ----------------------------------------------------------------------------- helpers

def _unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-9)


def _angle(a, b):
    return np.degrees(np.arccos(np.clip(np.sum(_unit(a) * _unit(b), -1), -1, 1)))


def _roll(v):
    return np.degrees(np.arctan2(v[..., 0], v[..., 2]))


def _elev(v):
    return np.degrees(np.arccos(np.clip(-np.asarray(v)[..., 1], -1, 1)))


def _mov(x, lo, hi, min_frac=0.3):
    """nan-aware mean of x[i+lo:i+hi] for every i (window in samples)."""
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


# ----------------------------------------------------------------------------- continuous grid

def build_grid(records):
    """Place per-second summaries of one cow on a continuous 1 Hz grid.

    ``records``: iterable of (epoch0_ms, summary) sorted or not; overlapping seconds keep the
    first record.  Returns dict with t0_ms, n and stacked arrays (gaps NaN / coverage 0).
    """
    items = sorted(((float(e), s) for e, s in records), key=lambda x: x[0])
    if not items:
        raise ValueError("No motion records")
    t0 = np.floor(items[0][0] / 1000) * 1000
    end = max(e + 1000 * len(s["coverage"]) for e, s in items)
    n = int(np.ceil((end - t0) / 1000))
    grid = dict(t0_ms=float(t0), n=n, coverage=np.zeros(n), acc_mean=np.full((n, 3), np.nan),
                acc_sd=np.full((n, 3), np.nan), gyr_mean=np.full((n, 3), np.nan), gyr_sd=np.full((n, 3), np.nan),
                record=np.full(n, -1, dtype=np.int32), offsets_s=[])
    for k, (e, s) in enumerate(items):
        off = int(round((e - t0) / 1000))
        m = len(s["coverage"])
        lo, hi = max(0, off), min(n, off + m)
        if hi <= lo:
            grid["offsets_s"].append(off)
            continue
        free = grid["record"][lo:hi] < 0
        idx = np.arange(lo, hi)[free]
        src = idx - off
        grid["coverage"][idx] = np.asarray(s["coverage"], float)[src]
        for key in ("acc_mean", "acc_sd", "gyr_mean", "gyr_sd"):
            grid[key][idx] = np.asarray(s[key], float)[src]
        grid["record"][idx] = k
        grid["offsets_s"].append(off)
    return grid


# ----------------------------------------------------------------------------- signal layer

def _fit_offset(q, tol=0.07):
    norm = float(np.median(np.linalg.norm(q, axis=1)))
    info = dict(quiet_norm=norm, offset=[0.0, 0.0, 0.0], scale=1.0, corrected=False)
    if abs(norm - 1) <= tol:
        return info
    from scipy.optimize import least_squares
    x0 = np.median(q, 0) * (1 - 1 / max(norm, 1e-6))
    fit = least_squares(lambda p: np.r_[np.linalg.norm(q - p, axis=1) - 1.0, 0.01 * p], x0,
                        loss="soft_l1", f_scale=0.02)
    residual = float(np.median(np.abs(np.linalg.norm(q - fit.x, axis=1) - 1)))
    centred = _unit(q - fit.x)
    spread = float(_angle(centred, _unit(np.median(centred, 0))[None, :]).max())
    # A sphere fit needs orientation diversity; otherwise only rescale the magnitude.
    if residual < 0.04 and spread > 25:
        info.update(offset=[float(v) for v in fit.x], corrected=True, residual=residual)
    else:
        info.update(scale=norm, rescaled=True)
    return info


def signals(grid):
    """Orientation, angular-rate RMS and dynamic acceleration on the grid, offset corrected.

    The offset is a device constant; it is re-estimated hourly from quiet seconds up to the
    start of that hour + lookahead, so no window uses later data.  While a large bias cannot be
    fitted yet the seconds are reported as unknown.
    """
    am, gm, gsd, asd = grid["acc_mean"], grid["gyr_mean"], grid["gyr_sd"], grid["acc_sd"]
    n = grid["n"]
    valid = (grid["coverage"] >= 0.5) & np.isfinite(am).all(1) & (np.linalg.norm(np.nan_to_num(am), axis=1) > 0.2)
    gyro = np.sqrt(np.sum(np.nan_to_num(gm) ** 2 + np.nan_to_num(gsd) ** 2, 1))
    quiet = valid & (gyro < 3.0)
    valid0 = valid.copy()
    acc = np.array(am, dtype=np.float64)
    look = LOOKAHEAD_MS // 1000
    info_all = []
    info = dict(offset=[0.0, 0.0, 0.0], scale=1.0, corrected=False)
    fitted_on = -1
    for start in range(0, n, BLOCK_S):
        stop = min(n, start + BLOCK_S)
        # Causal: the offset for this hour only uses quiet seconds up to start + lookahead.
        idx = np.flatnonzero(quiet[:min(n, start + look)])
        if idx.size >= 120 and idx.size >= 1.5 * max(fitted_on, 0):
            q = am[idx[:: max(1, idx.size // 6000)]]
            info = _fit_offset(q)
            fitted_on = idx.size
        acc[start:stop] = (am[start:stop] - np.asarray(info["offset"])) / float(info.get("scale", 1.0))
        # A large bias that cannot be fitted yet (too little orientation diversity so far) makes
        # the gravity direction meaningless: report these seconds as unknown, not as a posture.
        seen = np.flatnonzero(valid0[:min(n, start + look)])
        norm_now = float(np.median(np.linalg.norm(am[seen[:: max(1, seen.size // 6000)]], axis=1))) if seen.size else 1.0
        if not info.get("corrected") and abs(norm_now - 1.0) > 0.15:
            valid[start:stop] = False
        info_all.append(dict(start_s=start, **info))
    u = _unit(acc)
    u[~valid] = np.nan
    worn = wear_mask(u, np.where(valid, gyro, np.nan))
    valid = valid & worn
    u[~valid] = np.nan
    return dict(u=u, gyro=np.where(valid, gyro, np.nan), dyn=np.where(valid, np.linalg.norm(np.nan_to_num(asd), axis=1), np.nan),
                valid=valid, worn=worn, n=n, calibration=info_all)


def wear_mask(u, gyro):
    """False where the ring is not on a living tail.

    On the tail the ring's Y axis carries gravity (tail elevation < 60 deg for > 95 % of worn
    time; labour tail-raising lasts minutes), and a living tail always moves.  A ring that fell
    off or hangs loose shows a sustained elevation > 75 deg or near-zero angular rate.  The
    window [t-1200 s, t+300 s] keeps the decision within the feature lookahead.
    """
    elev = _elev(u)
    high = np.where(np.isfinite(elev), (elev > 75).astype(float), np.nan)
    dead = np.where(np.isfinite(gyro), (gyro < 1.0).astype(float), np.nan)
    off = (_mov(high, -1200, 301, min_frac=0.1) > 0.8) | (_mov(dead, -1200, 301, min_frac=0.1) > 0.95)
    return ~off


def references(s):
    """Standing reference per second: hourly blocks from the preceding 24 h (+ lookahead)."""
    n = s["n"]
    act = _mov(s["dyn"], -30, 31)
    u = s["u"]
    ok = np.isfinite(act) & np.isfinite(u).all(1)
    ref = np.tile(np.asarray(FALLBACK_REFERENCE, float), (n, 1))
    status = np.zeros(n, dtype=np.int8)  # 0 fallback, 1 estimated
    look = LOOKAHEAD_MS // 1000
    last = None
    for start in range(0, n, BLOCK_S):
        stop = min(n, start + BLOCK_S)
        lo, hi = max(0, start - REF_HISTORY_S), min(n, start + look)
        sel = np.flatnonzero(ok[lo:hi]) + lo
        if sel.size >= 300:
            a = act[sel]
            top = sel[a >= np.percentile(a, 85)]
            r = _unit(np.median(u[top], 0))
            near = top[_angle(u[top], r[None, :]) < 35]
            if near.size > 30:
                r = _unit(np.median(u[near], 0))
            last = r
        if last is not None:
            ref[start:stop] = last
            status[start:stop] = 1
    return ref, status


POSTURE_FEATURES = [
    "ang_ref_5", "ang_ref_30", "ang_ref_120", "dz_ref_5", "dz_ref_30", "droll_ref_30", "dy_ref_30",
    "absroll_30", "z_30", "y_30", "absx_30",
    "gyro_30", "gyro_300", "dyn_30", "dyn_300", "still_120", "still_600", "ostd_60", "ostd_300",
    "ang_ref_p10_600", "ang_ref_p90_600",
    # Tail-elevation aware terms: tail raising rotates gravity from Y to Z, lying rolls it onto X.
    "elev_30", "delev_ref_30", "dabsx_ref_30", "td_ang_ref_300", "td_dabsx_300", "td_dz_300", "td_frac_300",
]


def posture_features(s, ref):
    """Per-second posture features; ``ref`` is the (n, 3) standing reference."""
    from scipy.ndimage import percentile_filter
    u, gyro, dyn = s["u"], s["gyro"], s["dyn"]
    cols = {}
    ang = _angle(u, ref)
    ang[~np.isfinite(u).all(1)] = np.nan
    for w in (5, 30, 120):
        m = _unit(_mov(u, -w, w + 1))
        cols[f"ang_ref_{w}"] = _angle(m, ref)
        if w in (5, 30):
            cols[f"dz_ref_{w}"] = m[:, 2] - ref[:, 2]
        if w == 30:
            cols["droll_ref_30"] = np.abs((_roll(m) - _roll(ref) + 180) % 360 - 180)
            cols["dy_ref_30"] = m[:, 1] - ref[:, 1]
            cols["absroll_30"] = np.abs(_roll(m))
            cols["z_30"], cols["y_30"], cols["absx_30"] = m[:, 2], m[:, 1], np.abs(m[:, 0])
            m30 = m
    lg, ld = np.log1p(gyro), np.log(dyn + 1e-3)
    cols["gyro_30"], cols["gyro_300"] = _mov(lg, -30, 31), _mov(lg, -300, 301)
    cols["dyn_30"], cols["dyn_300"] = _mov(ld, -30, 31), _mov(ld, -300, 301)
    still = np.where(np.isfinite(gyro), (gyro < STILL_DPS).astype(float), np.nan)
    cols["still_120"], cols["still_600"] = _mov(still, -120, 121), _mov(still, -600, 601)
    for w in (60, 300):
        mu = _mov(u, -w, w + 1)
        cols[f"ostd_{w}"] = np.sqrt(np.maximum(_mov(np.sum((u - mu) ** 2, 1), -w, w + 1), 0))
    fill = np.nanmedian(ang) if np.isfinite(ang).any() else 0.0
    a = np.where(np.isfinite(ang), ang, fill)
    # 10-min percentiles on a 10 s decimated series (exact per-second rank filters are too slow
    # for multi-day grids); each value only uses seconds within +-600 s.
    pad = (-len(a)) % 10
    a10 = np.r_[a, np.full(pad, a[-1] if len(a) else 0.0)].reshape(-1, 10).mean(1)
    rep = np.arange(len(a)) // 10
    cols["ang_ref_p10_600"] = percentile_filter(a10, 10, size=121, mode="nearest")[rep]
    cols["ang_ref_p90_600"] = percentile_filter(a10, 90, size=121, mode="nearest")[rep]
    elev, elev_ref = _elev(u), _elev(ref)
    cols["elev_30"] = _elev(m30)
    cols["delev_ref_30"] = cols["elev_30"] - elev_ref
    cols["dabsx_ref_30"] = np.abs(m30[:, 0]) - np.abs(ref[:, 0])
    down = np.isfinite(elev) & (elev <= elev_ref + 12)
    md = _unit(_mov(np.where(down[:, None], u, np.nan), -300, 301, min_frac=0.02))
    cols["td_ang_ref_300"] = _angle(md, ref)
    cols["td_dabsx_300"] = np.abs(md[:, 0]) - np.abs(ref[:, 0])
    cols["td_dz_300"] = md[:, 2] - ref[:, 2]
    cols["td_frac_300"] = _mov(np.where(np.isfinite(elev), down.astype(float), np.nan), -300, 301)
    X = np.column_stack([cols[k] for k in POSTURE_FEATURES])
    X[~s["valid"]] = np.nan
    return X


# ----------------------------------------------------------------------------- transition layer

TRANSITION_FEATURES = [
    "step_ang", "step_ang_long", "dz", "dz_long", "droll", "dy", "burst", "burst_ratio",
    "retain_post", "retain_pre", "ang_ref_pre", "ang_ref_post", "ref_gap", "still_pre", "still_post",
    "dyn_burst", "pl_pre", "pl_post", "pl_delta",
]


def transition_candidates(s, *, min_angle=8.0, min_sep=10):
    from scipy.signal import find_peaks
    u = s["u"]
    step = _angle(_unit(_mov(u, -25, -3)), _unit(_mov(u, 4, 26)))
    step[~np.isfinite(step)] = 0
    peaks, _ = find_peaks(step, height=min_angle, distance=min_sep)
    return peaks


def transition_features(s, ref, peaks, p_lying=None):
    u, gyro, dyn, n = s["u"], s["gyro"], s["dyn"], s["n"]
    still = np.where(np.isfinite(gyro), (gyro < STILL_DPS).astype(float), np.nan)

    def med(lo, hi, i):
        seg = u[max(0, i + lo):min(n, i + hi)]
        seg = seg[np.isfinite(seg).all(1)] if len(seg) else seg
        return _unit(np.median(seg, 0)) if len(seg) >= 3 else np.full(3, np.nan)

    def stat(x, lo, hi, i, fn=np.nanmean):
        seg = x[max(0, i + lo):min(n, i + hi)]
        return float(fn(seg)) if len(seg) and np.isfinite(seg).any() else np.nan

    rows = []
    for i in np.asarray(peaks, dtype=int):
        r = ref[i]
        pre, post = med(-25, -3, i), med(4, 26, i)
        pre_l, post_l = med(-90, -5, i), med(6, 91, i)
        pre_ll, post_ll = med(-240, -90, i), med(91, 241, i)
        with np.errstate(all="ignore"):
            f = dict(step_ang=_angle(pre, post), step_ang_long=_angle(pre_l, post_l),
                     dz=post[2] - pre[2], dz_long=post_l[2] - pre_l[2],
                     droll=abs(((_roll(post_l) - _roll(pre_l)) + 180) % 360 - 180), dy=post_l[1] - pre_l[1],
                     burst=stat(gyro, -8, 9, i, np.nanmax),
                     retain_post=_angle(post_l, post_ll), retain_pre=_angle(pre_l, pre_ll),
                     ang_ref_pre=_angle(pre_l, r), ang_ref_post=_angle(post_l, r),
                     still_pre=stat(still, -120, -5, i), still_post=stat(still, 6, 121, i),
                     dyn_burst=stat(dyn, -8, 9, i, np.nanmax))
            quiet = np.nanmax([stat(gyro, -60, -8, i, np.nanmedian), stat(gyro, 9, 60, i, np.nanmedian)])
            f["burst_ratio"] = np.log((f["burst"] + 1) / (quiet + 1)) if np.isfinite(quiet) else np.nan
            f["ref_gap"] = f["ang_ref_post"] - f["ang_ref_pre"]
            if p_lying is not None:
                f["pl_pre"], f["pl_post"] = stat(p_lying, -120, -5, i), stat(p_lying, 6, 121, i)
            else:
                f["pl_pre"] = f["pl_post"] = np.nan
            f["pl_delta"] = f["pl_post"] - f["pl_pre"]
        rows.append([float(f[k]) for k in TRANSITION_FEATURES])
    return np.asarray(rows, dtype=np.float64).reshape(len(rows), len(TRANSITION_FEATURES))


# ----------------------------------------------------------------------------- JSON models

def predict_model(model, X):
    """Class probabilities from a JSON model: logistic (``logit``) or numeric forest (``forest``)."""
    X = np.asarray(X, dtype=np.float64)
    med = np.asarray(model["median"], dtype=np.float64)
    if X.ndim != 2 or X.shape[1] != len(med):
        raise ValueError("Model feature count does not match")
    Z = np.where(np.isfinite(X), X, med)
    if model["kind"] == "logit":
        Z = (Z - np.asarray(model["mean"])) / np.asarray(model["scale"])
        logits = Z @ np.asarray(model["coef"], dtype=np.float64).T + np.asarray(model["intercept"], dtype=np.float64)
        if logits.shape[1] == 1:
            p = 1 / (1 + np.exp(-logits[:, 0]))
            return np.column_stack([1 - p, p])
        logits -= logits.max(1, keepdims=True)
        e = np.exp(logits)
        return e / e.sum(1, keepdims=True)
    if model["kind"] == "forest":
        out = np.zeros((len(Z), len(model["classes"])))
        for tree in model["trees"]:
            feat, thr = np.asarray(tree["feature"]), np.asarray(tree["threshold"])
            left, right = np.asarray(tree["left"]), np.asarray(tree["right"])
            value = np.asarray(tree["value"], dtype=np.float64)
            node = np.zeros(len(Z), dtype=np.int64)
            active = left[node] >= 0
            while active.any():
                idx = np.flatnonzero(active)
                nd = node[idx]
                node[idx] = np.where(Z[idx, feat[nd]] <= thr[nd], left[nd], right[nd])
                active = left[node] >= 0
            out += value[node]
        return out / len(model["trees"])
    raise ValueError("Unknown model kind")


# ----------------------------------------------------------------------------- state layer

def transition_prob_series(n, peaks, p_ld, p_su, hmm):
    """Per-second P(stand->lie), P(lie->stand) spread over +-window around each candidate."""
    a = np.full(n, float(hmm["leak"]))
    b = np.full(n, float(hmm["leak"]))
    w = int(hmm["transition_window_s"])
    g = float(hmm.get("transition_gain", 1.0))
    for i, pl, ps in zip(peaks, p_ld, p_su):
        lo, hi = max(0, int(i) - w), min(n, int(i) + w + 1)
        span = hi - lo
        if span <= 0:
            continue
        a[lo:hi] = np.maximum(a[lo:hi], 1 - (1 - min(0.999, g * pl)) ** (1 / span))
        b[lo:hi] = np.maximum(b[lo:hi], 1 - (1 - min(0.999, g * ps)) ** (1 / span))
    return a, b


def _likelihood_ratio(p_lying, valid, hmm):
    pi, fl, k = float(hmm["prior_lying"]), float(hmm["p_floor"]), float(hmm["kappa"])
    ok = np.isfinite(p_lying) & np.asarray(valid, bool)
    p = np.clip(np.where(ok, p_lying, pi), fl, 1 - fl)
    return np.where(ok, np.exp(k * (np.log(p / pi) - np.log((1 - p) / (1 - pi)))), 1.0), ok


def forward_filter(lr, a, b, valid, hmm, q0=None):
    """Causal filtered P(lying); the state resets to the prior after a gap > reset_gap_s."""
    n = len(lr)
    pi = float(hmm["prior_lying"])
    reset = int(hmm["reset_gap_s"])
    lr_, a_, b_, v_ = lr.tolist(), a.tolist(), b.tolist(), np.asarray(valid, bool).tolist()
    q = pi if q0 is None else float(q0)
    gap = 0
    out = [0.0] * n
    for t in range(n):
        if v_[t]:
            if gap > reset:
                q = pi
            gap = 0
        else:
            gap += 1
        pred = (1 - q) * a_[t] + q * (1 - b_[t])
        num = pred * lr_[t]
        q = num / (num + (1 - pred))
        out[t] = q
    return np.asarray(out)


def smooth_segment(fwd, lr, a, b, lo, hi, lag_end):
    """Fixed-lag smoothed P(lying) for seconds [lo, hi) using observations up to ``lag_end``."""
    r = 1.0
    out = np.empty(hi - lo)
    f_, lr_, a_, b_ = fwd, lr, a, b
    for t in range(lag_end - 1, lo - 1, -1):
        if t < hi:
            f = f_[t]
            out[t - lo] = f * r / (f * r + (1 - f))
        if t > lo:
            at, bt = a_[t], b_[t]
            e1 = lr_[t] * r
            r = (bt + (1 - bt) * e1) / ((1 - at) + at * e1)
    return out


def hard_path(post, valid, min_bout_s=60):
    """Two-state path; observed bouts shorter than ``min_bout_s`` merge into their neighbours."""
    s = (np.nan_to_num(np.asarray(post, float), nan=0.0) >= 0.5).astype(np.int8)
    if len(s) < 3:
        return s
    while True:
        edges = np.flatnonzero(np.diff(s)) + 1
        starts, ends = np.r_[0, edges], np.r_[edges, len(s)]
        lengths = ends - starts
        inner = np.arange(1, len(starts) - 1)
        if not len(inner):
            return s
        short = inner[lengths[inner] < min_bout_s]
        if not len(short):
            return s
        j = short[np.argmin(lengths[short])]
        s[starts[j]:ends[j]] = 1 - s[starts[j]]


# ----------------------------------------------------------------------------- series pipeline

ROW_COLUMNS = ("lying_ratio", "lying_ratio_hard", "lying_confidence", "lying_down_count", "standing_up_count",
               "lying_bout_minutes", "standing_reference_ok")


def infer_inputs(records, models):
    """Signal, posture and transition layers for one cow's records -> inputs of the state layer."""
    grid = build_grid(records)
    s = signals(grid)
    ref, ref_status = references(s)
    X = posture_features(s, ref)
    p = predict_model(models["posture"], X)[:, 1]
    p[~s["valid"]] = np.nan
    peaks = transition_candidates(s)
    if len(peaks):
        P = predict_model(models["transition"], transition_features(s, ref, peaks, p))
        p_su, p_ld = P[:, 1], P[:, 2]
    else:
        p_su = p_ld = np.zeros(0)
    return dict(t0_ms=grid["t0_ms"], n=s["n"], valid=s["valid"], p_lying=p, peaks=peaks, p_su=p_su, p_ld=p_ld,
                ref_status=ref_status, reference=ref, calibration=s["calibration"], record=grid["record"],
                offsets_s=grid["offsets_s"])


def state_layer(inputs, *, hmm=None, window_ms=600_000):
    """HMM fusion + fixed-lag smoothing + bouts -> (rows, posterior, state, onsets, offsets)."""
    hmm = {**DEFAULT_HMM, **(hmm or {})}
    n, t0, valid, p = inputs["n"], inputs["t0_ms"], np.asarray(inputs["valid"], bool), inputs["p_lying"]
    a, b = transition_prob_series(n, inputs["peaks"], inputs["p_ld"], inputs["p_su"], hmm)
    lr, _ = _likelihood_ratio(p, valid, hmm)
    fwd = forward_filter(lr, a, b, valid, hmm)
    first = int(t0 // window_ms) * window_ms
    post = np.full(n, np.nan)
    windows = []
    fl, lrl, al, bl = fwd.tolist(), lr.tolist(), a.tolist(), b.tolist()
    for ws in range(first, int(t0 + n * 1000), window_ms):
        lo = max(0, int(round((ws - t0) / 1000)))
        hi = min(n, int(round((ws + window_ms - t0) / 1000)))
        if hi <= lo:
            continue
        # Window [start, end) is smoothed with observations before end + SMOOTH_LAG_S only.
        post[lo:hi] = smooth_segment(fl, lrl, al, bl, lo, hi, min(n, hi + SMOOTH_LAG_S))
        windows.append((ws, lo, hi))
    post[~valid] = np.nan
    state = hard_path(np.where(valid, post, np.nan), valid, int(hmm["min_bout_s"]))
    edges = np.flatnonzero(np.diff(state)) + 1
    # Unobserved seconds on either side of an edge do not count as an observed transition.
    onsets = np.asarray([i for i in edges if state[i] == 1 and valid[i - 1] and valid[i]], int)
    offsets = np.asarray([i for i in edges if state[i] == 0 and valid[i - 1] and valid[i]], int)
    bout_minutes = {}
    for off in offsets:
        before = onsets[onsets < off]
        if len(before) and valid[before[-1]:off].mean() > 0.9:
            bout_minutes[int(off)] = (off - before[-1]) / 60.0
    ref_status = inputs.get("ref_status")
    rows = []
    for ws, lo, hi in windows:
        v = valid[lo:hi]
        row = dict(start_epoch_ms=int(ws), end_epoch_ms=int(ws + window_ms),
                   available_epoch_ms=int(ws + window_ms + LOOKAHEAD_MS),
                   coverage=round(min(1.0, float(v.sum()) / (window_ms / 1000)), 4))
        if v.sum() < 60:
            row.update({c: None for c in ROW_COLUMNS})
        else:
            q = post[lo:hi][v]
            ends = [bout_minutes[int(o)] for o in offsets[(offsets >= lo) & (offsets < hi)] if int(o) in bout_minutes]
            row.update(lying_ratio=float(np.mean(q)), lying_ratio_hard=float(np.mean(state[lo:hi][v])),
                       lying_confidence=float(np.mean(np.abs(2 * q - 1))),
                       lying_down_count=int(((onsets >= lo) & (onsets < hi)).sum()),
                       standing_up_count=int(((offsets >= lo) & (offsets < hi)).sum()),
                       lying_bout_minutes=float(np.mean(ends)) if ends else None,
                       standing_reference_ok=(float(np.mean(ref_status[lo:hi][v] == 1))
                                              if ref_status is not None else None))
        rows.append(row)
    return rows, post, state, onsets, offsets


def analyse_series(records, models, *, hmm=None, window_ms=600_000):
    """Lying occupancy for one cow's consecutive motion records.

    ``records``: iterable of (epoch0_ms, per-second summary).  ``models``: dict with
    ``posture`` and ``transition`` JSON models (and optional ``hmm`` parameters).
    Returns (rows, detail): absolute, epoch-aligned windows and the per-second arrays.
    """
    inputs = infer_inputs(records, models)
    rows, post, state, onsets, offsets = state_layer(inputs, hmm=hmm or models.get("hmm"), window_ms=window_ms)
    return rows, dict(inputs, posterior=post, state=state, lying_down_s=onsets, standing_up_s=offsets)


