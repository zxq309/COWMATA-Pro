"""行为事件频次 (behavior events) decision feature, plug-in ``cowmata-decision-feature-1``.

Bridges behavior recognition and calving decision (4.3.8): the event suite trained by
``cowmata_engine.behavior.train`` (起立 STANDING_UP / 卧倒 LYING_DOWN / 努责 STRAINING_BOUT,
tail-ring 9-axis, video-labelled) scans every Motion file; recognised events are counted per
absolute 10-min window.  Pre-calving restlessness (more lying bouts and posture changes in the
last 6-12 h, Jensen 2012; Miedema 2011; Borchers 2017) is then expressed by the engine's per-cow
baselines (1 h / 6 h sums, 24 h delta, 72 h z-score).

The suite folder (``suite.json`` + models) comes from ``$COWMATA_BEHAVIOR_SUITE`` or the active /
newest suite under ``<model_home>/versions``.  Only events whose ``available_ms`` is inside the
lookahead are used, so every row is causal.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import numpy as np

from cowmata_engine.features.base import WINDOW_MS, FeatureSpec, empty_row, window_grid, window_start

LOOKAHEAD_MS = 300_000  # second features use a +-5 min context; events are known <= 5 min after they end
CODES = ("STANDING_UP", "LYING_DOWN", "STRAINING_BOUT")

SPEC = FeatureSpec(
    key="behavior_events",
    title="行为事件频次",
    modality="motion",
    version="behavior-events-1",
    columns=("posture_changes", "standing_up_events", "lying_down_events", "straining_events",
             "straining_score_max", "behavior_observed_min"),
    primary="posture_changes",
    unit="次/10 min",
    lookahead_ms=LOOKAHEAD_MS,
    derivations=("1h", "6h", "d24", "z72", "slope6h"),
    expected_change="产前 6–12 h 起卧转换与卧倒次数增加（坐立不安），产前 1–3 h 努责事件成串出现。",
    column_titles={
        "posture_changes": "起卧转换次数",
        "standing_up_events": "起立次数",
        "lying_down_events": "卧倒次数",
        "straining_events": "努责事件数",
        "straining_score_max": "最高努责置信度",
        "behavior_observed_min": "有效观测分钟",
    },
)


def suite_dir() -> Path:
    configured = os.environ.get("COWMATA_BEHAVIOR_SUITE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    from cowmata_tailring.algorithms.paths import model_home
    from cowmata_tailring.algorithms.registry import active_suite, list_suites

    home = model_home()
    suite = active_suite(home) or next(iter(list_suites(home)), None)
    if suite is None:
        raise FileNotFoundError(f"缺少行为识别模型：{home}\\versions\\*\\suite.json（先运行行为识别训练）")
    return Path(suite["root"])


@lru_cache(maxsize=4)
def _suite(folder: str):
    from cowmata_tailring.algorithms.registry import read_suite

    suite = read_suite(Path(folder))
    codes = [m["code"] for m in suite["models"] if m["code"] in CODES]
    if not codes:
        raise ValueError("行为识别模型不含 起立/卧倒/努责 事件")
    return suite, tuple(codes)


def load_suite(folder=None):
    return _suite(str(Path(folder).resolve() if folder else suite_dir()))


def file_events(path, suite, codes):
    """(epoch_start, epoch_end, [(code, point_epoch, available_epoch, score)], observed ms per second grid)."""
    import json

    from cowmata_tailring.algorithms.evidence import infer_features
    from cowmata_tailring.algorithms.features import second_features
    from cowmata_tailring.annotation.data import GRAVITY_MS2, parse_motion_object

    document = json.loads(Path(path).read_bytes().decode("utf-8-sig"))
    motion = parse_motion_object(document, source_path=Path(path))
    acc = np.column_stack([motion.channels[k] for k in ("ax", "ay", "az")]) / GRAVITY_MS2
    gyro = np.column_stack([motion.channels[k] for k in ("gx", "gy", "gz")])
    valid = (np.linalg.norm(acc, axis=1) > .01) & (np.max(np.abs(gyro), axis=1) < 1023.5)
    f = second_features(motion.times_ms, acc, gyro, valid_samples=valid)
    feature = dict(f, duration_ms=motion.duration_ms, source=str(path))
    events = infer_features(suite, feature, list(codes))
    origin = motion.epoch_at(0.0)
    ev = [(e["code"], origin + float(e.get("point_ms", e["start_ms"])),
           origin + float(e.get("available_ms", e["end_ms"])), float(e.get("score", 0.0))) for e in events]
    seconds_epoch = origin + np.asarray(f["seconds"], float) * 1000.0
    observed = np.asarray(f["coverage"], float) * np.asarray(f["valid"], bool)
    return origin, origin + motion.duration_ms, ev, seconds_epoch, observed


def extract_series(sources, *, window_ms=WINDOW_MS, suite_folder=None):
    suite, codes = load_suite(suite_folder)
    acc = {}
    for path in sources:
        start, end, events, sec_epoch, observed = file_events(path, suite, codes)
        for a, b in window_grid(start, end, window_ms):
            acc.setdefault(a, dict(obs=0.0, ev=[], latest=b))
        starts = (sec_epoch // window_ms).astype(np.int64) * window_ms
        for a in np.unique(starts):
            acc.setdefault(int(a), dict(obs=0.0, ev=[], latest=int(a) + window_ms))["obs"] += float(observed[starts == a].sum())
        for code, point, available, score in events:
            a = window_start(point, window_ms)
            slot = acc.setdefault(a, dict(obs=0.0, ev=[], latest=a + window_ms))
            if available <= a + window_ms + LOOKAHEAD_MS:
                slot["ev"].append((code, score))
    rows = []
    for a in sorted(acc):
        slot = acc[a]
        cover = min(1.0, slot["obs"] / (window_ms / 1000.0))
        row = empty_row(SPEC, a, a + window_ms, coverage=cover)
        if slot["obs"] >= 60.0:
            count = {c: sum(1 for k, _ in slot["ev"] if k == c) for c in CODES}
            row.update(standing_up_events=float(count["STANDING_UP"]), lying_down_events=float(count["LYING_DOWN"]),
                       posture_changes=float(count["STANDING_UP"] + count["LYING_DOWN"]),
                       straining_events=float(count["STRAINING_BOUT"]) if "STRAINING_BOUT" in codes else None,
                       straining_score_max=max([s for k, s in slot["ev"] if k == "STRAINING_BOUT"], default=0.0)
                       if "STRAINING_BOUT" in codes else None,
                       behavior_observed_min=slot["obs"] / 60.0)
        rows.append(row)
    return rows


def extract(source, *, window_ms=WINDOW_MS):
    return extract_series([source], window_ms=window_ms)
