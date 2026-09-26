"""Calving outcome truth for decision training (never used as a model input).

Two sources, merged per cow:

* farm ledger (``扬大产犊登记汇总.csv``): registration clock time, minute resolution but often
  rounded by staff (±30–60 min);
* video-confirmed ``CALF_FULLY_EXPELLED`` labels from the calving dataset (second resolution).

When both exist within ``match_hours`` the video time wins. Cows without a registered
calving are *unlabelled*, not negative.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

HOUR_MS = 3_600_000


def ledger_calvings(ledger_path):
    """{cow_id: [epoch_ms, ...]} from the farm calving ledger."""
    from cowmata_tailring.edge_download.csv_targets import cow_identity, parse_time

    births = {}
    with Path(ledger_path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("已删除") == "1":
                continue
            day, clock = (row.get("生产日期") or "").strip(), (row.get("牛场登记生产时间") or "").strip()
            if not clock or ":" not in clock:
                continue
            try:
                text = day + "T" + clock if re.match(r"^\d{1,2}:", clock) else clock
                when = parse_time(text)
                cow, _ = cow_identity(row["牛号"])
            except (ValueError, KeyError):
                continue
            if when is not None and cow:
                births.setdefault(cow, set()).add(int(when.timestamp() * 1000))
    return {cow: sorted(values) for cow, values in births.items()}


def video_calvings(dataset_root, code="CALF_FULLY_EXPELLED", progress=lambda *_: None):
    """{cow_id: [epoch_ms, ...]} from reviewed paired labels (Raw/Label) of the calving dataset."""
    from cowmata_tailring.algorithms.dataset import scan_dataset
    from cowmata_tailring.workspace.sensor_records import load_sensor_json

    root = Path(dataset_root)
    folder = root / "CalfFullyExpelled" if (root / "CalfFullyExpelled").is_dir() else root
    index = scan_dataset(folder, progress=progress)
    births = {}
    for record in index["records"]:
        events = [e for e in record["events"] if e["code"] == code]
        if not events or not record.get("identity_eligible", True):
            continue
        try:
            sensor = load_sensor_json(record["raw"], kind=record.get("modality"))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for event in events:
            births.setdefault(record["cow_id"], set()).add(int(sensor.epoch_at(float(event["start_ms"]))))
    return {cow: sorted(values) for cow, values in births.items()}


def merge_calvings(ledger=None, video=None, *, match_hours=12):
    """Merge sources; returns {cow: [dict(epoch_ms, source, ledger_offset_h)]}."""
    ledger, video = ledger or {}, video or {}
    merged = {}
    for cow in sorted(set(ledger) | set(video)):
        items = [dict(epoch_ms=t, source="video", ledger_offset_h=None) for t in video.get(cow, [])]
        for t in ledger.get(cow, []):
            near = [i for i in items if i["source"] == "video" and abs(i["epoch_ms"] - t) <= match_hours * HOUR_MS]
            if near:
                near[0]["ledger_offset_h"] = round((t - near[0]["epoch_ms"]) / HOUR_MS, 3)
            elif not any(abs(i["epoch_ms"] - t) <= 20 * 24 * HOUR_MS for i in items if i["source"] == "ledger"):
                items.append(dict(epoch_ms=t, source="ledger", ledger_offset_h=None))
        merged[cow] = sorted(items, key=lambda i: i["epoch_ms"])
    return merged


def load_calvings(ledger_path=None, dataset_root=None, *, progress=lambda *_: None):
    ledger = ledger_calvings(ledger_path) if ledger_path and Path(ledger_path).is_file() else {}
    video = {}
    if dataset_root and Path(dataset_root).is_dir():
        try:
            video = video_calvings(dataset_root, progress=progress)
        except (OSError, ValueError) as exc:
            progress(0, 0, f"视频娩出标签不可用：{exc}")
    if not ledger and not video:
        raise ValueError("没有可用的产犊时间：请提供产犊登记 CSV 或含“犊牛完全娩出”标签的数据集")
    return merge_calvings(ledger, video)


def label_row(cow_calvings, t_ms, *, lookback_days=10):
    """Return (hours_to_calving, calving_epoch_ms, source) for the next calving, else None.

    Rows after the latest calving (postpartum) or more than ``lookback_days`` before the next
    calving are unlabelled so that far-from-term periods cannot silently become negatives.
    """
    for item in cow_calvings or ():
        delta = item["epoch_ms"] - t_ms
        if 0 < delta <= lookback_days * 24 * HOUR_MS:
            return delta / HOUR_MS, item["epoch_ms"], item["source"]
        if delta > lookback_days * 24 * HOUR_MS:
            return None
    return None


# 4.3.4 truth contract: annotation endpoints define a calving interval.  The
# legacy point helpers above remain for old callers, while the new helpers are
# used by the decision dataset builder and preserve endpoint provenance.
def video_calvings(dataset_root, code="CALF_FULLY_EXPELLED", progress=lambda *_: None):
    from cowmata_tailring.algorithms.dataset import scan_dataset
    from cowmata_tailring.workspace.sensor_records import load_sensor_json

    root = Path(dataset_root)
    records = scan_dataset(root, progress=progress)["records"]
    by_cow = {}
    for record in records:
        if not record.get("identity_eligible", True):
            continue
        try:
            sensor = load_sensor_json(record["raw"], kind=record.get("modality"))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        starts, ends = [], []
        for event in record.get("events", []):
            marker = event.get("code")
            when = event.get("start_ms", event.get("point_ms"))
            if when is None:
                continue
            epoch = int(sensor.epoch_at(float(when)))
            if marker == "FETAL_PART_FIRST_VISIBLE":
                starts.append(epoch)
            elif marker == code:
                ends.append(epoch)
        if not starts and not ends:
            continue
        bucket = by_cow.setdefault(record["cow_id"], {"starts": [], "ends": []})
        bucket["starts"].extend(starts)
        bucket["ends"].extend(ends)
    result = {}
    for cow, bucket in by_cow.items():
        starts = sorted(set(bucket["starts"]))
        used = set()
        items = []
        for end in sorted(set(bucket["ends"])):
            candidates = [(idx, value) for idx, value in enumerate(starts)
                          if idx not in used and value <= end and end - value <= 7 * 24 * HOUR_MS]
            if not candidates:
                continue
            idx, start = candidates[-1]
            used.add(idx)
            items.append(dict(start_epoch_ms=start, end_epoch_ms=end, epoch_ms=start,
                              duration_ms=end - start, source="annotation_gold",
                              label_quality="gold", training_eligible=True))
        if items:
            result[cow] = items
    return result


def merge_calvings(ledger=None, video=None, *, match_hours=12):
    """Prefer annotated intervals; ledger times are retained as approximate fallback only."""
    ledger, video = ledger or {}, video or {}
    merged = {}
    for cow in sorted(set(ledger) | set(video)):
        if video.get(cow):
            merged[cow] = sorted(video[cow], key=lambda item: item["start_epoch_ms"])
            continue
        merged[cow] = [dict(start_epoch_ms=t, end_epoch_ms=t, epoch_ms=t,
                            duration_ms=0, source="ledger_approx", label_quality="approximate",
                            training_eligible=False) for t in ledger.get(cow, [])]
    return merged


def label_row_detail(cow_calvings, t_ms, *, lookback_days=10):
    for item in sorted(cow_calvings or (), key=lambda value: value.get("start_epoch_ms", value.get("epoch_ms", 0))):
        start = int(item.get("start_epoch_ms", item.get("epoch_ms")))
        end = int(item.get("end_epoch_ms", start))
        if t_ms <= end and start - t_ms <= lookback_days * 24 * HOUR_MS:
            return dict(hours_to_calving=max(0.0, (start - t_ms) / HOUR_MS),
                        calving_epoch_ms=start, calving_start_epoch_ms=start,
                        calving_end_epoch_ms=end, calving_interval_ms=max(0, end - start),
                        label_source=item.get("source", "unknown"),
                        label_quality=item.get("label_quality", "unknown"),
                        training_eligible=bool(item.get("training_eligible", False)))
        if start - t_ms > lookback_days * 24 * HOUR_MS:
            return None
    return None


def label_row(cow_calvings, t_ms, *, lookback_days=10):
    detail = label_row_detail(cow_calvings, t_ms, lookback_days=lookback_days)
    if detail is None:
        return None
    return detail["hours_to_calving"], detail["calving_epoch_ms"], detail["label_source"]
