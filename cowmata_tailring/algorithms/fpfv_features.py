"""FPFV feature extraction on a continuous per-second tail-IMU series (causal, multi-scale).

Physiology encoded (see research_notes.md):
  * Stage-II onset (first fetal part visible) coincides with a *sustained, held* tail
    elevation (tail lifted 30-80 deg and kept there for minutes) rather than the brief
    raises of urination/defecation or the flat, motionless horizontal tail of lying.
  * Straining bouts cluster from ~-30 to +45 min, tail sensor temperature dips (wet /
    exposed tail) and restlessness increases over the preceding hours.
All windows are trailing (causal) so the same code can run on-line.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURE_VERSION = "fpfv-tail-hold-3"
SCALES = (30, 120, 300, 900)


def unit(v):
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-9)


def base_signals(s):
    u = unit(s["acc_mean"])
    elev = np.degrees(np.arccos(np.clip(-u[:, 1], -1, 1)))
    roll = np.arctan2(u[:, 0], u[:, 2])
    act = np.linalg.norm(s["gyr_sd"], axis=1)
    dyn = np.linalg.norm(s["acc_sd"], axis=1)
    temp = np.full(len(s["epoch_s"]), np.nan)
    if len(s["temp_ms"]) > 1:
        t = s["temp_ms"] / 1000
        idx = np.searchsorted(t, s["epoch_s"], side="right") - 1
        ok = (idx >= 0) & (s["epoch_s"] - t[np.clip(idx, 0, None)] < 180)
        temp[ok] = s["temp_c"][idx[ok]]
    return dict(elev=elev, roll_c=np.cos(roll), roll_s=np.sin(roll), act=act, dyn=dyn,
                jerk=s["jerk"].astype(float), temp=temp, valid=s["valid"].astype(bool))


def _run_length(mask):
    """Length (s) of the current run of True ending at each index."""
    out = np.zeros(len(mask), dtype=float)
    run = 0
    for i, m in enumerate(mask):
        run = run + 1 if m else 0
        out[i] = run
    return out


def features(s, step=10, rows=None):
    b = base_signals(s)
    n = len(b["elev"])
    v = b["valid"]
    df = pd.DataFrame({k: np.where(v, b[k], np.nan) for k in ("elev", "act", "dyn", "jerk", "roll_c", "roll_s")})
    df["temp"] = b["temp"]
    e = df["elev"]
    hold = ((e >= 30) & (e <= 85) & (df["act"] > 0.6)).astype(float).where(v)
    raised = (e > 45).astype(float).where(v)
    horiz_still = ((e > 70) & (df["act"] < 0.6)).astype(float).where(v)
    still = (df["act"] < 0.6).astype(float).where(v)
    up = ((e > 45) & (e.shift(1) <= 45)).astype(float).where(v)
    df["hold"], df["raised"], df["horiz_still"], df["still"], df["up"] = hold, raised, horiz_still, still, up
    cols = {}
    for w in SCALES:
        r = df.rolling(w, min_periods=int(w * .5))
        med = r[["elev", "act"]].median()
        mean = r[["elev", "act", "dyn", "jerk", "hold", "raised", "horiz_still", "still", "roll_c", "roll_s"]].mean()
        cols[f"elev_med_{w}"] = med["elev"]
        cols[f"elev_sd_{w}"] = r["elev"].std()
        cols[f"act_med_{w}"] = med["act"]
        cols[f"act_mean_{w}"] = mean["act"]
        cols[f"dyn_mean_{w}"] = mean["dyn"]
        cols[f"jerk_mean_{w}"] = mean["jerk"]
        for k in ("hold", "raised", "horiz_still", "still"):
            cols[f"{k}_frac_{w}"] = mean[k]
        cols[f"roll_R_{w}"] = np.sqrt(mean["roll_c"] ** 2 + mean["roll_s"] ** 2)
        cols[f"raise_onsets_{w}"] = df["up"].rolling(w, min_periods=int(w * .5)).mean() * 60  # per observed minute
    # long context (restlessness & posture changes over hours)
    for w in (3600, 3 * 3600):
        r = df.rolling(w, min_periods=int(w * .3))
        cols[f"act_mean_{w}"] = r["act"].mean()
        cols[f"raised_frac_{w}"] = r["raised"].mean()
        cols[f"hold_frac_{w}"] = r["hold"].mean()
        cols[f"raise_onsets_{w}"] = df["up"].rolling(w, min_periods=int(w * .3)).mean() * 60
        cols[f"horiz_still_frac_{w}"] = r["horiz_still"].mean()
    base_act = df["act"].rolling(12 * 3600, min_periods=3600).median()
    cols["act_ratio_1h_12h"] = np.log((cols["act_mean_3600"] + 1) / (base_act + 1))
    cols["act_ratio_5m_1h"] = np.log((cols["act_mean_300"] + 1) / (cols["act_mean_3600"] + 1))
    cols["hold_ratio_5m_1h"] = cols["hold_frac_300"] - cols["hold_frac_3600"]
    # sustained hold: current continuous run of held elevation (tolerate short dips via 30s median)
    hold_s = (cols["elev_med_30"].between(30, 85) & (cols["act_med_30"] > 0.6)).to_numpy()
    cols["hold_run_s"] = pd.Series(_run_length(hold_s))
    cols["hold_run_max_900"] = cols["hold_run_s"].rolling(900, min_periods=1).max()
    # temperature: dip vs recent baseline and short slope
    t = df["temp"]
    tb1 = t.rolling(3600, min_periods=600).median()
    tb6 = t.rolling(6 * 3600, min_periods=1800).median()
    cols["temp_d1h"] = t - tb1
    cols["temp_d6h"] = t - tb6
    cols["temp_slope_10m"] = t - t.shift(600)
    # straining-like rhythmic pulses: autocorrelation of act envelope over 60 s
    a = df["act"].fillna(df["act"].median())
    for lag in (3, 6, 10):
        cols[f"act_ac{lag}_60"] = a.rolling(60, min_periods=40).corr(a.shift(lag))
    # rhythmic straining lifts the tail every ~0.5-2 min: elevation autocorrelation over 5 min
    ef = e.interpolate(limit=20).fillna(e.median())
    for lag in (30, 60, 90):
        cols[f"elev_ac{lag}_300"] = ef.rolling(300, min_periods=200).corr(ef.shift(lag))
    # ---- per-animal normalisation: tail carriage and activity differ strongly between cows ----
    nmin = n // 60
    if nmin >= 10:
        em = np.nanmedian(np.where(v, b["elev"], np.nan)[:nmin * 60].reshape(nmin, 60), axis=1)
        am = np.nanmedian(np.where(v, b["act"], np.nan)[:nmin * 60].reshape(nmin, 60), axis=1)
        be = pd.Series(em).rolling(1440, min_periods=120).median().bfill().to_numpy()
        ba = pd.Series(am).rolling(1440, min_periods=120).median().bfill().to_numpy()
        base_e = np.r_[np.repeat(be, 60), np.full(n - nmin * 60, be[-1])]
        base_a = np.r_[np.repeat(ba, 60), np.full(n - nmin * 60, ba[-1])]
    else:
        base_e = np.full(n, np.nanmedian(b["elev"]))
        base_a = np.full(n, np.nanmedian(b["act"]))
    base_e = np.where(np.isfinite(base_e), base_e, 20.0)
    base_a = np.where(np.isfinite(base_a), base_a, 3.0)
    rel = e - base_e
    hold_rel = ((rel > 20) & (df["act"] > 0.6)).astype(float).where(v)
    up_rel = ((rel > 25) & (rel.shift(1) <= 25)).astype(float).where(v)
    for w in (120, 300, 900, 3600):
        cols[f"elev_rel_{w}"] = rel.rolling(w, min_periods=int(w * .4)).median()
        cols[f"hold_rel_frac_{w}"] = hold_rel.rolling(w, min_periods=int(w * .4)).mean()
        cols[f"up_rel_rate_{w}"] = up_rel.rolling(w, min_periods=int(w * .4)).mean() * 60
        cols[f"act_rel_{w}"] = np.log((df["act"].rolling(w, min_periods=int(w * .4)).mean() + 1) / (base_a + 1))
    cols["base_elev_24h"] = pd.Series(base_e)
    X = pd.DataFrame(cols)
    cov = pd.Series(v.astype(float)).rolling(300, min_periods=1).mean().to_numpy()
    if rows is None:
        idx = np.arange(0, n, step)
        rows = idx[(cov[idx] >= .5) & v[idx]]
    return X.iloc[rows].to_numpy(np.float32), s["epoch_s"][rows], list(X.columns), rows


def _reverse(s):
    r = {k: (v[::-1].copy() if isinstance(v, np.ndarray) and len(v) == len(s["epoch_s"]) else v) for k, v in s.items()}
    r["epoch_s"] = -s["epoch_s"][::-1]
    if len(s["temp_ms"]):
        r["temp_ms"] = -s["temp_ms"][::-1]
        r["temp_c"] = s["temp_c"][::-1]
    return r


FUTURE_KEYS = ("elev_med", "elev_sd", "act_mean", "hold_frac", "raised_frac", "raise_onsets", "horiz_still_frac", "dyn_mean", "elev_ac", "temp_slope",
               "elev_rel", "hold_rel_frac", "up_rel_rate", "act_rel")


def features_bidir(s, step=10):
    """Offline (annotation) mode: past windows, mirrored future windows and future-minus-past contrasts."""
    Xp, tp, names, rows = features(s, step)
    n = len(s["epoch_s"])
    Xf_al, _, _, _ = features(_reverse(s), step, rows=(n - 1 - rows))
    sel = [i for i, n in enumerate(names) if n.startswith(FUTURE_KEYS) and not n.endswith(("_3600", "_10800"))]
    fut = Xf_al[:, sel]
    diff = fut - Xp[:, sel]
    X = np.hstack([Xp, fut, diff]).astype(np.float32)
    return X, tp, names + ["fut_" + names[i] for i in sel] + ["dfp_" + names[i] for i in sel]
