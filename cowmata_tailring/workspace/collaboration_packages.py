"""Offline collaboration, immutable task manifests and fail-closed annotation import."""
from __future__ import annotations

import copy
import errno
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from .catalog import VIDEO_SUFFIXES, SourceBusyError, assert_not_being_written, digest_file, file_stamp
from .dataset_access import DatasetLease
from .farm_layout import CATEGORY_PATHS, MARKER, RECORDINGS, collaboration_home, farm_identity, shared_farm
from .package_paths import check, safe_path, unsafe_relative
from .storage import ProjectLock, atomic_json, read_json

SCHEMA = 'cowmata-collaboration-v1'
MANIFEST = '协作清单.json'
ASSIGNMENT = '.cowmata-assignment.json'
CHUNK = 4 * 1024 * 1024
SENSOR_KINDS = ('Motion', 'PPG', 'Temp')
PRIMARY_KINDS = ('Motion', 'PPG')
_DAY = re.compile(r'\d{4}-\d{2}-\d{2}')
_VIEW = re.compile(r'视角\d{2}')
_PARTIAL = re.compile(r'\.[0-9a-f]{32}\.partial')
_PARTIAL_LOCK = re.compile(r'\.[0-9a-f]{32}\.partial\.lock')
# Sequential-scan hint: hundreds of GB of recordings stream through without
# evicting the file cache that annotation playback relies on.
_SEQUENTIAL_READ = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_SEQUENTIAL', 0)
# 4.4.5: packages are plain folders (no ZIP), written straight into the folder the operator picks.
RAW_SUFFIX = '_原始'
RETURN_TAG = '_标注_'
RAW_INBOX = '原始数据包'
RECEIVED_INBOX = '已接收'
CONFLICT_INBOX = '冲突待核对'
_MAX_PATH = 259
_TEMP_SUFFIX = 13  # atomic writes go through '<name>.<8 random>.tmp'
_TARGET_HINTS = ('派包', '派发', '协作', '转移', '标注', '工程', '共享', '交换', '数据包')


def farm_root(path):
    """Return the unified farm root when a category subdirectory was selected.

    The picker is intentionally allowed to start in a category folder (for
    example ``...\\产犊``), but all collaboration manifests and locks must be
    rooted at the directory carrying ``.cowmata-farm.json``.
    """
    value = Path(path).resolve(strict=True)
    return shared_farm(value) or value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def label_schema():
    from cowmata_tailring.annotation.defaults import DEFAULT_LABELS
    return hashlib.sha256(canonical(DEFAULT_LABELS)).hexdigest()


def _registry(root):
    return collaboration_home(root) / '派发记录'


def _linked(entry):
    """A directory entry that is a symbolic link or junction; such entries are never followed."""
    return entry.is_symlink() or getattr(entry, 'is_junction', lambda: False)()


def _listing(path):
    """Sorted entries of one folder; an unreadable or vanished folder lists nothing."""
    try:
        with os.scandir(path) as entries:
            return sorted(entries, key=lambda entry: entry.name)
    except OSError:
        return []


def _folders(path):
    return [entry for entry in _listing(path) if not _linked(entry) and entry.is_dir(follow_symlinks=False)]


def _is_day(name):
    if not _DAY.fullmatch(name):
        return False
    try:
        date.fromisoformat(name)
    except ValueError:
        return False
    return True


def _sensor_files(directory, prefix):
    """Raw JSON below one device-day folder as [(relative, DirEntry)], plus entries left out.

    Directory-listing data only: no per-file path resolution or stat call, so a
    farm with tens of thousands of records lists in seconds.
    """
    found, left_out, stack = [], [], [(directory, prefix)]
    while stack:
        path, relative = stack.pop()
        for entry in _listing(path):
            child = relative + '/' + entry.name
            if _linked(entry):
                left_out.append(child)
            elif entry.is_dir(follow_symlinks=False):
                stack.append((entry.path, child))
            elif entry.name.lower().endswith('.json') and not entry.name.endswith('.标注.json'):
                if unsafe_relative(child):
                    left_out.append(child)
                else:
                    found.append((child, entry))
    found.sort(key=lambda item: item[0])
    return found, left_out


def _dispatch_history(root):
    """Units already dispatched: package ids, file lists and stamps. Damaged records are ignored."""
    assigned, prior_paths, prior_stamps = {}, {}, {}
    for path in sorted(_registry(root).glob('*.json')):
        try:
            entry = read_json(path, {})
            if entry.get('status') != 'ready':
                continue
            manifest = entry['manifest']
            for unit in manifest['units']:
                assigned.setdefault(unit['key'], []).append(manifest['package_id'])
                prior_paths.setdefault(unit['key'], set()).update(unit['paths'])
            prior_stamps.update(manifest.get('sensor_stamps') or {})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return assigned, prior_paths, prior_stamps


def _changed(entry, stamp):
    """Size or modification time differs from the dispatched snapshot (listing data, no extra stat)."""
    try:
        size, modified = json.loads(stamp)[:2]
        current = entry.stat(follow_symlinks=False)
    except (OSError, ValueError, TypeError):
        return True
    return current.st_size != size or current.st_mtime_ns != modified


def _label_files(root, category):
    """Existing labels of one category keyed by normalised path: one walk, not one stat per record."""
    meta = os.path.join(root, *category.split('/'), '标注工程')
    found, stack = {}, [os.path.join(meta, kind) for kind in PRIMARY_KINDS]
    while stack:
        for entry in _listing(stack.pop()):
            if _linked(entry):
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
            elif entry.name.endswith('.标注.json'):
                found[os.path.normcase(entry.path)] = entry.path
    return found


def _annotation_state(root, primary, labels):
    """(records with saved work, records marked done); an unreadable label counts as unannotated."""
    saved = completed = 0
    for relative in primary:
        try:
            label = labels.get(os.path.normcase(str(_annotation_path(root, relative))))
            if label is None:
                continue
            doc = read_json(Path(label), {})
            work = doc.get('work', doc)
            done = work.get('progress', {}).get('status') == 'done'
            populated = bool(work.get('project', {}).get('events') or work.get('drafts') or done)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        saved += int(populated)
        completed += int(done)
    return saved, completed


def inventory(root, category, *, cancelled=lambda: False, progress=lambda *_: None):
    """Device-day units of one category with their annotation and dispatch state.

    4.4.2: one folder walk using directory-listing data; unreadable entries are
    left out (``skipped_files``) instead of stopping the scan.
    """
    root = farm_root(root)
    if not farm_identity(root) or category not in CATEGORY_PATHS:
        raise ValueError('请选择已统一目录的牧场及健康类别')
    assigned, prior_paths, prior_stamps = _dispatch_history(root)
    base = os.path.join(root, *category.split('/'))
    days = [(kind, day) for kind in SENSOR_KINDS for day in _folders(os.path.join(base, kind)) if _is_day(day.name)]
    groups, listed = {}, {}
    for index, (kind, day) in enumerate(days, 1):
        check(cancelled)
        for owner in _folders(day.path):
            key = category + '/' + day.name + '/' + owner.name
            value = groups.setdefault(key, dict(key=key, category=category, day=day.name, owner=owner.name, paths=[],
                                                bytes=0, modalities=[], dispatches=assigned.get(key, []), skipped_files=[]))
            files, left_out = _sensor_files(owner.path, '/'.join((category, kind, day.name, owner.name)))
            value['skipped_files'] += left_out
            for relative, entry in files:
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    value['skipped_files'].append(relative)
                    continue
                value['paths'].append(relative)
                value['bytes'] += size
                listed[relative] = entry
            if files:
                value['modalities'].append(kind)
        progress(index, len(days), kind + ' ' + day.name)
    labels = _label_files(root, category)
    result = []
    for value in groups.values():
        if not value['paths']:
            continue
        primary = [p for p in value['paths'] if '/Motion/' in p] or [p for p in value['paths'] if '/PPG/' in p]
        saved, completed = _annotation_state(root, primary, labels)
        value.update(annotation_records=len(primary), annotated_records=saved, completed_records=completed,
                     annotation_status='done' if primary and completed == len(primary) else 'partial' if saved else 'new')
        prior = prior_paths.get(value['key'], set())
        value['new_records'] = len(set(value['paths']) - prior) if prior else 0
        value['changed_records'] = sum(p in prior_stamps and _changed(listed[p], prior_stamps[p]) for p in value['paths'])
        value['dispatch_status'] = ('new' if not prior else 'supplement' if value['new_records']
                                    else 'changed' if value['changed_records'] else 'assigned')
        result.append(value)
    return result

def plan_dispatch(root, units, *, count=1, views=None, purpose='annotation', cancelled=lambda: False):
    # Planning only reads the farm and takes no dataset lease, so it never waits for,
    # or blocks, annotation, data organisation or downloading (4.4.2).
    return _plan_dispatch(farm_root(root), units, count=count, views=views, purpose=purpose, cancelled=cancelled)


def plan_dispatch_groups(root, groups, *, purpose='annotation', cancelled=lambda: False):
    """Honor explicit per-package dates; every package includes all video views."""
    root = farm_root(root)
    groups = [list(group) for group in groups]
    if not groups or any(not group for group in groups):
        raise ValueError('每个包都需要选择资料；存在未选择日期的空包')
    units = [unit for group in groups for unit in group]
    if len({u['key'] for u in units}) != len(units):
        raise ValueError('同一设备日期被重复选择到多个包')
    if any(len({u['category'] for u in group}) != 1 for group in groups):
        raise ValueError('每个包请选择一个健康类别')
    return _plan_dispatch(root, units, count=len(groups), purpose=purpose, cancelled=cancelled, groups=groups)


def _check_unit_files(root, units):
    """Re-list the selected device-day folders against the scanned lists.

    Returns (notes, gone). Newly landed files are only reported (the package
    stays frozen to the scan); vanished files are returned for the caller to
    leave out. Neither stops a dispatch.
    """
    notes, gone = [], set()
    for unit in units:
        current = set()
        for kind in SENSOR_KINDS:
            prefix = '/'.join((unit['category'], kind, unit['day'], unit['owner']))
            current.update(relative for relative, _ in _sensor_files(os.path.join(root, *prefix.split('/')), prefix)[0])
        planned = set(unit['paths'])
        gone |= planned - current
        if current - planned:
            notes.append(unit['key'] + ' 新增 ' + str(len(current - planned)) + ' 个未扫描文件；按扫描时清单派发')
    return notes, gone


def _link_stat(st):
    return stat.S_ISLNK(st.st_mode) or getattr(st, 'st_reparse_tag', 0) == getattr(stat, 'IO_REPARSE_TAG_MOUNT_POINT', -1)


class _Snapshot:
    """Size and stamp of farm files: one lstat per file and one per distinct folder."""

    def __init__(self, root):
        self.root = str(root)
        self.folders = {}

    def _folder_ok(self, relative):
        if relative not in self.folders:
            parent = relative.rpartition('/')[0]
            ok = not parent or self._folder_ok(parent)
            if ok:
                try:
                    ok = not _link_stat(os.lstat(os.path.join(self.root, *relative.split('/'))))
                except OSError:
                    ok = False
            self.folders[relative] = ok
        return self.folders[relative]

    def measure(self, relative):
        """(size, stamp) of a regular farm file; the stamp equals ``file_stamp`` of that file."""
        reason = unsafe_relative(relative)
        if reason:
            raise ValueError(reason)
        folder = relative.rpartition('/')[0]
        if folder and not self._folder_ok(folder):
            raise FileNotFoundError(2, '所在目录已不存在或是链接', relative)
        st = os.lstat(os.path.join(self.root, *relative.split('/')))
        if _link_stat(st) or not stat.S_ISREG(st.st_mode):
            raise ValueError('不是普通文件（链接或目录）')
        return st.st_size, json.dumps([st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino])


def _skip_reason(exc):
    if isinstance(exc, FileNotFoundError):
        return '文件已不存在'
    if isinstance(exc, SourceBusyError):
        return '正在写入，可能仍在下载或复制'
    if isinstance(exc, OSError):
        return '无法读取：' + (exc.strerror or str(exc))
    return str(exc)


def _day_videos(root, days, views):
    """Recordings the receiving side accepts (录像/<日期>/视角NN/<文件>); anything else is noted."""
    found, missing, notes = [], [], []
    for day in days:
        count = 0
        for entry in _listing(os.path.join(root, RECORDINGS, day)):
            relative = RECORDINGS + '/' + day + '/' + entry.name
            if _linked(entry) or not entry.is_dir(follow_symlinks=False):
                if os.path.splitext(entry.name)[1].lower() in VIDEO_SUFFIXES:
                    notes.append('录像不在“视角NN”目录内，未打包：' + relative)
                continue
            if views is not None and entry.name not in views:
                continue
            nested, outside = 0, 0
            for item in _listing(entry.path):
                if _linked(item) or item.is_dir(follow_symlinks=False):
                    nested += 1
                elif os.path.splitext(item.name)[1].lower() in VIDEO_SUFFIXES:
                    if _VIEW.fullmatch(entry.name):
                        found.append(relative + '/' + item.name)
                        count += 1
                    else:
                        outside += 1
            if outside:
                notes.append('视角目录名不是“视角NN”，接收端无法识别，' + str(outside) + ' 个录像未打包：' + relative)
            if nested:
                notes.append('视角目录内的子文件夹未打包：' + relative)
        if not count:
            missing.append(day)
    return found, missing, notes


def _plan_dispatch(root, units, *, count=1, views=None, purpose='annotation', cancelled=lambda: False, groups=None):
    """Freeze what is on disk now by name, size and stamp.

    4.4.2: the operator has already checked the data, so planning neither parses
    sensor JSON nor probes recordings and takes seconds even for a 500 GB
    recording day. Vanished files and links are left out with a note, and a
    missing Temp/PPG record never holds a device-day back.
    """
    from .package_readiness import download_cycle_warning
    root = farm_root(root)
    identity = farm_identity(root)
    if not identity or not units or not 1 <= count <= len(units):
        raise ValueError('请选择资料；分包数量不能超过设备日期条目数')
    if purpose not in {'annotation', 'review'}:
        raise ValueError('Invalid task purpose')
    if purpose == 'annotation' and any(u.get('annotation_status') == 'done' for u in units):
        raise ValueError('已完成标注的数据请使用复核任务，避免重复标注')
    if purpose == 'review' and any(not u.get('annotated_records') for u in units):
        raise ValueError('复核任务只选择已有标注或草稿的数据')
    if len({u['key'] for u in units}) != len(units):
        raise ValueError('同一设备日期被重复选择')
    if views == []:
        raise ValueError('请至少选择一个录像视角；没有录像不能派包')
    copies = {u['key']: copy.deepcopy(u) for u in units}
    task_id = uuid4().hex
    if groups is not None:
        buckets = [[copies[u['key']] for u in group] for group in groups]
    else:
        # Legacy automatic plans distribute indivisible device-days.
        buckets = [[] for _ in range(count)]
        for unit in sorted(copies.values(), key=lambda u: (-len(u['paths']), u['day'], u['owner'])):
            group = min(buckets, key=lambda b: (len(b), sum(len(u['paths']) for u in b), sum(u['bytes'] for u in b)))
            group.append(unit)
    cycle = download_cycle_warning(root)
    snapshot = _Snapshot(root)
    plans = []
    for part, group in enumerate(buckets, 1):
        check(cancelled)
        warnings = [cycle] if cycle else []
        notes, gone = _check_unit_files(root, group)
        warnings += notes
        for unit in group:
            warnings += ['未打包（链接或文件名不安全）：' + p for p in unit.pop('skipped_files', [])]
            missing = set(SENSOR_KINDS) - set(unit.get('modalities') or ())
            if missing:
                warnings.append(unit['key'] + ' 缺少 ' + '、'.join(sorted(missing)) + '；不影响派包，按现有资料打包')
        days = sorted({u['day'] for u in group})
        categories = sorted({u['category'] for u in group})
        videos, missing_days, video_notes = _day_videos(root, days, views)
        warnings += video_notes
        if missing_days:
            warnings.append('所选日期还没有录像文件，包内仅含传感器 JSON：' + '、'.join(missing_days))
        entries, kept, skipped = [], set(), []
        for relative in sorted({p for u in group for p in u['paths']}) + videos:
            check(cancelled)
            try:
                if relative in gone:
                    raise FileNotFoundError(2, 'missing', relative)
                size, stamp = snapshot.measure(relative)
            except (OSError, ValueError) as exc:
                skipped.append(relative)
                warnings.append('未打包（' + _skip_reason(exc) + '）：' + relative)
                continue
            entries.append(dict(path=relative, size=size, stamp=stamp))
            kept.add(relative)
        for unit in group:
            unit['paths'] = [p for p in unit['paths'] if p in kept]
        empty = [u['key'] for u in group if not u['paths']]
        if empty:
            warnings.append('以下设备日没有可打包的传感器文件，本包不含它们：' + '、'.join(empty))
        group = [u for u in group if u['paths']]
        sensors = sorted(p for u in group for p in u['paths'])
        entries.sort(key=lambda e: e['path'])
        caption = categories[0].replace('/', '-') if len(categories) == 1 else '多类别'
        task_name = '复核' if purpose == 'review' else '标注'
        name = f'{root.name}_{caption}_{days[0]}至{days[-1]}_{task_name}_{task_id[:12]}_P{part:03}'
        readiness = dict(validation_scope='selected_directory', warnings=warnings, skipped=skipped,
                         checked_at=datetime.now(timezone.utc).isoformat(), sensor_files=len(sensors),
                         video_files=sum(Path(e['path']).suffix.lower() in VIDEO_SUFFIXES for e in entries))
        sensor_set = set(sensors)
        plans.append(dict(schema=SCHEMA, kind='raw', task_id=task_id, package_id=f'{task_id}-P{part:03}',
                          farm_id=identity['farm_id'], farm_name=root.name, part=part, parts=count,
                          label_schema=label_schema(), units=group, categories=categories, dates=days,
                          sensor_paths=sensors, entries=entries, base_name=name, missing_video_dates=missing_days,
                          estimated_bytes=sum(e['size'] for e in entries), selected_views=views,
                          capacity_policy='informational_only', purpose=purpose, readiness=readiness,
                          sensor_stamps={e['path']: e['stamp'] for e in entries if e['path'] in sensor_set}))
    return plans

class _SourceReadError(OSError):
    """A farm file failed while being read; tolerant packaging drops that member and carries on."""


def _open_source(path):
    return open(os.open(path, _SEQUENTIAL_READ), 'rb', buffering=0)


def _copy_to_file(source, target, cancelled, advance):
    """Stream one file into a new ``target`` through a reused buffer, flushed to disk; returns (bytes, sha256)."""
    digest, size = hashlib.sha256(), 0
    buffer = bytearray(CHUNK)
    view = memoryview(buffer)
    with open(target, 'xb') as output:
        while True:
            check(cancelled)
            try:
                count = source.readinto(buffer)
            except OSError as exc:
                raise _SourceReadError(exc.errno, '读取中断：' + (exc.strerror or str(exc))) from exc
            if not count:
                break
            chunk = view[:count]
            output.write(chunk)
            digest.update(chunk)
            size += count
            advance(count)
        output.flush()
        os.fsync(output.fileno())
    return size, digest.hexdigest()


def _tree(folder):
    """({casefold relative: (relative, size)}, [folder relatives]) below a package folder; links are refused."""
    files, folders, stack = {}, [], [(str(folder), '')]
    while stack:
        path, prefix = stack.pop()
        try:
            with os.scandir(path) as listing:
                entries = list(listing)
        except OSError as exc:
            raise OSError(exc.errno, '无法读取数据包文件夹：' + (exc.strerror or str(exc)), path) from exc
        for entry in entries:
            relative = prefix + entry.name
            if _linked(entry):
                raise ValueError('数据包内含符号链接或目录联接，不能使用：' + relative)
            if entry.is_dir(follow_symlinks=False):
                folders.append(relative)
                stack.append((entry.path, relative + '/'))
            else:
                files[relative.casefold()] = (relative, entry.stat(follow_symlinks=False).st_size)
    return files, folders


def _sizes(files):
    return {key: size for key, (_, size) in files.items()}


def _long_path_check(parent, name_length, relatives, margin=0, *, opening=False):
    """Fail before writing when a file would exceed the classic 260-character Windows path limit
    (``margin``: room for the label files annotation adds inside a raw task)."""
    longest = max((len(r) for r in relatives), default=0)
    if len(str(parent)) + 1 + name_length + 1 + longest + margin > _MAX_PATH:
        if opening:
            raise ValueError('数据包所在位置的路径太深，标注时保存的文件会超过 Windows 260 个字符的限制：'
                             '请在“放到”里选择更短的位置（例如 E:\\2_标注器\\标注），软件会把整个文件夹移过去')
        raise ValueError('目标位置的路径太深：数据包里最长的文件路径会超过 Windows 260 个字符的限制，'
                         '请选择更短的位置（例如 E:\\2_标注器\\派包）')


def _rename_into_place(source, target):
    """Rename a finished folder (or file) to its final name; retried while antivirus or indexing briefly holds new files."""
    for attempt in range(40):
        if os.path.lexists(target):
            raise FileExistsError('目标位置已有同名数据包，不能覆盖：' + str(target))
        try:
            os.rename(source, target)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.25)


def _partial_lock(partial):
    return Path(partial).with_name(Path(partial).name + '.lock')


def _new_partial(parent):
    """A hidden work folder next to the final package. Its writer lock sits beside it (``.<id>.partial.lock``)
    and is taken before the folder exists and released only after it was renamed into place or removed,
    so another run never mistakes a folder being finished for an abandoned one."""
    partial = Path(parent) / ('.' + uuid4().hex + '.partial')
    lock = ProjectLock(_partial_lock(partial))
    try:
        partial.mkdir()
    except BaseException:
        _release_partial(partial, lock)
        raise
    return partial, lock


def _release_partial(partial, lock):
    lock.close()
    try:
        _partial_lock(partial).unlink(missing_ok=True)
    except OSError:
        pass


def _drop_partial(partial, lock):
    shutil.rmtree(partial, ignore_errors=True)
    _release_partial(partial, lock)


def _lock_free(path):
    """True: the lock file exists and nobody holds it; False: a live writer holds it; None: there is none.
    The file is never created here."""
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, 'O_BINARY', 0))
    except FileNotFoundError:
        return None
    except OSError:
        return False
    try:
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def _abandoned(partial):
    """A work folder nobody writes any more: its writer lock is free, or it never had one."""
    return _lock_free(_partial_lock(partial)) is not False


def _write_folder(output, manifest, entries, *, cancelled, progress, tolerant=False, finalize=None, margin=0):
    """Write one package folder in a single streaming pass; nothing is compressed.

    Files go into a hidden ``.<id>.partial`` folder next to ``output`` (locked while being written),
    each flushed to disk with its SHA-256 taken from the bytes written; ``协作清单.json`` lists them,
    and only the complete folder is renamed to ``output``. ``tolerant`` (raw dispatch, 4.4.2): a source
    that vanished, is still held open for writing, or fails while being read is left out instead of
    stopping the package; an entry whose ``requires`` member was left out is skipped too.
    ``finalize(manifest, skipped, changed, stamps)`` returns the stored manifest. Returns stay strict.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(output):
        raise FileExistsError('目标位置已有同名数据包，不能覆盖：' + str(output))
    _long_path_check(output.parent, max(len(output.name), 42), [e['path'] for e in entries] + [MANIFEST], margin)
    total = sum(e['size'] for e in entries)
    if shutil.disk_usage(output.parent).free < total + 16 * 1024**2:
        raise OSError('磁盘剩余空间不足；请更换位置或减少所选范围')
    done, members, skipped, changed, stamps = 0, [], [], [], {}

    def advance(count, label):
        nonlocal done
        done += count
        progress(done, total, label)

    partial, lock = _new_partial(output.parent)
    published = False
    try:
        for entry in entries:
            check(cancelled)
            start, path, before = done, entry.get('source'), None
            if entry.get('requires') in {item['path'] for item in skipped}:
                skipped.append(dict(path=entry['path'], reason='对应的原始记录未打包'))
                continue
            try:
                if path is None:
                    source = io.BytesIO(entry['payload'])
                else:
                    path = Path(path)
                    assert_not_being_written(path)
                    before = file_stamp(path)
                    if not tolerant and entry.get('stamp') and before != entry['stamp']:
                        raise ValueError('打包前原始数据发生变化，请重新扫描：' + str(path))
                    source = _open_source(path)
            except (OSError, ValueError) as exc:
                if not tolerant or path is None:
                    raise
                skipped.append(dict(path=entry['path'], reason=_skip_reason(exc)))
                advance(entry['size'], entry['path'])
                continue
            target = safe_path(partial, entry['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with source:
                    size, sha = _copy_to_file(source, target, cancelled, lambda count, label=entry['path']: advance(count, label))
            except _SourceReadError as exc:
                target.unlink(missing_ok=True)
                if not tolerant:
                    raise
                skipped.append(dict(path=entry['path'], reason=exc.strerror or str(exc)))
                advance(max(0, entry['size'] - (done - start)), entry['path'])
                continue
            after = before
            if path is not None:
                try:
                    after = file_stamp(path)
                except OSError:
                    after = None
            if not tolerant:
                if size != entry['size'] or after != before:
                    raise ValueError('打包期间源文件发生变化')
                if entry.get('sha256') and entry['sha256'] != sha:
                    raise ValueError('文件内容与原始派发记录不一致')
            elif path is not None:
                if after != before:
                    changed.append(entry['path'])
                stamps[entry['path']] = before
            members.append(dict(path=entry['path'], size=size, sha256=sha))
        manifest = {**manifest, 'members': members, 'created_at': datetime.now(timezone.utc).isoformat()}
        if finalize is not None:
            manifest = finalize(manifest, skipped, changed, stamps)
        with open(partial / MANIFEST, 'xb') as stream:
            stream.write(canonical(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        check(cancelled)
        files, _ = _tree(partial)
        files.pop(MANIFEST.casefold(), None)
        if _sizes(files) != {m['path'].casefold(): m['size'] for m in manifest['members']}:
            raise OSError('写入后的文件夹与清单不一致，请检查目标磁盘后重新生成')
        _rename_into_place(partial, output)
        published = True
        return manifest
    finally:
        if published:
            _release_partial(partial, lock)
        else:
            _drop_partial(partial, lock)


def _remove_stale_partials(folder):
    """Delete work folders (and ≤4.4.4 temporary archives) left by interrupted runs; one still being written stays."""
    reclaimed = 0
    listing = _listing(folder)
    for entry in listing:
        if not _PARTIAL.fullmatch(entry.name) or _linked(entry):
            continue
        try:
            if entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                os.unlink(entry.path)
            elif entry.is_dir(follow_symlinks=False) and _abandoned(Path(entry.path)):
                size = sum(_sizes(_tree(entry.path)[0]).values())
                shutil.rmtree(entry.path)
                _partial_lock(entry.path).unlink(missing_ok=True)
            else:
                continue
        except (OSError, ValueError):
            continue
        reclaimed += size
    for entry in listing:  # a writer that died after its folder was gone leaves only the lock file
        if _PARTIAL_LOCK.fullmatch(entry.name) and not os.path.lexists(entry.path[:-len('.lock')]) and _lock_free(entry.path):
            try:
                os.unlink(entry.path)
            except OSError:
                pass
    return reclaimed


def check_destination(root, destination):
    """The folder a package is written or moved into; never inside the farm data (its 协作 folder excepted)."""
    if destination is None or not str(destination).strip():
        raise ValueError('请选择数据包的目标位置')
    target = Path(str(destination).strip())
    if not target.is_absolute():
        raise ValueError('请选择完整的目标位置（含盘符），例如 E:\\2_标注器\\派包')
    target = target.resolve()
    farm = Path(root).resolve()
    home = collaboration_home(farm).resolve()
    if target == farm or (target.is_relative_to(farm) and not target.is_relative_to(home)):
        raise ValueError('目标位置不能在牧场数据目录里（会被当成牧场资料）；请选择其他位置')
    if os.path.lexists(target) and not target.is_dir():
        raise ValueError('目标位置不是文件夹：' + str(target))
    target.mkdir(parents=True, exist_ok=True)
    return target


def _cross_device(exc):
    return exc.errno == errno.EXDEV or getattr(exc, 'winerror', None) == 17


def move_package(source, destination, *, cancelled=lambda: False, progress=lambda *_: None, expect=None):
    """Move a package folder (or a ≤4.4.4 ZIP) into ``destination``; returns its new path.

    Same disk: one rename. Another disk: copied into a locked hidden work folder (every file flushed and
    size-checked), renamed into place, then the source is deleted. Cancelling or any failure before that
    point leaves the source untouched. ``expect``: package paths that will be written there later (label
    files of a raw task); the move is refused when they would exceed the Windows path limit.
    """
    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve()
    if source.parent == destination:
        return source
    if destination == source or destination.is_relative_to(source):
        raise ValueError('不能把数据包移到它自己的文件夹里')
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / source.name
    if os.path.lexists(target):
        raise FileExistsError('目标位置已有同名数据包：' + str(target))
    if source.is_dir():
        files, folders = _tree(source)
    else:
        files, folders = {source.name.casefold(): (source.name, source.stat().st_size)}, []
    _long_path_check(destination, len(source.name), [relative for relative, _ in files.values()] if source.is_dir() else [])
    if expect:
        _long_path_check(destination, len(source.name), expect, _TEMP_SUFFIX, opening=True)
    try:
        _rename_into_place(source, target)
        return target
    except OSError as exc:
        if not _cross_device(exc):
            raise
    total = sum(size for _, size in files.values())
    _long_path_check(destination, 42, [relative for relative, _ in files.values()] if source.is_dir() else [source.name])
    if shutil.disk_usage(destination).free < total + 16 * 1024**2:
        raise OSError('目标磁盘剩余空间不足：' + str(destination))
    done = 0

    def advance(count, label):
        nonlocal done
        done += count
        progress(done, total, label)

    partial, lock = _new_partial(destination)
    try:
        if source.is_dir():
            for relative in sorted(folders):
                safe_path(partial, relative).mkdir(parents=True, exist_ok=True)
            for relative, size in sorted(files.values()):
                check(cancelled)
                target_file = safe_path(partial, relative)
                target_file.parent.mkdir(parents=True, exist_ok=True)
                with _open_source(safe_path(source, relative)) as stream:
                    written, _ = _copy_to_file(stream, target_file, cancelled, lambda count, label=relative: advance(count, label))
                if written != size:
                    raise OSError('移动时源文件发生变化：' + relative)
            _rename_into_place(partial, target)
        else:
            copied = partial / source.name
            with _open_source(source) as stream:
                written, _ = _copy_to_file(stream, copied, cancelled, lambda count: advance(count, source.name))
            if written != total:
                raise OSError('移动时源文件发生变化：' + source.name)
            _rename_into_place(copied, target)
    finally:
        _drop_partial(partial, lock)  # empty (or already renamed away) after success
    if source.is_dir():
        shutil.rmtree(source, ignore_errors=True)
    else:
        source.unlink(missing_ok=True)
    return target


def dispatch(root, plans, *, destination=None, cancelled=lambda: False, progress=lambda *_: None, report=None):
    """Write each planned package as a folder in ``destination``; one failing package never stops the others.

    4.4.5: packages are plain folders (协作清单.json + the farm tree), written straight into the
    folder the operator picked (default 科牧特_协作标注\\原始数据包); nothing is compressed.
    4.4.2: dispatch only reads the farm, so it takes no dataset lease and runs alongside annotation,
    organisation and downloading. Files that vanished, are still being written or fail while being read
    are left out and listed in the package manifest and ``report['packages']``. Cancelling stops at once
    (finished packages are kept); if every package fails, the first error is raised.
    """
    root = Path(root).resolve(strict=True)
    identity = farm_identity(root)
    home = collaboration_home(root)
    home.mkdir(parents=True, exist_ok=True)
    report = {} if report is None else report
    records = report.setdefault('packages', [])
    outputs, failures = [], []
    lock = ProjectLock(home / '.dispatch.lock')
    try:
        if not lock.acquired:
            raise ValueError('另一个派发任务正在运行，请等它完成后再派包')
        target = check_destination(root, destination or home / RAW_INBOX)
        report['destination'] = str(target)
        reclaimed = _remove_stale_partials(target)
        if target != (home / RAW_INBOX).resolve():
            reclaimed += _remove_stale_partials(home / RAW_INBOX)
        report['reclaimed_bytes'] = reclaimed
        sizes = [sum(e.get('size', 0) for e in plan.get('entries', [])) for plan in plans]
        total, offset = sum(sizes), 0
        for index, plan in enumerate(plans):
            check(cancelled)
            record = dict(part=plan.get('part', index + 1), package_id=plan.get('package_id'), dates=plan.get('dates', []),
                          units=[], output=None, warnings=[], skipped=[], error=None)
            records.append(record)
            label = '包 ' + str(index + 1) + '/' + str(len(plans)) + ' · '

            def step(done, _total, text, base=offset, label=label):
                progress(base + done, total, label + '/'.join(str(text).split('/')[-2:]))
            try:
                output, manifest, replaced = _dispatch_one(root, identity, home, plan, target, cancelled, step)
            except InterruptedError:
                raise
            except Exception as exc:
                record['error'] = str(exc) or type(exc).__name__
                failures.append(exc)
            else:
                outputs.append(output)
                record.update(output=str(output), units=[u['key'] for u in manifest['units']],
                              warnings=manifest['readiness']['warnings'], skipped=manifest.get('skipped', []),
                              replaced=replaced)
            offset += sizes[index]
            progress(offset, total, label + ('失败' if record['error'] else '完成'))
    finally:
        lock.close()
    if failures and not outputs:
        raise failures[0]
    return outputs


def _dispatch_one(root, identity, home, plan, destination, cancelled, progress):
    if plan['farm_id'] != identity['farm_id']:
        raise ValueError('派发方案不属于此牧场')
    units = copy.deepcopy(plan['units'])
    if not units:
        raise ValueError('此包没有可打包的传感器资料，已跳过')
    notes, _ = _check_unit_files(root, units)
    snapshot, prefix, entries, left_out = _Snapshot(root), root.name + '/', [], {}
    for entry in plan['entries']:
        try:
            size, _stamp = snapshot.measure(entry['path'])
        except (OSError, ValueError) as exc:
            left_out[entry['path']] = _skip_reason(exc)
            continue
        entries.append(dict(entry, size=size, path=prefix + entry['path'],
                            source=os.path.join(root, *entry['path'].split('/'))))
    marker = canonical(identity)
    entries.append(dict(path=prefix + MARKER, payload=marker, size=len(marker)))
    manifest = {k: copy.deepcopy(v) for k, v in plan.items() if k not in {'entries', 'sensor_paths', 'replace_previous'}}
    manifest['units'] = units
    readiness = manifest['readiness'] = dict(manifest.get('readiness') or {})
    readiness['warnings'] = [*readiness.get('warnings', []), *notes]
    # Raw dispatch packages contain only the selected sensor JSON and
    # video files. Existing annotation/evidence files stay in the
    # local ledger and must not silently widen package scope.
    manifest['baseline_annotations'] = []
    manifest['baseline_evidence'] = []
    annotations = {}
    if plan.get('purpose') == 'review':
        for relative in plan['sensor_paths']:
            if ('/Motion/' not in relative and '/PPG/' not in relative) or relative in left_out:
                continue
            try:
                label = _annotation_path(root, relative)
                if not label.is_file():
                    continue
                doc = read_json(label, {})
                # Review labels are JSON too. Keep all label data, while
                # marking external pictures unavailable in this raw-only copy.
                for event in [*doc['work']['project']['events'], *doc['work'].get('drafts', [])]:
                    for item in event.get('screenshots', {}).get('items', []):
                        if item.get('status') == 'captured':
                            item['status'] = 'not_packaged'
                            item['message'] = '原始包仅派发 JSON 与视频，证据图保留在原工程'
                payload = canonical(doc)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                readiness['warnings'].append('复核标注无法读取，未打包：' + relative + '（' + str(exc) + '）')
                continue
            rel = label.relative_to(root).as_posix()
            manifest['baseline_annotations'].append(rel)
            annotations[prefix + rel] = rel
            entries.append(dict(path=prefix + rel, payload=payload, size=len(payload), requires=prefix + relative))
    output = Path(destination) / (plan['base_name'] + RAW_SUFFIX)

    def finalize(manifest, skipped, changed, stamps):
        reasons = {**left_out, **{item['path'][len(prefix):]: item['reason'] for item in skipped}}
        manifest['baseline_annotations'] = [rel for rel in manifest['baseline_annotations'] if rel not in reasons]
        kept, dropped = [], []
        for unit in manifest['units']:
            unit['paths'] = [p for p in unit['paths'] if p not in reasons]
            (kept if unit['paths'] else dropped).append(unit)
        if not kept:
            raise ValueError('此包的传感器文件都无法读取，未生成；请稍后重新派发')
        manifest['units'] = kept
        present = {p for unit in kept for p in unit['paths']}
        manifest['sensor_stamps'] = {p: stamps.get(prefix + p, s) for p, s in (manifest.get('sensor_stamps') or {}).items()
                                     if p in present}
        warnings = manifest['readiness']['warnings']
        for relative, reason in sorted(reasons.items()):
            kind = '复核标注未打包' if prefix + relative in annotations else '未打包'
            warnings.append(kind + '（' + reason + '）：' + relative)
        warnings += ['打包时文件仍在写入，已按读取到的内容打包：' + p[len(prefix):] for p in changed]
        if dropped:
            warnings.append('以下设备日没有可打包的传感器文件，本包不含它们：' + '、'.join(u['key'] for u in dropped))
        manifest['skipped'] = [dict(path=p, reason=r) for p, r in sorted(reasons.items())]
        return manifest

    result = _write_folder(output, manifest, entries, cancelled=cancelled, progress=progress, tolerant=True, finalize=finalize)
    atomic_json(_registry(root) / (plan['package_id'] + '.json'),
                dict(status='ready', manifest=result, output=str(output), format='folder'), backup=False)
    replaced = _cleanup_replaced_packages(root, plan, output)
    return output, result, replaced


def _pristine(folder, manifest):
    """A package folder exactly as dispatched: nobody has opened or annotated it yet."""
    try:
        if read_json(folder / MANIFEST, {}).get('package_id') != manifest.get('package_id'):
            return False
        files, _ = _tree(folder)
    except (OSError, ValueError, AttributeError):
        return False
    files.pop(MANIFEST.casefold(), None)
    return _sizes(files) == {m['path'].casefold(): m['size'] for m in manifest.get('members', [])}


def _cleanup_replaced_packages(root, plan, current_output):
    """Remove older packages covering the explicitly re-dispatched units.

    A ≤4.4.4 ZIP in the collaboration folder and a package folder nobody has opened yet are deleted
    with their dispatch record; a folder someone already works in is kept, with its record, so the
    annotation it carries can still be received.
    """
    result = dict(removed=[], kept=[])
    if not plan.get('replace_previous'):
        return result
    keys = {u['key'] for u in plan.get('units', [])}
    registry = _registry(root)
    for record in registry.glob('*.json'):
        value = read_json(record, {})
        manifest = value.get('manifest', {})
        if value.get('status') != 'ready' or manifest.get('package_id') == plan.get('package_id'):
            continue
        if not keys.intersection({u.get('key') for u in manifest.get('units', [])}):
            continue
        output = str(value.get('output') or '').strip()
        old = Path(output) if output else None
        if old is not None and old == Path(current_output):
            continue
        try:
            if old is None:
                pass  # recovered record without a known package location: only the record goes
            elif old.suffix.lower() == '.zip' and old.is_file() and old.resolve().is_relative_to(collaboration_home(root).resolve()):
                old.unlink()
                result['removed'].append(str(old))
            elif old.is_absolute() and old.is_dir():
                if not _pristine(old, manifest):
                    result['kept'].append(str(old))
                    continue
                shutil.rmtree(old)
                result['removed'].append(str(old))
        except OSError:
            result['kept'].append(str(old))
            continue
        record.unlink(missing_ok=True)
    return result


def _check_manifest(value):
    """Checks shared by package folders and ≤4.4.4 ZIPs: format, identities, label schema and the member list."""
    if not isinstance(value, dict) or value.get('schema') != SCHEMA or value.get('kind') not in {'raw', 'annotations'}:
        raise ValueError('协作包格式或版本不支持')
    try:
        UUID(value['farm_id'])
        UUID(value['task_id'])
        package_id, members = value['package_id'], value['members']
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError('协作清单内容不完整') from exc
    if not isinstance(package_id, str) or not re.fullmatch(re.escape(value['task_id']) + r'-P\d{3,}', package_id):
        raise ValueError('任务与分包编号不一致')
    if value.get('label_schema') != label_schema():
        raise ValueError('协作包的标签范式与当前软件不一致')
    if not isinstance(members, list):
        raise ValueError('协作清单内容不完整')
    seen = set()
    for member in members:
        if (not isinstance(member, dict) or type(member.get('size')) is not int or member['size'] < 0
                or not isinstance(member.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', member['sha256'])):
            raise ValueError('协作清单中的文件大小或内容摘要无效')
        reason = unsafe_relative(member.get('path'))
        if reason:
            raise ValueError(reason)
        key = member['path'].casefold()
        if key in seen or key == MANIFEST.casefold():
            raise ValueError('协作清单含重复路径')
        seen.add(key)
    return value


def validate_archive(path):
    """Manifest of a ≤4.4.4 ZIP package after checking its members (still accepted when opened or received)."""
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = set()
        for info in infos:
            safe_path(Path.cwd(), info.filename)
            key = info.filename.casefold()
            if key in names or info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                raise ValueError('压缩包含重复路径、目录链接或加密条目')
            names.add(key)
        if MANIFEST not in archive.namelist() or archive.getinfo(MANIFEST).file_size > 64 * 1024**2:
            raise ValueError('缺少有效的协作清单')
        value = _check_manifest(json.loads(archive.read(MANIFEST)))
        if {m['path'].casefold() for m in value['members']} != names - {MANIFEST.casefold()}:
            raise ValueError('ZIP 内容与清单不一致')
        for member in value['members']:
            if archive.getinfo(member['path']).file_size != member['size']:
                raise ValueError('ZIP 文件大小或内容摘要无效')
        return value


NOT_A_PACKAGE = '所选文件夹不是协作数据包（里面没有“协作清单.json”）：'


def _peek_folder(folder):
    """The checked 协作清单.json of a package folder, without touching any other file."""
    folder = Path(folder)
    listed = folder / MANIFEST
    try:
        if listed.is_symlink() or not listed.is_file() or listed.stat().st_size > 64 * 1024**2:
            raise ValueError(NOT_A_PACKAGE + str(folder))
        value = json.loads(listed.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError(NOT_A_PACKAGE + str(folder)) from exc
    return _check_manifest(value)


def validate_folder(folder, *, strict=True, cancelled=lambda: False):
    """Manifest of a package folder after checking its files.

    ``strict`` (annotation returns): exactly the listed files with their sizes and SHA-256, nothing else.
    Otherwise (raw tasks, annotated where they are): every listed file present with its listed size;
    files added by annotation are allowed.
    """
    folder = Path(folder)
    value = _peek_folder(folder)
    files, _ = _tree(folder)
    files.pop(MANIFEST.casefold(), None)
    for member in value['members']:
        check(cancelled)
        found = files.pop(member['path'].casefold(), None)
        if found is None:
            raise ValueError('数据包缺少清单里的文件：' + member['path'])
        if found[1] != member['size']:
            raise ValueError('数据包里的文件大小与清单不一致：' + member['path'])
        if strict and digest_file(safe_path(folder, member['path'])) != member['sha256']:
            raise ValueError('数据包里的文件内容与清单不一致：' + member['path'])
    if strict and files:
        raise ValueError('数据包含清单之外的文件：' + sorted(relative for relative, _ in files.values())[0])
    return value


def read_package(path, *, strict=True):
    """Manifest of a package folder (4.4.5) or a ≤4.4.4 ZIP."""
    path = Path(path)
    return validate_folder(path, strict=strict) if path.is_dir() else validate_archive(path)


def _extract(path, directory, manifest, cancelled, progress):
    total, done = sum(m['size'] for m in manifest['members']), 0
    if shutil.disk_usage(directory).free < total + 16 * 1024**2:
        raise OSError('解包磁盘空间不足')
    with zipfile.ZipFile(path) as archive:
        for member in manifest['members']:
            check(cancelled)
            destination = safe_path(directory, member['path'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with archive.open(member['path']) as source, destination.open('xb') as target:
                while block := source.read(CHUNK):
                    check(cancelled)
                    size += len(block)
                    if size > member['size']:
                        raise ValueError('解压大小超出清单')
                    digest.update(block)
                    target.write(block)
                    done += len(block)
                    progress(done, total, member['path'])
            if size != member['size'] or digest.hexdigest() != member['sha256']:
                raise ValueError('ZIP 内容校验失败：' + member['path'])


def _check_raw_members(manifest):
    """Farm folder name of a raw package whose members are all authorised raw / review paths."""
    if manifest['kind'] != 'raw':
        raise ValueError('请选择派发的原始数据包')
    name = manifest['farm_name']
    if not isinstance(name, str) or '/' in name:
        raise ValueError('牧场名称不能包含路径')
    safe_path(Path.cwd(), name)
    for member in manifest['members']:
        relative = PurePosixPath(member['path'])
        if relative.parts[0] != name:
            raise ValueError('原始包目录树与牧场不一致')
        rel = PurePosixPath(*relative.parts[1:]).as_posix()
        is_annotation = rel in manifest.get('baseline_annotations', []) and '/标注工程/' in rel and rel.endswith('.标注.json')
        is_evidence = rel in manifest.get('baseline_evidence', []) and '/标注工程/证据/' in rel and rel.endswith('.jpg')
        if rel != MARKER and not _raw_relative(rel, manifest) and not is_annotation and not is_evidence:
            raise ValueError('原始包包含非授权资料路径')
    return name


def open_raw_package(path, destination=None, *, cancelled=lambda: False, progress=lambda *_: None):
    """Farm root of a dispatched raw task, ready to annotate.

    4.4.5: a package folder is opened where it is, or first moved into ``destination`` when another
    folder is chosen; nothing is unpacked. A ≤4.4.4 ZIP is verified and unpacked into ``destination``.
    """
    path = Path(path)
    if path.is_dir():
        return _open_raw_folder(path, destination, cancelled, progress)
    if destination is None or not str(destination).strip():
        raise ValueError('旧版 ZIP 原始数据包需要选择解包位置')
    manifest = validate_archive(path)
    name = _check_raw_members(manifest)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    final = destination / manifest['package_id']
    if final.exists():
        raise FileExistsError('此任务已解包，请直接打开已有工程：' + str(final / name))
    with tempfile.TemporaryDirectory(prefix='.cowmata-unpack-', dir=destination) as temporary:
        staging = Path(temporary)
        _extract(path, staging, manifest, cancelled, progress)
        farm = staging / name
        if farm_identity(farm)['farm_id'] != manifest['farm_id']:
            raise ValueError('牧场标识不一致')
        referenced_evidence = set()
        for relative in manifest.get('baseline_annotations', []):
            label = safe_path(farm, relative)
            doc = read_json(label, {})
            if _validate_document(doc, farm, manifest) != label:
                raise ValueError('标注目录与派发目录树不一致')
            referenced_evidence.update(_evidence_entries(doc, label, farm))
            doc['source']['project_root_hint'] = str(final / name)
            atomic_json(label, doc, backup=False)
        if referenced_evidence != set(manifest.get('baseline_evidence', [])):
            raise ValueError('清单存在未引用或缺失的证据图')
        for category in manifest['categories']:
            atomic_json(safe_path(farm, category + '/标注工程/annotation-layout.json'), {'schema': 'dated-annotations-v1'}, backup=False)
        atomic_json(farm / ASSIGNMENT, manifest, backup=False)
        collaboration_home(farm).mkdir(parents=True, exist_ok=True)
        check(cancelled)
        staging.rename(final)
    return final / name


def _label_paths(manifest):
    """Package paths of the label files annotating this raw task will write (标注工程\\…\\*.标注.json)."""
    prefix = manifest['farm_name'] + '/'
    found = []
    for member in manifest['members']:
        relative = member['path'][len(prefix):] if member['path'].startswith(prefix) else ''
        if '/Motion/' in relative or '/PPG/' in relative:
            try:
                found.append(prefix + _annotation_path(Path(), relative).as_posix())
            except ValueError:
                continue
    return found


def _open_raw_folder(path, destination, cancelled, progress):
    package = Path(path).resolve(strict=True)
    if not (package / MANIFEST).is_file() and (package.parent / MANIFEST).is_file():
        package = package.parent  # the farm folder inside the package was picked
    manifest = validate_folder(package, strict=False)
    name = _check_raw_members(manifest)
    expect = [m['path'] for m in manifest['members']] + _label_paths(manifest)
    target = Path(destination).resolve() if destination is not None and str(destination).strip() else package.parent
    if target != package.parent and target != package and not target.is_relative_to(package):
        package = move_package(package, target, cancelled=cancelled, progress=progress, expect=expect)
    else:
        _long_path_check(package.parent, len(package.name), expect, _TEMP_SUFFIX, opening=True)
    farm = package / name
    identity = farm_identity(farm)
    if not identity or identity['farm_id'] != manifest['farm_id']:
        raise ValueError('牧场标识不一致')
    root, _assignment = task_assignment(farm)
    for relative in manifest.get('baseline_annotations', []):
        try:
            label = safe_path(root, relative)
            doc = read_json(label, {})
            if doc['source'].get('project_root_hint') != str(root):
                doc['source']['project_root_hint'] = str(root)
                atomic_json(label, doc, backup=False)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return root


def _raw_relative(relative, manifest):
    parts = PurePosixPath(relative).parts
    if parts[:1] == ('录像',):
        return len(parts) == 4 and bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', parts[1])) and bool(re.fullmatch(r'视角\d{2}', parts[2])) and PurePosixPath(parts[-1]).suffix.lower() in VIDEO_SUFFIXES
    for unit in manifest['units']:
        prefix = PurePosixPath(unit['category']).parts
        tail = parts[len(prefix):]
        if (unit['category'] in CATEGORY_PATHS and parts[:len(prefix)] == prefix
                and len(tail) == 4 and tail[0] in {'Motion', 'PPG', 'Temp'}
                and tail[1] == unit['day'] and tail[2] == unit['owner']
                and tail[3].endswith('.json') and not tail[3].endswith('.标注.json')
                and relative in unit['paths']):
            date.fromisoformat(unit['day'])
            return True
    return False


def _raw_members(manifest):
    prefix = manifest['farm_name'] + '/'
    return {m['path'][len(prefix):]: m for m in manifest['members'] if m['path'].startswith(prefix) and _raw_relative(m['path'][len(prefix):], manifest)}


def _annotation_path(root, relative):
    from .annotation_store import dated_path
    for category in CATEGORY_PATHS:
        if relative.startswith(category + '/'):
            target = dated_path(Path(root) / category / '标注工程', relative)
            if target:
                return target
    raise ValueError('标注来源没有对应的健康类别及日期目录')


def _validate_document(doc, root, assignment):
    from cowmata_tailring.annotation.defaults import DEFAULT_LABELS

    from .device_identity import parse_device_folder
    from .sensor_records import load_sensor_json
    from .work import SessionWork
    if doc.get('format') != 'cowmata-annotation' or doc.get('version') not in {1, 2} or doc.get('coordinates') != 'parent_imu_ms' or doc.get('embedded_imu'):
        raise ValueError('回传必须是完整记录的纯标注文件，不能夹带原始数据')
    relative = doc['source']['path']
    raw_members = _raw_members(assignment)
    member = raw_members.get(relative)
    if not member or '/Motion/' not in relative and '/PPG/' not in relative:
        raise ValueError('标注来源不在本次派发范围内')
    raw = safe_path(root, relative)
    before = file_stamp(raw)
    assert_not_being_written(raw)
    if digest_file(raw) != member['sha256'] or doc['source']['asset_id'] != member['sha256']:
        raise ValueError('原始来源内容与派发时不一致')
    motion = load_sensor_json(raw)
    if before != file_stamp(raw):
        raise ValueError('核验期间原始来源发生变化')
    if doc['work']['asset_id'] != member['sha256']:
        raise ValueError('标签与来源内容标识不一致')
    labels = doc['work']['project'].get('labels', [])
    if [(r.get('key'), r.get('code'), r.get('name')) for r in labels] != [(r.get('key'), r.get('code'), r.get('name')) for r in DEFAULT_LABELS]:
        raise ValueError('标签列表与当前 COWMATA 范式不一致，请先核对历史标签')
    work = SessionWork.from_dict(doc['work'])
    identity = parse_device_folder(raw.parent.name)
    if work.project.cow_id != identity.cow_id or str(motion.device).upper() != identity.device_id:
        raise ValueError('牛号或设备编号与派发来源不一致，需人工核对')
    from .paired_dataset import category_for
    if doc.get('dataset_category') != category_for(raw) or work.category_fields()['dataset_category'] != category_for(raw):
        raise ValueError('健康类别与原始资料目录不一致')
    lo, hi = doc['view']['start_ms'], doc['view']['end_ms']
    if lo != 0 or not isinstance(hi, (int, float)) or not math.isfinite(hi) or abs(hi-motion.duration_ms) > .001:
        raise ValueError('标注时间范围与完整原始记录不一致')
    ids = set()
    for event in work.project.events:
        if event.id in ids or not event.id or not 0 <= event.li < len(DEFAULT_LABELS):
            raise ValueError('存在重复事件或无效标签索引')
        ids.add(event.id)
        end = event.t1 if event.t1 is not None else event.t0
        if not math.isfinite(event.t0+end) or not 0 <= event.t0 <= end <= hi + .001:
            raise ValueError('标签事件时间超出原始记录')
        code = event.extras.get('label_code')
        if code and code != DEFAULT_LABELS[event.li]['code']:
            raise ValueError('事件代码与标签索引不一致')
    for draft in work.drafts:
        if draft['id'] in ids or not 0 <= draft['label_index'] < len(DEFAULT_LABELS):
            raise ValueError('视频草稿身份或标签索引无效')
        ids.add(draft['id'])
        begin, end = draft['reference_start'], draft.get('reference_end')
        if not math.isfinite(begin) or end is not None and (not math.isfinite(end) or end < begin):
            raise ValueError('视频草稿时间无效')
    for row in doc.get('video', {}).get('rows', []):
        video = raw_members.get(row.get('path'))
        if not video or not row['path'].startswith('录像/') or row.get('asset_id') != video['sha256']:
            raise ValueError('Video reference does not match the assigned recording')
        row.pop('external_source', None)
        row.pop('resolved_source', None)
    doc.get('video', {}).pop('archive', None)
    return _annotation_path(root, relative)


def _evidence_entries(doc, label, root):
    from .evidence import read_image, safe_relative
    entries = {}
    for event in [*doc['work']['project']['events'], *doc['work'].get('drafts', [])]:
        for item in event.get('screenshots', {}).get('items', []):
            if item.get('status') == 'captured':
                read_image(label.parent, item)
                path = safe_relative(label.parent, item['path'])
                if not path.is_file():
                    meta = next(p for p in label.parents if p.name == '标注工程')
                    path = safe_relative(meta, item['path'])
                relative = path.relative_to(root).as_posix()
                entries[relative] = dict(path=relative, source=path, size=path.stat().st_size,
                                         sha256=item['sha256'], stamp=file_stamp(path))
    return entries


NOT_A_TASK = ('此目录不是已派发任务：请选择原始数据包文件夹（含“协作清单.json”）或其中的牧场目录，'
              '或用“文件 → 打开协作数据包”打开后再标注')


def _raw_task(value):
    return isinstance(value, dict) and value.get('schema') == SCHEMA and value.get('kind') == 'raw'


def _manifest_digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _check_unpacked_task(manifest, root):
    """A ``协作清单.json`` found beside a farm folder must describe exactly this farm and these files."""
    identity = farm_identity(root)
    if not identity:
        raise ValueError('所选目录缺少牧场标识（.cowmata-farm.json），请完整复制原始数据包文件夹')
    if not _raw_task(manifest):
        raise ValueError('协作清单不是派发的原始数据包清单')
    if manifest.get('farm_id') != identity['farm_id']:
        raise ValueError('协作清单与所选牧场目录不属于同一牧场')
    UUID(manifest['task_id'])
    UUID(manifest['farm_id'])
    if not re.fullmatch(re.escape(manifest['task_id']) + r'-P\d{3,}', manifest['package_id']):
        raise ValueError('任务与分包编号不一致')
    if manifest.get('label_schema') != label_schema():
        raise ValueError('协作包的标签范式与当前软件不一致；请用与派包相同版本的 COWMATA Annotator 标注')
    prefix = manifest['farm_name'] + '/'
    bad = []
    for member in manifest.get('members', []):
        relative = member['path'][len(prefix):] if member['path'].startswith(prefix) else None
        # Only original data is checked here: review-task label files are meant to be edited.
        if relative is None or not _raw_relative(relative, manifest):
            continue
        try:
            if safe_path(root, relative).stat().st_size != member['size']:
                bad.append(relative)
        except OSError:
            bad.append(relative)
    if bad:
        raise ValueError(f'数据包里的资料与协作清单不一致（{len(bad)} 个原始文件缺失或大小不同，例如 {bad[0]}）；'
                         '请重新完整复制原始数据包文件夹（旧版 ZIP 请完整解压）')


def task_assignment(path, *, adopt=True):
    """Farm root and raw-task manifest of an unpacked collaboration package.

    Packages opened in the app carry ``.cowmata-assignment.json``. A 4.4.5 package folder (or a
    ≤4.4.4 ZIP unzipped by hand) has ``<folder>\\协作清单.json`` beside ``<folder>\\<牧场>`` instead;
    that manifest is checked against the farm marker, the label schema and every raw file size,
    then adopted exactly as ``open_raw_package`` writes it. The farm folder, one of its category
    folders or the package folder itself may be selected.
    """
    start = Path(path).resolve(strict=True)
    root = None
    if (start / MANIFEST).is_file():  # the package folder itself, even when it is kept inside another farm
        listed = read_json(start / MANIFEST, {})
        named = start / str(listed.get('farm_name', '')) if isinstance(listed, dict) else start
        candidates = [named, *(d for d in sorted(start.iterdir()) if d.is_dir())]
        root = next((d for d in candidates if d.is_dir() and d != start and farm_identity(d)), None)
    if root is None:
        root = shared_farm(start)
    if root is None:
        raise ValueError(NOT_A_TASK)
    assignment = read_json(root / ASSIGNMENT, {})
    if _raw_task(assignment):
        return root, assignment
    if not (root.parent / MANIFEST).is_file():
        raise ValueError(NOT_A_TASK)
    manifest = read_json(root.parent / MANIFEST, {})
    try:
        _check_unpacked_task(manifest, root)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('协作清单内容不完整，请重新完整复制原始数据包文件夹（' + str(exc) + '）') from exc
    if adopt:
        for category in manifest['categories']:
            layout = safe_path(root, category + '/标注工程/annotation-layout.json')
            if not layout.is_file():
                atomic_json(layout, {'schema': 'dated-annotations-v1'}, backup=False)
        atomic_json(root / ASSIGNMENT, manifest, backup=False)
    return root, manifest


def adopt_unpacked_task(path):
    """Best effort on project open: an unpacked task becomes a normal assigned task; anything else is left alone."""
    try:
        return task_assignment(path)[1]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def default_return_destination(root):
    """Where an annotation return goes by default: next to the raw package folder the task came in."""
    return Path(root).resolve().parent.parent


def make_return(root, *, destination=None, cancelled=lambda: False, progress=lambda *_: None):
    """Annotation return of an assigned task: a folder with the labels, their evidence images and
    ``协作清单.json``, written into ``destination`` (default: next to the raw package folder)."""
    root, assignment = task_assignment(root)
    entries, annotations = {}, []
    with DatasetLease([root], 'review'):
        for relative in _raw_members(assignment):
            if '/Motion/' not in relative and '/PPG/' not in relative:
                continue
            check(cancelled)
            label = _annotation_path(root, relative)
            if not label.is_file():
                continue
            assert_not_being_written(label)
            doc = json.loads(label.read_text(encoding='utf-8-sig'))
            target = _validate_document(doc, root, assignment)
            if target != label:
                raise ValueError('标注目录与派发目录树不一致')
            doc['source']['project_root_hint'] = ''
            payload = canonical(doc)
            rel = label.relative_to(root).as_posix()
            annotations.append(rel)
            entries[rel] = dict(path=rel, payload=payload, size=len(payload))
            entries.update(_evidence_entries(doc, label, root))
        if not annotations:
            raise ValueError('本任务尚未保存标注结果')
        manifest = {k: copy.deepcopy(assignment[k]) for k in ('schema', 'task_id', 'package_id', 'farm_id', 'farm_name', 'label_schema', 'base_name')}
        # 4.4.5: the task manifest travels with the return, so a dispatcher whose 派发记录 was lost
        # (new computer, restored backup) can still verify and receive it against its own raw data.
        manifest.update(kind='annotations', revision=uuid4().hex, annotations=annotations,
                        source_manifest_sha256=_manifest_digest(assignment), source_manifest=copy.deepcopy(assignment),
                        purpose=assignment.get('purpose', 'annotation'))
        target = check_destination(root, destination or default_return_destination(root))
        _remove_stale_partials(target)
        output = target / (manifest['base_name'] + RETURN_TAG + manifest['revision'][:12])
        _write_folder(output, manifest, list(entries.values()), cancelled=cancelled, progress=progress)
        return output


def _semantic_document(doc):
    doc = copy.deepcopy(doc)
    doc.get('source', {}).pop('project_root_hint', None)
    return doc


def _recover_receives(root):
    """Rollback interrupted publication only when its expected bytes still match."""
    for journal_path in (collaboration_home(root) / '接收记录').glob('*/事务.json'):
        journal = read_json(journal_path, {})
        if journal.get('status') != 'committing':
            continue
        entries = journal.get('publications', [])
        for entry in entries:
            target = safe_path(root, entry['path'])
            if target.exists() and digest_file(target) != entry['sha256']:
                raise ValueError('Interrupted import contains changed work; inspect ' + str(journal_path))
        for entry in reversed(entries):
            safe_path(root, entry['path']).unlink(missing_ok=True)
        journal['status'] = 'rolled_back'
        atomic_json(journal_path, journal, backup=False)


def _publish_annotation(root, relative, payload, source, journal, journal_path, expected=None):
    """Publish a fully flushed file atomically; never expose a partial label."""
    target = safe_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name('.receive-' + uuid4().hex)
    try:
        with temporary.open('xb') as output:
            if source is not None:
                with source.open('rb') as incoming:
                    shutil.copyfileobj(incoming, output, CHUNK)
            else:
                output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        sha = digest_file(temporary)
        if expected is None:
            expected = digest_file(source) if source is not None else hashlib.sha256(payload).hexdigest()
        if sha != expected:
            raise OSError('Annotation staging verification failed')
        if target.exists():
            raise FileExistsError(str(target))
        journal['publications'].append(dict(path=relative, sha256=sha))
        atomic_json(journal_path, journal, backup=False)
        if os.name == 'nt':
            temporary.rename(target)  # Windows rename refuses an existing target.
        else:
            os.link(temporary, target)
        journal['created'].append(relative)
        if digest_file(target) != sha:
            raise OSError('写入标注后的二次核验失败')
        atomic_json(journal_path, journal, backup=False)
    finally:
        temporary.unlink(missing_ok=True)


def _kept_raw_manifests(root):
    """(source, manifest) of the raw packages kept in 科牧特_协作标注\\原始数据包: folders and ≤4.4.4 ZIPs."""
    found = []
    for entry in _listing(collaboration_home(root) / RAW_INBOX):
        if _linked(entry) or _PARTIAL.fullmatch(entry.name):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                listed = Path(entry.path) / MANIFEST
                if listed.is_file() and listed.stat().st_size <= 64 * 1024**2:
                    found.append((entry.path, json.loads(listed.read_bytes())))
            elif entry.name.lower().endswith('.zip'):
                with zipfile.ZipFile(entry.path) as bundle:
                    if MANIFEST in bundle.namelist() and bundle.getinfo(MANIFEST).file_size <= 64 * 1024**2:
                        found.append((entry.path, json.loads(bundle.read(MANIFEST))))
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
    return found


def _recover_assignment(root, manifest):
    """4.4.5: rebuild a missing dispatch record (``派发记录``), e.g. after moving to a new computer.

    Candidates are the task manifest carried by the return itself and the raw packages kept in
    ``科牧特_协作标注\\原始数据包`` (only their small 协作清单.json is read). A candidate is used
    only if its digest equals the one the annotator signed into the return and it belongs to this
    farm; every label is then still verified against this computer's own raw bytes.
    """
    digest = manifest.get('source_manifest_sha256')
    identity = farm_identity(root) or {}
    candidates = []
    if isinstance(manifest.get('source_manifest'), dict):
        candidates.append(('', manifest['source_manifest']))
    candidates += _kept_raw_manifests(root)
    for source, value in candidates:
        if (_raw_task(value) and value.get('package_id') == manifest['package_id']
                and value.get('farm_id') == identity.get('farm_id') and digest and _manifest_digest(value) == digest):
            return value, source
    raise ValueError('本机没有该任务的派发记录（派发记录\\' + manifest['package_id'] + '.json）：请把对应的原始数据包（文件夹或 ZIP）放入 '
                     + str(collaboration_home(root) / RAW_INBOX) + ' 后重新接收，或请标注员用 4.4.5 及以上版本重新生成标注数据包')


def receive_return(root, path, *, archive_to=None, cancelled=lambda: False, progress=lambda *_: None):
    """Verify one annotation return (a folder, or a ≤4.4.4 ZIP) completely, then merge it into the farm.

    ``archive_to`` (4.4.5): after the merge the return is moved into this folder; a return held back
    for conflicts is moved into 科牧特_协作标注\\冲突待核对\\<任务> beside its report; a refused one
    stays where it is. Without ``archive_to`` the return is left in place (a conflicting one is copied).
    """
    root = Path(root).resolve(strict=True)
    path = Path(path).resolve(strict=True)
    folder = path.is_dir()
    manifest = _peek_folder(path) if folder else validate_archive(path)
    identity = farm_identity(root)
    if manifest['kind'] != 'annotations' or not identity or manifest['farm_id'] != identity['farm_id']:
        raise ValueError('标注包不属于此牧场' if manifest['kind'] == 'annotations' else '这是原始数据包，不是标注数据包')
    if folder:  # only now hash the (small) return, file by file
        manifest = validate_folder(path, strict=True, cancelled=cancelled)
    if archive_to is not None:
        archive_to = check_destination(root, archive_to)
        if archive_to == path or archive_to.is_relative_to(path):
            raise ValueError('不能把标注数据包移到它自己的文件夹里')
    record = _registry(root) / (manifest['package_id'] + '.json')
    original = read_json(record, {})
    recovered = None
    if original:
        assignment = original.get('manifest', {})
        if original.get('status') != 'ready' or _manifest_digest(assignment) != manifest.get('source_manifest_sha256'):
            raise ValueError('找不到对应的原始派发记录或任务清单不一致')
    else:
        assignment, recovered = _recover_assignment(root, manifest)
    annotations = manifest.get('annotations', [])
    if not annotations or len(set(annotations)) != len(annotations):
        raise ValueError('没有有效且唯一的标注记录')
    members = {m['path']: m for m in manifest['members']}
    for relative in members:
        safe_path(root, relative)
        if relative not in annotations and not ('/标注工程/' in relative and '/证据/' in relative and relative.endswith('.jpg')):
            raise ValueError('回传包含未授权文件或原始数据')
    if not set(annotations) <= set(members):
        raise ValueError('清单列出的标注文件不在数据包里')
    home = collaboration_home(root)
    report = dict(package_id=manifest['package_id'], package=str(path), imported=0, unchanged=0, conflicts=[], files=[])
    held = journal_dir = None
    with DatasetLease([root], 'organize'), ExitStack() as locks, tempfile.TemporaryDirectory(prefix='cowmata-receive-') as temporary:
        for category in {u['category'] for u in assignment['units']}:
            meta = safe_path(root, category + '/标注工程')
            meta.mkdir(parents=True, exist_ok=True)
            lock = ProjectLock(meta / 'writer.lock')
            locks.callback(lock.close)
            if not lock.acquired:
                raise ValueError('相关标注工程正在使用，请保存并关闭该工程后接收')
        _recover_receives(root)
        if folder:
            staging = path  # verified file by file below against the SHA-256 in 协作清单.json
        else:
            staging = Path(temporary)
            _extract(path, staging, manifest, cancelled, progress)
        pending, allowed_evidence = [], set()
        for relative in annotations:
            check(cancelled)
            saved = safe_path(staging, relative)
            data = saved.read_bytes()
            if hashlib.sha256(data).hexdigest() != members[relative]['sha256']:
                raise ValueError('标注数据包在核验期间被改动：' + relative)
            doc = json.loads(data.decode('utf-8-sig'))
            target = _validate_document(doc, root, assignment)
            if target.relative_to(root).as_posix() != relative:
                raise ValueError('标注包目录树与本地目录不一致')
            evidence = _evidence_entries(doc, saved, staging)
            allowed_evidence.update(evidence)
            doc['source']['project_root_hint'] = str(root)
            if target.exists():
                previous = json.loads(target.read_text(encoding='utf-8-sig'))
                if _semantic_document(previous) == _semantic_document(doc):
                    report['unchanged'] += 1
                else:
                    report['conflicts'].append(relative)
            else:
                pending.append((relative, canonical(doc)))
        if set(members) != set(annotations) | allowed_evidence:
            raise ValueError('清单存在未引用或缺失的证据图')
        if recovered is not None:
            # Every label has now been verified against this farm's own raw bytes.
            atomic_json(record, dict(status='ready', manifest=assignment, output=recovered,
                                     recovered=dict(source=recovered or path.name,
                                                    at=datetime.now(timezone.utc).isoformat())), backup=False)
            report['recovered_dispatch_record'] = str(record)
        if report['conflicts']:
            # Entire return is held, never partially applied across conflicting records.
            held = home / CONFLICT_INBOX / (manifest['package_id'] + '-' + uuid4().hex[:8])
            held.mkdir(parents=True)
            report['archive'] = str(held)
            atomic_json(held / '核验报告.json', report, backup=False)
        else:
            for relative in allowed_evidence:
                target = safe_path(root, relative)
                if target.exists() and digest_file(target) != members[relative]['sha256']:
                    raise ValueError('本地证据图同名但内容不同，停止接收')
            check(cancelled)
            journal_dir = home / '接收记录' / (manifest['package_id'] + '-' + uuid4().hex)
            journal_dir.mkdir(parents=True)
            journal = dict(status='committing', package_id=manifest['package_id'], created=[], publications=[], planned=[r for r, _ in pending])
            atomic_json(journal_dir / '事务.json', journal, backup=False)
            try:
                for relative in sorted(allowed_evidence):
                    target = safe_path(root, relative)
                    if not target.exists():
                        _publish_annotation(root, relative, None, safe_path(staging, relative), journal, journal_dir / '事务.json',
                                            expected=members[relative]['sha256'])
                for relative, payload in pending:
                    _publish_annotation(root, relative, payload, None, journal, journal_dir / '事务.json')
                    report['imported'] += 1
                    report['files'].append(relative)
                for category in {u['category'] for u in assignment['units']}:
                    atomic_json(safe_path(root, category + '/标注工程/annotation-layout.json'), {'schema': 'dated-annotations-v1'}, backup=False)
                journal['status'] = 'complete'
                atomic_json(journal_dir / '事务.json', journal, backup=False)
                atomic_json(journal_dir / '核验报告.json', report, backup=False)
            except Exception:
                # Only paths exclusively created by this transaction, never pre-existing work.
                for relative in reversed(journal['created']):
                    safe_path(root, relative).unlink()
                journal['status'] = 'rolled_back'
                atomic_json(journal_dir / '事务.json', journal, backup=False)
                raise
    # The farm is released; moving the return itself never touches labelled data.
    try:
        if held is not None and archive_to is not None:
            report['moved_to'] = str(move_package(path, held, cancelled=cancelled, progress=progress))
        elif held is not None:
            if folder:
                shutil.copytree(path, held / path.name)
            else:
                shutil.copyfile(path, held / '待核对标注包.zip')
        elif archive_to is not None:
            report['moved_to'] = str(move_package(path, archive_to, cancelled=cancelled, progress=progress))
    except (OSError, ValueError) as exc:
        report['not_moved'] = str(exc)
    if held is not None or (journal_dir is not None and ('moved_to' in report or 'not_moved' in report)):
        atomic_json((held or journal_dir) / '核验报告.json', report, backup=False)
    return report


def drive_roots():
    """[(root, kind)] of usable drives: local, removable and network (optical drives are left out)."""
    if os.name != 'nt':
        return []
    import ctypes
    kinds = {2: '移动磁盘', 3: '本地磁盘', 4: '网络驱动器', 6: '内存盘'}
    try:
        drives = os.listdrives()
    except (AttributeError, OSError):
        drives = [letter + ':\\' for letter in 'CDEFGHIJKLMNOPQRSTUVWXYZ' if os.path.exists(letter + ':\\')]
    found = []
    for drive in drives:
        kind = kinds.get(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive)))
        if kind:
            found.append((drive, kind))
    return found


@contextmanager
def _no_drive_popups():
    """Probing an empty card reader or removed media must not raise a Windows “no disk” dialog."""
    if os.name != 'nt':
        yield
        return
    import ctypes
    kernel32 = ctypes.windll.kernel32
    previous = ctypes.c_uint()
    changed = kernel32.SetThreadErrorMode(0x0001 | 0x8000, ctypes.byref(previous))  # FAILCRITICALERRORS | NOOPENFILEERRORBOX
    try:
        yield
    finally:
        if changed:
            kernel32.SetThreadErrorMode(previous.value, None)


def _free_text(path):
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return ''
    return '可用 ' + (f'{free / 1024**4:.1f} TB' if free >= 1024**4 else f'{free / 1024**3:.0f} GB')


def destination_candidates(mode, root=None, *, package=None, recent=(), drives=None):
    """Suggested target folders of one collaboration task as [(folder, note)]: default first, no duplicates.

    Defaults — dispatch: 科牧特_协作标注\\原始数据包; returns: next to the task's raw package folder;
    receive: 科牧特_协作标注\\已接收; open: where the package is now (opened in place). Then recently
    used folders and, for dispatch / returns / open, the drive roots (the system drive only when there is
    no other) with their first-level folders whose names suggest an exchange folder (派包、协作、转移 …).
    """
    found, seen = [], set()

    def add(folder, note):
        key = os.path.normcase(os.path.abspath(str(folder)))
        if key not in seen:
            seen.add(key)
            found.append((str(Path(folder)), note))

    farm = None
    if root and str(root).strip():
        try:
            start = Path(str(root).strip()).resolve()
            if (start / MANIFEST).is_file():  # a package folder (wherever it is kept): its farm sits inside
                name = read_json(start / MANIFEST, {}).get('farm_name')
                farm = start / name if isinstance(name, str) and farm_identity(start / name) else None
            if farm is None:
                farm = shared_farm(start)
        except (OSError, ValueError, TypeError, AttributeError):
            farm = None
    if mode == 'dispatch' and farm is not None:
        add(collaboration_home(farm) / RAW_INBOX, '默认 · 本机协作目录')
    elif mode == 'returns' and farm is not None:
        add(default_return_destination(farm), '默认 · 与原始数据包放在一起')
    elif mode == 'receive' and farm is not None:
        add(collaboration_home(farm) / RECEIVED_INBOX, '默认 · 协作目录“已接收”')
    elif mode == 'open' and package:
        source = Path(package).resolve()
        if source.suffix.lower() != '.zip' and not (source / MANIFEST).is_file() and (source.parent / MANIFEST).is_file():
            source = source.parent  # the farm folder inside the package was picked
        add(source.parent, '默认 · ' + ('解包到 ZIP 所在文件夹' if source.suffix.lower() == '.zip' else '原位置打开（不移动）'))
    for folder in recent or ():
        if folder and Path(folder).is_dir():
            add(folder, '最近使用')
    if mode in {'dispatch', 'returns', 'open'}:
        with _no_drive_popups():
            roots = drive_roots() if drives is None else list(drives)
            system = os.path.splitdrive(os.environ.get('SystemRoot', 'C:\\'))[0].upper()
            others = [item for item in roots if os.path.splitdrive(item[0])[0].upper() != system]
            for drive, kind in others or roots:
                add(drive, kind + ('' if kind == '网络驱动器' else ' · ' + _free_text(drive)))
                if kind == '网络驱动器':
                    continue
                for entry in _folders(drive):
                    if (not re.match(r'\d_', entry.name) and not entry.name.startswith(('$', '.'))
                            and any(hint in entry.name for hint in _TARGET_HINTS)):
                        add(entry.path, kind)
    return found[:24]
