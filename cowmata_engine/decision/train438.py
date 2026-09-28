"""4.3.8 calving decision training: weak ledger labels + gold-only evaluation.

Protocol (fixed before looking at gold results):

* Truth.  Gold = video annotation FETAL_PART_FIRST_VISIBLE (onset) .. CALF_FULLY_EXPELLED (22 calvings).
  Ledger (台账) times are approximate (onset shifted -0.5 h, see ``labels.merge_calvings``) and are
  used ONLY as down-weighted training rows (``weak_weight``) and for model selection.
* Folds.  Grouped by cow; gold cows and ledger cows are dealt round-robin separately so every fold
  holds ~1/k of the gold calvings.  Every probability used below is out-of-fold and cross-calibrated.
* Selection.  Algorithm and alert threshold are chosen on LEDGER cows only (maximise event
  detection subject to a false-alert budget).  Gold cows never influence any choice, so the gold
  numbers are an honest held-out estimate (still single farm, 22 events -> wide CI, reported).
* Event rule (strict).  A calving is detected when a sustained alert (``persistence`` consecutive
  hourly decisions >= threshold) STARTS within ``window_h`` hours before the onset.  Alerts that
  start earlier are false alerts; an alert that starts at/after onset is a miss.  We also report
  false alerts per cow-day and the share of monitored hours spent in alert, so "always alarm"
  cannot look good.
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from .dataset import column_key_map, model_columns, read_table
from .models import (ALGORITHMS, MODEL_SCHEMA, conformal_offset, fit_model, fit_time_to_event, importance,
                     predict_model, predict_time_to_event)
from .train import (HOUR_MS, MANIFEST_SCHEMA, _isotonic, _primaries, alert_episodes, choose_threshold,
                    cross_calibrate, dynamic_levels, group_importance, scope_mask, threshold_policy, window_metrics)

PROTOCOL = "cowmata-calving-protocol-4.3.8"
ALGOS_438 = ("expert_rules", "logistic", "random_forest", "xgboost")
GRID = np.round(np.arange(0.02, 0.951, 0.01), 2)


def split_folds(groups, gold_cows, k):
    """Cow folds with gold cows and weak cows dealt round-robin separately (deterministic)."""
    h = lambda c: hashlib.sha1(str(c).encode()).hexdigest()
    cows = sorted(set(groups), key=h)
    gold = [c for c in cows if c in gold_cows]
    weak = [c for c in cows if c not in gold_cows]
    fold_of = {c: i % k for i, c in enumerate(gold)}
    fold_of.update({c: i % k for i, c in enumerate(weak)})
    a = np.asarray([fold_of[g] for g in groups])
    return [(np.flatnonzero(a != f), np.flatnonzero(a == f)) for f in range(k)]


SENSOR_KEYS = ("lying_ratio", "straining_ratio", "behavior_events", "gyro_spectral_entropy", "temperature")


def sensor_ok(row):
    """The tail-ring 9-axis stream delivered data in the last 6 h (the device was worn and uploading)."""
    return any((row.get(f"coverage.{k}@6h") or 0) > 0 for k in SENSOR_KEYS)


def smooth_by_cow(rows, prob, span):
    """Causal EWMA of the hourly risk per cow (span in hours; 0 = raw). Same as deployment."""
    prob = np.asarray(prob, float)
    if not span:
        return prob
    alpha = 2.0 / (float(span) + 1.0)
    out = prob.copy()
    order = defaultdict(list)
    for i, r in enumerate(rows):
        order[r["cow_id"]].append((r["decision_epoch_ms"], i))
    for items in order.values():
        items.sort()
        acc, last = None, None
        for t, i in items:
            v = prob[i]
            if not np.isfinite(v):
                continue
            if acc is None or (last is not None and t - last > 6 * HOUR_MS):
                acc = v  # restart after a data gap
            else:
                acc = alpha * v + (1 - alpha) * acc
            out[i], last = acc, t
    return out


_EVENT_CACHE = {}


def _event_index(rows):
    """{(cow, onset): (row indices in time order, times, hours_to_calving, sensor_ok)}; cached per row list."""
    key = (id(rows), len(rows))
    hit = _EVENT_CACHE.get(key)
    if hit is not None and hit[0] is rows:
        return hit[1]
    groups = defaultdict(list)
    for i, r in enumerate(rows):
        groups[(r["cow_id"], int(r["calving_epoch_ms"]))].append((r["decision_epoch_ms"], i))
    out = {}
    for k, items in groups.items():
        items.sort()
        idx = np.asarray([i for _, i in items])
        out[k] = (idx, [t for t, _ in items], np.asarray([rows[i]["hours_to_calving"] for i in idx], float),
                  np.asarray([sensor_ok(rows[i]) for i in idx], bool))
    if len(_EVENT_CACHE) > 16:
        _EVENT_CACHE.clear()
    _EVENT_CACHE[key] = (rows, out)
    return out


def event_eval(rows, prob, threshold, *, persistence=2, window_h=24, min_cover=0.5, detail=False, min_history=None,
               rule="start"):
    """Strict per-calving evaluation (see module docstring).

    A calving is *evaluable* when the tail ring delivered data in >= ``min_cover`` of the hours of
    the detection window (worn and uploading).  Calvings without data are counted separately as a
    data-coverage problem (``no_sensor_data``): the product shows 数据不足 for them, it cannot warn.
    False alerts and alert time are counted per monitored cow-day (hours with sensor data).
    """
    prob = np.asarray(prob, float)
    det = ev = fa = miss_early = miss_short = miss_low = no_data = 0
    leads, exposure, alert_h, monitored, per = [], 0.0, 0, 0, []
    for (cow, onset), (index, t, hrs, has) in _event_index(rows).items():
        p = prob[index]
        pre = (hrs > 0) & (hrs <= window_h)
        cover = (pre & has).sum() / window_h
        history = float(np.nanmax(hrs)) - window_h  # hours of in-scope data before the detection window opens
        if min_history is not None and history < min_history:
            continue
        eps = alert_episodes(t, p, threshold, persistence=persistence)
        if rule == "active":
            # Industry reading: an alert is on at some moment inside the window before onset.
            hits = [e for e in eps if e["start"] < onset and e["end"] >= onset - window_h * HOUR_MS]
            false = [e for e in eps if e["end"] < onset - window_h * HOUR_MS]
        else:
            # Strict: the first alert of the episode starts inside the window.
            hits = [e for e in eps if onset - window_h * HOUR_MS <= e["start"] < onset]
            false = [e for e in eps if e["start"] < onset - window_h * HOUR_MS]
        early_active = [e for e in false if e["end"] >= onset - window_h * HOUR_MS]
        outside = (hrs > window_h) & has
        exposure += float(outside.sum())
        fa += len(false)
        alert_h += int(np.sum(np.isfinite(p) & (p >= threshold) & outside))
        monitored += int(outside.sum())
        lead = (onset - hits[0]["start"]) / HOUR_MS if hits else None
        if cover >= min_cover:
            ev += 1
            if hits:
                det += 1
                leads.append(lead)
            elif early_active:
                miss_early += 1
            elif pre.any() and np.nanmax(p[pre]) >= threshold:
                miss_short += 1
            else:
                miss_low += 1
        elif pre.any():
            no_data += 1
        if detail:
            per.append(dict(cow_id=cow, onset_epoch_ms=onset, pre_window_coverage=round(float(cover), 3),
                            history_before_window_h=round(history, 1),
                            evaluable=bool(cover >= min_cover), detected=bool(hits),
                            lead_hours=None if lead is None else round(lead, 2), false_alerts=len(false),
                            max_risk_pre=float(np.nanmax(p[pre])) if pre.any() else None))
    out = dict(calvings=len(_event_index(rows)), evaluable=ev, detected=det, detection=det / max(1, ev), no_sensor_data=no_data,
               data_coverage=ev / max(1, ev + no_data),
               lead_median_h=float(np.median(leads)) if leads else None,
               lead_iqr_h=[float(np.percentile(leads, 25)), float(np.percentile(leads, 75))] if leads else None,
               false_alerts=fa, exposure_cow_days=round(exposure / 24, 2),
               fa_per_cow_day=fa / (exposure / 24) if exposure else None,
               alert_time_fraction=alert_h / monitored if monitored else None,
               window_h=window_h, persistence=persistence, rule=rule,
               misses=dict(alert_started_too_early=miss_early, above_threshold_not_sustained=miss_short,
                           risk_below_threshold=miss_low))
    if detail:
        out["per_event"] = per
    return out


def wilson(k, n, z=1.96):
    if n == 0:
        return [None, None]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(float(c - h), 3), round(float(c + h), 3)]


def pick_rule(rows, prob, *, budget, max_alert, window_h, rule="active", spans=(0, 3, 6), persistences=(1, 2, 3)):
    """Alert rule (EWMA span, persistence, threshold) with the highest detection on these rows subject to
    false alerts <= ``budget`` per monitored cow-day AND time in alert <= ``max_alert`` (so a near-constant
    alarm can never be selected).  Returns (rule dict, curve of the chosen span/persistence)."""
    best, curves = None, {}
    for span in spans:
        sm = smooth_by_cow(rows, prob, span)
        for k in persistences:
            curve = []
            for thr in GRID:
                m = event_eval(rows, sm, float(thr), persistence=k, window_h=window_h, rule=rule)
                strict = event_eval(rows, sm, float(thr), persistence=k, window_h=window_h, rule="start")
                curve.append(dict(threshold=float(thr), detection=m["detection"], detection_strict=strict["detection"],
                                  fa_per_cow_day=m["fa_per_cow_day"] or 0.0, alert_time_fraction=m["alert_time_fraction"] or 0.0,
                                  lead_median_h=m["lead_median_h"]))
            curves[(span, k)] = curve
            for c in curve:
                if c["fa_per_cow_day"] <= budget and c["alert_time_fraction"] <= max_alert:
                    key = (round(c["detection"], 4), round(c["detection_strict"], 4), -c["alert_time_fraction"])
                    if best is None or key > best[0]:
                        best = (key, dict(ewma_span=span, persistence=k, threshold=c["threshold"]))
    if best is None:  # budget unattainable: least-alerting rule, reported as such
        span, k = 0, 2
        c = min(curves[(span, k)], key=lambda c: (c["fa_per_cow_day"], -c["detection"]))
        best = (None, dict(ewma_span=span, persistence=k, threshold=c["threshold"], budget_unattainable=True))
    rule_doc = best[1]
    return rule_doc, curves[(rule_doc["ewma_span"], rule_doc["persistence"])]


def _matrix(rows, columns):
    return np.asarray([[np.nan if r.get(c) is None else r[c] for c in columns] for r in rows], dtype=float)


DUE_COLUMN = "days_to_due"


def load_due_dates(ledger_path):
    """{cow_id: due epoch ms (12:00 UTC+8 of 预产期)} from the farm ledger.

    预产期 (expected calving date from insemination) is known in advance, so it is a valid input.
    ``提前预产期天数`` is computed from the outcome and is never read.
    """
    import io
    from datetime import timezone, timedelta

    text = Path(ledger_path).read_bytes().decode("utf-8-sig")
    result = {}
    for r in csv.DictReader(io.StringIO(text)):
        value = (r.get("预产期") or "").strip()
        if not value or str(r.get("已删除", "0")) not in ("0", ""):
            continue
        try:
            day = datetime.fromisoformat(value[:10])
        except ValueError:
            continue
        stamp = day.replace(hour=12, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000
        result[str(r.get("牛号", "")).strip()] = int(stamp)
    return result


def add_due(rows, due_dates):
    for r in rows:
        due = due_dates.get(str(r["cow_id"])) if due_dates else None
        r[DUE_COLUMN] = None if due is None else round((due - r["decision_epoch_ms"]) / 86_400_000, 3)
    return rows


def train_calving(dataset, output, *, horizon=12, horizons=(1, 2, 3, 6, 12), weak_weight=0.3,
                  algorithms=ALGOS_438, folds=5, persistence=2, window_h=12, fa_budget=0.30, max_alert=0.15,
                  rule="active", features=None, version=None, due_dates=None, hard_negative_weight=3.0, progress=print):
    started = time.monotonic()
    dataset = Path(dataset)
    folder = dataset if dataset.is_dir() else dataset.parent
    summary = json.loads((folder / "decision-dataset.json").read_text(encoding="utf-8"))
    rows = read_table(dataset)
    if due_dates:
        add_due(rows, due_dates)
    all_columns = model_columns(rows)
    keys = column_key_map(all_columns, summary.get("features"))
    if features:
        all_columns = [c for c in all_columns if keys.get(c) in set(features) or keys.get(c) == "context"]
        keys = {c: keys[c] for c in all_columns}
    rows = [r for r, keep in zip(rows, scope_mask(rows, keys)) if keep]
    gold_q = lambda r: r.get("label_quality") == "gold"
    weak_q = lambda r: r.get("label_quality") == "approximate"
    labelled = [r for r in rows if r.get("hours_to_calving") is not None and (gold_q(r) or (weak_weight > 0 and weak_q(r)))]
    columns = [c for c in model_columns(labelled) if c in set(all_columns)]
    probe = _matrix(labelled, columns)
    with np.errstate(invalid="ignore"):
        columns = [c for c, col in zip(columns, probe.T) if np.isfinite(col).sum() >= 10 and np.nanstd(col) > 1e-12]
    column_keys = column_key_map(columns, summary.get("features"))
    x = _matrix(labelled, columns)
    hrs = np.asarray([r["hours_to_calving"] for r in labelled], float)
    y = (hrs <= horizon).astype(int)
    groups = np.asarray([r["cow_id"] for r in labelled])
    gold = np.asarray([gold_q(r) for r in labelled])
    w = np.where(gold, 1.0, float(weak_weight))
    if hard_negative_weight and hard_negative_weight != 1.0:
        # Hours just outside the alert horizon are the negatives the farm confuses with calving.
        w = w * np.where((hrs > horizon) & (hrs <= horizon + 24), float(hard_negative_weight), 1.0)
    gold_cows = set(groups[gold])
    split = split_folds(groups, gold_cows, folds)
    primaries = _primaries()
    for key, info in summary.get("features", {}).items():
        cols = info.get("columns") or []
        primaries[key] = info["primary"] if info.get("primary") in cols else (primaries.get(key) if primaries.get(key) in cols else (cols or [None])[0])
    progress(f"rows={len(y)} gold_rows={int(gold.sum())} gold_cows={len(gold_cows)} weak_cows={len(set(groups[~gold]))} "
             f"columns={len(columns)} positives={int(y.sum())}")
    gold_rows = [r for r, g in zip(labelled, gold) if g]
    weak_rows = [r for r, g in zip(labelled, gold) if not g]
    board, oofs, docs_by = [], {}, {}
    for algo in algorithms:
        t0 = time.monotonic()
        oof = np.full(len(y), np.nan)
        docs = []
        for number, (tr, te) in enumerate(split, 1):
            doc = fit_model(algo, x[tr], y[tr], columns, groups=groups[tr], primaries=primaries, seed=438 + number,
                            sample_weight=w[tr])
            oof[te] = predict_model(doc, x[te])
            docs.append(doc)
        cal = cross_calibrate(oof, y, split)
        rule_doc, curve = pick_rule(weak_rows, cal[~gold], budget=fa_budget, max_alert=max_alert, window_h=window_h, rule=rule)
        thr, kk = rule_doc["threshold"], rule_doc["persistence"]
        sm = smooth_by_cow(labelled, cal, rule_doc["ewma_span"])
        ev = lambda rs, pr, **kw: event_eval(rs, pr, thr, persistence=kk, window_h=window_h, **kw)
        sel = ev(weak_rows, sm[~gold], rule=rule)
        held = ev(gold_rows, sm[gold], rule=rule, detail=True)
        sel_strict = ev(weak_rows, sm[~gold], rule="start")
        held_strict = ev(gold_rows, sm[gold], rule="start", detail=True)
        wm = window_metrics(y[gold], sm[gold], thr, groups[gold]) if len(set(y[gold])) == 2 else {}
        # Leakage check: sensors fitted shortly before calving leave the 24/72 h baselines empty.
        long_gold = ev(gold_rows, sm[gold], rule=rule, min_history=24)
        long_weak = ev(weak_rows, sm[~gold], rule=rule, min_history=24)
        board.append(dict(key=algo, title=ALGORITHMS[algo]["title"], threshold=thr, alert_rule=rule_doc,
                          selection_ledger=sel, selection_ledger_strict=sel_strict,
                          gold_long_history=long_gold, ledger_long_history=long_weak,
                          gold_heldout={k: v for k, v in held.items() if k != "per_event"},
                          gold_heldout_strict={k: v for k, v in held_strict.items() if k != "per_event"},
                          gold_window=wm, seconds=round(time.monotonic() - t0, 1)))
        oofs[algo], docs_by[algo] = (oof, cal, thr, curve, held, rule_doc, sm, held_strict), docs
        progress(f"{algo:14s} thr={thr:.2f} ledger det={sel['detection']:.3f} ({sel['detected']}/{sel['evaluable']}) "
                 f"fa/d={sel['fa_per_cow_day'] or 0:.3f} | GOLD det={held['detection']:.3f} ({held['detected']}/{held['evaluable']}) "
                 f"fa/d={held['fa_per_cow_day'] or 0:.3f} alert%={100 * (held['alert_time_fraction'] or 0):.1f} "
                 f"lead={held['lead_median_h']} auc={wm.get('roc_auc', float('nan')):.3f} {time.monotonic() - t0:.0f}s"
                 f" | >=24h-history ledger {long_weak['detected']}/{long_weak['evaluable']} gold {long_gold['detected']}/{long_gold['evaluable']} misses {sel['misses']} nodata {sel['no_sensor_data']}"
                 f" | rule {rule_doc} strict ledger {sel_strict['detected']}/{sel_strict['evaluable']} gold {held_strict['detected']}/{held_strict['evaluable']}")
    # Selection on ledger cows only.
    board.sort(key=lambda b: (round(b["selection_ledger"]["detection"], 3), -(b["selection_ledger"]["fa_per_cow_day"] or 0)),
               reverse=True)
    best = board[0]["key"]
    oof, cal, thr, curve, held, rule_doc, sm, held_strict = oofs[best]
    persistence = rule_doc["persistence"]
    result = dict(protocol=PROTOCOL, best=best, threshold=thr, board=board, curve=curve, gold_per_event=held["per_event"],
                  gold_ci95=wilson(held["detected"], held["evaluable"]), weak_weight=weak_weight, horizon=horizon,
                  window_h=window_h, persistence=persistence, fa_budget=fa_budget, columns=len(columns),
                  rows=int(len(y)), gold_rows=int(gold.sum()), gold_cows=sorted(gold_cows),
                  weak_cows=len(set(groups[~gold])), alert_rule=rule_doc, rule=rule, max_alert=max_alert,
                  gold_strict=board[0]["gold_heldout_strict"], gold_strict_per_event=held_strict["per_event"],
                  gold_strict_ci95=wilson(held_strict["detected"], held_strict["evaluable"]),
                  hard_negative_weight=hard_negative_weight)
    gold_curve = []
    for t in GRID:
        m = event_eval(gold_rows, sm[gold], float(t), persistence=persistence, window_h=window_h, rule=rule)
        gold_curve.append(dict(threshold=float(t), detection=m["detection"], fa_per_cow_day=m["fa_per_cow_day"] or 0.0,
                               alert_time_fraction=m["alert_time_fraction"] or 0.0, lead_median_h=m["lead_median_h"]))
    result["gold_curve"] = gold_curve
    if output is None:
        return result
    # ------------------------------------------------------------------ deployment
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    version = version or f"calving-438-{best}-{datetime.now():%Y%m%d-%H%M%S}"
    files, calibrators, thresholds, horizon_metrics = {}, {}, {}, {}
    for h in sorted(set(horizons) | {horizon}):
        yh = (hrs <= h).astype(int)
        if h == horizon:
            oof_h, cal_h, thr_h = oof, cal, thr
        else:
            oof_h = np.full(len(yh), np.nan)
            for number, (tr, te) in enumerate(split, 1):
                oof_h[te] = predict_model(fit_model(best, x[tr], yh[tr], columns, groups=groups[tr], primaries=primaries,
                                                    seed=438 + number, sample_weight=w[tr]), x[te])
            cal_h = cross_calibrate(oof_h, yh, split)
            thr_h = choose_threshold(cal_h[np.isfinite(cal_h)], yh[np.isfinite(cal_h)])
        ok = np.isfinite(oof_h)
        final = fit_model(best, x, yh, columns, groups=groups, primaries=primaries, sample_weight=w)
        calibrators[str(h)] = _isotonic(oof_h[ok], yh[ok])
        thresholds[str(h)] = float(thr_h)
        if len(set(yh[gold])) == 2:
            horizon_metrics[str(h)] = window_metrics(yh[gold], cal_h[gold], thr_h, groups[gold], bootstrap=0)
        name = f"model-{h}h.json"
        (output / name).write_text(json.dumps(final), encoding="utf-8")
        files[str(h)] = dict(file=name, sha256=hashlib.sha256((output / name).read_bytes()).hexdigest())
        progress(f"deploy horizon {h}h thr={thr_h:.2f}")
    tte_doc = fit_time_to_event(x, hrs, columns)
    pred = np.full((len(hrs), 3), np.nan)
    for tr, te in split:
        pred[te] = predict_time_to_event(fit_time_to_event(x[tr], hrs[tr], columns), x[te])
    offset = conformal_offset(pred, hrs)
    tte_doc["conformal_log"] = offset
    lo = np.expm1(np.log1p(pred[:, 0]) - offset)
    hi = np.expm1(np.log1p(pred[:, 2]) + offset)
    near = gold & (hrs <= 24)
    tte_eval = dict(gold_mae_hours_within_24h=float(np.nanmean(np.abs(pred[near, 1] - hrs[near]))) if near.any() else None,
                    gold_interval80_coverage=float(np.mean((hrs[gold] >= lo[gold]) & (hrs[gold] <= hi[gold]))) if gold.any() else None)
    (output / "model-time-to-calving.json").write_text(json.dumps(tte_doc), encoding="utf-8")
    tte = dict(file="model-time-to-calving.json",
               sha256=hashlib.sha256((output / "model-time-to-calving.json").read_bytes()).hexdigest())
    best_row = board[0]
    manifest = dict(
        schema=MANIFEST_SCHEMA, model_schema=MODEL_SCHEMA, protocol=PROTOCOL, version=version,
        created_at=datetime.now().isoformat(timespec="seconds"), algorithm=best, algorithm_title=ALGORITHMS[best]["title"],
        horizon_hours=horizon, horizons=[int(h) for h in files], columns=columns, column_keys=column_keys,
        primaries=primaries, files=files, time_to_calving=tte, calibrators=calibrators, thresholds=thresholds,
        persistence_hours=persistence, threshold_policy=threshold_policy(thresholds, horizon),
        levels=dynamic_levels(thresholds, horizon),
        feature_versions={k: v.get("version") for k, v in summary.get("features", {}).items()},
        dataset_fingerprint=summary.get("fingerprint"), training_cows=len(set(groups)), training_rows=int(len(y)),
        metrics=best_row["gold_window"], events=best_row["gold_heldout"], horizon_metrics=horizon_metrics,
        selection=best_row["selection_ledger"], gold_ci95=result["gold_ci95"], weak_weight=weak_weight,
        gold_strict=best_row["gold_heldout_strict"], gold_strict_ci95=result["gold_strict_ci95"],
        selection_strict=best_row["selection_ledger_strict"],
        alert_rule=dict(window_h=window_h, persistence_hours=persistence, ewma_span_hours=rule_doc["ewma_span"],
                        fa_budget_per_cow_day=fa_budget, max_alert_time_fraction=max_alert, threshold=thr, metric=rule,
                        rule=(f"12 小时产犊风险取本牛 {rule_doc['ewma_span']} 小时指数平滑（0=不平滑），连续 {persistence} 个小时 ≥ {thr:.2f} 即预警；"
                              f"开始产犊前 {window_h} 小时内预警处于开启状态记为命中（严格口径另报：预警起点须落在该 {window_h} 小时内）")),
        validation=(f"按牛分组 {folds} 折交叉验证（金标准牛与台账牛分别均匀分折）；台账产犊为弱标签（权重 {weak_weight}），"
                    f"算法与阈值只在台账牛上选择；金标准 {len(gold_cows)} 头牛只用于最终留出评估；单牧场，未做跨牧场外部验证"),
        complete=True,
    )
    (output / "decision.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    groups_importance = group_importance(docs_by[best], split, x, y, columns, column_keys)
    report = dict(result, manifest=manifest, group_importance=groups_importance, time_to_calving_eval=tte_eval,
                  column_importance=sorted(importance(json.loads((output / files[str(horizon)]["file"]).read_text(encoding="utf-8"))).items(),
                                           key=lambda kv: -kv[1])[:30],
                  elapsed_seconds=round(time.monotonic() - started, 1))
    (output / "training-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    with (output / "oof_predictions.csv").open("w", encoding="utf-8-sig", newline="") as s:
        wr = csv.writer(s)
        wr.writerow(["cow_id", "decision_epoch_ms", "hours_to_calving", "label_quality", "y", "risk_oof_calibrated"])
        for r, a, b in zip(labelled, y, cal):
            wr.writerow([r["cow_id"], int(r["decision_epoch_ms"]), r["hours_to_calving"], r.get("label_quality"), a, f"{b:.6f}"])
    return report
