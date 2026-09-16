"""Read XLSX wear ledgers with stdlib and classify exact device/cow/time records."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from .core import CHINA, DownloadError
from .settings import atomic_json

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
CATEGORY_NAMES = (
    "未分类",
    "产犊",
    "发情",
    "怀孕",
    "怀孕/孕早期",
    "怀孕/孕中期",
    "怀孕/孕晚期",
    "疫病",
    "正常",
    "待核对",
)


def folder_signature(folder):
    folder = Path(folder)
    if not folder.is_dir():
        return ()
    return tuple(
        sorted(
            (p.name, p.stat().st_size, p.stat().st_mtime_ns)
            for p in folder.glob("*.xlsx")
            if not p.name.startswith("~$") and not p.is_symlink()
        )
    )


def _xlsx(path):
    with zipfile.ZipFile(path) as archive:
        if (
            len(archive.infolist()) > 2000
            or sum(i.file_size for i in archive.infolist()) > 64 * 1024 * 1024
        ):
            raise DownloadError("台账过大，请使用精简的xlsx文件")
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        properties = workbook.find("m:workbookPr", NS)
        epoch = (
            datetime(1904, 1, 1, tzinfo=CHINA)
            if properties is not None and properties.get("date1904") in ("1", "true")
            else datetime(1899, 12, 30, tzinfo=CHINA)
        )
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = [
                "".join(t.text or "" for t in node.iter("{" + NS["m"] + "}t"))
                for node in ET.fromstring(archive.read("xl/sharedStrings.xml"))
            ]
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            x.get("Id"): x.get("Target") for x in relationships if x.get("TargetMode") != "External"
        }
        sheets = []
        for sheet in workbook.find("m:sheets", NS):
            target = targets.get(
                sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            )
            if not target:
                continue
            part = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if ".." in PurePosixPath(part).parts:
                raise DownloadError("台账工作表路径无效")
            xml = ET.fromstring(archive.read(part))
            cells = {}
            for c in xml.findall(".//m:sheetData/m:row/m:c", NS):
                value = c.find("m:v", NS)
                text = value.text if value is not None else ""
                if c.get("t") == "s":
                    text = shared[int(text)] if text else ""
                elif c.get("t") == "inlineStr":
                    text = "".join(t.text or "" for t in c.findall(".//m:t", NS))
                elif c.get("t") not in ("str", "e", "b") and text:
                    try:
                        text = float(text) if "." in text else int(text)
                    except ValueError:
                        pass
                cells[c.get("r")] = text
            for merge in xml.findall(".//m:mergeCell", NS):
                match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", merge.get("ref", ""))
                if match and match[1] == match[3] and int(match[4]) - int(match[2]) < 10000:
                    for row in range(int(match[2]), int(match[4]) + 1):
                        cells[f"{match[1]}{row}"] = cells.get(f"{match[1]}{match[2]}", "")
            sheets.append((sheet.get("name", ""), cells))
    return sheets, epoch


def _text(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip() if value is not None else ""


def _times(value, year, months, epoch):
    if isinstance(value, int | float) and 10000 < value < 100000:
        return [epoch + timedelta(days=value)]
    text = re.sub(r"^(?:大约|约)", "", _text(value))
    try:
        parsed = datetime.fromisoformat(text)
        return [parsed.replace(tzinfo=CHINA) if parsed.tzinfo is None else parsed.astimezone(CHINA)]
    except ValueError:
        pass
    match = re.fullmatch(
        r"(?:(\d{1,2})月)?(\d{1,2})日(\d{1,2})(?:时(?:(\d{1,2})分)?(?:(\d{1,2})秒)?|:(\d{1,2})(?::(\d{1,2}))?)",
        text,
    )
    if not match:
        return []
    result = []
    for month in [int(match[1])] if match[1] else months:
        try:
            result.append(
                datetime(
                    year,
                    month,
                    int(match[2]),
                    int(match[3]),
                    int(match[4] or match[6] or 0),
                    int(match[5] or match[7] or 0),
                    tzinfo=CHINA,
                )
            )
        except ValueError:
            pass
    return result


def parse_ledger(path):
    path = Path(path)
    raw = path.read_bytes()
    sheets, epoch = _xlsx(path)
    entries = []
    for name, cells in sheets:
        if not all(
            _text(cells.get(cell)) == expected
            for cell, expected in [
                ("B3", "牛号"),
                ("D4", "设备号"),
                ("E4", "佩戴开始"),
                ("F4", "佩戴结束"),
                ("I3", "监测目的"),
            ]
        ):
            continue
        match = re.search(r"(20\d{2})年", _text(cells.get("A1")))
        if not match:
            raise DownloadError("台账标题缺少明确年份，不能猜测日期")
        year = int(match[1])
        max_row = max(int(re.search(r"\d+", key)[0]) for key in cells)
        for row in range(5, max_row + 1):
            def get(column):
                return cells.get(f"{column}{row}", "")
            purpose = _text(get("I"))
            if not purpose:
                continue
            starts = _times(get("E"), year, (), epoch)
            ends = _times(get("F"), year, (), epoch)
            start = starts[0] if len(starts) == 1 else None
            end = ends[0] if len(ends) == 1 else None
            category, reason = "待核对", "缺少有效的佩戴起止时间"
            if start and end and end > start:
                months = range(start.month, end.month + 1)
                births = _times(get("G"), year, months, epoch)
                finishes = _times(get("H"), year, months, epoch)
                pairs = [(a, b) for a in births for b in finishes if start <= a <= b <= end]
                explicit = {
                    "发情": "发情",
                    "发情监测": "发情",
                    "疫病": "疫病",
                    "疫病监测": "疫病",
                    "疾病监测": "疫病",
                    "正常": "正常",
                    "正常监测": "正常",
                    "正常对照": "正常",
                }
                if len(pairs) == 1:
                    category, reason = "产犊", "产犊时间位于该设备佩戴期间"
                elif (
                    purpose == "孕后期监测"
                    and _text(get("G")) in ("", "/")
                    and _text(get("H")) in ("", "/")
                ):
                    category, reason = "怀孕", "孕后期监测，拆除时未记录产犊"
                elif purpose == "孕后期监测" and len(births) == 1 and births[0] > end:
                    category, reason = "怀孕", "产犊开始晚于该段设备拆除"
                elif purpose in explicit:
                    category, reason = explicit[purpose], "台账明确监测类别"
                elif purpose == "产后监测":
                    reason = "产后监测不等于佩戴期间产犊，类别待核对"
                elif purpose == "难产":
                    reason = "缺少明确产犊时间，疑似难产记录待核对"
                else:
                    reason = "产犊时间缺失、格式不清或与佩戴时间冲突"
            cow = _text(get("B"))
            device = _text(get("D")).upper()
            entries.append(
                {
                    "sheet": name,
                    "row": row,
                    "cow": cow,
                    "device": device,
                    "start": start.isoformat() if start else None,
                    "end": end.isoformat() if end else None,
                    "category": category,
                    "reason": reason,
                    "purpose": purpose,
                    "source_cells": f"B{row}:O{row}",
                }
            )
    if not entries:
        raise DownloadError("未找到受支持的台账表头（牛号、设备号、佩戴开始/结束、监测目的）")
    return {
        "version": 1,
        "file": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "entries": entries,
    }


class LedgerPlan:
    def __init__(self, value=None):
        self.value = value
        self.by_device = {}
        for row in (value or {}).get("entries", []):
            self.by_device.setdefault(row["device"], []).append(row)

    def classify(self, data):
        if not self.value:
            return {"category": "未分类", "reason": "尚未导入台账"}
        device = str(data.get("device") or "").upper()
        cow = (
            str(data.get("cow_id") or data.get("animal_number") or data.get("animalNumber") or "")
            .strip()
            .casefold()
        )
        try:
            stamp = datetime.fromtimestamp(int(data["create_time"]) / 1000, CHINA)
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            return {"category": "待核对", "reason": "数据采集时间无效"}
        candidates = []
        for row in self.by_device.get(device, []):
            if not row["start"]:
                continue
            start = datetime.fromisoformat(row["start"])
            end = datetime.fromisoformat(row["end"]) if row["end"] else None
            if stamp >= start and (end is None or stamp < end):
                candidates.append(row)
        matching = [r for r in candidates if cow and r["cow"].strip().casefold() == cow]
        if len(matching) == 1 and len(candidates) == 1:
            row = matching[0]
            return dict(
                category=row["category"],
                reason=row["reason"],
                ledger=self.value["file"],
                ledger_sha256=self.value["sha256"],
                sheet=row["sheet"],
                row=row["row"],
            )
        if candidates:
            return {
                "category": "待核对",
                "reason": "台账牛号不一致或佩戴记录重叠",
                "ledger": self.value["file"],
                "ledger_sha256": self.value["sha256"],
            }
        return {"category": "未分类", "reason": "台账没有匹配的设备和采集时段"}


def load_excel_plan(folder, state, log=lambda text: None):
    folder, state = Path(folder), Path(state)
    previous = state / "ledger-plan.json"
    candidates = (
        sorted(
            (
                p
                for p in folder.glob("*.xlsx")
                if not p.name.startswith("~$") and not p.is_symlink()
            ),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
            reverse=True,
        )
        if folder.is_dir()
        else []
    )
    for path in candidates:
        try:
            fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
            if previous.exists():
                value = json.loads(previous.read_text(encoding="utf-8"))
                if value.get("sha256") == fingerprint:
                    return LedgerPlan(value)
            value = parse_ledger(path)
            atomic_json(previous, value)
            log(f"已读取台账：{path.name}，{len(value['entries'])}段佩戴记录")
            return LedgerPlan(value)
        except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
            log(f"台账暂未应用：{path.name}（{exc}），保留上次有效分类")
    if previous.exists():
        try:
            return LedgerPlan(json.loads(previous.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return LedgerPlan()


CSV_CATEGORIES = {
    "healthy": "正常",
    "estrus": "发情",
    "pregnancy": "怀孕",
    "pregnancy_early": "怀孕/孕早期",
    "pregnancy_mid": "怀孕/孕中期",
    "pregnancy_late": "怀孕/孕晚期",
    "calving": "产犊",
    "disease": "疫病",
    "review": "待核对",
    "unclassified": "未分类",
}


def csv_category(row):
    """Honor explicit uploader classification; fill missing/unclassified from purpose."""
    value = str(row.get("数据分类") or "").strip()
    category = CSV_CATEGORIES.get(value, value if value in CSV_CATEGORIES.values() else "未分类")
    if category != "未分类":
        return category
    purpose = str(row.get("监测目的") or "").strip()
    purposes = {
        "孕后期监测": "怀孕/孕晚期", "孕晚期监测": "怀孕/孕晚期",
        "孕早期监测": "怀孕/孕早期", "孕中期监测": "怀孕/孕中期",
        "产犊监测": "产犊", "产后监测": "产犊", "难产": "产犊", "死胎": "产犊",
        "正常监测": "正常", "正常对照": "正常", "发情监测": "发情",
        "疫病监测": "疫病", "疾病监测": "疫病", "怀孕监测": "怀孕",
    }
    return purposes.get(purpose, purpose if purpose in CSV_CATEGORIES.values() else "未分类")


def parse_csv_ledger(path):
    from .site_records import read_csv

    path = Path(path)
    raw = path.read_bytes()
    entries = []

    def stamp(value):
        if not value or value == "/":
            return None
        if len(value) <= 10:
            raise ValueError("佩戴时间缺少时分")
        result = datetime.fromisoformat(value)
        return result.replace(tzinfo=CHINA) if result.tzinfo is None else result.astimezone(CHINA)

    for index, row in enumerate(read_csv(raw, "samples"), 2):
        if row["已删除"] == "1":
            continue
        reason, category = "上传器台账明确数据分类", csv_category(row)
        try:
            start, end = stamp(row["佩戴开始"]), stamp(row["佩戴结束"])
            if not start or (end is not None and end <= start):
                raise ValueError("佩戴起止时间无效")
        except ValueError:
            # Keep a known start with uncertain end as a review interval, never guess an end.
            try:
                start = stamp(row["佩戴开始"])
            except ValueError:
                start = None
            end, category, reason = None, "待核对", "台账佩戴起止时间需要核对"
        if row["核对提示"].strip():
            category, reason = "待核对", "台账核对提示：" + row["核对提示"]
        device = row["设备号"].upper().replace(":", "").replace("-", "").strip()
        if not re.fullmatch(r"[0-9A-F]{12}", device):
            continue
        entries.append(
            dict(
                sheet="样本试验台账",
                row=index,
                record_id=row["记录ID"],
                cow=row["牛号"].strip(),
                device=device,
                start=start.isoformat() if start else None,
                end=end.isoformat() if end else None,
                category=category,
                reason=reason,
                purpose=row["监测目的"],
                source_cells="CSV:" + str(index),
            )
        )
    return dict(
        version=2,
        file=path.name,
        sha256=hashlib.sha256(raw).hexdigest(),
        source_directory=str(path.parent.resolve()),
        entries=entries,
    )


def load_plan(folder, state, log=lambda text: None):
    folder, state = Path(folder), Path(state)
    path = folder / "样本试验台账.csv"
    previous = state / "csv-ledger-plan.json"
    if path.exists():
        try:
            if path.is_symlink():
                raise ValueError("台账路径是链接")
            value = parse_csv_ledger(path)
            atomic_json(previous, value)
            log(f"已应用上传器 CSV 台账：{len(value['entries'])} 段有效/待核对佩戴记录")
            return LedgerPlan(value)
        except (OSError, ValueError) as exc:
            log(f"CSV 台账暂未应用：{exc}；保留同目录上次核验的分类")
    if previous.exists():
        try:
            value = json.loads(previous.read_text(encoding="utf-8"))
            if value.get("source_directory") == str(folder.resolve()):
                return LedgerPlan(value)
        except (OSError, ValueError):
            pass
    return load_excel_plan(folder, state, log)
