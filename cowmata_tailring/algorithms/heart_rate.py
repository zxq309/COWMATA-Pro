"""Causal heart-rate decision features for calving prediction (tail-base PPG).

One PPG record (90 s, 100 Hz, red + IR) arrives roughly every 66 min. Each record
is turned into one pulse-rate measurement by :mod:`heart_rate_signal`; this module
keeps a per-binding history and, at any evaluation time, derives features that use
only measurements already received by the server at that time.

Measured patterns on 扬大_高邮牧场 (43 631 records, 303 cows, 175 calvings with
>=10 records in the last 48 h; see docs/heart-rate-algorithm.md):

* resting pulse rate (quality records) median 82.6 bpm, IQR 74–91 bpm;
* daily rhythm within cow: trough about 12:00 (−4.4 bpm), plateau 17:00–23:00
  (+3 bpm), amplitude ~7.7 bpm; it is removed before comparing with the baseline;
* against the cow's own 7–4 day baseline HR starts rising ~84–72 h before calving
  (+1.7 bpm), is +3 bpm from 72 h and +4.4 bpm in the last 24 h;
* PPG pulse quality falls in the last 12 h (restlessness, tail raising) and the
  6 h HR spread increases; both are supporting evidence, not alarms by themselves.

No calving label, ledger or video truth is read here. Output levels are evidence
for fusion; ``calving_probability`` is always ``None``.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .heart_rate_signal import VERSION as SIGNAL_VERSION
from .heart_rate_signal import EstimatorConfig, estimate_document

VERSION = "heart-rate-decision-1.0.0"
STATE_VERSION = 1
HOUR_MS = 3_600_000
CHINA_OFFSET_H = 8
QUALITY_GRADES = frozenset({"HIGH", "MEDIUM"})
# Within-cow mean deviation (bpm) by Beijing hour, records >48 h before calving.
CIRCADIAN_BPM = (2.2, 0.9, 1.0, -0.6, -1.5, -1.8, -2.1, -1.4, -2.3, -3.7, -3.3, -3.8,
                 -4.4, -4.0, -1.1, -0.9, 1.0, 2.9, 2.9, 3.3, 3.3, 3.0, 3.3, 2.7)
FEATURE_COLUMNS = (
    "heart_rate_bpm",
    "hr_level_6h_bpm",
    "hr_baseline_bpm",
    "hr_rise_bpm",
    "hr_robust_z",
    "hr_instability_bpm",
    "hr_slope_24h_bpm_per_h",
    "ppg_pulse_quality_6h",
    "ppg_pulse_quality_drop",
    "prv_rmssd_ratio",
)
COLUMN_TITLES = {
    "heart_rate_bpm": "最近一次合格心率 bpm",
    "hr_level_6h_bpm": "近 6 h 心率中位数（去日节律）bpm",
    "hr_baseline_bpm": "本牛基线（24–96 h 前，去日节律）bpm",
    "hr_rise_bpm": "心率较本牛基线上升 bpm",
    "hr_robust_z": "心率上升稳健 z 分数",
    "hr_instability_bpm": "近 6 h 心率离散度 bpm",
    "hr_slope_24h_bpm_per_h": "近 24 h 心率斜率 bpm/h",
    "ppg_pulse_quality_6h": "近 6 h 合格脉搏波比例",
    "ppg_pulse_quality_drop": "合格脉搏波比例较前 72 h 变化",
    "prv_rmssd_ratio": "脉率变异 RMSSD 相对基线",
}


def _stamp(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value <= 0:
        raise ValueError(f"{name} 必须是 Unix 毫秒整数")
    return int(value)


def measurement_timing(document, *, maximum_upload_lag_ms=3 * HOUR_MS):
    """Sampling start and server availability of one PPG JSON.

    Firmware with ``work_mode`` 6 records ``time`` at the start of the 90 s capture
    and uploads the file ~62 min later (``create_time``). Older files carry
    ``time == create_time``; then the capture time is only bounded by the upload.
    """
    create = _stamp(document.get("create_time"), "create_time")
    update = document.get("update_time")
    available = max(create, int(update)) if isinstance(update, (int, np.integer)) and update > 0 else create
    device_time = document.get("time")
    if (isinstance(device_time, (int, np.integer)) and not isinstance(device_time, bool)
            and 0 < create - device_time <= maximum_upload_lag_ms):
        return int(device_time), available, "device_time"
    return create, available, "create_time_upper_bound"


def measure_document(document, *, source="", source_sha256=None, config: EstimatorConfig | None = None):
    """One JSON-friendly heart-rate measurement from one PPG document."""
    sample, available, basis = measurement_timing(document)
    estimate = estimate_document(document, config)
    estimate.pop("config", None)
    uid = document.get("uid")
    return dict(schema=VERSION, signal_version=SIGNAL_VERSION, source=str(source),
                source_sha256=source_sha256, device_id=str(document.get("device") or "").upper(),
                uid=None if uid is None else str(uid), sample_epoch_ms=sample,
                available_epoch_ms=max(available, sample + int(1000 * (estimate.get("duration_s") or 0))),
                time_basis=basis, quality=estimate["grade"] in QUALITY_GRADES, **estimate)


def circadian_offset(epoch_ms):
    return CIRCADIAN_BPM[int((epoch_ms / HOUR_MS + CHINA_OFFSET_H) % 24)]


@dataclass(frozen=True)
class Config:
    history_hours: float = 120.0
    level_hours: float = 6.0
    baseline_start_hours: float = 96.0
    baseline_end_hours: float = 24.0
    minimum_level_samples: int = 2
    minimum_baseline_samples: int = 10
    provisional_baseline_samples: int = 6
    minimum_history_hours: float = 48.0
    minimum_instability_samples: int = 3
    slope_hours: float = 24.0
    minimum_slope_samples: int = 8
    quality_reference_hours: float = 72.0
    minimum_quality_reference_records: int = 10
    stale_minutes: float = 180.0
    latest_hold_minutes: float = 90.0
    robust_scale_floor_bpm: float = 3.0
    elevated_rise_bpm: float = 8.0
    strong_rise_bpm: float = 8.0
    strong_instability_bpm: float = 6.0
    strong_quality_drop: float = -0.15
    circadian_correction: bool = True
    # 24 hourly offsets (Beijing hour, bpm); None = CIRCADIAN_BPM. Datasets pass a
    # leave-one-cow-out profile so no cow is corrected with a table fitted on itself.
    circadian_bpm: tuple | None = None
    uid_retention_count: int = 4096

    def __post_init__(self):
        if self.circadian_bpm is not None:
            profile = tuple(float(v) for v in self.circadian_bpm)
            if len(profile) != 24 or not all(math.isfinite(v) for v in profile):
                raise ValueError("日节律校正表必须是 24 个有限数值")
            object.__setattr__(self, "circadian_bpm", profile)
        if not 0 < self.baseline_end_hours < self.baseline_start_hours <= self.history_hours:
            raise ValueError("基线窗口必须位于保留历史之内")
        if self.provisional_baseline_samples > self.minimum_baseline_samples:
            raise ValueError("临时基线样本数不能大于正式基线样本数")
        if self.strong_quality_drop >= 0:
            raise ValueError("强证据的脉搏波质量变化阈值必须为负")


def _theil_sen(x, y):
    if len(x) < 2:
        return None
    dx = x[:, None] - x[None, :]
    dy = y[:, None] - y[None, :]
    upper = np.triu(dx != 0, 1)
    slopes = dy[upper] / dx[upper]
    return float(np.median(slopes)) if len(slopes) else None


def _r(value, digits=4):
    return None if value is None or not math.isfinite(value) else round(float(value), digits)


class HeartRateModule:
    """One instance per cow/device/pregnancy binding. Calls must be serialised."""

    def __init__(self, cow_id, device_id, binding_id=None, config: Config | None = None):
        if not str(cow_id or "").strip() or not str(device_id or "").strip():
            raise ValueError("心率模块需要明确的牛号与设备号")
        self.cow_id = str(cow_id).strip()
        self.device_id = str(device_id).strip().upper()
        self.binding_id = str(binding_id or f"{self.cow_id}-{self.device_id}")
        self.config = config or Config()
        self._rows = []  # [sample_ms, available_ms, bpm|None, quality, rmssd|None]
        self._seen = {}
        self._last_evaluated_ms = 0

    def add_measurement(self, measurement):
        """Accept one output of :func:`measure_document` (any grade)."""
        if str(measurement.get("device_id") or self.device_id).upper() != self.device_id:
            raise ValueError("测量不属于该设备绑定")
        sample = _stamp(measurement["sample_epoch_ms"], "sample_epoch_ms")
        available = _stamp(measurement["available_epoch_ms"], "available_epoch_ms")
        if available < sample:
            raise ValueError("可用时间不能早于采样时间")
        key = measurement.get("source_sha256") or f"{measurement.get('uid')}|{sample}"
        fingerprint = f"{sample}|{measurement.get('heart_rate_bpm')}|{measurement.get('grade')}"
        if key in self._seen:
            if self._seen[key] != fingerprint:
                raise ValueError("同一来源的心率测量内容冲突")
            return "DUPLICATE_IGNORED"
        quality = bool(measurement.get("quality")) and measurement.get("heart_rate_bpm") is not None
        bpm = float(measurement["heart_rate_bpm"]) if quality else None
        rmssd = measurement.get("rmssd_ms") if quality else None
        self._rows.append([sample, available, bpm, quality, None if rmssd is None else float(rmssd)])
        self._rows.sort(key=lambda r: (r[0], r[1]))
        self._seen[key] = fingerprint
        while len(self._seen) > self.config.uid_retention_count:
            del self._seen[next(iter(self._seen))]
        newest = max(r[0] for r in self._rows)
        cutoff = newest - self.config.history_hours * HOUR_MS
        self._rows = [r for r in self._rows if r[0] >= cutoff]
        return "ACCEPTED"

    def _known(self, now):
        rows = [r for r in self._rows if r[1] <= now and r[0] <= now]
        if not rows:
            return None
        a = np.asarray([[r[0], r[1], np.nan if r[2] is None else r[2], float(r[3]),
                         np.nan if r[4] is None else r[4]] for r in rows], dtype=float)
        return a

    def evaluate(self, now_ms):
        """Features at ``now_ms`` using only measurements received by then."""
        now = _stamp(now_ms, "now_ms")
        if now < self._last_evaluated_ms:
            raise ValueError("评估时间不能倒退")
        self._last_evaluated_ms = now
        c = self.config
        features = {name: None for name in FEATURE_COLUMNS}
        extra = dict(quality_records_6h=0, records_6h=0, baseline_samples=0, history_hours=0.0,
                     latest_sample_epoch_ms=None, latest_quality_epoch_ms=None)
        a = self._known(now)
        if a is None:
            return self._render(now, "NO_DATA", features, extra)
        t, bpm, quality, rmssd = a[:, 0], a[:, 2], a[:, 3] > 0, a[:, 4]
        profile = np.asarray(c.circadian_bpm if c.circadian_bpm is not None else CIRCADIAN_BPM)
        corrected = bpm - (profile[((t / HOUR_MS + CHINA_OFFSET_H) % 24).astype(int)]
                           if c.circadian_correction else 0.0)
        extra["latest_sample_epoch_ms"] = int(t.max())
        extra["history_hours"] = round((now - t.min()) / HOUR_MS, 3)
        good_t = t[quality]
        if len(good_t):
            last = int(np.argmax(np.where(quality, t, -np.inf)))
            extra["latest_quality_epoch_ms"] = int(t[last])
            if now - t[last] <= c.latest_hold_minutes * 60000:
                features["heart_rate_bpm"] = _r(bpm[last], 2)
        w6 = t > now - c.level_hours * HOUR_MS
        g6 = w6 & quality
        extra["records_6h"], extra["quality_records_6h"] = int(w6.sum()), int(g6.sum())
        if w6.any():
            features["ppg_pulse_quality_6h"] = _r(quality[w6].mean())
        wq = (t > now - c.quality_reference_hours * HOUR_MS) & (t <= now - c.level_hours * HOUR_MS)
        if w6.any() and wq.sum() >= c.minimum_quality_reference_records:
            features["ppg_pulse_quality_drop"] = _r(quality[w6].mean() - quality[wq].mean())
        if g6.sum() >= c.minimum_level_samples:
            features["hr_level_6h_bpm"] = _r(np.median(corrected[g6]), 2)
        if g6.sum() >= c.minimum_instability_samples:
            features["hr_instability_bpm"] = _r(np.std(bpm[g6]), 3)
        s24 = quality & (t > now - c.slope_hours * HOUR_MS)
        if s24.sum() >= c.minimum_slope_samples:
            slope = _theil_sen(t[s24] / HOUR_MS, bpm[s24])
            features["hr_slope_24h_bpm_per_h"] = _r(slope)
        base = quality & (t > now - c.baseline_start_hours * HOUR_MS) & (t <= now - c.baseline_end_hours * HOUR_MS)
        extra["baseline_samples"] = int(base.sum())
        if base.sum() >= c.provisional_baseline_samples:
            reference = float(np.median(corrected[base]))
            scale = max(c.robust_scale_floor_bpm, 1.4826 * float(np.median(np.abs(corrected[base] - reference))))
            features["hr_baseline_bpm"] = _r(reference, 2)
            if features["hr_level_6h_bpm"] is not None:
                rise = features["hr_level_6h_bpm"] - reference
                features["hr_rise_bpm"] = _r(rise, 3)
                features["hr_robust_z"] = _r(rise / scale)
            rb = rmssd[base & np.isfinite(rmssd)]
            r6 = rmssd[g6 & np.isfinite(rmssd)]
            if len(rb) >= 5 and len(r6) >= 2 and np.median(rb) > 0:
                features["prv_rmssd_ratio"] = _r(np.median(r6) / np.median(rb))
        if now - t.max() > c.stale_minutes * 60000:
            status = "STALE_DATA"
        elif g6.sum() < c.minimum_level_samples:
            status = "LOW_COVERAGE"
        elif base.sum() < c.provisional_baseline_samples:
            status = "INSUFFICIENT_HISTORY"
        elif base.sum() < c.minimum_baseline_samples or extra["history_hours"] < c.minimum_history_hours:
            status = "PROVISIONAL"
        else:
            status = "VALID"
        return self._render(now, status, features, extra)

    def _render(self, now, status, features, extra):
        c = self.config
        usable = status in ("VALID", "PROVISIONAL")
        if not usable:
            for name in ("hr_rise_bpm", "hr_robust_z", "prv_rmssd_ratio"):
                features[name] = None
        rise = features["hr_rise_bpm"]
        if not usable or rise is None:
            level = "UNAVAILABLE"
        elif (rise >= c.strong_rise_bpm and (features["hr_instability_bpm"] or 0) >= c.strong_instability_bpm
              and features["ppg_pulse_quality_drop"] is not None
              and features["ppg_pulse_quality_drop"] <= c.strong_quality_drop):
            level = "STRONG"
        elif rise >= c.elevated_rise_bpm:
            level = "ELEVATED"
        else:
            level = "LOW"
        return {"module": "cowmata_heart_rate", "version": VERSION, "signal_version": SIGNAL_VERSION,
                "cow_id": self.cow_id, "device_id": self.device_id, "binding_id": self.binding_id,
                "evaluated_at_ms": now, "quality_status": status, "usable_for_fusion": usable,
                "heart_rate_evidence_level": level, "features": dict(features), **extra,
                "thresholds": {"elevated_rise_bpm": c.elevated_rise_bpm, "strong_rise_bpm": c.strong_rise_bpm,
                               "strong_instability_bpm": c.strong_instability_bpm,
                               "strong_quality_drop": c.strong_quality_drop},
                "calving_probability": None, "eta_hours": None}

    def to_state_json(self):
        state = dict(state_version=STATE_VERSION, module_version=VERSION, cow_id=self.cow_id,
                     device_id=self.device_id, binding_id=self.binding_id, config=asdict(self.config),
                     rows=self._rows, seen=list(self._seen.items()), last_evaluated_ms=self._last_evaluated_ms)
        return json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    @classmethod
    def from_state_json(cls, text):
        try:
            state = json.loads(text)
            if state["state_version"] != STATE_VERSION or state["module_version"] != VERSION:
                raise ValueError("状态版本不兼容；请用当前版本重放历史记录")
            obj = cls(state["cow_id"], state["device_id"], state["binding_id"], Config(**state["config"]))
            for row in state["rows"]:
                if len(row) != 5 or _stamp(row[1], "available") < _stamp(row[0], "sample"):
                    raise ValueError("历史记录格式错误")
            obj._rows = [list(r) for r in state["rows"]]
            obj._seen = dict(state["seen"])
            obj._last_evaluated_ms = int(state["last_evaluated_ms"])
            return obj
        except (KeyError, TypeError) as exc:
            raise ValueError(f"心率状态无效：{exc}") from exc


def as_fusion_features(result, *, now_ms=None):
    """Compact continuous features; unavailable heart rate must not veto other modalities."""
    now = result["evaluated_at_ms"] if now_ms is None else _stamp(now_ms, "now_ms")
    if now < result["evaluated_at_ms"]:
        raise ValueError("now_ms 不能早于评估时间")
    latest = result.get("latest_sample_epoch_ms")
    age = (now - latest) / 1000 if latest is not None else None
    usable = bool(result["usable_for_fusion"] and age is not None and age <= Config().stale_minutes * 60)
    f = result["features"]
    return {"heart_rate_available": usable,
            "heart_rate_level": result["heart_rate_evidence_level"] if usable else "UNAVAILABLE",
            "heart_rate_quality_status": result["quality_status"],
            "heart_rate_observation_age_seconds": age,
            **{k: (f[k] if usable or k in ("heart_rate_bpm", "ppg_pulse_quality_6h") else None)
               for k in FEATURE_COLUMNS}}


def replay(measurements, evaluation_times, *, cow_id, device_id, config: Config | None = None):
    """Evaluate a binding at each time; measurements may be given in any order."""
    module = HeartRateModule(cow_id, device_id, config=config)
    pending = sorted(measurements, key=lambda m: m["available_epoch_ms"])
    results, i = [], 0
    for now in sorted(evaluation_times):
        while i < len(pending) and pending[i]["available_epoch_ms"] <= now:
            module.add_measurement(pending[i])
            i += 1
        results.append(module.evaluate(int(now)))
    return results


def measurement_fingerprint(measurements):
    digest = hashlib.sha256()
    for m in sorted(measurements, key=lambda m: (m["sample_epoch_ms"], str(m.get("source_sha256")))):
        digest.update(json.dumps([m["sample_epoch_ms"], m.get("source_sha256"), m.get("heart_rate_bpm"),
                                  m.get("grade")], separators=(",", ":")).encode())
    return digest.hexdigest()


def measure_file(path):
    content = Path(path).read_bytes()
    document = json.loads(content.decode("utf-8-sig"))
    if not isinstance(document, dict) or document.get("imu"):
        raise ValueError("不是 PPG JSON")
    return measure_document(document, source=str(path), source_sha256=hashlib.sha256(content).hexdigest())


def attach_heart_rate(rows, ppg_records, *, time_key="decision_epoch_ms"):
    """Fill ``heart_rate_bpm`` and HR evidence columns of fusion rows, causally.

    Each row gets the state of its own cow/device binding at ``row[time_key]``: only
    captures the server had received by then are used. Returns data issues.
    """
    issues, bindings = [], {}
    for record in ppg_records:
        key = (record["cow_id"], record["device_id"], record.get("field_mark", ""))
        try:
            bindings.setdefault(key, []).append(measure_file(record["raw"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append(dict(path=str(record.get("raw")), reason=f"心率计算失败：{exc}"))
    by_binding = {}
    for row in rows:
        row.setdefault("heart_rate_bpm", None)
        key = (row.get("cow_id"), row.get("device_id"), row.get("field_mark", ""))
        if key in bindings and row.get(time_key) is not None:
            by_binding.setdefault(key, []).append(row)
    for key, items in by_binding.items():
        items.sort(key=lambda r: r[time_key])
        results = replay(bindings[key], [int(r[time_key]) for r in items], cow_id=key[0], device_id=key[1])
        for row, result in zip(items, results):
            f = result["features"]
            row["heart_rate_bpm"] = f["heart_rate_bpm"]
            row["heart_rate_level_6h_bpm"] = f["hr_level_6h_bpm"]
            row["heart_rate_rise_bpm"] = f["hr_rise_bpm"]
            row["heart_rate_instability_bpm"] = f["hr_instability_bpm"]
            row["ppg_pulse_quality_6h"] = f["ppg_pulse_quality_6h"]
            row["heart_rate_quality_status"] = result["quality_status"]
            row["heart_rate_evidence_level"] = result["heart_rate_evidence_level"]
            row["heart_rate_version"] = VERSION
    return issues
