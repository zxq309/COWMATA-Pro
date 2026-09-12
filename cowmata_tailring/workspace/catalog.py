from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .dataset_access import DatasetLease
from .storage import ProjectLock, atomic_json, read_json

META_DIR = "标注工程"
VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".dav", ".h264", ".h265", ".ts", ".mov"}
EXCLUDE_DIRS = {META_DIR, ".git", ".venv", "__pycache__", "node_modules", "runtime", "dist"}


def previous_video_files(video,day):
    if not video.is_dir():
        return
    previous=sorted((p for p in video.iterdir() if p.is_dir() and not p.is_symlink()
        and not getattr(p,'is_junction',lambda:False)() and re.fullmatch(r'\d{4}-\d{2}-\d{2}',p.name) and p.name<day),reverse=True)
    if not previous:
        return
    for view in previous[0].iterdir():
        if not view.is_dir() or view.is_symlink() or getattr(view,'is_junction',lambda:False)():
            continue
        files=sorted((p for p in view.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in VIDEO_SUFFIXES),key=lambda p:p.name)
        if files:
            yield files[-1]


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


def bind_location_metadata(metadata, stamp):
    """Bind content-level timing to a SHA-validated location, not old mtime.

    Call only after location identity validation. Consumers still check this
    location's complete stamp before decoding; this does not hash or trust files.
    """
    timeline = metadata.get("timeline") or {}
    if timeline.get("native"):
        size, mtime = json.loads(stamp)[:2]
        if timeline["native"]["source_size"] != size:
            raise ValueError("Native index source size disagrees with validated location")
        return {**metadata, "timeline": {**timeline, "source": {**timeline.get("source", {}), "size": size, "mtimeNs": mtime},
                "native": {**timeline["native"], "source_mtime_ns": mtime}}}
    return metadata


def digest_file(path: Path, *, quick: bool = False, cancelled=None) -> str:
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
                if cancelled and cancelled():
                    raise InterruptedError("内容核验已暂停")
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

    def __init__(self, root: Path | str, *, stability_seconds: float = 3.0,
                 load_session: bool = False, meta_path: Path | None = None, organization_owner=None, day=None):
        from .resource_layout import resource_context
        self.root = resource_context(Path(root).resolve(strict=True))
        if not self.root.is_dir():
            raise ValueError("请选择工程文件夹")
        self.day = day
        if day:
            from datetime import date
            date.fromisoformat(day)
            if not (self.root/'Motion'/day).is_dir():
                raise ValueError('所选日期没有九轴目录')
        self._extra_day_paths = set()
        # The full resource registry can contain large video packet indexes.
        # It is not needed to open a daily catalog or recover human work.
        self.resource_index = {}
        self._resource_records = None
        self.meta = Path(meta_path).resolve() if meta_path else self.root / META_DIR
        self.dated_annotations=bool(day or read_json(self.meta/'annotation-layout.json',{}).get('schema')=='dated-annotations-v1')
        if self.meta.is_symlink() or getattr(self.meta, "is_junction", lambda: False)():
            raise ValueError("标注工程目录不能是指向其他位置的链接")
        new = not self.meta.exists()
        self.source_lease = DatasetLease([self.root], owner=organization_owner)
        try:
            self.meta.mkdir(exist_ok=True)
            self.lock = ProjectLock(self.meta / "writer.lock")
        except Exception:
            self.source_lease.close()
            raise
        self.readonly = not self.lock.acquired
        self.stability_seconds = stability_seconds
        self.mutex = threading.RLock()
        self.load_pending = False
        try:
            if load_session and not self.readonly:
                marker = self.meta / ".load-pending.json"
                if new:
                    atomic_json(marker, {"owner": "cowmata-project-load-v1"})
                self.load_pending = read_json(marker, {}).get("owner") == "cowmata-project-load-v1"
            self._open_index()
        except Exception:
            if getattr(self, "db", None):
                self.db.close()
            self.lock.close()
            self.source_lease.close()
            self._cleanup_pending()
            raise

    def _open_index(self):
        self.recovered_index = None
        index_path = self.meta / "index.sqlite"
        self.db = sqlite3.connect(index_path, timeout=10, check_same_thread=False)
        try:
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Index integrity check failed")
        except sqlite3.DatabaseError as exc:
            self.db.close()
            # Python 3.10 exposes neither result-code constants nor errorcode.
            # SQLite's primary CORRUPT / NOTADB codes are 11 / 26. In the older
            # API, accept only its known corruption messages, never I/O/locks.
            code = getattr(exc, "sqlite_errorcode", None)
            corrupt = (code & 255 in {11, 26}) if code is not None else str(exc) in {
                "file is not a database", "database disk image is malformed", "Index integrity check failed"}
            if self.readonly or not corrupt:
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
                BEGIN IMMEDIATE;
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
                CREATE TABLE IF NOT EXISTS video_hints(
                    path TEXT PRIMARY KEY, stamp TEXT NOT NULL,
                    metadata TEXT NOT NULL);
                COMMIT;
            """)
            self.db.commit()
            # Re-probe only legacy IMU metadata. Human work and expensive video
            # OCR remain untouched; source identities remain content hashes.
            with self.db:
                query="SELECT id,metadata FROM assets WHERE kind='imu'"
                parameters=()
                if self.day:
                    query+=' AND id IN (SELECT asset_id FROM locations WHERE path LIKE ?)'
                    parameters=('Motion/'+self.day+'/%',)
                for row in self.db.execute(query,parameters).fetchall():
                    metadata = json.loads(row["metadata"])
                    if not metadata.get("ignored") and metadata.get("capture_timing", {}).get("revision") != 1:
                        metadata["recheck"] = True
                        self.db.execute("UPDATE assets SET metadata=? WHERE id=?", (json.dumps(metadata), row["id"]))
                        self.db.execute("UPDATE locations SET state='pending',attempt_at=0 WHERE asset_id=? AND state IN ('ready','review')", (row["id"],))

    def _write_check(self):
        if self.readonly:
            raise PermissionError("工程已由另一个窗口打开：当前只读")

    def close(self):
        with self.mutex:
            if self.lock.file.closed:
                return
            self.db.close()
            self.lock.close()
            self._cleanup_pending()
            self.source_lease.close()

    def finish_load(self):
        """Publish a usable project; failed new loads remain disposable."""
        if self.load_pending and not self.readonly:
            (self.meta / ".load-pending.json").unlink(missing_ok=True)
            self.load_pending = False

    def _cleanup_pending(self):
        if not self.load_pending or self.readonly:
            return
        root = self.meta.resolve()
        # Human work and unknown files are never classified as load debris.
        if any((root / name).exists() for name in ("annotations", "video_corrections", "evidence", "exports")):
            return
        owned = {"index.sqlite", "index.sqlite-journal", "index.sqlite-wal", "index.sqlite-shm",
                 "writer.lock", "ocr_profiles.json", "ocr_profiles.json.bak"}
        failed = False
        for folder in (root / "previews", root):
            if not folder.exists() or folder.is_symlink() or getattr(folder, "is_junction", lambda: False)():
                continue
            for path in folder.iterdir():
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                    continue
                generated = (bool(re.fullmatch(r"[0-9a-f]{64}\.jpg", path.name)) if folder != root else
                             path.name in owned or bool(re.fullmatch(r"ocr_profiles\.json\..+\.tmp", path.name)))
                if generated:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        failed = True
            try:
                folder.rmdir()  # Empty directories only; never recursive deletion.
            except OSError:
                pass
        if not failed:
            try:
                # Delete the recovery marker last, so an interrupted cleanup
                # can be retried after a file lock or process crash clears.
                (root / ".load-pending.json").unlink(missing_ok=True)
                root.rmdir()
            except OSError:
                pass

    def settings(self) -> dict:
        settings=read_json(self.meta / "project.json", {})
        if self.day:
            daily=read_json(self.settings_path(),None)
            if daily is not None:
                settings.update(daily)
            elif settings.get('active_day')!=self.day:
                for key in ('active_event','current_path','current_asset','reference_ms'):
                    settings.pop(key,None)
        return settings

    def settings_path(self):
        return self.meta/'.会话'/(self.day+'.json') if self.day else self.meta/'project.json'

    def save_settings(self, settings: dict):
        self._write_check()
        atomic_json(self.settings_path(), settings)

    def work_path(self, asset_id: str, *, for_write=False) -> Path:
        if len(asset_id) != 64 or any(c not in "0123456789abcdef" for c in asset_id):
            raise ValueError("非法素材标识")
        legacy=self.meta / "annotations" / (asset_id + ".json")
        if self.dated_annotations:
            from .annotation_store import dated_path
            with self.mutex:
                rows=self.db.execute('SELECT path FROM locations WHERE asset_id=? AND kind=\'imu\' ORDER BY path',(asset_id,)).fetchall()
            for row in sorted(rows,key=lambda r:not self.in_scope(r['path'])):
                destination=dated_path(self.meta,row['path'])
                if destination is not None:
                    return legacy if not for_write and not destination.exists() and legacy.exists() else destination
        return legacy

    def saved_work_assets(self):
        if not self.dated_annotations:
            return {p.stem for p in (self.meta/'annotations').glob('*.json') if len(p.stem)==64}
        with self.mutex:
            assets={r[0] for r in self.db.execute('SELECT asset_id,path FROM locations WHERE kind=\'imu\' AND asset_id IS NOT NULL') if self.in_scope(r[1])}
        return {asset for asset in assets if self.work_path(asset).is_file()}

    def source_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path.is_relative_to(self.meta):
            raise ValueError("素材路径越出工程目录")
        return path

    def scan(self, *, now: float | None = None, audit: bool = False, fast: bool = False,
             cancelled=None, progress=None) -> ScanResult:
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
        for folder, dirs, files in self.walk_scope(error):
            if cancelled and cancelled():
                raise InterruptedError("目录清点已取消")
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS
                       and not (Path(folder) / d).is_symlink()
                       and not getattr(Path(folder) / d, "is_junction", lambda: False)()]
            result.directories.append(str(folder))
            for filename in files:
                if cancelled and cancelled():
                    raise InterruptedError("目录清点已取消")
                path = Path(folder) / filename
                if path.is_symlink():
                    continue
                relative = path.relative_to(self.root).as_posix()
                path = self.source_path(relative)
                suffix = path.suffix.lower()
                kind = "imu" if suffix == ".json" else "video" if suffix in VIDEO_SUFFIXES else None
                if kind is None or path.is_symlink():
                    continue
                if "PPG" in Path(relative).parts or path.name == "资源索引.json":
                    continue
                try:
                    before = file_stamp(path)
                    cached = cached_locations.get(relative)
                    if not audit and cached and cached["stamp"] == before:
                        # Reconcile every path, but do not reread gigabytes of
                        # unchanged media on every periodic directory scan.
                        fingerprint = cached["fingerprint"]
                    elif fast and not audit:
                        # Directory reconciliation must not read every video.
                        # This is NOT a content identity; selected assets still
                        # receive full SHA-256 before publishing evidence.
                        fingerprint = "stat:"
                    else:
                        fingerprint = ("full:" if audit else "quick:") + digest_file(path, quick=not audit, cancelled=cancelled)
                    after = file_stamp(path)
                    if before != after:
                        # Keep the file visible but incapable of passing stability.
                        after += ":changing"
                    found[relative] = kind, after, fingerprint
                    if progress and len(found) % 250 == 0:
                        progress(f"正在清点工程目录 · 已发现 {len(found)} 份素材")
                except OSError as exc:
                    # A file disappearing mid-enumeration is not a complete scan.
                    error(exc)
        result.inspected = len(found)
        if cancelled and cancelled():
            raise InterruptedError("目录清点已取消")
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
                    if self.in_scope(relative) and relative not in found and previous["state"] != "missing":
                        result.missing.append(relative)
                        self.db.execute("UPDATE locations SET state='missing',error='源文件已不在原位置' WHERE path=?",
                                        (relative,))
        return result

    def in_scope(self, relative):
        return not self.day or relative.startswith(('Motion/'+self.day+'/', 'Video/'+self.day+'/')) or relative in self._extra_day_paths

    def walk_scope(self,error):
        if not self.day:
            yield from os.walk(self.root,onerror=error,followlinks=False)
            return
        self._extra_day_paths=set()
        # Enumerate only the chosen day's two material subtrees.
        for kind in ('Motion','Video'):
            directory=self.root/kind/self.day
            if directory.is_dir():
                yield from os.walk(directory,onerror=error,followlinks=False)
        # One last recording per camera from the nearest previous date is a
        # search candidate, never an assumed clock or proven coverage.
        for path in previous_video_files(self.root/'Video',self.day):
            self._extra_day_paths.add(path.relative_to(self.root).as_posix())
            yield str(path.parent),[],[path.name]

    def video_hints(self):
        with self.mutex:
            rows = self.db.execute("""SELECT h.path,h.metadata FROM video_hints h
                JOIN locations l ON l.path=h.path AND l.stamp=h.stamp
                WHERE l.state NOT IN ('missing','ignored')""").fetchall()
        return {r["path"]: json.loads(r["metadata"]) for r in rows if self.in_scope(r['path'])}

    def save_video_hint(self, relative, stamp, metadata):
        self._write_check()
        with self.mutex, self.db:
            row = self.db.execute("SELECT stamp,state FROM locations WHERE path=?", (relative,)).fetchone()
            if row is None or row["stamp"] != stamp or row["state"] == "missing":
                return False
            self.db.execute("INSERT OR REPLACE INTO video_hints VALUES(?,?,?)",
                            (relative, stamp, json.dumps(metadata, ensure_ascii=False)))
        return True

    def rows(self, *, kind: str | None = None) -> list[dict]:
        where,parameters='',[]
        if self.day:
            terms=['l.path LIKE ?','l.path LIKE ?']
            parameters=['Motion/'+self.day+'/%','Video/'+self.day+'/%']
            if self._extra_day_paths:
                terms.append('l.path IN ('+','.join('?' for _ in self._extra_day_paths)+')')
                parameters.extend(sorted(self._extra_day_paths))
            where=' WHERE ('+' OR '.join(terms)+')'
        with self.mutex:
            rows = self.db.execute("""SELECT l.*,a.metadata FROM locations l
                LEFT JOIN assets a ON l.asset_id=a.id"""+where+' ORDER BY l.path',parameters).fetchall()
        return [{**dict(r), "metadata": bind_location_metadata(json.loads(r["metadata"] or "{}"), r["stamp"])
                 if r["state"] in {"ready", "review"} else json.loads(r["metadata"] or "{}")}
                for r in rows if kind is None or r["kind"] == kind]

    def pending(self, *, now: float | None = None, retry_seconds: float = 60, eager=False) -> list[dict]:
        now = time.time() if now is None else now
        return [r for r in self.rows() if r["state"] in {"pending", "invalid"}
                and (eager and os.name == "nt" or now - r["stable_since"] >= self.stability_seconds)
                and now - r["attempt_at"] >= retry_seconds
                and not r["stamp"].endswith(":changing")]

    def index_one(self, relative: str, inspect, *, now: float | None = None, cancelled=None, eager=False) -> dict | None:
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
            if before != row["stamp"] or (now - row["stable_since"] < self.stability_seconds and not (eager and os.name == "nt")):
                return None
            assert_not_being_written(path)
            archived=self.archived_record(relative) if row['kind']=='video' else {}
            known=archived.get('sha256','')
            cached_identity=(archived.get('verified_stamp')==before and len(known)==64
                and all(c in '0123456789abcdef' for c in known))
            asset_id = known if cached_identity else digest_file(path, cancelled=cancelled)
            with self.mutex:
                cached = self.db.execute("SELECT metadata FROM assets WHERE id=?", (asset_id,)).fetchone()
            metadata = json.loads(cached[0]) if cached else {}
            if not metadata and archived.get('sha256')==asset_id:
                from cowmata_tailring.media.native_ps import SIGNATURE
                value=archived.get('metadata',{})
                if value.get('time_engine')==SIGNATURE and not value.get('recheck'):
                    metadata=bind_location_metadata(value,before)
            if not metadata or metadata.get("recheck"):
                previous_camera = metadata.get("camera")
                metadata = inspect(path, row["kind"], asset_id)
                if previous_camera and row["kind"] == "video":
                    metadata["camera"] = previous_camera
            if cancelled and cancelled():
                raise InterruptedError("素材检查已暂停")
            if not isinstance(metadata, dict):
                raise ValueError("素材检查未返回有效结果")
            assert_not_being_written(path)
            if file_stamp(path) != before:
                raise SourceBusyError("读取期间文件变化；等待复制完成后重试")
            with self.mutex, self.db:
                current = self.db.execute("SELECT stamp,state FROM locations WHERE path=?", (relative,)).fetchone()
                if current is None or current[0] != before or current[1] == "missing":
                    return None
                # Read the authoritative human record inside publication lock.
                # A slower background inspector cannot overwrite a newer save.
                correction = read_json(self.meta / "video_corrections" / (asset_id + ".json"), None)
                if correction and correction.get("asset_id") == asset_id:
                    metadata["manual_readings"] = correction.get("readings", [])
                    metadata["roi"] = correction.get("roi")
                    if correction.get("camera"):
                        metadata["camera"] = correction["camera"]
                    if correction.get("readings"):
                        from .clocks import manual_video_metadata
                        if metadata.get('duration_ms') and all('media_ms' in r and 'wall_ms' in r for r in correction['readings']):
                            metadata = manual_video_metadata(metadata,correction['readings'])
                        else:
                            metadata['intervals']=correction.get('intervals',[])
                            metadata['needs_review']=len(correction['readings'])<2
                state = "ignored" if metadata.get("ignored") else "review" if metadata.get("needs_review") else "ready"
                self.db.execute("INSERT INTO assets VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,indexed_at=excluded.indexed_at",
                                (asset_id, row["kind"], json.dumps(metadata, ensure_ascii=False), now))
                self.db.execute("UPDATE locations SET asset_id=?,state=?,error='',attempt_at=? WHERE path=?",
                                (asset_id, state, now, relative))
            return {"asset_id": asset_id, "path": relative, "state": state, "metadata": metadata}
        except SourceBusyError as exc:
            with self.mutex, self.db:
                self.db.execute("""UPDATE locations SET state='pending',error=?,stable_since=?,attempt_at=?
                    WHERE path=? AND stamp=? AND state!='missing'""", (str(exc), now, now if eager else 0, relative, row["stamp"]))
            return None
        except (OSError, ValueError, RuntimeError) as exc:
            with self.mutex, self.db:
                if cancelled and cancelled():
                    self.db.execute("UPDATE locations SET state='pending',error=?,attempt_at=0 WHERE path=? AND stamp=? AND state!='missing'",
                                    ("按需任务已切换，稍后可继续", relative, row["stamp"]))
                    return None
                self.db.execute("""UPDATE locations SET state='invalid',error=?,attempt_at=?
                    WHERE path=? AND stamp=? AND state!='missing'""", (str(exc), now, relative, row["stamp"]))
            return None

    def queue_ocr_upgrade(self, signature: str, *, time_signature=None) -> int:
        """Recheck derived OCR once per algorithm; preserve manual clocks/work."""
        self._write_check()
        queued = 0
        with self.mutex, self.db:
            for row in self.rows(kind="video"):
                metadata = row["metadata"]
                if row["state"] not in {"ready", "review"} or (metadata.get("ocr_engine") == signature and
                        (time_signature is None or metadata.get("time_engine") == time_signature)):
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

    def archived_record(self,relative):
        if self._resource_records is None:
            registry=read_json(self.root/'资源索引.json',{})
            self._resource_records={r['path']:r for r in registry.get('records',[]) if r.get('kind')=='video' and self.in_scope(r.get('path',''))}
        return self._resource_records.get(relative,{})

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
