"""4.4.6: time-resolved feature salience before calving and per-horizon adaptive feature weights.

Which signal matters depends on how close calving is: straining and tail raising dominate the last hours,
posture changes and lying bouts the last 6–12 h, temperature and rumination-like trends the day before. The
salience profile measures this from the data instead of assuming it:

* every model input column is compared, per time bin before calving onset (0–1, 1–2, 2–3, 3–6, 6–12, 12–24,
  24–48 h), with the cow-hours far from calving (> 48 h): discrimination (AUC) and a robust effect size;
* each prediction horizon h (1/2/3/6/12 h) weights the columns by their discrimination in the bins it covers
  (bins ending within h, weighted by rows), with small bins shrunk towards "no effect", and keeps the strongest
  columns with their direction;
* the weighted, signed, robust-z composite ``salience@<h>h`` becomes a model input of every horizon model.
  Inside cross-validation it is cross-fitted by cow (weights of a fold never see that fold's labels).

The full-data profile, weights and reference statistics are stored in the model manifest (``salience``) so that
inference computes the same composite, and the reverse/forward reports can show which features carry each horizon.
"""
from __future__ import annotations

import numpy as np

BINS = ((0, 1), (1, 2), (2, 3), (3, 6), (6, 12), (12, 24), (24, 48))
BIN_TITLES = ("0–1h", "1–2h", "2–3h", "3–6h", "6–12h", "12–24h", "24–48h")
REFERENCE_MIN_H = 48.0
TOP = 12            # columns kept per horizon
MARGIN = 0.02       # |AUC − 0.5| below this counts as no effect
SHRINK_ROWS = 30    # bins with few rows are shrunk towards no effect
MAX_REFERENCE = 20000
BEHAVIOUR = {"activity", "lying_ratio", "straining_ratio", "behavior_events", "gyro_spectral_entropy"}
METHOD = ("按产犊开始前的时段（0–1、1–2、2–3、3–6、6–12、12–24、24–48 小时）把每个输入特征与远离产犊（>48 小时）的时段比较："
          "区分度 AUC 与稳健效应量（中位数差 / 稳健标准差）。每个提前量只用它覆盖的时段、按行数加权取区分度，样本少的时段向无效应收缩，"
          "保留最强的特征及其方向，合成该提前量的显著性分数 salience@<h>h 作为模型输入；交叉验证中按牛交叉拟合，"
          "每一折的权重不使用该折的标签。")


def column_name(h):
    return f"salience@{int(h)}h"


def _auc(pos, neg):
    """Mann–Whitney AUC of ``pos`` over ``neg`` (ties count half)."""
    from scipy.stats import rankdata

    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    ranks = rankdata(np.concatenate([pos, neg]))
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def _robust(values):
    median = float(np.median(values))
    scale = float(np.subtract(*np.percentile(values, [75, 25])) / 1.349)
    if not np.isfinite(scale) or scale <= 1e-9:
        scale = float(np.std(values)) or 1.0
    return median, scale


def profile(x, hours, columns, *, seed=446):
    """Per column: reference median/scale and, per bin, rows, AUC and robust effect size."""
    x = np.asarray(x, float)
    hours = np.asarray(hours, float)
    reference = np.flatnonzero(hours > REFERENCE_MIN_H)
    if len(reference) > MAX_REFERENCE:
        reference = np.sort(np.random.default_rng(seed).choice(reference, MAX_REFERENCE, replace=False))
    bins = [np.flatnonzero((hours > lo) & (hours <= hi)) for lo, hi in BINS]
    stats = {}
    for j, column in enumerate(columns):
        ref = x[reference, j]
        ref = ref[np.isfinite(ref)]
        if len(ref) < 50:
            continue
        median, scale = _robust(ref)
        aucs, effects, counts = [], [], []
        for idx in bins:
            values = x[idx, j]
            values = values[np.isfinite(values)]
            counts.append(int(len(values)))
            if len(values) < 3:
                aucs.append(0.5)
                effects.append(0.0)
                continue
            aucs.append(_auc(values, ref))
            effects.append(float((np.median(values) - median) / scale))
        stats[column] = dict(median=median, scale=scale, auc=aucs, effect=effects, n=counts)
    return stats


def weights(stats, horizons, *, top=TOP):
    """{h: {column: [weight, sign]}}: discrimination within the bins a horizon covers, strongest columns only."""
    result = {}
    for h in horizons:
        covered = [b for b, (_, hi) in enumerate(BINS) if hi <= h] or [0]
        scores = {}
        for column, s in stats.items():
            n = np.asarray([s["n"][b] for b in covered], float)
            if n.sum() <= 0:
                continue
            delta = np.asarray([s["auc"][b] - 0.5 for b in covered])
            shrink = n / (n + SHRINK_ROWS)
            strength = np.maximum(np.abs(delta) * shrink - MARGIN, 0.0)
            score = float((n * strength).sum() / n.sum())
            if score > 0:
                scores[column] = (score, 1.0 if float((n * delta).sum()) >= 0 else -1.0)
        best = sorted(scores.items(), key=lambda kv: -kv[1][0])[:top]
        total = sum(v[0] for _, v in best)
        result[str(int(h))] = {c: [round(v[0] / total, 6), v[1]] for c, v in best} if total else {}
    return result


def composite(x, columns, stats, weight):
    """Weighted, signed, clipped robust-z composite of one horizon for every row of ``x`` (NaN when no input)."""
    x = np.asarray(x, float)
    index = {c: j for j, c in enumerate(columns)}
    total = np.zeros(len(x))
    used = np.zeros(len(x))
    for column, (w, sign) in weight.items():
        j = index.get(column)
        if j is None or column not in stats:
            continue
        z = np.clip((x[:, j] - stats[column]["median"]) / stats[column]["scale"], -5.0, 5.0)
        ok = np.isfinite(z)
        total[ok] += w * sign * z[ok]
        used[ok] += w
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(used > 0, total / np.where(used > 0, used, 1.0), np.nan)


def cross_fit(x, hours, columns, split, horizons):
    """Out-of-fold composites (rows × horizons) and the full-data (stats, weights) used for deployment."""
    horizons = [int(h) for h in horizons]
    out = np.full((len(x), len(horizons)), np.nan)
    for train, test in split:
        stats = profile(x[train], hours[train], columns)
        w = weights(stats, horizons)
        for k, h in enumerate(horizons):
            out[test, k] = composite(x[test], columns, stats, w[str(h)])
    stats = profile(x, hours, columns)
    return out, stats, weights(stats, horizons)


def manifest(stats, w, column_keys, titles):
    """The ``salience`` block of a model manifest: what inference needs plus the profile shown in reports."""
    used = sorted({c for horizon in w.values() for c in horizon})
    by_key = {}
    for column, s in stats.items():
        key = column_keys.get(column)
        if not key or key == "context":
            continue
        strength = max(abs(a - 0.5) for a in s["auc"])
        if key not in by_key or strength > by_key[key][0]:
            by_key[key] = (strength, column, s)
    features = []
    for key, (_, column, s) in sorted(by_key.items(), key=lambda kv: -kv[1][0]):
        peak = int(np.argmax([abs(a - 0.5) for a in s["auc"]]))
        features.append(dict(key=key, title=titles.get(key, key), group="行为" if key in BEHAVIOUR else "生理",
                             column=column, effect=[round(e, 3) for e in s["effect"]],
                             auc=[round(a, 3) for a in s["auc"]], n=s["n"], peak=BIN_TITLES[peak]))
    horizon_weights = {}
    for h, items in w.items():
        shares = {}
        for column, (weight, _) in items.items():
            key = column_keys.get(column) or "context"
            shares[key] = shares.get(key, 0.0) + weight
        horizon_weights[h] = {k: round(v, 4) for k, v in sorted(shares.items(), key=lambda kv: -kv[1])}
    return dict(schema="cowmata-salience-4.4.6", bins=list(BIN_TITLES), method=METHOD,
                reference_min_hours=REFERENCE_MIN_H,
                columns={c: [round(stats[c]["median"], 6), round(stats[c]["scale"], 6)] for c in used},
                weights=w, features=features, horizon_weights=horizon_weights)


def apply(rows, block):
    """Add ``salience@<h>h`` to decision rows (dicts) from a manifest's ``salience`` block."""
    if not block:
        return rows
    reference = {c: dict(median=v[0], scale=v[1]) for c, v in block.get("columns", {}).items()}
    for h, items in block.get("weights", {}).items():
        name = column_name(h)
        for row in rows:
            total = used = 0.0
            for column, (w, sign) in items.items():
                value, ref = row.get(column), reference.get(column)
                if value is None or ref is None:
                    continue
                try:
                    z = (float(value) - ref["median"]) / ref["scale"]
                except (TypeError, ValueError):
                    continue
                if not np.isfinite(z):
                    continue
                total += w * sign * min(5.0, max(-5.0, z))
                used += w
            row[name] = total / used if used > 0 else None
    return rows
