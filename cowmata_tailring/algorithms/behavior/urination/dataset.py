"""Scan the COWMATA behavior dataset and build labelled candidate tables.

Rules (identical to the app's shared-label contract):
* every ``*/Motion/Label/*_label.json`` is read, events are de-duplicated by
  (asset id, code, t0, t1) because one recording may sit in several folders;
* sessions without any reviewed event are *unknown*, never negatives;
* inside labelled sessions, candidates not overlapping a urination label are
  background, but they are weighted lower (``unknown_weight``) because the
  annotators may not have marked every urination.
"""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from . import ALGORITHM_VERSION, EVENT_CODE
from .candidates import DEFAULT_PARAMS, add_children, generate
from .features import add_session_relative, candidate_features, feature_names, session_stats
from .io import load_recording
from .signal import summarize




def scan_dataset(root: Path):
    root = Path(root)
    sessions, events, seen = {}, [], set()
    for label in sorted(root.glob("*/Motion/Label/*_label.json")):
        folder = label.relative_to(root).parts[0]
        try:
            doc = json.loads(label.read_text(encoding="utf-8"))
            proj = doc["work"]["project"]
            src = proj["source"]
        except (OSError, ValueError, KeyError):
            continue
        aid = src["asset_id"]
        raw = root / src.get("path", "")
        if not raw.is_file():
            raw = label.parent.parent / "Raw" / label.name.replace("_label.json", "_raw.json")
        ident = proj.get("device_identity") or {}
        s = sessions.setdefault(aid, dict(asset_id=aid, raw=str(raw), cow=str(proj.get("cow_id") or ident.get("cow_id") or ""),
                                          device=src.get("device") or ident.get("device_id") or "", folders=[],
                                          category=proj.get("dataset_category"), duration_s=(src.get("durationMs") or 0) / 1000,
                                          create_time_ms=src.get("createTimeMs"), n_events=0))
        if folder not in s["folders"]:
            s["folders"].append(folder)
        for e in proj.get("events", []):
            key = (aid, e.get("label_code"), e.get("t0"), e.get("t1"))
            if key in seen or e.get("t0") is None:
                continue
            seen.add(key)
            s["n_events"] += 1
            t1 = e.get("t1") if e.get("t1") is not None else e.get("t0")
            events.append(dict(event_id=hashlib.sha1(repr(key).encode()).hexdigest()[:12], asset_id=aid, cow=s["cow"],
                               code=e.get("label_code"), start_s=float(e["t0"]) / 1000, end_s=float(t1) / 1000,
                               confirmation=e.get("confirmation", "")))
    ev = pd.DataFrame(events, columns=["event_id", "asset_id", "cow", "code", "start_s", "end_s", "confirmation"])
    if len(ev):
        ev = _merge_overlapping(ev)
    return pd.DataFrame(sessions.values(), columns=["asset_id", "raw", "cow", "device", "folders", "category", "duration_s", "create_time_ms", "n_events"]), ev


def _merge_overlapping(ev: pd.DataFrame) -> pd.DataFrame:
    """The same act labelled twice (legacy + reviewed copy) becomes one event."""
    keep = []
    for (_, _), g in ev.sort_values("start_s").groupby(["asset_id", "code"], sort=False):
        cur = None
        for r in g.to_dict("records"):
            if cur is not None and r["start_s"] < cur["end_s"] and r["end_s"] > cur["start_s"]:
                cur["end_s"] = max(cur["end_s"], r["end_s"])
                cur["start_s"] = min(cur["start_s"], r["start_s"])
                cur["merged"] = cur.get("merged", 1) + 1
                continue
            if cur is not None:
                keep.append(cur)
            cur = dict(r, merged=1)
        keep.append(cur)
    return pd.DataFrame(keep).reset_index(drop=True)


def _params_key(params):
    blob = json.dumps({**DEFAULT_PARAMS, **(params or {})}, sort_keys=True) + ALGORITHM_VERSION
    return hashlib.sha1(blob.encode()).hexdigest()[:10]


def session_candidates(raw_path, params=None):
    rec = load_recording(raw_path)
    sec = summarize(rec)
    p = {**DEFAULT_PARAMS, **(params or {})}
    cands = add_children(generate(sec.acc, sec.valid, p, sec.gyro), sec.acc, sec.gyro, sec.valid)
    stats = session_stats(sec)
    rows = []
    for c in cands:
        f = candidate_features(c, sec, p["ref_s"], p["ref_gap_s"], stats)
        rows.append(dict(start_s=float(c.start), end_s=float(c.end + 1), **f))
    rows = add_session_relative(rows).to_dict("records") if rows else rows
    meta = dict(duration_s=len(sec), valid_s=int(sec.valid.sum()), calibration=sec.calibration, device=rec.device,
                create_time_ms=rec.create_time_ms)
    return rows, meta


def _worker(args):
    aid, raw, params, cache = args
    out = Path(cache) / f"{aid}_{_params_key(params)}.json"
    if out.is_file():
        return aid, json.loads(out.read_text(encoding="utf-8"))
    try:
        rows, meta = session_candidates(raw, params)
        doc = dict(rows=rows, meta=meta)
    except Exception as exc:  # recorded per session, never fatal for the dataset
        doc = dict(rows=[], meta=dict(error=repr(exc)))
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, default=float), encoding="utf-8")
    os.replace(tmp, out)
    return aid, doc


def build_candidates(sessions: pd.DataFrame, params=None, cache: Path | None = None, workers: int = 16, progress=None):
    if cache is None:
        raise ValueError("An external cache directory is required")
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    jobs = [(r.asset_id, r.raw, params, str(cache)) for r in sessions.itertuples()]
    frames, metas = [], {}
    with ProcessPoolExecutor(max(1, workers)) as ex:
        for i, (aid, doc) in enumerate(ex.map(_worker, jobs, chunksize=2)):
            metas[aid] = doc["meta"]
            if doc["rows"]:
                df = pd.DataFrame(doc["rows"])
                df.insert(0, "asset_id", aid)
                frames.append(df)
            if progress:
                progress(i + 1, len(jobs))
    cols = ["asset_id", "start_s", "end_s"] + feature_names()
    cand = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)
    return cand, metas


def overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def match_rule(ov, dur_a, dur_b, min_frac=0.5):
    return ov >= min_frac * min(dur_a, dur_b)


def label_candidates(cand: pd.DataFrame, sessions: pd.DataFrame, events: pd.DataFrame, unknown_weight=0.5):
    """Attach role / event id / hard-negative code and training weights."""
    cand = cand.copy()
    by = {aid: g for aid, g in events.groupby("asset_id")} if len(events) else {}
    cow = dict(zip(sessions.asset_id, sessions.cow))
    labelled = dict(zip(sessions.asset_id, sessions.n_events > 0))
    has_uri = set(events.loc[events.code == EVENT_CODE, "asset_id"]) if len(events) else set()
    role, eid, other = [], [], []
    for r in cand.itertuples():
        g = by.get(r.asset_id)
        rl, ei, oc = "U", "", ""
        if labelled.get(r.asset_id):
            rl = "N"
            if g is not None:
                best = 0.0
                for e in g.itertuples():
                    ov = overlap(r.start_s, r.end_s, e.start_s, e.end_s)
                    if e.code == EVENT_CODE and ov > 0:
                        if match_rule(ov, r.end_s - r.start_s, max(e.end_s - e.start_s, 1)):
                            rl, ei = "P", e.event_id
                        elif rl != "P":
                            rl = "IGNORE"
                    elif e.code != EVENT_CODE and ov > best:
                        best, oc = ov, e.code
        role.append(rl)
        eid.append(ei)
        other.append(oc)
    cand["cow"] = cand.asset_id.map(cow)
    cand["role"], cand["event_id"], cand["other_code"] = role, eid, other
    cand["session_has_urination"] = cand.asset_id.isin(has_uri)
    y = cand.role.eq("P").astype(int)
    w = np.where(cand.role.eq("P"), 1.0, np.where(cand.other_code.ne(""), 1.0, unknown_weight))
    w = np.where(cand.session_has_urination & cand.role.eq("N"), np.maximum(w, 0.8), w)
    cand["y"], cand["w"] = y, w
    pos = cand.role.eq("P")
    if pos.any():
        per_event = cand.loc[pos].groupby("event_id").event_id.transform("size")
        cand.loc[pos, "w"] = 1.0 / per_event
        # balance: positive mass equals background mass
        neg = cand.role.eq("N")
        cand.loc[pos, "w"] *= cand.loc[neg, "w"].sum() / max(cand.loc[pos, "w"].sum(), 1e-9)
    return cand
