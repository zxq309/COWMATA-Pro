"""Decision-feature plug-in contract (cowmata-decision-feature-1).

Every statistical feature used by the calving decision lives in its own module under
``cowmata_engine/features/<key>.py`` and exposes:

``SPEC``
    A :class:`FeatureSpec` describing the columns it produces.

``extract(source, *, window_ms=WINDOW_MS) -> list[dict]``
    Read ONE raw sensor JSON (Motion / PPG / Temp, whatever ``SPEC.modality`` says) and
    return one row per absolute, epoch-aligned window (see :func:`window_grid`).
    Each row carries the keys in :data:`ROW_KEYS` plus every name in ``SPEC.columns``.
    Values that cannot be computed are ``None`` (never 0 as a placeholder).

``extract_series(sources, *, window_ms=WINDOW_MS) -> list[dict]`` (optional)
    Same output, for features that need continuity across consecutive files of one
    device (posture state, straining context). ``sources`` are sorted by start time and
    belong to one cow/device. When defined, the engine prefers it over ``extract``.

Rules shared by all feature modules:

* Causal: a row may only use samples with epoch <= ``end_epoch_ms + SPEC.lookahead_ms``.
  ``available_epoch_ms`` is the earliest moment the row is known (>= end + lookahead).
* Windows are absolute: ``start_epoch_ms = floor(t / window_ms) * window_ms`` (UTC epoch ms),
  so rows from different modalities of the same cow join exactly.
* No calving labels, ledger or video truth may be read inside ``extract``.
* Per-cow baselines (24 h / 72 h deltas, z-scores, slopes) are added by the engine
  (:mod:`cowmata_engine.decision.dataset`); modules only return window-level values.
* Only numpy / scipy / pandas / sklearn / the existing ``cowmata_tailring`` readers may be
  used, and no Qt imports: the engine must run headless for the external front-end.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

FEATURE_API = "cowmata-decision-feature-1"
WINDOW_MS = 600_000  # 10 min
ROW_KEYS = ("start_epoch_ms", "end_epoch_ms", "available_epoch_ms", "coverage")


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    title: str
    modality: str  # "motion" | "ppg" | "temp"
    version: str
    columns: tuple[str, ...]
    primary: str
    unit: str = ""
    lookahead_ms: int = 0
    expected_change: str = ""  # documented pre-calving direction, e.g. "下降 0.3–0.5 °C"
    column_titles: dict = field(default_factory=dict)
    # Engine trend derivations to use ("1h", "6h", "d24", "z72", "slope6h", "circ"); empty = all.
    derivations: tuple = ()

    def as_dict(self):
        doc = asdict(self)
        doc["columns"] = list(self.columns)
        doc["derivations"] = list(self.derivations)
        doc["api"] = FEATURE_API
        return doc


def window_start(epoch_ms: float, window_ms: int = WINDOW_MS) -> int:
    return int(epoch_ms // window_ms) * window_ms


def window_grid(first_epoch_ms: float, last_epoch_ms: float, window_ms: int = WINDOW_MS):
    """Absolute windows [(start, end), ...] covering the closed span of a record."""
    if last_epoch_ms < first_epoch_ms:
        return []
    start = window_start(first_epoch_ms, window_ms)
    stop = window_start(last_epoch_ms, window_ms)
    return [(t, t + window_ms) for t in range(start, stop + window_ms, window_ms)]


def empty_row(spec: FeatureSpec, start: int, end: int, *, coverage: float = 0.0) -> dict:
    row = dict(start_epoch_ms=int(start), end_epoch_ms=int(end),
               available_epoch_ms=int(end + spec.lookahead_ms), coverage=float(coverage))
    row.update({name: None for name in spec.columns})
    return row


def finite_or_none(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def validate_rows(spec: FeatureSpec, rows) -> list[dict]:
    """Raise ValueError when a module breaks the contract; return rows unchanged."""
    for row in rows:
        missing = [k for k in (*ROW_KEYS, *spec.columns) if k not in row]
        if missing:
            raise ValueError(f"{spec.key}: 缺少输出列 {missing}")
        if row["end_epoch_ms"] <= row["start_epoch_ms"]:
            raise ValueError(f"{spec.key}: 窗口终点必须晚于起点")
        if row["available_epoch_ms"] < row["end_epoch_ms"]:
            raise ValueError(f"{spec.key}: available_epoch_ms 早于窗口终点，违反因果约束")
        for name in spec.columns:
            value = row[name]
            if value is not None and not (isinstance(value, (int, float)) and math.isfinite(value)):
                raise ValueError(f"{spec.key}: {name} 必须是有限数值或 None")
    return rows
