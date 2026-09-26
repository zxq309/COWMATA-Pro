"""Heart-rate decision feature (心率) for the cowmata-decision-feature-1 contract.

PPG arrives as one 90 s capture about every 66 min. ``extract_series`` replays the
causal :class:`HeartRateModule` of one cow/device and emits one row for EVERY 10 min
window from the first capture's sampling window to the window in which the last
capture reached the server, gaps included. Each row holds the state at the window end
(only captures the server had received by then); values that cannot be computed are
``None`` and ``coverage`` is the share of expected quality captures in the last 6 h,
so a missing value never disappears from the table.
"""
from __future__ import annotations

from pathlib import Path

from cowmata_tailring.algorithms.heart_rate import (
    COLUMN_TITLES,
    VERSION,
    Config,
    HeartRateModule,
)
from cowmata_tailring.algorithms.heart_rate import measure_file as read_measurement

from .base import WINDOW_MS, FeatureSpec, empty_row, finite_or_none, window_start

# Columns with verified pre-calving signal or that describe PPG quality
# (baseline level, robust z and RMSSD ratio stay in the algorithm module only).
DECISION_COLUMNS = (
    "heart_rate_bpm",
    "hr_level_6h_bpm",
    "hr_rise_bpm",
    "hr_instability_bpm",
    "hr_slope_24h_bpm_per_h",
    "ppg_pulse_quality_6h",
    "ppg_pulse_quality_drop",
)

SPEC = FeatureSpec(
    key="heart_rate",
    title="心率",
    modality="ppg",
    version=VERSION + "+w2",
    columns=DECISION_COLUMNS,
    primary="hr_level_6h_bpm",
    unit="bpm",
    lookahead_ms=0,
    expected_change=("去日节律 6 h 心率较本牛 24–96 h 前基线（hr_rise_bpm，116 头有结局牛中位数）：产前 72–36 h 约 +1.5~2 bpm，"
                     "最后 24–12 h 约 +3 bpm，最后 12 h 约 +4 bpm；6 h 心率离散度 5.7→8.8 bpm，合格脉搏波比例 0.67→0.56"),
    column_titles={k: COLUMN_TITLES[k] for k in DECISION_COLUMNS},
    # The module already removes the daily rhythm, so the engine's circ residual is omitted.
    derivations=("1h", "6h", "d24", "z72", "slope6h"),
)
# Expected quality captures in 6 h at the 66 min cadence; used only to express coverage.
EXPECTED_CAPTURES_6H = 5.4


def rows_from_measurements(measurements, *, history=(), cow_id="", device_id="", config: Config | None = None,
                           window_ms=WINDOW_MS, relative_to=None):
    """Window rows covering the whole span of ``measurements`` (one binding).

    ``history``: earlier captures of the same binding used only as causal context
    (baseline); they do not extend the emitted span.
    """
    if not measurements:
        return []
    device = device_id or measurements[0].get("device_id") or "UNKNOWN"
    module = HeartRateModule(cow_id or "UNKNOWN", device, config=config)
    pending = sorted([*history, *measurements], key=lambda m: (m["available_epoch_ms"], m["sample_epoch_ms"]))
    first = window_start(min(m["sample_epoch_ms"] for m in measurements), window_ms)
    last = window_start(max(m["available_epoch_ms"] for m in measurements), window_ms)
    rows, i, latest_source = [], 0, ""
    for start in range(first, last + 1, window_ms):
        end = start + window_ms
        while i < len(pending) and pending[i]["available_epoch_ms"] <= end:
            module.add_measurement(pending[i])
            latest_source = pending[i].get("source") or latest_source
            i += 1
        result = module.evaluate(end)
        features = result["features"]
        row = empty_row(SPEC, start, end, coverage=min(1.0, result["quality_records_6h"] / EXPECTED_CAPTURES_6H))
        row.update({name: finite_or_none(features.get(name)) for name in DECISION_COLUMNS})
        row["heart_rate_quality_status"] = result["quality_status"]
        row["heart_rate_evidence_level"] = result["heart_rate_evidence_level"]
        source = latest_source or measurements[0].get("source") or ""
        if relative_to and source:
            try:
                source = Path(source).relative_to(relative_to).as_posix()
            except ValueError:
                pass
        row["source"] = source
        rows.append(row)
    return rows


def extract_series(sources, *, window_ms=WINDOW_MS):
    measurements = []
    for path in sources:
        try:
            measurements.append(read_measurement(path))
        except (OSError, ValueError, KeyError, TypeError):
            continue  # one unreadable capture must not remove the rest of the series
    return rows_from_measurements(measurements, window_ms=window_ms)


def extract(source, *, window_ms=WINDOW_MS):
    """Single-file view: the capture's own pulse rate in its sampling window."""
    m = read_measurement(source)
    start = window_start(m["sample_epoch_ms"], window_ms)
    row = empty_row(SPEC, start, start + window_ms, coverage=1.0 if m["quality"] else 0.0)
    row["available_epoch_ms"] = max(row["end_epoch_ms"], int(m["available_epoch_ms"]))
    row["heart_rate_bpm"] = finite_or_none(m["heart_rate_bpm"]) if m["quality"] else None
    row["ppg_pulse_quality_6h"] = 1.0 if m["quality"] else 0.0
    return [row]
