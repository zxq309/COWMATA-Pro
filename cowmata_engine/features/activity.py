"""活动量 (activity) decision feature, plug-in ``cowmata-decision-feature-1``.

Window-level activity statistics of the tail-ring 9-axis signal in absolute 10-min windows.
Nothing here reads labels, ledgers or video; per-cow baselines, z-scores and slopes are derived by
the decision engine.

* ``vedba_mean_g``: VeDBA on the 50 Hz grid with a 2 s running-mean gravity estimate, no
  interpolation across gaps > 100 ms. Minute sums equal the 4.3.2 first-version activity engine
  (``assets/dataset_recipes/cowmata_activity_aux``, ``packet_minutes``) to < 1e-6 relative.
* ``activity_index``: the 4.3.2 combined-decision definition (per-second SD norm / mean norm of
  acceleration, ``algorithms/features.second_features``), averaged over valid seconds.
* ``active_frac`` / ``high_frac``: share of valid seconds whose 1 s mean VeDBA >= 0.05 g / 0.20 g.
  0.05 g is the trough between the resting mode (0.013-0.03 g) and movement in 137 k seconds of
  CalvingPred data; 0.20 g is about the 94th percentile.
* ``bouts_per_h``: onsets of runs of >= 3 consecutive active seconds per valid hour (restlessness).
* ``orient_deg_per_h``: path length of the per-second gravity direction (tail posture changes).
* ``device_motion_per_min``: the device's own per-minute ``motion`` count bucket.

Validation (CalvingPred dataset, 46 cows, 2 387 records; ratios to the same cow's -48..-24 h
baseline, median over 25-27 cows): VeDBA +11 % at -24..-12 h, +20 % at -12..-6 h, +43 % at
-6..-2 h (93 % of cows up), +86 % in the last 2 h (100 %); high-intensity seconds +98 % / +156 %;
tail posture change +44 % / +129 %. Single-window AUC for calving within 6 h: 0.64-0.71.
See ``4.3.4/活动量/README.md``.

``available_epoch_ms`` is the server receipt time of the packet that completes the window
(``update_time``; tail rings upload hourly, so median 32 min, P90 60 min after the window end),
or the window end when the receipt time is unknown.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from cowmata_engine.features.base import WINDOW_MS, FeatureSpec, finite_or_none, window_start

GRAVITY_MS2 = 9.80665
FS = 50
GRAVITY_WINDOW = 100
SEGMENT_GAP_MS = 100
ACTIVE_G = 0.05
HIGH_G = 0.20
BOUT_MIN_S = 3
MIN_VALID_S = 60  # a window needs at least one valid minute to report values

SPEC = FeatureSpec(
    key="activity",
    title="活动量",
    modality="motion",
    version="activity-2",
    columns=("vedba_mean_g", "activity_index", "active_frac", "high_frac", "bouts_per_h",
             "orient_deg_per_h", "device_motion_per_min"),
    primary="vedba_mean_g",
    unit="g",
    lookahead_ms=0,
    expected_change=("产前活动量逐步升高（相对本牛产前 24–48 h 基线，25–27 头牛中位数）："
                     "−24~−12 h VeDBA +11%，−12~−6 h +20%，−6~−2 h +43%（93% 的牛升高），"
                     "娩出前 2 h +86%（100% 的牛）；高强度秒占比 −6~−2 h 约翻倍、最后 2 h +156%；"
                     "尾部姿态变化最后 2 h +129%。有明显日节律，需与同一时段比较。"),
    column_titles={
        "vedba_mean_g": "VeDBA 动态加速度均值（g，与首版活动量引擎一致）",
        "activity_index": "活动量指数（4.3.2 综合决策口径）",
        "active_frac": "活动秒占比（1 s VeDBA ≥ 0.05 g）",
        "high_frac": "高强度秒占比（1 s VeDBA ≥ 0.20 g）",
        "bouts_per_h": "活动回合数/小时（连续 ≥3 个活动秒）",
        "orient_deg_per_h": "尾部姿态变化量（°/小时）",
        "device_motion_per_min": "设备端运动计数/分钟",
    },
)

ACCUMULATORS = ("observed_s", "valid_s", "vedba_sum", "vedba_n", "dyn_sum", "dyn_n", "active_s", "high_s",
                "bouts", "orient_deg", "dev_sum", "dev_n")


def _runs(flag):
    if not len(flag):
        return np.empty((0, 2), dtype=int)
    d = np.diff(np.r_[0, flag.astype(np.int8), 0])
    return np.column_stack([np.flatnonzero(d == 1), np.flatnonzero(d == -1)])


def minute_accumulators(path):
    """One raw Motion JSON -> (meta, {minute_start_epoch_ms: accumulators}); never crosses files."""
    from cowmata_tailring.algorithms.features import second_features
    from cowmata_tailring.annotation.data import parse_motion_object

    path = Path(path)
    content = path.read_bytes()
    doc = json.loads(content.decode("utf-8-sig"))
    m = parse_motion_object(doc, source_path=path)
    origin = float(m.epoch_at(0.0))
    t_rel = np.asarray(m.times_ms, dtype=np.float64)
    acc = np.column_stack([m.channels[k] for k in ("ax", "ay", "az")]).astype(np.float64) / GRAVITY_MS2
    gyro = np.column_stack([m.channels[k] for k in ("gx", "gy", "gz")]).astype(np.float64)
    first_min = int((origin + t_rel[0]) // 60000)
    n_min = int((origin + t_rel[-1]) // 60000) - first_min + 1
    a = {k: np.zeros(n_min) for k in ACCUMULATORS}

    sec_list, sv_list, sc_list, gu_list = [], [], [], []
    bounds = np.r_[0, np.flatnonzero(np.diff(t_rel) > SEGMENT_GAP_MS) + 1, len(t_rel)]
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        ts = t_rel[lo:hi]
        if len(ts) < 2:
            continue
        grid = np.arange(ts[0], ts[-1] + .1, 1000.0 / FS)
        vals = np.column_stack([np.interp(grid, ts, acc[lo:hi, k]) for k in range(3)])
        g_epoch = origin + grid
        ix = (g_epoch // 60000).astype(np.int64) - first_min
        a["observed_s"] += np.bincount(ix, minlength=n_min) / FS
        if len(grid) < GRAVITY_WINDOW:
            continue
        cum = np.vstack([np.zeros((1, 3)), np.cumsum(vals, axis=0)])
        grav = (cum[GRAVITY_WINDOW:] - cum[:-GRAVITY_WINDOW]) / GRAVITY_WINDOW
        dyn = np.linalg.norm(vals[GRAVITY_WINDOW - 1:] - grav, axis=1)
        ixv = ix[GRAVITY_WINDOW - 1:]
        a["vedba_sum"] += np.bincount(ixv, weights=dyn, minlength=n_min)
        a["vedba_n"] += np.bincount(ixv, minlength=n_min)
        sec = (g_epoch[GRAVITY_WINDOW - 1:] // 1000).astype(np.int64)
        s0 = sec[0]
        sc = np.bincount(sec - s0)
        keep = np.flatnonzero(sc)
        gu = grav / np.maximum(np.linalg.norm(grav, axis=1, keepdims=True), 1e-9)
        sec_list.append(s0 + keep)
        sc_list.append(sc[keep])
        sv_list.append(np.bincount(sec - s0, weights=dyn)[keep] / sc[keep])
        gu_list.append(np.column_stack([np.bincount(sec - s0, weights=gu[:, k])[keep] / sc[keep] for k in range(3)]))

    if sec_list:
        secs, sv, sc = np.concatenate(sec_list), np.concatenate(sv_list), np.concatenate(sc_list)
        orient = np.concatenate(gu_list)
        order = np.argsort(secs, kind="stable")
        secs, sv, sc, orient = secs[order], sv[order], sc[order], orient[order]
    else:
        secs, sv, sc, orient = np.empty(0, np.int64), np.empty(0), np.empty(0), np.empty((0, 3))
    sok = sc >= 0.8 * FS
    active = sok & (sv >= ACTIVE_G)
    high = sok & (sv >= HIGH_G)
    onsets = []
    if len(secs):
        breaks = np.r_[0, np.flatnonzero(np.diff(secs) != 1) + 1, len(secs)]
        for lo, hi in zip(breaks[:-1], breaks[1:]):
            for r0, r1 in _runs(active[lo:hi]):
                if r1 - r0 >= BOUT_MIN_S:
                    onsets.append(secs[lo + r0])
    onsets = np.asarray(onsets, dtype=np.int64)
    turn = np.full(len(secs), np.nan)
    if len(secs) > 1:
        dot = np.clip(np.sum(orient[1:] * orient[:-1], axis=1), -1, 1)
        turn[1:] = np.where(np.diff(secs) == 1, np.degrees(np.arccos(dot)), np.nan)

    def per_min(sec_values, weights=None):
        out = np.zeros(n_min)
        if len(sec_values):
            ix = (sec_values * 1000 // 60000).astype(np.int64) - first_min
            ok = (ix >= 0) & (ix < n_min)
            out += np.bincount(ix[ok], weights=None if weights is None else weights[ok], minlength=n_min)
        return out

    a["valid_s"] = per_min(secs[sok])
    a["active_s"] = per_min(secs[active])
    a["high_s"] = per_min(secs[high])
    a["bouts"] = per_min(onsets)
    tok = np.isfinite(turn) & sok
    a["orient_deg"] = per_min(secs[tok], turn[tok])

    sample_valid = (np.linalg.norm(acc, axis=1) > .01) & (np.max(np.abs(gyro), axis=1) < 1023.5)
    f = second_features(t_rel, acc, gyro, valid_samples=sample_valid)
    dyn_s = np.asarray(f["dynamic"], dtype=np.float64)
    dvalid = np.asarray(f["valid"], dtype=bool) & np.isfinite(dyn_s)
    dix = ((origin + np.asarray(f["seconds"]) * 1000) // 60000).astype(np.int64) - first_min
    ok = dvalid & (dix >= 0) & (dix < n_min)
    a["dyn_sum"] = np.bincount(dix[ok], weights=dyn_s[ok], minlength=n_min)
    a["dyn_n"] = np.bincount(dix[ok], minlength=n_min).astype(float)

    mot = m.channels.get("motion")
    if mot is not None and len(mot):
        mix = ((origin + np.asarray(m.motion_times_ms)) // 60000).astype(np.int64) - first_min
        good = (mix >= 0) & (mix < n_min)
        a["dev_sum"] = np.bincount(mix[good], weights=np.asarray(mot, float)[good], minlength=n_min)
        a["dev_n"] = np.bincount(mix[good], minlength=n_min).astype(float)

    minutes = {(first_min + i) * 60000: {k: float(a[k][i]) for k in ACCUMULATORS}
               for i in range(n_min) if a["observed_s"][i] > 0}
    meta = dict(source=str(path), sha256=hashlib.sha256(content).hexdigest(),
                received_epoch_ms=m.update_time_ms, sample_start_epoch_ms=origin + float(t_rel[0]),
                sample_end_epoch_ms=origin + float(t_rel[-1]))
    return meta, minutes


def _window_row(start, end, acc, available):
    seconds = (end - start) / 1000
    row = dict(start_epoch_ms=int(start), end_epoch_ms=int(end), available_epoch_ms=int(max(end, available)),
               coverage=float(min(1.0, acc["vedba_n"] / (seconds * FS))))
    row.update({name: None for name in SPEC.columns})
    if acc["valid_s"] >= MIN_VALID_S and acc["vedba_n"] > 0:
        valid_h = acc["valid_s"] / 3600
        row.update(
            vedba_mean_g=finite_or_none(acc["vedba_sum"] / acc["vedba_n"]),
            activity_index=finite_or_none(acc["dyn_sum"] / acc["dyn_n"]) if acc["dyn_n"] else None,
            active_frac=finite_or_none(acc["active_s"] / acc["valid_s"]),
            high_frac=finite_or_none(acc["high_s"] / acc["valid_s"]),
            bouts_per_h=finite_or_none(acc["bouts"] / valid_h),
            orient_deg_per_h=finite_or_none(acc["orient_deg"] / valid_h),
            device_motion_per_min=finite_or_none(acc["dev_sum"] / acc["dev_n"]) if acc["dev_n"] else None)
    return row


def windows_from_minutes(parts, window_ms=WINDOW_MS):
    """parts: [(meta, minutes)] of one cow/device -> rows; overlapping minutes are summed."""
    windows = {}
    for meta, minutes in parts:
        received = meta.get("received_epoch_ms")
        for minute, acc in minutes.items():
            start = window_start(minute, window_ms)
            slot = windows.setdefault(start, dict({k: 0.0 for k in ACCUMULATORS}, _available=0, _source=meta["source"]))
            for k in ACCUMULATORS:
                slot[k] += acc[k]
            end = start + window_ms
            slot["_available"] = max(slot["_available"], received if received else end)
    rows = []
    for start in sorted(windows):
        slot = windows[start]
        row = _window_row(start, start + window_ms, slot, slot["_available"])
        row["source"] = slot["_source"]
        rows.append(row)
    return rows


def extract_series(sources, *, window_ms=WINDOW_MS):
    """Rows for one cow/device; windows split across consecutive packets are merged."""
    return windows_from_minutes([minute_accumulators(p) for p in sources], window_ms)


def extract(source, *, window_ms=WINDOW_MS):
    return extract_series([source], window_ms=window_ms)
