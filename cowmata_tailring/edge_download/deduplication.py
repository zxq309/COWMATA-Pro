"""Conservative, recoverable duplicate removal for a single download root.

The caller holds RootSyncLock for its entire download cycle and owns the
SQLite connection. No server connection is opened here. A payload fingerprint
alone is deliberately insufficient: every original JSON field participates.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .core import CATEGORIES, CHINA, Cancelled, DownloadError, decoded, validate_payload

LOCK_TIMEOUT_SECONDS = 5.0
MAX_JSON_BYTES = 384 * 1024 * 1024
_TABLE = 'local_motion_duplicates_v1'
_DEVICE = re.compile(r'[0-9A-Fa-f]{12}')
_DAY = re.compile(r'\d{4}-\d{2}-\d{2}')
_COW_FOLDER = re.compile(r'(?:[0-9]{1,5}[A-Za-z0-9]*|[0-9A-Fa-f]{12}|待核对)')
_SCHEMA_VERSION = 1


def _check(cancel):
    if cancel.is_set():
        raise Cancelled()


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _signature(info):
    # Windows volume/file IDs may be unsigned 64-bit or even 128-bit. Keeping
    # them as decimal strings avoids SQLite's signed-64-bit integer boundary.
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, str(info.st_dev), str(info.st_ino))


def _same_file(first, second):
    # CPython's Windows stat/lstat and fstat can expose different ctime
    # semantics (birth time versus change time). Compare identity across APIs;
    # compare timestamps only between two calls to the same API.
    return (stat.S_ISREG(first.st_mode) and stat.S_ISREG(second.st_mode)
            and first.st_dev == second.st_dev and first.st_ino == second.st_ino)


def _same_content_info(first, second):
    return (_same_file(first, second) and first.st_size == second.st_size
            and first.st_mtime_ns == second.st_mtime_ns)


def _safe(root, path, *, missing=False):
    """Check every component without resolving through symlinks/junctions."""
    try:
        relative = path.relative_to(root)
        if '..' in relative.parts:
            return False
        current = root
        for part in (None, *relative.parts):
            if part is not None:
                current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if missing:
                    continue
                return False
            if _linked(info):
                return False
        return True
    except (OSError, ValueError):
        return False


class RootSyncLock:
    """Cancelable, process-wide exclusion across all configs using one root."""
    def __init__(self, root, cancel):
        self.root = Path(os.path.abspath(root))
        self.cancel = cancel
        self.stream = None

    def __enter__(self):
        _check(self.cancel)
        if not self.root.is_dir() or not _safe(self.root, self.root):
            raise DownloadError('保存目录不存在或是链接，无法建立下载互斥锁')
        state = self.root / '.edge-download'
        lock = state / 'sync.lock'
        if not _safe(self.root, lock, missing=True):
            raise DownloadError('下载锁路径包含链接，已停止本轮')
        state.mkdir(exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        fd = os.open(lock, flags, 0o600)
        try:
            if not _safe(self.root, lock) or not _same_file(os.fstat(fd), lock.lstat()):
                raise DownloadError('下载锁在打开时发生变化')
            self.stream = os.fdopen(fd, 'r+b', buffering=0)
            fd = None
            if self.stream.seek(0, os.SEEK_END) == 0:
                self.stream.write(b'\0')
                os.fsync(self.stream.fileno())
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                _check(self.cancel)
                self.stream.seek(0)
                try:
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except (OSError, BlockingIOError) as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DownloadError('此保存目录已有下载任务运行，已停止本轮；不会并行整理重复文件') from exc
                    if self.cancel.wait(min(0.1, remaining)):
                        raise Cancelled() from None
        except BaseException:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            raise
        finally:
            if fd is not None:
                os.close(fd)

    def __exit__(self, exc_type, exc, traceback):
        if self.stream is not None:
            try:
                self.stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DownloadError('JSON 含有重复字段名')
        result[key] = value
    return result


def _constant(value):
    raise DownloadError('JSON 含有非有限数字')


def _cow_display(value):
    original = '' if value is None else str(value)
    code = original.strip()
    match = re.fullmatch(r'([0-9]{1,5})([A-Za-z][A-Za-z0-9]*)?', code)
    if not match or _DEVICE.fullmatch(code):
        return dict(cow_number='', ear_tag='', cow_code_status='pending_review')
    number, tag = match.groups()
    return dict(cow_number=number, ear_tag=(tag or '').upper(),
                cow_code_status='parsed' if tag else 'missing_tag')


def _canonical(data):
    if not isinstance(data, dict):
        raise DownloadError('运动数据必须为 JSON 对象')
    device = data.get('device')
    if not isinstance(device, str) or not _DEVICE.fullmatch(device):
        raise DownloadError('记录缺少完整设备编号')
    stamp = data.get('create_time')
    if (isinstance(stamp, bool) or not isinstance(stamp, int | str)
            or not re.fullmatch(r'[0-9]+', str(stamp)) or int(stamp) <= 0):
        raise DownloadError('记录缺少精确毫秒采集时间')
    if type(data.get('version')) not in (int, str):
        raise DownloadError('IMU 版本格式无效')
    identities = []
    for name in ('cow_id', 'animal_number', 'animalNumber'):
        value = data.get(name)
        if value is not None and type(value) not in (str, int):
            raise DownloadError('历史牛号字段无效')
        if value is not None and str(value).strip():
            identities.append(str(value).strip().casefold())
    if len(set(identities)) > 1:
        raise DownloadError('记录的历史牛号互相冲突')
    validate_payload(data, 'motion')
    normalized = dict(data)
    normalized['imu'] = base64.b64encode(decoded(data, 'imu', True)).decode('ascii')
    # Removing an original source field, an empty identity or a confidence flag
    # would hide potentially important differences. Only these display fields
    # may be absent/present, and only when their exact value can be recomputed.
    if 'cow_id' in data:
        for field, expected in _cow_display(data['cow_id']).items():
            if field in data:
                if type(data[field]) is not str or data[field] != expected:
                    raise DownloadError('派生牛号或耳标字段不一致，保留待核对')
                normalized.pop(field)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return encoded, int(stamp)


@contextmanager
def _reader(path):
    """On Windows keep the survivor unwritable/unremovable while recycling."""
    if os.name == 'nt':
        import ctypes
        import msvcrt
        from ctypes import wintypes
        create = ctypes.WinDLL('kernel32', use_last_error=True).CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, OPEN_REPARSE_POINT.
        handle = create(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            close = ctypes.WinDLL('kernel32', use_last_error=True).CloseHandle
            close.argtypes = (wintypes.HANDLE,)
            close(handle)
            raise
    else:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        yield stream


@dataclass(frozen=True)
class _Snapshot:
    path: Path
    signature: tuple
    raw_sha256: str
    semantic_sha256: str | None
    create_time: int | None
    canonical: bytes | None = None


def _move_noreplace(source, destination):
    """Use same-volume moves, never overwrite either an original or backup."""
    if os.name == 'nt':
        os.rename(source, destination)
    else:
        os.link(source, destination, follow_symlinks=False)
        try:
            source.unlink()
        except BaseException:
            destination.unlink()
            raise


class DuplicateCatalog:
    """Reusable local index; caller owns the borrowed SQLite connection.

    refresh() discovers old files before contacting a server and recycles exact
    semantic duplicates. find_equivalent() always revalidates the actual file.
    This class does not commit or close the caller's transaction/connection.
    """
    def __init__(self, job, sqlite_connection, cancel, log=lambda message: None):
        self.job, self.db, self.cancel, self.log = job, sqlite_connection, cancel, log
        self.root = Path(os.path.abspath(job.farm))
        if job.category not in CATEGORIES or not _safe(self.root, self.root):
            raise DownloadError('重复检查的保存目录或类别无效')
        self.category = Path(job.category).parts
        self.batch = None
        self.db.execute(f'CREATE TABLE IF NOT EXISTS {_TABLE} ('
                        'path TEXT PRIMARY KEY,size INTEGER,mtime INTEGER,ctime INTEGER,'
                        'dev TEXT,ino TEXT,raw_sha256 TEXT,semantic_sha256 TEXT,'
                        'create_time INTEGER,schema_version INTEGER)')
        self.db.execute(f'CREATE INDEX IF NOT EXISTS {_TABLE}_semantic ON {_TABLE}(semantic_sha256)')

    def _relative(self, path):
        return path.relative_to(self.root).as_posix()

    def _day(self, text):
        try:
            return bool(_DAY.fullmatch(text)) and bool(date.fromisoformat(text))
        except ValueError:
            return False

    def _allowed(self, path):
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            return False
        if not parts or path.suffix.lower() != '.json' or path.name.startswith('.'):
            return False
        tail = parts[len(self.category):] if parts[:len(self.category)] == self.category else ()
        if (len(tail) == 4 and tail[0] == 'Motion' and self._day(tail[1])
                and re.fullmatch(r'[0-9A-Fa-f]{12}(?:-.+)?', tail[2])):
            return True
        if self.job.category != '未分类':
            return False
        if (len(parts) == 4 and parts[0] == 'Motion' and self._day(parts[1])
                and re.fullmatch(r'[0-9A-Fa-f]{12}(?:-.+)?', parts[2])):
            return True
        if (len(parts) == 4 and _DEVICE.fullmatch(parts[0])
                and self._day(parts[1]) and parts[2] == 'motion'):
            return True
        return (len(parts) == 5 and bool(_COW_FOLDER.fullmatch(parts[0]))
                and bool(_DEVICE.fullmatch(parts[1])) and self._day(parts[2])
                and parts[3] == 'motion')

    def _children(self, folder, directories):
        if not _safe(self.root, folder) or not folder.is_dir():
            return []
        result = []
        try:
            for child in folder.iterdir():
                _check(self.cancel)
                info = child.lstat()
                if _linked(info):
                    continue
                if (stat.S_ISDIR(info.st_mode) if directories else stat.S_ISREG(info.st_mode)):
                    result.append(child)
        except OSError as exc:
            self.log(f'重复检查暂未读取目录：{folder.name}（{exc}）')
        return sorted(result, key=lambda p: p.name)

    def _paths(self):
        roots = [self.root.joinpath(*self.category, 'Motion')]
        if self.job.category == '未分类':
            roots.append(self.root / 'Motion')
        for motion in roots:
            for day in self._children(motion, True):
                if self._day(day.name):
                    for subject in self._children(day, True):
                        if re.fullmatch(r'[0-9A-Fa-f]{12}(?:-.+)?', subject.name):
                            yield from self._children(subject, False)
        if self.job.category == '未分类':
            for first in self._children(self.root, True):
                if _DEVICE.fullmatch(first.name):
                    for day in self._children(first, True):
                        if self._day(day.name):
                            yield from self._children(day / 'motion', False)
                if _COW_FOLDER.fullmatch(first.name):
                    for device in self._children(first, True):
                        if _DEVICE.fullmatch(device.name):
                            for day in self._children(device, True):
                                if self._day(day.name):
                                    yield from self._children(day / 'motion', False)

    def _in_range(self, stamp):
        try:
            return stamp is not None and self.job.start <= datetime.fromtimestamp(stamp / 1000, CHINA) < self.job.end
        except (ValueError, OSError, OverflowError, TypeError):
            return False

    def _snapshot(self, path, stream=None):
        _check(self.cancel)
        if not _safe(self.root, path) or not path.is_file():
            return None
        if stream is None:
            with _reader(path) as opened:
                return self._snapshot(path, opened)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_JSON_BYTES or before.st_nlink > 1:
            return None
        signature = _signature(before)
        handle_before = os.fstat(stream.fileno())
        if not _same_content_info(handle_before, before):
            return None
        stream.seek(0)
        chunks = []
        total = 0
        while True:
            _check(self.cancel)
            block = stream.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_JSON_BYTES:
                return None
            chunks.append(block)
        raw = b''.join(chunks)
        if (total != before.st_size or not _safe(self.root, path)
                or _signature(path.lstat()) != signature
                or _signature(os.fstat(stream.fileno())) != _signature(handle_before)):
            return None
        try:
            data = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
            canonical, stamp = _canonical(data)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            canonical, stamp = None, None
        return _Snapshot(path, signature, hashlib.sha256(raw).hexdigest(),
                         hashlib.sha256(canonical).hexdigest() if canonical is not None else None,
                         stamp, canonical)

    def _store(self, snapshot):
        self.db.execute(f'INSERT OR REPLACE INTO {_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (self._relative(snapshot.path), *snapshot.signature, snapshot.raw_sha256,
                         snapshot.semantic_sha256, snapshot.create_time, _SCHEMA_VERSION))

    def _remember(self, path, cached=False):
        path = Path(os.path.abspath(path))
        if not self._allowed(path) or not _safe(self.root, path) or not path.is_file():
            return None
        relative = self._relative(path)
        try:
            info = path.lstat()
            if cached:
                row = self.db.execute(f'SELECT size,mtime,ctime,dev,ino,raw_sha256,semantic_sha256,'
                                      f'create_time,schema_version FROM {_TABLE} WHERE path=?',
                                      (relative,)).fetchone()
                if row and row[:5] == _signature(info) and row[8] == _SCHEMA_VERSION:
                    return _Snapshot(path, row[:5], row[5], row[6], row[7])
            snapshot = self._snapshot(path)
            if snapshot is not None:
                self._store(snapshot)
            else:
                self.db.execute(f'DELETE FROM {_TABLE} WHERE path=?', (relative,))
            return snapshot
        except OSError as exc:
            self.log(f'重复检查保留文件：{relative}（{exc}）')
            return None

    def remember(self, path, data=None):
        # A supplied Python object does not prove what reached the disk.
        self._remember(path)

    def _completed_paths(self):
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(completed)')}
        if 'path' not in columns:
            return set()
        return {Path(row[0]).as_posix() for row in self.db.execute('SELECT path FROM completed') if row[0]}

    def _candidates(self, semantic):
        indexed = self._completed_paths()
        rows = self.db.execute(f'SELECT path FROM {_TABLE} WHERE semantic_sha256=?', (semantic,)).fetchall()
        paths = [self.root / row[0] for row in rows]
        return sorted((p for p in paths if self._allowed(p)),
                      key=lambda p: (self._relative(p) not in indexed, self._relative(p)))

    def refresh(self):
        _check(self.cancel)
        groups = {}
        indexed = self._completed_paths()
        for path in self._paths():
            _check(self.cancel)
            snapshot = self._remember(path, cached=True)
            if snapshot is not None and snapshot.semantic_sha256 and self._in_range(snapshot.create_time):
                groups.setdefault(snapshot.semantic_sha256, []).append(snapshot.path)
        recycled = 0
        for paths in groups.values():
            if len(paths) < 2:
                continue
            paths.sort(key=lambda p: (self._relative(p) not in indexed, self._relative(p)))
            keep, *others = paths
            for duplicate in others:
                recycled += self._recycle_one(keep, duplicate)
        return recycled

    def find_equivalent(self, data):
        _check(self.cancel)
        try:
            canonical, stamp = _canonical(data)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            return None
        if not self._in_range(stamp):
            return None
        semantic = hashlib.sha256(canonical).hexdigest()
        for path in self._candidates(semantic):
            snapshot = self._remember(path)
            if snapshot is not None and snapshot.canonical == canonical and self._in_range(snapshot.create_time):
                return path, snapshot.raw_sha256
        return None

    def recycle_equivalents(self, keep_path, data):
        _check(self.cancel)
        try:
            canonical, stamp = _canonical(data)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            return 0
        keep_path = Path(os.path.abspath(keep_path))
        if not self._in_range(stamp) or not self._allowed(keep_path):
            return 0
        keep = self._remember(keep_path)
        if keep is None or keep.canonical != canonical:
            return 0
        return sum(self._recycle_one(keep_path, path) for path in self._candidates(keep.semantic_sha256)
                   if path != keep_path)

    def _recycle_destination(self, source):
        if self.batch is None:
            self.batch = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex
        batch = self.root / '.edge-download' / 'recycle' / self.batch
        destination = batch / source.relative_to(self.root)
        if not _safe(self.root, destination, missing=True):
            raise DownloadError('回收目录包含链接')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise DownloadError('回收目标已存在，保留活动副本')
        return batch / 'recovery.jsonl', destination

    def _journal(self, journal, event):
        if not _safe(self.root, journal, missing=True):
            raise DownloadError('恢复日志路径包含链接')
        encoded = (json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        with os.fdopen(os.open(journal, flags, 0o600), 'ab') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    def _rewire(self, duplicate, keep):
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(completed)')}
        if 'path' in columns:
            rows = self.db.execute('SELECT DISTINCT path FROM completed').fetchall()
            for row in rows:
                if row[0] and Path(row[0]).as_posix() == self._relative(duplicate):
                    if 'sha256' in columns:
                        self.db.execute('UPDATE completed SET path=?,sha256=? WHERE path=?',
                                        (self._relative(keep.path), keep.raw_sha256, row[0]))
                    else:
                        self.db.execute('UPDATE completed SET path=? WHERE path=?',
                                        (self._relative(keep.path), row[0]))
        self.db.execute(f'DELETE FROM {_TABLE} WHERE path=?', (self._relative(duplicate),))
        self._store(keep)

    def _recycle_one(self, keep_path, duplicate):
        _check(self.cancel)
        if keep_path == duplicate or not self._allowed(keep_path) or not self._allowed(duplicate):
            return 0
        moved, savepoint = False, False
        destination = journal = event = None
        try:
            if not _safe(self.root, keep_path) or not _safe(self.root, duplicate):
                return 0
            with _reader(keep_path) as survivor:
                keep = self._snapshot(keep_path, survivor)
                old = self._snapshot(duplicate)
                if (keep is None or old is None or keep.canonical is None
                        or keep.canonical != old.canonical
                        or not self._in_range(keep.create_time) or not self._in_range(old.create_time)):
                    return 0
                journal, destination = self._recycle_destination(duplicate)
                event = dict(job=self.batch, operation=uuid.uuid4().hex, state='prepared',
                             source=self._relative(duplicate), recycled=self._relative(destination),
                             keep=self._relative(keep_path), sha256=old.raw_sha256,
                             keep_sha256=keep.raw_sha256, semantic_sha256=keep.semantic_sha256,
                             time_utc=datetime.now(timezone.utc).isoformat())
                self._journal(journal, event)
                # A write, rename or editor save between indexing and deletion
                # must never remove the sole remaining original.
                fresh_keep = self._snapshot(keep_path, survivor)
                fresh_old = self._snapshot(duplicate)
                if (fresh_keep is None or fresh_old is None
                        or fresh_keep.raw_sha256 != keep.raw_sha256
                        or fresh_old.raw_sha256 != old.raw_sha256):
                    return 0
                _check(self.cancel)
                self.db.execute('SAVEPOINT recycle_duplicate')
                savepoint = True
                _move_noreplace(duplicate, destination)
                moved = True
                actual = self._snapshot(destination)
                after_keep = self._snapshot(keep_path, survivor)
                if (actual is None or actual.raw_sha256 != old.raw_sha256 or after_keep is None
                        or after_keep.raw_sha256 != keep.raw_sha256):
                    raise DownloadError('回收期间文件发生变化，已保留原副本')
                _check(self.cancel)
                self._rewire(duplicate, after_keep)
                self._journal(journal, dict(event, state='recycled'))
                self.db.execute('RELEASE SAVEPOINT recycle_duplicate')
                savepoint = False
            self.log(f'重复副本已移入可恢复回收区：{self._relative(duplicate)}')
            return 1
        except (OSError, ValueError, sqlite3.Error, Cancelled) as exc:
            if savepoint:
                try:
                    self.db.execute('ROLLBACK TO SAVEPOINT recycle_duplicate')
                    self.db.execute('RELEASE SAVEPOINT recycle_duplicate')
                except sqlite3.Error:
                    pass
            if moved:
                try:
                    if not _safe(self.root, duplicate, missing=True):
                        raise DownloadError('原路径变成链接')
                    _move_noreplace(destination, duplicate)
                    outcome = 'restored'
                except (OSError, ValueError) as restore_error:
                    outcome = 'restore_required'
                    self.log(f'原路径已变化；副本仍保存在 {self._relative(destination)}（{restore_error}）')
                try:
                    self._journal(journal, dict(event, state=outcome, reason=str(exc)))
                except (OSError, ValueError):
                    pass
            self.log(f'重复检查未删除原始记录：{self._relative(duplicate)}（{exc}）')
            if isinstance(exc, Cancelled):
                raise
            return 0

    def close(self):
        """Compatibility no-op: the caller owns and closes the SQLite DB."""
