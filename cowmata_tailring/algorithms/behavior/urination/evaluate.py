"""Event-level evaluation with cow-grouped (leave-one-cow-out) validation."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import EVENT_CODE
from .dataset import match_rule, overlap


def assign_folds(cand: pd.DataFrame, events: pd.DataFrame, sessions: pd.DataFrame):
    """One fold per urination cow; cows without urination labels are spread round-robin."""
    uri_cows = sorted(events.loc[events.code == EVENT_CODE, "cow"].unique())
    other = sorted(set(sessions.cow) - set(uri_cows))
    fold = {c: i for i, c in enumerate(uri_cows)}
    for j, c in enumerate(other):
        fold[c] = j % max(1, len(uri_cows))
    return fold, uri_cows


def nms_keep(cand: pd.DataFrame, score: np.ndarray, thr: float) -> np.ndarray:
    """Overlapping detections (parent/child candidates) collapse to the best-scoring one."""
    keep = np.zeros(len(cand), bool)
    above = np.flatnonzero(np.asarray(score) >= thr)
    if not len(above):
        return keep
    sub = cand.iloc[above][["asset_id", "start_s", "end_s"]].assign(score=np.asarray(score)[above], pos=above)
    for _, g in sub.groupby("asset_id"):
        kept = []
        for r in g.sort_values("score", ascending=False).itertuples():
            if all(r.end_s <= a or r.start_s >= b for a, b in kept):
                kept.append((r.start_s, r.end_s))
                keep[r.pos] = True
    return keep


def event_metrics(cand: pd.DataFrame, score: np.ndarray, thr: float, events: pd.DataFrame, sessions: pd.DataFrame,
                  scope: pd.Series | None = None):
    """Recall on labelled urination; unmatched detections in labelled sessions are
    *unverified* alarms (upper bound for false alarms, lower bound for precision)."""
    labelled = sessions.loc[sessions.n_events > 0] if scope is None else sessions.loc[scope]
    aids = set(labelled.asset_id)
    ev = events[(events.code == EVENT_CODE) & events.asset_id.isin(aids)]
    det = cand.assign(score=score)
    det = det[nms_keep(cand, score, thr) & det.asset_id.isin(aids).to_numpy()]
    det_by = {a: g for a, g in det.groupby("asset_id")}
    ev_by = {a: g for a, g in ev.groupby("asset_id")}
    tp_events, matched_det, partial_det = set(), set(), set()
    onset_err = []
    for aid, g in ev_by.items():
        d = det_by.get(aid)
        if d is None:
            continue
        for e in g.itertuples():
            best = None
            for r in d.itertuples():
                ov = overlap(r.start_s, r.end_s, e.start_s, e.end_s)
                if ov <= 0:
                    continue
                if match_rule(ov, r.end_s - r.start_s, max(e.end_s - e.start_s, 1)):
                    matched_det.add(r.Index)
                    best = r if best is None or r.score > best.score else best
                else:
                    partial_det.add(r.Index)
            if best is not None:
                tp_events.add(e.event_id)
                onset_err.append(best.start_s - e.start_s)
    n_det = len(det)
    fp = n_det - len(matched_det) - len(partial_det - matched_det)
    hours = float(labelled.duration_s.sum()) / 3600
    tp = len(tp_events)
    recall = tp / len(ev) if len(ev) else np.nan
    precision = len(matched_det) / max(len(matched_det) + fp, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9) if len(ev) else np.nan
    per_cow = {}
    for cow, g in ev.groupby("cow"):
        per_cow[str(cow)] = dict(events=len(g), detected=int(g.event_id.isin(tp_events).sum()))
    err = np.abs(np.asarray(onset_err))
    return dict(threshold=float(thr), events=int(len(ev)), detected=int(tp), recall=float(recall),
                detections=int(n_det), unverified_alarms=int(fp), precision_lower_bound=float(precision),
                f1_lower_bound=float(f1), unverified_alarms_per_hour=fp / max(hours, 1e-9), hours=hours,
                onset_abs_err_median_s=float(np.median(err)) if len(err) else None,
                onset_abs_err_p90_s=float(np.quantile(err, 0.9)) if len(err) else None, per_cow=per_cow)


def best_threshold(cand, score, events, sessions, grid=None, min_recall=0.0):
    grid = np.round(np.arange(0.05, 0.96, 0.025), 3) if grid is None else grid
    best = None
    for t in grid:
        m = event_metrics(cand, score, t, events, sessions)
        if m["recall"] < min_recall:
            continue
        if best is None or m["f1_lower_bound"] > best["f1_lower_bound"] + 1e-9:
            best = m
    return best
