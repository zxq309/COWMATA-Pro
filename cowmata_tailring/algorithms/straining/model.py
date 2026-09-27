"""Versioned straining bundle: two numeric forests plus segmentation parameters."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from ..models import predict_forest
from . import signal
from .candidates import candidate_features, candidate_names


def load_bundle(path):
    path = Path(path)
    if path.is_dir():
        path = path / "straining_bundle.json"
    content = path.read_bytes()
    bundle = json.loads(content.decode("utf-8"))
    if bundle.get("schema") != "cowmata-straining-bundle-1":
        raise ValueError("不是努责算法包")
    if bundle.get("feature_version") != signal.FEATURE_VERSION:
        raise ValueError("努责算法包特征版本与程序不一致，请用当前版本重新训练")
    if bundle["stage1"]["features"] != bundle["stage1_feature_names"]:
        raise ValueError("努责算法包特征顺序损坏")
    if bundle["stage2"]["features"] != candidate_names():
        raise ValueError("努责候选特征顺序与程序不一致")
    bundle["sha256"] = hashlib.sha256(content).hexdigest()
    return bundle


def score_seconds(bundle, feature):
    if feature["names"] != bundle["stage1_feature_names"]:
        raise ValueError("努责逐秒特征顺序与算法包不一致")
    return predict_forest(bundle["stage1"], feature["X"])


def candidates(bundle, feature, scores):
    seg = dict(bundle["segmentation"])
    events, smoothed = signal.segment(
        scores, feature["valid"], feature["pulses"], seg, feature["duration_ms"]
    )
    return events, smoothed


def detect(bundle, t_ms, acc_g, gyr_dps, *, return_details=False):
    """Straining bouts (ms relative to the record start) for one continuous 50 Hz record."""
    feature = signal.second_features(t_ms, acc_g, gyr_dps)
    scores = score_seconds(bundle, feature)
    events, smoothed = candidates(bundle, feature, scores)
    kept = []
    if events:
        p2 = predict_forest(bundle["stage2"], candidate_features(feature, events, smoothed))
        for e, p in zip(events, p2):
            if p >= bundle["stage2_threshold"]:
                kept.append(
                    {
                        **e,
                        "confidence": float(p),
                        "algorithm_version": bundle["version"],
                        "requires_review": True,
                    }
                )
    if return_details:
        return kept, dict(
            feature=feature, second_scores=scores, smoothed=smoothed, candidates=events
        )
    return kept


def detect_file(bundle, raw_path):
    """Read one COWMATA motion JSON (read-only) and return straining bouts with epoch times."""
    from cowmata_tailring.annotation.data import GRAVITY_MS2, parse_motion_object

    raw_path = Path(raw_path)
    motion = parse_motion_object(
        json.loads(raw_path.read_bytes().decode("utf-8-sig")), source_path=raw_path
    )
    ch = motion.channels
    acc = np.column_stack([ch[k] for k in ("ax", "ay", "az")]) / GRAVITY_MS2
    gyr = np.column_stack([ch[k] for k in ("gx", "gy", "gz")])
    events = detect(bundle, motion.times_ms, acc, gyr)
    epoch0 = motion.epoch_at(0)
    for e in events:
        e["start_epoch_ms"] = epoch0 + e["start_ms"]
        e["end_epoch_ms"] = epoch0 + e["end_ms"]
    return events
