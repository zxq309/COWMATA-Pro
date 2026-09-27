"""Train, validate (leave-one-cow-out) and re-tune the straining bundle from a paired dataset.

Re-run `train_bundle` whenever labels are added: regularity, both forests and the operating
threshold are refit on all data, and the report shows drift against the previous bundle.
Original Raw/Label files are only read.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from ..metrics import evaluate_events
from ..models import export_forest, predict_forest
from . import BUNDLE_SCHEMA, CODE, signal
from .candidates import candidate_features, candidate_names
from .regularity import CONTEXT_CODES, drift, regularity_report

SKIP_FOLDERS = {"Unlabeled", "Temp", "HistoricalLabels"}
STAGE1 = dict(n_estimators=96, max_depth=10, min_samples_leaf=8, max_features="sqrt")
STAGE2 = dict(n_estimators=160, max_depth=8, min_samples_leaf=4, max_features="sqrt")
NEG_SECONDS_PER_RECORD = 400
SEED = 38


def _eligible(event):
    return event.get("confirmation") != "needs_review" and not event.get("source_time_review")


def collect_records(root):
    """Union labels per raw asset; positives = records with straining, negatives = non-calving."""
    from cowmata_tailring.annotation.taxonomy import canonical_code, event_code

    assets = {}
    root = Path(root)
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in SKIP_FOLDERS):
        for label in sorted(glob.glob(str(folder / "Motion" / "Label" / "*_label.json"))):
            raw = label.replace(os.sep + "Label" + os.sep, os.sep + "Raw" + os.sep).replace(
                "_label.json", "_raw.json"
            )
            if not os.path.isfile(raw):
                continue
            doc = json.loads(Path(label).read_bytes().decode("utf-8-sig"))
            work = doc.get("work", {})
            project = work.get("project", {})
            asset = (doc.get("dataset") or {}).get("raw_sha256") or work.get("asset_id")
            ident = project.get("device_identity") or doc.get("device_identity") or {}
            category = doc.get("dataset_category") or project.get("dataset_category") or ""
            row = assets.setdefault(
                asset,
                dict(
                    asset_id=asset,
                    raw=raw,
                    category=category,
                    events={},
                    cow_id=str(project.get("cow_id") or ident.get("cow_id") or ""),
                    device_id=str(ident.get("device_id") or ""),
                ),
            )
            for e in project.get("events", []):
                original, _ = event_code(e, project.get("labels", []))
                code = canonical_code(original, category)
                t0, t1 = e.get("t0"), e.get("t1")
                if not code or not isinstance(t0, (int, float)):
                    continue
                row["events"][(code, float(t0), None if t1 is None else float(t1))] = dict(
                    code=code,
                    start_ms=float(t0),
                    end_ms=None if t1 is None else float(t1),
                    confirmation=e.get("confirmation", ""),
                    source_time_review=bool(e.get("source_time_review")),
                )
    records = []
    for row in assets.values():
        row["events"] = sorted(row["events"].values(), key=lambda e: e["start_ms"])
        s = [e for e in row["events"] if e["code"] == CODE and e["end_ms"] and _eligible(e)]
        row["straining"] = s
        row["kind"] = "pos" if s else ("neg" if row["category"] != "calving" else None)
        if row["kind"] and row["cow_id"]:
            records.append(row)
    return records


def _feature_job(args):
    raw, asset, cache = args
    path = Path(cache) / f"{signal.FEATURE_VERSION}-{asset[:24]}.npz"
    if not path.exists():
        from cowmata_tailring.annotation.data import GRAVITY_MS2, parse_motion_object

        m = parse_motion_object(
            json.loads(Path(raw).read_bytes().decode("utf-8-sig")), source_path=raw
        )
        acc = np.column_stack([m.channels[k] for k in ("ax", "ay", "az")]) / GRAVITY_MS2
        gyr = np.column_stack([m.channels[k] for k in ("gx", "gy", "gz")])
        f = signal.second_features(m.times_ms, acc, gyr)
        tmp = path.with_suffix(".tmp.npz")
        np.savez_compressed(
            tmp,
            X=f["X"],
            valid=f["valid"],
            seconds=f["seconds"],
            names=np.array(f["names"]),
            duration_ms=f["duration_ms"],
            **{"p_" + k: v for k, v in f["pulses"].items()},
        )
        os.replace(tmp, path)
    return str(path)


def load_feature(path):
    with np.load(path, allow_pickle=False) as z:
        return dict(
            X=z["X"],
            valid=z["valid"],
            seconds=z["seconds"],
            names=[str(n) for n in z["names"]],
            duration_ms=float(z["duration_ms"]),
            pulses={k[2:]: z[k] for k in z.files if k.startswith("p_")},
        )


def _labels(rec, feature):
    s = feature["seconds"] * 1000
    y = np.zeros(len(s), int)
    guard = np.zeros(len(s), bool)
    for e in rec["straining"]:
        y[(s >= e["start_ms"]) & (s <= e["end_ms"])] = 1
        guard |= (s >= e["start_ms"] - 3000) & (s <= e["end_ms"] + 3000)
    return y, guard & (y == 0)


def _rows(items, rng):
    X, Y = [], []
    for it in items:
        v = it["feature"]["valid"] & ~it["guard"]
        pos = np.flatnonzero(v & (it["y"] == 1))
        neg = np.flatnonzero(v & (it["y"] == 0))
        if it["kind"] == "neg" and len(neg) > NEG_SECONDS_PER_RECORD:
            neg = rng.choice(neg, NEG_SECONDS_PER_RECORD, replace=False)
        sel = np.r_[pos, neg]
        X.append(it["feature"]["X"][sel])
        Y.append(it["y"][sel])
    return np.vstack(X), np.concatenate(Y)


def _forest(X, Y, params, n_jobs):
    from sklearn.ensemble import RandomForestClassifier

    median = np.nan_to_num(np.nanmedian(np.where(np.isfinite(X), X, np.nan), axis=0))
    model = RandomForestClassifier(
        **params, class_weight="balanced_subsample", n_jobs=n_jobs, random_state=SEED
    ).fit(np.where(np.isfinite(X), X, median), Y)
    return model, median


def _predict(model, median, X):
    return model.predict_proba(np.where(np.isfinite(X), X, median))[:, 1]


def _folds(items):
    pos = sorted({it["cow_id"] for it in items if it["kind"] == "pos"})
    if len(pos) < 3:
        raise ValueError("努责至少需要 3 头有标签的牛才能做按牛留一验证")
    fold = {c: i for i, c in enumerate(pos)}
    for i, c in enumerate(sorted({it["cow_id"] for it in items} - set(pos))):
        fold[c] = i % len(pos)
    return pos, fold


def _curve(items, cand, p2):
    known = sum(len(it["straining"]) for it in items if it["kind"] == "pos")
    hours = sum(it["feature"]["valid"].sum() / 3600 for it in items if it["kind"] == "neg")
    neg = np.array([items[c["item"]]["kind"] == "neg" for c in cand], bool)
    y = np.array([c["matched"] for c in cand], bool)
    rows = []
    for t in np.round(np.arange(0.10, 0.91, 0.025), 3):
        keep = p2 >= t
        tp = int((keep & y).sum())
        in_calving = int((keep & ~neg).sum())
        fp = int((keep & neg).sum())
        rows.append(
            dict(
                threshold=float(t),
                recall=tp / known if known else 0.0,
                precision_in_calving_records=tp / in_calving if in_calving else None,
                false_alarms_per_hour_non_calving=fp / hours if hours else None,
                matched=tp,
                known=known,
                candidates_calving=in_calving,
                false_alarms_non_calving=fp,
            )
        )
    return rows


def _choose(curve, max_fa_per_hour):
    ok = [r for r in curve if (r["false_alarms_per_hour_non_calving"] or 0) <= max_fa_per_hour]
    pool = ok or curve
    return max(pool, key=lambda r: (round(r["recall"], 3), r["threshold"]))


def train_bundle(
    dataset_root,
    output,
    *,
    cache=None,
    previous=None,
    max_fa_per_hour=0.5,
    candidate_threshold=0.45,
    workers=16,
    progress=print,
):
    started = time.monotonic()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cache = Path(cache or output / "feature-cache")
    cache.mkdir(parents=True, exist_ok=True)
    records = collect_records(dataset_root)
    progress(
        f"记录 {len(records)}（含努责 {sum(r['kind'] == 'pos' for r in records)}），{workers} 路并行提取特征"
    )
    with ProcessPoolExecutor(max_workers=workers) as pool:
        paths = list(
            pool.map(_feature_job, [(r["raw"], r["asset_id"], str(cache)) for r in records])
        )
    items = []
    for r, p in zip(records, paths):
        f = load_feature(p)
        y, guard = _labels(r, f)
        items.append(dict(r, feature=f, y=y, guard=guard))
    names = items[0]["feature"]["names"]
    pos_cows, fold = _folds(items)
    rng = np.random.default_rng(SEED)
    # Stage 1 out-of-fold second scores (leave one straining cow out).
    oof = [None] * len(items)
    for k in range(len(pos_cows)):
        X, Y = _rows([it for it in items if fold[it["cow_id"]] != k], rng)
        m, med = _forest(X, Y, STAGE1, workers)
        for i, it in enumerate(items):
            if fold[it["cow_id"]] == k:
                oof[i] = _predict(m, med, it["feature"]["X"])
        progress(f"阶段1 留一牛验证 {k + 1}/{len(pos_cows)}")
    seg = dict(
        signal.DEFAULT_SEGMENTATION,
        high=candidate_threshold,
        low=round(candidate_threshold * 0.7, 3),
    )
    cand, cand_x = [], []
    for i, it in enumerate(items):
        ev, sm = signal.segment(
            oof[i],
            it["feature"]["valid"],
            it["feature"]["pulses"],
            seg,
            it["feature"]["duration_ms"],
        )
        matched = np.zeros(len(ev), bool)
        if it["kind"] == "pos" and ev:
            for _, j in evaluate_events(it["straining"], ev, observed_seconds=1)["pairs"]:
                matched[j] = True
        if ev:
            cand_x.append(candidate_features(it["feature"], ev, sm))
        cand += [dict(item=i, event=e, matched=bool(mt)) for e, mt in zip(ev, matched)]
    CX = np.vstack(cand_x) if cand_x else np.zeros((0, len(candidate_names())), np.float32)
    CY = np.array([c["matched"] for c in cand], int)
    CG = np.array([fold[items[c["item"]]["cow_id"]] for c in cand])
    if len(set(CY)) < 2:
        raise ValueError("候选中缺少命中或未命中样本，无法训练第二阶段")
    p2 = np.zeros(len(cand))
    for k in range(len(pos_cows)):
        tr, te = CG != k, CG == k
        if te.any() and len(set(CY[tr])) == 2:
            m, med = _forest(CX[tr], CY[tr], STAGE2, workers)
            p2[te] = _predict(m, med, CX[te])
    curve = _curve(items, cand, p2)
    chosen = _choose(curve, max_fa_per_hour)
    # Per-cow result at the chosen operating point.
    per_cow = {}
    for c, p in zip(cand, p2):
        it = items[c["item"]]
        if it["kind"] == "pos" and c["matched"] and p >= chosen["threshold"]:
            per_cow.setdefault(it["cow_id"], [0, 0])[0] += 1
    for it in items:
        if it["kind"] == "pos":
            per_cow.setdefault(it["cow_id"], [0, 0])[1] += len(it["straining"])
    # Final models on all data.
    X, Y = _rows(items, rng)
    m1, med1 = _forest(X, Y, STAGE1, workers)
    m2, med2 = _forest(CX, CY, STAGE2, workers)
    stage1, stage2 = export_forest(m1, names, med1), export_forest(m2, candidate_names(), med2)
    check = items[0]["feature"]["X"][:300]
    assert np.allclose(predict_forest(stage1, check), _predict(m1, med1, check), atol=1e-6)
    assert np.allclose(predict_forest(stage2, CX[:300]), _predict(m2, med2, CX[:300]), atol=1e-6)
    # Regularity from labels (independent of the models).
    reg = regularity_report(
        [
            dict(
                cow_id=it["cow_id"],
                feature=it["feature"],
                straining=it["straining"],
                context={c: [e for e in it["events"] if e["code"] == c] for c in CONTEXT_CODES},
            )
            for it in items
            if it["kind"] == "pos"
        ]
    )
    prev_reg = prev_version = None
    if previous:
        prev_doc = json.loads(
            (Path(previous) / "straining_bundle.json").read_text(encoding="utf-8")
        )
        prev_reg, prev_version = prev_doc.get("regularity"), prev_doc.get("version")
    fingerprint = hashlib.sha256(
        json.dumps(
            sorted(
                (r["asset_id"], [(e["start_ms"], e["end_ms"]) for e in r["straining"]])
                for r in records
            )
        ).encode()
    ).hexdigest()
    version = time.strftime("straining-%Y%m%d-%H%M") + "-" + fingerprint[:8]
    validation = dict(
        protocol="leave-one-straining-cow-out; stage2 trained on out-of-fold stage1 candidates; "
        "threshold chosen on the out-of-fold curve (max recall with non-calving false alarms "
        f"<= {max_fa_per_hour}/h)",
        chosen=chosen,
        curve=curve,
        per_cow={
            c: dict(matched=v[0], known=v[1], recall=v[0] / v[1])
            for c, v in sorted(per_cow.items())
        },
        stage1_candidate_recall=float(CY.sum() / max(chosen["known"], 1)),
        records=len(items),
        straining_records=sum(it["kind"] == "pos" for it in items),
        straining_cows=len(pos_cows),
        straining_bouts=chosen["known"],
        non_calving_hours=float(
            sum(it["feature"]["valid"].sum() / 3600 for it in items if it["kind"] == "neg")
        ),
    )
    bundle = dict(
        schema=BUNDLE_SCHEMA,
        code=CODE,
        version=version,
        feature_version=signal.FEATURE_VERSION,
        dataset_fingerprint=fingerprint,
        stage1_feature_names=names,
        stage1=stage1,
        stage2=stage2,
        segmentation=seg,
        stage2_threshold=chosen["threshold"],
        regularity=reg,
        regularity_drift=drift(prev_reg, reg),
        previous_version=prev_version,
        validation={k: v for k, v in validation.items() if k != "curve"},
        feature_importance=dict(
            sorted(zip(names, map(float, m1.feature_importances_)), key=lambda kv: -kv[1])[:15]
        ),
        requires_review=True,
        score_is_probability=False,
    )
    tmp = output / "straining_bundle.json.tmp"
    tmp.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, output / "straining_bundle.json")
    (output / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (output / "regularity.json").write_text(
        json.dumps(
            dict(regularity=reg, drift=bundle["regularity_drift"]), ensure_ascii=False, indent=1
        ),
        encoding="utf-8",
    )
    progress(
        f"完成 {version}：召回 {chosen['recall']:.3f}，产犊记录内精确率 "
        f"{chosen['precision_in_calving_records'] or 0:.3f}，非产犊误报 {chosen['false_alarms_per_hour_non_calving']:.3f}/h，"
        f"用时 {time.monotonic() - started:.0f}s"
    )
    return bundle
