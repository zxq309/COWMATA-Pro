"""Human work is separate from the rebuildable catalog. No source writes."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


class SnapshotWriter:
    """One detached snapshot at a time; explicit saves drain it before writing.

    fsync/backup/replace stay durable, but periodic autosave never runs those
    disk operations on the Qt playback thread. No stale queued snapshots.
    """

    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="annotation-save")
        self.pending = None

    def poll(self):
        if self.pending is not None and self.pending.done():
            self.flush()
        return self.pending is None

    def flush(self):
        if self.pending is not None:
            pending, self.pending = self.pending, None
            pending.result()

    @staticmethod
    def write(snapshot):
        for path, data in snapshot:
            atomic_json(path, data)

    def submit(self, snapshot):
        if not self.poll():
            return False
        self.pending = self.pool.submit(self.write, snapshot)
        return True

    def close(self):
        self.flush()
        self.pool.shutdown(wait=True)


def _replace_with_retry(source, target):
    # Windows readers/antivirus may briefly deny replacement of a closed file.
    # Keep the atomic replacement and bound waiting; persistent errors propagate.
    delays = (0, 0.02, 0.04, 0.08, 0.16)
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == len(delays) - 1:
                raise


def recovery_path(path):
    if path.name.endswith(".标注.json"):
        import hashlib

        root = (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            / "COWMATA Annotator/annotation-recovery"
        )
        return root / (hashlib.sha256(str(path.resolve()).encode()).hexdigest() + ".json")
    return path.with_suffix(path.suffix + ".bak")


def atomic_json(path: Path, value: Any, *, backup=True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if backup and path.is_file():
            # Never promote a corrupt primary over a valid recovery copy.
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                pass
            else:
                recovery = recovery_path(path)
                recovery.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, recovery)
        _replace_with_retry(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        backup = recovery_path(path)
        if not backup.is_file():
            backup = path.with_suffix(path.suffix + ".bak")
        if backup.is_file():
            return json.loads(backup.read_text(encoding="utf-8"))
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        backup = recovery_path(path)
        if not backup.is_file():
            backup = path.with_suffix(path.suffix + ".bak")
        if not backup.is_file():
            raise
        return json.loads(backup.read_text(encoding="utf-8"))


class ProjectLock:
    """OS lock; process death releases it, unlike an unowned stale sentinel."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        self.acquired = False
        if self.file.seek(0, 2) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except OSError:
            pass

    def close(self) -> None:
        if self.file.closed:
            return
        if self.acquired:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


def unique_batch(camera_dir: Path, name: str) -> Path:
    """Create an empty batch only; never merge, overwrite or move recordings."""
    if not name.strip() or name in {".", ".."} or any(c in name for c in '<>:"/\\|?*'):
        raise ValueError("批次名不能为空或包含路径/特殊字符")
    if name.endswith((".", " ")):
        raise ValueError("批次名不能以空格或点结束")
    if name.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        raise ValueError("批次名不能使用 Windows 保留设备名")
    for count in range(1, 10001):
        candidate = camera_dir / (name if count == 1 else f"{name}_{count:02d}")
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise ValueError("同名批次过多，请换一个名称")
