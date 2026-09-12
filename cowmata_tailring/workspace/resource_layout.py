"""Shared 3.4 resource schema and half-open acquisition intervals (UTC+08)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
MODALITIES = ("Motion", "PPG", "Video")
PPG_PLACEHOLDER = {"status": "reserved", "available": False, "channels": [],
                   "time_basis": "acquisition", "intervals": [], "annotations": []}


def day_at(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).date().isoformat()


def stamp_at(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]


def start_stamp(ms, *, milliseconds=False):
    return stamp_at(ms) if milliseconds else stamp_at(ms)[:19]


def dataset_filename(cow,device,start,suffix,*,milliseconds=False):
    import re
    cow,device=str(cow or 'NA'),str(device or 'NA')
    if any(not re.fullmatch(r'[A-Za-z0-9-]+',value) for value in (cow,device)):
        raise ValueError('数据集耳标或设备编号不能作为文件名，请先核对归类')
    return f'{cow}_{device}_{start_stamp(start,milliseconds=milliseconds)}{suffix}'


def covered_days(start, end):
    if end <= start:
        raise ValueError("采集结束时间必须晚于开始时间")
    first = datetime.fromtimestamp(start / 1000, TZ).date()
    last = datetime.fromtimestamp((end - .001) / 1000, TZ).date()
    return [(first + timedelta(days=n)).isoformat() for n in range((last-first).days + 1)]


def overlaps_day(start, end, day):
    lo = datetime.fromisoformat(day).replace(tzinfo=TZ).timestamp() * 1000
    return start < lo + 86400000 and end > lo


def resource_context(path):
    """Return category scope even when a user chooses Motion/day or Video/day."""
    path = Path(path).resolve()
    import json
    for directory in (path, *path.parents):
        marker = directory / "资源迁移.json"
        if marker.is_file():
            value = json.loads(marker.read_text(encoding="utf-8"))
            if value.get("schema") == "cowmata-relocation-3.4":
                destination = Path(value["target"]).resolve(strict=True)
                if not all((destination / name).is_dir() for name in MODALITIES):
                    raise ValueError("Relocated resource directory is incomplete")
                return destination
    for directory in (path, *path.parents):
        if all((directory / name).is_dir() for name in MODALITIES):
            return directory
    return path


def ppg_status(root):
    scope = resource_context(root)
    files = sum(1 for p in (scope / "PPG").rglob("*") if p.is_file() and p.name != "占位说明.txt")
    return {**PPG_PLACEHOLDER, "source_files": files,
            "status": "pending_parser" if files else "reserved"}
