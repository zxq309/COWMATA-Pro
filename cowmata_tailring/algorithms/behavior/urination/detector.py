"""Model bundle loading and end-to-end detection on one raw recording."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import EVENT_CODE, SCHEMA
from .candidates import DEFAULT_PARAMS, add_children, generate
from .evaluate import nms_keep
from .features import add_session_relative, candidate_features, session_stats
from .forest import predict_forest
from .io import load_recording
from .signal import summarize


def load_bundle(folder) -> dict:
    folder = Path(folder)
    doc = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise ValueError("不是排尿算法模型包")
    blob = (folder / doc["model_file"]).read_bytes()
    if hashlib.sha256(blob).hexdigest() != doc["model_sha256"]:
        raise ValueError("模型文件校验失败")
    doc["model"] = json.loads(blob)
    doc["root"] = str(folder)
    return doc


def score_candidates(rows: pd.DataFrame, bundle: dict) -> np.ndarray:
    if not len(rows):
        return np.zeros(0)
    feats = bundle["model"]["features"]
    return predict_forest(bundle["model"], rows.reindex(columns=feats).to_numpy(np.float32))


def detect_recording(rec, bundle: dict, threshold: float | None = None) -> tuple[pd.DataFrame, dict]:
    sec = summarize(rec)
    p = {**DEFAULT_PARAMS, **bundle.get("params", {})}
    cands = add_children(generate(sec.acc, sec.valid, p, sec.gyro), sec.acc, sec.gyro, sec.valid)
    stats = session_stats(sec)
    rows = add_session_relative(pd.DataFrame([dict(start_s=float(c.start), end_s=float(c.end + 1), **candidate_features(c, sec, p["ref_s"], p["ref_gap_s"], stats)) for c in cands]))
    thr = float(bundle["threshold"] if threshold is None else threshold)
    score = score_candidates(rows, bundle)
    info = dict(duration_s=len(sec), valid_s=int(sec.valid.sum()), candidates=len(rows), calibration=sec.calibration,
                device=rec.device, create_time_ms=rec.create_time_ms, threshold=thr, model_version=bundle.get("version"))
    if not len(rows):
        return pd.DataFrame(columns=["start_s", "end_s", "event_time_s", "score", "code"]), info
    rows["score"] = score
    rows["asset_id"] = str(rec.path)
    keep = nms_keep(rows, score, thr)
    out = rows.loc[keep, ["start_s", "end_s", "score", "dur_s", "still_run_s", "lift_deg", "gyro_hold", "return_ratio"]].copy()
    out = out.sort_values("start_s")
    out["event_time_s"] = out.start_s + np.minimum((out.end_s - out.start_s) / 2, 15)
    out["code"] = EVENT_CODE
    if rec.create_time_ms:
        out["start_unix_ms"] = rec.create_time_ms + 1000 * out.start_s
    out["requires_video_confirmation"] = bool(sec.calibration.get("suspect"))
    info["detections"] = int(len(out))
    return out.reset_index(drop=True), info


def detect_file(path, bundle: dict, threshold: float | None = None):
    return detect_recording(load_recording(path), bundle, threshold)
