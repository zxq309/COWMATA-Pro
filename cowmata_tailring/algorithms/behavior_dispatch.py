"""Unified behavior prediction adapter.

This module is the application-facing bridge for the clean behavior libraries.
It accepts one raw Motion JSON and an explicit external model directory. It
returns review candidates only; it never writes labels or research artifacts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .behavior_library import spec_for
from cowmata_tailring.annotation.data import GRAVITY_MS2, parse_motion_object


def _motion(path: Path):
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    motion = parse_motion_object(document, source_path=path)
    ch = motion.channels
    acc = np.column_stack([ch[k] for k in ("ax", "ay", "az")]) / GRAVITY_MS2
    gyro = np.column_stack([ch[k] for k in ("gx", "gy", "gz")])
    return motion, acc, gyro


def _relative(events: list[dict[str, Any]], motion, code: str) -> list[dict[str, Any]]:
    epoch0 = float(motion.epoch_at(0))
    result = []
    for event in events:
        item = dict(event)
        item.setdefault("code", code)
        item.setdefault("review_status", "pending")
        item.setdefault("requires_human_confirmation", True)
        if "start_ms" not in item and "start_epoch_ms" in item:
            item["start_ms"] = float(item["start_epoch_ms"]) - epoch0
        if "end_ms" not in item and "end_epoch_ms" in item:
            item["end_ms"] = float(item["end_epoch_ms"]) - epoch0
        if "point_ms" not in item:
            item["point_ms"] = float(item.get("start_ms", 0.0))
        for key in ("start_ms", "point_ms", "end_ms"):
            if key in item:
                item[key] = float(np.clip(item[key], 0.0, float(motion.duration_ms)))
        result.append(item)
    return result


def predict_behavior(code: str, source: str | Path, model_dir: str | Path, *, threshold: float | None = None) -> dict[str, Any]:
    """Run one clean algorithm and return review candidates.

    Model files remain under the configured external model home. The returned
    result is deliberately in-memory so callers decide where to persist a run.
    """
    spec_for(code)
    raw = Path(source).resolve()
    root = Path(model_dir).resolve()
    motion, acc, gyro = _motion(raw)
    if code == "STANDING_UP":
        from .behavior.standup.detector import StandupDetector
        events = StandupDetector(root).detect(motion.times_ms, acc, gyro)
    elif code == "LYING_DOWN":
        from .liedown import detect_motion_file
        model = json.loads((root / "model.json").read_text(encoding="utf-8")) if root.is_dir() else json.loads(root.read_text(encoding="utf-8"))
        events = detect_motion_file(raw, model, threshold=threshold)
    elif code == "STRAINING_BOUT":
        from .straining.model import detect_file, load_bundle
        events = detect_file(load_bundle(root), raw)
    elif code == "URINATION":
        from .behavior.urination.detector import detect_file, load_bundle
        table, info = detect_file(raw, load_bundle(root), threshold)
        events = []
        for row in table.to_dict("records"):
            events.append(dict(code="URINATION", start_ms=float(row.get("start_s", 0)) * 1000,
                               end_ms=float(row.get("end_s", 0)) * 1000,
                               point_ms=float(row.get("event_time_s", 0)) * 1000,
                               score=float(row.get("score", 0)), info=info))
    elif code == "FETAL_PART_FIRST_VISIBLE":
        from .fpfv import detect_records
        model = json.loads((root / "model.json").read_text(encoding="utf-8")) if root.is_dir() else json.loads(root.read_text(encoding="utf-8"))
        events, _ = detect_records([motion], model, threshold=threshold)
    elif code == "CALF_FULLY_EXPELLED":
        from .behavior.calf_expelled import base_signals, feature_matrix, load_model, predict
        model_path = root / "model.json" if root.is_dir() else root
        model = load_model(model_path)
        sec = np.floor(motion.times_ms / 1000.0).astype(int)
        count = int(sec[-1]) + 1 if len(sec) else 0
        if not count:
            events = []
        else:
            acc_s = np.stack([np.nanmean(acc[sec == i], axis=0) if np.any(sec == i) else np.full(3, np.nan) for i in range(count)])
            gyr_s = np.stack([np.nanmean(gyro[sec == i], axis=0) if np.any(sec == i) else np.full(3, np.nan) for i in range(count)])
            sig = base_signals(acc_s, gyr=gyr_s)
            X, names = feature_matrix(sig)
            if names != model.get("features"):
                raise ValueError("CFE model feature order does not match current algorithm")
            configured = model.get("threshold") if threshold is None else threshold
            if configured is None:
                raise ValueError("CFE model is missing its calibrated threshold")
            result = predict(model, X, times_ms=np.arange(count, dtype=float) * 1000.0,
                             threshold=float(configured), duration_ms=float(motion.duration_ms))
            events = result["events"]
    else:
        raise KeyError(code)
    return dict(code=code, model_dir=str(root), source=str(raw), duration_ms=float(motion.duration_ms),
                candidates=_relative(list(events), motion, code),
                semantics="review_candidates_only")


__all__ = ["predict_behavior"]
