"""Collection category is independent of observed behavior and health outcomes."""
from __future__ import annotations

import csv
from pathlib import Path

CATEGORIES = {"healthy": "正常健康", "estrus": "发情",
              "pregnancy_early": "孕早期", "pregnancy_mid": "孕中期", "pregnancy_late": "孕晚期",
              "calving": "产犊", "disease": "疫病"}
CONTEXT_FILE = "数据分类.csv"
LEGACY_FIELDS = ["target_relative_path", "dataset_category", "dataset_category_label", "collection_start",
          "collection_end", "task_id", "confirmed_at", "note"]
IDENTITY_FIELDS = ["device_id", "cow_id", "field_mark", "source_folder", "record_date", "record_start_ms", "identity_provenance"]
FIELDS = [*LEGACY_FIELDS, *IDENTITY_FIELDS]


def category_fields(code):
    if code not in CATEGORIES:
        raise ValueError("请先选择数据类别：正常健康、发情、孕早期、孕中期、孕晚期、产犊或疫病")
    return {"dataset_category": code, "dataset_category_label": CATEGORIES[code]}


def read_context(root, relative):
    root = Path(root).resolve()
    source = (root / relative).resolve()
    if not source.is_relative_to(root):
        raise ValueError("Source escapes dataset root")
    for directory in source.parents:
        if not directory.is_relative_to(root):
            break
        table = directory / CONTEXT_FILE
        if not table.is_file():
            continue
        name = source.relative_to(directory).as_posix()
        with table.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("target_relative_path") == name:
                    category_fields(row.get("dataset_category"))
                    return row
    return {}


def update_context(root, plan):
    """Upsert by relative source path; retries never duplicate assignments."""
    import os
    table = root / CONTEXT_FILE
    records = {}
    if table.exists():
        with table.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames not in (FIELDS, LEGACY_FIELDS):
                raise ValueError("数据分类表头不兼容，停止整理")
            records = {r["target_relative_path"]: r for r in reader}
    for row in plan["rows"]:
        if row["status"] != "ready":
            continue
        relative = Path(row["target"]).relative_to(root).as_posix()
        if plan["mode"] == "normalize":
            old = Path(row["source"]).relative_to(root).as_posix()
            if old in records:
                records[relative] = {**records.pop(old), "target_relative_path": relative}
        else:
            records[relative] = {"target_relative_path": relative, **category_fields(plan["category"]),
                "collection_start": plan["start"], "collection_end": plan["end"], "task_id": plan["id"],
                "confirmed_at": plan["created_at"], "note": plan.get("note", ""),
                **{field: row.get(field, "") for field in IDENTITY_FIELDS}}
    if not records:
        return
    temporary = table.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records.values())
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(table)
