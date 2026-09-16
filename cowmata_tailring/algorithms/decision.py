"""Causal evidence fusion and cow-held-out calving decision models."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import warnings
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from cowmata_tailring.edge_download.csv_targets import cow_identity, parse_time
from cowmata_tailring.temperature import (
    CONTRACT,
    read_temperature_record,
    validate_contract,
    validate_temperature_identity,
)
from cowmata_tailring.workspace.storage import atomic_json

from .analysis import load_features, write_table
from .evidence import add_baselines, evidence_rows, infer_features
from .inputs import scan_inputs
from .models import export_forest, predict_forest
from .registry import child, read_suite

FEATURES = (
    "activity_index",
    "motion_coverage",
    "lying_fraction_known",
    "known_posture_coverage",
    "straining_onsets",
    "straining_seconds",
    "temperature_c",
    "activity_index_change",
    "temperature_c_change",
    "heart_rate_bpm",
    "spo2_percent",
    "ppg_coverage",
    "ppg_straining_onsets",
)
ALGORITHMS = {
    "xgboost": "XGBoost 梯度提升树",
    "random_forest": "随机森林",
    "decision_tree": "决策树",
}
SCHEMA = "cowmata-calving-decision-3.9"


def external_temperatures(root):
    rows, issues = [], []
    for file in Path(root).rglob("*.json"):
        relative = file.relative_to(Path(root))
        if "temp" not in {p.casefold() for p in relative.parts} or any(p.startswith(".") or p == "标注工程" for p in relative.parts):
            continue
        try:
            from cowmata_tailring.workspace.device_identity import resolve_device_identity

            data = json.loads(file.read_text(encoding="utf-8-sig"))
            owner = resolve_device_identity(file, data.get("device"))
            if owner["status"] != "ready":
                raise ValueError(owner["message"])
            sample = read_temperature_record(data)
            validate_temperature_identity(data, owner["cow_id"], owner["field_mark"])
            value = sample["value"]
            if not -30 <= value <= 80:
                raise ValueError("独立温度数值需要核对")
            rows.append(
                dict(
                    cow=owner["cow_id"],
                    device=owner["device_id"],
                    mark=owner["field_mark"],
                    time=int(data["create_time"]),
                    value=value,
                    source=str(file),
                    available_at_ms=sample["available_at_ms"],
                    time_basis=sample["time_basis"],
                    sample_id=sample["sample_id"],
                    source_kind=sample["source_kind"],
                )
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            issues.append(dict(path=str(file), reason=str(exc)))
    return rows, issues


def build_fusion(root, suites, output, cache, *, codes=None, selections=None, progress=lambda *_: None,
                 input_index=None, include_external_temperature=True, input_temperatures=None):
    index = input_index if input_index is not None else scan_inputs(root, progress=progress)
    selected = [read_suite(p) for p in suites]
    for suite in selected:
        allowed = (selections or {}).get(str(suite['root']))
        suite['models'] = [m for m in suite['models'] if (allowed is None or m['code'] in allowed)
                           and (codes is None or m['code'] in codes)]
    selected = [suite for suite in selected if suite['models']]
    rows, issues = [], list(index["issues"])
    for i, record in enumerate(index["records"]):
        try:
            f = load_features(record, cache)
            events = []
            versions = []
            for suite in selected:
                if suite.get("modality", "motion") != record["modality"]:
                    continue
                events.extend(infer_features(suite, f, codes))
                versions.append(suite["version"])
            # Event decisions use context after their peak. Delay the entire evidence row
            # by the maximum context look-ahead, and join temperature only up to that cutoff.
            record_rows = evidence_rows(record, f, events, causal=True)
            for row in record_rows:
                row["device_id"] = record["device_id"]
                row["field_mark"] = record["field_mark"]
                row["modality"] = record["modality"]
                row["ppg_coverage"] = (
                    row["motion_coverage"] if record["modality"] == "ppg" else None
                )
                row["ppg_straining_onsets"] = (
                    row["straining_onsets"] if record["modality"] == "ppg" else None
                )
                if record["modality"] == "ppg":
                    row["motion_coverage"] = 0
                    row["activity_index"] = None
                    row["straining_onsets"] = None
                    row["straining_seconds"] = None
                row["behavior_versions"] = versions
                row["decision_epoch_ms"] = row["end_epoch_ms"] + 20000
                available = f.get("update_time_ms")
                if available is not None and available > row["decision_epoch_ms"]:
                    row["temperature_c"] = None
                    row["temperature_samples"] = 0
                    row["temperature_availability"] = "packet_not_yet_received"
                row["heart_rate_bpm"] = None
                row["spo2_percent"] = None
                row["prediction_probability"] = None
                row["evidence_interpretation"] = "行为模型输出与传感器温度；缺失项保留为空"
            rows.extend(record_rows)
        except (OSError, ValueError, KeyError) as exc:
            issues.append(dict(path=record["raw"], reason=str(exc)))
        progress(i + 1, len(index["records"]), "汇总行为、活动量与温度")
    temperatures, temp_issues = ((input_temperatures, []) if input_temperatures is not None
                                else external_temperatures(root) if include_external_temperature else ([], []))
    issues.extend(temp_issues)
    temperature_groups = defaultdict(list)
    seen_temperatures = set()
    for sample in temperatures:
        key = (sample["cow"], sample["device"], sample["mark"])
        observation = (key, sample["time"], sample["value"])
        if observation not in seen_temperatures:
            seen_temperatures.add(observation)
            temperature_groups[key].append(sample)
    temperature_index = {}
    for key, samples in temperature_groups.items():
        samples.sort(key=lambda sample: sample["time"])
        temperature_index[key] = ([sample["time"] for sample in samples], samples)
    for row in rows:
        times, samples = temperature_index.get((row["cow_id"],row["device_id"],row["field_mark"]), ([], []))
        left = bisect_left(times, row["start_epoch_ms"])
        right = bisect_left(times, row["end_epoch_ms"])
        matches = [sample for sample in samples[left:right]
                   if sample["available_at_ms"] <= row["decision_epoch_ms"]]
        if matches:
            row["temperature_c"] = float(np.median([t["value"] for t in matches]))
            row["temperature_samples"] = len(matches)
            row["temperature_sources"] = [t["source"] for t in matches]
            row["temperature_availability"] = "independent_samples_available"
        # occupancy keeps unknown segments explicit; only known coverage is used.
        row["lying_fraction_known"] = row.get("lying_fraction_known", row.get("lying_fraction"))
    optical = [r for r in rows if r["modality"] == "ppg"]
    motion = [r for r in rows if r["modality"] == "motion"]
    consumed = set()
    for row in motion:
        matches = [
            r
            for r in optical
            if r["cow_id"] == row["cow_id"]
            and r["device_id"] == row["device_id"]
            and r["field_mark"] == row["field_mark"]
            and r["start_epoch_ms"] >= row["start_epoch_ms"]
            and r["end_epoch_ms"] <= row["end_epoch_ms"]
        ]
        if matches:
            row["ppg_coverage"] = min(
                1,
                sum((r["end_epoch_ms"] - r["start_epoch_ms"]) * r["ppg_coverage"] for r in matches)
                / max(1, row["end_epoch_ms"] - row["start_epoch_ms"]),
            )
            row["ppg_straining_onsets"] = sum(r["ppg_straining_onsets"] for r in matches)
            row["ppg_sources"] = [r["source"] for r in matches]
            consumed.update(id(r) for r in matches)
    rows = motion + [r for r in optical if id(r) not in consumed]
    add_baselines(rows)
    result = dict(
        schema="cowmata-fusion-3.9",
        rows=rows,
        issues=issues,
        index_fingerprint=index["fingerprint"],
        behavior_models=[dict(version=s["version"], sha256=s["hash"], codes=sorted(m["code"] for m in s["models"])) for s in selected],
        delayed_seconds=20,
        temperature_basis="sensor_celsius",
        temperature_contract=dict(CONTRACT),
        future_extensions=["heart_rate_bpm", "spo2_percent"],
    )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / "综合证据.json", result)
    write_table(output / "综合证据.csv", rows)
    write_table(output / "证据数据问题.csv", issues, ["path", "reason"])
    return result


def attach_outcomes(rows, ledger_path, horizon_hours=24):
    births = {}
    with Path(ledger_path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("已删除") == "1":
                continue
            day, clock = row.get("生产日期", ""), row.get("牛场登记生产时间", "")
            if not clock or ":" not in clock:
                continue
            try:
                clock = clock.strip()
                text = day.strip() + "T" + clock if re.match(r"^\d{1,2}:", clock) else clock
                date = parse_time(text)
                cow, _ = cow_identity(row["牛号"])
                if date is not None:
                    births.setdefault(cow, []).append(date.timestamp() * 1000)
            except (ValueError, KeyError):
                continue
    labeled = []
    for row in rows:
        now = row.get("decision_epoch_ms", row.get("end_epoch_ms"))
        if now is None:
            continue
        future = [b for b in births.get(row["cow_id"], []) if 0 < b - now <= 7 * 86400000]
        if not future:
            continue  # no outcome does not establish a negative
        birth = min(future)
        labeled.append(
            {
                **row,
                "outcome": int(birth - now <= horizon_hours * 3600000),
                "calving_epoch_ms": birth,
            }
        )
    return labeled


def matrix(rows):
    def value(row, key):
        try:
            x = float(row.get(key))
            return x if np.isfinite(x) else np.nan
        except (TypeError, ValueError):
            return np.nan

    return np.asarray([[value(r, k) for k in FEATURES] for r in rows], dtype=float)


def fit(algorithm, x, y, seed=390):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        median = np.nan_to_num(np.nanmedian(x, axis=0))
    z = np.column_stack([np.where(np.isfinite(x), x, median), ~np.isfinite(x)])
    if algorithm == "xgboost":
        from xgboost import XGBClassifier

        model = XGBClassifier(
            n_estimators=120,
            max_depth=3,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            tree_method="hist",
            n_jobs=1,
            random_state=seed,
            eval_metric="logloss",
        )
    elif algorithm == "random_forest":
        from sklearn.ensemble import RandomForestClassifier

        model = RandomForestClassifier(
            n_estimators=96, max_depth=6, min_samples_leaf=4, n_jobs=1, random_state=seed
        )
    elif algorithm == "decision_tree":
        from sklearn.tree import DecisionTreeClassifier

        model = DecisionTreeClassifier(max_depth=5, min_samples_leaf=4, random_state=seed)
    else:
        raise ValueError("未知决策算法")
    model.fit(z, y)
    return model, median


def transformed(x, median):
    return np.column_stack([np.where(np.isfinite(x), x, median), ~np.isfinite(x)])


def train_decision(
    evidence,
    ledger_path,
    output,
    algorithm="xgboost",
    horizon_hours=24,
    *,
    progress=lambda *_: None,
):
    from sklearn.metrics import (
        average_precision_score,
        brier_score_loss,
        confusion_matrix,
        precision_recall_curve,
        roc_auc_score,
        roc_curve,
    )
    from sklearn.model_selection import GroupKFold

    validate_contract(evidence.get("temperature_contract"))
    if horizon_hours not in (6, 12, 24, 48):
        raise ValueError("预测提前量无效")
    labeled = attach_outcomes(evidence["rows"], ledger_path, horizon_hours)
    if not labeled:
        raise ValueError("没有可用的产犊时间与事前证据；未登记产犊的牛不能自动当作负例")
    x = matrix(labeled)
    y = np.asarray([r["outcome"] for r in labeled])
    groups = np.asarray([r["cow_id"] for r in labeled])
    if len(set(groups)) < 3 or set(y) != {0, 1}:
        raise ValueError("需要至少三头独立牛，且包含提前量内的正例及提前量外的已知产犊负例")
    probabilities = np.full(len(y), np.nan)
    folds = []
    for number, (train, test) in enumerate(
        GroupKFold(n_splits=min(5, len(set(groups)))).split(x, y, groups), 1
    ):
        if set(y[train]) != {0, 1}:
            raise ValueError("某个按牛划分的训练折缺少正例或负例；请补充数据")
        model, median = fit(algorithm, x[train], y[train], 390 + number)
        probabilities[test] = model.predict_proba(transformed(x[test], median))[:, 1]
        folds.append(
            dict(
                fold=number,
                train_cows=sorted(set(groups[train])),
                test_cows=sorted(set(groups[test])),
            )
        )
        progress(number, min(5, len(set(groups))), "按牛分组验证决策模型")
    tn, fp, fn, tp = confusion_matrix(y, probabilities >= 0.5, labels=[0, 1]).ravel()
    precision, recall, _ = precision_recall_curve(y, probabilities)
    fpr, tpr, _ = roc_curve(y, probabilities)
    bins = []
    for lo in np.arange(0, 1, 0.1):
        mask = (probabilities >= lo) & (
            probabilities <= min(1, lo + 0.1) if lo > 0.89 else probabilities < lo + 0.1
        )
        if mask.any():
            bins.append(
                dict(
                    predicted=float(np.mean(probabilities[mask])),
                    observed=float(np.mean(y[mask])),
                    count=int(mask.sum()),
                )
            )
    metrics = dict(
        roc_auc=float(roc_auc_score(y, probabilities)),
        pr_auc=float(average_precision_score(y, probabilities)),
        brier=float(brier_score_loss(y, probabilities)),
        sensitivity=float(tp / max(1, tp + fn)),
        specificity=float(tn / max(1, tn + fp)),
        precision=float(tp / max(1, tp + fp)),
        confusion_matrix=[[int(tn), int(fp)], [int(fn), int(tp)]],
        threshold=0.5,
        validation="按牛分组留出；未完成跨牧场前瞻验证",
        probability_calibrated=False,
    )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    model, median = fit(algorithm, x, y)
    names = list(FEATURES) + [f + "_missing" for f in FEATURES]
    if algorithm == "xgboost":
        file = output / "xgboost.json"
        model.save_model(file)
    else:
        file = output / "forest.json"
        forest = (
            model
            if algorithm == "random_forest"
            else SimpleNamespace(classes_=model.classes_, estimators_=[model])
        )
        atomic_json(file, export_forest(forest, names, np.r_[median, np.zeros(len(FEATURES))]))
    manifest = dict(
        schema=SCHEMA,
        version=output.name,
        algorithm=algorithm,
        feature_names=list(FEATURES),
        temperature_contract=dict(CONTRACT),
        median=median.tolist(),
        model_file=file.name,
        sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
        horizon_hours=horizon_hours,
        threshold=0.5,
        probability_calibrated=False,
        metrics=metrics,
        training_rows=len(y),
        training_cows=len(set(groups)),
        feature_coverage=np.mean(np.isfinite(x), axis=0).tolist(),
        behavior_models=evidence.get("behavior_models", []),
        feature_importance=dict(zip(names, [float(v) for v in model.feature_importances_])),
        label_policy="精确产犊登记时间之前7天；无结局者排除",
        complete=True,
    )
    atomic_json(output / "decision.json", manifest)
    report = dict(
        **manifest,
        folds=folds,
        calibration=bins,
        pr_curve=list(zip(recall.tolist(), precision.tolist())),
        roc_curve=list(zip(fpr.tolist(), tpr.tolist())),
    )
    atomic_json(output / "决策训练评价.json", report)
    write_table(
        output / "决策逐记录留出评价.csv",
        [
            dict(
                cow_id=r["cow_id"],
                decision_epoch_ms=r["decision_epoch_ms"],
                outcome=int(label),
                heldout_score=float(score),
                source=r["source"],
            )
            for r, label, score in zip(labeled, y, probabilities)
        ],
    )
    atomic_json(
        output / "training-inputs.json",
        dict(
            evidence_fingerprint=evidence.get("index_fingerprint"),
            evidence_rows=labeled,
            calving_csv_sha256=hashlib.sha256(Path(ledger_path).read_bytes()).hexdigest(),
        ),
    )
    return report


def read_decision(folder):
    folder = Path(folder)
    if folder.is_file():
        folder = folder.parent
    doc = json.loads((folder / "decision.json").read_text(encoding="utf-8"))
    if (
        doc.get("schema") != SCHEMA
        or not doc.get("complete")
        or doc.get("feature_names") != list(FEATURES)
    ):
        raise ValueError("决策模型不完整或输入格式不兼容")
    validate_contract(doc.get("temperature_contract"))
    file = child(folder, doc["model_file"])
    if hashlib.sha256(file.read_bytes()).hexdigest() != doc["sha256"]:
        raise ValueError("决策模型校验失败")
    median = np.asarray(doc["median"])
    if median.shape != (len(FEATURES),) or not np.isfinite(median).all():
        raise ValueError("模型缺失值参数无效")
    return folder, doc


def predict_decision(evidence, model_path, output):
    folder, doc = read_decision(model_path)
    validate_contract(evidence.get("temperature_contract"))
    # Behavioral feature semantics are model-version-dependent.
    if doc.get("behavior_models") != evidence.get("behavior_models", []):
        raise ValueError("行为模型版本与决策训练时不同；请恢复对应行为模型或重新训练决策模型")
    rows = [dict(r) for r in evidence["rows"]]
    if not rows:
        raise ValueError("尚无证据窗口")
    x = matrix(rows)
    z = transformed(x, np.asarray(doc["median"]))
    if doc["algorithm"] == "xgboost":
        from xgboost import XGBClassifier

        model = XGBClassifier()
        model.load_model(folder / doc["model_file"])
        scores = model.predict_proba(z)[:, 1]
    else:
        scores = predict_forest(
            json.loads((folder / doc["model_file"]).read_text(encoding="utf-8")), z
        )
    for row, values, score in zip(rows, x, scores):
        used = [name for name, coverage in zip(FEATURES, doc["feature_coverage"]) if coverage > 0]
        absent = [
            name for name, value in zip(FEATURES, values) if name in used and not np.isfinite(value)
        ]
        row["missing_features"] = absent
        row["risk_score"] = (
            None
            if not np.isfinite(values).any()
            or max(row.get("motion_coverage") or 0, row.get("ppg_coverage") or 0) < 0.5
            else float(score)
        )
        row["warning_level"] = (
            "数据不足"
            if row["risk_score"] is None
            else "关注并复核"
            if score >= doc["threshold"]
            else "继续观察"
        )
        row["horizon_hours"] = doc["horizon_hours"]
        row["decision_model"] = doc["version"]
        row["probability_calibrated"] = False
    output = Path(output)
    result = dict(
        schema="cowmata-decision-result-3.9",
        rows=rows,
        model=doc,
        issues=evidence.get("issues", []),
    )
    atomic_json(output / "综合决策结果.json", result)
    write_table(output / "综合决策结果.csv", rows)
    return result
