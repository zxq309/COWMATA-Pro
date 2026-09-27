"""Causal SpO2 decision features from red/IR tail PPG (key ``spo2``).

One PPG capture (60–90 s) arrives about every 66 min. :class:`SpO2Module` keeps the
per-binding history of oximetry measurements and, at any evaluation time ``now``,
uses only captures the server had received (``available_epoch_ms <= now``):

* ``spo2_percent``            median SpO2 of quality-passed captures in the last 3 h
* ``spo2_ratio_r``            median ratio-of-ratios R (calibration-free; higher = less saturated)
* ``perfusion_index_percent`` median IR perfusion index (pulsatile AC/DC, %) in the last 3 h
* ``spo2_variability_6h``     IQR of SpO2 over the last 6 h (desaturation episodes widen it)
* ``spo2_desat_share_6h``     share of last-6 h captures ≥2 %-points below the cow's own
                              24 h baseline (t-27 h .. t-3 h, ≥6 captures) — self-baselined

Absolute SpO2 uses the MAX3010x vendor curve and is not calibrated against bovine blood
gas; baselines, z-scores and slopes are derived by the decision engine. No ledger,
label or video truth is read here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

HOUR_MS = 3_600_000
RECENT_MS, SHORT_MS = 3 * HOUR_MS, 6 * HOUR_MS
BASE_FROM_MS, BASE_TO_MS, MIN_BASELINE = 27 * HOUR_MS, 3 * HOUR_MS, 6
DESAT_DROP_PERCENT = 2.0
FEATURE_COLUMNS = (
    "spo2_percent",
    "spo2_ratio_r",
    "perfusion_index_percent",
    "spo2_variability_6h",
    "spo2_desat_share_6h",
)
COLUMN_TITLES = {
    "spo2_percent": "近 3 h 血氧中位数 %（厂商曲线，未经牛血气标定）",
    "spo2_ratio_r": "近 3 h 红光/红外比值 R 中位数",
    "perfusion_index_percent": "近 3 h 灌注指数 PI %（红外脉动 AC/DC）",
    "spo2_variability_6h": "近 6 h 血氧四分位距 %",
    "spo2_desat_share_6h": "近 6 h 低于本牛 24 h 基线 2 个百分点的占比",
}


def _num(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


def measurement_from_analysis(analysis, *, sample_epoch_ms, available_epoch_ms, source="", source_sha256=None,
                              device_id="", time_basis=""):
    ok = analysis.get("spo2_percent") is not None and analysis.get("quality") in ("good", "fair")
    return dict(sample_epoch_ms=int(sample_epoch_ms), available_epoch_ms=int(max(available_epoch_ms, sample_epoch_ms)),
                valid=bool(ok), spo2_percent=_num(analysis.get("spo2_percent")) if ok else None,
                ratio_r=_num(analysis.get("ratio_r")) if ok else None,
                heart_rate_bpm=_num(analysis.get("heart_rate_bpm")),
                perfusion_index_percent=_num(analysis.get("perfusion_index_percent")),
                quality=analysis.get("quality"), windows_valid=analysis.get("windows_valid"),
                windows_total=analysis.get("windows_total"), device_id=device_id,
                source=str(source), source_sha256=source_sha256, time_basis=time_basis)


def measure_document(document, *, source="", source_sha256=None):
    from cowmata_tailring.spo2 import analyse_ppg_document, ppg_timing
    sample, available, basis = ppg_timing(document)
    ppg, analysis = analyse_ppg_document(document, source)
    return measurement_from_analysis(analysis, sample_epoch_ms=sample,
                                     available_epoch_ms=max(available, sample + int(round(ppg.duration_ms))),
                                     source=source, source_sha256=source_sha256,
                                     device_id=str(document.get("device") or "").upper(), time_basis=basis)


def measure_file(path):
    content = Path(path).read_bytes()
    document = json.loads(content.decode("utf-8-sig"))
    if not isinstance(document, dict) or document.get("imu") or not document.get("ir_data"):
        raise ValueError("不是红光/红外 PPG JSON")
    return measure_document(document, source=str(path), source_sha256=hashlib.sha256(content).hexdigest())


def unusable_measurement(path, reason=""):
    """Timing-only, invalid measurement for a PPG capture whose waveform cannot be analysed."""
    from cowmata_tailring.spo2 import ppg_timing
    try:
        content = Path(path).read_bytes()
        document = json.loads(content.decode("utf-8-sig"))
        if not isinstance(document, dict) or document.get("imu"):
            return None
        sample, available, basis = ppg_timing(document)
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return dict(sample_epoch_ms=int(sample), available_epoch_ms=int(max(available, sample)), valid=False,
                spo2_percent=None, ratio_r=None, heart_rate_bpm=None, perfusion_index_percent=None,
                quality="unusable", windows_valid=0, windows_total=0,
                device_id=str(document.get("device") or "").upper(), source=str(path),
                source_sha256=hashlib.sha256(content).hexdigest(), time_basis=basis, reason=str(reason))


def measurement_from_record(record, *, source=""):
    """A saved ``cowmata-spo2-1`` record (only quality-passed captures are saved)."""
    return dict(sample_epoch_ms=int(record["time"]), available_epoch_ms=int(record["available_at_ms"]),
                valid=True, spo2_percent=_num(record["value"]), ratio_r=_num(record.get("ratio_r")),
                heart_rate_bpm=_num(record.get("heart_rate_bpm")),
                perfusion_index_percent=_num(record.get("perfusion_index_percent")),
                quality=record.get("quality"), source=str(source), source_sha256=record.get("source_sha256"))


def _median(values):
    values = [v for v in values if v is not None]
    return float(np.median(values)) if values else None


class SpO2Module:
    """Chronological per-binding measurements with causal evaluation."""

    def __init__(self, measurements=()):
        unique = {}
        for m in measurements:
            key = (m["sample_epoch_ms"], m.get("source_sha256") or m.get("source"))
            unique.setdefault(key, m)
        self.items = sorted(unique.values(), key=lambda m: m["sample_epoch_ms"])
        self.times = np.array([m["sample_epoch_ms"] for m in self.items], dtype=np.int64)

    def window(self, now, lo_ms, hi_ms=0):
        a = int(np.searchsorted(self.times, now - lo_ms, side="right"))
        b = int(np.searchsorted(self.times, now - hi_ms, side="right"))
        return [m for m in self.items[a:b] if m["available_epoch_ms"] <= now]

    def evaluate(self, now):
        now = int(now)
        recent = self.window(now, RECENT_MS)
        short = self.window(now, SHORT_MS)
        base = [m for m in self.window(now, BASE_FROM_MS, BASE_TO_MS) if m["valid"]]
        rv = [m for m in recent if m["valid"]]
        sv = [m for m in short if m["valid"]]
        out = dict.fromkeys(FEATURE_COLUMNS)
        out["spo2_percent"] = _median([m["spo2_percent"] for m in rv])
        out["spo2_ratio_r"] = _median([m["ratio_r"] for m in rv])
        out["perfusion_index_percent"] = _median([m["perfusion_index_percent"] for m in recent])
        if len(sv) >= 3:
            q75, q25 = np.percentile([m["spo2_percent"] for m in sv], [75, 25])
            out["spo2_variability_6h"] = float(q75 - q25)
        baseline = _median([m["spo2_percent"] for m in base]) if len(base) >= MIN_BASELINE else None
        if baseline is not None and len(sv) >= 2:
            out["spo2_desat_share_6h"] = float(np.mean([m["spo2_percent"] <= baseline - DESAT_DROP_PERCENT for m in sv]))
        known = [m for m in self.items if m["available_epoch_ms"] <= now and m["sample_epoch_ms"] <= now]
        return dict(features=out, spo2_baseline_percent=baseline, valid_captures_6h=len(sv), captures_6h=len(short),
                    latest_sample_epoch_ms=known[-1]["sample_epoch_ms"] if known else None,
                    latest_valid_epoch_ms=rv[-1]["sample_epoch_ms"] if rv else None)


def attach_spo2(rows, ppg_records, *, time_key="decision_epoch_ms", extra_measurements=None):
    """Fill ``spo2_percent`` and SpO2 evidence columns of fusion rows, causally.

    ``extra_measurements``: {(cow, device, mark): [measurement, ...]} from derived SpO2 JSON.
    Returns data issues.
    """
    issues, bindings = [], {}
    for record in ppg_records:
        key = (record["cow_id"], record["device_id"], record.get("field_mark", ""))
        try:
            bindings.setdefault(key, []).append(measure_file(record["raw"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append(dict(path=str(record.get("raw")), reason=f"血氧计算失败：{exc}"))
    for key, items in (extra_measurements or {}).items():
        bindings.setdefault(key, []).extend(items)
    modules = {key: SpO2Module(items) for key, items in bindings.items()}
    for row in rows:
        row.setdefault("spo2_percent", None)
        key = (row.get("cow_id"), row.get("device_id"), row.get("field_mark", ""))
        now = row.get(time_key)
        if key not in modules or now is None:
            continue
        result = modules[key].evaluate(int(now))
        f = result["features"]
        row["spo2_percent"] = f["spo2_percent"]
        row["spo2_ratio_r"] = f["spo2_ratio_r"]
        row["perfusion_index_percent"] = f["perfusion_index_percent"]
        row["spo2_variability_6h"] = f["spo2_variability_6h"]
        row["spo2_desat_share_6h"] = f["spo2_desat_share_6h"]
        row["spo2_valid_captures_6h"] = result["valid_captures_6h"]
    return issues
