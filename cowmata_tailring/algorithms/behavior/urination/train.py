"""Re-learn the urination pattern and model from the current dataset.

    python -m cowmata_tailring.algorithms.behavior.urination.train --dataset DATA --models MODELS --cache CACHE

Steps: scan labels -> Lift-Hold-Return candidates -> pattern profile ->
nested leave-one-cow-out evaluation (ExtraTrees, rule baseline) -> final fit ->
versioned bundle. The new version becomes active only when its honest
(LOCO) event F1 is not worse than the active one, unless --force is given.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import ALGORITHM_VERSION, SCHEMA
from .candidates import DEFAULT_PARAMS
from .dataset import build_candidates, label_candidates, scan_dataset
from .evaluate import assign_folds, best_threshold, event_metrics
from .features import feature_names
from .forest import export_forest, fit_forest, predict_forest
from .profile import build_profile, rule_score




def _fit_predict(tr, te, feats, n_estimators):
    m = tr.role.isin(["P", "N"])
    model, med = fit_forest(tr.loc[m, feats].to_numpy(np.float32), tr.loc[m, "y"].to_numpy(), tr.loc[m, "w"].to_numpy(),
                            n_estimators=n_estimators)
    x = te[feats].to_numpy(np.float32)
    return model.predict_proba(np.where(np.isfinite(x), x, med))[:, 1]


def loco(cand, events, sessions, feats, n_estimators=200, nested=True, log=print):
    fold_of, uri_cows = assign_folds(cand, events, sessions)
    folds = cand.cow.map(fold_of).fillna(-1).astype(int).to_numpy()
    oof = np.full(len(cand), np.nan)
    rule_oof = np.full(len(cand), np.nan)
    fold_thr, rule_thr = {}, {}
    for k in range(len(uri_cows)):
        te, tr = folds == k, (folds != k) & (folds >= 0)
        ctr = cand[tr]
        oof[te] = _fit_predict(ctr, cand[te], feats, n_estimators)
        prof = build_profile(ctr)
        rule_oof[te] = rule_score(cand[te], prof)
        if nested:
            inner = np.full(tr.sum(), np.nan)
            ftr = folds[tr]
            for j in sorted(set(ftr) - {k}):
                inner[ftr == j] = _fit_predict(ctr[ftr != j], ctr[ftr == j], feats, max(60, n_estimators // 3))
            str_ = sessions[sessions.cow.map(fold_of).fillna(-1).astype(int).ne(k) & sessions.has_uri]
            b = best_threshold(ctr, inner, events, str_)
            fold_thr[k] = b["threshold"] if b else 0.5
            rb = best_threshold(ctr, rule_score(ctr, prof), events, str_, grid=np.arange(0.5, 1.01, 1 / 8))
            rule_thr[k] = rb["threshold"] if rb else 1.0
        log(f"  fold {k + 1}/{len(uri_cows)} cow={uri_cows[k]} thr={fold_thr.get(k)}")
    return oof, rule_oof, folds, fold_thr, rule_thr, uri_cows


def nested_metrics(cand, score, folds, thr_by_fold, events, sessions, fold_of, scope_col="has_uri"):
    """Apply each fold's own (inner-chosen) threshold to its held-out cows."""
    t = np.array([thr_by_fold.get(f, 1.1) for f in folds])
    passed = np.where(score >= t, 1.0, 0.0)
    return event_metrics(cand, passed, 0.5, events, sessions, scope=sessions[scope_col])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--models", type=Path, required=True)
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--trees", type=int, default=256)
    ap.add_argument("--unknown-weight", type=float, default=0.5)
    ap.add_argument("--adapt", action="store_true", help="用规律画像自动调整候选阈值")
    ap.add_argument("--force", action="store_true", help="即使不优于当前版本也激活")
    ap.add_argument("--version", default=None)
    a = ap.parse_args(argv)
    began = time.time()
    log = lambda *x: print(*x, flush=True)

    sessions, events = scan_dataset(a.dataset)
    if sessions.empty or events.empty:
        raise ValueError("No labelled motion records were found")
    lab = sessions[sessions.n_events > 0].reset_index(drop=True)
    uri = events[events.code == "URINATION"]
    lab["has_uri"] = lab.asset_id.isin(set(uri.asset_id))
    lab["no_uri"] = ~lab.has_uri
    log(f"会话 {len(sessions)}，已标注 {len(lab)}（其中含排尿标注 {int(lab.has_uri.sum())}），排尿事件 {len(uri)}（已合并重复标注），排尿牛 {uri.cow.nunique()}")
    if uri.cow.nunique() < 3:
        raise ValueError("Nested cow-held-out validation requires at least three cows with urination labels")
    params = dict(DEFAULT_PARAMS)
    cand, metas = build_candidates(lab, params, a.cache, a.workers)
    cand = label_candidates(cand, lab, events, a.unknown_weight)
    if cand.empty or not cand.role.eq("P").any() or not cand.role.eq("N").any():
        raise ValueError("Training needs positive and background candidates")
    profile = build_profile(cand)
    if a.adapt and abs(profile["recommended_params"]["on_g"] - params["on_g"]) > 0.005:
        params.update(profile["recommended_params"])
        log("自适应候选阈值 on_g ->", params["on_g"])
        cand, metas = build_candidates(lab, params, a.cache, a.workers)
        cand = label_candidates(cand, lab, events, a.unknown_weight)
        profile = build_profile(cand)
    cand_recall = cand.loc[cand.role.eq("P"), "event_id"].nunique() / max(len(uri), 1)
    log(f"候选 {len(cand)} 个（{len(cand) / (lab.duration_s.sum() / 3600):.1f}/h），候选召回 {cand_recall:.3f}")
    feats = feature_names()

    log("嵌套留一头牛交叉验证 ...")
    oof, rule_oof, folds, fthr, rthr, uri_cows = loco(cand, events, lab, feats, a.trees, log=log)
    fold_of, _ = assign_folds(cand, events, lab)
    honest = nested_metrics(cand, oof, folds, fthr, events, lab, fold_of)
    honest_rule = nested_metrics(cand, rule_oof, folds, rthr, events, lab, fold_of)
    other_sessions = nested_metrics(cand, oof, folds, fthr, events, lab, fold_of, scope_col="no_uri")
    pooled = best_threshold(cand, oof, events, lab[lab.has_uri])
    curve = [event_metrics(cand, oof, t, events, lab, scope=lab.has_uri) for t in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)]
    log(f"LOCO 嵌套（含排尿标注会话）：召回 {honest['recall']:.3f} 精度下界 {honest['precision_lower_bound']:.3f} F1下界 {honest['f1_lower_bound']:.3f} 未核实报警/h {honest['unverified_alarms_per_hour']:.3f}")
    log(f"规则基线：召回 {honest_rule['recall']:.3f} 精度下界 {honest_rule['precision_lower_bound']:.3f} F1下界 {honest_rule['f1_lower_bound']:.3f}")
    log(f"未标排尿的已标注会话：报警 {other_sessions['detections']} 次 / {other_sessions['hours']:.0f} h = {other_sessions['unverified_alarms_per_hour']:.3f}/h（疑似漏标，进入复核队列）")

    thr = float(np.median(list(fthr.values()))) if fthr else pooled["threshold"]
    m = cand.role.isin(["P", "N"])
    model, med = fit_forest(cand.loc[m, feats].to_numpy(np.float32), cand.loc[m, "y"].to_numpy(), cand.loc[m, "w"].to_numpy(), n_estimators=a.trees)
    forest = export_forest(model, feats, med)
    x = cand[feats].to_numpy(np.float32)
    parity = float(np.max(np.abs(predict_forest(forest, x[:500]) - model.predict_proba(np.where(np.isfinite(x[:500]), x[:500], med))[:, 1])))
    imp = sorted(zip(feats, model.feature_importances_), key=lambda z: -z[1])

    fp = hashlib.sha256(pd.util.hash_pandas_object(uri[["asset_id", "start_s", "end_s"]].sort_values(["asset_id", "start_s"]), index=False).values.tobytes()).hexdigest()
    version = a.version or datetime.now().strftime("uri-%Y%m%d-%H%M%S")
    if not version or Path(version).name != version or version in {".", ".."} or "/" in version or "\\" in version:
        raise ValueError("Invalid model version")
    out = a.models / "versions" / version
    out.mkdir(parents=True, exist_ok=False)
    blob = json.dumps(forest, separators=(",", ":")).encode()
    (out / "model.json").write_bytes(blob)
    report = dict(version=version, algorithm=ALGORITHM_VERSION, created=datetime.now().isoformat(timespec="seconds"),
                  dataset=str(a.dataset), dataset_fingerprint=fp, sessions_labelled=int(len(lab)),
                  hours_labelled=float(lab.duration_s.sum() / 3600), urination_events=int(len(uri)),
                  urination_cows=[str(c) for c in uri_cows], candidates=int(len(cand)), candidate_recall=float(cand_recall),
                  loco_nested=honest, loco_rule_baseline=honest_rule, loco_sessions_without_urination_labels=other_sessions,
                  loco_pooled_best=pooled, operating_curve=curve,
                  fold_thresholds={str(uri_cows[k]): v for k, v in fthr.items()}, threshold=thr,
                  export_parity_max_abs=parity, feature_importance=[(f, float(v)) for f, v in imp[:25]],
                  unknown_weight=a.unknown_weight, elapsed_s=time.time() - began,
                  notes=["精度/误报以已标注会话中未命中排尿标签的检测计为未核实报警，是保守下界/上界。",
                         "阈值由每折内部的留一头牛交叉验证选出，外层牛从未参与阈值和模型选择。"])
    bundle = dict(schema=SCHEMA, version=version, code="URINATION", title="排尿", model_file="model.json",
                  model_sha256=hashlib.sha256(blob).hexdigest(), threshold=thr, params=params,
                  feature_version=ALGORITHM_VERSION, metrics=dict(recall=honest["recall"], precision_lower_bound=honest["precision_lower_bound"],
                  f1_lower_bound=honest["f1_lower_bound"], unverified_alarms_per_hour=honest["unverified_alarms_per_hour"]))
    (out / "bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    (out / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    cols = ["asset_id", "cow", "start_s", "end_s", "role", "event_id", "other_code", "dur_s", "peak_g", "lift_deg", "gyro_hold", "return_ratio"]
    oo = cand[cols].assign(oof_score=oof, fold=folds)
    oo.to_csv(out / "oof_candidates.csv", index=False, encoding="utf-8-sig")
    raw = dict(zip(lab.asset_id, lab.raw))
    review = oo[(oo.role == "N") & (oo.oof_score >= thr)].sort_values("oof_score", ascending=False).head(200)
    review.assign(raw=review.asset_id.map(raw)).to_csv(out / "review_queue.csv", index=False, encoding="utf-8-sig")
    missed = oo[oo.role.eq("P")].groupby("event_id").oof_score.max()
    ev_out = uri.assign(best_oof=uri.event_id.map(missed)).sort_values("best_oof")
    ev_out.to_csv(out / "events_oof.csv", index=False, encoding="utf-8-sig")

    active_ptr = a.models / "active.json"
    prev = None
    if active_ptr.is_file():
        prev_v = json.loads(active_ptr.read_text(encoding="utf-8"))["version"]
        pb = a.models / "versions" / prev_v / "bundle.json"
        if pb.is_file():
            prev = json.loads(pb.read_text(encoding="utf-8"))
    promote = a.force or prev is None or honest["f1_lower_bound"] >= prev["metrics"]["f1_lower_bound"] - 0.01
    bundle["events"] = int(len(uri))
    (out / "bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    if promote:
        active_ptr.write_text(json.dumps(dict(version=version), ensure_ascii=False), encoding="utf-8")
    log(f"模型 {version} 已保存 -> {out}；{'已激活' if promote else '未激活（未优于当前版本）'}；阈值 {thr:.3f}；导出一致性 {parity:.2e}")
    return out



def train(dataset, models, cache, *, workers=16, trees=256, unknown_weight=0.5,
          adapt=False, force=False, version=None):
    """Train and export a version under the explicitly supplied external model home.

    Returns the version directory; numerical code never writes inside the package.
    """
    args = ["--dataset", str(dataset), "--models", str(models), "--cache", str(cache),
            "--workers", str(workers), "--trees", str(trees),
            "--unknown-weight", str(unknown_weight)]
    if adapt:
        args.append("--adapt")
    if force:
        args.append("--force")
    if version is not None:
        args += ["--version", str(version)]
    return main(args)


if __name__ == "__main__":
    main()
