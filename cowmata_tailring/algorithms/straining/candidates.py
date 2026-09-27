"""Bout-level evidence for stage-2 re-ranking of stage-1 candidates."""

from __future__ import annotations

import numpy as np

CONTEXT_COLUMNS = (
    "pulse_direction_R_24s",
    "quiet_prom_median_24s",
    "quiet_prom_cv_24s",
    "log_hf_mean_15s",
    "log_gyr95_max_31s",
    "posture_change_90s",
    "side_lying_index",
    "tail_elev_rel_600s",
    "loud_pulses_24s",
    "dev_mean_15s",
)


def candidate_names():
    names = [
        "duration_s",
        "log_duration",
        "quiet_pulses",
        "quiet_pulses_per_min",
        "ipi_median_s",
        "ipi_cv",
        "score_max",
        "score_mean",
        "score_p25",
        "neighbours_5min",
        "neighbours_15min",
        "nearest_neighbour_s",
    ]
    for col in CONTEXT_COLUMNS:
        names += [col + "_median", col + "_max"]
    return names


def candidate_features(feature, events, scores):
    """One row per candidate; `feature` is the output of signal.second_features."""
    names = feature["names"]
    X = feature["X"]
    pulses = feature["pulses"]
    qt = np.sort(pulses["t"][pulses["quiet"].astype(bool)]) if len(pulses["t"]) else np.zeros(0)
    mids = np.array([(e["start_ms"] + e["end_ms"]) / 2000 for e in events])
    rows = []
    for i, e in enumerate(events):
        a, b = (
            int(e["start_ms"] // 1000),
            max(int(e["end_ms"] // 1000), int(e["start_ms"] // 1000) + 1),
        )
        inside = qt[(qt >= a) & (qt <= b)]
        ipi = np.diff(inside)
        d = b - a
        near = np.abs(np.delete(mids, i) - mids[i]) if len(mids) > 1 else np.zeros(0)
        s = scores[a:b]
        row = [
            d,
            np.log1p(d),
            len(inside),
            len(inside) / max(d, 1) * 60,
            np.median(ipi) if len(ipi) else 30.0,
            np.std(ipi) / np.mean(ipi) if len(ipi) > 2 else 2.0,
            float(s.max()),
            float(s.mean()),
            float(np.percentile(s, 25)),
            float((near < 300).sum()),
            float((near < 900).sum()),
            float(near.min()) if len(near) else 3600.0,
        ]
        for col in CONTEXT_COLUMNS:
            v = X[a:b, names.index(col)]
            v = v[np.isfinite(v)]
            row += [float(np.median(v)), float(np.max(v))] if len(v) else [np.nan, np.nan]
        rows.append(row)
    return np.asarray(rows, dtype=np.float32).reshape(len(events), len(candidate_names()))
