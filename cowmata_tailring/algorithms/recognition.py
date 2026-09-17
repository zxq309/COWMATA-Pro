"""Batch behavior recognition shared with candidate and decision workflows."""

from pathlib import Path

from cowmata_tailring.workspace.storage import atomic_json

from .analysis import load_features, write_table
from .evidence import infer_features
from .inputs import scan_inputs
from .registry import read_suite


def recognize(root, suite_path, code, output, cache, *, progress=lambda *_: None):
    suite = read_suite(suite_path)
    if code not in {m["code"] for m in suite["models"]}:
        raise ValueError("导入模型不包含所选行为，请重新选择模型")
    index = scan_inputs(root, progress=progress)
    rows, issues = [], list(index["issues"])
    records = [r for r in index["records"] if r["modality"] == suite.get("modality", "motion")]
    if not records:
        raise ValueError("未找到与导入模型匹配的原始数据类型")
    for i, record in enumerate(records):
        try:
            feature = load_features(record, cache)
            events = infer_features(suite, feature, [code])
            for e in events:
                rows.append(
                    dict(
                        cow_id=record["cow_id"],
                        device_id=record["device_id"],
                        field_mark=record["field_mark"],
                        modality=record["modality"],
                        asset_id=record["asset_id"],
                        source=record["raw"],
                        start_epoch_ms=feature["epoch_offset_ms"] + e["start_ms"],
                        end_epoch_ms=feature["epoch_offset_ms"] + e["end_ms"],
                        model_version=suite["version"],
                        **e,
                    )
                )
        except (OSError, ValueError, KeyError) as exc:
            issues.append(dict(path=record["raw"], reason=str(exc)))
        progress(i + 1, len(records), "识别所选行为")
    output = Path(output)
    result = dict(
        schema="cowmata-behavior-recognition-3.9",
        code=code,
        model=suite["version"],
        modality=suite.get("modality", "motion"),
        records=len(records),
        rows=rows,
        issues=issues,
        score_is_probability=False,
        requires_review=True,
    )
    write_table(output / "行为识别结果.csv", rows)
    write_table(output / "输入与识别问题.csv", issues, ["path", "reason"])
    atomic_json(output / "识别报告.json", result)
    atomic_json(output / "原始记录索引.json", index)
    return result
