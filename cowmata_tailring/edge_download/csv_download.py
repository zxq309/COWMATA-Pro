"""CSV-driven Motion/PPG/Temp download, verified cache and stable identity folders."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .core import (
    CHINA,
    MODALITIES,
    Cancelled,
    Client,
    DownloadError,
    Result,
    Target,
    checked_path,
    fingerprint,
    record_datetime,
    validate_payload,
)
from .csv_targets import CsvPlan
from .deduplication import RootSyncLock
from .local_records import LocalRecords
from .settings import atomic_json


def save_record(job, plan, kind, data):
    validate_payload(data, kind)
    stamp = record_datetime(data, kind)
    wear, reason = plan.resolve_download(data["device"], stamp, data.get("cow_id", ""))
    if wear is None:
        raise DownloadError("台账未授权下载：" + reason)
    category = wear.category
    owner = wear.identity.folder_name if wear else data["device"].upper() + "-待核对"
    folder = checked_path(
        job.farm, Path(category) / MODALITIES[kind] / stamp.strftime("%Y-%m-%d") / owner
    )
    validate_payload(data, kind)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    file = checked_path(job.farm, folder / (stamp.strftime("%Y-%m-%d_%H-%M-%S") + ".json"))
    folder.mkdir(parents=True, exist_ok=True)
    if file.exists():
        previous = file.read_bytes()
        try:
            old = json.loads(previous)
            validate_payload(old, kind)
            valid = True
        except (ValueError, TypeError, KeyError):
            valid = False
        if valid:
            if fingerprint(old, kind) == fingerprint(data, kind):
                return file, False, wear, reason
            raise DownloadError("同秒时间戳文件冲突，原件保留，未另建重复名称：" + str(file))
        backup = checked_path(
            job.farm, Path(".edge-download/recovery") / (file.name + "." + uuid.uuid4().hex)
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(previous)
        # Only replace an invalid file after preserving its exact bytes.
        repair = True
    else:
        repair = False
    fd, temporary = tempfile.mkstemp(prefix=".edge-", suffix=".partial", dir=folder)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if repair:
            if file.read_bytes() != previous:
                raise DownloadError("目标文件在核对后发生变化，已停止替换")
            os.replace(temporary, file)
        elif os.name == "nt":
            os.rename(temporary, file)
        else:
            os.link(temporary, file)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return file, True, wear, reason


def run_csv_job(
    job, cancel, log=lambda message: None, progress=lambda done, total: None, client_factory=Client
):
    plan = CsvPlan(job.ledger_directory)
    if not plan.ready:
        raise DownloadError("必须先完整核对三份现场 CSV；本轮未开始下载")
    result = Result()
    root = Path(job.farm)
    root.mkdir(parents=True, exist_ok=True)
    with RootSyncLock(root, cancel):
        state = checked_path(root, ".edge-download")
        atomic_json(
            state / "csv-download-plan.json",
            dict(
                schema="cowmata-csv-plan-3.9",
                sources=plan.sources,
                records=plan.preview(),
                issues=plan.issues,
                births=plan.births,
            ),
        )
        for issue in plan.issues:
            log(f"台账待核对 {issue['source']} 第 {issue['row']} 行：{issue['message']}")
        ranges = list(plan.bounds(job.start, job.end))
        if not ranges:
            result.pending = sum(r["eligibility"] == "pending" for r in plan.preview())
            log("本轮没有符合条件的样本；未请求原始数据。缺项补全后下轮重新核对。")
            return result
        client = client_factory(job.base_url, cancel, log)
        db = sqlite3.connect(checked_path(root, state / "csv-completed.sqlite3"))
        db.execute(
            "CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY,path TEXT,sha TEXT,ledger TEXT)"
        )
        log(
            f"已自动配置 {len(plan.by_device)} 台设备、{len(plan.wears)} 段佩戴记录；本轮 {len(ranges)} 个查询时段"
        )
        seen = set()
        try:
            local = LocalRecords(root, db, cancel, log)
            local.refresh()
            for device, lo, hi in ranges:
                while lo < hi:
                    client.check()
                    end = min(hi, lo + timedelta(days=1))
                    try:
                        items = client.listing(Target(device), lo, end, ("motion", "pulse", "temp"))
                        for kind, uid, actual_device, history_cow in items:
                            client.check()
                            key = json.dumps([job.base_url.rstrip("/"), kind, uid, actual_device])
                            if key in seen:
                                continue
                            seen.add(key)
                            try:
                                existing = local.find_uid(kind, actual_device, uid, lo, end)
                                if existing is not None:
                                    result.skipped += 1
                                    log(
                                        "已存在，跳过下载：" + existing.relative_to(root).as_posix()
                                    )
                                    progress(
                                        result.saved + result.skipped + result.failed, len(seen)
                                    )
                                    continue
                                cached = db.execute(
                                    "SELECT path,sha,ledger FROM files WHERE key=?", (key,)
                                ).fetchone()
                                data = None
                                if cached:
                                    previous = checked_path(root, cached[0])
                                    if previous.is_file():
                                        raw = previous.read_bytes()
                                        if hashlib.sha256(raw).hexdigest() == cached[1]:
                                            if local.remember(previous, kind):
                                                result.skipped += 1
                                                continue
                                            data = json.loads(raw)
                                if data is None:
                                    data = client.record(kind, uid, actual_device, history_cow)
                                actual = datetime.fromtimestamp(
                                    int(data["create_time"]) / 1000, CHINA
                                )
                                if not lo <= actual < end:
                                    raise DownloadError("详情采集时间不在请求时段")
                                wear, reason = plan.resolve_download(
                                    actual_device,
                                    actual,
                                    data.get("cow_id")
                                    or data.get("animal_number")
                                    or data.get("animalNumber")
                                    or "",
                                )
                                if wear is None:
                                    raise DownloadError("台账未授权保存：" + reason)
                                existing = local.find_data(kind, data)
                                if existing is not None:
                                    file, saved = existing, False
                                    wear, reason = plan.resolve_download(
                                        actual_device, actual, data.get("cow_id", "")
                                    )
                                else:
                                    file, saved, wear, reason = save_record(job, plan, kind, data)
                                local.remember(file, kind)
                                relative = file.relative_to(root).as_posix()
                                digest = hashlib.sha256(file.read_bytes()).hexdigest()
                                db.execute(
                                    "INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                                    (key, relative, digest, plan.fingerprint),
                                )
                                db.commit()
                                provenance = dict(
                                    path=relative,
                                    sha256=digest,
                                    sources=plan.sources,
                                    device=actual_device,
                                    uid=uid,
                                    kind=kind,
                                    reason=reason,
                                    source_row=wear.row if wear else None,
                                    source_file=wear.source if wear else None,
                                )
                                if saved:
                                    with (state / "csv-provenance.jsonl").open(
                                        "a", encoding="utf-8"
                                    ) as stream:
                                        stream.write(
                                            json.dumps(provenance, ensure_ascii=False) + "\n"
                                        )
                                result.saved += int(saved)
                                result.skipped += int(not saved)
                                result.pending += int(wear is None)
                                log(("已下载：" if saved else "已存在：") + relative)
                            except (ValueError, OSError, sqlite3.Error) as exc:
                                result.failed += 1
                                log(f"下载失败 {kind}/{device}/{uid}：{exc}")
                            progress(result.saved + result.skipped + result.failed, len(seen))
                    except DownloadError as exc:
                        result.failed += 1
                        log(f"查询失败 {device} {lo:%Y-%m-%d}：{exc}")
                    lo = end
        except Cancelled:
            result.canceled = True
        finally:
            db.close()
    return result
