"""COWMATA tail-ring stand-up (起立过程) detector — signal layer.

numpy/scipy only so it runs inside the COWMATA Pro portable runtime.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import median_filter, uniform_filter1d
from scipy.signal import butter, find_peaks, sosfiltfilt

FS = 25.0


def resample(times_ms, acc_g, gyro_dps, fs=FS, gap_ms=1000.0):
    t = np.asarray(times_ms, dtype=np.float64)
    acc = np.asarray(acc_g, dtype=np.float64)
    gyr = np.asarray(gyro_dps, dtype=np.float64)
    order = np.argsort(t, kind="stable")
    t, acc, gyr = t[order], acc[order], gyr[order]
    keep = np.r_[True, np.diff(t) > 0]
    t, acc, gyr = t[keep], acc[keep], gyr[keep]
    step = 1000.0 / fs
    grid = np.arange(np.ceil(t[0] / step) * step, t[-1], step)
    a = np.column_stack([np.interp(grid, t, acc[:, i]) for i in range(3)])
    g = np.column_stack([np.interp(grid, t, gyr[:, i]) for i in range(3)])
    j = np.clip(np.searchsorted(t, grid), 1, len(t) - 1)
    valid = np.minimum(np.abs(grid - t[j - 1]), np.abs(t[j] - grid)) < gap_ms
    return grid, a, g, valid


def lp(x, fs, fc, order=2):
    return sosfiltfilt(butter(order, fc / (fs / 2), output="sos"), x, axis=0)


def unit(v):
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-9)


def estimate_bias(acc, fs):
    """Detect constant per-axis offsets (e.g. device 546C50CA01F1 has ~+1.9 g on z).

    A healthy accelerometer at rest reads |g|≈1. When the static norm departs by
    >0.25 g we fit a sphere to static samples; the centre is the bias.
    """
    grav = lp(acc, fs, 0.25)
    dyn = uniform_filter1d(np.linalg.norm(acc - lp(acc, fs, 0.5), axis=1), int(fs))
    static = dyn < 0.03
    pts = grav[static] if static.sum() > fs * 30 else grav
    rest_norm = float(np.median(np.linalg.norm(pts, axis=1)))
    bias = np.zeros(3)
    quality = "ok"
    if abs(rest_norm - 1.0) > 0.25:
        quality = "norm_out_of_range"
        p = pts[:: max(1, len(pts) // 5000)]
        A = np.column_stack([2 * p, np.ones(len(p))])
        sol, *_ = np.linalg.lstsq(A, (p ** 2).sum(1), rcond=None)
        c = sol[:3]
        r = np.sqrt(max(sol[3] + c @ c, 1e-6))
        if 0.6 < r < 1.4 and np.linalg.norm(c) < 3:
            bias, quality = c, "bias_corrected"
        else:
            # Fall back to removing the median excess along the dominant axis only.
            m = np.median(pts, 0)
            k = int(np.argmax(np.abs(m)))
            corr = np.zeros(3)
            corr[k] = m[k] - np.sign(m[k]) * np.sqrt(max(1 - (m ** 2).sum() + m[k] ** 2, 0.05))
            bias, quality = corr, "bias_axis_corrected"
    return bias, rest_norm, quality


def preprocess(times_ms, acc_g, gyro_dps, fs=FS):
    t, acc, gyr, valid = resample(times_ms, acc_g, gyro_dps, fs)
    bias, rest_norm, quality = estimate_bias(acc, fs)
    acc = acc - bias
    grav = lp(acc, fs, 0.25)
    dyn_vec = acc - lp(acc, fs, 0.5)
    dyn = np.sqrt(uniform_filter1d((dyn_vec ** 2).sum(1), int(fs)))
    gmag = uniform_filter1d(np.linalg.norm(gyr, axis=1), int(fs))
    return dict(t=t, acc=acc, gyr=gyr, grav=grav, dir=unit(grav), dyn=dyn, gmag=gmag, valid=valid,
                fs=fs, rest_norm=rest_norm, bias=bias, quality=quality)


def step_profile(sig, half_s=18.0, guard_s=2.0):
    """1 Hz grid; score = |median dir in (t+guard, t+guard+half) - median dir in (t-guard-half, t-guard)|."""
    fs = sig["fs"]
    hop = int(fs)
    d = sig["dir"]
    n = len(d) // hop
    sec = d[: n * hop].reshape(n, hop, 3).mean(1)
    k = int(half_s)
    gd = int(guard_s)
    med = median_filter(sec, size=(k, 1), mode="nearest")
    c = k // 2
    idx = np.arange(n)
    b_i, a_i = idx - gd - c, idx + gd + c
    ok = (b_i >= 0) & (a_i < n)
    before = np.full((n, 3), np.nan)
    after = np.full((n, 3), np.nan)
    before[ok] = med[b_i[ok]]
    after[ok] = med[a_i[ok]]
    score = np.linalg.norm(after - before, axis=1)
    return dict(sec_t=sig["t"][0] + (idx + 0.5) * 1000.0, score=score, before=before, after=after)


def active_runs(x, hi, lo, merge_gap, min_len):
    above = x >= lo
    edges = np.diff(np.r_[0, above.astype(np.int8), 0])
    runs = [[a, b] for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))
            if np.max(x[a:b]) >= hi]
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [r for r in merged if r[1] - r[0] >= min_len]


DEFAULT_PARAMS = dict(
    step_half_s=18.0, step_guard_s=2.0, step_min=0.12, peak_distance_s=12,
    dyn_hi=0.12, gyro_hi=30.0, act_lo_ratio=0.45, merge_gap_s=3.0, min_burst_s=0.8, attach_s=20.0,
)


def candidates(sig, params=DEFAULT_PARAMS):
    """Persistent orientation-step peaks, each bound to the activity burst that caused it."""
    st = step_profile(sig, params["step_half_s"], params["step_guard_s"])
    score = np.nan_to_num(st["score"])
    peaks, _ = find_peaks(score, height=params["step_min"], distance=int(params["peak_distance_s"]))
    fs = sig["fs"]
    t = sig["t"]
    act = np.maximum(sig["dyn"] / params["dyn_hi"], sig["gmag"] / params["gyro_hi"])
    runs = active_runs(act, 1.0, params["act_lo_ratio"], int(params["merge_gap_s"] * fs),
                       int(params["min_burst_s"] * fs))
    out = []
    for p in peaks:
        tc = st["sec_t"][p]
        win = params["attach_s"] * 1000
        best, best_mv = None, -1.0
        for a, b in runs:
            if t[b - 1] < tc - win or t[a] > tc + win:
                continue
            pre = sig["dir"][max(0, a - int(3 * fs)):a + 1].mean(0)
            post = sig["dir"][b - 1:min(len(t), b + int(3 * fs))].mean(0)
            mv = float(np.linalg.norm(post - pre))
            if mv > best_mv:
                best, best_mv = (a, b), mv
        if best is None:
            continue
        a, b = best
        out.append(dict(i0=int(a), i1=int(b), t0=float(t[a]), t1=float(t[b - 1]), anchor=float(tc),
                        step=float(score[p]), burst_move=best_mv))
    out.sort(key=lambda c: (c["t0"], -c["step"]))
    dedup = []
    for c in out:
        if dedup and c["t0"] <= dedup[-1]["t1"]:
            if c["step"] > dedup[-1]["step"]:
                dedup[-1] = c
            continue
        dedup.append(c)
    return dedup, st, runs
