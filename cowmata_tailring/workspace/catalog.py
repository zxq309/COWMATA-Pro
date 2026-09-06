from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .storage import ProjectLock, atomic_json, read_json

META_DIR = "标注工程"
VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".dav", ".h264", ".h265", ".ts", ".mov"}
EXCLUDE_DIRS = {META_DIR, ".git", ".venv", "__pycache__", "node_modules", "runtime", "dist"}


class SourceBusyError(OSError):
    pass


def assert_not_being_written(path: Path):
    """A paused Explorer copy still holds a write handle, even if size is final."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in {32, 33}:
            raise SourceBusyError("文件有写入占用，可能正在复制；等待写入结束后再检查")
        raise OSError(error, "无法检查文件可读性", str(path))
    kernel.CloseHandle(handle)


def file_stamp(path: Path) -> str:
    stat = path.stat()
    return json.dumps([stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino])


def digest_file(path: Path, *, quick: bool = False) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if quick:
            size = stream.seek(0, 2)
            digest.update(str(size).encode("ascii"))
            # Detection fingerprint only, NEVER used as the stable asset ID.
            for offset in sorted({0, max(0, size // 2 - 32768), max(0, size - 65536)}):
                stream.seek(offset)
                digest.update(stream.read(65536))
        else:
            while block := stream.read(4 * 1024 * 1024):
                digest.update(block)
    return digest.hexdigest()


@dataclass
class ScanResult:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    complete: bool = True
    inspected: int = 0


class Catalog:
    """Immutable SHA-256 assets with versioned locations and cheap reconciliation.

    An unchanged file reuses its OCR. Metadata/fingerprint changes invalidate the
    location immediately; stable, fully hashed, parsed content may be republished.
    Missing source records and their human work are never deleted by a scan.
    """

    def __init__(self, root: Path | str, *, stability_seconds: float = 3.0):
        self.root = Path(root).resolve(strict=True)
        self.meta = self.root / META_DIR
        self.meta.mkdir(exist_ok=True)
        self.lock = ProjectLock(self.meta / "writer.lock")
        self.readonly = not self.lock.acquired
        self.stability_seconds = stability_seconds
        self.mutex = threading.RLock()
        self.recovered_index = None
        index_path = self.meta / "index.sqlite"
        self.db = sqlite3.connect(index_path, timeout=10, check_same_thread=False)
        try:
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Index integrity check failed")
        except sqlite3.DatabaseError as exc:
            self.db.close()
            code = getattr(exc, "sqlite_errorcode", sqlite3.SQLITE_CORRUPT) & 255
            if self.readonly or code not in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
                self.lock.close()
                raise RuntimeError(str(exc)) from exc
            # Quarantine only the rebuildable index. Human work and original
            # recordings are separate and are never removed by this recovery.
            self.recovered_index = self.meta / f"index.corrupt.{time.time_ns()}.sqlite"
            try:
                index_path.rename(self.recovered_index)
                self.db = sqlite3.connect(index_path, timeout=10, check_same_thread=False)
            except OSError:
                self.lock.close()
                raise
        self.db.row_factory = sqlite3.Row
        if not self.readonly:
            # DELETE journal is portable to removable/shared disks; single writer.
            self.db.executescript("""
                PRAGMA journal_mode=DELETE;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS assets(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    metadata TEXT NOT NULL, indexed_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS locations(
                    path TEXT PRIMARY KEY, kind TEXT NOT NULL, stamp TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, asset_id TEXT REFERENCES assets(id),
                    state TEXT NOT NULL, stable_since REAL NOT NULL, last_seen REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT '', attempt_at REAL NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS locations_asset ON locations(asset_id);
                CREATE TABLE IF NOT EXISTS revisions(
                    path TEXT NOT NULL, asset_id TEXT, replaced_at REAL NOT NULL);
            """)
            self.db.commit()

    def _write_check(self):
        if self.readonly:
            raise PermissionError("工程已由另一个窗口打开：当前只读")

    def close(self):
        with self.mutex:
            if self.lock.file.closed:
                return
            self.db.close()
            self.lock.close()

    def settings(self) -> dict:
        return read_json(self.meta / "project.json", {})

    def save_settings(self, settings: dict):
        self._write_check()
        atomic_json(self.meta / "project.json", settings)

    def work_path(self, asset_id: str) -> Path:
        if len(asset_id) != 64 or any(c not in "0123456789abcdef" for c in asset_id):
            raise ValueError("非法素材标识")
        return self.meta / "annotations" / (asset_id + ".json")

    def source_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path.is_relative_to(self.meta):
            raise ValueError("素材路径越出工程目录")
        return path

    def scan(self, *, now: float | None = None, audit: bool = False) -> ScanResult:
        self._write_check()
        now = time.time() if now is None else now
        result = ScanResult()
        found: dict[str, tuple[str, str, str]] = {}
        with self.mutex:
            cached_locations = {r["path"]: dict(r) for r in self.db.execute("SELECT path,stamp,fingerprint FROM locations")}

        def error(exc):
            result.complete = False
            result.errors.append(str(exc))

        if not self.root.is_dir():
            error(OSError("工程根目录暂不可访问；保留原索引，不判定文件删除"))
            return result
        for folder, dirs, files in os.walk(self.root, onerror=error, followlinks=False):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS
                       and not (Path(folder) / d).is_symlink()
                       and not getattr(Path(folder) / d, "is_junction", lambda: False)()]
            result.directories.append(str(folder))
            for filename in files:
                path = Path(folder) / filename
                suffix = path.suffix.lower()
                kind = "imu" if suffix == ".json" else "video" if suffix in VIDEO_SUFFIXES else None
                if kind is None or path.is_symlink():
                    continue
                relative = path.relative_to(self.root).as_posix()
                try:
                    before = file_stamp(path)
                    cached = cached_locations.get(relative)
                    if not audit and cached and cached["stamp"] == before:
                        # Reconcile every path, but do not reread gigabytes of
                        # unchanged media on every periodic directory scan.
                        fingerprint = cached["fingerprint"]
                    else:
                        fingerprint = ("full:" if audit else "quick:") + digest_file(path, quick=not audit)
                    after = file_stamp(path)
                    if before != after:
                        # Keep the file visible but incapable of passing stability.
                        after += ":changing"
                    found[relative] = kind, after, fingerprint
                except OSError as exc:
                    # A file disappearing mid-enumeration is not a complete scan.
                    error(exc)
        result.inspected = len(found)
        with self.mutex, self.db:
            old = {r["path"]: dict(r) for r in self.db.execute("SELECT * FROM locations")}
            for relative, (kind, stamp, fingerprint) in found.items():
                previous = old.get(relative)
                changed = previous is None or previous["stamp"] != stamp
                if previous and previous["fingerprint"].split(":", 1)[0] == fingerprint.split(":", 1)[0]:
                    changed |= previous["fingerprint"] != fingerprint
                if audit and previous and previous["asset_id"]:
                    changed |= fingerprint != "full:" + previous["asset_id"]
                if previous and previous["state"] == "missing":
                    changed = True
                if changed:
                    if previous:
                        result.changed.append(relative)
                        if previous["asset_id"]:
                            self.db.execute("INSERT INTO revisions VALUES(?,?,?)",
                                            (relative, previous["asset_id"], now))
                    else:
                        result.added.append(relative)
                    self.db.execute("""INSERT OR REPLACE INTO locations
                        (path,kind,stamp,fingerprint,asset_id,state,stable_since,last_seen,error,attempt_at)
                        VALUES(?,?,?,?,NULL,'pending',?,?,'等待文件稳定及可读性检查',0)""",
                                    (relative, kind, stamp, fingerprint, now, now))
                else:
                    self.db.execute("UPDATE locations SET last_seen=?,fingerprint=? WHERE path=?",
                                    (now, fingerprint, relative))
            if result.complete:
                for relative, previous in old.items():
                    if relative not in found and previous["state"] != "missing":
                        result.missing.append(relative)
                        self.db.execute("UPDATE locations SET state='missing',error='源文件已不在原位置' WHERE path=?",
                                        (relative,))
        return result

    def rows(self, *, kind: str | None = None) -> list[dict]:
        with self.mutex:
            rows = self.db.execute("""SELECT l.*,a.metadata FROM locations l
                LEFT JOIN assets a ON l.asset_id=a.id ORDER BY l.path""").fetchall()
        return [{**dict(r), "metadata": json.loads(r["metadata"] or "{}")}
                for r in rows if kind is None or r["kind"] == kind]

    def pending(self, *, now: float | None = None, retry_seconds: float = 60) -> list[dict]:
        now = time.time() if now is None else now
        return [r for r in self.rows() if r["state"] in {"pending", "invalid"}
                and now - r["stable_since"] >= self.stability_seconds
                and now - r["attempt_at"] >= retry_seconds
                and not r["stamp"].endswith(":changing")]

    def index_one(self, relative: str, inspect, *, now: float | None = None) -> dict | None:
        """Hash + inspect outside DB lock; publish iff the source is still identical."""
        self._write_check()
        now = time.time() if now is None else now
        with self.mutex:
            row = self.db.execute("SELECT * FROM locations WHERE path=?", (relative,)).fetchone()
        if row is None or row["state"] == "missing":
            return None
        path = self.source_path(relative)
        try:
            before = file_stamp(path)
            if before != row["stamp"] or now - row["stable_since"] < self.stability_seconds:
                return None
            assert_not_being_written(path)
            asset_id = digest_file(path)
            with self.mutex:
                cached = self.db.execute("SELECT metadata FROM assets WHERE id=?", (asset_id,)).fetchone()
            metadata = json.loads(cached[0]) if cached else {}
            if not metadata or metadata.get("recheck"):
                metadata = inspect(path, row["kind"], asset_id)
            if not isinstance(metadata, dict):
                raise ValueError("素材检查未返回有效结果")
            correction = read_json(self.meta / "video_corrections" / (asset_id + ".json"), None)
            if correction and correction.get("asset_id") == asset_id:
                metadata["manual_readings"] = correction.get("readings", [])
                metadata["roi"] = correction.get("roi")
                if correction.get("camera"):
                    metadata["camera"] = correction["camera"]
                if correction.get("readings"):
                    metadata["intervals"] = correction["intervals"]
                    metadata["needs_review"] = len(correction["readings"]) < 2
            if file_stamp(path) != before:
                raise SourceBusyError("读取期间文件变化；等待复制完成后重试")
            state = "ignored" if metadata.get("ignored") else "review" if metadata.get("needs_review") else "ready"
            with self.mutex, self.db:
                current = self.db.execute("SELECT stamp,state FROM locations WHERE path=?", (relative,)).fetchone()
                if current is None or current[0] != before or current[1] == "missing":
                    return None
                self.db.execute("INSERT INTO assets VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,indexed_at=excluded.indexed_at",
                                (asset_id, row["kind"], json.dumps(metadata, ensure_ascii=False), now))
                self.db.execute("UPDATE locations SET asset_id=?,state=?,error='',attempt_at=? WHERE path=?",
                                (asset_id, state, now, relative))
            return {"asset_id": asset_id, "path": relative, "state": state, "metadata": metadata}
        except SourceBusyError as exc:
            with self.mutex, self.db:
                self.db.execute("""UPDATE locations SET state='pending',error=?,stable_since=?,attempt_at=0
                    WHERE path=? AND stamp=? AND state!='missing'""", (str(exc), now, relative, row["stamp"]))
            return None
        except (OSError, ValueError, RuntimeError) as exc:
            with self.mutex, self.db:
                self.db.execute("""UPDATE locations SET state='invalid',error=?,attempt_at=?
                    WHERE path=? AND stamp=? AND state!='missing'""", (str(exc), now, relative, row["stamp"]))
            return None

    def queue_ocr_upgrade(self, signature: str) -> int:
        """Recheck derived OCR once per algorithm; preserve manual clocks/work."""
        self._write_check()
        queued = 0
        with self.mutex, self.db:
            for row in self.rows(kind="video"):
                metadata = row["metadata"]
                if row["state"] not in {"ready", "review"} or metadata.get("ocr_engine") == signature:
                    continue
                # Two explicit human anchors remain authoritative, including
                # historical projects whose generated OCR engine is older.
                if len(metadata.get("manual_readings", [])) >= 2:
                    continue
                self.db.execute("UPDATE assets SET metadata=json_set(metadata,'$.recheck',1) WHERE id=?", (row["asset_id"],))
                self.db.execute("UPDATE locations SET state='pending',attempt_at=0,error=? WHERE path=?",
                                ("OCR 算法已升级，等待复核；人工标签和校准记录保留", row["path"]))
                queued += 1
        return queued

    def recheck(self, relative: str):
        self._write_check()
        with self.mutex, self.db:
            row = self.db.execute("SELECT asset_id FROM locations WHERE path=?", (relative,)).fetchone()
            if row and row[0]:
                # Invalidate derived metadata, not identity or any annotation.
                self.db.execute("UPDATE assets SET metadata=json_set(metadata,'$.recheck',1) WHERE id=?", (row[0],))
            self.db.execute("UPDATE locations SET state='pending',attempt_at=0 WHERE path=? AND state!='missing'", (relative,))

    def update_metadata(self, asset_id: str, metadata: dict):
        self._write_check()
        with self.mutex, self.db:
            self.db.execute("UPDATE assets SET metadata=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), asset_id))
            self.db.execute("UPDATE locations SET state=? WHERE asset_id=? AND state IN ('ready','review')",
                            ("review" if metadata.get("needs_review") else "ready", asset_id))
