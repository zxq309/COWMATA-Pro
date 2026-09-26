"""Angular-velocity (gyroscope) spectral entropy — calving decision feature ``gse-1``.

Origin: 牛尾环产犊预测第二轮复核报告 (2026-09-16) §4 — "最后 1 小时角速度频谱熵 12/13 头下降".
The computation below is the locked recipe re-validated on 扬大_高邮牧场 (see
``4.3.3/角速度频谱熵/角速度频谱熵_规律与算法.md``).

Per absolute UTC minute
    * one contiguous gap-free run of >= 50 s at ~50 Hz (no interpolation across gaps,
      saturated gyro / zero-acceleration frames are gaps; 6.3 Hz records are rejected
      because the 0.1–5 Hz band exceeds their Nyquist frequency);
    * Welch PSD of each gyro axis (10 s Hann, 50 % overlap, mean removed), summed over
      x/y/z. The trace of the spectral matrix is invariant to how the ring sits on the tail;
    * ``H = -sum(p log p) / log K`` over the K bins in 0.1–5 Hz, p = normalised power;
    * band fractions 0.1–0.5 / 0.5–2 / 2–5 Hz and total 0.1–5 Hz power.

Per 10 min window (absolute grid) the minute values are summarised by their median, using only
minutes whose rotation power is above the sensor-noise floor: an almost motionless (or
unworn) tail gives a flat white-noise spectrum with entropy close to 1 that says nothing about
behaviour. Band fractions are power-weighted over the same minutes.

Only four validated, non-redundant columns are exported (column audit ``lab/s09_columns.py``,
leave-cow-out on 116 cows). Rotation power carries most of the decision value (held-out AUC drop
0.26 when permuted, 2 h task) and stands in for activity on cows without an activity table
(Spearman 0.92 with ``vedba_mean_g`` where both exist). The 0.5–2 Hz fraction (exactly
1 − low − high), the active-minute fraction and the valid-minute count (= 10 × coverage) add
nothing and stay in the row only as diagnostics (``DIAGNOSTICS``). The engine's ``d24`` trend is
not requested: removing it did not lower any held-out AUC.

The value of a window is known once the packet containing its last sample has reached the
server, so ``available_epoch_ms = max(window end, packet update_time)``.
No labels, ledger or video are read here.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .base import WINDOW_MS, FeatureSpec, empty_row, finite_or_none, window_start

MINUTE_MS = 60_000
BAND = (0.1, 5.0)
SUB_BANDS = {"low": (0.1, 0.5), "mid": (0.5, 2.0), "high": (2.0, 5.0)}
WELCH_SECONDS = 10.0
MIN_RUN_S = 50.0
MIN_RATE_HZ = 40.0
MAX_RATE_HZ = 60.0
GAP_MS = 100.0
SATURATION_DPS = 1023.5
# Summed-axis 0.1–5 Hz rotation power (dps^2) below which a minute is treated as "tail still".
# 372 076 扬大 minutes: sensor-noise mode at 10^-2.5, empty gap around 10^-1.5, behaviour mode
# from 10^-0.5 upwards; the threshold sits in the gap and was fixed before any calving analysis.
ACTIVE_POWER_DPS2 = 0.1
MIN_ACTIVE_MINUTES = 3

SPEC = FeatureSpec(
    key="gyro_spectral_entropy",
    title="角速度频谱熵",
    modality="motion",
    version="gse-1",
    columns=(
        "gyro_spectral_entropy",
        "gyro_band_low_frac",
        "gyro_band_high_frac",
        "gyro_power_log10",
    ),
    primary="gyro_spectral_entropy",
    unit="0–1（归一化香农熵）",
    lookahead_ms=0,
    expected_change="胎儿首见前最后 1–2 h：频谱熵下降（全部 116 头 73/96 头，中位 −0.027；视频定时牛 12/20），"
                    "0.1–0.5 Hz 慢转动占比上升（视频牛 20/20，中位 +0.074）、2–5 Hz 占比下降（15/20）；"
                    "转动功率约 −12 h 起上升，最后 3 h 约为 −24~−12 h 的 2.6 倍（20/22）；"
                    "频谱形状提前 3 h 以上无稳定变化。晚期确认信号，须与活动量、温降联合",
    column_titles={
        "gyro_spectral_entropy": "角速度频谱熵（活动分钟中位数）",
        "gyro_band_low_frac": "0.1–0.5 Hz 慢转动能量占比",
        "gyro_band_high_frac": "2–5 Hz 快转动能量占比",
        "gyro_power_log10": "0.1–5 Hz 转动功率 log10(dps²)",
    },
    derivations=("1h", "6h", "z72", "slope6h", "circ"),
)
DIAGNOSTICS = ("gyro_band_mid_frac", "gyro_active_fraction", "gyro_valid_minutes")

MINUTE_FIELDS = ("minute_epoch_ms", "entropy", "power", "low", "mid", "high", "peak_hz", "rate_hz")


def _welch_axis_sum(block, fs):
    from scipy.signal import welch

    nperseg = int(round(WELCH_SECONDS * fs))
    freqs, psd = welch(block, fs=fs, window="hann", nperseg=nperseg, noverlap=nperseg // 2,
                       detrend="constant", axis=0)
    return freqs, psd.sum(axis=1)


def spectrum_features(gyro_dps, fs):
    """Entropy/band features of one contiguous (n, 3) gyro block; None if too short."""
    gyro = np.asarray(gyro_dps, dtype=float)
    if gyro.ndim != 2 or gyro.shape[1] != 3 or len(gyro) < WELCH_SECONDS * fs:
        return None
    freqs, psd = _welch_axis_sum(gyro, fs)
    df = float(freqs[1] - freqs[0])
    band = (freqs >= BAND[0] - 1e-9) & (freqs <= BAND[1] + 1e-9)
    power = psd[band]
    total = float(power.sum())
    if not np.isfinite(total) or total <= 0:
        return None
    p = power / total
    nz = p[p > 0]
    entropy = float(-(nz * np.log(nz)).sum() / np.log(len(p)))
    out = dict(entropy=entropy, power=total * df, peak_hz=float(freqs[band][int(np.argmax(power))]))
    for name, (lo, hi) in SUB_BANDS.items():
        mask = (freqs[band] >= lo - 1e-9) & (freqs[band] < hi - 1e-9 if name != "high" else freqs[band] <= hi + 1e-9)
        out[name] = float(power[mask].sum() / total)
    return out


def _runs(times_ms, valid):
    """Contiguous index ranges [a, b) of valid samples without gaps > GAP_MS."""
    t = np.asarray(times_ms, dtype=float)
    breaks = np.flatnonzero((np.diff(t) > GAP_MS) | ~valid[1:] | ~valid[:-1]) + 1
    edges = np.r_[0, breaks, len(t)]
    return [(int(a), int(b)) for a, b in zip(edges[:-1], edges[1:]) if b - a > 1 and valid[a:b].all()]


def minute_table(path):
    """Decode one Motion JSON → (minutes array [n, len(MINUTE_FIELDS)], meta)."""
    from cowmata_tailring.annotation.data import GRAVITY_MS2, parse_motion_object

    path = Path(path)
    doc = json.loads(path.read_bytes().decode("utf-8-sig"))
    motion = parse_motion_object(doc, source_path=path)
    t = np.asarray(motion.times_ms, dtype=float)
    gyro = np.column_stack([motion.channels[k] for k in ("gx", "gy", "gz")]).astype(float)
    acc = np.column_stack([motion.channels[k] for k in ("ax", "ay", "az")]).astype(float) / GRAVITY_MS2
    valid = (np.isfinite(gyro).all(axis=1) & np.isfinite(acc).all(axis=1)
             & (np.linalg.norm(acc, axis=1) > 0.01) & (np.max(np.abs(gyro), axis=1) < SATURATION_DPS))
    epoch = motion.epoch_at(0.0) + t
    meta = dict(source=str(path), device=motion.device, rate_hz=float(motion.sample_rate_hz),
                first_epoch_ms=float(epoch[0]) if len(epoch) else None,
                last_epoch_ms=float(epoch[-1]) if len(epoch) else None,
                update_time_ms=motion.update_time_ms, samples=int(len(t)))
    rows = []
    if len(t) < 2:
        return np.empty((0, len(MINUTE_FIELDS))), meta
    for a, b in _runs(t, valid):
        dt = np.diff(t[a:b])
        fs = 1000.0 / float(np.median(dt))
        if not MIN_RATE_HZ <= fs <= MAX_RATE_HZ:
            continue
        minute = np.floor(epoch[a:b] / MINUTE_MS).astype(np.int64)
        for m in np.unique(minute):
            idx = np.flatnonzero(minute == m) + a
            if (t[idx[-1]] - t[idx[0]]) / 1000.0 < MIN_RUN_S:
                continue
            feats = spectrum_features(gyro[idx], fs)
            if feats is None:
                continue
            rows.append([m * MINUTE_MS, feats["entropy"], feats["power"], feats["low"], feats["mid"],
                         feats["high"], feats["peak_hz"], fs])
    return (np.asarray(rows, dtype=float) if rows else np.empty((0, len(MINUTE_FIELDS)))), meta


def _window_row(start, minutes, available):
    row = empty_row(SPEC, start, start + WINDOW_MS)
    row["available_epoch_ms"] = int(max(start + WINDOW_MS + SPEC.lookahead_ms, available or 0))
    n = len(minutes)
    row["coverage"] = min(1.0, n / (WINDOW_MS / MINUTE_MS))
    row.update({name: None for name in DIAGNOSTICS})
    row["gyro_valid_minutes"] = float(n)
    if not n:
        return row
    m = np.asarray(minutes, dtype=float)
    power = m[:, 2]
    active = power >= ACTIVE_POWER_DPS2
    row["gyro_power_log10"] = finite_or_none(np.log10(np.median(power) + 1e-6))
    row["gyro_active_fraction"] = float(active.mean())
    if active.sum() < MIN_ACTIVE_MINUTES:
        return row  # still / unworn tail: spectrum is sensor noise, keep shape columns missing
    m = m[active]
    row["gyro_spectral_entropy"] = finite_or_none(np.median(m[:, 1]))
    weights = m[:, 2] / max(float(m[:, 2].sum()), 1e-12)
    for column, j in (("gyro_band_low_frac", 3), ("gyro_band_mid_frac", 4), ("gyro_band_high_frac", 5)):
        row[column] = finite_or_none(np.sum(weights * m[:, j]))
    return row


def windows_from_minutes(minutes, available_by_minute=None, *, window_ms=WINDOW_MS):
    """Aggregate a minute table (possibly from several files) into absolute windows."""
    if window_ms != WINDOW_MS:
        raise ValueError("gse-1 只支持 10 分钟窗口")
    minutes = np.asarray(minutes, dtype=float).reshape(-1, len(MINUTE_FIELDS))
    if not len(minutes):
        return []
    order = np.argsort(minutes[:, 0], kind="stable")
    minutes = minutes[order]
    available = (np.asarray(available_by_minute, dtype=float)[order] if available_by_minute is not None
                 else np.zeros(len(minutes)))
    # A minute seen twice (overlapping uploads) keeps its first decoded copy.
    _, first = np.unique(minutes[:, 0], return_index=True)
    minutes, available = minutes[first], available[first]
    starts = (minutes[:, 0] // window_ms * window_ms).astype(np.int64)
    unique, first = np.unique(starts, return_index=True)
    bounds = np.r_[first, len(starts)]
    return [_window_row(int(start), minutes[a:b], float(np.max(available[a:b])))
            for start, a, b in zip(unique, bounds[:-1], bounds[1:])]


def extract(source, *, window_ms=WINDOW_MS):
    minutes, meta = minute_table(source)
    rows = windows_from_minutes(minutes, np.full(len(minutes), meta.get("update_time_ms") or 0.0),
                                window_ms=window_ms)
    if not rows and meta.get("first_epoch_ms") is not None:
        start = window_start(meta["first_epoch_ms"], window_ms)
        rows = [_window_row(start, [], meta.get("update_time_ms"))]
    for row in rows:
        row["source"] = str(source)
    return rows


def extract_series(sources, *, window_ms=WINDOW_MS):
    """Consecutive files of one cow/device: windows straddling two uploads are merged."""
    tables, available, origin = [], [], []
    for source in sources:
        try:
            minutes, meta = minute_table(source)
        except (ValueError, KeyError, TypeError, OSError):
            continue  # undecodable file = missing data, never zeros
        tables.append(minutes)
        available.append(np.full(len(minutes), meta.get("update_time_ms") or 0.0))
        origin.extend([str(source)] * len(minutes))
    if not tables:
        return []
    minutes = np.vstack(tables)
    rows = windows_from_minutes(minutes, np.concatenate(available), window_ms=window_ms)
    starts = (minutes[:, 0] // window_ms * window_ms).astype(np.int64)
    first_source = {}
    for start, source in zip(starts, origin):
        first_source.setdefault(int(start), source)
    for row in rows:
        row["source"] = first_source.get(row["start_epoch_ms"], "")
    return rows
