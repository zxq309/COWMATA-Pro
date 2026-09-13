"""Opening-frame naming only. Never publishes verified playback intervals."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg
from cowmata_tailring.media.subprocess_tools import run_cancellable

from . import organization as core
from .resource_layout import day_at, start_stamp
from .storage import atomic_json

_local = threading.local()


def protected_file(path):
    return (
        path.suffix.lower() in {".json", ".jsonl", ".csv", ".tsv", ".sqlite", ".sqlite3", ".db"}
        or re.search(r"标签|标注|annotation|label", path.name, re.I) is not None
        or any(
            p.lower() in {"motion", "ppg", "九轴", "标签", "标注", "labels", "annotations"}
            for p in path.parts
        )
        or path.name in core.PROTECTED_NAMES
        or any(part in core.PROTECTED for part in path.parts)
    )


def nonvideo_signature(head, size):
    if not size:
        return "空文件"
    if head.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01", b"\x1aE\xdf\xa3", b"FLV")):
        return None
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "静态图片" if head[8:12] in {b"heic", b"heix", b"avif", b"mif1"} else None
    if head.startswith(b"RIFF"):
        return (
            None
            if head[8:12] == b"AVI "
            else "音频或图片"
            if head[8:12] in {b"WAVE", b"WEBP"}
            else None
        )
    for magic in (
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"GIF87a",
        b"GIF89a",
        b"%PDF-",
        b"PK\x03\x04",
        b"Rar!\x1a\x07",
        b"7z\xbc\xaf\x27\x1c",
        b"MZ",
    ):
        if head.startswith(magic):
            return "图片、文档、压缩包或程序文件"
    try:
        text = head.decode("utf-16" if head.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        if len(head) == size and text and all(c.isprintable() or c in "\r\n\t\ufeff" for c in text):
            return "文本文件"
    except UnicodeError:
        pass
    return None


def probe(path, cancelled):
    _, ffprobe = find_ffmpeg()
    result = run_cancellable(
        [
            str(ffprobe),
            "-v",
            "error",
            "-probesize",
            "8388608",
            "-analyzeduration",
            "3000000",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        timeout=20,
        cancelled=cancelled,
    )
    if result.returncode:
        raise ValueError("媒体格式无法确认，保留原文件待确认")
    return json.loads(result.stdout)


def opening_frame(path, media_ms, cancelled):
    from PIL import Image

    ffmpeg, _ = find_ffmpeg()
    result = run_cancellable(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "info",
            "-threads",
            "2",
            "-probesize",
            "8388608",
            "-analyzeduration",
            "3000000",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-vf",
            f"trim=start={media_ms / 1000:.6f},showinfo",
            "-frames:v",
            "1",
            "-fps_mode",
            "passthrough",
            "-f",
            "image2pipe",
            "-c:v",
            "png",
            "pipe:1",
        ],
        timeout=25,
        cancelled=cancelled,
    )
    match = re.search(
        r"\bn:\s*0\b.*?pts_time:([\d.eE+\-]+)", result.stderr.decode("utf-8", "replace")
    )
    if result.returncode or not result.stdout or not match:
        raise ValueError("开始帧不能解码，保留原录像待确认")
    return Image.open(io.BytesIO(result.stdout)).convert("RGB"), float(match[1]) * 1000


def read_clock(frame, path, cancelled, *, quick=False):
    from .ocr import TimestampOCR

    if not hasattr(_local, "ocr"):
        _local.ocr = TimestampOCR()
    _local.ocr.engine.cancelled = cancelled
    from .hik_osd import PROFILES

    if frame.size in PROFILES:
        pixel_candidate = True
        if hasattr(_local.ocr, "native_check"):
            fast = _local.ocr.native_check(
                frame, filename=path.name, family="hikvision-hk1", opening_only=True
            )
            if fast.get("success"):
                return {**fast, "method": "hik_whole_clock"}
            pixel_candidate = any(p.get("date") for p in fast.get("date_passes", []))
        report = {
            "success": False,
            "wall_ms": None,
            "metadata": {},
            "warnings": [],
            "filename": path.name,
            "method": "hik_pixel_first_frame",
        }
        if (
            not quick
            and pixel_candidate
            and (_local.ocr._hik_read(frame, report) or report.get("enhancement_conflict"))
        ):
            return report
    # One image, at most four corner crops. No temporal sampling or enhancement loop.
    return _local.ocr.routing_read(frame, filename=path.name, raw_only=True, max_passes=4)


def native_first_start(path, cancelled=lambda: False):
    """Read at most 1 MiB; no tail scan, generic creation tag, or filesystem date."""
    from cowmata_tailring.media.native_ps import PREFIX, _scan

    with Path(path).open("rb") as stream:
        head = stream.read(1024 * 1024)
    if not head.startswith(PREFIX + b"\xba"):
        return None
    try:
        parsed = _scan(head, cancelled=cancelled)
        anchors, frames = parsed["anchors"], parsed["frames"]
        if not anchors or not frames:
            return None
        pts, stamp = anchors[0]
        if not 0 <= pts - frames[0] <= 10000:
            return None
        if any(abs((wall - stamp) - (at - pts)) > 2000 for at, wall in anchors[:4]):
            return None
        return dict(start_ms=stamp - (pts - frames[0]), family=parsed["family"])
    except ValueError:
        return None


def opening_timestamp(path, cancelled):
    native = native_first_start(path, cancelled)
    first_media = 0.0
    last_error = ""
    for index, offset in enumerate((0, 1000, 2000, 4000)):
        if cancelled():
            break
        try:
            frame, actual = opening_frame(path, 0 if not index else first_media + offset, cancelled)
            if not index:
                first_media = actual
            if native:
                report = dict(
                    success=True,
                    wall_ms=native["start_ms"],
                    method="native_first_frame",
                    family=native["family"],
                )
                return frame, actual, report, native["start_ms"], "native_first_frame"
            report = (
                read_clock(frame, path, cancelled)
                if not index
                else read_clock(frame, path, cancelled, quick=True)
            )
            if (
                report.get("success")
                and report.get("wall_ms") is not None
                and not report.get("enhancement_conflict")
            ):
                if not index:
                    return frame, actual, report, report["wall_ms"], "first_frame_ocr"
                # OSD is second-quantized. Record this as an estimate, never as verified sync.
                estimate = (
                    math.floor((report["wall_ms"] - (actual - first_media) + 500) / 1000) * 1000
                )
                return frame, actual, report, estimate, "opening_ocr_estimate"
        except (OSError, ValueError, RuntimeError) as exc:
            last_error = str(exc)
    raise ValueError("开头自动尝试仍未取得可靠时间，已保留并跳过。" + last_error)


def inspect(path, cache, cancelled=lambda: False):
    path, cache = Path(path), Path(cache)
    external_cancelled = cancelled
    deadline = time.monotonic() + 40

    def cancelled():
        return external_cancelled() or time.monotonic() >= deadline

    row = {"source": str(path), "kind": "video", "device": "", "status": "blocked", "message": ""}
    try:
        path = core.safe_path(path)
        row.update(size=path.stat().st_size, identity=core.identity(path))
        if protected_file(path):
            return {
                **row,
                "status": "skip",
                "protected": True,
                "message": "九轴、标签或工程文件保留",
            }
        with core.prevent_writes(path):
            core.check_cancel(cancelled)
            with path.open("rb") as stream:
                reason = nonvideo_signature(stream.read(128 * 1024), row["size"])
            if reason:
                return {**row, "kind": "nonvideo", "status": "nonvideo", "message": reason}
            key = hashlib.sha256(str(path).encode()).hexdigest()
            saved = cache / (key + ".opening-352.json")
            try:
                result = json.loads(saved.read_text(encoding="utf-8"))
                if result.get("identity") == row["identity"] and result.get("status") == "ready":
                    return result
            except (OSError, ValueError):
                pass
            info = probe(path, cancelled)
            video = next(
                (
                    s
                    for s in info.get("streams", [])
                    if s.get("codec_type") == "video"
                    and not s.get("disposition", {}).get("attached_pic")
                ),
                None,
            )
            fmt = info.get("format", {}).get("format_name", "")
            if not video or any(t in fmt for t in ("image2", "_pipe", "gif")):
                return {
                    **row,
                    "kind": "nonvideo",
                    "status": "nonvideo",
                    "message": "媒体解析成功，非录像视频流",
                }
            frame, actual, report, wall_start, basis = opening_timestamp(path, cancelled)
            cache.mkdir(parents=True, exist_ok=True)
            preview = cache / (key + "-" + str(row["identity"][-1]) + ".opening.png")
            frame.save(preview)
            row["preview_path"] = str(preview)
            if (
                report.get("enhancement_conflict")
                or not report.get("success")
                or report.get("wall_ms") is None
            ):
                raise ValueError("首帧完整日期时间未读清，保留原文件待确认")
            lo = float(wall_start) - 480 * 60000
            if not math.isfinite(lo):
                raise ValueError("首帧时间戳无效")
            # End is a routing estimate only. It is not evidence of coverage.
            try:
                duration = (
                    float(video.get("duration") or info.get("format", {}).get("duration", 0)) * 1000
                )
            except (TypeError, ValueError):
                duration = 0
            duration = duration if math.isfinite(duration) and duration > 0 else 0
            metadata = {
                "naming_only": True,
                "needs_review": True,
                "intervals": [],
                "samples": [{"media_ms": actual, "wall_ms": report["wall_ms"], "ocr": report}],
                "archive_time": {"start_ms": wall_start, "basis": basis, "start_verified": False},
                "start_display": start_stamp(lo),
                "duration_ms": duration,
                "end_estimated": True,
                "width": frame.width,
                "height": frame.height,
            }
            extension = path.suffix.lower()
            if extension not in core.VIDEO_SUFFIXES:
                extension = next(
                    (
                        ext
                        for token, ext in [
                            ("mp4", ".mp4"),
                            ("mov", ".mov"),
                            ("mpegts", ".ts"),
                            ("mpeg", ".mpg"),
                            ("matroska", ".mkv"),
                            ("avi", ".avi"),
                            ("h264", ".h264"),
                            ("hevc", ".h265"),
                            ("webm", ".webm"),
                            ("asf", ".asf"),
                            ("flv", ".flv"),
                        ]
                        if token in fmt.split(",")
                    ),
                    "",
                )
                if not extension:
                    raise ValueError(
                        "Unsupported recording container; original retained for review: " + fmt
                    )
            row.update(
                status="ready",
                record_start_ms=lo,
                record_end_ms=lo + max(1000, duration),
                record_date=day_at(lo),
                covered_dates=[day_at(lo)],
                metadata=metadata,
                extension=extension,
                message={
                    "native_first_frame": "按流内绝对时间命名",
                    "first_frame_ocr": "按首帧画面时间命名",
                    "opening_ocr_estimate": "按开头邻近帧回推开始秒命名",
                }[basis],
            )
            if core.identity(path) != row["identity"]:
                raise ValueError("识别期间文件变化，保留原处")
            cache.mkdir(parents=True, exist_ok=True)
            atomic_json(saved, row)
    except Exception as exc:
        row.update(status="blocked", message=str(exc))
    return row


def parallel_items(items, work, *, workers=4, cancelled=lambda: False):
    workers = max(1, min(8, int(workers)))
    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="video-intake") as pool:
        pending = set()
        while True:
            core.check_cancel(cancelled)
            while len(pending) < workers:
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                pending.add(pool.submit(work, item))
            if not pending:
                return
            done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                core.check_cancel(cancelled)
                yield future.result()


def plan_import(
    target,
    sources,
    start="",
    end=None,
    note="",
    cancelled=lambda: False,
    progress=lambda *_: None,
    *,
    category=None,
    farm="",
    cache=None,
    transfer="copy",
    scenario="mixed",
    workers=4,
    on_row=lambda *_: None,
    streaming=False,
    skip_sources=(),
    video_suffix_only=False,
):
    import os
    import uuid
    from datetime import date

    from . import resource_import as legacy
    from .data_category import category_fields, category_root

    if transfer not in {"copy", "move"}:
        raise ValueError("未知保存方式")
    if end and not start:
        raise ValueError("请先选择起始日期")
    if start and date.fromisoformat(start) > date.fromisoformat(end or start):
        raise ValueError("结束日期不能早于起始日期")
    resource_root = core.safe_path(target)
    farm_path = core.safe_path(farm) if farm and Path(farm).is_absolute() else None
    farm_name = farm_path.name if farm_path else core.safe_name(farm or resource_root.name)
    reference = []
    if scenario == "attach_video":
        selected = farm_path or resource_root
        scope = next(
            (p for p in (resource_root, *resource_root.parents) if (p / "Motion").is_dir()), None
        )
        if scope is None:
            scope = category_root(selected, farm_name, category) if category else selected
        root, code, reference = legacy.reference_motion_scope(scope)
        if category and category != code:
            raise ValueError("所选类别与已有九轴类别不一致")
        category = code
        farm_path = root.parent.parent if root.parent.name == "怀孕" else root.parent
        farm_name = farm_path.name
    elif scenario == "mixed":
        category_fields(category)
        if category == "pregnancy":
            raise ValueError("请先选择孕期阶段")
        root = category_root(farm_path or resource_root, farm_name, category)
    else:
        raise ValueError("未知整理场景")
    if not sources:
        raise ValueError("请添加九轴或录像来源")
    cache = (
        Path(cache)
        if cache
        else Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        / "COWMATA Annotator/resource-probes"
    )
    cache.mkdir(parents=True, exist_ok=True)
    reference_days = {d for r in reference for d in r["covered_dates"]}
    jobs = {}
    for spec in sources:
        source = core.safe_path(spec["path"])
        if core.overlaps(source, Path(__file__).resolve().parents[2]):
            raise ValueError("录像来源不能包含软件目录，请选择具体采集目录")
        if core.overlaps(source, cache) or core.overlaps(root, cache):
            raise ValueError("识别缓存必须放在来源和目标之外")
        declared = spec["kind"]
        if declared not in {"imu", "video", "auto"}:
            raise ValueError("未知来源类型")
        if scenario == "attach_video" and declared == "imu":
            continue
        for path in core.walk_files(source, cancelled):
            if str(path) in skip_sources:
                continue
            # Existing organized targets are never cleanup input, even when nested in source.
            if path.is_relative_to(root):
                continue
            is_imu = path.suffix.lower() == ".json" and not any(
                p in core.PROTECTED for p in path.parts
            )
            kind = "imu" if is_imu and declared != "video" and scenario == "mixed" else "video"
            if (
                video_suffix_only
                and kind == "video"
                and path.suffix.lower() not in core.VIDEO_SUFFIXES
            ):
                continue
            if protected_file(path) and kind != "imu" or declared == "imu" and kind != "imu":
                continue
            camera = spec.get("camera") or "auto"
            explicit = camera in core.VIEWS
            if not explicit:
                for parent in path.parents:
                    match = re.match(r"^视角0?([1-8])(?:$|[_\- ])", parent.name)
                    if match:
                        camera = f"视角{int(match[1]):02d}"
                        break
            entry = dict(
                path=path,
                kind=kind,
                camera=camera,
                explicit=explicit,
                delete_allowed=declared == "video",
                source_root=source,
            )
            previous = jobs.get(str(path))
            if previous:
                if previous["explicit"] and explicit and previous["camera"] != camera:
                    raise ValueError("同一录像被指定为不同视角：" + str(path))
                if previous["explicit"] and not explicit:
                    entry["camera"], entry["explicit"] = previous["camera"], True
                entry["delete_allowed"] = entry["delete_allowed"] or previous["delete_allowed"]
            jobs[str(path)] = entry

    def prepare(entry):
        path = entry["path"]
        if entry["kind"] == "imu":
            try:
                result = legacy.plan_import(
                    resource_root,
                    [{"kind": "imu", "path": str(path)}],
                    start,
                    end,
                    note,
                    cancelled,
                    category=category,
                    farm=str(farm_path) if farm_path else farm_name,
                    cache=cache,
                    transfer=transfer,
                )
                return entry, (result["rows"][0] if result["rows"] else None)
            except (ValueError, OSError, RuntimeError) as exc:
                return entry, dict(
                    source=str(path),
                    kind="imu",
                    size=path.stat().st_size,
                    status="blocked",
                    message=str(exc),
                )
        row = inspect(path, cache, cancelled)
        if row["status"] in {"ready", "nonvideo"}:
            try:
                row["sha256"] = legacy.verified_source_digest(path, cache, cancelled)
            except Exception as exc:
                row.update(status="blocked", message=str(exc))
        return entry, row

    rows, reserved = [], {}
    plan = dict(
        mode="import",
        schema="cowmata-resources-3.4",
        id=uuid.uuid4().hex,
        fast_video=True,
        target=str(root),
        resource_root=str(resource_root),
        farm=farm_name,
        farm_path=str(farm_path or root.parent),
        category=category,
        note=note,
        sources=sources,
        scenario=scenario,
        reference_records=reference,
        created_at=core.now(),
        transfer=transfer,
        allow_partial=True,
        requested_start=start,
        requested_end=end or "",
        start=start,
        end=end or start,
        rows=rows,
        total_files=len(jobs),
        streaming=streaming,
        workers=workers,
    )

    def generate():
        for entry, row in parallel_items(
            jobs.values(), prepare, workers=workers, cancelled=cancelled
        ):
            if row is None:
                continue
            path = entry["path"]
            if row["kind"] == "nonvideo" and row["status"] == "nonvideo":
                if entry["delete_allowed"]:
                    row.update(
                        status="ready",
                        operation="delete_nonvideo",
                        target=str(path),
                        owner="",
                        cleanup_root=str(entry["source_root"]),
                        message="确认非录像，执行时删除：" + row["message"],
                    )
                else:
                    row.update(status="skip", message="混合来源中的非视频保留")
            elif row["kind"] == "video" and row["status"] == "ready":
                day, camera = row["record_date"], entry["camera"]
                if camera not in core.VIEWS:
                    row.update(status="blocked", message="视角未明确，请在来源表指定视角01至视角08")
                elif (
                    start
                    and not start <= day <= (end or start)
                    or reference
                    and day not in reference_days
                ):
                    row.update(
                        status="skip", message="开始日期不在所选日期或已有九轴日期内，保留原处"
                    )
                else:
                    base = (
                        root
                        / "Video"
                        / day
                        / camera
                        / (start_stamp(row["record_start_ms"]) + row["extension"])
                    )
                    destination = base
                    counter = 0
                    while True:
                        key = os.path.normcase(str(destination))
                        known = reserved.get(key)
                        if destination.exists() and known is None:
                            known = legacy.verified_source_digest(destination, cache, cancelled)
                        if known is None or known == row["sha256"]:
                            break
                        counter += 1
                        destination = base.with_name(f"{base.stem}__{counter:03d}{base.suffix}")
                    row.update(
                        target=str(destination),
                        owner=camera,
                        batch=day,
                        timezone_offset_minutes=480,
                        time_basis="unix_epoch_ms",
                        transfer="move"
                        if transfer == "move" and core.volume(path) == core.volume(root)
                        else "copy",
                    )
                    row["metadata"]["camera"] = camera
                    if key in reserved:
                        row.update(status="skip", message="相同内容本批已列入归档")
                    elif destination.exists():
                        row.update(status="existing", message="相同内容已归档")
                    reserved[key] = row["sha256"]
            rows.append(row)
            if row.get("record_date"):
                day = row["record_date"]
                plan["start"] = min(plan["start"], day) if plan["start"] else day
                plan["end"] = max(plan["end"], day) if plan["end"] else day
            if not streaming:
                on_row(dict(row))
            yield row
            progress(len(rows), len(jobs), str(path))

    if streaming:
        return plan, generate()
    for _ in generate():
        pass
    core.check_identity_ambiguity(rows)
    return plan


def organize(
    target,
    sources,
    start="",
    end=None,
    note="",
    cancelled=lambda: False,
    progress=lambda *_: None,
    *,
    job,
    on_row=lambda *_: None,
    **options,
):
    """One lease, bounded parallel recognition, immediate verified transfers."""
    from . import resource_import as legacy

    job = core.safe_path(job)
    job.mkdir(parents=True, exist_ok=True)
    local_stop = threading.Event()

    def stopped():
        return local_stop.is_set() or cancelled()

    final_rows = {}

    def completed(row):
        if row["status"] in {"blocked", "invalid"}:
            row = {
                **row,
                "message": row.get("message", "").replace("待确认", "").rstrip("；。")
                + "；本次已自动跳过，原文件保留。",
            }
        final_rows[row["source"]] = dict(row)
        on_row(row)

    saved = job / "plan.json"
    previous = json.loads(saved.read_text(encoding="utf-8")) if saved.is_file() else None
    recovered = set()
    if previous and previous.get("streaming"):
        if any(r["status"] in {"ready", "existing"} for r in previous["rows"]):
            legacy.execute(previous, job, stopped, progress, on_row=completed)
            recovered = {
                key for key, row in final_rows.items() if row["status"] in {"done", "deleted"}
            }
    plan, rows = plan_import(
        target,
        sources,
        start,
        end,
        note,
        stopped,
        progress,
        streaming=True,
        skip_sources=recovered,
        video_suffix_only=True,
        **options,
    )
    if previous and previous.get("streaming"):
        plan["id"] = previous["id"]
    atomic_json(saved, plan)
    try:
        result = legacy.execute(plan, job, stopped, progress, on_row=completed, row_stream=rows)
        result["rows"] = list(final_rows.values())
        result["archived_files"] = sum(r["status"] == "done" for r in result["rows"])
        atomic_json(job / "result.json", result)
        return result
    finally:
        local_stop.set()
        rows.close()
