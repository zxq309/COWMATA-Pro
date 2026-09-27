"""Small persistent progress records and destination-volume media staging."""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from functools import wraps
from pathlib import Path

from . import organization as core
from .storage import atomic_json

PHASES = {"read": "读取原始录像", "convert": "转换 MP4", "verify": "校验视频", "archive": "归档"}
STATES = {"waiting": "等待", "processing": "处理中", "done": "已归档",
          "existing": "已复用", "blocked": "待核对", "paused": "已暂停", "skipped": "范围外"}


SCRATCH_DIR = "COWMATA-归类缓存"
SCRATCH_MIN_FREE = 200 * 1024**3


def scratch_volume(target, avoid=()):
    """Pick a fast local SSD for intermediate media when the archive disk is slow.

    Every recording is written as raw DAV, remuxed, verified and hashed before
    the final MP4 is committed: about six passes over the data. Doing those on
    a USB RAID/HDD target starves it (measured 40-60 MiB/s under load), while
    the same work takes 17 s per recorded hour on local NVMe. Like Frigate or
    Plex transcoders, stage on local SSD and write the verified product once.
    ``COWMATA_DAHUA_SCRATCH`` may name a folder, or ``0`` to keep the target.
    """
    import shutil
    import string

    from .fast_transfer import drive_profile

    override = os.environ.get("COWMATA_DAHUA_SCRATCH", "").strip()
    if override.lower() in {"0", "off", "none"}:
        return None
    if override:
        return Path(override).resolve()
    if os.name != "nt":
        return None
    target_profile = drive_profile(Path(target).resolve().anchor)
    if target_profile["seek_penalty"] is False:
        return None  # The archive disk is already a local SSD.
    avoid_disks = {target_profile["disk"], *avoid} - {None}
    system = os.environ.get("SystemDrive", "C:").upper().rstrip("\\") + "\\"
    candidates = []
    for letter in string.ascii_uppercase:
        anchor = letter + ":\\"
        try:
            if not os.path.isdir(anchor):
                continue
            profile = drive_profile(anchor)
            free = shutil.disk_usage(anchor).free
        except OSError:
            continue
        if profile["seek_penalty"] is not False or profile["disk"] in avoid_disks or free < SCRATCH_MIN_FREE:
            continue
        candidates.append((anchor != system, free, anchor))
    if not candidates:
        return None
    return Path(max(candidates)[2]) / SCRATCH_DIR


def media_root(job):
    import json
    job = Path(job).resolve()
    config = job / "dahua-storage.json"
    if not config.is_file():
        return job
    value = json.loads(config.read_text(encoding="utf-8"))
    root = Path(value["root"]).resolve()
    if value.get("scratch"):
        expected = Path(value["scratch"]).resolve() / "原始录像" / job.name
    else:
        expected = root / ".归类缓存" / "原始录像" / job.name
    if Path(value["media_root"]).resolve() != expected or expected.is_symlink():
        raise ValueError("视频任务暂存路径不合法")
    return expected


def configure_storage(job, root, *, scratch=None):
    job, root = Path(job).resolve(), Path(root).resolve()
    if job == root or job.is_relative_to(root) or root.is_relative_to(job):
        raise ValueError("任务记录与输出目录必须独立")
    scratch = Path(scratch).resolve() if scratch else None
    if scratch is not None and (scratch.is_relative_to(root) or root.is_relative_to(scratch)
                                or scratch.is_relative_to(job) or job.is_relative_to(scratch)):
        raise ValueError("临时缓存目录必须独立于牧场和任务记录")
    legacy = root / ".归类缓存" / "原始录像" / job.name
    expected = scratch / "原始录像" / job.name if scratch else legacy
    # Resolve before creating so junctions cannot redirect media to another disk.
    if expected.resolve() != expected:
        raise ValueError("视频任务暂存目录不能经过链接")
    old = media_root(job)
    saved_root = (json.loads((job / "dahua-storage.json").read_text(encoding="utf-8")).get("root")
                  if (job / "dahua-storage.json").is_file() else None)
    # A resumed job may move its intermediate cache between the farm and a
    # local SSD folder of the same job; another farm or job is refused.
    moved = old.name == job.name and old.parent.name == "原始录像" and old.parent.parent.name == SCRATCH_DIR
    if (saved_root is not None and Path(saved_root).resolve() != root) or (
            old not in {job, expected, legacy} and not moved):
        raise ValueError("恢复任务的输出牧场已变化，请重新扫描")
    expected.mkdir(parents=True, exist_ok=True)
    value = dict(root=str(root), media_root=str(expected))
    if scratch is not None:
        value["scratch"] = str(scratch)
    atomic_json(job / "dahua-storage.json", value, backup=False)
    return expected


def purge_stale_media(folder, keep=()):
    """Delete derived DAV/MP4 left in a previous cache location of this job.

    Only task-owned intermediate media under ``records`` is removed; the
    recorder disk is never written and JSON receipts stay for audit. Rows in
    ``keep`` still have prepared outputs that await their archive commit.
    """
    records = Path(folder) / "records"
    if not records.is_dir() or records.resolve() != records:
        return 0
    removed = 0
    keep = {value[:24] for value in keep}
    for record in records.iterdir():
        if record.name in keep or record.is_symlink() or not record.is_dir():
            continue
        for path in record.rglob("*"):
            if path.is_symlink() or not path.resolve().is_relative_to(records):
                raise ValueError("旧视频缓存路径异常，停止清理")
            if path.is_file() and path.suffix.lower() in {".dav", ".mp4"}:
                path.unlink()
                removed += 1
    return removed


def record_folder(job, source_id, cancelled):
    """Adopt only this job's legacy cache, one record at a time, with verification."""
    import re

    from .catalog import digest_file
    from .fast_transfer import copy_verified
    if not re.fullmatch(r"[a-f0-9]{64}", source_id):
        raise ValueError("原始录像记录标识无效")
    job = Path(job).resolve()
    base = media_root(job)
    folder = base / "records" / source_id[:24]
    if folder.resolve() != folder:
        raise ValueError("视频缓存不能经过链接")
    folder.mkdir(parents=True, exist_ok=True)
    legacy = job / "records" / source_id[:24]
    if legacy != folder and legacy.is_dir():
        # Every resolved source/destination is constrained before moving/deleting.
        for source in sorted(legacy.rglob("*")):
            core.check_cancel(cancelled)
            if source.is_symlink() or not source.resolve().is_relative_to(legacy):
                raise ValueError("旧任务缓存存在链接，停止迁移")
            if not source.is_file():
                continue
            destination = folder / source.relative_to(legacy)
            if not destination.resolve().is_relative_to(folder):
                raise ValueError("旧任务缓存迁移路径越界")
            destination.parent.mkdir(parents=True, exist_ok=True)
            before = core.identity(source)
            with core.prevent_writes(source):
                sha = digest_file(source, cancelled=cancelled)
                if destination.exists():
                    if digest_file(destination, cancelled=cancelled) != sha:
                        raise ValueError("新旧视频缓存内容冲突，已保留两份")
                else:
                    partial = destination.with_name(destination.name + ".adopting")
                    copy_verified(source, partial, sha, cancelled=cancelled)
                    core.move_no_replace(partial, destination)
                if core.identity(source) != before:
                    raise ValueError("旧任务缓存迁移期间发生变化")
                source.unlink()
    return folder


def release_media(job, source_id, *, normalized_only=False):
    """Remove derived media only; original disk/files and small receipts stay."""
    folder = media_root(job) / "records" / source_id[:24]
    if not folder.is_dir() or folder.resolve() != folder:
        return
    for path in folder.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(folder):
            raise ValueError("视频缓存路径异常，停止清理")
        if path.is_file() and path.suffix.lower() in ({".dav"} if normalized_only else {".dav", ".mp4"}):
            path.unlink()


def synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class RunLog:
    def __init__(self, job, selected, mapping, emit):
        self.job, self.emit = Path(job), emit
        self.started = time.monotonic()
        self.rows = {r["id"]: dict(source_id=r["id"], source=r["source"],
            owner=mapping[r["group"]], record_start_ms=r.get("index_start_ms"),
            status="waiting", phase="", file_seconds=0, read_seconds=0,
            convert_seconds=0, verify_seconds=0, archive_seconds=0,
            size=0, targets=[], method="", message="等待处理") for r in selected}
        self._lock = threading.RLock()
        self.local = threading.local()
        self.active = {}
        self.current = None
        self.last_emit = 0.0
        self.last_save = self.started
        self.status = "running"
        self.parallel_workers = None
        self.read_strategy = "pending"
        self.outputs = []
        for row in self.rows.values():
            self.emit(dict(row, event_kind="task_record"))
        self.save()

    @property
    def current(self):
        return self.rows.get(getattr(self.local, "source_id", None))

    @current.setter
    def current(self, value):
        self.local.source_id = value["source_id"] if value else None

    @property
    def record_started(self):
        return self.active[self.current["source_id"]][0]

    @property
    def phase_started(self):
        return self.active[self.current["source_id"]][1]

    @phase_started.setter
    def phase_started(self, value):
        self.active[self.current["source_id"]][1] = value

    @synchronized
    def select(self, source_id):
        self.local.source_id = source_id

    @synchronized
    def snapshot(self, source_id=None):
        original = self.rows[source_id] if source_id is not None else self.current
        row = dict(original, targets=list(original["targets"]))
        timing = self.active.get(row["source_id"])
        if timing and row["status"] in {"processing", "blocked"}:
            row["file_seconds"] = round(time.monotonic() - timing[0], 3)
            if row.get("phase"):
                key = row["phase"] + "_seconds"
                row[key] = round(row.get(key, 0) + time.monotonic() - timing[1], 3)
        return row

    @synchronized
    def pulse(self):
        now = time.monotonic()
        if self.active and now - self.last_emit >= 0.5:
            for source_id in self.active:
                self.emit(dict(self.snapshot(source_id), event_kind="task_record"))
            self.last_emit = now
            if now - self.last_save >= 5:
                self.save(json_only=True)

    @synchronized
    def begin(self, source_id):
        self.select(source_id)
        now = time.monotonic()
        self.active[source_id] = [now, now]
        self.current.update(status="processing", started_at=core.now())
        self.stage("read", "读取与核对原始录像")

    @synchronized
    def stage(self, phase, message="", **details):
        if not self.current:
            return
        now = time.monotonic()
        prior = self.current["phase"]
        if prior:
            key = prior + "_seconds"
            self.current[key] = round(self.current.get(key, 0) + now - self.phase_started, 3)
        self.phase_started = now
        if phase != prior:
            self.current.update(media_percent=0, frames=0, fps=0, media_speed="")
        self.current.update(phase=phase, message=message or PHASES.get(phase, phase), **details)
        self.last_emit = 0
        self.pulse()

    @synchronized
    def archive(self, row):
        if row.get("status") in {"done", "existing"}:
            self.outputs.append({k: v for k, v in row.items() if not k.startswith("_")})
            if self.current:
                self.current["targets"].append(row["target"])
                self.current["size"] += row.get("size", 0)
        elif row.get("status") == "blocked" and self.current:
            self.current.update(status="blocked", message=row.get("message", "归档失败"))
        self.emit(dict(row, event_kind="archive_record"))

    @synchronized
    def finish(self, status, message=""):
        value = self.snapshot()
        value.update(status=status, phase="", finished_at=core.now(),
                     message=message or STATES.get(status, status))
        self.rows[value["source_id"]] = value
        self.current = value
        self.active.pop(value["source_id"], None)
        self.emit(dict(value, event_kind="task_record"))
        # Rewriting every record's report per finished row was O(N²) for a
        # 3000-row task; the report stays at most a few seconds behind, and
        # is always written once no row is running (pause / end).
        if not self.active or time.monotonic() - self.last_save >= 5:
            self.save()
        self.current = None

    @synchronized
    def save(self, status=None, *, json_only=False):
        if status:
            self.status = status
        records = [self.snapshot(r["source_id"]) for r in self.rows.values()]
        report = dict(status=self.status, elapsed_seconds=round(time.monotonic()-self.started, 3),
                      parallel_workers=self.parallel_workers, read_strategy=self.read_strategy,
                      records=records, outputs=self.outputs, updated_at=core.now())
        atomic_json(self.job / "dahua-run.json", report, backup=False, indent=None)
        self.last_save = time.monotonic()
        if json_only:
            return report
        fields = [("状态","status"),("开始时间","started_at"),("视角","owner"),("来源","source"),
                  ("归档目标","targets"),("大小_MiB","size"),("读取_秒","read_seconds"),
                  ("转换_秒","convert_seconds"),("校验_秒","verify_seconds"),("归档_秒","archive_seconds"),
                  ("总耗时_秒","file_seconds"),("处理方式","method"),("说明","message")]
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow([name for name, _ in fields])
        for row in records:
            values = dict(row, status=STATES.get(row["status"], row["status"]),
                          targets=" | ".join(row["targets"]), size=round(row["size"]/1048576, 2))
            writer.writerow([values.get(key, "") for _, key in fields])
        target = self.job / "视频任务记录.csv"
        partial = target.with_suffix(".csv.tmp")
        partial.write_text(buffer.getvalue(), encoding="utf-8-sig", newline="")
        os.replace(partial, target)
        return report
