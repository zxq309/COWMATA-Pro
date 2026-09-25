"""Label-free Lift-Hold-Return candidate generator (1 Hz).

Two guards keep the reference honest:
* the reference window never overlaps the previous candidate (otherwise the
  lifted tail becomes the reference and the tail drop looks like a new lift);
* a long candidate whose last ``settle_s`` seconds are steady is a posture
  shift - it is closed and the steady state becomes the next reference.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEFAULT_PARAMS = dict(
    on_g=0.08,  # |a - ref| needed to open a candidate (g)
    release_rel=0.35,  # candidate closes when displacement < rel * peak ...
    release_abs_g=0.03,  # ... and never below this absolute floor
    release_s=3,  # consecutive released seconds that close a candidate
    ref_s=20,  # reference window length before onset (s)
    ref_gap_s=2,  # guard between reference window and onset
    backtrack_s=6,  # onset search before the trigger second
    min_s=5,  # shortest candidate kept
    max_s=200,  # hard cap
    settle_after_s=60,  # posture-shift test starts after this duration
    settle_s=30,  # steady window length for the posture-shift test
    settle_range_g=0.06,  # steady = displacement range below this ...
    settle_gyro_dps=8.0,  # ... and median gyro below this
)


@dataclass
class Candidate:
    start: int  # first second (inclusive)
    end: int  # last second (inclusive)
    peak_g: float
    ref: np.ndarray
    truncated: bool
    shift: bool = False
    child: bool = False

    @property
    def duration(self):
        return self.end - self.start + 1


def add_children(cands: list[Candidate], acc, gyro, valid, min_parent_s=45, min_run_s=10, margin_s=3,
                 gyro_max_dps=8.0) -> list[Candidate]:
    """Long candidates may contain a urination inside walking / posture noise.
    The longest lifted-and-still run becomes an extra (child) candidate."""
    out = []
    for c in cands:
        out.append(c)
        if c.duration < min_parent_s:
            continue
        idx = np.arange(c.start, c.end + 1)
        disp = np.linalg.norm(acc[idx] - c.ref, axis=1)
        m = (disp >= 0.5 * disp.max()) & (gyro[idx] < gyro_max_dps) & valid[idx]
        d = np.diff(np.r_[0, m.astype(int), 0])
        starts, stops = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        for a, b in zip(stops[:-1], starts[1:]):
            if b - a <= 2:
                m[a:b] = True
        d = np.diff(np.r_[0, m.astype(int), 0])
        starts, stops = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        if not len(starts):
            continue
        i = int(np.argmax(stops - starts))
        run = stops[i] - starts[i]
        if run < min_run_s or run > 0.75 * c.duration:
            continue
        s = max(c.start, c.start + starts[i] - margin_s)
        e = min(c.end, c.start + stops[i] - 1 + margin_s)
        out.append(Candidate(start=int(s), end=int(e), peak_g=float(disp.max()), ref=c.ref, truncated=False, child=True))
    return out


def generate(acc: np.ndarray, valid: np.ndarray, params: dict | None = None, gyro: np.ndarray | None = None) -> list[Candidate]:
    p = {**DEFAULT_PARAMS, **(params or {})}
    n = len(acc)
    gyro = np.zeros(n) if gyro is None else gyro
    out: list[Candidate] = []
    lead = p["ref_s"] + p["ref_gap_s"]
    t, last_end, held = lead, -1, None
    while t < n:
        if t - lead <= last_end and held is None:
            t = last_end + 1 + lead
            continue
        if not valid[t]:
            t += 1
            continue
        if t - lead <= last_end:
            # The tail came back (or settled) after the previous candidate:
            # keep that reference instead of going blind for ``lead`` seconds.
            ref = held
        else:
            held = None
            lo, hi = t - lead, t - p["ref_gap_s"]
            ok = valid[lo:hi]
            if ok.sum() < p["ref_s"] // 2:
                t += 1
                continue
            ref = np.median(acc[lo:hi][ok], axis=0)
        hi = max(t - p["ref_gap_s"], last_end + 1)
        d = float(np.linalg.norm(acc[t] - ref))
        if d < p["on_g"]:
            t += 1
            continue
        s = t
        while s > hi and s > t - p["backtrack_s"] and valid[s - 1] and np.linalg.norm(acc[s - 1] - ref) >= 0.5 * p["on_g"]:
            s -= 1
        peak, k, low, last_high, truncated, shift = d, t, 0, t, False, False
        disp = [d]
        while True:
            if k + 1 >= n or not valid[k + 1] or k + 1 - s >= p["max_s"]:
                truncated = True
                break
            k += 1
            dk = float(np.linalg.norm(acc[k] - ref))
            disp.append(dk)
            peak = max(peak, dk)
            if dk < max(p["release_abs_g"], p["release_rel"] * peak):
                low += 1
                if low >= p["release_s"]:
                    break
            else:
                low = 0
                last_high = k
            if k - s + 1 >= p["settle_after_s"] + p["settle_s"] and low == 0:
                w = np.asarray(disp[-p["settle_s"]:])
                if np.ptp(w) < p["settle_range_g"] and np.median(gyro[k - p["settle_s"] + 1:k + 1]) < p["settle_gyro_dps"]:
                    last_high = k - p["settle_s"]
                    shift = truncated = True
                    break
        end = last_high
        if end - s + 1 >= p["min_s"]:
            out.append(Candidate(start=s, end=end, peak_g=peak, ref=ref, truncated=truncated, shift=shift))
        if shift:
            last_end, held = end, np.median(acc[end + 1:k + 1], axis=0)
        elif truncated:
            last_end, held = max(k, end), None
        else:
            last_end, held = max(k, end), ref
        t = last_end + 1
    return out
