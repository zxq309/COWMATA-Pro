"""Train / re-tune the tail-ring lying-down (LYING_DOWN) algorithm from COWMATA_Behavior_Dataset.

Re-run whenever labels are added: the per-record cache is keyed by the raw SHA-256, so only new
records are decoded. Every run re-derives the physical regularities, re-validates with
leave-one-cow-out (LOCO), re-selects the threshold and boundary corrections, and writes a new
versioned suite that the application imports like any other behaviour model.

Usage (from the source root, Python 3.12+):
    python scripts/train_liedown.py --dataset "%COWMATA_DATASET_HOME%\\COWMATA_Behavior_Dataset" \
        --output "%COWMATA_ALGORITHM_HOME%\\runs\\liedown-20260925-01" --workers 16
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cowmata_tailring.algorithms import liedown as LD  # noqa: E402

CACHE_VERSION = LD.ALGORITHM + "-cache-1"
POSTURE_STEP_S = 10


# ----------------------------------------------------------------------------- dataset index
def read_label(path: Path, root: Path):
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    project = doc["work"]["project"]
    identity = doc.get("device_identity") or project.get("device_identity") or {}
    source = doc.get("source") or {}
    rel = source.get("path") or project["source"]["path"]
    events = []
    for e in project.get("events", []):
        code = e.get("label_code") or (e.get("legacy") or {}).get("code")
        if code is None or e.get("t0") is None:
            continue
        t0 = float(e["t0"])
        t1 = float(e["t1"]) if e.get("t1") is not None else t0
        events.append(dict(code=code, start_ms=t0, end_ms=max(t0, t1)))
    return dict(raw=str(root / rel.replace("/", os.sep)), asset_id=source.get("asset_id") or project["source"].get("asset_id"),
                cow_id=str(identity.get("cow_id", "")), device_id=str(identity.get("device_id", "")),
                label=str(path), events=events)


def build_index(root: Path):
    records = {}
    for label in sorted(root.glob("*/Motion/Label/*_label.json")):
        try:
            r = read_label(label, root)
        except (OSError, ValueError, KeyError) as exc:
            print("skip label", label, exc)
            continue
        cur = records.setdefault(r["asset_id"], dict(r, labels=[]))
        cur["labels"].append(r["label"])
        seen = {(e["code"], e["start_ms"], e["end_ms"]) for e in cur["events"]}
        for e in r["events"]:
            if (e["code"], e["start_ms"], e["end_ms"]) not in seen:
                cur["events"].append(e)
                seen.add((e["code"], e["start_ms"], e["end_ms"]))
    return list(records.values())


def dedupe_events(events, code):
    out = []
    for e in sorted((e for e in events if e["code"] == code), key=lambda e: e["start_ms"]):
        if out and e["start_ms"] <= out[-1]["end_ms"]:
            out[-1]["end_ms"] = max(out[-1]["end_ms"], e["end_ms"])
            continue
        out.append(dict(e))
    return out


# ----------------------------------------------------------------------------- per record work
def extract(args):
    record, cache_dir = args
    cache = Path(cache_dir) / f"{record['asset_id']}.{CACHE_VERSION}.npz"
    if cache.is_file():
        z = np.load(cache, allow_pickle=False)
        return record["asset_id"], {k: z[k] for k in z.files}
    from cowmata_tailring.annotation.data import GRAVITY_MS2, load_motion_json
    content = Path(record["raw"]).read_bytes()
    if hashlib.sha256(content).hexdigest() != record["asset_id"]:
        raise ValueError("Raw content does not match its label identity: " + record["raw"])
    motion = load_motion_json(record["raw"])
    acc = np.column_stack([motion.channels[k] for k in ("ax", "ay", "az")]) / GRAVITY_MS2
    gyro = np.column_stack([motion.channels[k] for k in ("gx", "gy", "gz")])
    s = LD.prepare(motion.times_ms, acc, gyro)
    peaks = LD.candidates(s)
    x = LD.event_features(s, peaks)
    bounds = np.array([LD.boundaries(s, int(i)) for i in peaks], dtype=np.float64).reshape(-1, 2) / LD.HZ
    px = LD.posture_seconds(s).astype(np.float32)
    out = dict(peaks_s=peaks / LD.HZ, X=x, bounds_s=bounds, PX=px, hours=np.array([s["n"] / LD.HZ / 3600]),
               calib=np.array(s["calib"]["offset"] + [float(s["calib"]["corrected"])]))
    tmp = cache.with_suffix(".tmp.npz")
    np.savez(tmp, **out)
    os.replace(tmp, cache)
    return record["asset_id"], out


# ----------------------------------------------------------------------------- models
def fit_forest(x, y, *, trees, leaf, depth=None, seed=0):
    from sklearn.ensemble import RandomForestClassifier
    median = np.nanmedian(x, 0)
    median = np.where(np.isfinite(median), median, 0.0)
    xi = np.where(np.isfinite(x), x, median)
    model = RandomForestClassifier(trees, min_samples_leaf=leaf, max_depth=depth, max_features="sqrt",
                                   class_weight="balanced_subsample", n_jobs=-1, random_state=seed)
    model.fit(xi.astype(np.float32), y)
    return model, median


def forest_json(model, names, median):
    from cowmata_tailring.algorithms.models import export_forest
    return export_forest(model, names, median)


def predict(model, median, x):
    xi = np.where(np.isfinite(x), x, median).astype(np.float32)
    return model.predict_proba(xi)[:, 1]


POSTURE_CFG = dict(trees=64, leaf=30, depth=14)
EVENT_CFG = dict(trees=200, leaf=2, depth=None)


def match(det_t, det_bounds, gt, tol=3.0):
    """One-to-one event matching: a detection point inside [start-tol, end+tol]."""
    used, pairs = set(), []
    for k, e in enumerate(gt):
        cand = [j for j, t in enumerate(det_t) if j not in used and e["start_ms"] / 1000 - tol <= t <= e["end_ms"] / 1000 + tol]
        if cand:
            mid = (e["start_ms"] + e["end_ms"]) / 2000
            j = min(cand, key=lambda j: abs(det_t[j] - mid))
            used.add(j)
            pairs.append((k, j))
    return pairs


def nms(t, score, thr, sep):
    keep = []
    for j in np.argsort(-score):
        if score[j] < thr:
            break
        if all(abs(t[j] - t[k]) > sep for k in keep):
            keep.append(j)
    return sorted(keep)


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _safe_extract(args):
    try:
        return extract(args)
    except Exception:
        return None


def _forest_eval(model, x):
    from cowmata_tailring.algorithms.models import predict_forest
    x = np.asarray(x, dtype=np.float64)
    return predict_forest(model, x) if len(x) else np.zeros(0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--thresholds", default="0.2,0.25,0.3,0.35,0.4,0.5,0.6")
    ap.add_argument("--plausibility", type=int, default=400,
                    help="unlabelled records scored with the final model as a field-rate sanity check (0 = off)")
    args = ap.parse_args()
    started = time.monotonic()
    root, out = Path(args.dataset).resolve(), Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache).resolve() if args.cache else out.parent.parent / "cache" / "liedown"
    cache.mkdir(parents=True, exist_ok=True)

    everything = build_index(root)
    # Unreviewed recordings are not negatives: train/validate only on records carrying event labels.
    records = [r for r in everything if r["events"]]
    unlabelled = [r for r in everything if not r["events"]]
    print(f"[1/6] index: {len(everything)} unique raw records, {len(records)} with event labels, workers={args.workers}", flush=True)
    data, issues = {}, []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(extract, (r, str(cache))): r for r in records}
        for n, fut in enumerate(futures, 1):
            r = futures[fut]
            try:
                sha, d = fut.result()
                data[sha] = d
            except Exception as exc:  # keep going; the issue is reported
                issues.append(dict(raw=r["raw"], reason=repr(exc)))
            if n % 25 == 0 or n == len(futures):
                print(f"      extracted {n}/{len(futures)}", flush=True)
    records = [r for r in records if r["asset_id"] in data]
    cows = sorted({r["cow_id"] for r in records})
    hours = sum(float(data[r["asset_id"]]["hours"][0]) for r in records)

    # ---- candidate table
    rows_x, meta = [], []
    for r in records:
        d = data[r["asset_id"]]
        lds, sus = dedupe_events(r["events"], "LYING_DOWN"), dedupe_events(r["events"], "STANDING_UP")
        lab = np.zeros(len(d["peaks_s"]), dtype=int)
        for code, evs in ((2, sus), (1, lds)):
            for e in evs:
                m = (d["peaks_s"] >= e["start_ms"] / 1000 - 3) & (d["peaks_s"] <= e["end_ms"] / 1000 + 3)
                lab[m] = code
        for j in range(len(lab)):
            meta.append((r["asset_id"], r["cow_id"], j, int(lab[j])))
        rows_x.append(d["X"])
    X = np.vstack(rows_x) if rows_x else np.zeros((0, len(LD.EVENT_FEATURES)))
    sha_col = np.array([m[0] for m in meta]); cow_col = np.array([m[1] for m in meta])
    j_col = np.array([m[2] for m in meta]); lab_col = np.array([m[3] for m in meta])
    t_col = np.concatenate([data[r["asset_id"]]["peaks_s"] for r in records])
    b_col = np.vstack([data[r["asset_id"]]["bounds_s"] for r in records])
    gt = {r["asset_id"]: dedupe_events(r["events"], "LYING_DOWN") for r in records}
    n_gt = sum(len(v) for v in gt.values())
    print(f"[2/6] candidates {len(X)} ({len(X) / hours:.1f}/h), labelled lying-down {n_gt}, cows {len(cows)}", flush=True)

    # ---- regularity report (the "law")
    f = {k: X[:, i] for i, k in enumerate(LD.EVENT_FEATURES)}
    law_rows = []
    for r in records:
        d = data[r["asset_id"]]
        for e in gt[r["asset_id"]]:
            m = np.flatnonzero((d["peaks_s"] >= e["start_ms"] / 1000 - 3) & (d["peaks_s"] <= e["end_ms"] / 1000 + 3))
            row = dict(cow_id=r["cow_id"], raw=Path(r["raw"]).name, start_s=e["start_ms"] / 1000,
                       duration_s=(e["end_ms"] - e["start_ms"]) / 1000, has_candidate=bool(m.size))
            if m.size:
                best = m[np.argmax(d["X"][m, LD.EVENT_FEATURES.index("ang_l")])]
                for k in ("ang_l", "dz_l", "droll_abs", "e_core", "e_peak", "burst_ratio", "retain_post", "trans_s"):
                    row[k] = float(d["X"][best, LD.EVENT_FEATURES.index(k)])
            law_rows.append(row)
    write_csv(out / "卧倒逐事件规律.csv", law_rows)

    def share(key, fn):
        v = np.array([r[key] for r in law_rows if key in r and np.isfinite(r[key])])
        return float(np.mean(fn(v))) if v.size else None
    def q(key):
        v = np.array([r[key] for r in law_rows if key in r and np.isfinite(r[key])])
        return {p: float(np.percentile(v, p)) for p in (5, 25, 50, 75, 95)} if v.size else None
    law = dict(
        events=len(law_rows), cows=len({r["cow_id"] for r in law_rows}),
        candidate_coverage=float(np.mean([r["has_candidate"] for r in law_rows])) if law_rows else None,
        duration_s=q("duration_s"), orientation_change_deg=q("ang_l"), z_change_g=q("dz_l"),
        burst_gyro_dps=q("e_core"), orientation_retention_deg=q("retain_post"),
        share_orientation_ge_10deg=share("ang_l", lambda v: v >= 10), share_z_drop=share("dz_l", lambda v: v < 0),
        share_burst_ge_18dps=share("e_core", lambda v: v >= 18), share_burst_ratio_positive=share("burst_ratio", lambda v: v >= 0),
        share_all_four=None)
    four = [r for r in law_rows if "ang_l" in r]
    if four:
        law["share_all_four"] = float(np.mean([(r["ang_l"] >= 10) and (r["dz_l"] < -0.03) and (r["e_core"] >= 18) and (r["burst_ratio"] >= 0) for r in four]))
    print("[3/6] law:", json.dumps({k: law[k] for k in ("events", "share_orientation_ge_10deg", "share_z_drop", "share_burst_ge_18dps", "share_all_four")}), flush=True)

    # ---- posture OOF (LOCO) and final posture model
    def posture_xy(sel_cows):
        xs, ys = [], []
        for r in records:
            if r["cow_id"] not in sel_cows:
                continue
            d = data[r["asset_id"]]
            lab = LD.posture_labels(r["events"], len(d["PX"]))
            k = np.arange(0, len(lab), POSTURE_STEP_S)
            k = k[lab[k] >= 0]
            xs.append(d["PX"][k]); ys.append(lab[k])
        return (np.vstack(xs), np.concatenate(ys)) if xs else (None, None)
    p_oof, posture_acc = {}, []
    for c in cows:
        xtr, ytr = posture_xy(set(cows) - {c})
        m, med = fit_forest(xtr, ytr, **POSTURE_CFG)
        for r in records:
            if r["cow_id"] != c:
                continue
            d = data[r["asset_id"]]
            p = predict(m, med, d["PX"]).astype(np.float64)
            p[~np.isfinite(d["PX"]).all(1)] = np.nan
            p_oof[r["asset_id"]] = p
            lab = LD.posture_labels(r["events"], len(p))
            k = (lab >= 0) & np.isfinite(p)
            if k.any():
                posture_acc.append(dict(cow_id=c, seconds=int(k.sum()), correct=int(((p[k] > .5) == lab[k]).sum())))
    pa = sum(x["correct"] for x in posture_acc) / max(1, sum(x["seconds"] for x in posture_acc))
    print(f"[4/6] posture LOCO per-second accuracy {pa:.3f}", flush=True)
    ctx = np.vstack([LD.posture_context(p_oof[r["asset_id"]], data[r["asset_id"]]["peaks_s"]) for r in records])
    XM = np.hstack([X, ctx])

    # ---- event LOCO
    y = (lab_col == 1).astype(int)
    score = np.zeros(len(XM))
    for c in cows:
        te = cow_col == c
        if te.sum() == 0:
            continue
        m, med = fit_forest(XM[~te], y[~te], **EVENT_CFG)
        score[te] = predict(m, med, XM[te])
    thresholds = [float(v) for v in args.thresholds.split(",")]
    evals, best = [], None
    for thr in thresholds:
        tp = fp = 0; on, off = [], []
        for r in records:
            sha = r["asset_id"]; sel = np.flatnonzero(sha_col == sha)
            keep = sel[nms(t_col[sel], score[sel], thr, LD.DEFAULT_PARAMS["nms_s"])]
            pairs = match(t_col[keep], b_col[keep], gt[sha])
            tp += len(pairs); fp += len(keep) - len(pairs)
            for k, j in pairs:
                on.append(b_col[keep[j], 0] - gt[sha][k]["start_ms"] / 1000)
                off.append(b_col[keep[j], 1] - gt[sha][k]["end_ms"] / 1000)
        P = tp / max(1, tp + fp); R = tp / max(1, n_gt); F1 = 2 * P * R / max(1e-9, P + R)
        row = dict(threshold=thr, TP=tp, FN=n_gt - tp, FP=fp, precision=P, recall=R, F1=F1, FP_per_hour=fp / hours,
                   onset_bias_s=float(np.median(on)) if on else 0.0, offset_bias_s=float(np.median(off)) if off else 0.0,
                   onset_abs_median_s=float(np.median(np.abs(np.array(on) - np.median(on)))) if on else None,
                   offset_abs_median_s=float(np.median(np.abs(np.array(off) - np.median(off)))) if off else None)
        evals.append(row)
        if best is None or F1 > best["F1"] + 1e-9:
            best = row
    write_csv(out / "LOCO逐阈值评估.csv", evals)
    print("[5/6] LOCO best:", json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in best.items()}), flush=True)

    # per-cow metrics, misses and suspected missing labels at the chosen threshold
    thr = best["threshold"]; per_cow = {}; misses, suspects = [], []
    for r in records:
        sha = r["asset_id"]; sel = np.flatnonzero(sha_col == sha)
        keep = sel[nms(t_col[sel], score[sel], thr, LD.DEFAULT_PARAMS["nms_s"])]
        pairs = match(t_col[keep], b_col[keep], gt[sha])
        pc = per_cow.setdefault(r["cow_id"], dict(cow_id=r["cow_id"], events=0, TP=0, FP=0, hours=0.0))
        pc["events"] += len(gt[sha]); pc["TP"] += len(pairs); pc["FP"] += len(keep) - len(pairs)
        pc["hours"] += float(data[sha]["hours"][0])
        hit = {k for k, _ in pairs}; used = {j for _, j in pairs}
        for k, e in enumerate(gt[sha]):
            if k not in hit:
                misses.append(dict(cow_id=r["cow_id"], raw=r["raw"], start_s=e["start_ms"] / 1000, end_s=e["end_ms"] / 1000))
        for j, idx in enumerate(keep):
            if j not in used:
                suspects.append(dict(cow_id=r["cow_id"], raw=r["raw"], point_s=float(t_col[idx]), start_s=float(b_col[idx, 0]),
                                     end_s=float(b_col[idx, 1]), score=float(score[idx]),
                                     standing_up_labelled_here=bool(lab_col[idx] == 2), labels=";".join(r["labels"])))
    for pc in per_cow.values():
        pc["recall"] = pc["TP"] / pc["events"] if pc["events"] else None
        pc["FP_per_hour"] = pc["FP"] / pc["hours"] if pc["hours"] else None
    write_csv(out / "LOCO逐牛评估.csv", sorted(per_cow.values(), key=lambda r: r["cow_id"]))
    write_csv(out / "漏检卧倒.csv", misses)
    write_csv(out / "疑似漏标卧倒-待视频复核.csv", sorted(suspects, key=lambda r: -r["score"]))
    write_csv(out / "数据问题.csv", issues or [dict(raw="", reason="")])

    # ---- final models (posture on all cows; event model on LOCO posture context)
    xall, yall = posture_xy(set(cows))
    pm, pmed = fit_forest(xall, yall, **POSTURE_CFG)
    em, emed = fit_forest(XM, y, **EVENT_CFG)
    params = dict(LD.DEFAULT_PARAMS, onset_bias_s=best["onset_bias_s"], offset_bias_s=best["offset_bias_s"])
    fingerprint = hashlib.sha256("\n".join(sorted(r["asset_id"] + ":" + json.dumps(r["events"], sort_keys=True) for r in records)).encode()).hexdigest()
    model = forest_json(em, LD.MODEL_FEATURES, emed)
    model.update(code=LD.CODE, feature_version="event-shape-1", threshold=round(float(thr), 4), algorithm=LD.ALGORITHM,
                 posture=forest_json(pm, LD.POSTURE_FEATURES, pmed), params=params, score_is_probability=False,
                 dataset_fingerprint=fingerprint, law=law,
                 validation_summary=dict(unit="leave-one-cow-out", cows=len(cows), known_events=n_gt,
                                         matched_events=best["TP"], known_recall=best["recall"], precision_vs_labels=best["precision"],
                                         F1=best["F1"], unmatched_per_hour=best["FP_per_hour"], posture_second_accuracy=pa))
    from cowmata_tailring.workspace.storage import atomic_json
    from cowmata_tailring.workspace.shared_labels import CONTRACT
    atomic_json(out / "lying_down.json", model)
    plaus = None
    if args.plausibility and unlabelled:
        step = max(1, len(unlabelled) // args.plausibility)
        sample = unlabelled[::step][: args.plausibility]
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            got = [f for f in pool.map(_safe_extract, [(r, str(cache)) for r in sample]) if f is not None]
        n_det, h, per_rec = 0, 0.0, []
        for sha, d in got:
            ph = _forest_eval(model["posture"], d["PX"])
            ctx_u = LD.posture_context(ph, d["peaks_s"])
            sc = _forest_eval(model, np.hstack([d["X"], ctx_u])) if len(d["X"]) else np.zeros(0)
            k = nms(d["peaks_s"], sc, thr, LD.DEFAULT_PARAMS["nms_s"])
            n_det += len(k); h += float(d["hours"][0]); per_rec.append(len(k))
        plaus = dict(records=len(got), hours=h, detections=n_det, per_hour=n_det / h if h else None,
                     per_day=24 * n_det / h if h else None, records_with_any=float(np.mean(np.array(per_rec) > 0)) if per_rec else None,
                     literature="9 +/- 3 lying bouts per day (Ito et al. 2009) = 0.25-0.5 per hour")
        print("      plausibility on unlabelled data:", json.dumps(plaus), flush=True)
    report = dict(models=[dict(code=LD.CODE, algorithm=LD.ALGORITHM, **model["validation_summary"])],
                  law=law, loco=evals, posture_accuracy_by_cow=posture_acc, settings=dict(posture=POSTURE_CFG, event=EVENT_CFG, params=params),
                  hours=hours, records=len(records), unlabelled_plausibility=plaus, issues=issues, elapsed_seconds=time.monotonic() - started,
                  interpretation="LOCO; unmatched detections include unlabeled real lying-downs, see 疑似漏标卧倒-待视频复核.csv")
    atomic_json(out / "评估报告.json", report)
    manifest = dict(shared_label_contract=dict(CONTRACT), schema="cowmata-event-suite-1", version=out.name,
                    feature_version="event-shape-1", modality="motion", training_version=LD.ALGORITHM,
                    dataset_fingerprint=fingerprint,
                    models=[dict(code=LD.CODE, title="卧倒过程", file="lying_down.json", modality="motion",
                                 threshold=model["threshold"], experimental=False, requires_video_confirmation=False,
                                 sha256=hashlib.sha256((out / "lying_down.json").read_bytes()).hexdigest())],
                    report="评估报告.json", complete=True, elapsed_seconds=time.monotonic() - started)
    atomic_json(out / "suite.json", manifest)
    print(f"[6/6] suite written: {out}  ({time.monotonic() - started:.0f} s)", flush=True)


if __name__ == "__main__":
    main()