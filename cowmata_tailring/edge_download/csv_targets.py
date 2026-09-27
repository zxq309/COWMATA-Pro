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
# Outcome words are calving RESULTS, never timestamps; unknown outcome texts
# reach the same treatment through the birth-registry note channel below.
OUTCOME_WORDS = ("死胎", "流产", "难产", "早产")


def is_outcome_text(value):
    text = str(value or "").strip()
    return bool(text) and any(word in text for word in OUTCOME_WORDS)


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


def equipment_start_time(row):
    """Read the normalized date for merged Excel cells without changing CSV.

    The field-ledger importer preserves the literal cell text in ``日期``
    (for example ``新硅胶垫``) and records the inherited date in ``归类目录``.
    That literal is not a date error when the derived directory contains a
    unique YYYY-MM-DD segment for the wearing record.
    """
    raw = str(row.get("日期", "") or "").strip()
    try:
        return parse_time(raw), ""
    except ValueError:
        catalog = str(row.get("归类目录", "") or "").replace("\\", "/")
        match = re.search(r"/佩戴台账/(\d{4}-\d{2}-\d{2})/", catalog)
        if match:
            return parse_time(match.group(1)), "日期栏使用合并单元格原文；已采用归类目录中的继承日期"
        raise ValueError("设备台账日期不是有效日期：" + raw) from None


def birth_time(day, clock):
    """Split a birth-registry time cell into a legal time or an outcome text.

    A full datetime string (``2026-08-18 09:30``) is a legal time and must
    not be misfiled into notes; any other non-empty unparseable text is an
    outcome note such as ``死胎`` and is never used as a timestamp.
    """
    day = str(day or "").strip()
    clock = str(clock or "").strip()
    if not clock:
        text = day
    elif re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", clock):
        text = day + "T" + clock if day else clock
    else:
        text = clock
    try:
        return parse_time(text), None
    except ValueError:
        return None, (clock or text)


class _IncompleteRow(ValueError):
    """A merely unfinished row stays pending; it is never a format error."""


def _edit_distance_within_one(a, b):
    """True when a single edit turns a into b, for near-code suggestions."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    skipped = False
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


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
    end_issue: bool = False


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
        self.wears, self.issues, self.notes, self.births = [], [], [], {}
        self.sources = {}
        valid_headers = True
        self.evaluations = {}
        self.unparsed_samples = []
        self.sample_rows = {}
        self.outcome_rows = set()
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
                    self.sample_rows[index] = row
                try:
                    if name == FILES[2]:
                        if not str(row.get("牛号", "") or "").strip():
                            raise _IncompleteRow("待补全：牛号未填写")
                        ear, _ = cow_identity(row.get("牛号"))
                        day, clock = row.get("生产日期", ""), row.get("牛场登记生产时间", "")
                        date, outcome = birth_time(day, clock)
                        if outcome is not None:
                            self.notes.append(dict(
                                kind="outcome",
                                source=name,
                                row=index,
                                record_id=str(row.get("记录ID", "") or ""),
                                cow=ear,
                                cow_text=str(row.get("牛号", "") or ""),
                                device="",
                                field="牛场登记生产时间",
                                text=outcome,
                                message=f"牛场登记生产时间为“{outcome}”，按结局备注保留，未作为时间匹配",
                                download_start="",
                                download_end="",
                            ))
                            continue
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
                    if not str(cow or "").strip():
                        raise _IncompleteRow("待补全：牛号未填写")
                    if not str(dev or "").strip():
                        raise _IncompleteRow("待补全：设备号未填写")
                    ear, mark = cow_identity(cow, row.get("现场标记", ""))
                    identity = DeviceIdentity(device_id(dev), ear, mark)
                    start_text = row.get("佩戴开始") if sample else row.get("日期")
                    end_text = row.get("佩戴结束") if sample else row.get("拆除时间(掉落）")
                    inherited_warning = ""
                    if sample:
                        start = parse_time(start_text)
                    else:
                        start, inherited_warning = equipment_start_time(row)
                    end_issue = ""
                    try:
                        end = parse_time(end_text)
                    except ValueError:
                        end = None
                        end_issue = "拆除时间不是有效日期：" + str(end_text)
                        self.issues.append(self._row_issue(name, index, row, end_issue))
                    if not start:
                        if str(start_text or "").strip():
                            raise ValueError("佩戴开始不是有效日期：" + str(start_text))
                        raise _IncompleteRow("待补全：佩戴开始日期未填写")
                    exact = bool(sample and len(str(start_text)) > 10)
                    warnings = [row["核对提示"]] if row.get("核对提示", "").strip() else []
                    if inherited_warning:
                        warnings.append(inherited_warning)
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
                        Wear(identity, start, end, category, name, index, exact, warnings,
                             end_issue=bool(end_issue))
                    )
                except (ValueError, TypeError) as exc:
                    # Merely unfinished rows stay pending and invisible to the
                    # download plan; only format/identity errors form the
                    # copyable problem list.
                    if not isinstance(exc, _IncompleteRow):
                        self.issues.append(self._row_issue(name, index, row, str(exc)))
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
        self._authorize_outcome_rows()
        for wear in self.wears:
            if wear.source != FILES[0]:
                continue
            status, reason = self.evaluations[wear.row]
            if status == "eligible" and (
                not wear.exact
                or (wear.category in ("未分类", "待核对") and wear.row not in self.outcome_rows)
            ):
                self.evaluations[wear.row] = (
                    "pending",
                    "台账身份或时段待核对：" + "；".join(wear.warnings),
                )
        self._enrich_device_issues()
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

    def _authorize_outcome_rows(self):
        """Outcome texts authorize the wearing range; they never forge times.

        A row whose calving ended as 死胎/流产/难产/早产 (or whose cow has a
        non-time birth-registry note) downloads by device and wearing range
        when the identity and that range are reliable.  The category itself,
        the missing birth times and the original text are all preserved.
        """
        by_ear = {}
        for note in self.notes:
            if note.get("kind") == "outcome" and note["source"] == FILES[2] and note["cow"]:
                by_ear.setdefault(note["cow"], []).append(note)
        for wear in self.wears:
            if wear.source != FILES[0] or wear.end_issue or not wear.exact:
                continue
            row = self.sample_rows.get(wear.row)
            if row is None:
                continue
            purpose = str(row.get("监测目的", "") or "").strip()
            linked = by_ear.get(wear.identity.cow_id, [])
            outcome_row = is_outcome_text(purpose) or bool(linked)
            if outcome_row and linked:
                # Link the birth-registry note to every reliable wearing of
                # this cow, whatever the sample row's final eligibility is.
                start = wear.start.isoformat()
                end = wear.end.isoformat() if wear.end else ""
                for note in linked:
                    if not note["device"]:
                        note["device"] = wear.identity.device_id
                        note["download_start"] = start
                        note["download_end"] = end
                    elif note["device"] != wear.identity.device_id and not any(
                        n.get("record_id") == note.get("record_id")
                        and n.get("device") == wear.identity.device_id
                        for n in self.notes
                    ):
                        self.notes.append(dict(note, device=wear.identity.device_id,
                                               download_start=start, download_end=end))
            if not outcome_row:
                continue
            status, _ = self.evaluations[wear.row]
            if status != "pending":
                continue
            if str(row.get("核对提示", "") or "").strip():
                continue
            sensors = {k: str(row.get(k, "") or "").strip() for k in ("九轴", "脉搏", "温度")}
            if not all(sensors.values()) or sensors["九轴"] != "有效" or sensors["温度"] != "有效":
                continue
            text = purpose if is_outcome_text(purpose) else "、".join(n["text"] for n in linked)
            self.outcome_rows.add(wear.row)
            self.evaluations[wear.row] = (
                "eligible",
                f"结局已备注（{text}）：未补生产时间、未改分类；按设备与佩戴范围下载",
            )
            self.notes.append(dict(
                kind="outcome",
                source=FILES[0],
                row=wear.row,
                record_id=str(row.get("记录ID", "") or ""),
                cow=wear.identity.cow_id,
                cow_text=str(row.get("牛号", "") or ""),
                device=wear.identity.device_id,
                field="监测目的",
                text=purpose or text,
                message=f"样本监测目的为“{purpose or text}”，按结局备注保留；未补产犊时间，分类保持原样",
                download_start=wear.start.isoformat(),
                download_end=wear.end.isoformat() if wear.end else "",
            ))

    def _enrich_device_issues(self):
        """Suggest near codes and cross-table occurrences; never auto-replace."""
        valid_devices = set(self.by_device)
        for issue in self.issues:
            if issue.get("field") not in ("设备号", "设备编码") or not issue.get("device"):
                continue
            code = re.sub(r"[^0-9A-Fa-f]", "", issue["device"]).upper()
            near = sorted(d for d in valid_devices if _edit_distance_within_one(code, d))
            others = [
                f"{other['source']} 第 {other['row']} 行"
                for other in self.issues
                if other is not issue
                and other.get("field") in ("设备号", "设备编码")
                and re.sub(r"[^0-9A-Fa-f]", "", str(other.get("device", ""))).upper() == code
            ]
            parts = []
            if near:
                parts.append("待确认候选（相近编码，程序不会自动替换）：" + "、".join(near))
            if others:
                parts.append("同一编码还出现在：" + "、".join(others))
            parts.append("请现场确认后在原台账修正，经上传器同步后再点“更新台账并下载”补齐")
            issue["suggestion"] = "；".join(parts)

    def _row_issue(self, source, row_index, row, message):
        device_field = "设备编码" if source == FILES[1] else "设备号"
        cow_field = "新佩戴牛号" if source == FILES[1] else "牛号"
        device = str(row.get(device_field, "") or "").strip()
        cow = str(row.get(cow_field, "") or "").strip()
        field, raw, suggestion = self._explain(source, message, row)
        return dict(
            source=source,
            row=row_index,
            message=message,
            cow=cow,
            device=device,
            record_id=str(row.get("记录ID", "") or ""),
            field=field,
            raw=raw,
            reason=message,
            suggestion=suggestion,
        )

    @staticmethod
    def _explain(source, message, row):
        device_field = "设备编码" if source == FILES[1] else "设备号"
        if message.startswith("设备编号"):
            return device_field, str(row.get(device_field, "") or "").strip(), "设备号应为 12 位十六进制（4 位简写自动补全）"
        if message.startswith("牛号需"):
            return "牛号", str(row.get("牛号", "") or "").strip(), "牛号需以五位耳标开头"
        if "现场标记" in message:
            return "现场标记", str(row.get("现场标记", "") or "").strip(), "牛号后缀与现场标记需一致"
        if "佩戴开始" in message or "日期不是有效日期" in message:
            field = "日期" if source == FILES[1] else "佩戴开始"
            return field, str(row.get(field, "") or "").strip(), "请填写有效的佩戴开始时间"
        if "拆除时间" in message:
            field = "拆除时间(掉落）"
            return field, str(row.get(field, "") or "").strip(), "拆除时间应为日期或留空"
        if "佩戴结束时间不晚于" in message:
            return "佩戴结束", str(row.get("佩戴结束", "") or "").strip(), "佩戴结束须晚于佩戴开始"
        return "", "", "请现场核对后修正台账并通过上传器同步"

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
            if not eligible:
                continue
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
                if not active or len({(w.identity, w.category) for w in active}) != 1:
                    continue
                active_identity = next(iter(active)).identity
                # An unfinished row of the SAME cow/device cannot misfile data
                # and must not block the complete row.  A different identity
                # covering the same segment is a real overlap and stays
                # excluded until the ledger is corrected.
                conflict = [
                    w
                    for w in samples
                    if w.identity != active_identity
                    and w.start <= lo
                    and (w.end is None or lo < w.end)
                ]
                if conflict:
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
        eligible = [w for w in active if self.eligibility(w)[0] == "eligible"]
        if not eligible:
            return None, "此时段没有通过五字段与有效性核对的样本记录"
        # An unfinished row of the SAME cow/device cannot misfile data.  A
        # different identity covering the same stamp is a real overlap and
        # stays excluded until the ledger is corrected on site.
        eligible_ids = {w.identity for w in eligible}
        if any(w.identity not in eligible_ids for w in active):
            return None, "此时段与其他牛号的记录重叠，需现场核对"
        wear, reason = self.resolve(device, stamp, raw_cow)
        if wear is None or wear.source != FILES[0]:
            return None, reason or "身份或分类未明确"
        # An outcome row keeps its own 待核对/未分类 label; the outcome note,
        # not a confirmed category, is what authorized the download.
        if wear.category == "待核对" and wear.row not in self.outcome_rows:
            return None, "分类待核对，暂不下载"
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
