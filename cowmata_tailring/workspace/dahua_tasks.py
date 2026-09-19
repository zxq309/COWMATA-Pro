"""Independent Dahua preparation jobs; originals never enter legacy deletion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from contextlib import ExitStack
from functools import wraps
from pathlib import Path

from . import organization as core
from .catalog import digest_file
from .dahua_media import (
    packet_clock,
    probe,
    recorded_clock,
    segments,
    thumbnail,
    time_ms,
    transcode,
)
from .dahua_source import DHFSReader, check, disks, normalize_file
from .data_category import category_fields, category_root
from .dataset_access import DatasetLease, overlaps
from .resource_layout import covered_days, day_at, start_stamp
from .storage import ProjectLock, atomic_json

VIEWS = tuple(f"视角{i:02}" for i in range(1, 21))
ADAPTER = "cowmata-dahua-1"


def task_root():
    return (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "COWMATA Annotator" / "dahua-tasks"
    )


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def job_file(job, name):
    job = Path(job).resolve()
    candidate = (job / name).resolve()
    if not candidate.is_relative_to(job):
        raise ValueError("任务文件路径越界")
    return candidate


def fresh_disk(saved):
    from .dahua_source import disk_info
    # Revalidate the known read-only device, not every unrelated drive per clip.
    if isinstance(saved.get("number"), int):
        try:
            actual = disk_info(saved["number"])
            if actual["identity"] == saved["identity"]:
                return actual
        except OSError:
            pass
    matches = [d for d in disks() if d["identity"] == saved["identity"]]
    if len(matches) != 1:
        raise ValueError("原磁盘未连接或磁盘身份已变化；请重新连接原盘")
    return matches[0]


def scan(request, job, cancelled=lambda: False, progress=lambda *_: None):
    job = Path(job)
    job.mkdir(parents=True, exist_ok=True)
    rows = []
    if request["mode"] == "disk":
        disk = fresh_disk(request["disk"])
        with DHFSReader(disk["path"], disk["size"], disk["identity"], cancelled) as reader:
            rows = reader.recordings()
        for row in rows:
            row.update(mode="disk", group="channel:" + row["channel"])
        snapshot = dict(adapter=ADAPTER, mode="disk", disk=disk, rows=rows)
    else:
        paths = []
        folder_inputs = []
        for item in request.get("files", []):
            source = Path(item).resolve(strict=True)
            if source.is_symlink():
                raise ValueError("原始码流来源不能是符号链接")
            if source.is_dir():
                folder_inputs.append(source)
                paths.extend(
                    p
                    for p in core.walk_files(source, cancelled)
                    if p.suffix.lower() in {".dav", ".dhav", ".h264", ".h265"}
                )
            else:
                paths.append(source)
        paths = sorted(set(paths))
        if not paths:
            raise ValueError("没有找到 DAV/DHAV 原始码流文件")
        for position, path in enumerate(paths):
            check(cancelled)
            before = core.identity(path)
            # Discovery uses file identity only. Hash selected sources in normalized().
            sha = None
            identifier = hashlib.sha256((str(path) + "|" + json.dumps(before)).encode()).hexdigest()
            row = dict(
                id=identifier,
                source=str(path),
                source_identity=before,
                sha256=sha,
                mode="file",
                group=(
                    "folder:" + str(path.parent)
                    if any(path.is_relative_to(p) for p in folder_inputs)
                    else "file:" + str(path)
                ),
                channel="待人工映射",
                stream="未知",
                status="discovered",
            )
            rows.append(row)
            progress(position + 1, len(paths), path.name)
        snapshot = dict(adapter=ADAPTER, mode="file", files=[str(p) for p in paths], rows=rows)
    atomic_json(job / "dahua-index.json", snapshot, backup=False)
    return snapshot


def group_channel(group):
    """Read an explicit channel number; never use discovery order as a camera ID."""
    if group.startswith("channel:"):
        match = re.fullmatch(r"channel:(\d+)", group)
        numbers = [int(match[1])] if match else []
    else:
        kind, _, path = group.partition(":")
        if kind not in {"folder", "file"}:
            return None
        name = Path(path).stem if kind == "file" else Path(path).name
        numbers = [
            int(n)
            for n in re.findall(
                r"(?:^|[ _-])(?:channel|ch|通道|视角)[ _-]*0*(\d+)(?=$|[ _.\-])", name, re.I
            )
        ]
    return numbers[0] if len(numbers) == 1 and 1 <= numbers[0] <= 20 else None


def automatic_mapping(groups):
    candidates = {}
    for group in groups:
        number = group_channel(group)
        if number is not None:
            candidates.setdefault(number, []).append(group)
    return {
        values[0]: VIEWS[number - 1] for number, values in candidates.items() if len(values) == 1
    }


def index_summary(index):
    groups = {}
    for row in index["rows"]:
        groups[row["group"]] = groups.get(row["group"], 0) + 1
    return dict(
        adapter=index["adapter"],
        mode=index["mode"],
        groups=groups,
        total=len(index["rows"]),
        invalid=sum(r["status"] == "invalid" for r in index["rows"]),
    )


def original_paths(index):
    return [] if index["mode"] == "disk" else [Path(p) for p in index["files"]]


_source_read_lock = threading.RLock()


def serial_source_read(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        # Raw mechanical disks keep one reader while other clips convert/verify.
        with _source_read_lock:
            return method(*args, **kwargs)
    return wrapped


def preparation_workers():
    """Queue every recorder view; raw-disk reads remain serialized, conversions overlap.

    The worker count is deliberately the recorder capacity (20), while each
    decoder is restricted to one thread. This starts all mapped views instead
    of leaving 18 views in a hidden two-worker queue.
    """
    return len(VIEWS)


@serial_source_read
def normalized(row, index, job, cancelled):
    from .dahua_run import record_folder
    folder = record_folder(job, row["id"], cancelled)
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "normalized.dav"
    saved = read_json(folder / "source.json", {})
    if saved and saved.get("source_record_id") != row["id"]:
        raise ValueError("暂存记录身份冲突，请重新扫描")
    if index["mode"] == "file":
        path = Path(row["source"])
        if core.identity(path) != row["source_identity"]:
            raise ValueError("原始码流已变化，请重新扫描")
        source_sha = digest_file(path, cancelled=cancelled)
        if core.identity(path) != row["source_identity"] or row.get("sha256") not in (
            None,
            source_sha,
        ):
            raise ValueError("原始码流已变化，请重新扫描")
        if row.get("sha256") is None:
            row.update(sha256=source_sha, status="indexed")
            atomic_json(job / "dahua-index.json", index, backup=False)
    else:
        disk = fresh_disk(index["disk"])
    if source.is_file() and saved.get("sha256") == digest_file(source, cancelled=cancelled):
        if index["mode"] == "file":
            return source, saved
        # Revalidate the selected chain even when the normalized bytes are cached.
        with DHFSReader(disk["path"], disk["size"], disk["identity"], cancelled, lazy_descriptors=True) as reader:
            part = reader.partitions[row["partition"]]
            chain, _ = reader.chain(part, row["descriptor"])
            fingerprint = hashlib.sha256(
                b"".join(reader.descriptor(part, i) for i in chain)
            ).hexdigest()
            if fingerprint != row["fingerprint"]:
                raise ValueError("原盘录像索引已变化")
        return source, saved
    temporary = folder / (uuid.uuid4().hex + ".dav")
    try:
        if index["mode"] == "disk":
            with DHFSReader(disk["path"], disk["size"], disk["identity"], cancelled, lazy_descriptors=True) as reader:
                info = reader.extract(row, temporary)
        else:
            with core.prevent_writes(Path(row["source"])):
                info = normalize_file(row["source"], temporary, cancelled)
        check(cancelled)
        os.replace(temporary, source)  # Private task cache only, never a source.
        info["source_record_id"] = row["id"]
        atomic_json(folder / "source.json", info, backup=False)
        return source, info
    finally:
        temporary.unlink(missing_ok=True)


def prepare_preview(row, index, job, cancelled=lambda: False):
    source, info = normalized(row, index, job, cancelled)
    details = probe(source, cancelled, dav=True)
    picture = source.parent / "preview.jpg"
    if not picture.is_file():
        thumbnail(source, picture, cancelled, dav=True)
    result = {**info, "image": str(picture), "video": details["video"]}
    atomic_json(source.parent / "preview.json", result, backup=False)
    return result


def video_row(prepared, view, root, reserved, cancelled=lambda: False):
    if view not in VIEWS:
        raise ValueError("归档视角必须是 01–20")
    source = Path(prepared["path"])
    root = Path(root)
    begin = prepared["start_ms"]
    end = begin + prepared["duration_ms"]
    from .farm_layout import video_root
    target = video_root(root) / day_at(begin) / view / (start_stamp(begin) + ".mp4")
    initial = target
    index = 0
    while True:
        known = reserved.get(str(target))
        if target.exists():
            known = digest_file(target, cancelled=cancelled)
        if known is None or known == prepared["sha256"]:
            break
        index += 1
        target = initial.with_name(initial.stem + f"__{index:03}" + initial.suffix)
    reserved[str(target)] = prepared["sha256"]
    return dict(
        source=str(source),
        target=str(target),
        kind="video",
        owner=view,
        status="existing" if target.exists() else "ready",
        identity=core.identity(source),
        sha256=prepared["sha256"],
        size=source.stat().st_size,
        transfer="copy",
        record_start_ms=begin,
        record_end_ms=end,
        record_date=day_at(begin),
        covered_dates=covered_days(begin, end),
        timezone_offset_minutes=480,
        metadata={**prepared["metadata"], "camera": view},
        message="MP4 已通过校验，等待归档",
    )


def select_records(index, request):
    mapping = request.get("mapping", {})
    if not mapping or any(v not in VIEWS for v in mapping.values()):
        raise ValueError("请先明确原通道到视角 01–20 的映射")
    if len(set(mapping.values())) != len(mapping.values()) and index["mode"] == "disk":
        raise ValueError("不同原通道不能映射到同一视角")
    rows = [r for r in index["rows"] if r["group"] in mapping]
    if not rows:
        raise ValueError("已选映射中没有录像")
    lo, hi = time_ms(request.get("start")), time_ms(request.get("end"))
    if lo is not None and hi is not None and hi <= lo:
        raise ValueError("结束时间必须晚于开始时间")
    if index["mode"] != "disk" or lo is None and hi is None:
        return rows
    # Include the nearest neighbouring records on each side. Actual packet
    # boundaries, not the descriptor timestamps, decide which frames are output.
    selected = []
    for group in mapping:
        ordered = sorted(
            [r for r in rows if r["group"] == group and r["status"] != "invalid"],
            key=lambda r: r["index_start_ms"],
        )
        eligible = [
            r
            for r in ordered
            if (lo is None or r["index_end_ms"] >= lo) and (hi is None or r["index_start_ms"] <= hi)
        ]
        before = [r for r in ordered if lo is not None and r["index_end_ms"] < lo]
        after = [r for r in ordered if hi is not None and r["index_start_ms"] > hi]
        selected.extend(eligible + before[-1:] + after[:1])
        selected.extend(r for r in rows if r["group"] == group and r["status"] == "invalid")
    return list({r["id"]: r for r in selected}.values())


def prepare_record(row, index, request, job, cancelled, stage=lambda *_args, **_kw: None):
    stage("read", "读取与核对原始录像")
    source, source_info = normalized(row, index, job, cancelled)
    stage("verify", "核对码流时钟")
    input_info = probe(source, cancelled, dav=True)
    clock = recorded_clock(source, input_info, cancelled) or packet_clock(source, cancelled, dav=True)
    timing = clock.get("recovery")
    input_video = input_info["video"]
    started = source_info["start_ms"]
    ended = started + round(clock["duration"] * 1000)
    bounds = segments(
        started,
        ended,
        request.get("start"),
        request.get("end"),
        request.get("split_midnight", True),
    )
    results = []
    for lo, hi in bounds:
        options = dict(
            adapter=ADAPTER,
            source_sha256=source_info["sha256"],
            lo=lo,
            hi=hi,
            profile="avc-hevc-native-clock-v5" if timing else "avc-hevc-vfr-verified-v5",
            storage_profile=request.get("storage_profile", "native"),
        )
        key = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()
        directory = source.parent / key[:16]
        directory.mkdir(exist_ok=True)
        target = directory / "prepared.mp4"
        saved = read_json(directory / "prepared.json", {})
        if saved and saved.get("options") != options:
            raise ValueError("暂存记录身份冲突，请重新扫描")
        if target.is_file() and saved.get("sha256") == digest_file(target, cancelled=cancelled):
            saved["path"] = str(target)
            if isinstance(saved.get("metadata", {}).get("timeline", {}).get("source"), dict):
                saved["metadata"]["timeline"]["source"]["path"] = str(target)
            results.append(saved)
            continue
        free = shutil.disk_usage(directory).free
        if free < max(256 * 1024**2, source.stat().st_size * 2):
            raise OSError("暂存磁盘空间不足，任务已保留，可释放空间后继续")
        temporary = directory / (uuid.uuid4().hex + ".mp4")
        try:
            stage("convert", "转换 MP4")
            converted = transcode(source, temporary, lo - started, hi - lo, cancelled, stage=stage, timing=timing, storage_profile=options["storage_profile"])
            video = converted["info"]["video"]
            if (video["width"], video["height"]) != (input_video["width"], input_video["height"]):
                raise ValueError("转码改变了原视频分辨率")
            check(cancelled)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        stage("verify", "计算成品校验值")
        sha = digest_file(target, cancelled=cancelled)
        # Video PTS remains separate from epoch time. Intervals allow browsing;
        # they never claim a verified camera clock or a Motion calibration.
        duration = converted["duration_ms"]
        timeline = converted["timeline"]
        actual_start = lo + round(float(timeline.get("firstPtsMs", 0)))
        wall = actual_start + 480 * 60000
        timeline["source"]["path"] = str(target.resolve())
        metadata = dict(
            camera="",
            codec=video["codec_name"],
            width=video["width"],
            height=video["height"],
            format=converted["info"]["format"].get("format_name"),
            duration_ms=duration,
            timeline=timeline,
            needs_review=True,
            naming_only=False,
            time_engine=ADAPTER,
            intervals=[
                dict(
                    wall_start=wall,
                    wall_end=wall + duration,
                    media_start=0,
                    media_end=duration,
                    verified=False,
                    warnings=["码流时间已恢复；相机时钟与 Motion 同步尚未人工核对"],
                )
            ],
            warnings=["码流时间不等于 Motion 已校准；暂停画面仍需核对"],
            dahua=dict(
                source_id=row["id"],
                original_source=row["source"],
                source_sha256=source_info["sha256"],
                source_identity=row["source_identity"],
                index_start_ms=row.get("index_start_ms"),
                index_end_ms=row.get("index_end_ms"),
                packet_start_ms=started,
                packet_end_ms=ended,
                packet_clock=clock,
                settings=converted["settings"],
                full_decode_verified=converted["settings"].get("verification_mode") == "full_decode",
                motion_calibrated=False,
                query_start=request.get("start"),
                query_end=request.get("end"),
            ),
        )
        if timing and timing.get("wall_clock_events"):
            metadata["warnings"].append("录像机发生校时；连续播放计时已恢复，日期时间与传感器同步需核对")
            for interval in metadata["intervals"]:
                interval["warnings"].append(metadata["warnings"][-1])
        value = dict(
            path=str(target),
            sha256=sha,
            start_ms=actual_start,
            duration_ms=duration,
            metadata=metadata,
            options=options,
        )
        atomic_json(directory / "prepared.json", value, backup=False)
        results.append(value)
    return results


def pending_video_job(target):
    """Find the farm's actual pending video job, independent of the last scan."""
    from .dataset_access import registry_root

    if not target:
        return None
    farm = Path(target).resolve()
    matches = []
    for path in (registry_root() / "pending").glob("*.json"):
        pending = read_json(path)
        if not pending or not any(overlaps(farm, p) for p in pending["paths"]):
            continue
        job = Path(pending["job"]).resolve()
        plan = read_json(job / "dahua-plan.json", {})
        if (plan.get("adapter") == ADAPTER
                and plan.get("id") == pending["task_id"]
                and Path(plan["request"]["target"]).resolve() == farm):
            matches.append(job)
    matches = list(dict.fromkeys(matches))
    if len(matches) > 1:
        raise ValueError("此牧场有多个未完成的视频任务，请核对任务记录：" + "；".join(map(str, matches)))
    return matches[0] if matches else None


def resolve_video_job(request, job):
    """Resume only an identical selection; leave the dataset lease checks intact."""
    job = Path(job).resolve()
    pending = pending_video_job(request["target"])
    if pending is None or pending == job:
        return job
    saved = read_json(pending / "dahua-plan.json")
    current = read_json(job / "dahua-index.json", {})
    previous = read_json(pending / "dahua-index.json", {})
    same = saved["request"] == request and current.get("mode") == previous.get("mode")
    if same and current.get("mode") == "disk":
        same = current["disk"]["identity"] == previous["disk"]["identity"]
    if same:
        fields = ("id", "source_identity", "fingerprint", "group")
        def selected_identity(index):
            return sorted(json.dumps({k: r.get(k) for k in fields}, sort_keys=True)
                          for r in select_records(index, request))
        same = selected_identity(current) == selected_identity(previous)
    if not same:
        raise ValueError("此牧场有未完成的视频任务，当前来源、范围或映射不同；请点击“恢复上次视频任务”继续原任务：" + str(pending))
    return pending


def organize(
    request, job, cancelled=lambda: False, progress=lambda *_: None, on_row=lambda *_: None
):
    from .catalog import file_stamp, verified_archive_matches
    from .dahua_run import RunLog, configure_storage, media_root, release_media
    from .farm_layout import storage_root, video_root
    from .resource_import import execute

    job = resolve_video_job(request, job)
    index = read_json(job / "dahua-index.json")
    if not index or index.get("adapter") != ADAPTER:
        raise ValueError("请先扫描原始录像")
    selected = select_records(index, request)
    farm = Path(request["target"]).resolve()
    category = request["category"]
    category_fields(category)
    if category == "pregnancy":
        raise ValueError("请选择孕早期、孕中期或孕晚期")
    root = category_root(farm, farm.name, category)
    reference = []
    scenario = request.get("scenario", "mixed")
    if scenario == "attach_video":
        from .resource_import import reference_motion_scope
        root, code, reference = reference_motion_scope(root)
        if code != category:
            raise ValueError("已有 Motion 类别与所选类别不一致")
    elif scenario != "mixed":
        raise ValueError("未知归类方式")
    json_sources, existing_json_sources = [], []
    for value in request.get("json_sources", []):
        source = Path(value).resolve()
        if overlaps(storage_root(root), source):
            existing_json_sources.append(str(source))
        elif source not in json_sources:
            json_sources.append(source)
    sources = original_paths(index) + json_sources
    if any(overlaps(storage_root(root), p) for p in [job, *sources]) or any(overlaps(job, p) for p in sources):
        raise ValueError("输出、原始来源和任务暂存目录必须相互独立")
    signature = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    saved = read_json(job / "dahua-plan.json", {})
    if saved and saved.get("request_sha256") != signature:
        raise ValueError("恢复任务的范围或映射已变化；请重新扫描建立新任务")
    token = saved.get("id") or uuid.uuid4().hex
    completed = dict(saved.get("completed_records", {}))
    prepared_records = dict(saved.get("prepared_records", {}))
    plan = dict(adapter=ADAPTER, id=token, request=request, request_sha256=signature,
                status="preparing", rows=[], issues=[], completed_records=completed, prepared_records=prepared_records,
                existing_json_sources=existing_json_sources)
    with ExitStack() as stack:
        destination_root = storage_root(root)
        stage = destination_root / ".归类缓存" / "原始录像" / job.name
        lease = stack.enter_context(DatasetLease(
            [destination_root, stage / "records", job / "records", *sources], "organize", owner=token))
        if index["mode"] == "disk":
            disk = fresh_disk(index["disk"])
            lockdir = task_root() / "disk-locks"
            lockdir.mkdir(parents=True, exist_ok=True)
            device_lock = ProjectLock(lockdir / (disk["identity"] + ".lock"))
            stack.callback(device_lock.close)
            if not device_lock.acquired:
                raise OSError("此原盘正在被另一归类任务读取，请稍后继续")
        configure_storage(job, destination_root)
        lease.mark_pending(token, job)
        on_row(dict(event_kind="task_resume", job=str(job)))
        log = RunLog(job, selected, request["mapping"], on_row)
        reserved, provenance = {}, []
        verified_index = {r["path"]: r for r in read_json(destination_root / "资源索引.json", {}).get("records", [])}
        active_outputs = []

        def checkpoint():
            atomic_json(job / "dahua-plan.json", plan, backup=False)

        stopping = threading.Event()

        def is_cancelled():
            log.pulse()
            return stopping.is_set() or cancelled()

        def archived(value):
            if value.get("status") == "done":
                active_outputs.append(dict(value, archive_identity=core.identity(Path(value["target"])),
                                           archive_stamp=file_stamp(Path(value["target"]))))
            log.archive(value)
            checkpoint()

        commit = dict(adapter=ADAPTER, mode="import", schema="cowmata-resources-3.4", id=token,
            target=str(root), resource_root=str(farm), farm=farm.name, farm_path=str(farm),
            sources=[dict(path=str(stage / "records"), kind="video"),
                     dict(path=str(job / "records"), kind="video")]
                    + [dict(path=str(p), kind="imu") for p in json_sources],
            category=category, note=request.get("note", ""), created_at=core.now(),
            scenario=scenario, reference_records=reference, transfer="move", delete_unusable=False,
            allow_partial=True, fast_video=True, incremental_dahua=True,
            start="", end="", rows=plan["rows"], total_files=len(selected))

        def prepare_one(row):
            log.begin(row["id"])
            if row["status"] == "invalid":
                raise ValueError(row.get("message", "索引无效"))
            prior = completed.get(row["id"])
            pending_record = prepared_records.get(row["id"])
            prepared_rows = []
            if prior or pending_record:
                receipt = prior or pending_record
                if receipt.get("source_identity") != row["source_identity"] or receipt.get("fingerprint") != row.get("fingerprint"):
                    raise ValueError("已归档记录的原始身份变化，请重新扫描")
                if index["mode"] == "file":
                    if core.identity(Path(row["source"])) != row["source_identity"]:
                        raise ValueError("原始码流已变化，请重新扫描")
                else:
                    with _source_read_lock, DHFSReader(disk["path"], disk["size"], disk["identity"], is_cancelled, lazy_descriptors=True) as reader:
                        part = reader.partitions[row["partition"]]
                        chain, _ = reader.chain(part, row["descriptor"])
                        actual = hashlib.sha256(b"".join(reader.descriptor(part, i) for i in chain)).hexdigest()
                        if actual != row["fingerprint"]:
                            raise ValueError("原盘录像索引已变化")
            if prior:
                log.stage("verify", "核对已归档成品，完整成品不重复转码", method="复用已归档")
                for output in prior["outputs"]:
                    target = Path(output["target"])
                    if not target.resolve().is_relative_to(video_root(root).resolve()):
                        raise ValueError("恢复记录的归档路径越界")
                    if not target.is_file():
                        raise ValueError("已归档视频缺失，请核对归档目录")
                    receipt = verified_index.get(target.relative_to(destination_root).as_posix(), {})
                    reuse = verified_archive_matches(target, receipt, output.get("sha256"))
                    if (core.identity(target) != output["archive_identity"]
                            or (not reuse and digest_file(target, cancelled=is_cancelled) != output["sha256"])):
                        raise ValueError("已归档视频发生变化，已保留文件并停止复用")
                    value = {**output, "status": "existing", "transfer": "move",
                             "identity": core.identity(target), "_verified_reuse": reuse}
                    prepared_rows.append(value)
            elif pending_record:
                # The complete output list is durable before the first move.
                # execute verifies target identity/hash when a move finished
                # before its receipt; unfinished parts retain their cache.
                prepared_rows = [dict(value, status="ready") for value in pending_record["outputs"]]
            else:
                values = prepare_record(row, index, request, job, is_cancelled, log.stage)
                return prior, values, True

            return prior, prepared_rows, False


        def row_stream():
            from .dahua_parallel import prepared_records as parallel_records
            preparing = parallel_records(selected, prepare_one, preparation_workers(),
                                         lambda: check(is_cancelled))
            try:
                for position, (row, future) in enumerate(preparing):
                    check(is_cancelled)
                    log.select(row["id"])
                    active_outputs.clear()
                    try:
                        prior, values, needs_planning = future.result()
                        prepared_rows = [] if needs_planning else values
                        if needs_planning:
                            for value in values:
                                result = video_row(value, request["mapping"][row["group"]], root, reserved, is_cancelled)
                                # These are our validated, derived MP4s. Move only on the
                                # destination volume; legacy C: preparations use verified copy.
                                Path(result["target"]).parent.mkdir(parents=True, exist_ok=True)
                                if Path(result["source"]).stat().st_dev == Path(result["target"]).parent.stat().st_dev:
                                    result["transfer"] = "move"
                                result["source_id"] = row["id"]
                                timeline_source = result["metadata"].get("timeline", {}).get("source")
                                if isinstance(timeline_source, dict):
                                    timeline_source["path"] = result["target"]
                                prepared_rows.append(result)
                        if not prior:
                            prepared_records[row["id"]] = dict(source_identity=row["source_identity"],
                                fingerprint=row.get("fingerprint"), outputs=prepared_rows)
                            checkpoint()
                        for result in prepared_rows:
                            result["_file_started"] = log.record_started
                            log.stage("archive", "逐段归档，成功后即可在录像目录查看",
                                      method=("复用已归档" if prior else
                                          result["metadata"].get("dahua", {}).get("settings", {}).get("encoder",
                                          result["metadata"].get("dahua", {}).get("settings", {}).get("video_processing", "已校验 MP4"))))
                            plan["rows"].append(result)
                            days = result["covered_dates"]
                            commit["start"] = min(commit["start"] or days[0], days[0])
                            commit["end"] = max(commit["end"] or days[-1], days[-1])
                            checkpoint()
                            if prior and result.pop("_verified_reuse", False):
                                reused = dict(result, status="existing", file_seconds=0,
                                              message="已归档，来源与目标身份未变化，直接复用")
                                active_outputs.append(reused)
                                log.archive(reused)
                            else:
                                yield result
                            if log.current["status"] == "blocked":
                                raise ValueError(log.current["message"])
                            provenance.append(dict(source_id=row["id"], group=row["group"],
                                target=result["target"], sha256=result["sha256"], origin=result["metadata"].get("dahua", {})))
                        completed[row["id"]] = dict(source_identity=row["source_identity"],
                            fingerprint=row.get("fingerprint"), outputs=list(active_outputs))
                        prepared_records.pop(row["id"], None)
                        plan["progress"] = position + 1
                        checkpoint()
                        release_media(job, row["id"])
                        log.finish("existing" if prior else ("done" if prepared_rows else "skipped"))
                    except InterruptedError:
                        log.finish("paused", "任务已暂停，已归档成品保留，可继续")
                        raise
                    except (OSError, ValueError, RuntimeError) as exc:
                        check(is_cancelled)
                        issue = dict(source=row["id"], original_source=row["source"], status="blocked", message=str(exc))
                        plan["issues"].append(issue)
                        # No cache growth for invalid recordings; originals remain read-only.
                        # Retain prepared pieces if any target commit needs recovery.
                        if row["id"] not in prepared_records:
                            release_media(job, row["id"])
                        log.finish("blocked", str(exc))
                    plan["progress"] = position + 1
                    checkpoint()
                    progress(position + 1, len(selected), f"已处理 {position+1}/{len(selected)} 段")
            finally:
                stopping.set()
                preparing.close()
                for source_id in list(log.active):
                    log.select(source_id)
                    log.finish("paused", "任务已暂停，已校验缓存保留，可继续")
                stopping.clear()
            if json_sources:
                from .video_intake import plan_import
                sensor_plan = plan_import(str(farm), [dict(kind="imu", path=str(p)) for p in json_sources],
                    "", None, category=category, farm=str(farm), transfer="copy",
                    delete_unusable=False, cancelled=is_cancelled)
                for value in sensor_plan["rows"]:
                    plan["rows"].append(value)
                    yield value

        checkpoint()
        try:
            def commit_progress(current, total, message):
                log.pulse()
            stream = row_stream()
            try:
                result = execute(commit, job, is_cancelled, commit_progress,
                                 on_row=archived, row_stream=stream, _lease=lease)
            finally:
                stopping.set()
                stream.close()
            attachments = root / "归类附属文件" / "大华导入" / token
            attachments.mkdir(parents=True, exist_ok=True)
            atomic_json(attachments / "来源与映射.json",
                dict(adapter=ADAPTER, request=request,
                     slots=[dict(view=v, groups=[k for k,val in request["mapping"].items() if val==v]) for v in VIEWS],
                     source_disk=index.get("disk"), records=provenance, issues=plan["issues"]), backup=False)
            plan.update(status="completed" if any(v.get("outputs") for v in completed.values()) else "no_output", result=result,
                        output=str(video_root(root)), media_root=str(media_root(job)))
            checkpoint()
            report = log.save(plan["status"])
            atomic_json(attachments / "视频任务记录.json", report, backup=False)
            shutil.copy2(job / "视频任务记录.csv", attachments / "视频任务记录.csv")
            return plan
        except BaseException as exc:
            plan["status"] = "paused" if isinstance(exc, InterruptedError) else "failed"
            checkpoint()
            log.save(plan["status"])
            lease.mark_pending(token, job)
            raise
