"""Cow-grouped training, evaluation and visual diagnostics for the calving decision.

Honesty rules applied throughout:

* folds are split by cow (no cow appears in both training and validation);
* every reported score comes from the JSON model that would be deployed (``predict_model``);
* probabilities are calibrated by cross-fitted isotonic regression before Brier / ECE /
  threshold metrics are computed, and the final calibrator is fitted on out-of-fold scores;
* event-level metrics (detected calvings, lead time, false alerts per cow-day) are reported
  next to window-level metrics because a farm acts on alerts, not on single windows.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from .dataset import HORIZONS, column_key_map, model_columns, read_table, univariate
from .models import (
    ALGORITHMS,
    DEFAULT_ALGORITHMS,
    MODEL_SCHEMA,
    conformal_offset,
    fit_model,
    fit_time_to_event,
    importance,
    predict_model,
    predict_time_to_event,
)

HOUR_MS = 3_600_000
MANIFEST_SCHEMA = "cowmata-decision-4.3.3"
LEVELS = (
    (0.80, "临产", "12 小时内极可能产犊：安排专人看护，准备助产"),
    (0.50, "高度关注", "进入围产关注窗口：增加巡栏频次，复核视频与努责证据"),
    (0.25, "关注", "个体指标偏离本牛基线：留意后续变化"),
    (0.0, "正常", "未见明显产前信号"),
)


def _down(points, limit=200):
    if len(points) <= limit:
        return points
    index = np.unique(np.linspace(0, len(points) - 1, limit).astype(int))
    return [points[i] for i in index]


def cow_folds(groups, n_splits):
    """Deterministic, balanced cow folds (sorted cows dealt round-robin by row count)."""
    counts = defaultdict(int)
    for g in groups:
        counts[g] += 1
    order = sorted(counts, key=lambda c: (-counts[c], hashlib.sha1(str(c).encode()).hexdigest()))
    fold_of = {cow: i % n_splits for i, cow in enumerate(order)}
    assignment = np.asarray([fold_of[g] for g in groups])
    return [(np.flatnonzero(assignment != k), np.flatnonzero(assignment == k)) for k in range(n_splits)]


def _isotonic(score, y):
    from sklearn.isotonic import IsotonicRegression

    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(score, y)
    return dict(x=iso.X_thresholds_.tolist(), y=iso.y_thresholds_.tolist())


def _apply_cal(cal, score):
    return np.interp(score, cal["x"], cal["y"]) if cal else score


def cross_calibrate(score, y, folds):
    out = np.array(score, dtype=float)
    for train, test in folds:
        ok = np.isfinite(score[train])
        if len(set(y[train][ok])) == 2:
            out[test] = _apply_cal(_isotonic(score[train][ok], y[train][ok]), score[test])
    return out


def choose_threshold(prob, y):
    """Threshold maximising Youden's J (sensitivity + specificity − 1) on calibrated OOF probabilities.

    Youden's J does not depend on prevalence; F1 degenerates to "alarm always" when most monitored
    hours are already close to calving.
    """
    best, value = 0.5, -1.0
    pos, neg = max(1, int(np.sum(y == 1))), max(1, int(np.sum(y == 0)))
    for thr in np.round(np.arange(0.05, 0.951, 0.01), 2):
        pred = prob >= thr
        j = np.sum(pred & (y == 1)) / pos + np.sum(~pred & (y == 0)) / neg - 1
        if j > value:
            best, value = float(thr), float(j)
    return best


def scope_mask(rows, column_keys):
    """Keep rows inside each feature's own computed time span for that cow.

    Feature datasets may be computed only around some periods; outside that span a value is
    missing because nobody computed it, which would leak the outcome through missingness.
    """
    spans = {}
    by_key = defaultdict(list)
    for c, k in column_keys.items():
        if k not in (None, "context"):
            by_key[k].append(c)
    for row in rows:
        t = row["decision_epoch_ms"]
        for key, cols in by_key.items():
            if any(row.get(c) is not None for c in cols):
                lo, hi = spans.get((row["cow_id"], key), (t, t))
                spans[(row["cow_id"], key)] = (min(lo, t), max(hi, t))
    cow_spans = defaultdict(list)
    for (cow, _), span in spans.items():
        cow_spans[cow].append(span)
    keep = []
    for row in rows:
        t = row["decision_epoch_ms"]
        keep.append(all(lo <= t <= hi + HOUR_MS for lo, hi in cow_spans.get(row["cow_id"], ())))
    return np.asarray(keep, dtype=bool)


def window_metrics(y, prob, threshold, groups=None, *, bootstrap=200, seed=433):
    from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score

    ok = np.isfinite(prob)
    y, prob = y[ok], prob[ok]
    groups = np.asarray(groups)[ok] if groups is not None else None
    pred = prob >= threshold
    tp, fp = int(np.sum(pred & (y == 1))), int(np.sum(pred & (y == 0)))
    fn, tn = int(np.sum(~pred & (y == 1))), int(np.sum(~pred & (y == 0)))
    mcc_den = np.sqrt(float(tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    bins = np.clip((prob * 10).astype(int), 0, 9)
    ece = float(sum(abs(prob[bins == b].mean() - y[bins == b].mean()) * np.mean(bins == b)
                    for b in range(10) if np.any(bins == b)))
    result = dict(
        rows=int(len(y)), positives=int(y.sum()), prevalence=float(y.mean()),
        roc_auc=float(roc_auc_score(y, prob)), pr_auc=float(average_precision_score(y, prob)),
        brier=float(brier_score_loss(y, prob)), log_loss=float(log_loss(y, np.clip(prob, 1e-6, 1 - 1e-6))),
        ece=ece, threshold=float(threshold),
        sensitivity=tp / max(1, tp + fn), specificity=tn / max(1, tn + fp), precision=tp / max(1, tp + fp),
        npv=tn / max(1, tn + fn), f1=2 * tp / max(1, 2 * tp + fp + fn),
        mcc=float((tp * tn - fp * fn) / mcc_den) if mcc_den > 0 else 0.0,
        confusion_matrix=[[tn, fp], [fn, tp]],
    )
    if groups is not None and bootstrap:
        rng = np.random.default_rng(seed)
        cows = np.unique(groups)
        members = {c: np.flatnonzero(groups == c) for c in cows}
        aucs = []
        for _ in range(bootstrap):
            pick = np.concatenate([members[c] for c in rng.choice(cows, len(cows), replace=True)])
            if len(set(y[pick])) == 2:
                aucs.append(roc_auc_score(y[pick], prob[pick]))
        if aucs:
            result["roc_auc_ci95"] = [float(np.percentile(aucs, 2.5)), float(np.percentile(aucs, 97.5))]
    return result


def curves(y, prob, hours):
    from sklearn.metrics import precision_recall_curve, roc_curve

    ok = np.isfinite(prob)
    fpr, tpr, _ = roc_curve(y[ok], prob[ok])
    precision, recall, _ = precision_recall_curve(y[ok], prob[ok])
    calibration = []
    for lo in np.arange(0, 1, 0.1):
        mask = ok & (prob >= lo) & ((prob < lo + 0.1) if lo < 0.89 else (prob <= 1.0))
        if mask.any():
            calibration.append(dict(predicted=float(prob[mask].mean()), observed=float(y[mask].mean()),
                                    count=int(mask.sum())))
    risk = []
    for lo in range(0, 240, 6):
        mask = ok & (hours > lo) & (hours <= lo + 6)
        if mask.sum() >= 5:
            q = np.percentile(prob[mask], [25, 50, 75])
            risk.append(dict(hours_before=-(lo + 3), q25=float(q[0]), median=float(q[1]), q75=float(q[2]),
                             n=int(mask.sum())))
    return dict(roc=_down(list(zip(fpr.tolist(), tpr.tolist()))),
                pr=_down(list(zip(recall.tolist(), precision.tolist()))),
                calibration=calibration, risk_by_hours=risk)


def alert_episodes(times, prob, threshold, *, persistence=2, gap_ms=HOUR_MS):
    """Sustained alerts: ≥ ``persistence`` consecutive hourly decisions at/above threshold."""
    episodes, run = [], []
    for t, p in zip(times, prob):
        if np.isfinite(p) and p >= threshold and (not run or t - run[-1][0] <= gap_ms):
            run.append((t, p))
            continue
        if len(run) >= persistence:
            episodes.append(dict(start=run[persistence - 1][0], first=run[0][0], end=run[-1][0],
                                 peak=max(v for _, v in run), windows=len(run)))
        run = [(t, p)] if np.isfinite(p) and p >= threshold else []
    if len(run) >= persistence:
        episodes.append(dict(start=run[persistence - 1][0], first=run[0][0], end=run[-1][0],
                             peak=max(v for _, v in run), windows=len(run)))
    return episodes


def event_metrics(rows, prob, threshold, horizon, *, persistence=2):
    """Per-calving detection, lead time and false alerts per cow-day outside the horizon."""
    by_event = defaultdict(list)
    for row, p in zip(rows, prob):
        by_event[(row["cow_id"], row["calving_epoch_ms"])].append((row["decision_epoch_ms"], p, row["hours_to_calving"]))
    detected, leads, false_alerts, exposure_h, evaluable, per_event = 0, [], 0, 0.0, 0, []
    for (cow, calving), items in by_event.items():
        items.sort()
        times = [t for t, _, _ in items]
        probs = np.asarray([p for _, p, _ in items], dtype=float)
        hours = np.asarray([h for *_, h in items], dtype=float)
        inside = hours <= horizon
        covered = inside.sum() / max(1, horizon)
        episodes = alert_episodes(times, probs, threshold, persistence=persistence)
        hits = [e for e in episodes if calving - horizon * HOUR_MS <= e["start"] < calving]
        false = [e for e in episodes if e["start"] < calving - horizon * HOUR_MS]
        exposure_h += float(np.sum(~inside))
        false_alerts += len(false)
        lead = (calving - hits[0]["start"]) / HOUR_MS if hits else None
        if covered >= 0.5:
            evaluable += 1
            if hits:
                detected += 1
                leads.append(lead)
        per_event.append(dict(cow_id=cow, calving_epoch_ms=int(calving), horizon_coverage=round(float(covered), 3),
                              detected=bool(hits), lead_hours=None if lead is None else round(lead, 2),
                              false_alerts=len(false), max_probability=float(np.nanmax(probs)) if len(probs) else None))
    return dict(
        calvings=len(by_event), evaluable_calvings=evaluable, detected=detected,
        event_sensitivity=detected / max(1, evaluable),
        lead_time_median_h=float(np.median(leads)) if leads else None,
        lead_time_iqr_h=[float(np.percentile(leads, 25)), float(np.percentile(leads, 75))] if leads else None,
        false_alerts=false_alerts, exposure_cow_days=round(exposure_h / 24, 2),
        false_alerts_per_cow_day=false_alerts / max(1e-9, exposure_h / 24) if exposure_h else None,
        persistence_hours=persistence, per_event=per_event,
        lead_hist=np.histogram(leads, bins=range(0, horizon + 6, 6))[0].tolist() if leads else [],
    )


def _primaries():
    from cowmata_engine.features import available_features

    return {key: module.SPEC.primary for key, module, _ in available_features() if module is not None}


def group_importance(docs, folds, x, y, columns, column_keys, *, seed=433):
    """AUC drop when all columns of one feature key are permuted inside each validation fold."""
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(seed)
    groups = defaultdict(list)
    for i, c in enumerate(columns):
        groups[column_keys.get(c, "context")].append(i)
    base_scores, drops = [], defaultdict(list)
    for (_, test), doc in zip(folds, docs):
        if doc is None or len(set(y[test])) < 2:
            continue
        ref = roc_auc_score(y[test], predict_model(doc, x[test]))
        base_scores.append(ref)
        for key, idx in groups.items():
            shuffled = x[test].copy()
            perm = rng.permutation(len(test))
            shuffled[:, idx] = shuffled[perm][:, idx]
            drops[key].append(ref - roc_auc_score(y[test], predict_model(doc, shuffled)))
    return sorted([dict(feature=k, auc_drop=float(np.mean(v)), folds=len(v)) for k, v in drops.items()],
                  key=lambda r: -r["auc_drop"])


def _uses(column, keys, column_keys):
    return column_keys.get(column) in keys or column_keys.get(column) == "context"


def train_decision(dataset, output, *, algorithms=DEFAULT_ALGORITHMS, horizon=24, horizons=HORIZONS,
                   folds=5, persistence=2, features=None, require=None, progress=lambda *_: None,
                   cancelled=lambda: False):
    """Train and evaluate. ``features`` limits model inputs to these feature keys; ``require``
    keeps only decision rows where each listed feature has data in the last 6 h, so that models
    are compared on one and the same population."""
    started = time.monotonic()
    dataset = Path(dataset)
    folder = dataset if dataset.is_dir() else dataset.parent
    summary_file = folder / "decision-dataset.json"
    summary = json.loads(summary_file.read_text(encoding="utf-8")) if summary_file.is_file() else {}
    rows = read_table(dataset)
    all_columns = model_columns(rows)
    all_keys = column_key_map(all_columns, summary.get("features"))
    if features:
        all_columns = [c for c in all_columns if _uses(c, set(features), all_keys)]
        all_keys = {c: all_keys[c] for c in all_columns}
    in_scope = scope_mask(rows, all_keys)
    if require:
        in_scope &= np.asarray([all((r.get(f"coverage.{k}@6h") or 0) > 0 for k in require) for r in rows])
    excluded_scope = int((~in_scope).sum())
    rows = [r for r, keep in zip(rows, in_scope) if keep]
    target = f"y_{horizon}h"
    labelled = [r for r in rows if r.get(target) is not None]
    if not labelled:
        raise ValueError("数据集没有产犊结局：请在构建数据集时提供产犊登记或娩出标签")
    columns = [c for c in model_columns(labelled) if c in set(all_columns)]
    probe = np.asarray([[np.nan if r.get(c) is None else r[c] for c in columns] for r in labelled], dtype=float)
    with np.errstate(invalid="ignore"):
        informative = [np.isfinite(col).sum() >= 10 and np.nanstd(col) > 1e-12 for col in probe.T]
    columns = [c for c, keep in zip(columns, informative) if keep]
    column_keys = column_key_map(columns, summary.get("features"))
    x = np.asarray([[np.nan if r.get(c) is None else r[c] for c in columns] for r in labelled], dtype=float)
    y = np.asarray([int(r[target]) for r in labelled])
    groups = np.asarray([r["cow_id"] for r in labelled])
    hours = np.asarray([r["hours_to_calving"] for r in labelled], dtype=float)
    cows = sorted(set(groups))
    if len(cows) < 3 or len(set(y)) < 2:
        raise ValueError("至少需要 3 头有产犊结局的牛，且同时包含提前量内与提前量外的窗口")
    n_splits = max(2, min(int(folds), len(cows)))
    split = cow_folds(groups, n_splits)
    primaries = _primaries()
    for key, info in summary.get("features", {}).items():
        columns_k = info.get("columns") or []
        if info.get("primary") in columns_k:
            primaries[key] = info["primary"]
        elif primaries.get(key) not in columns_k:
            primaries[key] = (columns_k or [None])[0]
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    board, fold_docs, raw_oof = [], {}, {}
    total = len(algorithms) * n_splits + 6
    step = 0
    for algorithm in algorithms:
        if algorithm not in ALGORITHMS:
            raise ValueError(f"未知决策算法：{algorithm}")
        began = time.monotonic()
        oof = np.full(len(y), np.nan)
        docs, fold_curves = [], []
        for number, (train, test) in enumerate(split, 1):
            if cancelled():
                raise InterruptedError("决策训练已取消")
            if len(set(y[train])) < 2:
                docs.append(None)
                continue
            history = {}
            doc = fit_model(algorithm, x[train], y[train], columns, groups=groups[train], primaries=primaries,
                            seed=433 + number, curves=history if algorithm == "xgboost" else None,
                            eval_set=(x[test], y[test]) if algorithm == "xgboost" else None)
            oof[test] = predict_model(doc, x[test])
            docs.append(doc)
            if history:
                fold_curves.append(dict(fold=number, train_logloss=_down(history["train"]["logloss"], 300),
                                        valid_logloss=_down(history.get("valid", {}).get("logloss", []), 300),
                                        valid_auc=_down(history.get("valid", {}).get("auc", []), 300)))
            step += 1
            progress(step, total, f"按牛交叉验证 · {ALGORITHMS[algorithm]['title']} · 第 {number}/{n_splits} 折")
        calibrated = cross_calibrate(oof, y, split)
        threshold = choose_threshold(calibrated[np.isfinite(calibrated)], y[np.isfinite(calibrated)])
        metrics = window_metrics(y, calibrated, threshold, groups)
        metrics["roc_auc_raw"] = window_metrics(y, oof, 0.5, bootstrap=0)["roc_auc"]
        events = event_metrics(labelled, calibrated, threshold, horizon, persistence=persistence)
        board.append(dict(key=algorithm, **ALGORITHMS[algorithm], metrics=metrics,
                          events={k: v for k, v in events.items() if k != "per_event"},
                          curves=curves(y, calibrated, hours), fold_curves=fold_curves,
                          seconds=round(time.monotonic() - began, 2)))
        fold_docs[algorithm], raw_oof[algorithm] = docs, (oof, calibrated, threshold, events)
    # Rank by PR-AUC (imbalanced target) then event sensitivity minus false-alert burden.
    def rank(item):
        m, e = item["metrics"], item["events"]
        return (round(m["pr_auc"], 3), e["event_sensitivity"] - 0.1 * (e["false_alerts_per_cow_day"] or 0))

    board.sort(key=rank, reverse=True)
    best = board[0]["key"]
    oof, calibrated, threshold, events = raw_oof[best]
    step += 1
    progress(step, total, "特征组置换重要性")
    groups_importance = group_importance(fold_docs[best], split, x, y, columns, column_keys)
    learn_algo = "xgboost" if best == "stacking" else best
    learning = []
    for fraction in (0.25, 0.5, 0.75, 1.0):
        if cancelled():
            raise InterruptedError("决策训练已取消")
        scores = np.full(len(y), np.nan)
        train_auc = None
        rng = np.random.default_rng(int(fraction * 100))
        for train, test in split:
            train_cows = sorted(set(groups[train]))
            keep = set(rng.choice(train_cows, max(2, int(round(len(train_cows) * fraction))), replace=False))
            sub = np.asarray([i for i in train if groups[i] in keep])
            if len(set(y[sub])) < 2:
                continue
            doc = fit_model(learn_algo, x[sub], y[sub], columns, groups=groups[sub], primaries=primaries)
            scores[test] = predict_model(doc, x[test])
            train_auc = window_metrics(y[sub], predict_model(doc, x[sub]), 0.5, bootstrap=0)["roc_auc"]
        ok = np.isfinite(scores)
        if ok.sum() and len(set(y[ok])) == 2:
            learning.append(dict(fraction=fraction, cows=int(round((len(cows) * (n_splits - 1) / n_splits) * fraction)),
                                 valid_auc=window_metrics(y[ok], scores[ok], 0.5, bootstrap=0)["roc_auc"],
                                 train_auc=train_auc))
    step += 1
    progress(step, total, "学习曲线")
    # Deployment models: best algorithm for every horizon + time-to-calving regressor.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    version = f"decision-{horizon}h-{best}-{stamp}"
    files, calibrators, thresholds, horizon_metrics = {}, {}, {}, {}
    for h in horizons:
        if cancelled():
            raise InterruptedError("决策训练已取消")
        key = f"y_{h}h"
        rows_h = [r for r in rows if r.get(key) is not None]
        if h == horizon:
            xh, yh, gh, oof_h, cal_h, thr_h = x, y, groups, oof, calibrated, threshold
        else:
            xh = np.asarray([[np.nan if r.get(c) is None else r[c] for c in columns] for r in rows_h], dtype=float)
            yh = np.asarray([int(r[key]) for r in rows_h])
            gh = np.asarray([r["cow_id"] for r in rows_h])
            if len(set(yh)) < 2:
                continue
            split_h = cow_folds(gh, n_splits)
            oof_h = np.full(len(yh), np.nan)
            for train, test in split_h:
                if len(set(yh[train])) == 2:
                    oof_h[test] = predict_model(fit_model(best, xh[train], yh[train], columns, groups=gh[train],
                                                          primaries=primaries), xh[test])
            cal_h = cross_calibrate(oof_h, yh, split_h)
            thr_h = choose_threshold(cal_h[np.isfinite(cal_h)], yh[np.isfinite(cal_h)])
        ok = np.isfinite(oof_h)
        final = fit_model(best, xh, yh, columns, groups=gh, primaries=primaries)
        calibrators[str(h)] = _isotonic(oof_h[ok], yh[ok])
        thresholds[str(h)] = thr_h
        horizon_metrics[str(h)] = {k: v for k, v in window_metrics(yh, cal_h, thr_h, gh, bootstrap=0).items()}
        name = f"model-{h}h.json"
        (output / name).write_text(json.dumps(final), encoding="utf-8")
        files[str(h)] = dict(file=name, sha256=hashlib.sha256((output / name).read_bytes()).hexdigest())
        step += 1
        progress(step, total, f"训练部署模型 · {h} 小时")
    tte, tte_eval = None, None
    try:
        all_rows = [r for r in rows if r.get("hours_to_calving") is not None]
        xt = np.asarray([[np.nan if r.get(c) is None else r[c] for c in columns] for r in all_rows], dtype=float)
        ht = np.asarray([r["hours_to_calving"] for r in all_rows], dtype=float)
        gt = np.asarray([r["cow_id"] for r in all_rows])
        pred = np.full((len(ht), 3), np.nan)
        for train, test in cow_folds(gt, n_splits):
            pred[test] = predict_time_to_event(fit_time_to_event(xt[train], ht[train], columns), xt[test])
        offset = conformal_offset(pred, ht)
        raw_coverage = float(np.mean((ht >= pred[:, 0]) & (ht <= pred[:, 2])))
        pred[:, 0] = np.expm1(np.log1p(pred[:, 0]) - offset)
        pred[:, 2] = np.expm1(np.log1p(pred[:, 2]) + offset)
        near = ht <= 72
        err = np.abs(pred[:, 1] - ht)
        tte_eval = dict(
            mae_hours_within_72h=float(np.nanmean(err[near])) if near.any() else None,
            mae_hours_within_24h=float(np.nanmean(err[ht <= 24])) if (ht <= 24).any() else None,
            interval80_coverage=float(np.mean((ht >= pred[:, 0]) & (ht <= pred[:, 2]))),
            interval80_coverage_before_conformal=raw_coverage, conformal_log_offset=offset,
            by_bin=[dict(hours_before=-(lo + 6), median_pred=float(np.nanmedian(pred[(ht > lo) & (ht <= lo + 12), 1])),
                         p10=float(np.nanmedian(pred[(ht > lo) & (ht <= lo + 12), 0])),
                         p90=float(np.nanmedian(pred[(ht > lo) & (ht <= lo + 12), 2])),
                         n=int(np.sum((ht > lo) & (ht <= lo + 12))))
                    for lo in range(0, 168, 12) if np.sum((ht > lo) & (ht <= lo + 12)) >= 5])
        tte_doc = fit_time_to_event(xt, ht, columns)
        tte_doc["conformal_log"] = offset
        (output / "model-time-to-calving.json").write_text(json.dumps(tte_doc), encoding="utf-8")
        tte = dict(file="model-time-to-calving.json",
                   sha256=hashlib.sha256((output / "model-time-to-calving.json").read_bytes()).hexdigest())
    except ValueError as exc:
        tte_eval = dict(error=str(exc))
    step += 1
    progress(step, total, "剩余时间回归")
    final_primary = json.loads((output / files[str(horizon)]["file"]).read_text(encoding="utf-8"))
    column_importance = sorted(importance(final_primary).items(), key=lambda kv: -kv[1])[:30]
    manifest = dict(
        schema=MANIFEST_SCHEMA, model_schema=MODEL_SCHEMA, version=version, created_at=datetime.now().isoformat(timespec="seconds"),
        algorithm=best, algorithm_title=ALGORITHMS[best]["title"], horizon_hours=horizon,
        horizons=[int(h) for h in files], columns=columns, column_keys=column_keys, primaries=primaries, files=files,
        time_to_calving=tte, calibrators=calibrators, thresholds=thresholds, persistence_hours=persistence,
        levels=[dict(min_probability=a, level=b, advice=c) for a, b, c in LEVELS],
        feature_versions={k: v.get("version") for k, v in summary.get("features", {}).items()},
        dataset_fingerprint=summary.get("fingerprint"), training_cows=len(cows), training_rows=int(len(y)),
        metrics=board[0]["metrics"], events=board[0]["events"], horizon_metrics=horizon_metrics,
        validation=f"按牛分组 {n_splits} 折交叉验证；概率经交叉拟合等渗校准；阈值取 Youden 指数最优；单牧场数据，未做跨牧场外部验证",
        excluded_outside_feature_span=excluded_scope,
        feature_subset=list(features) if features else None, required_features=list(require) if require else None,
        complete=True,
    )
    (output / "decision.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    report = dict(
        manifest, leaderboard=board, per_event=events["per_event"], group_importance=groups_importance,
        column_importance=column_importance, learning_curve=learning, time_to_calving_eval=tte_eval,
        folds=[dict(fold=i + 1, test_cows=sorted(set(groups[test])), rows=int(len(test)))
               for i, (_, test) in enumerate(split)],
        univariate=univariate(rows, horizon)[:40], profile=summary.get("profile", {}),
        dataset=dict({k: summary.get(k) for k in ("rows", "labelled_rows", "cows", "calving_cows", "calvings",
                                                  "label_sources", "positives", "feature_coverage", "missing_features")}),
        elapsed_seconds=round(time.monotonic() - started, 1),
    )
    (output / "决策训练报告.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    with (output / "决策留出预测.csv").open("w", encoding="utf-8-sig") as stream:
        stream.write("cow_id,decision_epoch_ms,hours_to_calving,y,oof_raw,oof_calibrated\n")
        for r, a, b, c in zip(labelled, y, oof, calibrated):
            stream.write(f"{r['cow_id']},{int(r['decision_epoch_ms'])},{r['hours_to_calving']},{a},{b:.6f},{c:.6f}\n")
    return report
