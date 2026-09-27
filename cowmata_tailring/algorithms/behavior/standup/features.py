"""Per-candidate physical features for the stand-up / lie-down / other classifier."""
from __future__ import annotations

import numpy as np

FEATURE_NAMES = [
    "step", "burst_move", "burst_dur_s",
    "d_x", "d_y", "d_z", "pre_x", "pre_y", "pre_z", "post_x", "post_y", "post_z",
    "d_z_long", "retention_60s", "turn_deg", "path_deg", "efficiency",
    "dyn_peak", "dyn_mean", "gyro_peak", "gyro_mean", "jerk_peak",
    "pre_dyn_med", "post_dyn_med", "post_pre_act_logratio",
    "pre_ori_sd", "post_ori_sd", "pre_quiet_s",
    "move_first_half_frac", "z_overshoot", "peak_pos_frac",
    "n_subbursts", "ref_pre_angle", "ref_post_angle", "ref_gain",
    "acc_norm_peak", "post_act_60s", "pre_act_60s",
]


def _ang(a, b):
    a = a / max(np.linalg.norm(a), 1e-9)
    b = b / max(np.linalg.norm(b), 1e-9)
    return float(np.degrees(np.arccos(np.clip(a @ b, -1, 1))))


def record_reference(sig):
    """Upright reference: gravity direction during sustained moderate activity (walking/feeding happen standing)."""
    fs = int(sig["fs"])
    n = len(sig["dir"]) // (fs * 10)
    if n < 6:
        return None
    blk_dyn = sig["dyn"][: n * fs * 10].reshape(n, -1)
    blk_dir = sig["dir"][: n * fs * 10].reshape(n, fs * 10, 3).mean(1)
    level = np.median(blk_dyn, 1)
    sel = level >= np.percentile(level, 80)
    if sel.sum() < 3:
        return None
    ref = np.median(blk_dir[sel], 0)
    return ref / max(np.linalg.norm(ref), 1e-9)


def candidate_features(sig, c, ref=None):
    fs = sig["fs"]
    t, d, dyn, gm = sig["t"], sig["dir"], sig["dyn"], sig["gmag"]
    n = len(t)
    a, b = c["i0"], c["i1"]
    f = lambda s: int(s * fs)  # noqa: E731
    pre = d[max(0, a - f(20)):max(1, a - f(1))]
    post = d[min(n - 1, b + f(1)):min(n, b + f(20))]
    if len(pre) < f(3) or len(post) < f(3):
        return None
    gpre, gpost = np.median(pre, 0), np.median(post, 0)
    dd = gpost - gpre
    lpre = d[max(0, a - f(60)):max(1, a - f(1))]
    lpost = d[min(n - 1, b + f(1)):min(n, b + f(60))]
    late = d[min(n - 1, b + f(45)):min(n, b + f(75))]
    retention = _ang(np.median(late, 0), gpost) if len(late) > f(5) else np.nan
    seg = d[a:b]
    steps = np.linalg.norm(np.diff(seg[:: max(1, f(0.2))], axis=0), axis=1)
    path = float(np.degrees(np.sum(steps)))
    turn = _ang(gpre, gpost)
    mid = a + (b - a) // 2
    move_first = np.linalg.norm(np.median(d[max(a, mid - f(1)):mid + 1], 0) - gpre)
    total = max(np.linalg.norm(dd), 1e-6)
    zs = d[a:b, 2]
    lo, hi = min(gpre[2], gpost[2]), max(gpre[2], gpost[2])
    overshoot = float(max(zs.max() - hi, lo - zs.min(), 0.0))
    burst_dyn = dyn[a:b]
    thr = 0.5 * burst_dyn.max()
    ab = burst_dyn > thr
    n_sub = int(np.sum(np.diff(np.r_[0, ab.astype(int)]) == 1))
    acc = sig["acc"]
    jerk = np.linalg.norm(np.diff(acc[a:b + 1], axis=0), axis=1) * fs
    pre_dyn = dyn[max(0, a - f(20)):max(1, a - f(1))]
    post_dyn = dyn[min(n - 1, b + f(1)):min(n, b + f(20))]
    quiet = dyn[max(0, a - f(120)):a][::-1] < 0.06
    pre_quiet = float(np.argmin(quiet) / fs) if (len(quiet) and not quiet.all()) else len(quiet) / fs
    act60_post = float(np.mean(dyn[min(n - 1, b):min(n, b + f(60))]))
    act60_pre = float(np.mean(dyn[max(0, a - f(60)):max(1, a)]))
    if ref is not None:
        rp, rq = _ang(gpre, ref), _ang(gpost, ref)
    else:
        rp = rq = np.nan
    v = [
        c["step"], c["burst_move"], (b - a) / fs,
        dd[0], dd[1], dd[2], gpre[0], gpre[1], gpre[2], gpost[0], gpost[1], gpost[2],
        float(np.median(lpost[:, 2]) - np.median(lpre[:, 2])), retention, turn, path, turn / (path + 1),
        float(burst_dyn.max()), float(burst_dyn.mean()), float(gm[a:b].max()), float(gm[a:b].mean()),
        float(np.percentile(jerk, 95)) if len(jerk) else 0.0,
        float(np.median(pre_dyn)), float(np.median(post_dyn)),
        float(np.log((np.median(post_dyn) + .01) / (np.median(pre_dyn) + .01))),
        float(np.degrees(np.mean(np.std(pre, 0)))), float(np.degrees(np.mean(np.std(post, 0)))), pre_quiet,
        float(move_first / total), overshoot, float(np.argmax(burst_dyn) / max(len(burst_dyn), 1)),
        n_sub, rp, rq, rp - rq if ref is not None else np.nan,
        float(np.linalg.norm(acc[a:b], axis=1).max()), act60_post, act60_pre,
    ]
    return np.asarray(v, dtype=np.float64)
