"""待产犊台账: the rows of 样本试验台账 whose calving start and end are both blank.

Derived on the server from the samples CSV after every accepted write, so the
three views (产犊 / 孕晚期 / 待产犊) can never disagree with the ledger itself.
Leaving or entering the cohort is appended to 待产犊变更记录.csv; that log is
what scores pure out-of-sample calving predictions once an outcome is filled.
"""
from __future__ import annotations

import csv
import io
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ledger_core import CATEGORIES, FIELDS, classify_record, directory

PENDING = "pending_calving"
PENDING_FILE = "待产犊台账.csv"
CHANGES_FILE = "待产犊变更记录.csv"
CHANGE_FIELDS = ["时间", "变更", "记录ID", "牛号", "设备号", "佩戴开始", "佩戴结束", "原分类", "新分类", "产犊开始", "产犊结束", "说明"]
ZONE = timezone(timedelta(hours=8))


def _atomic(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _encode(rows, fields):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\r\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def _read(path):
    if not path.is_file():
        return None
    return list(csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8-sig"), newline="")))


def derive(rows):
    """Recompute the derived columns and return (all rows, pending rows) without mutating input."""
    result = []
    for row in rows:
        row = dict(row)
        if row.get("已删除") != "1":
            row["数据分类"] = classify_record(row)[0]
            row["归类目录"] = directory(row)
        result.append(row)
    pending = [r for r in result if r.get("已删除") != "1" and r["数据分类"] == PENDING]
    pending.sort(key=lambda r: (r.get("佩戴开始", ""), r.get("牛号", ""), r["记录ID"]))
    return result, pending


def refresh(samples_path, now=None):
    """Rewrite 待产犊台账.csv next to the samples CSV; log cohort changes. Returns change counts."""
    samples_path = Path(samples_path)
    rows = _read(samples_path) or []
    everything, pending = derive(rows)
    by_id = {r["记录ID"]: r for r in everything}
    folder = samples_path.parent
    target = folder / PENDING_FILE
    previous = {r["记录ID"]: r for r in (_read(target) or [])}
    current = {r["记录ID"]: r for r in pending}
    stamp = (now or datetime.now(ZONE)).isoformat(timespec="seconds")
    changes = []
    for key in sorted(set(previous) | set(current)):
        old, new = previous.get(key), current.get(key)
        if old and not new:
            row = by_id.get(key, old)
            deleted = row.get("已删除") == "1" or key not in by_id
            kind = "删除" if deleted else "移出"
            category = "已删除" if deleted else CATEGORIES.get(row.get("数据分类", ""), row.get("数据分类", ""))
            changes.append(dict(row, 时间=stamp, 变更=kind, 原分类=CATEGORIES[PENDING], 新分类=category,
                                说明="产犊时间已填写" if kind == "移出" else "台账记录已删除"))
        elif new and not old:
            changes.append(dict(new, 时间=stamp, 变更="新增", 原分类="",
                                新分类=CATEGORIES[PENDING], 说明="产犊开始、结束均未填写"))
        elif new and any(old.get(f, "") != new.get(f, "") for f in FIELDS if f not in ("版本", "修改时间")):
            changes.append(dict(new, 时间=stamp, 变更="修改", 原分类=CATEGORIES[PENDING], 新分类=CATEGORIES[PENDING],
                                说明="仍待产犊，台账字段有更新"))
    # Log first: if it is locked (open in Excel) the snapshot stays old, so the
    # next refresh finds and logs the same changes instead of losing them.
    if changes:
        log = folder / CHANGES_FILE
        existing = _read(log) or []
        _atomic(log, _encode(existing + [{k: c.get(k, "") for k in CHANGE_FIELDS} for c in changes], CHANGE_FIELDS))
    content = _encode(pending, FIELDS)
    if not target.exists() or target.read_bytes() != content:
        _atomic(target, content)
    counts = {"待产犊": len(pending)}
    for change in changes:
        counts[change["变更"]] = counts.get(change["变更"], 0) + 1
    return counts
