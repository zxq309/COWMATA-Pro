"""PPG pulse-wave decision feature (PPG 脉诊) for the cowmata-decision-feature-1 contract.

Each 90 s capture is analysed by :mod:`cowmata_tailring.algorithms.ppg_pulse`; this module
turns the captures of one cow/device into causal 10 min windows. A window holds the median
of the quality captures (SQI >= 60) that the SERVER had received by the window end within
the last 6 h, so the value is known at ``end_epoch_ms`` and never uses later data.

Only information that the ``heart_rate`` and ``spo2`` plug-ins do not already provide is
exported (no pulse rate, no perfusion index) so the single fused model does not count the
same physiology twice. Rows are emitted for every window from the first capture's sampling
window to the last capture's arrival window; missing values stay ``None``.
"""
from __future__ import annotations

from collections import Counter

import numpy as np

from cowmata_tailring.algorithms.ppg_pulse import VERSION
from cowmata_tailring.algorithms.ppg_pulse import measure_file as read_measurement

from .base import WINDOW_MS, FeatureSpec, empty_row, finite_or_none, window_start

HOUR_MS = 3_600_000
LOOKBACK_MS = 6 * HOUR_MS
EXPECTED_CAPTURES_6H = 5.4

# column -> (title, extractor)
COLUMNS = {
    "ppg_rmssd_ms": ("脉搏间期 RMSSD（迷走张力）ms", lambda m: m.get("rmssd_ms")),
    "ppg_sd1sd2": ("Poincaré SD1/SD2（副交感/交感平衡）", lambda m: m.get("sd1sd2")),
    "ppg_irregularity": ("脉律不齐比例（Malik 20%）", lambda m: m.get("irregularity")),
    "ppg_resp_rate_bpm": ("PPG 推算呼吸频率 次/分", lambda m: m.get("resp_rate_bpm")),
    "ppg_harmonic_c0": ("总谐波灌注 C0（心包经）‰", lambda m: m.get("harmonic_c0")),
    "ppg_c1_liver": ("C1/C0 肝经谐波比", lambda m: (m.get("harmonic_ratio") or [None])[0]),
    "ppg_c3_spleen": ("C3/C0 脾经谐波比", lambda m: (m.get("harmonic_ratio") or [None] * 3)[2]),
    "ppg_harmonic_cv_mid": ("中阶谐波逐搏变异 CV4–6 %", lambda m: m.get("harmonic_cv_mid")),
    "ppg_reflection_index": ("反射指数 RI（外周阻力）", lambda m: m.get("reflection_index")),
    "ppg_sqi": ("PPG 信号质量 SQI", lambda m: m.get("sqi")),
}

SPEC = FeatureSpec(
    key="ppg_pulse",
    title="PPG 脉诊",
    modality="ppg",
    version=VERSION + "+w6h",
    columns=tuple(COLUMNS),
    primary="ppg_rmssd_ms",
    unit="",
    lookahead_ms=0,
    expected_change=("尾动脉脉搏波：产前迷走张力（RMSSD、SD1/SD2）相对本牛基线下降、呼吸频率与谐波逐搏变异升高、"
                     "谐波形态（C1/C3 比）偏离；数值规律见 4.3.7 算法报告（22 次金标准产犊，按牛留一）"),
    column_titles={k: v[0] for k, v in COLUMNS.items()},
    derivations=("1h", "6h", "d24", "z72", "slope6h"),
)


def device_polarity(measurements):
    """Majority optical polarity of one device from its clean captures (the sensor is fixed)."""
    votes = Counter(m.get("polarity") for m in measurements if (m.get("sqi") or 0) >= 80 and m.get("polarity") in (1, -1))
    return votes.most_common(1)[0][0] if votes else None


def rows_from_measurements(measurements, *, window_ms=WINDOW_MS):
    if not measurements:
        return []
    items = sorted(measurements, key=lambda m: (m["available_epoch_ms"], m["sample_epoch_ms"]))
    first = window_start(min(m["sample_epoch_ms"] for m in items), window_ms)
    last = window_start(max(m["available_epoch_ms"] for m in items), window_ms)
    rows = []
    for start in range(first, last + window_ms, window_ms):
        end = start + window_ms
        recent = [m for m in items if end - LOOKBACK_MS < m["available_epoch_ms"] <= end]
        good = [m for m in recent if m.get("quality")]
        row = empty_row(SPEC, start, end, coverage=min(1.0, len(good) / EXPECTED_CAPTURES_6H))
        for name, (_, getter) in COLUMNS.items():
            pool = recent if name == "ppg_sqi" else good
            values = [v for v in (finite_or_none(getter(m)) for m in pool) if v is not None]
            row[name] = float(np.median(values)) if values else None
        row["source"] = (good or recent or items[:1])[-1].get("source", "")
        rows.append(row)
    return rows


def extract_series(sources, *, window_ms=WINDOW_MS):
    measurements = []
    for path in sources:
        try:
            measurements.append(read_measurement(path))
        except (OSError, ValueError, KeyError, TypeError, IndexError, FloatingPointError):
            continue
    polarity = device_polarity(measurements)
    if polarity is not None:
        for i, m in enumerate(measurements):
            if m.get("polarity") != polarity:
                try:
                    measurements[i] = read_measurement(m["source"], polarity=polarity)
                except (OSError, ValueError, KeyError, TypeError, IndexError, FloatingPointError):
                    pass
    return rows_from_measurements(measurements, window_ms=window_ms)


def extract(source, *, window_ms=WINDOW_MS):
    """Single-file view: the capture's own values in its sampling window."""
    m = read_measurement(source)
    return [dict(row, available_epoch_ms=max(row["end_epoch_ms"], int(m["available_epoch_ms"])))
            for row in rows_from_measurements([dict(m, available_epoch_ms=m["sample_epoch_ms"])], window_ms=window_ms)[:1]]