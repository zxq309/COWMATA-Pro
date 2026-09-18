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


REQUIRED_FIELDS = ("产犊开始", "产犊结束", "九轴", "脉搏", "温度")


def sample_eligibility(row):
    """Only sample evaluation authorizes transfer; device ledgers never do."""
    invalid = [key for key in ("九轴", "温度") if str(row.get(key, "")).strip() == "无效"]
    if invalid:
        return "excluded", "、".join(invalid) + "无效，不下载此条记录的三类数据"
    missing = [key for key in REQUIRED_FIELDS if not str(row.get(key, "")).strip()]
    if missing:
        return "pending", "待补全：" + "、".join(missing)
    unknown = [key for key in ("九轴", "温度") if str(row.get(key, "")).strip() != "有效"]
    if unknown:
        return "pending", "待确认有效性：" + "、".join(unknown)
    purpose = str(row.get("监测目的", "")).strip()
    category = csv_category(row)
    if category in ("未分类", "待核对"):
        return "pending", "上传器分类尚未确认，暂不下载"
    # Postpartum and other non-calving monitoring may explicitly use '/' for both fields.
    confirmed_calving = str(row.get("数据分类") or "").strip() in ("calving", "产犊")
    if confirmed_calving or "产犊" in purpose or purpose in ("难产", "死胎"):
        try:
            values = [str(row.get(k, "")).strip() for k in REQUIRED_FIELDS[:2]]
            if not all(re.search(r"[ T]\d{1,2}:\d{2}", value) for value in values):
                raise ValueError()
            start, end = map(parse_time, values)
            if not start or not end or end <= start:
                raise ValueError()
        except (ValueError, TypeError):
            return "pending", "产犊监测须填写有效的产犊开始、结束时间，且结束晚于开始"
    return "eligible", "五项已填写，九轴与温度有效；下载九轴、PPG、温度"


class CsvPlan:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.wears, self.issues, self.births = [], [], {}
        self.sources = {}
        valid_headers = True
        self.evaluations = {}
        self.unparsed_samples = []
        for name in FILES:
            file = self.folder / name
            if not file.is_file():
                self.issues.append({"source": name, "row": 0, "message": "尚未找到 CSV"})
                continue
            raw = file.read_bytes()
            self.sources[name] = hashlib.sha256(raw).hexdigest()
            rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
            required = (
                {"设备号", "牛号", "佩戴开始", "监测目的", *REQUIRED_FIELDS}
                if name == FILES[0]
                else {"设备编码", "新佩戴牛号", "日期"}
                if name == FILES[1]
                else {"牛号", "生产日期"}
            )
            missing_headers = required - set(rows.fieldnames or ())
            if missing_headers:
                valid_headers = False
                self.issues.append(
                    dict(
                        source=name,
                        row=1,
                        message="CSV 缺少必要列：" + "、".join(sorted(missing_headers)),
                    )
                )
                continue
            for index, row in enumerate(rows, 2):
                if row.get("已删除") == "1":
                    continue
                sample = name == FILES[0]
                if sample:
                    self.evaluations[index] = sample_eligibility(row)
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
                    if end_issue:
                        category = "待核对"
                    self.wears.append(
                        Wear(identity, start, end, category, name, index, exact, warnings)
                    )
                except (ValueError, TypeError) as exc:
                    self.issues.append(dict(source=name, row=index, message=str(exc)))
                    if sample:
                        previous, reason = self.evaluations[index]
                        self.evaluations[index] = (
                            previous if previous == "excluded" else "pending",
                            reason + "；" + str(exc),
                        )
                        self.unparsed_samples.append(
                            dict(
                                device=str(row.get("设备号", "")),
                                cow=str(row.get("牛号", "")),
                                mark="",
                                folder="",
                                start=str(row.get("佩戴开始", "")),
                                end=str(row.get("佩戴结束", "")),
                                category=csv_category(row),
                                source=name,
                                row=index,
                                warnings=str(exc),
                            )
                        )
        self.ready = valid_headers and len(self.sources) == len(FILES)
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
        for wear in self.wears:
            if wear.source != FILES[0]:
                continue
            status, reason = self.evaluations[wear.row]
            if status == "eligible" and (not wear.exact or wear.category in ("未分类", "待核对")):
                self.evaluations[wear.row] = (
                    "pending",
                    "台账身份或时段待核对：" + "；".join(wear.warnings),
                )
        # Detect concurrent edits rather than authorize a mixed three-file snapshot.
        if self.ready:
            self.ready = all(
                hashlib.sha256((self.folder / name).read_bytes()).hexdigest() == digest
                for name, digest in self.sources.items()
            )
            if not self.ready:
                self.issues.append(
                    dict(source="三个 CSV", row=0, message="台账在读取期间发生变化，请重试")
                )
        material = "|".join(k + v for k, v in sorted(self.sources.items()))
        self.fingerprint = hashlib.sha256(material.encode()).hexdigest()

    def eligibility(self, wear):
        if wear.source != FILES[0]:
            return "reference", "设备佩戴核对来源；不能单独授权下载"
        status, reason = self.evaluations[wear.row]
        if not self.ready and status == "eligible":
            return "pending", "三份 CSV 未全部读取或读取期间发生变化"
        return status, reason

    def bounds(self, start, end):
        if not self.ready:
            return
        for device, wears in sorted(self.by_device.items()):
            samples = [w for w in wears if w.source == FILES[0]]
            eligible = [w for w in samples if self.eligibility(w)[0] == "eligible"]
            cuts = sorted(
                {
                    start,
                    end,
                    *(max(start, min(end, t)) for w in samples for t in (w.start, w.end or end)),
                }
            )
            merged = []
            for lo, hi in zip(cuts, cuts[1:]):
                active = [w for w in eligible if w.start <= lo and (w.end is None or lo < w.end)]
                rejected = [
                    w
                    for w in samples
                    if self.eligibility(w)[0] != "eligible"
                    and w.start <= lo
                    and (w.end is None or lo < w.end)
                ]
                if not active or rejected or len({(w.identity, w.category) for w in active}) != 1:
                    continue
                if merged and lo == merged[-1][1]:
                    merged[-1] = (merged[-1][0], hi)
                else:
                    merged.append((lo, hi))
            for lo, hi in merged:
                yield device, lo, hi

    def resolve_download(self, device, stamp, raw_cow=""):
        if not self.ready:
            return None, "三份 CSV 未全部核对完成"
        active = [
            w
            for w in self.by_device.get(device_id(device), [])
            if w.source == FILES[0] and w.start <= stamp and (w.end is None or stamp < w.end)
        ]
        if not active or any(self.eligibility(w)[0] != "eligible" for w in active):
            return None, "此时段没有通过五字段与有效性核对的样本记录"
        wear, reason = self.resolve(device, stamp, raw_cow)
        if wear is None or wear.category == "待核对" or wear.source != FILES[0]:
            return None, reason or "身份或分类未明确"
        return wear, reason

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
        records = [
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
        ] + self.unparsed_samples
        for record in records:
            if record["source"] == FILES[0]:
                status, reason = self.evaluations[record["row"]]
                if not self.ready and status == "eligible":
                    status, reason = "pending", "三份 CSV 未全部核对完成"
            else:
                status, reason = "reference", "设备佩戴核对来源；不能单独授权下载"
            record.update(eligibility=status, reason=reason)
        return records
