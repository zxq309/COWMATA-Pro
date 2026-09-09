"""Read-only planning and journalled, same-volume moves. Never copies media."""
from __future__ import annotations

import base64
import csv
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .catalog import EXCLUDE_DIRS, VIDEO_SUFFIXES, Catalog, assert_not_being_written, file_stamp
from .data_category import CONTEXT_FILE, FIELDS, category_fields, update_context
from .dataset_access import DatasetLease, overlaps
from .storage import ProjectLock

VIEWS = tuple(f"视角{i:02d}" for i in range(1, 9))
PROTECTED = EXCLUDE_DIRS | {"标注成果", "整理隔离", "整理记录", "assets", "vendor", "model_runtime_20260906"}
PROTECTED_NAMES = {"绑定.csv", "实验佩戴记录.csv", "整理清单.csv", CONTEXT_FILE, "package-manifest.json"}
MANIFEST_FIELDS = "source_path,target_relative_path,kind,device_or_camera,batch,size,sha256,status,at".split(",")
TABLES = {
    "绑定.csv": "device_id,cow_id,barn_id,valid_from,valid_to,timezone,status,evidence",
    "实验佩戴记录.csv": "experiment_id,experiment_type,device_id,cow_id,barn_id,valid_from,valid_to,timezone,status,evidence,note",
    "整理清单.csv": ",".join(MANIFEST_FIELDS),
}


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def safe_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("请选择不含上级跳转的绝对路径")
    path = Path(os.path.abspath(path))
    if path.parent == path:
        raise ValueError("不能把整个磁盘作为数据整理目录")
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part.lstat() if part.exists() else None, "st_file_attributes", 0) & 0x400:
            raise ValueError("不整理符号链接、目录联接或重定向路径：" + str(part))
    return path


def safe_name(value):
    if not value or value in {".", ".."} or value.endswith((" ", ".")) or re.search(r'[<>:"/\\|?*\x00-\x1f]', value):
        raise ValueError("设备号或目录名包含不支持的字符")
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("目录名不能使用 Windows 保留名称")
    return value


def identity(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def volume(path):
    while not path.exists():
        path = path.parent
    return path.stat().st_dev


def check_cancel(cancelled):
    if cancelled():
        raise InterruptedError("任务已暂停；已完成项保留，可继续原任务")


def walk_files(root, cancelled=lambda: False):
    root = safe_path(root)
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise ValueError("来源目录或文件不存在：" + str(root))
    def fail(exc):
        raise exc
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        check_cancel(cancelled)
        dirs[:] = sorted(d for d in dirs if d not in PROTECTED)
        for name in list(dirs):
            safe_path(Path(folder) / name)
        for name in sorted(files):
            check_cancel(cancelled)
            yield safe_path(Path(folder) / name)


def inspect_file(path, kind=None):
    stat = path.stat()
    row = {"source": str(path), "size": stat.st_size, "identity": identity(path), "kind": "other",
           "device": "", "status": "skip", "message": "配套文件保留原处", "quarantine": False}
    if path.name in PROTECTED_NAMES or path.name.endswith(".标注.json") or any(part in PROTECTED for part in path.parts):
        row["message"] = "已有标注或软件文件，受保护"
        row["protected"] = True
        return row
    suffix = path.suffix.lower()
    if path.name.lower() in {"thumbs.db", "desktop.ini", ".ds_store"} or suffix in {".tmp", ".part", ".partial", ".pyc"}:
        row.update(status="junk", message="临时或系统文件，可确认后隔离", quarantine=True)
        return row
    if suffix not in VIDEO_SUFFIXES | {".json"}:
        return row
    row["kind"] = "imu" if suffix == ".json" else "video"
    if kind == "imu" and row["kind"] == "video":
        row["message"] = "不属于此来源指定的素材类型，保留原处"
        row["protected"] = True
        return row
    if stat.st_size == 0:
        row.update(status="invalid", message="空文件，不纳入归类", quarantine=True)
        return row
    try:
        assert_not_being_written(path)
        if row["kind"] == "imu":
            # One bounded JSON at a time, in a separate worker process. Never
            # decode video or retain waveforms from an entire dataset in RAM.
            if stat.st_size > 64 * 1024**2:
                raise ValueError("九轴 JSON 超过 64 MiB，请单独核对")
            with path.open(encoding="utf-8-sig") as stream:
                obj = json.load(stream)
            if not isinstance(obj, dict) or obj.get("format") == "cowmata-annotation":
                row["message"] = "不是原始九轴记录，保留原处"
                row["protected"] = isinstance(obj, dict) and obj.get("format") == "cowmata-annotation"
                return row
            if not isinstance(obj.get("imu"), str) or not obj["imu"]:
                row["message"] = "不含九轴 imu 字段，保留原处"
                return row
            device = str(obj.get("device") or obj.get("device_id") or "").strip()
            if not device:
                raise ValueError("JSON 内没有明确设备号，不按文件夹或牛号猜测")
            safe_name(device)
            raw = base64.b64decode(obj["imu"], validate=True)
            if not raw or not any(len(raw) % width == 0 for width in (18, 20, 22)):
                raise ValueError("imu 数据没有完整的已支持帧结构")
            if obj.get("version") is not None and (obj["version"] not in (0, 1, 2) or len(raw) % (18 + 2 * obj["version"])):
                raise ValueError("imu 版本与帧结构不匹配")
            row["device"] = device
            if kind == "video":
                row.update(protected=True, message="录像来源中发现原始九轴，保留并请核对来源")
                return row
        if identity(path) != row["identity"]:
            raise OSError("文件在审查期间发生变化")
        row.update(status="ready", message="结构审查通过；采集时间与视频解码仍由标注工具核验")
    except (OSError, ValueError, TypeError) as exc:
        row.update(status="invalid", message=str(exc), quarantine=False)
    return row


def audit(roots, cancelled=lambda: False, progress=lambda *_: None):
    started = time.monotonic()
    rows, seen = [], set()
    for value in roots:
        for path in walk_files(value, cancelled):
            if str(path) in seen:
                continue
            seen.add(str(path))
            rows.append(inspect_file(path))
            progress(len(rows), 0, str(path))
    return {"mode": "audit", "created_at": now(), "roots": list(map(str, roots)), "rows": rows,
            "seconds": round(time.monotonic() - started, 3)}


def plan_import(target, sources, start, end=None, note="", cancelled=lambda: False, progress=lambda *_: None, *, category=None):
    category_fields(category)
    target = safe_path(target)
    start_date, end_date = date.fromisoformat(start), date.fromisoformat(end or start)
    if end_date < start_date:
        raise ValueError("结束日期不能早于开始日期")
    period = start_date.isoformat() if start_date == end_date else f"{start_date.isoformat()}至{end_date.isoformat()}"
    if not sources:
        raise ValueError("至少选择一份九轴文件或一个素材目录")
    rows, seen, batches, reserved = [], set(), {}, set()
    token = uuid.uuid4().hex
    quarantine = target.parent / "整理隔离" / token
    for spec in sources:
        source = safe_path(spec["path"])
        if overlaps(source, target):
            raise ValueError("导入来源与目标不能相同或互为父子目录；工程内请用规范视角名称")
        kind = spec["kind"]
        camera = spec.get("camera", "")
        if kind not in {"imu", "video"} or kind == "video" and camera not in VIEWS:
            raise ValueError("每路录像必须明确选择视角01至视角08")
        for path in walk_files(source, cancelled):
            if str(path) in seen:
                raise ValueError("来源重复或互相重叠，请删除重复来源：" + str(path))
            seen.add(str(path))
            row = inspect_file(path, kind)
            if row["status"] == "ready":
                owner = row["device"] if kind == "imu" else camera
                base = target / "九轴" / owner if kind == "imu" else target / camera
                key = (str(base), str(path.parent))
                if key not in batches:
                    batches[key] = base / period
                destination = batches[key] / path.name
                if destination.exists() or os.path.normcase(str(destination)) in reserved:
                    for number in range(1, 10001):
                        batch = base / f"{period}_{number:03d}"
                        candidate = batch / path.name
                        if not candidate.exists() and os.path.normcase(str(candidate)) not in reserved:
                            batches[key] = batch
                            break
                    else:
                        raise ValueError("批次编号已用完，请核对目标目录")
                destination = batches[key] / path.name
                reserved.add(os.path.normcase(str(destination)))
                row.update(target=str(destination), owner=owner, batch=batches[key].name)
                if volume(path) != volume(target):
                    row.update(status="blocked", message="跨盘移动需要复制，本功能不执行跨盘搬运")
            elif not row.get("protected") and (row["status"] in {"junk", "skip"} or row.get("quarantine")):
                row.update(status="quarantine", operation="quarantine", owner="", batch=token,
                           target=str(quarantine / f"{len(rows):06d}" / path.name),
                           message="非素材文件移至工程外隔离；不进入九轴或视角目录")
                if volume(path) != volume(target):
                    row.update(status="blocked", message="跨盘不能快速隔离，原文件保留")
            rows.append(row)
            progress(len(rows), 0, str(path))
    return {"mode": "import", "id": token, "target": str(target), "quarantine_root": str(quarantine), "sources": sources,
            "start": start_date.isoformat(), "end": end_date.isoformat(), "created_at": now(), "note": note,
            "category": category, "rows": rows}


def plan_normalize(target, cancelled=lambda: False, progress=lambda *_: None):
    target = safe_path(target)
    if not target.is_dir():
        raise ValueError("请先选择已有数据工程")
    rows, reserved = [], set()
    token = uuid.uuid4().hex
    quarantine = target.parent / "整理隔离" / token
    for folder in sorted(target.iterdir()):
        match = re.fullmatch(r"视角0?([1-8])(?:[_\- ].+)?", folder.name)
        if not match or not folder.is_dir():
            continue
        camera = f"视角{int(match[1]):02d}"
        if folder.name == camera:
            continue
        for path in walk_files(folder, cancelled):
            row = inspect_file(path, "video")
            if row["status"] == "ready":
                destination = target / camera / path.relative_to(folder)
                row.update(target=str(destination), owner=camera, batch=path.parent.name)
                if destination.exists() or os.path.normcase(str(destination)) in reserved:
                    row.update(status="blocked", message="目标已有同名文件，不合并或覆盖，请先核对批次")
                reserved.add(os.path.normcase(str(destination)))
            elif not row.get("protected") and (row["status"] in {"junk", "skip"} or row.get("quarantine")):
                row.update(status="quarantine", operation="quarantine", owner="", batch=token,
                           target=str(quarantine / f"{len(rows):06d}" / path.name),
                           message="非素材文件移至工程外隔离；不进入九轴或视角目录")
            rows.append(row)
            progress(len(rows), 0, str(path))
    return {"mode": "normalize", "id": token, "target": str(target), "quarantine_root": str(quarantine),
            "sources": [{"path": str(target)}], "created_at": now(), "rows": rows}


def plan_quarantine(report, target):
    target = safe_path(target)
    token = uuid.uuid4().hex
    # Outside the scanner's root, same volume, reversible without a data copy.
    quarantine = target.parent / "整理隔离" / token
    rows = []
    for index, item in enumerate(report["rows"]):
        if not item.get("quarantine"):
            continue
        row = dict(item)
        row.update(target=str(quarantine / f"{index:06d}" / Path(row["source"]).name),
                   owner="", batch=token, status="ready")
        if volume(Path(row["source"])) != volume(quarantine):
            row.update(status="blocked", message="跨盘不能快速隔离，请在来源所在磁盘审查")
        rows.append(row)
    return {"mode": "quarantine", "id": token, "target": str(quarantine), "sources": [{"path": p} for p in report["roots"]],
            "created_at": now(), "rows": rows}


@contextmanager
def prevent_writes(path):
    if os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "文件有占用或不可读，保留原处", str(path))
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def move_no_replace(source, target):
    if volume(source) != volume(target):
        raise ValueError("跨盘移动被拦截，不会退回复制后删源")
    if os.name == "nt":
        # Windows rename refuses existing targets and cross-volume moves.
        os.rename(source, target)
    else:
        # Exclusive hard-link creation preserves the same file object; no bytes
        # are copied, and an existing target is never replaced on POSIX either.
        os.link(source, target, follow_symlinks=False)
        source.unlink()


def append_journal(path, entry):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": now(), **entry}, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def completed_sources(journal):
    done, offset = set(), 0
    if not journal.exists():
        return done
    # A killed process may leave a partial final append. Keep all durable
    # records; repair only the incomplete tail before continuing that task.
    with journal.open("r+b") as stream:
        for line in stream:
            if not line.endswith(b"\n"):
                stream.truncate(offset)
                break
            event = json.loads(line)
            if event.get("phase") == "done":
                done.add(event["source"])
            offset += len(line)
    return done


def append_manifest(root, row):
    path = root / "整理清单.csv"
    with path.open(encoding="utf-8-sig", newline="") as stream:
        fields = next(csv.reader(stream))
    if fields != MANIFEST_FIELDS:
        raise ValueError("整理清单表头不同，请先核对，原清单未覆盖")
    item = dict(zip(MANIFEST_FIELDS, (row["source"], Path(row["target"]).relative_to(root).as_posix(),
                row["kind"], row["owner"], row["batch"], row["size"], "", "moved", now())))
    with path.open("a", encoding="utf-8", newline="") as stream:
        csv.DictWriter(stream, fieldnames=fields).writerow(item)
        stream.flush()
        os.fsync(stream.fileno())


def _rewrite(value, replacements):
    if isinstance(value, str):
        for old, new in replacements:
            if value == old or value.startswith(old + "/") or value.startswith(old + "\\"):
                return new + value[len(old):]
        return value
    if isinstance(value, list):
        return [_rewrite(item, replacements) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = _rewrite(key, replacements)
            if name in result:
                raise ValueError("视角校准或设置重名，请先核对，原标注未合并")
            result[name] = _rewrite(item, replacements)
        return result
    return value


def relocate_metadata(plan, job, *, validate_only=False):
    """Preserve asset IDs, samples, labels and clock anchors while relinking paths."""
    target = Path(plan["target"])
    moved = [r for r in plan["rows"] if r["status"] == "ready"]
    for root in (target, *target.parents):
        meta = root / "标注工程"
        if not meta.is_dir():
            continue
        mapping = [(str(Path(r["source"]).relative_to(root)), str(Path(r["target"]).relative_to(root))) for r in moved]
        replacements = [(a.replace("\\", "/"), b.replace("\\", "/")) for a, b in mapping]
        replacements += [(r["source"], r["target"]) for r in moved]
        cameras = {(Path(r["source"]).relative_to(target).parts[0], r["owner"]) for r in moved}
        replacements += sorted(cameras)
        backup = job / "metadata-before" / str(len(root.parts))
        if not validate_only:
            backup.mkdir(parents=True, exist_ok=True)
        index = meta / "index.sqlite"
        if index.is_file():
            db = sqlite3.connect(index.as_uri() + "?mode=ro", uri=True, timeout=1) if validate_only else sqlite3.connect(index, timeout=1)
            try:
                if validate_only:
                    for (raw,) in db.execute("SELECT metadata FROM assets"):
                        _rewrite(json.loads(raw), replacements)
                    continue_index = False
                else:
                    continue_index = True
                if continue_index and not (backup / "index.sqlite").exists():
                    with sqlite3.connect(backup / "index.sqlite") as saved:
                        db.backup(saved)
                with db:
                    for r in moved if continue_index else []:
                        old = Path(r["source"]).relative_to(root).as_posix()
                        new = Path(r["target"]).relative_to(root).as_posix()
                        stamp = file_stamp(Path(r["target"]))
                        db.execute("UPDATE locations SET path=?,stamp=? WHERE path=?", (new, stamp, old))
                        db.execute("UPDATE video_hints SET path=?,stamp=? WHERE path=?", (new, stamp, old))
                    for asset, raw in db.execute("SELECT id,metadata FROM assets").fetchall() if continue_index else []:
                        value = json.loads(raw)
                        changed = _rewrite(value, replacements)
                        if value != changed:
                            db.execute("UPDATE assets SET metadata=? WHERE id=?", (json.dumps(changed, ensure_ascii=False), asset))
            finally:
                db.close()
        for path in [meta / "project.json", *sorted((meta / "annotations").glob("*.json"))]:
            if not path.is_file():
                continue
            raw = path.read_bytes()
            value = json.loads(raw)
            changed = _rewrite(value, replacements)
            if changed != value and not validate_only:
                saved = backup / path.relative_to(meta)
                saved.parent.mkdir(parents=True, exist_ok=True)
                if not saved.exists():
                    saved.write_bytes(raw)
                temporary = path.with_name(path.name + ".organize.tmp")
                with temporary.open("w", encoding="utf-8") as stream:
                    json.dump(changed, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(path)


def execute(plan, job, cancelled=lambda: False, progress=lambda *_: None):
    root, job = safe_path(plan["target"]), safe_path(job)
    sources = [safe_path(spec["path"]) for spec in plan["sources"]]
    if plan["mode"] not in {"import", "normalize", "quarantine"}:
        raise ValueError("不支持的整理任务")
    if plan["mode"] == "import":
        category_fields(plan.get("category"))
    selected = [r for r in plan["rows"] if r["status"] in {"ready", "quarantine"}]
    if not selected or any(r["status"] == "blocked" for r in plan["rows"]):
        raise ValueError("没有可移动项或存在阻断项，请先核对预览")
    if any(overlaps(path, job) for path in (root, *sources)):
        raise ValueError("任务日志必须放在待整理工程之外")
    job.mkdir(parents=True, exist_ok=True)
    journal = job / "journal.jsonl"
    done = completed_sources(journal)
    started, moved = time.monotonic(), 0
    quarantine = safe_path(plan["quarantine_root"]) if plan.get("quarantine_root") else root
    if quarantine != root and (overlaps(quarantine, root) or any(overlaps(quarantine, source) for source in sources)):
        raise ValueError("隔离目录必须在素材与目标工程之外")
    with DatasetLease([root, quarantine, *sources], "organize", owner=plan["id"]) as lease, ExitStack() as stack:
        # Also honour writer locks held by older installed clients.
        legacy_roots = {p for path in (root, *sources) for p in (path, *path.parents) if (p / "标注工程").is_dir()}
        for parent in legacy_roots:
            lock = ProjectLock(parent / "标注工程" / "writer.lock")
            stack.callback(lock.close)
            if not lock.acquired:
                raise OSError("相关工程仍在标注，请保存并暂停后再整理")
        manifest_keys = set()
        for row in selected:
            source, target = safe_path(row["source"]), safe_path(row["target"])
            destination_root = quarantine if row.get("operation") == "quarantine" else root
            if not target.is_relative_to(destination_root) or not any(source == base or source.is_relative_to(base) for base in sources):
                raise ValueError("计划路径超出选择的来源或目标范围")
            if source == target or source.name in PROTECTED_NAMES or source.name.endswith(".标注.json") or any(p in PROTECTED for p in source.parts):
                raise ValueError("计划包含受保护文件或原地覆盖")
            if target.exists():
                if source.exists() or identity(target) != row["identity"]:
                    raise ValueError("目标已有文件或身份不符，未覆盖：" + str(target))
            elif not source.is_file() or identity(source) != row["identity"] or volume(source) != volume(root):
                raise ValueError("源文件已变化或不在同盘，请重新审查：" + str(source))
        check_cancel(cancelled)
        if (root / CONTEXT_FILE).is_file():
            with (root / CONTEXT_FILE).open(encoding="utf-8-sig", newline="") as stream:
                if next(csv.reader(stream), None) != FIELDS:
                    raise ValueError("数据分类表头不兼容，停止整理")
        if plan["mode"] != "quarantine" and (root / "整理清单.csv").exists():
            with (root / "整理清单.csv").open(encoding="utf-8-sig", newline="") as stream:
                if next(csv.reader(stream), None) != MANIFEST_FIELDS:
                    raise ValueError("整理清单表头不同，停止执行")
        if plan["mode"] == "normalize":
            relocate_metadata(plan, job, validate_only=True)
        marked = False
        root.mkdir(parents=True, exist_ok=True)
        if plan["mode"] != "quarantine":
            for name in ("九轴", *VIEWS):
                safe_path(root / name).mkdir(exist_ok=True)
            for name, fields in TABLES.items():
                path = safe_path(root / name)
                if not path.exists():
                    with path.open("x", encoding="utf-8", newline="") as stream:
                        csv.writer(stream).writerow(fields.split(","))
            with (root / "整理清单.csv").open(encoding="utf-8-sig", newline="") as stream:
                if next(csv.reader(stream)) != MANIFEST_FIELDS:
                    raise ValueError("整理清单表头不同，停止执行")
            with (root / "整理清单.csv").open(encoding="utf-8-sig", newline="") as stream:
                manifest_keys = {(r["source_path"], r["target_relative_path"]) for r in csv.DictReader(stream)}
        for row in selected:
            check_cancel(cancelled)
            source, target = safe_path(row["source"]), safe_path(row["target"])
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                safe_path(target)
                with prevent_writes(source):
                    if identity(source) != row["identity"]:
                        raise ValueError("源文件在执行前变化，已停止")
                    append_journal(journal, {"phase": "intent", **row})
                    if not marked:
                        lease.mark_pending(plan["id"], job)
                        marked = True
                    move_no_replace(source, target)
                    if identity(target) != row["identity"]:
                        raise ValueError("移动后文件身份不一致，已停止并保留文件")
                    append_journal(journal, {"phase": "moved", **row})
            if str(source) not in done:
                if plan["mode"] != "quarantine" and row.get("operation") != "quarantine":
                    key = (row["source"], Path(row["target"]).relative_to(root).as_posix())
                    if key not in manifest_keys:
                        append_manifest(root, row)
                        manifest_keys.add(key)
                append_journal(journal, {"phase": "done", **row})
            moved += 1
            progress(moved, len(selected), str(target))
        if not marked:
            lease.mark_pending(plan["id"], job)
        if plan["mode"] != "quarantine":
            update_context(root, plan)
        if plan["mode"] == "normalize":
            relocate_metadata(plan, job)
            # Only directories made empty by these moves; never recurse-delete.
            for parent in sorted({p for r in selected for p in Path(r["source"]).parents if p.is_relative_to(root) and p != root}, key=lambda p: len(p.parts), reverse=True):
                try:
                    parent.rmdir()
                except OSError:
                    pass
        elif plan["mode"] == "import":
            for source in sources:
                if not source.is_dir():
                    continue
                for folder, _dirs, _files in os.walk(source, topdown=False):
                    path = Path(folder)
                    if path == source or any(p in PROTECTED for p in path.parts):
                        continue
                    safe_path(path)
                    try:
                        path.rmdir()
                    except OSError:
                        pass
        # Existing writer locks above must be released before creating Catalog.
        stack.close()
        if plan["mode"] != "quarantine":
            catalog = Catalog(root, stability_seconds=0, organization_owner=lease.id)
            try:
                if catalog.readonly:
                    raise OSError("素材已移动；索引被其他进程占用，请稍后在标注工具重新核对")
                catalog.scan(fast=True, cancelled=cancelled)
            finally:
                catalog.close()
        append_journal(journal, {"phase": "complete", "count": moved})
        lease.complete(plan["id"])
    return {**plan, "completed": True, "moved": moved, "bytes": sum(r["size"] for r in selected),
            "seconds": round(time.monotonic() - started, 3), "identity_method": "same_volume_file_id_size_mtime",
            "journal": str(journal), "finished_at": now()}
