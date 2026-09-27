"""Data-driven pattern profile ("规律") learned from labelled urination events.

The profile is re-estimated every time the dataset grows. It provides
1) human-readable statistics of the Lift-Hold-Return pattern per cow,
2) an interpretable rule baseline, and
3) adapted candidate-generator parameters so recall keeps up with new data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

KEY = ["dur_s", "peak_g", "hold_med_g", "hold_frac50", "lift_deg", "rise_s", "fall_s", "return_ratio",
       "post_deg", "gyro_pre", "gyro_hold", "gyro_onset_max", "gyro_offset_max", "dyn_hold", "lift_dir_consistency",
       "lift_dir_x", "lift_dir_y", "lift_dir_z", "ref_uy", "mag_hold_deg"]

# (feature, side, quantile, slack) - a candidate must look like >= 95 % of known urinations
RULES = [
    ("dur_s", "ge", 0.02, 0.8), ("dur_s", "le", 0.98, 1.5),
    ("peak_g", "ge", 0.05, 0.8), ("hold_frac50", "ge", 0.05, 0.8),
    ("return_ratio", "le", 0.95, 1.2), ("gyro_hold", "le", 0.95, 1.2),
    ("dyn_hold", "le", 0.95, 1.2), ("lift_dir_consistency", "ge", 0.05, 0.9),
]


def build_profile(cand: pd.DataFrame) -> dict:
    pos = cand[cand.role.eq("P")]
    # one row per event: the best-matching (longest) candidate
    pos = pos.sort_values("dur_s").groupby("event_id").tail(1)
    qs = [0.02, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.98]
    overall = {k: {str(q): float(pos[k].quantile(q)) for q in qs} for k in KEY if k in pos}
    per_cow = {}
    for cow, g in pos.groupby("cow"):
        per_cow[str(cow)] = dict(events=int(len(g)), **{k: float(g[k].median()) for k in KEY if k in g})
    rules = []
    for feat, side, q, slack in RULES:
        v = float(pos[feat].quantile(q))
        rules.append(dict(feature=feat, side=side, value=v * slack if side == "ge" else v * slack))
    neg = cand[cand.role.eq("N")]
    confusers = {}
    for code, g in neg[neg.other_code.ne("")].groupby("other_code"):
        confusers[code] = dict(candidates=int(len(g)), **{k: float(g[k].median()) for k in ("dur_s", "peak_g", "gyro_hold", "return_ratio", "lift_deg")})
    p5 = float(pos["peak_g"].quantile(0.05)) if len(pos) else 0.2
    return dict(events=int(len(pos)), cows=int(pos.cow.nunique()), overall=overall, per_cow=per_cow, rules=rules,
                confusers=confusers, recommended_params=dict(on_g=float(np.clip(0.6 * p5, 0.05, 0.10))))


def rule_score(cand: pd.DataFrame, profile: dict) -> np.ndarray:
    ok = np.zeros(len(cand))
    for r in profile["rules"]:
        x = cand[r["feature"]].to_numpy(float)
        passed = x >= r["value"] if r["side"] == "ge" else x <= r["value"]
        ok += np.where(np.isfinite(x), passed, 0)
    return ok / len(profile["rules"])
