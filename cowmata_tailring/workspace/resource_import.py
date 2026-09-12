"""Preview and resumable, hash-verified resource imports shared by GUI and CLI."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import time
import uuid
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path

from . import organization as core
from .catalog import assert_not_being_written, digest_file
from .data_category import category_fields, category_root, update_context
from .dataset_access import DatasetLease
from .resource_layout import MODALITIES, covered_days, day_at, stamp_at
from .storage import atomic_json


@lru_cache(maxsize=8)
def zero_digest(size):
    sha = hashlib.sha256()
    block = bytes(min(size, 4 * 1024 * 1024))
    remaining = size
    while remaining:
        count = min(remaining, len(block))
        sha.update(block[:count])
        remaining -= count
    return sha.hexdigest()


def verified_source_digest(path, cache, cancelled):
    """Reuse a full-read digest only while the original file identity matches."""
    before = core.identity(path)
    saved = cache / (hashlib.sha256(str(path).encode()).hexdigest() + ".source.json")
    try:
        record = json.loads(saved.read_text(encoding="utf-8"))
        sha = record.get("sha256", "")
        if record.get("identity") == before and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha):
            return sha
    except (OSError, ValueError):
        pass
    sha = digest_file(path, cancelled=cancelled)
    if core.identity(path) != before:
        raise ValueError("Source changed during hashing")
    atomic_json(saved, {"identity": before, "sha256": sha})
    return sha


def archive_bounds(path, metadata):
    """Prefer the native start for naming; retain OCR status for evidence."""
    spans = metadata.get("intervals", [])
    native = metadata.get("timeline", {}).get("native")
    readings = [s for s in metadata.get("samples", []) if s.get("wall_ms") is not None]
    if (native and readings and len(spans) == 1 and
            abs(spans[0]["wall_start"]-native["wall_start"]) < 1 and
            abs(spans[0]["wall_end"]-native["wall_start"]-native["duration_ms"]) < 1 and
            all(abs(s["wall_ms"]-s["media_ms"]-native["wall_start"]) <= 2000 for s in readings)):
        # Two independent clocks (packet clock and image OCR), not two
        # repeated scans. Full packet continuity was already validated.
        metadata["needs_review"] = False
        spans[0]["verified"] = True
        spans[0]["warnings"] = []
        metadata["warnings"] = []
        metadata["clock_cross_check"] = {"basis": "native_packets+image_ocr", "observations": len(readings)}
    if spans and not metadata.get("needs_review") and all(s.get("verified") for s in spans):
        start = min(s["wall_start"] for s in spans)
        end = max(s["wall_end"] for s in spans)
        if native:
            start = native["wall_start"]
        else:
            from cowmata_tailring.media.native_ps import native_hint
            hint = native_hint(path)
            if hint and hint.get("start_ms") is not None:
                if abs(hint["start_ms"] - start) > 2000:
                    raise ValueError("视频开始时间未通过流内时钟与画面读数复核")
                start = hint["start_ms"]
        return start, end, True
    opening = [s for s in readings if s["media_ms"] <= 5000]
    if native and all(abs(s["wall_ms"]-s["media_ms"]-native["wall_start"]) <= 2000 for s in opening):
        # User-selected policy: complete packet clocks may route an archive,
        # with an explicit pending name. This NEVER verifies evidence intervals.
        start = native["wall_start"]
        metadata["archive_time"] = {"start_verified": False, "start_ms": start,
                                   "basis": "native_packet_clock", "ocr_verified": False,
                                   "observations": len(opening),
                                   "later_clock_conflicts": sum(abs(s["wall_ms"]-s["media_ms"]-start)>2000 for s in readings)}
        return start, start + native["duration_ms"], False
    from cowmata_tailring.media.native_ps import native_hint
    hint = native_hint(path)
    if not hint or hint.get("start_ms") is None:
        raise ValueError("视频内部时钟与 OCR 尚未一致确认；请在标注工具复核时间戳后重试")
    segments = metadata.get("timeline", {}).get("segments", [])
    limit = min(5001, segments[0]["publicEndMs"]) if segments else 5001
    samples = [s for s in metadata.get("samples", []) if s.get("wall_ms") is not None and s["media_ms"] < limit]
    if not samples:
        metadata["archive_time"] = {"start_verified": False, "start_ms": hint["start_ms"],
                                   "basis": "native_first_frame", "ocr_verified": False,
                                   "observations": 0, "end_is_hint": True}
        endpoint = hint["end_ms"]-2000 if hint.get("end_ms") is not None else hint["start_ms"]+metadata.get("duration_ms", 1)
        return hint["start_ms"], max(hint["start_ms"]+1, endpoint), False
    if not samples or any(abs(s["wall_ms"]-s["media_ms"]-hint["start_ms"]) > 2000 for s in samples):
        raise ValueError("视频开始时间未通过流内时钟与画面读数复核")
    end = hint.get("end_ms")
    if end is not None:
        end -= 2000
    else:
        observed = [s for s in metadata.get("samples", []) if s.get("wall_ms") is not None]
        last = max(observed, key=lambda s: s["media_ms"], default=None)
        remainder = metadata.get("duration_ms", 0) - last["media_ms"] if last else float("inf")
        if not last or not 0 <= remainder <= 1500:
            raise ValueError("Video end date has not been observed")
        end = last["wall_ms"] + remainder
    # The endpoint is a routing bound, not a confirmed evidence interval. Do
    # not include it in the filename or change metadata.needs_review.
    metadata["archive_time"] = {"start_verified": True, "start_ms": hint["start_ms"],
                                "end_is_hint": True, "observations": len(samples)}
    return hint["start_ms"], max(hint["start_ms"]+1, end), False


def preserve_annotation_work(root, selected, job):
    """Relink human work by SHA; retain conflicting snapshots in the task log."""
    import hashlib
    import re
    assets = {r["sha256"] for r in selected}
    parents = {p for r in selected for p in Path(r["source"]).parents if (p / "标注工程").is_dir()}
    preserved, conflicts = 0, []
    for parent in sorted(parents):
        replacements = []
        for row in selected:
            source = Path(row["source"])
            if not source.is_relative_to(parent):
                continue
            relative = Path(row["target"]).relative_to(root).as_posix()
            replacements.extend([(str(source), str(Path(row["target"]))), (source.relative_to(parent).as_posix(), relative)])
            if row["kind"] == "video":
                replacements.extend((part, row["owner"]) for part in source.parts if re.match(r"^视角\d", part))
        meta = parent / "标注工程"
        candidates = [(p, p.relative_to(meta)) for p in (meta / "annotations").glob("*.json") if p.stem in assets]
        candidates += [(p, p.relative_to(meta)) for p in (meta / "video_corrections").glob("*.json") if p.stem in assets]
        if (meta / "project.json").is_file():
            candidates.append((meta / "project.json", Path("project.json")))
        for source, relative in candidates:
            original = json.loads(source.read_text(encoding="utf-8"))
            value = core._rewrite(original, replacements)
            destination = root / "标注工程" / relative
            if destination.exists():
                if json.loads(destination.read_text(encoding="utf-8")) == value:
                    continue
                backup = job / "历史标注冲突" / hashlib.sha256(str(parent).encode()).hexdigest()[:12] / relative
                atomic_json(backup, original)
                conflicts.append(str(backup))
                continue
            atomic_json(destination, value)
            preserved += 1
    return {"preserved": preserved, "conflicts": conflicts}


def plan_import(target, sources, start="", end=None, note="", cancelled=lambda: False,
                progress=lambda *_: None, *, category=None, farm="扬大_高邮牧场", cache=None, transfer="copy"):
    from cowmata_tailring.annotation.data import parse_motion_object

    from .device_identity import resolve_device_identity
    from .probe import SourceInspector

    category_fields(category)
    if transfer not in {"copy", "move"}:
        raise ValueError("Invalid transfer mode")
    resource_root = core.safe_path(target)
    farm = core.safe_name(farm.strip())
    root = category_root(resource_root, farm, category)
    if not sources:
        raise ValueError("请添加九轴或视频来源；PPG 目录自动保留占位")
    token = uuid.uuid4().hex
    cache = Path(cache) if cache else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "COWMATA Annotator/resource-probes"
    cache.mkdir(parents=True, exist_ok=True)
    rows, seen, reserved = [], set(), {}
    for spec in sources:
        source = core.safe_path(spec["path"])
        if core.overlaps(source, root):
            raise ValueError("来源与目标不能重叠")
        kind, camera = spec["kind"], spec.get("camera", "")
        if kind not in {"imu", "video"} or kind == "video" and camera not in core.VIEWS:
            raise ValueError("来源须为九轴或视角01至视角08；PPG 暂留占位")
        inspector = SourceInspector(source if source.is_dir() else source.parent, cache,
                                    progress=lambda text: progress(len(rows), 0, text))
        class Stop:
            def is_set(self):
                return cancelled()
        inspector.stop = Stop()
        for path in core.walk_files(source, cancelled):
            if str(path) in seen:
                raise ValueError("来源重复：" + str(path))
            seen.add(str(path))
            row = core.inspect_file(path, kind)
            if row["status"] == "ready":
                try:
                    sha = verified_source_digest(path, cache, cancelled)
                    row["sha256"] = sha
                    if kind == "imu":
                        obj = json.loads(path.read_text(encoding="utf-8-sig"))
                        motion = parse_motion_object(obj, source_path=path)
                        row.update(resolve_device_identity(path, motion.device))
                        timing = motion.capture_timing()
                        lo = timing["sample_start_epoch_ms"]
                        hi = timing["sample_end_epoch_ms"] + 1000 / motion.sample_rate_hz
                        owner, modality = row["folder_name"], "Motion"
                        row["metadata"] = inspector(path, "imu", sha)
                    else:
                        with path.open("rb") as stream:
                            zero_header = not any(stream.read(1024))
                        if zero_header and sha == zero_digest(row["size"]):
                            raise ValueError("视频全为零字节，仅为空白占位，需补齐原始录像")
                        progress(len(rows), 0, "内部时间戳 + OCR 复核：" + str(path))
                        cached = cache / (sha + ".probe.json")
                        metadata = json.loads(cached.read_text(encoding="utf-8")) if cached.is_file() else inspector.video(path, sha)
                        lo, hi, complete_clock = archive_bounds(path, metadata)
                        # Playback/OCR uses local calendar milliseconds, while
                        # the resource index and IMU use UTC acquisition epoch.
                        lo -= inspector.timezone_minutes * 60000
                        hi -= inspector.timezone_minutes * 60000
                        if not cached.exists():
                            atomic_json(cached, metadata)
                        owner, modality = camera, "Video"
                        row["metadata"] = {**metadata, "camera": camera}
                    days = covered_days(lo, hi)
                    day = day_at(lo)
                    if start and (day < start or day > (end or start)):
                        raise ValueError(f"真实开始日期 {day} 超出所填日期范围；留空可自动识别")
                    filename = f"{stamp_at(lo)}__{stamp_at(hi)}__{sha[:12]}{path.suffix.lower()}"
                    if kind == "video":
                        filename = f"{stamp_at(lo)[:19]}{path.suffix.lower()}"
                        if not complete_clock:
                            row["timeline_review_required"] = True
                    destination = root / modality / day / owner / filename
                    key = os.path.normcase(str(destination))
                    row.update(target=str(destination), owner=owner, batch=day, record_date=day,
                               record_start_ms=lo, record_end_ms=hi, covered_dates=days,
                               time_basis="unix_epoch_ms", timezone_offset_minutes=480,
                               transfer="move" if transfer == "move" and core.volume(path) == core.volume(root) else "copy")
                    if key in reserved:
                        if reserved[key] != sha:
                            raise ValueError("目标同名但内容不同，停止覆盖")
                        row.update(status="skip", message="本批重复内容，已有同身份目标")
                    elif destination.exists():
                        if digest_file(destination, cancelled=cancelled) != sha:
                            raise ValueError("目标同名但内容不同，停止覆盖")
                        row.update(status="existing", message="相同内容已归档，无需再次复制")
                    else:
                        row.update(status="ready", message="采集时间已确认；跨日按真实覆盖索引，复制后再次校验 SHA-256")
                        if row.get("timeline_review_required"):
                            row["message"] = "开始时间已由流内时钟和 OCR 核实；部分播放区间时间待复核"
                            if row["metadata"].get("archive_time", {}).get("ocr_verified") is False:
                                row["message"] = "按流内首帧时间归档；OCR 未读清，时间待复核，未确认区间不能保存已核验证据"
                    reserved[key] = sha
                    if core.identity(path) != row["identity"]:
                        raise ValueError("审查期间源文件变化")
                except (ValueError, OSError, RuntimeError) as exc:
                    row.update(status="blocked", message=str(exc))
            rows.append(row)
            progress(len(rows), 0, str(path))
    core.check_identity_ambiguity(rows)
    dated = [r for r in rows if r.get("covered_dates")]
    return {"mode": "import", "schema": "cowmata-resources-3.4", "id": token,
            "target": str(root), "resource_root": str(resource_root), "farm": farm,
            "sources": sources, "category": category, "note": note, "created_at": core.now(),
            "transfer": transfer,
            "start": min((r["record_date"] for r in dated), default=start),
            "end": max((r["covered_dates"][-1] for r in dated), default=end or start), "rows": rows}


def execute(plan, job, cancelled=lambda: False, progress=lambda *_: None):
    started = time.monotonic()
    root, job = core.safe_path(plan["target"]), core.safe_path(job)
    selected = [r for r in plan["rows"] if r["status"] in {"ready", "existing"}]
    unresolved = [r for r in plan["rows"] if r["status"] in {"blocked", "invalid"}]
    if not selected or unresolved and not plan.get("allow_partial"):
        raise ValueError("无有效素材或存在待核实项，请先核对报告")
    sources = [core.safe_path(s["path"]) for s in plan["sources"]]
    if any(core.overlaps(job, p) for p in [root, *sources]):
        raise ValueError("任务记录需位于素材目录之外")
    job.mkdir(parents=True, exist_ok=True)
    copied, moved = 0, 0
    with DatasetLease([root, *sources], "organize", owner=plan["id"]) as lease, ExitStack() as locks:
        # Also honor installed 3.3 clients which only hold writer.lock.
        parents = {p for value in [root, *sources, *(Path(r["source"]) for r in selected)] for p in (value, *value.parents)}
        for parent in parents:
            if (parent / "标注工程").is_dir():
                lock = core.ProjectLock(parent / "标注工程/writer.lock")
                locks.callback(lock.close)
                if not lock.acquired:
                    raise OSError("相关工程仍在标注，请保存并暂停后再整理")
        root.mkdir(parents=True, exist_ok=True)
        index_path = root / "资源索引.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {"schema": "cowmata-resources-3.4", "records": []}
        indexed = {r["path"]: r for r in index["records"]}
        for row in selected:
            core.check_cancel(cancelled)
            source, destination = core.safe_path(row["source"]), core.safe_path(row["target"])
            if not destination.is_relative_to(root) or not any(source == s or source.is_relative_to(s) for s in sources):
                raise ValueError("计划路径越界")
            if not source.exists() and row.get("transfer") == "move" and destination.exists():
                if core.identity(destination) != row["identity"] or digest_file(destination, cancelled=cancelled) != row["sha256"]:
                    raise ValueError("Moved target identity does not match the saved plan")
                source_present = False
            else:
                source_present = True
            if source == destination or source_present and core.identity(source) != row["identity"]:
                raise ValueError("来源已变化，请重新预览")
            if source_present:
                assert_not_being_written(source)
            lease.mark_pending(plan["id"], job)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + "." + plan["id"] + ".partial")
            with core.prevent_writes(source if source_present else destination):
                if destination.exists():
                    if digest_file(destination, cancelled=cancelled) != row["sha256"]:
                        raise ValueError("已存在目标校验失败：" + str(destination))
                elif row.get("transfer") == "move":
                    core.append_journal(job / "journal.jsonl", {"phase": "move_intent", **row})
                    core.move_no_replace(source, destination)
                    if core.identity(destination) != row["identity"]:
                        raise ValueError("Moved file identity changed")
                    moved += 1
                else:
                    # The task-owned partial is safe to restart after cancellation.
                    with source.open("rb") as inp, temporary.open("wb") as out:
                        while block := inp.read(4 * 1024 * 1024):
                            core.check_cancel(cancelled)
                            out.write(block)
                        out.flush()
                        os.fsync(out.fileno())
                    if digest_file(temporary, cancelled=cancelled) != row["sha256"]:
                        raise ValueError("复制后 SHA-256 不一致，保留原件与任务记录")
                    if core.identity(source) != row["identity"]:
                        raise ValueError("复制期间来源变化")
                    shutil.copystat(source, temporary)
                    core.move_no_replace(temporary, destination)
                    copied += 1
            relative = destination.relative_to(root).as_posix()
            indexed[relative] = {"path": relative, **{k: row[k] for k in
                ("kind", "sha256", "size", "owner", "record_start_ms", "record_end_ms", "covered_dates", "metadata")},
                "time_basis": "unix_epoch_ms", "timezone_offset_minutes": row.get("timezone_offset_minutes", 480),
                "source": row["source"], "device_id": row.get("device_id", ""),
                "cow_id": row.get("cow_id", ""), "field_mark": row.get("field_mark", "")}
            core.append_journal(job / "journal.jsonl", {"phase": "verified", "source": str(source),
                                  "target": str(destination), "sha256": row["sha256"]})
            progress(len(indexed), len(selected), str(destination))
        all_days = sorted({day for r in indexed.values() for day in r["covered_dates"]})
        for day in all_days:
            for modality in MODALITIES:
                (root / modality / day).mkdir(parents=True, exist_ok=True)
            for view in core.VIEWS:
                (root / "Video" / day / view).mkdir(exist_ok=True)
            (root / "PPG" / day / "占位说明.txt").write_text("PPG 预留：后续接入原始采集时间、通道和标注；当前无有效 PPG 数据。\n", encoding="utf-8")
        index.update(records=list(indexed.values()), dates=all_days, farm=plan["farm"],
                     **category_fields(plan["category"]), updated_at=core.now(),
                     ppg={"status": "reserved", "available": False})
        atomic_json(index_path, index)
        context_plan = {**plan, "rows": [{**r, "status": "ready"} for r in selected]}
        update_context(root, context_plan)
        history = preserve_annotation_work(root, selected, job)
        for parent in parents:
            if parent != root and (parent / "标注工程").is_dir() and any(
                    r.get("transfer") == "move" and Path(r["source"]).is_relative_to(parent) for r in selected):
                atomic_json(parent / "资源迁移.json", {"schema": "cowmata-relocation-3.4", "target": str(root), "task": str(job)})
        report = root / "整理异常.csv"
        prior_exceptions = {}
        if report.exists():
            with report.open(encoding="utf-8-sig", newline="") as stream:
                prior_exceptions = {r["source"]: r for r in csv.DictReader(stream)}
        for row in selected:
            prior_exceptions.pop(row["source"], None)
        prior_exceptions.update({r["source"]: r for r in unresolved})
        with report.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["source", "status", "message"], extrasaction="ignore")
            writer.writeheader()
            writer.writerows(prior_exceptions.values())
        with (root / "时间复核清单.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["path", "source", "sha256", "record_start_ms"], extrasaction="ignore")
            writer.writeheader()
            writer.writerows(r for r in indexed.values() if r["kind"] == "video" and r["metadata"].get("needs_review"))
        lease.complete(plan["id"])
    return {**plan, "completed": True, "moved": moved+copied, "same_volume_moved": moved, "copied": copied,
            "existing": len(selected)-copied-moved, "unresolved": len(unresolved),
            "history": history,
            "seconds": round(time.monotonic()-started, 3), "identity_method": "sha256",
            "finished_at": core.now()}
