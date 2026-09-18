"""Opening-frame naming only. Never publishes verified playback intervals."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from cowmata_tailring.media.ffmpeg_tools import find_ffmpeg
from cowmata_tailring.media.subprocess_tools import run_cancellable

from . import organization as core
from .classification_resources import resource_budget, resource_snapshot
from .farm_layout import shared_farm, storage_root, video_root
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
            "-skip_estimate_duration_from_pts", "1",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        timeout=90,
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
            "-skip_estimate_duration_from_pts", "1",
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
        timeout=90,
        cancelled=cancelled,
    )
    match = re.search(
        r"\bn:\s*0\b.*?pts_time:([\d.eE+\-]+)", result.stderr.decode("utf-8", "replace")
    )
    if result.returncode or not result.stdout or not match:
        raise ValueError("开始帧不能解码，保留原录像待确认")
    return Image.open(io.BytesIO(result.stdout)).convert("RGB"), float(match[1]) * 1000


def parse_archive_stamp(text):
    """Recover OCR punctuation only; never invent missing date/time digits."""
    from .ocr import parse_stamp
    strict = parse_stamp(text)
    if strict:
        return strict
    import unicodedata
    from datetime import datetime
    text = unicodedata.normalize('NFKC', text)
    pattern = r'(?<!\d)(20\d{2})\s*[-/.年]\s*(\d{2})\s*[-/.月]\s*(\d{2})[ T日]*(\d{2})[ :：_.-]*(\d{2})[ :：_.-]*(\d{2})(?!\d)'
    stamps = set()
    for match in re.finditer(pattern, text):
        try:
            stamps.add(datetime(*map(int, match.groups())).strftime('%Y-%m-%d %H:%M:%S'))
        except ValueError:
            pass
    return stamps.pop() if len(stamps) == 1 else None


def read_clock(frame, path, cancelled, *, quick=False):
    from .ocr import TimestampOCR

    if not hasattr(_local, "ocr"):
        _local.ocr = TimestampOCR()
    _local.ocr.engine.cancelled = cancelled
    from .hik_osd import PROFILES

    try:
        with path.open('rb') as stream:
            dahua = b'DHGS' in stream.read(4096)
    except OSError:
        dahua = False
    if frame.size in PROFILES and not dahua:
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
    hints = getattr(_local, 'clock_rois', {})
    key = str(path.parent)
    report = _local.ocr.routing_read(frame, filename=path.name, hint=hints.get(key), raw_only=True, max_passes=5, parser=parse_archive_stamp)
    if not report.get('success') and not quick:
        report = _local.ocr.routing_read(frame, filename=path.name, hint=hints.get(key), raw_only=False, max_passes=15, parser=parse_archive_stamp)
    if report.get('success') and report.get('roi'):
        hints[key] = report['roi']
        _local.clock_rois = hints
    return report


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
    core.check_cancel(cancelled)
    # Recover timestamped source names, never filesystem modification times.
    match = re.search(r'(20\d{2})[-_](\d{2})[-_](\d{2})[ _T](\d{2})[-_:](\d{2})[-_:](\d{2})', path.stem)
    if match:
        from datetime import datetime
        wall = (datetime(*map(int, match.groups())) - datetime(1970, 1, 1)).total_seconds()*1000
        frame, actual = opening_frame(path, 0, cancelled)
        return frame, actual, dict(success=True, wall_ms=wall), wall, 'source_filename'
    raise ValueError("视频时间仍未确定，需补充采集日期后继续归类：" + last_error)


def inspect(path, cache, cancelled=lambda: False):
    path, cache = Path(path), Path(cache)
    external_cancelled = cancelled
    started = time.monotonic()

    def cancelled():
        return external_cancelled()

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
            from .intake_health import blank_recording
            blank = blank_recording(path, cache, cancelled)
            if blank:
                return {**row, 'status': 'empty_video', 'classification': 'all_zero',
                        'health': blank, 'file_seconds': round(time.monotonic()-started, 3),
                        'message': '完整核验为全零文件，没有可解码录像；原件保留，无需手动归类'}
            with path.open("rb") as stream:
                reason = nonvideo_signature(stream.read(128 * 1024), row["size"])
            if reason:
                return {**row, "kind": "nonvideo", "status": "nonvideo", "message": reason}
            key = hashlib.sha256(str(path).encode()).hexdigest()
            saved = cache / (key + ".opening-352.json")
            try:
                result = json.loads(saved.read_text(encoding="utf-8"))
                if result.get("identity") == row["identity"] and result.get("status") == "ready":
                    return {**result, 'recognition_seconds': round(time.monotonic() - started, 3), 'recognition_cached': True}
            except (OSError, ValueError):
                pass
            # PS cameras store a private stream clock. Probing the entire tail
            # first caused false timeouts on busy disks and padded recordings.
            with path.open('rb') as stream:
                is_ps = stream.read(4) == b'\x00\x00\x01\xba'
            info = (dict(streams=[dict(codec_type='video')], format=dict(format_name='mpeg'))
                    if is_ps else probe(path, cancelled))
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
                    "source_filename": "按来源文件名日期归类，时间尚待复核",
                    "native_first_frame": "按流内绝对时间命名",
                    "first_frame_ocr": "按首帧画面时间命名",
                    "opening_ocr_estimate": "按开头邻近帧回推开始秒命名",
                }[basis],
            )
            if core.identity(path) != row["identity"]:
                raise ValueError("识别期间文件变化，保留原处")
            cache.mkdir(parents=True, exist_ok=True)
            atomic_json(saved, row)
    except InterruptedError:
        raise
    except Exception as exc:
        core.check_cancel(external_cancelled)
        row.update(status="blocked", message=str(exc))
    row['recognition_seconds'] = round(time.monotonic() - started, 3)
    return row


def parallel_items(items, work, *, workers=4, cancelled=lambda: False, lane_key=None):
    if lane_key is not None:
        from collections import defaultdict, deque

        groups = defaultdict(deque)
        for item in items:
            groups[lane_key(item)].append(item)
        # One outstanding file per lane, with a global cap and round-robin admission.
        ready = deque(groups)
        slots = max(1, min(32, int(workers)))
        with ThreadPoolExecutor(max_workers=slots, thread_name_prefix="view-prepare") as pool:
            pending = {}
            while ready or pending:
                core.check_cancel(cancelled)
                while ready and len(pending) < slots:
                    key = ready.popleft()
                    pending[pool.submit(work, groups[key].popleft())] = key
                done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    key = pending.pop(future)
                    result = future.result()
                    yield result
                    core.check_cancel(cancelled)
                    if groups[key]:
                        ready.append(key)
        return
    workers = max(1, min(32, int(workers)))
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
    delete_unusable=False,
    on_stage=lambda *_: None,
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
    from .classification_report import source_key

    skipped_keys = {source_key(p) for p in skip_sources}
    reference_days = {d for r in reference for d in r["covered_dates"]}
    index_path = root / "资源索引.json"
    existing_index = (
        json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
    )
    already = {
        os.path.normcase(str(Path(r["source"]))): r
        for r in existing_index.get("records", [])
        if r.get("source")
    }
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
        excluded = [core.safe_path(p) for p in spec.get("exclude", [])]
        for path in core.walk_files(source, cancelled):
            if any(path == p or path.is_relative_to(p) for p in excluded):
                continue
            if source_key(path) in skipped_keys:
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
                aliases = {
                    "乐橙": "视角01",
                    "右1": "视角02",
                    "右2": "视角03",
                    "右3": "视角04",
                    "左1": "视角05",
                    "左2": "视角06",
                    "左3": "视角07",
                }
                for parent in path.parents:
                    if parent.name in aliases:
                        camera = aliases[parent.name]
                        break
                    match = re.match(r"^视角0?([1-9]|1[0-9]|20)(?:$|[_\- ])", parent.name)
                    if match:
                        camera = f"视角{int(match[1]):02d}"
                        break
            entry = dict(
                path=path,
                kind=kind,
                camera=camera,
                explicit=explicit,
                delete_allowed=declared == "video" and not streaming,
                source_root=source,
            )
            previous = jobs.get(source_key(path))
            if previous:
                if previous["explicit"] and explicit and previous["camera"] != camera:
                    raise ValueError("同一录像被指定为不同视角：" + str(path))
                if previous["explicit"] and not explicit:
                    entry["camera"], entry["explicit"] = previous["camera"], True
                entry["delete_allowed"] = entry["delete_allowed"] or previous["delete_allowed"]
            jobs[source_key(path)] = entry

    def prepare(entry):
        path = entry["path"]
        known = already.get(os.path.normcase(str(path)))
        if known and known.get('sha256') and known.get('verified_stamp'):
            from .catalog import file_stamp
            try:
                destination = core.safe_path(root / known['path'])
                current = core.identity(path)
                source_identity = known.get('source_identity')
                if source_identity is None:
                    # Upgrade the old full-hash cache without rereading videos.
                    saved_hash = cache / (hashlib.sha256(str(path).encode()).hexdigest() + '.source.json')
                    cached_hash = json.loads(saved_hash.read_text(encoding='utf-8'))
                    if cached_hash.get('sha256') == known['sha256']:
                        source_identity = cached_hash.get('identity')
                camera_ok = not entry['explicit'] or known.get('owner') == entry['camera']
                if (camera_ok and source_identity == current and destination.is_relative_to(root)
                        and destination.is_file() and file_stamp(destination) == known['verified_stamp']):
                    row = {**known, 'source': str(path), 'target': str(destination), 'identity': current,
                           'status': 'done', 'existing_verified': True, 'resumed_complete': True,
                           'record_date': day_at(known['record_start_ms']), 'file_seconds': 0,
                           'recognition_seconds': 0, 'transfer_seconds': 0,
                           'message': '已归类，文件未变化，直接复用'}
                    return entry, row
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if entry["kind"] == "imu":
            try:
                from .ppg_intake import plan_ppg, plan_temperature
                temperature = plan_temperature(path, root, cache, transfer, cancelled, digest=legacy.verified_source_digest)
                if temperature is not None:
                    return entry, temperature
                ppg = plan_ppg(path, root, cache, transfer, cancelled, digest=legacy.verified_source_digest)
                if ppg is not None:
                    return entry, ppg
                result = legacy.plan_import(
                    resource_root,
                    [{"kind": "imu", "path": str(path)}],
                    '',
                    None,
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
        began = time.monotonic()
        started_at = core.now()
        copy_stop = threading.Event()
        staged = None
        copy_future = None
        stage_path = core.safe_path(root / '.归类缓存' / plan['id'] / 'prepared' / (hashlib.sha256(str(path).encode()).hexdigest() + '.partial'))
        current_identity = core.identity(path)

        def announce(stage):
            if streaming:
                on_stage(dict(source=str(path), kind='video', owner=entry['camera'], status='processing',
                              stage=stage, message=stage, started_at=started_at,
                              file_seconds=round(time.monotonic()-began, 3), _file_started=began))

        def copy_early():
            from .fast_transfer import copy_verified
            stage_path.parent.mkdir(parents=True, exist_ok=True)
            with core.prevent_writes(path):
                if core.identity(path) != current_identity:
                    raise OSError('来源已变化，请重新预览')
                stats = copy_verified(path, stage_path, None, cancelled=lambda: cancelled() or copy_stop.is_set(),
                    progress=lambda current, total, phase: progress(current, total,
                        ('快速复制' if phase == 'copy' else '校验目标') + ' · ' + entry['camera'] + ' · ' + path.name))
                if core.identity(path) != current_identity:
                    raise OSError('复制期间来源变化')
            return dict(path=str(stage_path), stamp=core.file_stamp(stage_path), identity=current_identity, **stats)

        announce('各视角独立处理中：传输与核验并行')
        if streaming and not (transfer == 'move' and core.volume(path) == core.volume(root)):
            copy_future = staging_pool.submit(copy_early)
        def analyze_video():
            core.check_cancel(cancelled)
            health, seconds = {}, 0.0
            if delete_unusable:
                from .intake_health import assess_video
                before = time.monotonic()
                announce('正在核验录像')
                health = assess_video(path, cache, cancelled)
                seconds = time.monotonic()-before
            if health.get('delete_reason'):
                return health, seconds, None
            announce('正在识别采集日期')
            before = time.monotonic()
            value = inspect(path, cache, cancelled)
            if value['status'] == 'blocked' and any(word in value.get('message', '') for word in ('timed out', '占用', '媒体格式无法确认')):
                core.check_cancel(cancelled)
                error = value['message']
                value = inspect(path, cache, cancelled)
                value['retry_reason'] = error
            value['recognition_seconds'] = round(time.monotonic()-before, 3)
            return health, seconds, value

        try:
            announce('等待核验资源；复制仍按队列进行')
            health, health_seconds, row = heavy_pool.submit(analyze_video).result()
            if health.get('delete_reason'):
                copy_stop.set()
                if copy_future:
                    try:
                        staged = copy_future.result()
                    except (OSError, ValueError, RuntimeError):
                        pass
                    stage_path.unlink(missing_ok=True)
                row = dict(source=str(path), target=str(path), kind='video', status='ready',
                    operation='delete_unusable_video', identity=current_identity, health=health,
                    size=path.stat().st_size, owner=entry['camera'], recognition_seconds=0,
                    message='确认异常，立即删除：' + health['delete_reason'])
            else:
                if copy_future:
                    staged = copy_future.result()
                    row['prefetched'] = staged
                    row['sha256'] = staged['sha256']
                if row['status'] in {'ready', 'nonvideo'} and not row.get('sha256'):
                    row['sha256'] = legacy.verified_source_digest(path, cache, cancelled)
            row.update(health=health, health_seconds=round(health_seconds, 3),
                       started_at=started_at, _file_started=began,
                       file_seconds=round(time.monotonic()-began, 3))
            return entry, row
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            copy_stop.set()
            if copy_future:
                try:
                    copy_future.result()
                except (OSError, ValueError, RuntimeError):
                    pass
            core.check_cancel(cancelled)
            return entry, dict(source=str(path), kind='video', status='blocked', owner=entry['camera'],
                               started_at=started_at, finished_at=core.now(),
                               file_seconds=round(time.monotonic()-began, 3), message=str(exc))

    rows, reserved = [], {}
    plan = dict(
        mode="import",
        schema="cowmata-resources-3.4",
        id=uuid.uuid4().hex,
        fast_video=True,
        target=str(storage_root(root)),
        category_scope=str(root) if shared_farm(root) else None,
        resource_root=str(resource_root),
        farm=farm_name,
        farm_path=str(farm_path or root.parent),
        category=category,
        note=note,
        sources=sources,
        scenario=scenario,
        reference_records=[{**r, "path": (root.relative_to(storage_root(root)) / r["path"]).as_posix()} for r in reference],
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
        delete_unusable=delete_unusable,
        workers=workers,
        inventory=[
            dict(
                source=str(e["path"]),
                kind=e["kind"],
                status="pending",
                owner=e["camera"] if e["camera"] in core.VIEWS else "",
                message="等待处理",
            )
            for e in jobs.values()
        ],
    )

    # Feed recognition fairly across cameras, not an entire folder at a time.
    from collections import defaultdict, deque

    groups = defaultdict(deque)
    for entry in jobs.values():
        groups[(str(entry["source_root"]), entry["camera"])].append(entry)
    ordered = []
    while groups:
        for key in list(groups):
            ordered.append(groups[key].popleft())
            if not groups[key]:
                del groups[key]

    staging_pool = heavy_pool = None
    budget = resource_budget(resource_snapshot())
    plan["resource_budget"] = budget.to_dict()

    def generate_rows():
        for entry, row in parallel_items(
            ordered,
            prepare,
            workers=max(workers, min(32, len(sources))),
            cancelled=cancelled,
            lane_key=(lambda e: (str(e["source_root"]), e["camera"])) if streaming else None,
        ):
            if row is None:
                row = dict(
                    source=str(entry["path"]),
                    kind=entry["kind"],
                    status="excluded_aux",
                    message="非采集记录，原件保留",
                    size=entry["path"].stat().st_size,
                )
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
                    row.update(
                        status="invalid" if streaming else "skip",
                        message="不是视频内容，原文件保留",
                    )
            elif row["kind"] == "video" and row["status"] == "ready" and not row.get("operation"):
                day, camera = row["record_date"], entry["camera"]
                if camera not in core.VIEWS:
                    # Preserve an unknown camera's source identity, never merge
                    # unrelated cameras into one arbitrary numbered view.
                    camera = "来源_" + core.safe_name(path.parent.name)
                if reference and day not in reference_days:
                    row["message"] += "；该日没有九轴记录，仍按视频实际日期归类"
                if start and not start <= day <= (end or start):
                    row["message"] += "；扩展日期范围以保留正常视频"
                base = (
                    video_root(root)
                    / day
                    / camera
                    / (start_stamp(row["record_start_ms"]) + row["extension"])
                )
                destination = base
                counter = 0
                while True:
                    key = os.path.normcase(str(destination))
                    known = reserved.get(key)
                    if isinstance(known, str) and known.startswith("pending:"):
                        # A prior lane may still be copying. Resolve only this
                        # collision so identical sources share its final target.
                        known = legacy.verified_source_digest(Path(known[8:]), cache, cancelled)
                        reserved[key] = known
                    if destination.exists() and known is None:
                        known = legacy.verified_source_digest(destination, cache, cancelled)
                    if known is not None and not row.get("sha256"):
                        row["sha256"] = legacy.verified_source_digest(path, cache, cancelled)
                    if known is None or known == row.get("sha256"):
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
                    row.update(status="existing", message="相同内容本批共用已验证归档目标")
                elif destination.exists():
                    row.update(status="existing", message="相同内容已归档")
                reserved[key] = row.get("sha256") or "pending:" + str(path)
            rows.append(row)
            if row.get("record_date"):
                day = row["record_date"]
                plan["start"] = min(plan["start"], day) if plan["start"] else day
                plan["end"] = max(plan["end"], day) if plan["end"] else day
            if not streaming:
                on_row(dict(row))
            yield row
            if row.get("target") and row.get("sha256"):
                reserved[os.path.normcase(row["target"])] = row["sha256"]
            progress(len(rows), plan["total_files"], str(path))

    def generate():
        nonlocal staging_pool, heavy_pool
        with ThreadPoolExecutor(max_workers=budget.heavy_workers, thread_name_prefix='bounded-decode') as decoders:
            with ThreadPoolExecutor(max_workers=budget.copy_workers, thread_name_prefix='bounded-copy') as copies:
                staging_pool, heavy_pool = copies, decoders
                yield from generate_rows()

    if streaming:
        return plan, generate()
    for _ in generate():
        pass
    core.check_identity_ambiguity(rows)
    return plan


def _organize(
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
    _report,
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

    started = time.monotonic()
    from .classification_report import source_key
    live = _report

    completed_lock = threading.RLock()

    def completed(row):
        with completed_lock:
            publish_row(row)

    def publish_row(row):
        row = dict(row)
        row['task_seconds'] = round(time.monotonic() - started, 3)
        final_rows[source_key(row['source'])] = row
        live.row(row)
        on_row(row)

    saved = job / "plan.json"
    previous = json.loads(saved.read_text(encoding="utf-8")) if saved.is_file() else None
    selected_roots = [(core.safe_path(spec['path']), [core.safe_path(p) for p in spec.get('exclude', [])]) for spec in sources]
    def in_selection(row):
        source = core.safe_path(row['source'])
        return any((source == p or source.is_relative_to(p))
                   and not any(source == e or source.is_relative_to(e) for e in excluded)
                   for p, excluded in selected_roots)
    if previous:
        from .organization_live import validate_resume
        validate_resume(previous, dict(options, target=target, sources=sources, start=start, end=end))
        previous['rows'] = [r for r in previous['rows'] if in_selection(r)]
        previous['sources'] = sources
        previous['delete_unusable'] = bool(options.get('delete_unusable'))
    recovered = set()
    recovered_rows = []
    if previous and previous.get('streaming'):
        from .catalog import file_stamp
        index_path = Path(previous['target']) / '资源索引.json'
        index = json.loads(index_path.read_text(encoding='utf-8')) if index_path.is_file() else {}
        indexed = {r['path']: r for r in index.get('records', [])}
        remaining = []
        journal = job / 'journal.jsonl'
        deletions = {}
        if journal.is_file():
            for line in journal.read_text(encoding='utf-8').splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('phase') == 'deleted' and event.get('operation') == 'delete_unusable_video':
                    deletions[source_key(event['source'])] = event
        for event in deletions.values():
            if in_selection(event) and not Path(event['source']).exists():
                done = {**event, 'status': 'deleted', 'target': '', 'file_seconds': 0}
                recovered.add(event['source'])
                recovered_rows.append(done)
                completed(done)
        for row in previous['rows']:
            if row['source'] in recovered or row.get('operation') == 'delete_unusable_video':
                continue
            if row['status'] not in {'ready', 'existing', 'done'} or not row.get('target'):
                continue
            destination = Path(row['target'])
            source = Path(row['source'])
            try:
                relative = destination.relative_to(Path(previous['target'])).as_posix()
                archived = indexed.get(relative, {})
                source_ok = (core.identity(source) == row['identity'] if source.exists() else row.get('transfer') == 'move')
                if (source_ok and destination.is_file() and archived.get('verified_stamp') == file_stamp(destination)
                        and archived.get('sha256') == row.get('sha256') and row.get('sha256')):
                    done = {**row, 'status': 'done', 'resumed_complete': True, 'file_seconds': 0, 'recognition_seconds': 0, 'transfer_seconds': 0, 'message': '已归类，来源与目标身份未变化，无需重复复制'}
                    recovered.add(str(source))
                    recovered_rows.append(done)
                    completed(done)
                    continue
            except (OSError, ValueError, KeyError):
                pass
            if (options.get('delete_unusable') and row.get('kind') == 'video' and source.exists()
                    and row.get('health', {}).get('stamp') != file_stamp(source)):
                # Older plans have no full-health proof. Rescan under the new
                # policy while retaining this task ID and its partial copies.
                continue
            remaining.append({**{k: v for k,v in row.items() if k != '_file_started'}, 'status': 'ready'})
        if remaining:
            # Complete only interrupted transfers. Never rehash hundreds of GB
            # whose full verification and file identity are already recorded.
            legacy.execute({**previous, 'rows': remaining}, job, stopped, progress, on_row=completed)
            for row in final_rows.values():
                if row['status'] in {'done', 'deleted'} and row['source'] not in recovered:
                    recovered.add(row['source'])
                    recovered_rows.append(row)
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
        on_stage=completed,
        **options,
    )
    if previous and previous.get("streaming"):
        plan["id"] = previous["id"]
    plan['rows'].extend(recovered_rows)
    plan['total_files'] += len(recovered_rows)
    live.seed(plan.get('inventory', []) + recovered_rows)
    atomic_json(saved, plan, backup=False)
    report_errors = []

    def clock_report():
        while not local_stop.wait(1):
            try:
                with completed_lock:
                    active = [r for r in live.rows.values() if r.get('status') == 'processing' and r.get('_file_started')]
                    for row in active:
                        row['file_seconds'] = round(time.monotonic()-row['_file_started'], 3)
                        row['task_seconds'] = round(time.monotonic()-started, 3)
                    if active:
                        live.dirty = True
                        live.flush(force=True)
            except Exception as exc:
                report_errors.append(exc)
                local_stop.set()
                return

    clock_thread = threading.Thread(target=clock_report, name='classification-clock', daemon=True)
    clock_thread.start()
    try:
        result = legacy.execute(plan, job, stopped, progress, on_row=completed, row_stream=rows)
        result["rows"] = list(final_rows.values())
        result["archived_files"] = sum(r["status"] == "done" for r in result["rows"])
        result['seconds'] = round(time.monotonic() - started, 3)
        result['completed'] = not result.get('unresolved')
        result['requires_attention'] = bool(result.get('unresolved'))
        result['report_path'] = str(live.path)
        result['reused_files'] = sum(bool(r.get('existing_verified') or r.get('resumed_complete')) for r in result['rows'])
        atomic_json(job / "result.json", result)
        return result
    finally:
        local_stop.set()
        rows.close()
        clock_thread.join()
        atomic_json(saved, plan, backup=False)
        if report_errors:
            raise report_errors[0]


def organize(target, sources, start='', end=None, note='', cancelled=lambda: False,
             progress=lambda *_: None, *, job, on_row=lambda *_: None,
             on_report=lambda *_: None, **options):
    from .classification_report import LiveReport, counts
    with LiveReport(job, on_publish=on_report) as report:
        result = _organize(target, sources, start, end, note, cancelled, progress,
                           job=job, on_row=on_row, _report=report, **options)
        report.flush(force=True)
        result['rows'] = list(report.rows.values())
        result['counts'] = counts(result['rows'])
        result['total_files'] = result['counts']['total']
        result['report_path'] = str(report.path)
        atomic_json(Path(job) / 'result.json', result)
        return result
