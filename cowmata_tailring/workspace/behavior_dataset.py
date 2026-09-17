"""Per-behavior, per-cow samples derived from complete, unaltered mother data."""

from __future__ import annotations

import base64
import json
from collections import Counter
from pathlib import Path

import numpy as np

from cowmata_tailring.annotation.data import parse_motion_object

from .catalog import digest_file
from .legacy_labels import normalized_cow
from .mother_dataset import _all_documents, _write_table, export_dataset
from .resource_layout import dataset_filename
from .storage import atomic_json
from .work import SessionWork

BEHAVIORS = {
    "STANDING": "站立",
    "LYING": "躺卧",
    "WALKING": "行走",
    "FEEDING": "采食",
    "STANDING_UP": "起立过程",
    "LYING_DOWN": "卧倒过程",
    "TAIL_RAISED": "抬尾",
    "TAIL_WAGGING": "甩尾",
    "URINATION": "排尿",
    "DEFECATION": "排便",
    "MOUNTING": "爬跨",
    "STRAINING_BOUT": "努责",
}
CATEGORIES = {
    "estrus",
    "calving",
    "pregnancy_early",
    "pregnancy_mid",
    "pregnancy_late",
    "disease",
    "healthy",
}


def behavior_allowed(code, category):
    if code not in BEHAVIORS or category not in CATEGORIES:
        return False
    return (
        category == "estrus"
        if code == "MOUNTING"
        else category == "calving"
        if code == "STRAINING_BOUT"
        else True
    )


def audit_annotations(sources, *, progress=lambda *_: None, cancelled=lambda: False):
    counts = Counter()
    issues = []
    cows = set()
    for i, (path, doc) in enumerate(_all_documents(sources)):
        if cancelled():
            raise InterruptedError("标签检查已暂停")
        work = SessionWork.from_dict(doc["work"])
        counts["documents"] += 1
        cow = normalized_cow(work.project.cow_id)
        if cow:
            cows.add(cow)
        for event in work.project.events:
            counts["events"] += 1
            code = work.project.labels[event.li].code
            reason = ""
            if (
                not cow
                or work.project.extras.get("device_identity", {}).get("status") == "conflict"
            ):
                reason = "牛耳标待核"
            elif event.extras.get("confirmation") not in {"confirmed", "legacy_imported"}:
                reason = "标签待复核"
            elif code in BEHAVIORS and not behavior_allowed(
                code, work.project.extras.get("dataset_category", "")
            ):
                reason = "行为与归类不对应"
            if reason:
                issues.append(
                    dict(source=str(path), event_id=event.id, cow_id=cow, code=code, reason=reason)
                )
        progress(i + 1, 0, str(path))
    return dict(
        documents=counts["documents"],
        events=counts["events"],
        cows=len(cows),
        issues=issues,
        review_events=len(issues),
        source_modified=False,
    )


def refresh_manifest(root, **values):
    path = Path(root) / "dataset-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(values)
    manifest["files"] = [
        dict(path=p.relative_to(root).as_posix(), sha256=digest_file(p), size=p.stat().st_size)
        for p in sorted(Path(root).rglob("*"))
        if p.is_file() and p not in {path, path.with_suffix(path.suffix + ".bak")}
    ]
    atomic_json(path, manifest,backup=False)


def build_behavior_dataset(
    sources,
    target,
    *,
    behaviors=None,
    native_features=False,
    app_root=None,
    split_map=None,
    progress=lambda *_: None,
    cancelled=lambda: False,
):
    selected = list(BEHAVIORS) if behaviors is None else list(dict.fromkeys(behaviors))
    if not selected or any(code not in BEHAVIORS for code in selected):
        raise ValueError("请选择需要构建的行为")
    result = export_dataset(
        sources, target, split_map=split_map, views=False, progress=progress, cancelled=cancelled
    )
    root = Path(target)
    refresh_manifest(root, pipeline_complete=False)
    events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    source_rows = [
        json.loads(line)
        for line in (root / "sources.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    source_map = {row["asset_id"]: row for row in source_rows}
    rows, review = [], []
    for i, (asset, source) in enumerate(source_map.items()):
        if cancelled():
            raise InterruptedError("行为数据集构建已暂停")
        chosen = [e for e in events if e["source_asset_id"] == asset and e["code"] in selected]
        if not chosen:
            continue
        path = root / source["path"]
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        motion = parse_motion_object(doc, source_path=path)
        payload = base64.b64decode("".join(doc["imu"].split()), validate=True)
        raw = np.ndarray(
            (motion.sample_count, 9),
            dtype="<i2",
            buffer=payload,
            offset=motion.frame_bytes - 18,
            strides=(motion.frame_bytes, 2),
        )
        for event in chosen:
            reason = event.get("exclusion_reason", "")
            if not event["training_eligible"]:
                reason = reason or "标签待复核"
            elif not behavior_allowed(event["code"], event["dataset_category"]):
                reason = "行为与归类不对应"
            elif event["coordinates"] != "parent_imu_ms":
                reason = "缺少母九轴坐标"
            if reason:
                review.append({**event, "reason": reason})
                continue
            first = int(np.searchsorted(motion.times_ms, event["start_ms"]))
            stop = (
                int(np.searchsorted(motion.times_ms, event["end_ms"], side="right"))
                if event["end_ms"] is not None
                else first + 1
            )
            stop = min(stop, motion.sample_count)
            if first >= stop:
                review.append({**event, "reason": "标签范围内没有原始采样点"})
                continue
            cow = event["cow_id"]
            directory = root / "行为数据集" / BEHAVIORS[event["code"]] / cow
            directory.mkdir(parents=True, exist_ok=True)
            epoch = (
                event["start_epoch_ms"]
                if event["start_epoch_ms"] is not None
                else motion.epoch_at(event["start_ms"])
            )
            filename = dataset_filename(cow,event['device_id'],epoch,'.npz',milliseconds=True)
            output = directory / filename
            if output.exists():
                raise ValueError('同一起始时间存在多个行为版本，请先复核母标签')
            np.savez_compressed(
                output,
                raw_counts=raw[first:stop],
                parent_ms=motion.times_ms[first:stop],
                absolute_unix_ms=motion.epoch_at(motion.times_ms[first:stop]),
                original_frame_index=np.arange(first, stop),
                cow_id=np.array(cow),
                device_id=np.array(event["device_id"]),
                behavior=np.array(event["code"]),
                category=np.array(event["dataset_category"]),
                split=np.array(event["split"]),
                source_sha256=np.array(asset),
                event_id=np.array(event["event_id"]),
            )
            rows.append(
                {
                    **event,
                    "sample_path": output.relative_to(root).as_posix(),
                    "samples": stop - first,
                    "first_frame": first,
                    "stop_frame": stop,
                }
            )
        progress(i + 1, len(source_map), source["path"])
    for event in events:
        if event["code"] in selected and event["source_asset_id"] not in source_map:
            review.append({**event, "reason": "缺少完整九轴原件"})
    fields = [
        "event_id",
        "cow_id",
        "device_id",
        "field_mark",
        "code",
        "dataset_category",
        "start_ms",
        "end_ms",
        "start_epoch_ms",
        "end_epoch_ms",
        "split",
        "sample_path",
        "samples",
        "first_frame",
        "stop_frame",
        "source_asset_id",
    ]
    _write_table(root / "行为数据集/样本索引.csv", rows, fields)
    _write_table(root / "行为数据集/待核标签.csv", review, fields + ["reason"])
    for code in selected:
        _write_table(
            root / "行为数据集" / BEHAVIORS[code] / "样本索引.csv",
            [r for r in rows if r["code"] == code],
            fields,
        )
    result.update(
        behavior_samples=len(rows), behavior_review=len(review), selected_behaviors=selected
    )
    if native_features:
        from .dataset_pipeline import build_native_features

        result["native_algorithms"] = build_native_features(
            root,
            app_root=app_root,
            selected_codes=selected,
            behavior_folders=BEHAVIORS,
            progress=progress,
            cancelled=cancelled,
        )
    refresh_manifest(
        root,
        pipeline_complete=True,
        behavior_result=result,
        sample_layout="behavior/ear_tag/ear_tag_device_start_ms.npz",
        category_subfolders=False,
    )
    return result


def build_decision_dataset(sources, target, *, progress=lambda *_: None, cancelled=lambda: False):
    from .decision_dataset import export_decision

    result = export_dataset(sources, target, progress=progress, cancelled=cancelled)
    refresh_manifest(Path(target), pipeline_complete=False)
    result["decision"] = export_decision(target, progress=progress, cancelled=cancelled)
    refresh_manifest(Path(target), pipeline_complete=True, decision_result=result["decision"])
    return result
