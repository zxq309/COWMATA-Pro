"""Read paired datasets; merge class copies, retain uncertainty and provenance."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import INDIVIDUAL_CODES


def stamp(path):
    value = Path(path).stat()
    return [value.st_size, value.st_mtime_ns]


def scan_dataset(
    root,
    *,
    pool_general_events=False,
    progress=lambda *_: None,
    cancelled=lambda: False,
    collect_shared=False,
):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("Dataset directory does not exist")
    records, issues, entries = {}, [], []
    paths = sorted(root.rglob("*_label.json"))
    for index, label in enumerate(paths):
        if cancelled():
            raise InterruptedError("Dataset scan cancelled")
        if label.is_symlink() or ".label-history" in label.parts:
            continue
        try:
            before = stamp(label)
            content = label.read_bytes()
            doc = json.loads(content.decode("utf-8-sig"))
            if stamp(label) != before:
                raise ValueError("Label changed while being read")
            work = doc.get("work", {})
            project = work.get("project", {})
            data = doc.get("dataset", {})
            identity = project.get("device_identity") or doc.get("device_identity") or {}
            raw = label.parent.parent / "Raw" / label.name.replace("_label.json", "_raw.json")
            if not raw.is_file() or raw.is_symlink():
                raise ValueError("Missing original paired JSON")
            asset = (
                data.get("raw_sha256")
                or work.get("asset_id")
                or doc.get("source", {}).get("asset_id")
            )
            if not isinstance(asset, str) or not re.fullmatch(r"[0-9a-f]{64}", asset):
                raise ValueError("Missing original content identity")
            # The documented filename is a fallback only; conflicts are retained.
            parts = raw.name.split("_", 1)[0].split("-", 2)
            filename_cow = parts[1] if len(parts) >= 2 else ""
            cow = str(project.get("cow_id") or identity.get("cow_id") or filename_cow).strip()
            device = str(
                identity.get("device_id")
                or project.get("source", {}).get("device")
                or (parts[0] if len(parts) >= 2 else "")
            ).strip()
            mark = str(identity.get("field_mark") or (parts[2] if len(parts) == 3 else ""))
            if not cow or not device:
                raise ValueError("Missing cow or device identity")
            row = records.setdefault(
                asset,
                dict(
                    asset_id=asset,
                    cow_id=cow,
                    device_id=device,
                    field_mark=mark,
                    modality="ppg" if "PPG" in raw.parts else "motion",
                    raw=str(raw),
                    aliases=[],
                    labels=[],
                    events={},
                    review_coverage=[],
                    category=doc.get("dataset_category", ""),
                    conflicts=[],
                    raw_stamp=stamp(raw),
                    identity_eligible=True,
                    split_group=asset,
                    label_stamps={},
                ),
            )
            if row["cow_id"] != cow or identity.get("cow_id") and str(identity["cow_id"]) != cow:
                row["conflicts"].append("conflicting_cow_identity")
            if row["device_id"] != device:
                row["conflicts"].append("conflicting_device_identity")
            row["aliases"].append(str(raw))
            row["labels"].append(str(label))
            row["label_stamps"][str(label)] = before
            for e in project.get("events", []):
                code = e.get("label_code")
                start, end = e.get("t0"), e.get("t1")
                if (
                    not code
                    or not isinstance(start, int | float)
                    or not math.isfinite(start)
                    or start < 0
                ):
                    issues.append(
                        dict(path=str(label), reason="invalid_event", event_id=e.get("id"))
                    )
                    continue
                if end is not None and (
                    not isinstance(end, int | float) or not math.isfinite(end) or end < start
                ):
                    issues.append(
                        dict(path=str(label), reason="invalid_event_range", event_id=e.get("id"))
                    )
                    continue
                key = (code, float(start), float(end) if end is not None else None)
                row["events"].setdefault(
                    key,
                    dict(
                        code=code,
                        start_ms=start,
                        end_ms=end,
                        event_id=e.get("id"),
                        confirmation=e.get("confirmation", ""),
                        source_time_review=bool(e.get("source_time_review")),
                        label_source=str(label),
                    ),
                )
            # A per-event reviewed_range does NOT establish reviewed background.
            for interval in doc.get("review_coverage", []):
                if (
                    isinstance(interval, list)
                    and len(interval) == 2
                    and 0 <= interval[0] < interval[1]
                ):
                    row["review_coverage"].append(interval)
            entries.append(
                [
                    str(label.relative_to(root)),
                    hashlib.sha256(content).hexdigest(),
                    str(raw.relative_to(root)),
                    stamp(raw),
                ]
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append(dict(path=str(label), reason=str(exc)))
        if index % 32 == 0 or index + 1 == len(paths):
            progress(index + 1, len(paths), "扫描标签与原始数据")
    result = []
    for row in records.values():
        if row["conflicts"]:
            issues.extend(
                dict(path=row["raw"], reason=reason) for reason in sorted(set(row["conflicts"]))
            )
            if not pool_general_events or "conflicting_device_identity" in row["conflicts"]:
                continue
            row["identity_eligible"] = False
            row["events"] = {
                k: e for k, e in row["events"].items() if e["code"] not in INDIVIDUAL_CODES
            }
            if not row["events"]:
                continue
        row["events"] = sorted(row["events"].values(), key=lambda e: (e["start_ms"], e["code"]))
        row["review_coverage"] = sorted({tuple(v) for v in row["review_coverage"]})
        result.append(row)
    from cowmata_tailring.workspace.shared_labels import CONTRACT, link_records

    shared = link_records(result, issues, cancelled, collect_all=collect_shared)
    counts = Counter(e["code"] for r in result for e in r["events"])
    cows = defaultdict(set)
    for r in result:
        for e in r["events"]:
            cows[e["code"]].add(r["cow_id"])
    digest = hashlib.sha256(
        json.dumps([entries, pool_general_events], sort_keys=True).encode()
    ).hexdigest()
    return dict(
        schema="algorithm-dataset-1",
        root=str(root),
        fingerprint=digest,
        records=result,
        pool_general_events=pool_general_events,
        shared_label_contract=dict(CONTRACT),
        shared_labels=shared,
        issues=issues,
        summary=dict(
            label_files=len(paths),
            unique_records=len(result),
            labeled_records=sum(bool(r["events"]) for r in result),
            events=dict(counts),
            event_cows={k: len(v) for k, v in cows.items()},
            cow_count=len({r["cow_id"] for r in result}),
            complete_review_asserted=False,
        ),
    )
