"""Signal layer of the straining (努责) detector for tail-mounted 9-axis rings: contraction-pulse-train model.

Observed regularity (COWMATA_Behavior_Dataset, 222 bouts / 20 cows): each labeled straining bout
is a train of quiet, smooth tail-tilt pulses (~1 s wide, a few degrees) recurring every 2.5-5.5 s
while high-frequency tail motion stays low. Tail wagging produces much larger, gyro-rich pulses.

Pipeline: gravity direction -> tilt deviation from slow posture -> pulse detection (5 Hz)
-> per-second pulse-train and motion-context features -> numeric forest score
-> hysteresis + gap merge -> straining bouts. Only numpy/scipy are required at inference.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import maximum_filter1d, median_filter, uniform_filter1d
from scipy.signal import butter, find_peaks, sosfiltfilt

FEATURE_VERSION = "straining-pulse-train-3"
FS = 50.0
DS = 10  # 50 Hz -> 5 Hz for pulse work
QUIET_HF_DPS = 8.0  # max mean high-frequency gyro inside a contraction pulse
MIN_PROMINENCE_DEG = 1.0
_SOS = {}


def _sos(kind, f):
    key = (kind, f if np.isscalar(f) else tuple(f))
    if key not in _SOS:
        _SOS[key] = butter(2, f, btype=kind, fs=FS, output="sos")
    return _SOS[key]


def _segments(t_ms, max_gap_ms=200.0, min_len=FS * 30):
    cut = np.flatnonzero(np.diff(t_ms) > max_gap_ms) + 1
    edges = np.r_[0, cut, len(t_ms)]
    return [(a, b) for a, b in zip(edges[:-1], edges[1:]) if b - a >= min_len]


def _count_in(times, centers, half):
    times = np.sort(times)
    return np.searchsorted(times, centers + half, "right") - np.searchsorted(
        times, centers - half, "left"
    )


def _window_stat(times, values, centers, half, fn, default=np.nan):
    order = np.argsort(times)
    times, values = times[order], values[order]
    lo = np.searchsorted(times, centers - half, "left")
    hi = np.searchsorted(times, centers + half, "right")
    out = np.full(len(centers), default, float)
    for i, (a, b) in enumerate(zip(lo, hi)):
        if b > a:
            out[i] = fn(values[a:b])
    return out


def _ipi_stats(times, centers, half):
    times = np.sort(times)
    lo = np.searchsorted(times, centers - half, "left")
    hi = np.searchsorted(times, centers + half, "right")
    med = np.full(len(centers), np.nan)
    cv = np.full(len(centers), np.nan)
    for i, (a, b) in enumerate(zip(lo, hi)):
        if b - a >= 3:
            d = np.diff(times[a:b])
            m = np.median(d)
            med[i] = m
            cv[i] = np.std(d) / max(m, 1e-6)
    return med, cv


def signal_layers(acc_g, gyr_dps):
    acc = np.asarray(acc_g, float)
    gyr = np.asarray(gyr_dps, float)
    grav = sosfiltfilt(_sos("low", 0.8), acc, axis=0)
    u = grav / np.maximum(np.linalg.norm(grav, axis=1, keepdims=True), 1e-6)
    base = sosfiltfilt(_sos("low", 0.03), u, axis=0)
    base /= np.maximum(np.linalg.norm(base, axis=1, keepdims=True), 1e-6)
    dev = np.degrees(np.arccos(np.clip(np.sum(u * base, 1), -1, 1)))
    hf = np.linalg.norm(sosfiltfilt(_sos("high", 1.5), gyr, axis=0), axis=1)
    dyn = np.linalg.norm(acc - grav, axis=1)
    elev = np.degrees(np.arccos(np.clip(-u[:, 1], -1, 1)))  # device y axis runs along the tail
    return dict(u=u, base=base, dev=dev, hf=hf, dyn=dyn, elev=elev, gyr=np.linalg.norm(gyr, axis=1))


def detect_pulses(t_ms, layers):
    d5 = layers["dev"][::DS]
    h5 = layers["hf"][::DS]
    t5 = np.asarray(t_ms)[::DS] / 1000.0
    pk, pr = find_peaks(d5, prominence=MIN_PROMINENCE_DEG, width=(2, 40), distance=8)
    if not len(pk):
        return dict(
            t=np.zeros(0),
            prom=np.zeros(0),
            width=np.zeros(0),
            hf=np.zeros(0),
            quiet=np.zeros(0, bool),
            dx=np.zeros(0),
            dy=np.zeros(0),
            dz=np.zeros(0),
        )
    vec = layers["u"][pk * DS] - layers["base"][pk * DS]
    vec /= np.maximum(np.linalg.norm(vec, axis=1, keepdims=True), 1e-9)
    lo = np.clip(pr["left_ips"].astype(int), 0, len(h5) - 1)
    hi = np.clip(pr["right_ips"].astype(int) + 1, 1, len(h5))
    c = np.r_[0, np.cumsum(h5)]
    hfm = (c[hi] - c[lo]) / np.maximum(hi - lo, 1)
    return dict(
        t=t5[pk],
        prom=pr["prominences"],
        width=pr["widths"] / (FS / DS),
        hf=hfm,
        quiet=hfm < QUIET_HF_DPS,
        dx=vec[:, 0],
        dy=vec[:, 1],
        dz=vec[:, 2],
    )


def second_features(t_ms, acc_g, gyr_dps):
    """Return per-second feature matrix; seconds outside contiguous >=30 s segments are invalid."""
    t_ms = np.asarray(t_ms, float)
    if len(t_ms) < 2 or np.any(np.diff(t_ms) <= 0):
        raise ValueError("timestamps must be strictly increasing")
    n = int(t_ms[-1] // 1000) + 1
    centers = np.arange(n) + 0.5
    valid = np.zeros(n, bool)
    sec_cols = {
        k: np.full(n, np.nan) for k in ("hf", "gyr95", "dyn95", "dev", "elev", "bx", "by", "bz")
    }
    P = {k: [] for k in ("t", "prom", "width", "hf", "quiet", "dx", "dy", "dz")}
    for a, b in _segments(t_ms):
        L = signal_layers(np.asarray(acc_g)[a:b], np.asarray(gyr_dps)[a:b])
        p = detect_pulses(t_ms[a:b], L)
        for k in P:
            P[k].append(p[k])
        sec = (t_ms[a:b] // 1000).astype(int)
        s0, s1 = sec[0] + 1, sec[-1]  # drop partial edge seconds
        valid[s0:s1] = True
        cnt = np.bincount(sec - sec[0])
        starts = np.r_[0, np.cumsum(cnt)[:-1]]
        for j, s in enumerate(range(sec[0], sec[-1] + 1)):
            if cnt[j] == 0:
                continue
            sl = slice(starts[j], starts[j] + cnt[j])
            sec_cols["hf"][s] = L["hf"][sl].mean()
            sec_cols["gyr95"][s] = np.percentile(L["gyr"][sl], 95)
            sec_cols["dyn95"][s] = np.percentile(L["dyn"][sl], 95)
            sec_cols["dev"][s] = L["dev"][sl].mean()
            sec_cols["elev"][s] = L["elev"][sl].mean()
            sec_cols["bx"][s], sec_cols["by"][s], sec_cols["bz"][s] = L["base"][sl].mean(0)
    P = {k: (np.concatenate(v) if v else np.zeros(0)) for k, v in P.items()}
    q = P["quiet"].astype(bool)
    qt, qp, qw = P["t"][q], P["prom"][q], P["width"][q]
    F = {}
    for h in (6, 12, 24):
        F[f"quiet_pulses_{2 * h}s"] = _count_in(qt, centers, h).astype(float)
    F["all_pulses_24s"] = _count_in(P["t"], centers, 12).astype(float)
    F["quiet_fraction_24s"] = F["quiet_pulses_24s"] / np.maximum(F["all_pulses_24s"], 1)
    F["ipi_median_24s"], F["ipi_cv_24s"] = _ipi_stats(qt, centers, 12)
    F["ipi_median_48s"], F["ipi_cv_48s"] = _ipi_stats(qt, centers, 24)
    F["quiet_prom_median_24s"] = _window_stat(qt, qp, centers, 12, np.median)
    F["quiet_prom_max_24s"] = _window_stat(qt, qp, centers, 12, np.max)
    F["quiet_width_median_24s"] = _window_stat(qt, qw, centers, 12, np.median)
    F["nearest_quiet_pulse_s"] = np.minimum(_nearest(qt, centers), 60.0)
    qv = np.column_stack([P["dx"][q], P["dy"][q], P["dz"][q]]) if q.any() else np.zeros((0, 3))
    F["pulse_direction_R_24s"] = _resultant(qt, qv, centers, 12)
    F["pulse_direction_R_48s"] = _resultant(qt, qv, centers, 24)
    F["quiet_prom_cv_24s"] = _window_stat(
        qt, qp, centers, 12, lambda v: np.std(v) / max(np.mean(v), 1e-6) if len(v) >= 3 else np.nan
    )
    F["loud_pulses_24s"] = F["all_pulses_24s"] - F["quiet_pulses_24s"]

    def fill(x):
        return np.where(np.isfinite(x), x, np.nanmedian(x) if np.isfinite(x).any() else 0.0)

    hf, g95, d95, dev, elev = (fill(sec_cols[k]) for k in ("hf", "gyr95", "dyn95", "dev", "elev"))
    for w in (5, 15, 31):
        F[f"log_hf_mean_{w}s"] = np.log1p(uniform_filter1d(hf, w))
        F[f"dev_mean_{w}s"] = uniform_filter1d(dev, w)
    F["log_gyr95_max_9s"] = np.log1p(maximum_filter1d(g95, 9))
    F["log_gyr95_max_31s"] = np.log1p(maximum_filter1d(g95, 31))
    F["dyn95_mean_15s"] = uniform_filter1d(d95, 15)
    F["tail_elev_15s"] = uniform_filter1d(elev, 15)
    F["tail_elev_rel_600s"] = F["tail_elev_15s"] - median_filter(elev, 601, mode="nearest")
    bvec = np.column_stack([fill(sec_cols[k]) for k in ("bx", "by", "bz")])
    bvec /= np.maximum(np.linalg.norm(bvec, axis=1, keepdims=True), 1e-9)
    for h in (15, 45):
        prev = np.vstack([np.repeat(bvec[:1], h, 0), bvec[:-h]])
        nxt = np.vstack([bvec[h:], np.repeat(bvec[-1:], h, 0)])
        F[f"posture_change_{2 * h}s"] = np.degrees(np.arccos(np.clip(np.sum(prev * nxt, 1), -1, 1)))
    F["side_lying_index"] = np.abs(bvec[:, 0]) - np.abs(bvec[:, 2])
    F["pulse_to_motion_logratio"] = np.log(
        (F["dev_mean_15s"] + 0.1) / (uniform_filter1d(hf, 15) + 1.0)
    )
    # Contraction clusters (continuous analogue of a leaky contraction counter).
    train = (
        (F["quiet_pulses_24s"] >= 4) & (np.nan_to_num(F["pulse_direction_R_24s"]) >= 0.6) & valid
    ).astype(float)
    F["train_share_600s"] = uniform_filter1d(train, 601)
    F["train_share_180s"] = uniform_filter1d(train, 181)
    F["train_peak_300s"] = maximum_filter1d(uniform_filter1d(train, 31), 301)
    names = list(F)
    X = np.column_stack([F[k] for k in names]).astype(np.float32)
    X[~valid] = np.nan
    return dict(
        X=X,
        names=names,
        valid=valid,
        seconds=centers,
        pulses=P,
        feature_version=FEATURE_VERSION,
        duration_ms=float(t_ms[-1]),
    )


def _resultant(times, vecs, centers, half):
    order = np.argsort(times)
    times, vecs = times[order], vecs[order]
    lo = np.searchsorted(times, centers - half, "left")
    hi = np.searchsorted(times, centers + half, "right")
    c = np.vstack([np.zeros((1, 3)), np.cumsum(vecs, 0)]) if len(vecs) else np.zeros((1, 3))
    n = hi - lo
    s = c[hi] - c[lo] if len(vecs) else np.zeros((len(centers), 3))
    return np.where(n >= 3, np.linalg.norm(s, axis=1) / np.maximum(n, 1), np.nan)


def _nearest(times, centers):
    if not len(times):
        return np.full(len(centers), np.inf)
    times = np.sort(times)
    i = np.clip(np.searchsorted(times, centers), 1, len(times) - 1)
    return np.minimum(np.abs(times[i] - centers), np.abs(times[i - 1] - centers))


DEFAULT_SEGMENTATION = dict(
    smooth_s=5, high=0.6, low=0.4, merge_gap_s=6, min_duration_s=3, min_quiet_pulses=2
)


def segment(scores, valid, pulses, params=None, duration_ms=None):
    p = {**DEFAULT_SEGMENTATION, **(params or {})}
    s = np.where(valid & np.isfinite(scores), scores, 0.0)
    if p["smooth_s"] > 1:
        s = uniform_filter1d(s, int(p["smooth_s"]))
    hi, lo = s >= p["high"], s >= p["low"]
    edge = np.diff(np.r_[False, lo, False].astype(int))
    runs = [
        (a, b)
        for a, b in zip(np.flatnonzero(edge == 1), np.flatnonzero(edge == -1))
        if hi[a:b].any()
    ]
    merged = []
    for a, b in runs:
        if merged and a - merged[-1][1] <= p["merge_gap_s"] and valid[merged[-1][1] : a].all():
            merged[-1][1] = b
        else:
            merged.append([a, b])
    qt = np.sort(pulses["t"][pulses["quiet"].astype(bool)]) if len(pulses["t"]) else np.zeros(0)
    events = []
    for a, b in merged:
        if b - a < p["min_duration_s"]:
            continue
        n_q = int(np.searchsorted(qt, b, "right") - np.searchsorted(qt, a, "left"))
        if n_q < p["min_quiet_pulses"]:
            continue
        end = float(b * 1000) if duration_ms is None else min(float(b * 1000), duration_ms)
        events.append(
            dict(
                code="STRAINING_BOUT",
                start_ms=float(a * 1000),
                end_ms=end,
                score=float(s[a:b].max()),
                mean_score=float(s[a:b].mean()),
                contraction_pulses=n_q,
                time_semantics="proposed_interval",
                review_status="pending",
            )
        )
    return events, s
