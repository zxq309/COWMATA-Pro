"""SpO2 decision feature (血氧) for the cowmata-decision-feature-1 contract.

PPG arrives as one capture about every 66 min, so ``extract_series`` replays the causal
:class:`~cowmata_tailring.algorithms.spo2_features.SpO2Module` of one cow/device and
emits, for each 10 min window, the state known at the window start (only captures the
server had received before that window began).

Every window from the first capture's sampling window to the last capture's arrival
window is emitted, gaps included: values that cannot be computed are ``None`` and
``coverage`` is the share of expected captures in the last 6 h that passed the gate, so
missing data never disappears from the table (missing-is-leakage protection).
"""
from __future__ import annotations

from cowmata_tailring.algorithms.spo2_features import (
    COLUMN_TITLES,
    FEATURE_COLUMNS,
    SpO2Module,
    unusable_measurement,
)
from cowmata_tailring.algorithms.spo2_features import measure_file as read_measurement
from cowmata_tailring.algorithms.spo2_signal import VERSION

from .base import WINDOW_MS, FeatureSpec, empty_row, finite_or_none, window_start

SPEC = FeatureSpec(
    key="spo2",
    title="血氧",
    modality="ppg",
    version=VERSION,
    columns=FEATURE_COLUMNS,
    primary="perfusion_index_percent",
    unit="%",
    lookahead_ms=0,
    expected_change=("血氧本身产前无显著变化（中位 99.6 %，63 % 的记录 ≥99 %，天花板效应；文献动脉 SaO2 96.9→96.7 %）；"
                     "灌注指数 PI 较本牛 −96~−36 h 基线在 −48~−24 h 升高约 12 %（66 % 的牛升高，Wilcoxon p=1e-4），"
                     "−24~−12 h 约 +14 %（p=0.008），此后维持高位、产后回落；R 比值略降（不显著）"),
    column_titles=dict(COLUMN_TITLES),
)
# Expected captures in 6 h at the 66 min cadence; used only to express coverage.
EXPECTED_CAPTURES_6H = 5.4


def rows_from_measurements(measurements, *, window_ms=WINDOW_MS):
    """Continuous window rows of one binding (first sampling window .. last arrival window)."""
    if not measurements:
        return []
    module = SpO2Module(measurements)
    first = window_start(min(m["sample_epoch_ms"] for m in module.items), window_ms)
    last = window_start(max(m["available_epoch_ms"] for m in module.items), window_ms)
    rows = []
    for start in range(first, last + window_ms, window_ms):
        result = module.evaluate(start)
        row = empty_row(SPEC, start, start + window_ms,
                        coverage=min(1.0, result["valid_captures_6h"] / EXPECTED_CAPTURES_6H))
        row.update({name: finite_or_none(result["features"][name]) for name in FEATURE_COLUMNS})
        rows.append(row)
    return rows


def extract_series(sources, *, window_ms=WINDOW_MS):
    measurements = []
    for path in sources:
        try:
            measurements.append(read_measurement(path))
        except (OSError, ValueError, KeyError, TypeError):
            # keep the attempt on the time axis when only the waveform is unusable
            fallback = unusable_measurement(path)
            if fallback is not None:
                measurements.append(fallback)
    rows = rows_from_measurements(measurements, window_ms=window_ms)
    source = str(sources[0]) if sources else ""
    for row in rows:
        row["source"] = source
    return rows


def extract(source, *, window_ms=WINDOW_MS):
    """Single-file view: the capture's own oximetry in its sampling window."""
    m = read_measurement(source)
    start = window_start(m["sample_epoch_ms"], window_ms)
    row = empty_row(SPEC, start, start + window_ms, coverage=1.0 if m["valid"] else 0.0)
    row["available_epoch_ms"] = max(row["end_epoch_ms"], int(m["available_epoch_ms"]))
    row["spo2_percent"] = m["spo2_percent"]
    row["spo2_ratio_r"] = m["ratio_r"]
    row["perfusion_index_percent"] = m["perfusion_index_percent"]
    return [row]
