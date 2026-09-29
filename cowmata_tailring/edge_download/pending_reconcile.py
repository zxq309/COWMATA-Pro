"""4.4.1: keep the 待产犊 folder in step with 样本试验台账.

When the field team fills a 待产犊 cow's calving start/end, its wearing becomes 产犊 (inside the
wearing) or 孕晚期 ('/' or calved after removal). Every file of that wearing already downloaded into
``<farm>\\待产犊`` is moved to the category the ledger now gives — the same resolution the downloader
uses to file new data (``CsvPlan.resolve``), so upload, server and download stay aligned. Rows that
are still blank stay in 待产犊 and keep feeding the real-time decider. Bytes are never rewritten;
a different file already at the destination is left untouched and reported.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from pathlib import Path

from .core import CHINA

PENDING = "待产犊"
MODALITIES = ("Motion", "PPG", "Temp")
NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.json$")
KEEP = {PENDING, "待核对", "未分类", "待分类", ""}


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).digest()


def reconcile_pending(farm, ledger_directory, log=lambda message: None, plan=None):
    """Move 待产犊 files whose ledger row now has an outcome. Returns {category: moved files}."""
    from .csv_targets import FILES, CsvPlan

    farm = Path(farm)
    base = farm / PENDING
    if not base.is_dir():
        return {}
    plan = plan or CsvPlan(ledger_directory)
    if not plan.ready:
        return {}
    moved, conflicts = {}, 0
    for modality in MODALITIES:
        root = base / modality
        if not root.is_dir():
            continue
        for day in sorted(p for p in root.iterdir() if p.is_dir()):
            for folder in sorted(p for p in day.iterdir() if p.is_dir()):
                parts = folder.name.split("-")
                device, raw_cow = parts[0].upper(), "-".join(parts[1:])
                for file in sorted(folder.glob("*.json")):
                    m = NAME.match(file.name)
                    if not m:
                        continue
                    stamp = datetime(*(int(x) for x in m.groups()), tzinfo=CHINA)
                    wear, _reason = plan.resolve(device, stamp, raw_cow)
                    if wear is None or wear.source != FILES[0] or wear.category in KEEP:
                        continue
                    target = farm / Path(wear.category) / modality / day.name / wear.identity.folder_name / file.name
                    if target.exists():
                        if _digest(target) == _digest(file):
                            file.unlink()
                            moved[wear.category] = moved.get(wear.category, 0) + 1
                        else:
                            conflicts += 1
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(file, target)
                    moved[wear.category] = moved.get(wear.category, 0) + 1
    for folder in sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            folder.rmdir()
        except OSError:
            pass
    if moved or conflicts:
        log("待产犊核对：台账已补产犊时间，" + "，".join(f"{n} 个文件归入 {c}" for c, n in moved.items())
            + (f"；{conflicts} 个文件目标已有不同内容，保留在待产犊" if conflicts else ""))
    return moved
