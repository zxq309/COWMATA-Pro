"""Process-wide dataset leases, stored outside scientific data directories."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .storage import ProjectLock, atomic_json


def registry_root():
    return Path(os.environ.get("COWMATA_ACCESS_DIR") or
                Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "COWMATA Annotator" / "dataset-access")


def overlaps(first, second):
    first, second = Path(first).resolve(), Path(second).resolve()
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _locked_registry():
    root = registry_root()
    root.mkdir(parents=True, exist_ok=True)
    lock = ProjectLock(root / "registry.lock")
    if not lock.acquired:
        lock.close()
        raise OSError("数据目录状态正在更新，请稍后重试")
    return root, lock


def _active(root):
    for path in root.glob("*.json"):
        if len(path.stem) != 32:
            continue
        probe = ProjectLock(path.with_suffix(".lock"))
        stale = probe.acquired
        probe.close()
        if stale:
            path.unlink(missing_ok=True)
            path.with_suffix(".lock").unlink(missing_ok=True)
            continue
        try:
            yield json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue


def _check(root, paths, kind, owner=None):
    for path in (root / "pending").glob("*.json"):
        pending = json.loads(path.read_text(encoding="utf-8"))
        if owner not in {pending["task_id"], pending["owner_id"]} and any(overlaps(a, b) for a in paths for b in pending["paths"]):
            raise OSError("此目录有未完成的整理任务，请在数据整理窗口继续原任务：" + pending["job"])
    for lease in _active(root):
        if lease["id"] == owner or kind == lease["kind"] == "annotation":
            continue
        if any(overlaps(a, b) for a in paths for b in lease["paths"]):
            raise OSError("目录正在标注或整理，请先保存并暂停相关工程；其他目录不受影响")


def ensure_available(paths, kind="annotation", owner=None):
    root, lock = _locked_registry()
    try:
        _check(root, paths, kind, owner)
    finally:
        lock.close()


class DatasetLease:
    def __init__(self, paths, kind="annotation", *, owner=None):
        self.id = uuid.uuid4().hex
        self.file_lock = None
        self.path = None
        root, lock = _locked_registry()
        try:
            paths = sorted({str(Path(p).resolve()) for p in paths})
            self.paths = paths
            _check(root, paths, kind, owner)
            self.path = root / (self.id + ".json")
            self.file_lock = ProjectLock(self.path.with_suffix(".lock"))
            if not self.file_lock.acquired:
                raise OSError("无法锁定本次数据整理任务")
            self.path.write_text(json.dumps({"id": self.id, "kind": kind, "paths": paths,
                                             "pid": os.getpid()}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            self.close()
            raise
        finally:
            lock.close()

    def close(self):
        if self.file_lock is not None:
            # Remove the declaration before releasing its handle. The registry
            # reader cannot mistake a partially released lease for an active one.
            if self.path:
                self.path.unlink(missing_ok=True)
            self.file_lock.close()
            if self.path:
                self.path.with_suffix(".lock").unlink(missing_ok=True)
            self.file_lock = None

    def mark_pending(self, task_id, job):
        if len(task_id) != 32 or any(c not in "0123456789abcdef" for c in task_id):
            raise ValueError("Invalid organization task ID")
        root, lock = _locked_registry()
        try:
            directory = root / "pending"
            directory.mkdir(exist_ok=True)
            atomic_json(directory / (task_id + ".json"),
                        {"task_id": task_id, "owner_id": self.id, "job": str(job), "paths": self.paths})
        finally:
            lock.close()

    def complete(self, task_id):
        path = registry_root() / "pending" / (task_id + ".json")
        if path.is_file():
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["owner_id"] != self.id:
                raise OSError("Organization task ownership changed")
            path.unlink()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
