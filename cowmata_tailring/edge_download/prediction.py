"""Primary-ledger prediction downloads, with durable per-wearing enrollment.

Preview is read-only. The download worker persists enrollment while holding
RootSyncLock, before any network request. Labels never authorize a transfer.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from cowmata_tailring.workspace.device_identity import DeviceIdentity

from .core import CHINA, DownloadError, checked_path
from .csv_targets import FILES, Wear, cow_identity, is_outcome_text, parse_time
from .settings import atomic_json
from .site_records import _write

CATEGORY = "待预测"
STATE = ".edge-download/prediction-tracking.json"
FIELDS = ("记录ID", "牛号", "设备号", "佩戴开始", "佩戴结束", "佩戴状态", "产犊状态",
          "产犊开始", "产犊结束", "九轴", "脉搏", "温度", "数据有效性", "下载状态",
          "原始数据路径", "已下载文件数", "源文件", "源记录ID", "源行号", "更新时间")


def wear_key(wear):
    value = [wear.identity.device_id, wear.identity.cow_id, wear.start.isoformat()]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def _label(row):
    begin, end = str(row.get("产犊开始", "") or "").strip(), str(row.get("产犊结束", "") or "").strip()
    if begin or end:
        try:
            first, last = parse_time(begin), parse_time(end)
            if first and last and first < last:
                return "已记录产犊起止"
        except ValueError:
            pass
        if begin == end == "/" and str(row.get("监测目的", "")).strip() in ("孕后期监测", "产后监测"):
            return "本段无产犊起止（按原台账）"
        return "产犊标注待核对"
    return "产犊情况待补录"


class PredictionPlan:
    """Only open sample rows enroll; closed rows need a prior enrollment."""

    def __init__(self, folder, root=None):
        self.folder = Path(folder)
        self.root = Path(root) if root is not None else None
        self.wears, self.issues, self.notes, self.births = [], [], [], {}
        self.sources, self.sample_rows, self.evaluations, self.by_device = {}, {}, {}, {}
        self.unparsed_samples, self.keys = [], {}
        self.ready = False
        self.verified_ranges = []
        self.fingerprint = ""
        self.entries = {}
        if self.root is not None:
            path = checked_path(self.root, STATE)
            if path.exists():
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                    if state.get("schema") != 1 or not isinstance(state.get("records"), dict):
                        raise ValueError("跟踪状态格式错误")
                    self.entries = state["records"]
                    for key, item in self.entries.items():
                        if not re.fullmatch("[a-f0-9]{64}", key) or not isinstance(item, dict):
                            raise ValueError("跟踪记录格式错误")
                        for field in ("device", "cow", "start", "folder", "status", "metadata"):
                            if field not in item:
                                raise ValueError("跟踪记录缺少字段：" + field)
                        if item["status"] not in ("ongoing", "closing", "finished", "missing", "review") or not isinstance(item["metadata"], dict):
                            raise ValueError("跟踪状态或台账元数据无效")
                except (OSError, ValueError, TypeError) as exc:
                    raise DownloadError("无法读取预测跟踪状态，原文件保留：" + str(exc)) from exc
        # Auxiliary sheets do not authorize, end or reclassify a primary record.
        path = self.folder / FILES[0]
        try:
            raw = path.read_bytes()
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
            missing = {"牛号", "设备号", "佩戴开始", "佩戴结束"} - set(reader.fieldnames or ())
            if missing:
                raise ValueError("样本台账缺少必要列：" + "、".join(sorted(missing)))
            if len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise ValueError("样本台账包含重复列名")
            rows = list(reader)
            digest = hashlib.sha256(raw).hexdigest()
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("样本台账读取期间有修改，请重试")
            self.sources[FILES[0]] = digest
            self.fingerprint = digest
        except (OSError, UnicodeError, ValueError, csv.Error) as exc:
            self.issues.append(dict(source=FILES[0], row=0, message=str(exc)))
            return
        self.ready = True
        observed = set()
        for index, row in enumerate(rows, 2):
            if row.get("已删除") == "1" or not any(row.values()):
                continue
            self.sample_rows[index] = row
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("CSV 行列数与表头不一致")
                dev = str(row.get("设备号", "") or "").strip().upper()
                if not re.fullmatch(r"[0-9A-F]{12}", dev):
                    raise ValueError("设备号必须是完整的 12 位十六进制编码")
                ear, mark = cow_identity(row.get("牛号"), row.get("现场标记", ""))
                start_text = str(row.get("佩戴开始", "") or "").strip()
                start = parse_time(start_text)
                if not start or not re.search(r"[T ]\d{1,2}:\d{2}", start_text):
                    raise ValueError("佩戴开始须填写有效的日期和时分")
                end_text = str(row.get("佩戴结束", "") or "").strip()
                end, end_issue = None, False
                if end_text:
                    try:
                        end = parse_time(end_text)
                        if end is None or not re.search(r"[T ]\d{1,2}:\d{2}", end_text) or end <= start:
                            raise ValueError()
                    except ValueError:
                        end_issue = True
                        self.issues.append(dict(source=FILES[0], row=index,
                                                message="佩戴结束不是有效时间或不晚于开始；停止该记录下载"))
                wear = Wear(DeviceIdentity(dev, ear, mark), start, end, CATEGORY, FILES[0], index,
                            True, [], end_issue=end_issue)
                for field in ("产犊开始", "产犊结束", "监测目的", "样本评价"):
                    text = str(row.get(field, "") or "").strip()
                    if is_outcome_text(text):
                        self.notes.append(dict(kind="outcome", source=FILES[0], row=index,
                            record_id=str(row.get("记录ID", "") or ""), cow=ear,
                            cow_text=str(row.get("牛号", "") or ""), device=dev, field=field,
                            text=text, message=f"{field}中的结局备注保留：{text}；未补造时间",
                            download_start=start.isoformat(), download_end=end.isoformat() if end else ""))
                key = wear_key(wear)
                self.keys[index] = key
                self.wears.append(wear)
                self.by_device.setdefault(dev, []).append(wear)
                entry = self.entries.get(key)
                if end_issue:
                    status, reason = "pending", "佩戴结束待核对"
                elif end is not None and entry is None:
                    status, reason = "excluded", "首次读取时已结束，不新建历史下载任务"
                else:
                    if entry is None:
                        entry = dict(device=dev, cow=ear, start=start.isoformat(), end="",
                                     folder=wear.identity.folder_name, status="ongoing", metadata={},
                                     first_seen=datetime.now(CHINA).isoformat(), history=[], files=[])
                        self.entries[key] = entry
                    status, reason = "eligible", "仍在佩戴，下载用于预测"
                    if end is not None:
                        status, reason = "eligible", "佩戴已结束，补齐结束前的数据"
                    self._observe(entry, wear, row)
                self.evaluations[index] = (status, reason)
                if entry is not None:
                    observed.add(key)
                    if end_issue:
                        entry["status"] = "review"
                        entry["metadata"] = dict(row)
                        entry["source_row"] = index
            except (ValueError, TypeError) as exc:
                self.issues.append(dict(source=FILES[0], row=index, message=str(exc)))
                self.evaluations[index] = ("pending", str(exc))
                self.unparsed_samples.append(dict(device=str(row.get("设备号", "") or ""),
                    cow=str(row.get("牛号", "") or ""), mark="", folder="", start=str(row.get("佩戴开始", "") or ""),
                    end=str(row.get("佩戴结束", "") or ""), category=CATEGORY, source=FILES[0], row=index,
                    warnings=str(exc), eligibility="pending", reason=str(exc)))
        for key, entry in self.entries.items():
            if key not in observed:
                entry["status"] = "missing"
        duplicates = {}
        for wear in self.wears:
            duplicates.setdefault(self.keys[wear.row], []).append(wear)
        for key, wears in duplicates.items():
            if len({(wear.end, wear.end_issue) for wear in wears}) > 1:
                reason = "同一牛号、设备和佩戴开始有矛盾的结束时间，需核对重复行"
                for wear in wears:
                    wear.end_issue = True
                    self.evaluations[wear.row] = ("pending", reason)
                    self.issues.append(dict(source=FILES[0], row=wear.row, message=reason))
                if key in self.entries:
                    self.entries[key]["status"] = "review"
        conflicts = set()
        for wears in self.by_device.values():
            for i, first in enumerate(wears):
                for second in wears[i + 1:]:
                    if self.keys[first.row] == self.keys[second.row]:
                        continue
                    if ((first.end is None or second.start < first.end)
                            and (second.end is None or first.start < second.end)):
                        conflicts.update((first.row, second.row))
        for wear in self.wears:
            if wear.row in conflicts:
                reason = "同一设备的不同佩戴记录时段重叠，暂停下载并核对归属"
                self.evaluations[wear.row] = ("pending", reason)
                self.issues.append(dict(source=FILES[0], row=wear.row, message=reason))
                entry = self.entries.get(self.keys[wear.row])
                if entry:
                    entry["status"] = "review"
        self.wears.sort(key=lambda w: (w.identity.device_id, w.start, w.row))

    def _observe(self, entry, wear, row):
        end = wear.end.isoformat() if wear.end else ""
        # Row numbers are provenance, not identities. Label-only updates must
        # never restart an already completed wearing download.
        if entry.get("metadata") != row:
            if entry.get("metadata"):
                entry.setdefault("history", []).append(dict(updated_at=entry.get("updated_at", ""),
                                                           metadata=entry["metadata"]))
            entry["metadata"] = dict(row)
            entry["updated_at"] = datetime.now(CHINA).isoformat()
        if entry.get("end") != end or entry.get("status") in ("missing", "review"):
            entry["status"] = "closing" if end else "ongoing"
        elif end and entry.get("status") != "finished":
            entry["status"] = "closing"
        entry["end"], entry["source_row"] = end, wear.row

    def eligibility(self, wear):
        return self.evaluations[wear.row]

    def record_folder(self, wear):
        entry = self.entries.get(self.keys.get(wear.row))
        return entry["folder"] if entry else wear.identity.folder_name

    def bounds(self, start, end):
        if not self.ready:
            return
        for device, wears in sorted(self.by_device.items()):
            cuts = sorted({start, end, *(max(start, min(end, t)) for w in wears for t in (w.start, w.end or end))})
            merged = []
            for lo, hi in zip(cuts, cuts[1:]):
                active = [w for w in wears if w.start <= lo and (w.end is None or lo < w.end)]
                # Multiple distinct wearings, including ineligible rows, are a
                # real identity conflict. Never guess an old wearing end.
                if len({self.keys[w.row] for w in active}) != 1:
                    continue
                eligible = [w for w in active if self.eligibility(w)[0] == "eligible"]
                if not eligible or any(w.end_issue for w in active):
                    continue
                if merged and merged[-1][1] == lo:
                    merged[-1] = (merged[-1][0], hi)
                else:
                    merged.append((lo, hi))
            for lo, hi in merged:
                yield device, lo, hi

    def resolve_download(self, device, stamp, raw_cow=""):
        active = [w for w in self.by_device.get(str(device).strip().upper(), [])
                  if w.start <= stamp and (w.end is None or stamp < w.end)]
        if not self.ready or not active or len({self.keys[w.row] for w in active}) != 1:
            return None, "核心台账未匹配或佩戴记录重叠"
        if any(w.end_issue for w in active):
            return None, "佩戴结束待核对"
        wear = active[0]
        if self.eligibility(wear)[0] != "eligible":
            return None, self.eligibility(wear)[1]
        if str(raw_cow or "").strip():
            try:
                ear, _ = cow_identity(raw_cow)
            except ValueError:
                return None, "原始数据牛号无法核对"
            if ear != wear.identity.cow_id:
                return None, "原始数据牛号与核心台账不一致"
        return wear, self.eligibility(wear)[1]

    def resolve(self, device, stamp, raw_cow=""):
        return self.resolve_download(device, stamp, raw_cow)

    def preview(self):
        result = list(self.unparsed_samples)
        for wear in self.wears:
            status, reason = self.eligibility(wear)
            entry = self.entries.get(self.keys[wear.row], {})
            result.append(dict(device=wear.identity.device_id, cow=wear.identity.cow_id,
                mark=wear.identity.field_mark, folder=self.record_folder(wear), category=CATEGORY,
                start=wear.start.isoformat(), end=wear.end.isoformat() if wear.end else "",
                source=wear.source, row=wear.row, warnings="；".join(wear.warnings),
                eligibility=status, reason=reason, record_id=self.keys[wear.row],
                tracking_state=entry.get("status", "historical")))
        return sorted(result, key=lambda r: r["row"])

    def persist(self, db=None, *, cutoff=None):
        if self.root is None or not self.ready:
            return
        from .download_status import covered, load_done
        done = load_done(db) if db is not None else {}
        for _key, entry in self.entries.items():
            lo = parse_time(entry["start"])
            hi = parse_time(entry.get("end"))
            if db is not None:
                try:
                    rows = db.execute("SELECT path,stamp FROM local_raw_records WHERE device=? AND stamp>=? ORDER BY stamp,path",
                                      (entry["device"], int(lo.timestamp()*1000))).fetchall()
                    entry["files"] = [path for path, stamp in rows if (hi is None or stamp < hi.timestamp()*1000)
                                      and Path(path).parent.name == entry["folder"]
                                      and Path(path).parts[0] == CATEGORY and (self.root/path).is_file()]
                except sqlite3.OperationalError:
                    pass
                intervals = done.get(entry["device"], [])
                if hi is not None and cutoff is not None and hi <= cutoff and entry["status"] not in ("missing", "review"):
                    entry["status"] = "finished" if covered(intervals, lo, hi) else "closing"
        atomic_json(checked_path(self.root, STATE), dict(schema=1, records=self.entries))
        self._export("预测台账.csv", list(self.entries.items()))
        self._export("待预测清单.csv", [(k, v) for k, v in self.entries.items() if v["status"] == "ongoing"])

    def _export(self, name, entries):
        path = checked_path(self.root, Path(CATEGORY) / name)
        previous, extras = {}, []
        if path.exists():
            try:
                reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig")), strict=True)
                if "记录ID" not in (reader.fieldnames or []):
                    raise ValueError("缺少记录ID")
                extras = [f for f in reader.fieldnames if f not in FIELDS]
                previous = {r["记录ID"]: r for r in reader}
            except (OSError, ValueError, csv.Error) as exc:
                raise DownloadError("预测 CSV 无法合并，保留原文件：" + str(exc)) from exc
        if name == "预测台账.csv":
            active_path = checked_path(self.root, Path(CATEGORY) / "待预测清单.csv")
            if active_path.exists():
                try:
                    reader = csv.DictReader(io.StringIO(active_path.read_text(encoding="utf-8-sig")), strict=True)
                    if "记录ID" not in (reader.fieldnames or []):
                        raise ValueError("缺少记录ID")
                    active_extras = [f for f in reader.fieldnames if f not in FIELDS]
                    extras = list(dict.fromkeys([*extras, *active_extras]))
                    for active in reader:
                        target = previous.setdefault(active["记录ID"], {})
                        for field in active_extras:
                            if not target.get(field):
                                target[field] = active.get(field, "")
                except (OSError, ValueError, csv.Error) as exc:
                    raise DownloadError("待预测 CSV 无法合并，保留原文件：" + str(exc)) from exc
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=[*FIELDS, *extras], lineterminator="\n")
        writer.writeheader()
        names = dict(ongoing="持续下载", closing="收尾补下载", finished="已完成收尾", missing="核心记录缺失待核对", review="时间待核对")
        for key, entry in sorted(entries, key=lambda kv: (kv[1]["start"], kv[1]["cow"], kv[0])):
            row = entry["metadata"]
            values = {f: previous.get(key, {}).get(f, "") for f in extras}
            validity = "、".join(f"{k}:{str(row.get(k, '') or '').strip() or '未评价'}" for k in ("九轴", "脉搏", "温度"))
            values.update(dict(zip(FIELDS, [key, row.get("牛号", entry["cow"]), entry["device"], entry["start"], entry.get("end", ""),
                "待核对" if entry["status"] in ("review", "missing") else "已结束" if entry.get("end") else "佩戴中", _label(row), row.get("产犊开始", ""), row.get("产犊结束", ""),
                row.get("九轴", ""), row.get("脉搏", ""), row.get("温度", ""), validity, names[entry["status"]],
                json.dumps(entry.get("files", []), ensure_ascii=False), len(entry.get("files", [])), FILES[0],
                row.get("记录ID", ""), entry.get("source_row", ""), entry.get("updated_at", "")])) )
            writer.writerow(values)
        # Preserve algorithm/user-added columns when an active row leaves the
        # active list: archive the previous CSV outside that generated list.
        if path.exists() and previous and set(previous) - {k for k, _ in entries}:
            raw = path.read_bytes()
            backup = checked_path(self.root, Path(".edge-download/prediction-csv-history") / (hashlib.sha256(raw).hexdigest()+"-"+name))
            if not backup.exists():
                _write(backup, raw)
        _write(path, buffer.getvalue().encode("utf-8-sig"))
