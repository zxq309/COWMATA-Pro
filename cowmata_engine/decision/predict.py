"""Inference: multi-horizon calving risk, time-to-calving, warning level and explanations."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from cowmata_engine.features import FEATURE_MODULES

from .dataset import build_decision_rows, extract_feature_tables, load_feature_tables
from .models import predict_model, predict_time_to_event
from .runtime import HOUR_MS, MANIFEST_SCHEMA, alert_episodes

RESULT_SCHEMA = "cowmata-decision-result-4.3.4"
FEATURE_TITLES = {key: title for key, (_, title) in FEATURE_MODULES.items()}
OUTPUT_FIELDS = {
    "cow_id": "牛耳标",
    "decision_epoch_ms": "预测时刻（使用此刻之前已收到的数据）",
    "risk": "各提前量内产犊的校准概率（6/12/24/48 h，单调不减）",
    "risk_primary": "主提前量（模型训练提前量）产犊概率",
    "threshold": "主提前量预警阈值（按牛留出 Youden 指数最优）",
    "warning_level": "预警等级：正常 / 关注 / 高度关注 / 临产 / 数据不足",
    "advice": "处置建议",
    "hours_to_calving_p50": "预计距产犊小时数（中位数）",
    "hours_to_calving_p10": "预计距产犊小时数 P10（80% 区间下限）",
    "hours_to_calving_p90": "预计距产犊小时数 P90（80% 区间上限）",
    "drivers": "主要驱动特征（特征、方向、相对本牛基线 z 分数、贡献）",
    "feature_coverage": "各特征近 6 h 数据覆盖率",
    "missing_features": "模型使用但当前缺失的特征",
    "history_hours": "本牛可用参考历史（小时），<24 h 基线不可靠",
    "model_version": "决策模型版本",
}


def read_model(folder):
    folder = Path(folder)
    if folder.is_file():
        folder = folder.parent
    manifest = json.loads((folder / "decision.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA or not manifest.get("complete"):
        raise ValueError("决策模型不完整或不是 4.3.4 决策模型")
    docs = {}
    for horizon, item in manifest["files"].items():
        file = folder / Path(item["file"]).name
        if hashlib.sha256(file.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"决策模型文件校验失败：{file.name}")
        docs[int(horizon)] = json.loads(file.read_text(encoding="utf-8"))
        if docs[int(horizon)]["columns"] != manifest["columns"]:
            raise ValueError("决策模型输入列与清单不一致")
    tte = None
    if manifest.get("time_to_calving"):
        file = folder / Path(manifest["time_to_calving"]["file"]).name
        if hashlib.sha256(file.read_bytes()).hexdigest() != manifest["time_to_calving"]["sha256"]:
            raise ValueError("剩余时间模型校验失败")
        tte = json.loads(file.read_text(encoding="utf-8"))
    return folder, manifest, docs, tte


def _feature_of(column):
    base = column.split("@")[0]
    if base.startswith("coverage."):
        return base.split(".", 1)[1]
    for key in FEATURE_MODULES:
        if base == key or base.startswith(key + "."):
            return key
    return None


def _shap(doc, x):
    """Exact TreeSHAP contributions for XGBoost (also used via the stacking XGBoost base)."""
    import xgboost as xgb

    if doc["algorithm"] == "stacking":
        doc = next((b for b in doc["bases"] if b["algorithm"] == "xgboost"), None)
        if doc is None:
            return None
    if doc["algorithm"] != "xgboost":
        return None
    booster = xgb.Booster(params=dict(nthread=2))
    booster.load_model(bytearray(base64.b64decode(doc["booster"])))
    return booster.predict(xgb.DMatrix(x, missing=np.nan), pred_contribs=True)[:, :-1]


def explain(manifest, doc, x, column_keys):
    """Top drivers per row, aggregated by feature key."""
    columns = manifest["columns"]
    contrib = _shap(doc, x)
    z_index = defaultdict(list)
    for i, c in enumerate(columns):
        if c.endswith("@z72") and column_keys.get(c) not in (None, "context"):
            z_index[column_keys[c]].append(i)
    drivers = []
    for r in range(len(x)):
        scores = defaultdict(float)
        if contrib is not None:
            for i, c in enumerate(columns):
                key = column_keys.get(c)
                if key and key != "context" and np.isfinite(contrib[r, i]):
                    scores[key] += float(contrib[r, i])
        else:
            for key, idx in z_index.items():
                vals = x[r, idx]
                vals = vals[np.isfinite(vals)]
                if len(vals):
                    scores[key] = float(np.max(np.abs(vals)))
        items = []
        for key, value in sorted(scores.items(), key=lambda kv: -abs(kv[1]))[:3]:
            zs = x[r, z_index.get(key, [])] if z_index.get(key) else np.array([])
            zs = zs[np.isfinite(zs)]
            z = float(zs[np.argmax(np.abs(zs))]) if len(zs) else None
            items.append(dict(feature=key, title=FEATURE_TITLES.get(key, key), contribution=round(value, 4),
                              z=None if z is None else round(z, 2),
                              direction=None if z is None else ("高于本牛基线" if z > 0 else "低于本牛基线"),
                              pushes=("升高风险" if value > 0 else "降低风险") if contrib is not None else "偏离基线"))
        drivers.append(items)
    return drivers


def _level(manifest, risk, row, low_data):
    if low_data:
        return "数据不足", "当前窗口缺少有效信号，无法判断；检查设备佩戴与上传"
    horizons = sorted(risk)
    primary = manifest["horizon_hours"]
    thr = manifest["thresholds"]
    policy = manifest.get("threshold_policy") or {}
    short = horizons[0]
    base = float(thr[str(primary)])
    critical = min(0.999, max(float(thr.get(str(short), base)),
                              base * float(policy.get("critical_factor", 1.25))))
    attention = max(0.001, base * float(policy.get("attention_factor", 0.60)))
    levels = {item["level"]: item["advice"] for item in manifest["levels"]}
    if risk[short] >= critical:
        return "临产", levels["临产"]
    if risk.get(primary, 0) >= base:
        return "高度关注", levels["高度关注"]
    if risk.get(primary, 0) >= attention or (
            horizons[-1] in risk and risk[horizons[-1]] >= float(thr.get(str(horizons[-1]), base))):
        return "关注", levels["关注"]
    return "正常", levels["正常"]


def predict_rows(rows, model_folder):
    """Score decision rows (as produced by :func:`build_decision_rows`)."""
    folder, manifest, docs, tte = read_model(model_folder)
    columns = manifest["columns"]
    if not rows:
        return dict(schema=RESULT_SCHEMA, rows=[], model=manifest)
    x = np.asarray([[np.nan if r.get(c) is None else r[c] for c in columns] for r in rows], dtype=float)
    column_keys = manifest.get("column_keys") or {c: _feature_of(c) for c in columns}
    used = sorted({k for c, k in column_keys.items() if k and k != "context" and not c.startswith("coverage.")})
    probs = {}
    for horizon, doc in docs.items():
        raw = predict_model(doc, x)
        cal = manifest["calibrators"].get(str(horizon))
        probs[horizon] = np.interp(raw, cal["x"], cal["y"]) if cal else raw
    order = sorted(probs)
    stacked = np.maximum.accumulate(np.column_stack([probs[h] for h in order]), axis=1)
    hours = predict_time_to_event(tte, x) if tte else None
    drivers = explain(manifest, docs[manifest["horizon_hours"]], x, column_keys)
    out = []
    for i, row in enumerate(rows):
        coverage = {k: round(float(row.get(f"coverage.{k}@6h") or 0.0), 3) for k in FEATURE_MODULES}
        missing = [k for k in used if coverage.get(k, 0) <= 0]
        risk = {h: float(stacked[i, j]) for j, h in enumerate(order)}
        low = sum(v > 0 for v in coverage.values()) == 0
        level, advice = _level(manifest, risk, row, low)
        item = dict(
            cow_id=row["cow_id"], devices=row.get("devices"), decision_epoch_ms=int(row["decision_epoch_ms"]),
            risk={f"{h}h": round(v, 4) for h, v in risk.items()},
            risk_primary=round(risk[manifest["horizon_hours"]], 4),
            threshold=float(manifest["thresholds"][str(manifest["horizon_hours"])]),
            warning_level=level, advice=advice, drivers=drivers[i], feature_coverage=coverage,
            missing_features=missing, history_hours=row.get("history_hours"),
            history_status="参考历史不足 24 小时，基线类特征不可用" if (row.get("history_hours") or 0) < 24 else "参考历史充足",
            model_version=manifest["version"],
        )
        if hours is not None:
            item.update(hours_to_calving_p10=round(float(hours[i, 0]), 1), hours_to_calving_p50=round(float(hours[i, 1]), 1),
                        hours_to_calving_p90=round(float(hours[i, 2]), 1),
                        predicted_calving_epoch_ms=int(row["decision_epoch_ms"] + hours[i, 1] * HOUR_MS))
        for key in ("hours_to_calving", "calving_epoch_ms"):
            if row.get(key) is not None:
                item["truth_" + key] = row[key]  # evaluation only; never an input
        out.append(item)
    episodes = []
    by_cow = defaultdict(list)
    for item in out:
        by_cow[item["cow_id"]].append(item)
    primary = manifest["horizon_hours"]
    for cow, items in by_cow.items():
        items.sort(key=lambda r: r["decision_epoch_ms"])
        found = alert_episodes([r["decision_epoch_ms"] for r in items], np.asarray([r["risk_primary"] for r in items]),
                               float(manifest["thresholds"][str(primary)]), persistence=manifest.get("persistence_hours", 2))
        for e in found:
            inside = [r for r in items if e["first"] <= r["decision_epoch_ms"] <= e["end"]]
            eta = [r.get("predicted_calving_epoch_ms") for r in inside if r.get("predicted_calving_epoch_ms")]
            episodes.append(dict(cow_id=cow, first_alert_ms=e["first"], confirmed_alert_ms=e["start"], last_alert_ms=e["end"],
                                 peak_risk=round(float(e["peak"]), 4), windows=e["windows"],
                                 predicted_calving_epoch_ms=int(np.median(eta)) if eta else None,
                                 attention_until_ms=e["end"] + primary * HOUR_MS))
    return dict(schema=RESULT_SCHEMA, rows=out, alert_episodes=episodes, output_fields=OUTPUT_FIELDS,
                model={k: manifest[k] for k in ("version", "algorithm", "algorithm_title", "horizon_hours", "horizons",
                                                 "thresholds", "metrics", "events", "validation", "feature_versions",
                                                 "created_at")},
                used_features=used)


def coverage_summary(rows):
    by_cow = defaultdict(list)
    for r in rows:
        by_cow[r["cow_id"]].append(r)
    result = []
    for cow, items in by_cow.items():
        times = sorted(r["decision_epoch_ms"] for r in items)
        gaps = [(b - a) / HOUR_MS for a, b in zip(times, times[1:])]
        result.append(dict(cow_id=cow, first_ms=times[0], last_ms=times[-1], span_hours=round((times[-1] - times[0]) / HOUR_MS, 1),
                           decision_points=len(times), largest_gap_hours=round(max(gaps, default=0), 1),
                           features={k: round(float(np.mean([r.get(f"coverage.{k}@6h") or 0 for r in items])), 3)
                                     for k in FEATURE_MODULES}))
    return result


def predict_folder(folder, model, output, *, workers=None, features_root=None, progress=lambda *_: None,
                   cancelled=lambda: False):
    """Rolling prediction over a folder of raw JSON (or precomputed feature windows)."""
    started = time.monotonic()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    issues = []
    if features_root:
        tables, manifests = load_feature_tables(features_root)
    else:
        _, issues, _ = extract_feature_tables(folder, workers=workers, output=output / "Features",
                                              progress=progress, cancelled=cancelled)
        tables, manifests = load_feature_tables(output / "Features")
    if not tables or not any(tables.values()):
        raise ValueError("所选文件夹没有可用的特征窗口；请核对数据类型与设备目录命名")
    rows = build_decision_rows(tables, manifests, progress=progress, cancelled=cancelled)
    if not rows:
        raise ValueError("所选文件夹没有可预测的时段")
    result = predict_rows(rows, model)
    result.update(coverage=coverage_summary(rows), issues=issues[:2000], issue_count=len(issues),
                  input=dict(path=str(folder), features={k: m.get("version") for k, m in manifests.items()},
                             decision_points=len(rows), elapsed_seconds=round(time.monotonic() - started, 2),
                             interpretation="逐小时回放：每个时刻只使用该时刻之前已可用的数据；预警不等于确认产犊。"))
    (output / "综合决策结果.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    write_result_csv(output / "综合决策结果.csv", result["rows"])
    return result


def write_result_csv(path, rows):
    import csv

    header = ["cow_id", "decision_epoch_ms", "risk_6h", "risk_12h", "risk_24h", "risk_48h", "risk_primary", "threshold",
              "warning_level", "hours_to_calving_p10", "hours_to_calving_p50", "hours_to_calving_p90",
              "drivers", "missing_features", "history_hours", "model_version"]
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        for r in rows:
            writer.writerow([r["cow_id"], r["decision_epoch_ms"], *[r["risk"].get(f"{h}h", "") for h in (6, 12, 24, 48)],
                             r["risk_primary"], r["threshold"], r["warning_level"], r.get("hours_to_calving_p10", ""),
                             r.get("hours_to_calving_p50", ""), r.get("hours_to_calving_p90", ""),
                             "；".join(f"{d['title']}({d['direction'] or d['pushes']})" for d in r["drivers"]),
                             ",".join(r["missing_features"]), r.get("history_hours"), r["model_version"]])
