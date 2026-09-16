"""Ledger 1.3.1 identities and per-wearing download plan; originals stay unchanged."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from cowmata_tailring.workspace.device_identity import DeviceIdentity

from .core import CHINA
from .ledger import csv_category

PREFIX = "546C50CA"
FILES = ("样本试验台账.csv", "扬大测试设备台账.csv", "扬大产犊登记汇总.csv")


def device_id(value):
    text = str(value or "").strip().upper()
    if re.fullmatch(r"[0-9A-F]{12}", text):
        return text
    if re.fullmatch(r"[0-9A-F]{4}", text):
        return PREFIX + text
    raise ValueError("设备编号应为完整 12 位或 4 位简写：" + text)


def cow_identity(value, mark=""):
    text = str(value or "").strip()
    match = re.fullmatch(r"([0-9]{5})(?:-?([A-Za-z0-9\u4e00-\u9fff]+))?", text)
    if not match:
        raise ValueError("牛号需以五位耳标开头：" + text)
    suffix = match[2] or ""
    explicit = str(mark or "").strip()
    if explicit and suffix and explicit.casefold() != suffix.casefold():
        raise ValueError("牛号后缀与现场标记列不一致")
    suffix = explicit or suffix
    if suffix and not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff]+", suffix):
        raise ValueError("现场标记含有路径分隔符或特殊字符")
    return match[1], suffix


def parse_time(value):
    value = str(value or "").strip()
    if not value or value in ("/", "-", "无", "?", "？"):
        return None
    result = datetime.fromisoformat(value.replace("/", "-"))
    return result.replace(tzinfo=CHINA) if result.tzinfo is None else result.astimezone(CHINA)


@dataclass
class Wear:
    identity: DeviceIdentity
    start: datetime
    end: datetime | None
    category: str
    source: str
    row: int
    exact: bool
    warnings: list = field(default_factory=list)


class CsvPlan:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.wears, self.issues, self.births = [], [], {}
        self.sources = {}
        for name in FILES:
            file = self.folder / name
            if not file.is_file():
                self.issues.append({"source": name, "row": 0, "message": "尚未找到 CSV"})
                continue
            raw = file.read_bytes()
            self.sources[name] = hashlib.sha256(raw).hexdigest()
            rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
            for index, row in enumerate(rows, 2):
                if row.get("已删除") == "1":
                    continue
                try:
                    if name == FILES[2]:
                        ear, _ = cow_identity(row.get("牛号"))
                        day, clock = row.get("生产日期", ""), row.get("牛场登记生产时间", "")
                        text = (
                            day + "T" + clock
                            if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", clock)
                            else clock or day
                        )
                        date = parse_time(text)
                        if date:
                            self.births.setdefault(ear, []).append(
                                dict(time=date.isoformat(), source=name, row=index)
                            )
                        continue
                    sample = name == FILES[0]
                    if not sample and row.get("记录类型") not in ("", "佩戴"):
                        continue
                    cow = row.get("牛号") if sample else row.get("新佩戴牛号")
                    dev = row.get("设备号") if sample else row.get("设备编码")
                    if not cow and not dev:
                        continue
                    ear, mark = cow_identity(cow, row.get("现场标记", ""))
                    identity = DeviceIdentity(device_id(dev), ear, mark)
                    start_text = row.get("佩戴开始") if sample else row.get("日期")
                    end_text = row.get("佩戴结束") if sample else row.get("拆除时间(掉落）")
                    start = parse_time(start_text)
                    end_issue = ""
                    try:
                        end = parse_time(end_text)
                    except ValueError:
                        end = None
                        end_issue = "拆除时间不是有效日期：" + str(end_text)
                        self.issues.append(dict(source=name, row=index, message=end_issue))
                    if not start:
                        raise ValueError("缺少佩戴开始日期")
                    exact = bool(sample and len(str(start_text)) > 10)
                    warnings = [row["核对提示"]] if row.get("核对提示", "").strip() else []
                    if end_issue:
                        warnings.append(end_issue)
                    if not exact:
                        warnings.append("台账只有日期；当天换牛记录需按原始牛号核对")
                    if end and len(str(end_text)) <= 10:
                        end += timedelta(days=1)
                    if end and end <= start:
                        raise ValueError("佩戴结束时间不晚于开始时间")
                    category = csv_category(row)
                    if end_issue or (sample and warnings):
                        category = "待核对"
                    self.wears.append(
                        Wear(identity, start, end, category, name, index, exact, warnings)
                    )
                except (ValueError, TypeError) as exc:
                    self.issues.append(dict(source=name, row=index, message=str(exc)))
        self.wears.sort(key=lambda w: (w.identity.device_id, w.start, not w.exact))
        self.by_device = {}
        for wear in self.wears:
            self.by_device.setdefault(wear.identity.device_id, []).append(wear)
        # An open wearing record ends when the device is next assigned to a different cow.
        for wears in self.by_device.values():
            for wear in wears:
                later = [
                    w.start for w in wears if w.start > wear.start and w.identity != wear.identity
                ]
                if wear.end is None and later:
                    wear.end = min(later)
        material = "|".join(k + v for k, v in sorted(self.sources.items()))
        self.fingerprint = hashlib.sha256(material.encode()).hexdigest()

    def bounds(self, start, end):
        for device, wears in sorted(self.by_device.items()):
            intervals = sorted((max(w.start, start), min(w.end or end, end)) for w in wears)
            merged = []
            for lo, hi in intervals:
                if hi <= lo:
                    continue
                if merged and lo <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
                else:
                    merged.append((lo, hi))
            for lo, hi in merged:
                yield device, lo, hi

    def resolve(self, device, stamp, raw_cow=""):
        candidates = [
            w
            for w in self.by_device.get(device_id(device), [])
            if w.start <= stamp and (w.end is None or stamp < w.end)
        ]
        if raw_cow:
            try:
                ear, mark = cow_identity(raw_cow)
            except ValueError:
                return None, "原始记录牛号格式需要核对"
            candidates = [
                w
                for w in candidates
                if w.identity.cow_id == ear
                and (
                    not mark
                    or not w.identity.field_mark
                    or w.identity.field_mark.casefold() == mark.casefold()
                )
            ]
        exact = [w for w in candidates if w.exact]
        if exact:
            candidates = exact
        identities = {w.identity for w in candidates}
        if len(identities) != 1:
            return (
                None,
                "佩戴时段重叠或牛号不一致" if candidates else "CSV 未匹配到该设备、牛号和采集时段",
            )
        chosen = sorted(candidates, key=lambda w: (w.source != FILES[0], w.row))[0]
        categories = {w.category for w in candidates if w.source == FILES[0]}
        if len(categories) > 1:
            chosen = Wear(
                chosen.identity,
                chosen.start,
                chosen.end,
                "待核对",
                chosen.source,
                chosen.row,
                chosen.exact,
                ["同一时段的台账分类存在冲突"],
            )
        return chosen, "；".join(chosen.warnings)

    def preview(self):
        return [
            dict(
                device=w.identity.device_id,
                cow=w.identity.cow_id,
                mark=w.identity.field_mark,
                folder=w.identity.folder_name,
                start=w.start.isoformat(),
                end=w.end.isoformat() if w.end else "",
                category=w.category,
                source=w.source,
                row=w.row,
                warnings="；".join(w.warnings),
            )
            for w in self.wears
        ]
