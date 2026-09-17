"""Independent Dahua preparation jobs; originals never enter legacy deletion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from contextlib import ExitStack
from pathlib import Path

from . import organization as core
from .catalog import digest_file
from .dahua_media import packet_clock, probe, segments, thumbnail, time_ms, transcode
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


def normalized(row, index, job, cancelled):
    folder = job_file(job, "records/" + row["id"][:24])
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
        with DHFSReader(disk["path"], disk["size"], disk["identity"], cancelled) as reader:
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
            with DHFSReader(disk["path"], disk["size"], disk["identity"], cancelled) as reader:
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
    target = root / "Video" / day_at(begin) / view / (start_stamp(begin) + ".mp4")
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
        message="MP4 已完整解码，等待归档",
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


def prepare_record(row, index, request, job, cancelled):
    source, source_info = normalized(row, index, job, cancelled)
    clock = packet_clock(source, cancelled, dav=True)
    input_video = probe(source, cancelled, dav=True)["video"]
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
            profile="h264-vfr-crf23-v2",
        )
        key = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()
        directory = source.parent / key[:16]
        directory.mkdir(exist_ok=True)
        target = directory / "prepared.mp4"
        saved = read_json(directory / "prepared.json", {})
        if saved and saved.get("options") != options:
            raise ValueError("暂存记录身份冲突，请重新扫描")
        if target.is_file() and saved.get("sha256") == digest_file(target, cancelled=cancelled):
            results.append(saved)
            continue
        free = shutil.disk_usage(directory).free
        if free < max(256 * 1024**2, source.stat().st_size * 2):
            raise OSError("暂存磁盘空间不足，任务已保留，可释放空间后继续")
        temporary = directory / (uuid.uuid4().hex + ".mp4")
        try:
            converted = transcode(source, temporary, lo - started, hi - lo, cancelled)
            video = converted["info"]["video"]
            if (video["width"], video["height"]) != (input_video["width"], input_video["height"]):
                raise ValueError("转码改变了原视频分辨率")
            check(cancelled)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
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
            codec="h264",
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
                full_decode_verified=True,
                motion_calibrated=False,
                query_start=request.get("start"),
                query_end=request.get("end"),
            ),
        )
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


def organize(
    request, job, cancelled=lambda: False, progress=lambda *_: None, on_row=lambda *_: None
):
    from .resource_import import execute

    index = read_json(Path(job) / "dahua-index.json")
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
    job = Path(job).resolve()
    # JSON already in (or containing) the destination is existing farm data,
    # not an external import source. In particular the former UI allowed users
    # to select the output farm itself. Preserve it without scanning/reimporting.
    json_sources = []
    existing_json_sources = []
    for value in request.get("json_sources", []):
        source = Path(value).resolve()
        if overlaps(root, source):
            existing_json_sources.append(str(source))
        elif source not in json_sources:
            json_sources.append(source)
    sources = original_paths(index) + json_sources
    if any(overlaps(root, p) for p in [job, *sources]) or any(overlaps(job, p) for p in sources):
        raise ValueError("输出、原始来源和任务暂存目录必须相互独立")
    signature = hashlib.sha256(
        json.dumps(request, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    saved = read_json(job / "dahua-plan.json", {})
    if saved and saved.get("request_sha256") != signature:
        raise ValueError("恢复任务的范围或映射已变化；请重新扫描建立新任务")
    token = saved.get("id") or uuid.uuid4().hex
    plan = dict(
        adapter=ADAPTER,
        id=token,
        request=request,
        request_sha256=signature,
        status="preparing",
        rows=[],
        existing_json_sources=existing_json_sources,
    )
    atomic_json(job / "dahua-plan.json", plan, backup=False)
    with ExitStack() as stack:
        lease = stack.enter_context(
            DatasetLease([root, job / "records", *sources], "organize", owner=token)
        )
        if index["mode"] == "disk":
            disk = fresh_disk(index["disk"])
            lockdir = task_root() / "disk-locks"
            lockdir.mkdir(parents=True, exist_ok=True)
            device_lock = ProjectLock(lockdir / (disk["identity"] + ".lock"))
            stack.callback(device_lock.close)
            if not device_lock.acquired:
                raise OSError("此原盘正在被另一归类任务读取，请稍后继续")
        lease.mark_pending(token, job)
        rows = []
        reserved = {}
        issues = []
        provenance = []
        try:
            for position, row in enumerate(selected):
                check(cancelled)
                progress(position, len(selected), "读取与转换 · " + row["group"])
                try:
                    if row["status"] == "invalid":
                        raise ValueError(row.get("message", "索引无效"))
                    prepared = prepare_record(row, index, request, job, cancelled)
                    for value in prepared:
                        result = video_row(
                            value, request["mapping"][row["group"]], root, reserved, cancelled
                        )
                        rows.append(result)
                        on_row(result)
                        provenance.append(
                            dict(
                                source_id=row["id"],
                                group=row["group"],
                                target=result["target"],
                                sha256=result["sha256"],
                                origin=result["metadata"]["dahua"],
                            )
                        )
                except (OSError, ValueError, RuntimeError) as exc:
                    check(cancelled)
                    issue = dict(
                        source=row["id"],
                        original_source=row["source"],
                        status="blocked",
                        message=str(exc),
                    )
                    issues.append(issue)
                    on_row(issue)
                plan.update(rows=rows, issues=issues, progress=position + 1)
                atomic_json(job / "dahua-plan.json", plan, backup=False)
            if json_sources:
                from .video_intake import plan_import

                sensor_plan = plan_import(
                    str(farm),
                    [dict(kind="imu", path=str(p)) for p in json_sources],
                    "",
                    None,
                    category=category,
                    farm=str(farm),
                    transfer="copy",
                    delete_unusable=False,
                    cancelled=cancelled,
                )
                rows.extend(sensor_plan["rows"])
            ready = [r for r in rows if r["status"] in {"ready", "existing"}]
            if not ready:
                plan.update(status="no_output", issues=issues)
                atomic_json(job / "dahua-plan.json", plan, backup=False)
                lease.complete(token)
                return plan
            dates = sorted({day for row in ready for day in row["covered_dates"]})
            commit = dict(
                adapter=ADAPTER,
                mode="import",
                schema="cowmata-resources-3.4",
                id=token,
                target=str(root),
                resource_root=str(farm),
                farm=farm.name,
                farm_path=str(farm),
                sources=[dict(path=str(job / "records"), kind="video")]
                + [dict(path=str(p), kind="imu") for p in json_sources],
                category=category,
                note=request.get("note", ""),
                created_at=core.now(),
                scenario=scenario,
                reference_records=reference,
                transfer="copy",
                delete_unusable=False,
                allow_partial=True,
                fast_video=True,
                start=dates[0],
                end=dates[-1],
                rows=rows,
            )
            result = execute(commit, job, cancelled, progress, on_row=on_row, _lease=lease)
            for item in result.get("rows", []):
                if item.get("status") == "blocked":
                    issue = dict(
                        source=item.get("source", ""),
                        status="blocked",
                        message=item.get("message", ""),
                    )
                    if issue not in issues:
                        issues.append(issue)
            attachments = root / "归类附属文件" / "大华导入" / token
            attachments.mkdir(parents=True, exist_ok=True)
            atomic_json(
                attachments / "来源与映射.json",
                dict(
                    adapter=ADAPTER,
                    request=request,
                    slots=[
                        dict(
                            view=v, groups=[k for k, val in request["mapping"].items() if val == v]
                        )
                        for v in VIEWS
                    ],
                    source_disk=index.get("disk"),
                    records=provenance,
                    issues=issues,
                ),
                backup=False,
            )
            plan.update(status="completed", result=result, issues=issues, output=str(root))
            atomic_json(job / "dahua-plan.json", plan, backup=False)
            return plan
        except BaseException:
            # execute() may have completed its own commit; keep new-task recovery
            # discoverable until the provenance and final state are durable.
            lease.mark_pending(token, job)
            raise
