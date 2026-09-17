"""Per-record calving evidence; missing posture and uncalibrated scores stay explicit."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from cowmata_tailring.workspace.storage import atomic_json

from .analysis import load_features, write_table
from .models import predict_forest, score_events
from .posture import occupancy
from .registry import read_suite


def merged(intervals):
    result = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if result and a <= result[-1][1]:
            result[-1][1] = max(b, result[-1][1])
        else:
            result.append([a, b])
    return result


def infer_features(suite, feature, codes=None):
    events = []
    for entry in suite["models"]:
        if codes is not None and entry["code"] not in codes:
            continue
        model = json.loads((Path(suite["root"]) / entry["file"]).read_text(encoding="utf-8"))
        if model["features"] != feature["names"]:
            raise ValueError("Algorithm feature order does not match the trained model")
        scores = predict_forest(model, feature["X"])
        events.extend(score_events(scores, feature["valid_context"], code=entry["code"],
                                   threshold=entry["threshold"], duration_ms=feature["duration_ms"]))
    return events


def evidence_rows(record, feature, events, *, window_ms=600000, causal=False):
    if window_ms <= 0:
        raise ValueError("Evidence window must be positive")
    result = []
    duration = feature["duration_ms"]
    for start in range(0, int(np.ceil(duration)), window_ms):
        end = min(start + window_ms, duration)
        times = np.asarray(feature["seconds"]) * 1000
        valid = (times >= start) & (times < end) & feature["valid"]
        activity = np.asarray(feature["dynamic"])[valid]
        activity = activity[np.isfinite(activity)]
        temp_times = np.asarray(feature.get("temperature_ms", []))
        temp = np.asarray(feature.get("temperature_c", []))
        temp = temp[(temp_times >= start) & (temp_times < end) & np.isfinite(temp)]
        visible = [e for e in events if e.get('available_ms',e.get('end_ms',0)+20000) <= end+20000] if causal else events
        posture = occupancy(visible, start, end, observed_intervals=feature["segments"])
        bouts = [e for e in visible if e["code"] == "STRAINING_BOUT"
                 and e["start_ms"] < end and e["end_ms"] > start]
        bout_seconds = sum((b-a)/1000 for a, b in merged(
            [(max(start, e["start_ms"]), min(end, e["end_ms"])) for e in bouts]))
        epoch = feature.get("epoch_offset_ms")
        result.append(dict(cow_id=record["cow_id"], asset_id=record["asset_id"],
            source=record["raw"], start_ms=start, end_ms=end,
            start_epoch_ms=epoch+start if epoch is not None else None,
            end_epoch_ms=epoch+end if epoch is not None else None,
            activity_index=float(np.mean(activity)) if len(activity) else None,
            motion_coverage=min(1., float(valid.sum()) / ((end-start)/1000)),
            temperature_c=float(np.median(temp)) if len(temp) else None,
            temperature_samples=len(temp),
            straining_onsets=sum(start <= e["start_ms"] < end for e in bouts),
            straining_seconds=bout_seconds, **posture,
            prediction_probability=None, warning_level=None,
            evidence_interpretation="四项证据；事件由模型推断，温度为传感器温度，未输出产犊结论"))
    return result


def add_baselines(rows):
    history = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r["cow_id"], r["start_epoch_ms"] or 0, r["asset_id"])):
        now = row["start_epoch_ms"]
        identity = row["cow_id"] or ("unidentified", row["asset_id"])
        prior = [r for r in history[identity] if now is not None
                 and r["end_epoch_ms"] is not None
                 and now-86400000 <= r["end_epoch_ms"] <= now
                 and r.get("decision_epoch_ms", r["end_epoch_ms"])
                 <= row.get("decision_epoch_ms", now)]
        for key in ("activity_index", "temperature_c"):
            values = [r[key] for r in prior if r[key] is not None]
            baseline = float(np.median(values)) if len(values) >= 6 else None
            row[key+"_baseline"] = baseline
            row[key+"_change"] = row[key]-baseline if row[key] is not None and baseline is not None else None
        row["baseline_windows"] = len(prior)
        history[identity].append(row)
    return rows


def build_evidence(index, cache, suite_path, output, *, progress=lambda *_: None, cancelled=lambda: False):
    suite = read_suite(suite_path)
    rows, issues, seen = [], list(index.get("issues", [])), set()
    records = index["records"]
    for i, record in enumerate(records):
        if cancelled():
            raise InterruptedError("Evidence extraction cancelled")
        if record["asset_id"] in seen or not record.get("identity_eligible", True):
            continue
        seen.add(record["asset_id"])
        try:
            feature = load_features(record, cache)
            events = infer_features(suite, feature)
            rows.extend(evidence_rows(record, feature, events))
        except (OSError, ValueError, KeyError) as exc:
            issues.append(dict(path=record["raw"], reason=str(exc)))
        progress(i+1, len(records), "按牛提取四项证据")
    add_baselines(rows)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    write_table(output / "四项证据与趋势.csv", rows)
    write_table(output / "证据数据问题.csv", issues, ["path", "reason"])
    summary = dict(schema="calving-evidence-1", model_version=suite["version"],
        dataset_fingerprint=index.get("fingerprint"), rows=rows, issues=issues,
        evaluation=[
            dict(metric="证据窗口数", value=len(rows), scope="各牛、各原始记录独立计算"),
            dict(metric="平均九轴覆盖率", value=float(np.mean([r["motion_coverage"] for r in rows])) if rows else None,
                 scope="有效秒占窗口时长"),
            dict(metric="有温度窗口数", value=sum(r["temperature_c"] is not None for r in rows), scope="传感器温度"),
            dict(metric="平均姿态可知覆盖率", value=float(np.mean([r["known_posture_coverage"] for r in rows])) if rows else None,
                 scope="起立/卧倒推断；缺口、冲突与起始未知均保留"),
            dict(metric="预警准确率 / 灵敏度 / 提前量", value=None,
                 scope="当前仅输出证据；尚无预警模型、阈值及连续审核真值，不能计算")])
    atomic_json(output / "产犊证据报告.json", summary)
    write_table(output / "产犊监测评价.csv", summary["evaluation"])
    return summary
