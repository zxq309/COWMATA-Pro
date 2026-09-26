"""温度 (temperature) decision feature, plug-in ``cowmata-decision-feature-1``.

Tail-ring skin temperature in absolute 10-min windows. The tail ring stores one signed int16
temperature bucket per ~60 s inside each Motion JSON (``base64_int16_le_x0.01``); bucket times
are the IMU-bucket midpoints used by ``cowmata_tailring.temperature.motion_temperature_records``
(``imu_bucket_midpoint_estimate``). Standalone scalar Temp JSONs (``cowmata-temperature-1``,
``data`` in Celsius) are accepted as well. No labels, ledgers or video are read here; per-cow
24 h / 72 h baselines, z-scores, slopes and circadian residuals are derived by the engine.

Columns
* ``temp_median_c`` (primary): median of on-cow minutes. On-cow = 34.0-41.5 C; the CalvingPred
  temperature histogram is bimodal (on-cow 36-40 C, detached / contact-loss 22-29 C = ambient).
* ``temp_max_c``: maximum on-cow minute, the representative value used by the tail-base studies
  (Koyama 2018 Vet J; Higaki 2020 JDS / 2022 Animals; Miwa 2019 JRD: hourly max, residual vs the
  same hour of the previous 3 days).
* ``temp_standing_median_c``: median of on-cow minutes while standing (tail roll < 60 deg from the
  minute-mean gravity, roll = atan2(|gx|, gz)). Lying presses the tail against the body and
  raises skin temperature; the standing-only same-clock decline was one of the strongest single
  features in 43 cows (24 h AUC 0.80).
* ``on_cow_fraction``: on-cow minutes / temperature minutes in the window (sensor contact quality).

``coverage`` = temperature minutes observed / window minutes. Medians need >= 5 on-cow minutes,
the standing median >= 3 standing minutes; otherwise None.
``available_epoch_ms`` = latest server receipt (``update_time``) of the packets contributing to
the window, never earlier than the window end.

Validation (4.3.4/温度, 46 cows, 2 387 Motion records, 137 820 minutes, 44 cows with T0): the
same-clock previous-day difference turns negative from about -21 h (-0.2 to -0.5 C until -8 h),
then falls sharply from -6 h (-0.35 C) to -1 h (-1.32 C); last-24 h mean vs previous 24 h
median -0.37 C (IQR -0.49..-0.21, 79 % of cows >= 0.2 C); recovery 3-5 h after calving.
Circadian amplitude about 0.57 C (low 07-09 h, high 13-19 h Beijing time).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from cowmata_engine.features.base import (
    WINDOW_MS,
    FeatureSpec,
    finite_or_none,
    window_grid,
    window_start,
)

GRAVITY_MS2 = 9.80665
ON_COW_MIN_C = 34.0
ON_COW_MAX_C = 41.5
LYING_ROLL_DEG = 60.0
MIN_ON_COW_MINUTES = 5
MIN_STANDING_MINUTES = 3
MIN_IMU_SAMPLES = 50

SPEC = FeatureSpec(
    key="temperature",
    title="温度",
    modality="motion",
    version="temperature-2",
    columns=("temp_median_c", "temp_max_c", "temp_standing_median_c", "on_cow_fraction"),
    primary="temp_median_c",
    unit="°C",
    lookahead_ms=0,
    expected_change=("产前尾根温度两段式下降（46 头牛，44 头有 T0）：与前一天同时刻相比，"
                     "−21~−8 h 低 0.2–0.5 °C，−6 h 起急降，−1 h 低约 1.3 °C；"
                     "产前 24 h 均值较再前 24 h 下降中位 0.37 °C（79% 的牛 ≥0.2 °C），产后 3–5 h 恢复。"
                     "日节律振幅约 0.57 °C（07–09 时低、13–19 时高），必须按同钟点或多日基线比较，"
                     "至少需要 24 h（建议 3 天）佩戴历史。"),
    column_titles={
        "temp_median_c": "在体温度中位数（°C，34–41.5 °C 视为在体）",
        "temp_max_c": "在体温度最大值（°C，尾根文献按小时最大值做残差）",
        "temp_standing_median_c": "站姿分钟温度中位数（°C，排除卧压升温）",
        "on_cow_fraction": "在体分钟占比（接触/脱落质量）",
    },
    derivations=("1h", "6h", "d24", "z72", "slope6h", "circ"),
)


def _decode_int16(text):
    raw = base64.b64decode("".join(str(text).split()), validate=True)
    if len(raw) % 2:
        raise ValueError("temperature 字节数不是 int16 的整数倍")
    return np.frombuffer(raw, dtype="<i2").astype(np.int64)


def packet_minutes(path):
    """One raw JSON -> (meta, [(epoch_ms, celsius, standing | None)]); never crosses files.

    ``meta["span"]`` is the acquisition span of the record so that windows without any
    temperature bucket are still emitted (coverage 0, values None)."""
    path = Path(path)
    content = path.read_bytes()
    doc = json.loads(content.decode("utf-8-sig"))
    if not isinstance(doc, dict):
        raise ValueError("JSON 顶层必须是对象")
    received = doc.get("update_time")
    received = int(received) if type(received) is int and received > 0 else None
    meta = dict(source=str(path), sha256=hashlib.sha256(content).hexdigest(), received_epoch_ms=received)
    if doc.get("imu") in (None, ""):
        from cowmata_tailring.temperature import read_temperature_record

        sample = read_temperature_record(doc)
        meta["received_epoch_ms"] = sample["available_at_ms"]
        meta["span"] = (int(sample["time"]), int(sample["time"]))
        return meta, [(int(sample["time"]), float(sample["value"]), None)]
    from cowmata_tailring.annotation.data import parse_motion_object

    m = parse_motion_object(doc, source_path=path)
    meta["span"] = (float(m.epoch_at(float(m.times_ms[0]))), float(m.epoch_at(float(m.times_ms[-1]))))
    if not doc.get("temperature"):
        return meta, []
    values = _decode_int16(doc["temperature"]) / 100.0
    rel = np.asarray(m.temperature_times_ms, dtype=np.float64)
    count = min(len(values), len(rel))
    step = m.duration_ms / len(values) if len(values) else 60000.0
    t_imu = np.asarray(m.times_ms, dtype=np.float64)
    acc = np.column_stack([np.asarray(m.channels[k], np.float64) for k in ("ax", "ay", "az")]) / GRAVITY_MS2
    lo = np.searchsorted(t_imu, rel[:count] - step / 2, side="left")
    hi = np.searchsorted(t_imu, rel[:count] + step / 2, side="left")
    out = []
    for i in range(count):
        standing = None
        if hi[i] - lo[i] >= MIN_IMU_SAMPLES:
            g = acc[lo[i]:hi[i]].mean(axis=0)
            standing = math.degrees(math.atan2(abs(g[0]), g[2])) < LYING_ROLL_DEG
        out.append((int(round(m.epoch_at(float(rel[i])))), float(values[i]), standing))
    return meta, out


def _row(start, end, samples, available, source):
    row = dict(start_epoch_ms=int(start), end_epoch_ms=int(end), available_epoch_ms=int(max(end, available)),
               coverage=float(min(1.0, len(samples) / ((end - start) / 60000.0))), source=source)
    row.update({name: None for name in SPEC.columns})
    if not samples:
        return row
    c = np.array([s[1] for s in samples])
    on = (c >= ON_COW_MIN_C) & (c <= ON_COW_MAX_C)
    row["on_cow_fraction"] = finite_or_none(on.mean())
    if on.sum() >= MIN_ON_COW_MINUTES:
        row["temp_median_c"] = finite_or_none(np.median(c[on]))
        row["temp_max_c"] = finite_or_none(c[on].max())
    standing = on & np.array([s[2] is True for s in samples])
    if standing.sum() >= MIN_STANDING_MINUTES:
        row["temp_standing_median_c"] = finite_or_none(np.median(c[standing]))
    return row


def windows_from_packets(parts, window_ms=WINDOW_MS):
    """parts: [(meta, minutes)] of one cow/device -> rows. Re-uploaded identical minutes count once."""
    windows = {}
    for meta, minutes in parts:
        received = meta.get("received_epoch_ms")
        if meta.get("span"):
            for start, end in window_grid(meta["span"][0], meta["span"][1], window_ms):
                slot = windows.setdefault(start, dict(samples={}, available=0, source=meta["source"]))
                slot["available"] = max(slot["available"], received if received else end)
        for epoch, value, standing in minutes:
            start = window_start(epoch, window_ms)
            slot = windows.setdefault(start, dict(samples={}, available=0, source=meta["source"]))
            key = (epoch, value)
            if key not in slot["samples"] or slot["samples"][key] is None:
                slot["samples"][key] = standing
            slot["available"] = max(slot["available"], received if received else start + window_ms)
    rows = []
    for start in sorted(windows):
        slot = windows[start]
        samples = [(t, v, s) for (t, v), s in sorted(slot["samples"].items())]
        rows.append(_row(start, start + window_ms, samples, slot["available"], slot["source"]))
    return rows


def extract_series(sources, *, window_ms=WINDOW_MS, issues=None):
    """Rows for one cow/device; windows split across consecutive packets are merged.

    A file that cannot be decoded is skipped (reported in ``issues`` when given) instead of
    discarding the whole cow/device series."""
    parts = []
    for path in sources:
        try:
            parts.append(packet_minutes(path))
        except Exception as exc:  # corrupt / non-Motion JSON: keep the rest of the series
            if issues is not None:
                issues.append(dict(path=str(path), feature=SPEC.key, reason=f"{type(exc).__name__}: {exc}"))
    return windows_from_packets(parts, window_ms)


def extract(source, *, window_ms=WINDOW_MS):
    return extract_series([source], window_ms=window_ms)