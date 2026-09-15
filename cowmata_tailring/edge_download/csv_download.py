"""CSV-driven Motion/PPG/Temp download, verified cache and stable identity folders."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .core import CHINA, MODALITIES, Cancelled, Client, DownloadError, Result, Target, checked_path
from .csv_targets import CsvPlan
from .deduplication import RootSyncLock
from .settings import atomic_json


def save_record(job, plan, kind, data):
    stamp = datetime.fromtimestamp(int(data["create_time"]) / 1000, CHINA)
    wear, reason = plan.resolve(data["device"], stamp, data.get("cow_id", ""))
    category = wear.category if wear else "待核对"
    owner = wear.identity.folder_name if wear else data["device"].upper() + "-待核对"
    folder = checked_path(
        job.farm, Path(category) / MODALITIES[kind] / stamp.strftime("%Y-%m-%d") / owner
    )
    # Empty modalities make the same category directly selectable as a project.
    for modality in ("Motion", "PPG", "Temp", "Video"):
        checked_path(job.farm, Path(category) / modality).mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    file = checked_path(
        job.farm,
        folder / (stamp.strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3] + "_" + digest[:12] + ".json"),
    )
    if file.is_file() and file.read_bytes() == raw:
        return file, False, wear, reason
    folder.mkdir(parents=True, exist_ok=True)
    if file.exists():
        # Retain a damaged local copy for review; never silently overwrite it.
        backup = checked_path(
            job.farm,
            Path(".edge-download/recovery")
            / (file.name + "." + datetime.now(CHINA).strftime("%Y%m%d%H%M%S%f")),
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(file.read_bytes())
    temporary = checked_path(job.farm, file.with_suffix(".partial"))
    temporary.write_bytes(raw)
    temporary.replace(file)
    return file, True, wear, reason


def run_csv_job(
    job, cancel, log=lambda message: None, progress=lambda done, total: None, client_factory=Client
):
    plan = CsvPlan(job.ledger_directory)
    if not plan.wears:
        raise DownloadError("未能从现场 CSV 读取有效佩戴记录；请先刷新 CSV 并查看台账核对清单")
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
        client = client_factory(job.base_url, cancel, log)
        db = sqlite3.connect(checked_path(root, state / "csv-completed.sqlite3"))
        db.execute(
            "CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY,path TEXT,sha TEXT,ledger TEXT)"
        )
        ranges = list(plan.bounds(job.start, job.end))
        log(
            f"已自动配置 {len(plan.by_device)} 台设备、{len(plan.wears)} 段佩戴记录；本轮 {len(ranges)} 个查询时段"
        )
        seen = set()
        try:
            for device, lo, hi in ranges:
                while lo < hi:
                    client.check()
                    end = min(hi, lo + timedelta(days=1))
                    try:
                        items = client.listing(Target(device), lo, end, job.kinds)
                        for kind, uid, actual_device, history_cow in items:
                            client.check()
                            key = json.dumps([job.base_url.rstrip("/"), kind, uid, actual_device])
                            if key in seen:
                                continue
                            seen.add(key)
                            try:
                                cached = db.execute(
                                    "SELECT path,sha,ledger FROM files WHERE key=?", (key,)
                                ).fetchone()
                                data = None
                                if cached:
                                    previous = checked_path(root, cached[0])
                                    if previous.is_file():
                                        raw = previous.read_bytes()
                                        if hashlib.sha256(raw).hexdigest() == cached[1]:
                                            if cached[2] == plan.fingerprint:
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
                                file, saved, wear, reason = save_record(job, plan, kind, data)
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
