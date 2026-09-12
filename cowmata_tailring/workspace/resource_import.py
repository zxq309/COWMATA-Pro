"""Preview and resumable, hash-verified resource imports shared by GUI and CLI."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from datetime import time as daytime
from functools import lru_cache
from pathlib import Path

from . import organization as core
from .catalog import assert_not_being_written, digest_file, file_stamp
from .data_category import category_fields, category_root, update_context
from .dataset_access import DatasetLease
from .resource_layout import MODALITIES, TZ, covered_days, day_at, start_stamp
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
    if metadata.get("manual_readings"):
        from .clocks import manual_video_metadata
        metadata.update(manual_video_metadata(metadata, metadata["manual_readings"]))
        opening = [r for r in metadata["manual_readings"] if r["media_ms"] <= 5000]
        if opening:
            first = min(opening, key=lambda r: r["media_ms"])
            start = first["wall_ms"] - first["media_ms"]
            if any(abs(r["wall_ms"] - r["media_ms"] - start) > 2000 for r in opening):
                raise ValueError("开头人工读数互相冲突，请先核验录像时间")
            metadata["archive_time"] = dict(start_ms=start, start_verified=True,
                basis="manual_opening_reading", observations=len(opening), end_is_hint=True)
            return start, max(r["wall_end"] for r in metadata["intervals"]), False
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
        opening=[s for s in readings if 0<=s['media_ms']<=5000]
        if not opening or not metadata.get('duration_ms'):
            raise ValueError("视频内部时钟与 OCR 尚未一致确认；请在标注工具复核时间戳后重试")
        first=min(opening,key=lambda s:s['media_ms'])
        start=first['wall_ms']-first['media_ms']
        if any(abs(s['wall_ms']-s['media_ms']-start)>2000 for s in opening):
            raise ValueError('开头画面读数互相冲突，请先核验录像时间')
        metadata['archive_time']={'start_ms':start,'start_verified':False,'basis':'opening_image_ocr',
            'ocr_verified':False,'observations':len(opening)}
        return start,start+metadata['duration_ms'],False
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
        from .annotation_store import LAYOUT, dated_path
        for row in selected:
            path=Path(row['source'])
            if row['kind']!='imu' or not path.is_relative_to(parent):
                continue
            saved=dated_path(meta,path.relative_to(parent))
            destination=dated_path(root/'标注工程',Path(row['target']).relative_to(root))
            if saved and saved.is_file() and destination:
                candidates.append((saved,destination.relative_to(root/'标注工程')))
                atomic_json(root/'标注工程/annotation-layout.json',{'schema':LAYOUT})
                replacements.append((str(parent),str(root)))
        candidates += [(p, p.relative_to(meta)) for p in (meta / "video_corrections").glob("*.json") if p.stem in assets]
        if any(r['kind']=='imu' for r in selected) and (meta / "project.json").is_file():
            candidates.append((meta / "project.json", Path("project.json")))
        for source, relative in candidates:
            original = json.loads(source.read_text(encoding="utf-8"))
            value = core._rewrite(original, replacements)
            if relative.parts[0]=='video_corrections':
                owner=next((r['owner'] for r in selected if r['sha256']==source.stem and r['kind']=='video'),None)
                if owner:
                    value['camera']=owner
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


def reference_motion_scope(target):
    """Use one already classified category; never infer class from video content."""
    from cowmata_tailring.annotation.data import load_motion_json

    from .data_category import CATEGORIES
    target=core.safe_path(target)
    root=next((p for p in (target,*target.parents) if (p/'Motion').is_dir()),None)
    if root is None:
        raise ValueError('请选择已归类的具体九轴类别工程，例如产犊或怀孕/孕晚期；不要选择包含多个类别的牧场根目录')
    index_path=root/'资源索引.json'
    index=json.loads(index_path.read_text(encoding='utf-8')) if index_path.is_file() else {}
    code=index.get('dataset_category') or next((k for k,v in CATEGORIES.items() if v==root.name),None)
    if code not in CATEGORIES or code=='pregnancy':
        raise ValueError('已有九轴工程的类别或孕期尚未明确，请先完成九轴归类')
    records=[]
    if any(r.get('kind')=='imu' for r in index.get('records',[])):
        for row in index['records']:
            if row.get('kind')!='imu':
                continue
            path=core.safe_path(root/row['path'])
            if not path.is_relative_to(root):
                raise ValueError('九轴索引路径越界')
            if path.is_file() and path.stat().st_size==row.get('size'):
                records.append({**row,'covered_dates':row.get('covered_dates') or covered_days(row['record_start_ms'],row['record_end_ms']),
                    'reference_path':str(path),'identity':core.identity(path)})
    else:
        for path in core.walk_files(root/'Motion'):
            if path.suffix.lower()!='.json':
                continue
            checked=core.inspect_file(path,'imu')
            if checked['status']!='ready':
                continue
            motion=load_motion_json(path)
            lo,hi=motion.epoch_at(0),motion.epoch_at(motion.duration_ms)+1000/motion.sample_rate_hz
            records.append(dict(source=str(path),reference_path=str(path),identity=core.identity(path),kind='imu',sha256=digest_file(path),size=path.stat().st_size,
                owner=checked['folder_name'],record_start_ms=motion.epoch_at(0),
                record_end_ms=motion.epoch_at(motion.duration_ms)+1000/motion.sample_rate_hz,
                cow_id=checked.get('cow_id',''),device_id=motion.device,field_mark=checked.get('field_mark',''),
                path=path.relative_to(root).as_posix(),covered_dates=covered_days(lo,hi),
                metadata={'device':motion.device,'duration_ms':motion.duration_ms,'capture_timing':motion.capture_timing()}))
    if not records:
        raise ValueError('未找到可作为视频归类基准的九轴记录')
    return root,code,records


def material_source_groups(sources,target,scenario,cancelled):
    """Discover only relevant files once, preserving explicit camera assignments."""
    candidates={}
    for spec in sources:
        source=core.safe_path(spec['path'])
        declared=spec['kind']
        if declared not in {'imu','video','auto'}:
            raise ValueError('未知素材类型')
        if scenario=='attach_video' and declared=='imu':
            continue
        if core.overlaps(source,target) and not (
                scenario=='attach_video' or source.is_dir() and target!=source and target.is_relative_to(source)):
            raise ValueError('来源与目标不能重叠')
        for path in core.walk_files(source,cancelled):
            if target!=source and target.is_relative_to(source) and path.is_relative_to(target):
                continue
            actual='imu' if path.suffix.lower()=='.json' else 'video' if path.suffix.lower() in core.VIDEO_SUFFIXES else None
            if actual is None or declared!='auto' and actual!=declared or scenario=='attach_video' and actual!='video':
                continue
            camera=spec.get('camera') or 'auto'
            explicit=camera in core.VIEWS
            if actual=='video' and camera=='auto':
                for parent in path.parents:
                    match=re.match(r'^视角0?([1-8])(?:$|[_\- ])',parent.name)
                    if match:
                        camera=f'视角{int(match[1]):02d}'
                        break
            key=str(path)
            item={'path':str(source),'kind':actual,'camera':camera,'explicit':explicit,'files':[]}
            previous=candidates.get(key)
            if previous:
                if actual=='video' and previous['explicit'] and explicit and previous['camera']!=camera:
                    raise ValueError('同一录像被指定为不同视角，请核对来源表：'+key)
                if previous['explicit'] or not explicit:
                    continue
            candidates[key]={**item,'file':path}
    groups={}
    for item in candidates.values():
        key=(item['path'],item['kind'],item['camera'])
        groups.setdefault(key,{k:v for k,v in item.items() if k!='file'})['files'].append(item['file'])
    return list(groups.values())


def source_video_metadata(path, sha, cached, inspector):
    """Reuse content-matched indexes and the nearest authoritative human record."""
    import sqlite3

    from .catalog import bind_location_metadata, file_stamp
    from .clocks import manual_video_metadata
    from .probe import NATIVE_SIGNATURE

    metadata, correction = {}, None
    for parent in path.parents:
        meta = parent / "标注工程"
        if correction is None:
            saved = meta / "video_corrections" / (sha + ".json")
            if saved.is_file():
                value = json.loads(saved.read_text(encoding="utf-8"))
                if value.get("asset_id") == sha:
                    correction = value
        database = meta / "index.sqlite"
        if not metadata and database.is_file():
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                row = connection.execute("SELECT metadata FROM assets WHERE id=? AND kind='video'", (sha,)).fetchone()
            value = json.loads(row[0]) if row else {}
            if value.get("time_engine") == NATIVE_SIGNATURE and not value.get("native_check_pending"):
                metadata = value
    if not metadata:
        if cached.is_file():
            metadata = bind_location_metadata(json.loads(cached.read_text(encoding="utf-8")), file_stamp(path))
        else:
            metadata = inspector.video(path, sha)
    else:
        metadata = bind_location_metadata(metadata, file_stamp(path))
    if correction and correction.get("readings"):
        metadata = manual_video_metadata(metadata, correction["readings"])
        metadata["roi"] = correction.get("roi")
    return metadata


def plan_import(target, sources, start="", end=None, note="", cancelled=lambda: False,
                progress=lambda *_: None, *, category=None, farm="扬大_高邮牧场", cache=None, transfer="copy",scenario='mixed'):
    from cowmata_tailring.annotation.data import parse_motion_object

    from .device_identity import resolve_device_identity
    from .probe import SourceInspector

    if transfer not in {"copy", "move"}:
        raise ValueError("Invalid transfer mode")
    date_lo, date_hi = float('-inf'), float('inf')
    if end and not start:
        raise ValueError('请先选择起始日期，再选择结束日期')
    if start:
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end or start)
        except ValueError as exc:
            raise ValueError('日期格式须为 YYYY-MM-DD，请用日历选择') from exc
        if first > last:
            raise ValueError('结束日期不能早于起始日期')
        date_lo = datetime.combine(first, daytime(), TZ).timestamp() * 1000
        date_hi = datetime.combine(last + timedelta(days=1), daytime(), TZ).timestamp() * 1000
    resource_root = core.safe_path(target)
    farm_value = farm.strip()
    farm_path = core.safe_path(farm_value) if Path(farm_value).is_absolute() else None
    farm = farm_path.name if farm_path else core.safe_name(farm_value) if farm_value else resource_root.name
    reference=[]
    if scenario=='attach_video':
        selected_root = farm_path or resource_root
        existing_scope = next((p for p in (selected_root, *selected_root.parents) if (p/'Motion').is_dir()), None)
        scope = existing_scope or (category_root(selected_root, farm, category) if category else selected_root)
        root,existing_category,reference=reference_motion_scope(scope)
        if category and category != existing_category:
            raise ValueError('所选类别与已有九轴类别不一致，请核对牧场目录和数据类别')
        category = existing_category
        farm=root.parent.parent.name if root.parent.name=='怀孕' else root.parent.name
        reference = [r for r in reference if r['record_start_ms'] < date_hi and r['record_end_ms'] > date_lo]
        if not reference:
            raise ValueError('所选日期范围没有已归类九轴，请调整起止日期')
    elif scenario=='mixed':
        category_fields(category)
        if category == 'pregnancy':
            raise ValueError('请先选择孕期阶段：孕早期、孕中期或孕晚期。')
        root = category_root(farm_path or resource_root, farm, category)
    else:
        raise ValueError('未知整理场景')
    if not sources:
        raise ValueError("请添加九轴或视频来源；PPG 目录自动保留占位")
    token = uuid.uuid4().hex
    cache = Path(cache) if cache else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "COWMATA Annotator/resource-probes"
    cache.mkdir(parents=True, exist_ok=True)
    rows, seen, reserved = [], set(), {}
    ignored=0
    for spec in material_source_groups(sources,root,scenario,cancelled):
        source = core.safe_path(spec["path"])
        kind, camera = spec["kind"], spec.get("camera", "")
        inspector = SourceInspector(source if source.is_dir() else source.parent, cache,
                                    progress=lambda text: progress(len(rows), 0, text))
        class Stop:
            def is_set(self):
                return cancelled()
        inspector.stop = Stop()
        for path in spec['files']:
            if str(path) in seen:
                continue
            seen.add(str(path))
            row = core.inspect_file(path, kind)
            if row['status']=='skip':
                ignored+=1
                continue
            if kind=='video' and camera not in core.VIEWS:
                row.update(status='blocked',message='未识别视角编号，请添加各摄像头子目录并指定视角01至视角08')
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
                        metadata = source_video_metadata(path, sha, cached, inspector)
                        lo, hi, complete_clock = archive_bounds(path, metadata)
                        # Playback/OCR uses local calendar milliseconds, while
                        # the resource index and IMU use UTC acquisition epoch.
                        lo -= inspector.timezone_minutes * 60000
                        hi -= inspector.timezone_minutes * 60000
                        if not cached.exists():
                            atomic_json(cached, metadata)
                        owner, modality = camera, "Video"
                        row["metadata"] = {**metadata, "camera": camera}
                        if reference:
                            matched=[r for r in reference if lo < min(r['record_end_ms'], date_hi)
                                     and hi > max(r['record_start_ms'], date_lo)]
                            row['matched_imu_records']=len(matched)
                            if not matched:
                                row.update(status='skip',message='此录像没有覆盖所选九轴工程的时段，保留原处')
                                rows.append(row)
                                continue
                            row['reference_cow_ids']=sorted({r.get('cow_id','') for r in matched if r.get('cow_id')})
                    days = covered_days(lo, hi)
                    day = day_at(lo)
                    if scenario != 'attach_video' and start and (day < start or day > (end or start)):
                        raise ValueError(f"真实开始日期 {day} 超出所填日期范围；留空可自动识别")
                    filename = f"{start_stamp(lo)}{path.suffix.lower()}"
                    if kind == "video":
                        if not complete_clock:
                            row["timeline_review_required"] = True
                    destination = root / modality / day / owner / filename
                    key = os.path.normcase(str(destination))
                    row.update(target=str(destination), owner=owner, batch=day, record_date=day,
                               record_start_ms=lo, record_end_ms=hi, covered_dates=days,
                               time_basis="unix_epoch_ms", timezone_offset_minutes=480,
                               transfer="move" if transfer == "move" and core.volume(path) == core.volume(root) else "copy")
                    if destination==path:
                        row.update(status='skip',message='此文件已按规范归档，保持原位')
                    elif key in reserved:
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
                            if row['metadata'].get('archive_time',{}).get('basis')=='opening_image_ocr':
                                row['message']='按开头画面 OCR 推算起始时间归档；时间仍待复核'
                            if row['metadata'].get('archive_time',{}).get('basis')=='manual_opening_reading':
                                row['message']='按已保存的开头人工读数归档；未核验区间仍保留待核状态'
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
            "scenario":scenario,"reference_records":reference,"ignored_files":ignored,
            "transfer": transfer, "requested_start": start, "requested_end": end or '',
            "farm_path": str(farm_path) if farm_path else str(root.parent.parent if root.parent.name == '怀孕' else root.parent),
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
        for reference in plan.get('reference_records',[]):
            if core.identity(Path(reference['reference_path']))!=reference['identity']:
                raise ValueError('作为归类基准的九轴已变化，请重新预览')
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
        for reference in plan.get('reference_records',[]):
            indexed[reference['path']]={**indexed.get(reference['path'],{}),
                **{k:v for k,v in reference.items() if k not in {'identity','reference_path'}}}
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
                    from .fast_transfer import copy_verified
                    transfer_stats = copy_verified(source, temporary, row['sha256'], cancelled=cancelled,
                        progress=lambda current, total, phase: progress(current, total,
                            ('快速复制' if phase == 'copy' else '校验目标') + ' · ' + str(source.name)))
                    core.append_journal(job / 'journal.jsonl', {'phase': 'copy_verified',
                        'source': str(source), 'target': str(destination), **transfer_stats})
                    if core.identity(source) != row["identity"]:
                        raise ValueError("复制期间来源变化")
                    shutil.copystat(source, temporary)
                    core.move_no_replace(temporary, destination)
                    copied += 1
            relative = destination.relative_to(root).as_posix()
            if row.get('transfer')=='move' and source.is_relative_to(root):
                indexed.pop(source.relative_to(root).as_posix(),None)
            indexed[relative] = {"path": relative, **{k: row[k] for k in
                ("kind", "sha256", "size", "owner", "record_start_ms", "record_end_ms", "covered_dates", "metadata")},
                "time_basis": "unix_epoch_ms", "timezone_offset_minutes": row.get("timezone_offset_minutes", 480),
                "source": row["source"], "verified_stamp":file_stamp(destination),"device_id": row.get("device_id", ""),
                "cow_id": row.get("cow_id", ""), "field_mark": row.get("field_mark", "")}
            core.append_journal(job / "journal.jsonl", {"phase": "verified", "source": str(source),
                                  "target": str(destination), "sha256": row["sha256"]})
            progress(len(indexed), len(selected), str(destination))
        all_days = sorted({day for r in indexed.values() for day in r["covered_dates"]})
        for day in all_days:
            for modality in (('Video','PPG') if plan.get('scenario')=='attach_video' else MODALITIES):
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
            if plan.get('scenario')!='attach_video' and parent != root and (parent / "标注工程").is_dir() and any(
                    r.get("transfer") == "move" and Path(r["source"]).is_relative_to(parent) for r in selected):
                remaining=any(p.suffix.lower() in core.VIDEO_SUFFIXES | {'.json'} and p.name not in core.PROTECTED_NAMES | {'资源索引.json','资源迁移.json'}
                    and not p.name.endswith(('.events_meta.json','.标注.json')) and not p.is_relative_to(root)
                    for p in core.walk_files(parent,cancelled))
                if remaining:
                    continue
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
