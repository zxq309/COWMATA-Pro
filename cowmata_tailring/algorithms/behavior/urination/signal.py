"""One-second summaries and a guarded per-recording accelerometer self-check.

Every feature downstream is computed from these summaries. Seconds with less
than 80 % of the expected samples are invalid and are never bridged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .io import Recording


@dataclass
class Seconds:
    acc: np.ndarray  # (n, 3) mean acceleration, g (uncalibrated)
    acc_cal: np.ndarray  # (n, 3) bias-corrected unit-scale acceleration
    dyn: np.ndarray  # (n,) within-second acceleration SD norm, g
    gyro: np.ndarray  # (n,) RMS angular speed, dps
    gyro_max: np.ndarray  # (n,) max angular speed, dps
    mag: np.ndarray  # (n, 3)
    valid: np.ndarray  # (n,) bool
    saturated: np.ndarray  # (n,) bool
    fs_hz: float
    calibration: dict = field(default_factory=dict)

    def __len__(self):
        return len(self.gyro)


def _binned_mean(idx, x, n, cnt):
    return np.stack([np.bincount(idx, weights=x[:, j], minlength=n) for j in range(x.shape[1])], 1) / np.maximum(cnt, 1)[:, None]


def sphere_fit(a):
    A = np.c_[2 * a, np.ones(len(a))]
    y = (a ** 2).sum(1)
    sol, *_ = np.linalg.lstsq(A, y, rcond=None)
    c = sol[:3]
    r2 = sol[3] + c @ c
    if not np.isfinite(r2) or r2 <= 0:
        return None, None, np.inf
    r = float(np.sqrt(r2))
    resid = float(np.median(np.abs(np.linalg.norm(a - c, axis=1) - r)))
    return c, r, resid


def self_calibrate(acc, quiet):
    """Detect a mis-scaled / biased ring and correct only when well-posed."""
    norm = np.linalg.norm(acc[quiet], axis=1) if quiet.any() else np.linalg.norm(acc, axis=1)
    static_norm = float(np.median(norm)) if len(norm) else float("nan")
    info = dict(static_norm_g=static_norm, quiet_seconds=int(quiet.sum()), method="none",
                center=[0.0, 0.0, 0.0], radius=1.0, suspect=bool(not (0.85 <= static_norm <= 1.15)))
    if not info["suspect"]:
        return acc.copy(), info
    pts = acc[quiet] if quiet.sum() >= 120 else acc
    spread = np.sqrt(np.linalg.eigvalsh(np.cov(pts.T))) if len(pts) > 10 else np.zeros(3)
    if len(pts) >= 120 and spread[-2] >= 0.05:
        c, r, resid = sphere_fit(pts)
        if c is not None and 0.6 <= r <= 1.6 and resid < 0.05:
            info.update(method="sphere_fit", center=c.tolist(), radius=r, residual=resid)
            return (acc - c) / r, info
    # Bias of unknown direction: fall back to pure normalisation (direction only).
    info["method"] = "normalise_only"
    return acc / np.maximum(np.linalg.norm(acc, axis=1, keepdims=True), 1e-6), info


def summarize(rec: Recording, min_coverage: float = 0.8) -> Seconds:
    t = rec.times_ms / 1000.0
    idx = np.floor(t).astype(np.int64)
    n = int(idx[-1]) + 1
    cnt = np.bincount(idx, minlength=n).astype(float)
    dt = np.diff(rec.times_ms)
    fs = 1000.0 / float(np.median(dt[dt <= 100])) if np.any(dt <= 100) else 50.0
    acc = _binned_mean(idx, rec.acc_g, n, cnt)
    sq = _binned_mean(idx, rec.acc_g ** 2, n, cnt)
    dyn = np.sqrt(np.maximum(sq - acc ** 2, 0).sum(1))
    w2 = (rec.gyro_dps.astype(np.float64) ** 2).sum(1)
    gyro = np.sqrt(np.bincount(idx, weights=w2, minlength=n) / np.maximum(cnt, 1))
    gmax = np.zeros(n)
    np.maximum.at(gmax, idx, np.sqrt(w2))
    mag = _binned_mean(idx, rec.mag.astype(np.float64), n, cnt)
    sat_frames = np.any(np.abs(rec.raw_axes[:, 0:3].astype(np.int32)) >= 32767, axis=1)
    saturated = np.bincount(idx, weights=sat_frames, minlength=n) > 0
    valid = cnt >= min_coverage * fs
    for lo, hi in zip(rec.times_ms[:-1][dt > 1000], rec.times_ms[1:][dt > 1000]):
        valid[int(lo // 1000):int(hi // 1000) + 1] = False
    quiet = valid & (gyro < 4.0) & (dyn < 0.03)
    acc_cal, info = self_calibrate(acc, quiet)
    return Seconds(acc=acc, acc_cal=acc_cal, dyn=dyn, gyro=gyro, gyro_max=gmax, mag=mag, valid=valid,
                   saturated=saturated, fs_hz=fs, calibration=info)
