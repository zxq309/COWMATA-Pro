"""Per-candidate features (bias-invariant by design).

A biased / mis-scaled ring (e.g. static |a| = 2.4 g) keeps the *difference*
between two gravity readings intact, so lift size, lift angle and lift
direction are all computed from ``a(t) - ref``. Absolute-orientation features
(``ref_u*``) are set to NaN for suspect rings and imputed by the model.
"""
from __future__ import annotations

import numpy as np

from .candidates import Candidate
from .signal import Seconds

FEATURES: list[str] | None = None


def _unit(v):
    v = np.asarray(v, dtype=float)
    return v / max(float(np.linalg.norm(v)), 1e-9)


def _chord_deg(d_g):
    """Rotation angle of a unit gravity vector whose tip moved by |d| (g)."""
    return np.degrees(2 * np.arcsin(np.clip(np.asarray(d_g, dtype=float) / 2, 0, 1)))


def _angle(a, b):
    a = np.atleast_2d(a)
    u = a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-9)
    return np.degrees(np.arccos(np.clip(u @ _unit(b), -1, 1)))


def _med(x):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    return float(np.median(x)) if len(x) else np.nan


def _q(x, q):
    x = np.asarray(x, dtype=float)
    return float(np.quantile(x, q)) if len(x) else np.nan


def _window(sec: Seconds, lo: int, hi: int):
    lo, hi = max(0, lo), min(len(sec), hi)
    if hi <= lo:
        return np.zeros(0, dtype=int)
    idx = np.arange(lo, hi)
    return idx[sec.valid[idx]]


def _longest_run(mask):
    """(start, length) of the longest True run."""
    if not len(mask) or not mask.any():
        return 0, 0
    d = np.diff(np.r_[0, mask.astype(int), 0])
    starts, stops = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    i = int(np.argmax(stops - starts))
    return int(starts[i]), int(stops[i] - starts[i])


def _still_hold(disp, gyro, peak, gyro_max_dps=8.0, gap=2):
    """Longest lifted-and-still run; gaps up to ``gap`` s (a single swish) are bridged."""
    m = (disp >= 0.5 * peak) & (gyro < gyro_max_dps)
    if gap and m.any():
        d = np.diff(np.r_[0, m.astype(int), 0])
        starts, stops = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        for a, b in zip(stops[:-1], starts[1:]):
            if b - a <= gap:
                m[a:b] = True
    return _longest_run(m)


def session_stats(sec: Seconds) -> dict:
    v = sec.valid
    return dict(gyro=_med(sec.gyro[v]), dyn=_med(sec.dyn[v]))


def candidate_features(c: Candidate, sec: Seconds, ref_s: int = 20, ref_gap_s: int = 2, stats: dict | None = None) -> dict:
    stats = stats or session_stats(sec)
    s, e = c.start, c.end
    dur = e - s + 1
    seg = np.arange(s, e + 1)
    seg = seg[sec.valid[seg]]
    if len(seg) == 0:
        seg = np.arange(s, e + 1)
    ref_idx = _window(sec, s - ref_s - ref_gap_s, s - ref_gap_s)
    if len(ref_idx) == 0:
        ref_idx = _window(sec, s - 10, s)
    delta = sec.acc[seg] - c.ref
    disp = np.linalg.norm(delta, axis=1)
    q = max(1, int(round(dur * 0.2)))
    cm = (seg >= s + q) & (seg <= e - q) if dur >= 5 else np.ones(len(seg), bool)
    if not cm.any():
        cm[:] = True
    core, dcore, dvec = seg[cm], disp[cm], delta[cm]
    peak = float(disp.max())
    above80 = np.flatnonzero(disp >= 0.8 * peak)
    half = disp >= 0.5 * peak
    lifts = int(np.sum(np.diff(np.r_[0, half.astype(int)]) == 1))
    lift_mean = dvec.mean(0)
    lift_dir = _unit(lift_mean)
    udv = dvec / np.maximum(np.linalg.norm(dvec, axis=1, keepdims=True), 1e-9)
    suspect = bool(sec.calibration.get("suspect"))
    refu = _unit(np.median(sec.acc_cal[ref_idx], 0)) if len(ref_idx) else np.full(3, np.nan)
    if suspect:
        refu = np.full(3, np.nan)
    post_idx = _window(sec, e + 3, e + 16)
    later_idx = _window(sec, e + 20, e + 60)
    pre_ctx = _window(sec, s - 80, s - ref_s - ref_gap_s)
    post_disp = float(np.linalg.norm(np.median(sec.acc[post_idx], 0) - c.ref)) if len(post_idx) else np.nan
    later_disp = float(np.linalg.norm(np.median(sec.acc[later_idx], 0) - c.ref)) if len(later_idx) else np.nan
    g = sec.gyro
    on_idx = _window(sec, s - 3, s + 5)
    off_idx = _window(sec, e - 3, e + 5)
    gy_pre, gy_hold = _med(g[ref_idx]), _med(g[core])
    m_ref = np.median(sec.mag[ref_idx], 0) if len(ref_idx) else sec.mag[s]
    m_hold = np.median(sec.mag[core], 0)
    m_post = np.median(sec.mag[post_idx], 0) if len(post_idx) else None
    path = float(np.linalg.norm(np.diff(sec.acc[seg], axis=0), axis=1).sum()) if len(seg) > 1 else 0.0
    sg = stats.get("gyro", np.nan)
    gseg = g[seg]
    r0, rl = _still_hold(disp, gseg, peak)
    lifted_run = _longest_run(disp >= 0.5 * peak)[1]
    if rl >= 3:
        rd, rg, rv = disp[r0:r0 + rl], gseg[r0:r0 + rl], delta[r0:r0 + rl]
        run_dir = _unit(rv.mean(0))
        pre_run = gseg[max(0, r0 - 5):r0]
        still = dict(still_run_s=float(rl), still_frac=float(rl / dur), still_lift_deg=float(_chord_deg(np.median(rd))),
                     still_gyro=float(np.median(rg)), still_disp_cv=float(np.std(rd) / max(np.mean(rd), 1e-6)),
                     still_dir_z=float(run_dir[2]), still_dir_y=float(run_dir[1]), still_start_rel=float(r0 / dur),
                     still_pre_gyro=float(np.max(pre_run)) if len(pre_run) else np.nan)
    else:
        still = dict(still_run_s=float(rl), still_frac=float(rl / dur), still_lift_deg=np.nan, still_gyro=np.nan,
                     still_disp_cv=np.nan, still_dir_z=np.nan, still_dir_y=np.nan, still_start_rel=np.nan, still_pre_gyro=np.nan)
    tpl_x = np.linspace(0, len(disp) - 1, 8)
    tpl = {f"shape_d{i}": float(v) for i, v in enumerate(np.interp(tpl_x, np.arange(len(disp)), disp / max(peak, 1e-6)))}
    tpl.update({f"shape_g{i}": float(v) for i, v in enumerate(np.interp(tpl_x, np.arange(len(gseg)), np.log1p(gseg)))})
    return dict(
        **still, **tpl, lifted_run_s=float(lifted_run),
        dur_s=float(dur), log_dur=float(np.log(dur)), valid_frac=float(sec.valid[s:e + 1].mean()),
        truncated=float(c.truncated), shift=float(c.shift), child=float(c.child),
        peak_g=peak, hold_med_g=_med(dcore), hold_p10_g=_q(dcore, 0.1), hold_frac50=float(half.mean()),
        hold_cv=float(np.std(dcore) / max(np.mean(dcore), 1e-6)),
        rise_s=float(above80[0]), fall_s=float(len(disp) - 1 - above80[-1]),
        peak_pos=float(np.argmax(disp) / max(len(disp) - 1, 1)), n_lifts=float(lifts),
        path_per_s=path / dur, path_efficiency=float(np.linalg.norm(sec.acc[seg[-1]] - sec.acc[seg[0]]) / max(path, 1e-6)),
        lift_deg=float(_chord_deg(_med(dcore))), lift_deg_max=float(_chord_deg(peak)),
        lift_dir_x=float(lift_dir[0]), lift_dir_y=float(lift_dir[1]), lift_dir_z=float(lift_dir[2]),
        lift_dir_consistency=float(np.linalg.norm(udv.mean(0))),
        ref_ux=float(refu[0]), ref_uy=float(refu[1]), ref_uz=float(refu[2]),
        post_disp_g=post_disp, return_ratio=post_disp / max(peak, 1e-6) if np.isfinite(post_disp) else np.nan,
        later_disp_g=later_disp, post_deg=float(_chord_deg(post_disp)) if np.isfinite(post_disp) else np.nan,
        gyro_pre=gy_pre, gyro_hold=gy_hold, gyro_hold_p90=_q(g[core], 0.9),
        gyro_hold_logratio=float(np.log((gy_hold + 1) / (gy_pre + 1))) if np.isfinite(gy_pre) else np.nan,
        gyro_hold_rel_session=float(np.log((gy_hold + 1) / (sg + 1))) if np.isfinite(sg) else np.nan,
        gyro_onset_max=float(g[on_idx].max()) if len(on_idx) else np.nan,
        gyro_offset_max=float(g[off_idx].max()) if len(off_idx) else np.nan,
        gyro_post=_med(g[post_idx]), gyro_ctx_pre=_med(g[pre_ctx]), gyro_session=sg,
        swish_frac=float(np.mean(sec.gyro_max[core] > 40)),
        dyn_pre=_med(sec.dyn[ref_idx]), dyn_hold=_med(sec.dyn[core]), dyn_hold_p90=_q(sec.dyn[core], 0.9),
        mag_hold_deg=float(_angle(m_hold, m_ref)[0]) if np.linalg.norm(m_ref) > 0 else np.nan,
        mag_post_deg=float(_angle(m_post, m_ref)[0]) if m_post is not None and np.linalg.norm(m_ref) > 0 else np.nan,
        static_norm_g=float(sec.calibration.get("static_norm_g", np.nan)), cal_suspect=float(suspect),
        saturated_frac=float(sec.saturated[seg].mean()),
    )


SESSION_REL = ["peak_g", "hold_med_g", "still_run_s", "gyro_hold", "dur_s"]


def add_session_relative(rows):
    """Individual baselining: each candidate relative to the other candidates of
    the same recording (rank and ratio to the recording median)."""
    import pandas as pd

    rows = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    for f in SESSION_REL:
        if not len(rows):
            rows[f + "_srank"], rows[f + "_srel"] = [], []
            continue
        rows[f + "_srank"] = rows[f].rank(pct=True)
        rows[f + "_srel"] = rows[f] / (rows[f].median() + 1e-6)
    return rows


def feature_names() -> list[str]:
    global FEATURES
    if FEATURES is None:
        n = 120
        acc = np.tile([0.0, -0.95, 0.3], (n, 1)).astype(float)
        acc[40:70] = [0.0, -0.8, 0.6]
        sec = Seconds(acc=acc, acc_cal=acc, dyn=np.zeros(n), gyro=np.ones(n), gyro_max=np.ones(n),
                      mag=np.ones((n, 3)), valid=np.ones(n, bool), saturated=np.zeros(n, bool), fs_hz=50.0,
                      calibration={})
        base = list(candidate_features(Candidate(40, 69, 0.3, acc[0], False), sec))
        FEATURES = base + [f + s for f in SESSION_REL for s in ("_srank", "_srel")]
    return FEATURES
