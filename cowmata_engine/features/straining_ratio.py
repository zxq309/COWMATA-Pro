"""努责占比 (straining ratio) decision feature, plug-in ``cowmata-decision-feature-1``.

Share of observed time in each absolute 10-min window that the cow spends in abdominal
straining, measured from the tail-ring 9-axis signal:

1. The reviewed straining detector (``cowmata_tailring.algorithms.straining``, contraction
   pulse-train model) scores every second and proposes candidate bouts with a stage-2 score.
2. Only the part of an accepted bout that is covered by a chain of quiet contraction pulses
   (gap <= 9 s, padded 0.5 s / 1.0 s) is counted. Video labels start ~0.5 s before the first and
   end ~0.7 s after the last pulse, whereas the detector's bout edges run ~10 s early / 7 s late.
3. Each bout counts with its calibrated probability g(stage-2) (probabilistic classify-and-count).
4. The window share is corrected for recognition error with the adjusted classify-and-count /
   Rogan-Gladen estimator ``(raw - FPR) / (TPR - FPR)`` using the second-level TPR / FPR measured
   against video labels (leave-one-cow-out), clipped to [0, 1].

Model files (never stored in the source tree) live in a directory containing
``straining_bundle.json`` + ``straining_ratio_params.json``: ``$COWMATA_STRAINING_RATIO_MODEL`` or
``<model_home>/experiments/STRAINING_BOUT/straining_ratio``.
"""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

import numpy as np

from cowmata_engine.features.base import WINDOW_MS, FeatureSpec, empty_row, finite_or_none

PARAMS_SCHEMA = "cowmata-straining-ratio-params-1"
PARAMS_FILE = "straining_ratio_params.json"
BUNDLE_FILE = "straining_bundle.json"
MIN_VALID_S = 60.0
LOOKAHEAD_MS = 900_000  # stage-2 neighbour counts use +-15 min, second features +-5 min

SPEC = FeatureSpec(
    key="straining_ratio",
    title="努责占比",
    modality="motion",
    version="straining-ratio-2",
    columns=("straining_ratio", "straining_ratio_raw", "straining_seconds", "straining_bouts",
             "straining_recurrent_bouts", "straining_pulses_per_min", "straining_confidence_max"),
    primary="straining_ratio",
    unit="fraction",
    lookahead_ms=LOOKAHEAD_MS,
    # Far from calving ~98 % of windows are 0, so baseline deltas / z-scores / diurnal residuals are meaningless.
    derivations=("1h", "6h", "slope6h"),
    expected_change=("产前 >3 h 与背景相同（10 min 窗均值约 0.002，98% 窗为 0）；-3~-2 h 开始上升"
                     "（60 min 均值 0.011→0.031）；娩出前最后 1 h 60 min 均值 0.107（中位 0.095，P90 0.27，"
                     "61% 窗 >2%）；娩出后 1 h 内回落到背景。只对 ≤3 h 的临产窗口有区分力。"
                     "44 头有产犊时间的牛 / 2 297 h 连续数据 + 25 段视频努责标签，按牛留一验证。"),
    column_titles={
        "straining_ratio": "努责占比（误差校正）",
        "straining_ratio_raw": "努责占比（未校正）",
        "straining_seconds": "努责秒数（校正后）",
        "straining_bouts": "努责段数",
        "straining_recurrent_bouts": "成串努责段数（±5 min 内有其他努责）",
        "straining_pulses_per_min": "收缩脉冲次数/有效分钟",
        "straining_confidence_max": "最高努责段置信度",
    },
)


# ----------------------------------------------------------------------------- model files

def default_model_dir() -> Path:
    configured = os.environ.get("COWMATA_STRAINING_RATIO_MODEL", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    from cowmata_tailring.algorithms.paths import model_home

    return model_home() / "experiments" / "STRAINING_BOUT" / "straining_ratio"


@lru_cache(maxsize=4)
def _load(folder: str):
    from cowmata_tailring.algorithms.straining import load_bundle

    folder = Path(folder)
    params = json.loads((folder / PARAMS_FILE).read_text(encoding="utf-8"))
    if params.get("schema") != PARAMS_SCHEMA:
        raise ValueError("不是努责占比参数文件")
    bundle = load_bundle(folder / BUNDLE_FILE)
    if params.get("bundle_sha256") and params["bundle_sha256"] != bundle["sha256"]:
        raise ValueError("努责占比参数与努责算法包版本不一致，请重新生成参数")
    return bundle, params


def load_model(folder=None):
    folder = Path(folder) if folder else default_model_dir()
    if not (folder / PARAMS_FILE).is_file() or not (folder / BUNDLE_FILE).is_file():
        raise FileNotFoundError(f"缺少努责占比模型：{folder}（需要 {BUNDLE_FILE} 与 {PARAMS_FILE}）")
    return _load(str(folder.resolve()))


# ----------------------------------------------------------------------------- pure functions

def bout_weight(params, p2):
    """Calibrated probability that a candidate bout is real straining (isotonic step table)."""
    x, y = params["bout_calibration"]["x"], params["bout_calibration"]["y"]
    return np.interp(np.asarray(p2, float), x, y)


def core_mask(seconds, bouts_ms, quiet_pulse_s, gap_s, pad_before_s, pad_after_s):
    """Seconds (centres, s) covered by chains of quiet contraction pulses inside the given bouts."""
    seconds = np.asarray(seconds, float)
    qt = np.sort(np.asarray(quiet_pulse_s, float))
    mask = np.zeros(len(seconds), bool)
    for a, b in bouts_ms:
        q = qt[(qt >= a / 1000.0) & (qt <= b / 1000.0)]
        if len(q) == 0:
            continue
        start = q[0]
        for x, y in zip(q[:-1], q[1:]):
            if y - x > gap_s:
                mask |= (seconds >= start - pad_before_s) & (seconds <= x + pad_after_s)
                start = y
        mask |= (seconds >= start - pad_before_s) & (seconds <= q[-1] + pad_after_s)
    return mask


def record_detection(bundle, t_ms, acc_g, gyr_dps):
    """All candidate bouts (with stage-2 score) plus the per-second validity of one record."""
    from cowmata_tailring.algorithms.models import predict_forest
    from cowmata_tailring.algorithms.straining import signal
    from cowmata_tailring.algorithms.straining.candidates import candidate_features
    from cowmata_tailring.algorithms.straining.model import candidates, score_seconds

    feature = signal.second_features(t_ms, acc_g, gyr_dps)
    scores = score_seconds(bundle, feature)
    events, smoothed = candidates(bundle, feature, scores)
    p2 = predict_forest(bundle["stage2"], candidate_features(feature, events, smoothed)) if events else np.zeros(0)
    pulses = feature["pulses"]
    quiet = pulses["t"][pulses["quiet"].astype(bool)] if len(pulses["t"]) else np.zeros(0)
    return dict(seconds=np.asarray(feature["seconds"], float), valid=np.asarray(feature["valid"], bool),
                bout_start_ms=np.array([e["start_ms"] for e in events], float),
                bout_end_ms=np.array([e["end_ms"] for e in events], float),
                bout_p2=np.asarray(p2, float), quiet_pulse_t=np.asarray(quiet, float))


def window_rows(records, params, *, window_ms=WINDOW_MS):
    """records: [dict(epoch0_ms, seconds, valid, bout_start_ms, bout_end_ms, bout_p2, quiet_pulse_t, source)]
    of ONE cow/device (any order). Returns contract rows sorted by window start."""
    seg = params["core"]
    thr = float(params["stage2_threshold"])
    tpr, fpr = float(params["tpr"]), float(params["fpr"])
    neigh_ms = float(params["recurrence_window_ms"])
    starts = np.sort(np.concatenate([np.asarray(r["epoch0_ms"], float) + np.asarray(r["bout_start_ms"], float)[np.asarray(r["bout_p2"], float) >= thr]
                                     for r in records])) if records else np.zeros(0)
    acc = {}
    for r in records:
        sec = np.asarray(r["seconds"], float)
        valid = np.asarray(r["valid"], bool)
        e0 = float(r["epoch0_ms"])
        p2 = np.asarray(r["bout_p2"], float)
        keep = p2 >= thr
        a_ms, b_ms, p_ok = np.asarray(r["bout_start_ms"], float)[keep], np.asarray(r["bout_end_ms"], float)[keep], p2[keep]
        weights = bout_weight(params, p_ok) if len(p_ok) else np.zeros(0)
        w = np.zeros(len(sec))
        qt = np.asarray(r["quiet_pulse_t"], float)
        in_core = np.zeros(len(qt), bool)
        for a, b, g in zip(a_ms, b_ms, weights):
            m = core_mask(sec, [(a, b)], qt, seg["gap_s"], seg["pad_before_s"], seg["pad_after_s"])
            w = np.maximum(w, m * g)
            if m.any():
                lo, hi = sec[m].min() - 0.5, sec[m].max() + 0.5
                in_core |= (qt >= lo) & (qt <= hi) & (qt >= a / 1000) & (qt <= b / 1000)
        win = np.floor((e0 + sec * 1000.0) / window_ms).astype(np.int64)
        bwin = np.floor((e0 + a_ms) / window_ms).astype(np.int64)
        recurrent = np.array([((starts >= e0 + a - neigh_ms) & (starts <= e0 + a + neigh_ms)).sum() - 1 >= 1 for a in a_ms], bool)
        qwin = np.floor((e0 + qt * 1000.0) / window_ms).astype(np.int64)
        for k in np.unique(win):
            sel = (win == k) & valid
            d = acc.setdefault(int(k), dict(valid_s=0.0, w_s=0.0, bouts=0, recurrent=0, pulses=0, conf=0.0, sources=[]))
            d["valid_s"] += float(sel.sum())
            d["w_s"] += float(w[sel].sum())
            inb = bwin == k
            d["bouts"] += int(inb.sum())
            d["recurrent"] += int((inb & recurrent).sum())
            d["pulses"] += int(((qwin == k) & in_core).sum())
            over = [g for a, b, g in zip(a_ms, b_ms, weights)
                    if e0 + a < (k + 1) * window_ms and e0 + b > k * window_ms]
            if over:
                d["conf"] = max(d["conf"], float(max(over)))
            if r.get("source"):
                d["sources"].append(str(r["source"]))
    rows = []
    for k in sorted(acc):
        d = acc[k]
        start = k * window_ms
        row = empty_row(SPEC, start, start + window_ms, coverage=min(1.0, d["valid_s"] * 1000.0 / window_ms))
        if d["valid_s"] >= MIN_VALID_S:
            raw = d["w_s"] / d["valid_s"]
            ratio = float(np.clip((raw - fpr) / max(tpr - fpr, 1e-6), 0.0, 1.0))
            row.update(straining_ratio=finite_or_none(ratio), straining_ratio_raw=finite_or_none(raw),
                       straining_seconds=finite_or_none(ratio * d["valid_s"]),
                       straining_bouts=d["bouts"], straining_recurrent_bouts=d["recurrent"],
                       straining_pulses_per_min=finite_or_none(d["pulses"] / (d["valid_s"] / 60.0)),
                       straining_confidence_max=finite_or_none(d["conf"]))
        if d["sources"]:
            row["source"] = sorted(set(d["sources"]))[0]
        rows.append(row)
    return rows


# ----------------------------------------------------------------------------- plug-in entry points

def _read_motion(path):
    from cowmata_tailring.annotation.data import GRAVITY_MS2, parse_motion_object

    path = Path(path)
    motion = parse_motion_object(json.loads(path.read_bytes().decode("utf-8-sig")), source_path=path)
    ch = motion.channels
    acc = np.column_stack([ch[k] for k in ("ax", "ay", "az")]) / GRAVITY_MS2
    gyr = np.column_stack([ch[k] for k in ("gx", "gy", "gz")])
    return motion, acc, gyr


def extract_series(sources, *, window_ms=WINDOW_MS, model_dir=None):
    bundle, params = load_model(model_dir)
    records = []
    for path in sources:
        motion, acc, gyr = _read_motion(path)
        if len(motion.times_ms) < 50 * 30:
            continue
        det = record_detection(bundle, motion.times_ms, acc, gyr)
        det.update(epoch0_ms=float(motion.epoch_at(0)), source=str(path))
        records.append(det)
    return window_rows(records, params, window_ms=window_ms)


def extract(source, *, window_ms=WINDOW_MS, model_dir=None):
    return extract_series([source], window_ms=window_ms, model_dir=model_dir)


def model_fingerprint(folder=None):
    folder = Path(folder) if folder else default_model_dir()
    h = hashlib.sha256()
    for name in (BUNDLE_FILE, PARAMS_FILE):
        h.update((folder / name).read_bytes())
    return h.hexdigest()


__all__ = ["SPEC", "extract", "extract_series", "window_rows", "record_detection", "core_mask",
           "bout_weight", "load_model", "default_model_dir", "model_fingerprint"]
