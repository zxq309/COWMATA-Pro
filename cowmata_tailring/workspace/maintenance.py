"""Bounded housekeeping; resumable jobs and scientific data are never disposable."""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import ExitStack
from pathlib import Path

from .storage import ProjectLock, atomic_json, recovery_path

_projects: set[Path] = set()


def clean_version_cache(root, meta, version):
    """Expire recorded playback caches once per release, under an exclusive lease.

    Only manifest-owned containers and validated packet-cache documents qualify.
    The catalog, corrections, previews, evidence and resumable jobs are retained.
    Failed or busy cleanup is retried on a later project open.
    """
    from .dataset_access import DatasetLease
    from .storage import read_json
    root, meta = Path(root).absolute(), Path(meta).absolute()
    marker = meta / '.cache-version.json'
    if not meta.is_dir() or not _plain(meta, root):
        return
    try:
        if read_json(marker, {}).get('version') == version:
            return
        with DatasetLease([root], 'maintenance'):
            lock = ProjectLock(meta / 'writer.lock')
            try:
                if not lock.acquired:
                    return
                cache = meta / 'cache'
                if cache.exists() and not _plain(cache, meta):
                    return
                compat = cache / 'compatibility'
                manifest = compat / 'manifest.json'
                if compat.exists():
                    if not _plain(compat, meta) or not _plain(manifest, meta):
                        return
                    entries = read_json(manifest, {})
                    if not isinstance(entries, dict):
                        return
                    retained = dict(entries)
                    for asset, entry in entries.items():
                        if not re.fullmatch(r'[0-9a-f]{64}', asset) or not isinstance(entry, dict):
                            continue
                        path = compat / (asset + '.mkv')
                        if not _plain(path, meta):
                            return
                        if path.is_file():
                            if entry.get('method') != 'stream_copy':
                                continue
                            path.unlink()
                        retained.pop(asset, None)
                    if manifest.is_file():
                        atomic_json(manifest, retained, backup=False)
                for folder, suffix, parse in _timeline_cache_types():
                    directory = cache / folder
                    if not directory.exists():
                        continue
                    if not _plain(directory, meta):
                        return
                    for path in directory.glob('*' + suffix):
                        if not re.fullmatch(r'[0-9a-f]{20}' + re.escape(suffix), path.name) or not _plain(path, meta):
                            continue
                        try:
                            parse(json.loads(path.read_text(encoding='utf-8')))
                        except (ValueError, TypeError, KeyError):
                            continue
                        path.unlink()
                atomic_json(marker, {'version': version}, backup=False)
            finally:
                lock.close()
    except (OSError, ValueError, TypeError):
        return


def _timeline_cache_types():
    from cowmata_tailring.media.dahua_duration import DahuaDurationIndex
    from cowmata_tailring.media.timeline import MediaTimelineIndex
    return (
        ('packet-timelines', '.timeline.json', MediaTimelineIndex.from_dict),
        ('dahua-timelines', '.dahua-duration.json', DahuaDurationIndex.from_dict),
    )


def remember_project(path):
    """Only remember explicit project roots touched by this process."""
    try:
        path = Path(path).absolute()
        if path.resolve() == path and ((path / '.cowmata-farm.json').is_file()
                                      or (path / '资源索引.json').is_file()):
            _projects.add(path)
    except OSError:
        pass  # Optional housekeeping must never prevent opening/saving data.


def _plain(path, root):
    try:
        return (path.resolve() == path and path.is_relative_to(root)
                and not path.is_symlink() and not path.is_junction())
    except OSError:
        return False


def _empty_dirs(path, root, report, deadline, budget):
    """rmdir only: no wildcard deletion, no links, no nonempty directory removal."""
    if not _plain(path, root) or not path.is_dir() or time.monotonic() >= deadline:
        return
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if budget[0] <= 0 or time.monotonic() >= deadline:
                    return
                budget[0] -= 1
                child = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    _empty_dirs(child, root, report, deadline, budget)
        path.rmdir()  # Fails safely if any file appeared or remains.
        report['removed_dirs'] += 1
    except OSError:
        pass


def _index_backup(scope, report):
    primary = scope / '资源索引.json'
    old = scope / '资源索引.json.bak'
    if not old.is_file() or not all(_plain(p, scope) for p in (primary, old)):
        return
    try:
        current = json.loads(primary.read_text(encoding='utf-8'))
        previous = json.loads(old.read_text(encoding='utf-8'))
        if not all(isinstance(v, dict) and isinstance(v.get('records'), list)
                   for v in (current, previous)):
            return
        target = recovery_path(primary)
        # Retain the most recent recovery snapshot. Never remove the only
        # fallback while the primary is corrupt or the new copy is not verified.
        if not target.is_file() or old.stat().st_mtime_ns > target.stat().st_mtime_ns:
            atomic_json(target, previous, backup=False)
        recovered = json.loads(target.read_text(encoding='utf-8'))
        if not isinstance(recovered, dict) or not isinstance(recovered.get('records'), list):
            return
        old.unlink()
        report['relocated_backups'] += 1
    except (OSError, ValueError):
        pass


def clean_project(root, *, seconds=2.0, max_entries=2000):
    """Remove empty staging folders and relocate valid index recovery copies.

    A maintenance lease excludes all live readers/writers, including another
    Pro instance. Pending jobs remain untouched and keep all nonempty staging.
    """
    from .dataset_access import DatasetLease
    from .farm_layout import CATEGORY_PATHS
    root = Path(root).absolute()
    report = dict(removed_dirs=0, relocated_backups=0, skipped=False)
    if not _plain(root, root) or not root.is_dir():
        report['skipped'] = True
        return report
    scopes = [root] + [root / name for name in CATEGORY_PATHS
                       if (root / name).is_dir() and _plain(root / name, root)]
    try:
        with DatasetLease([root], 'maintenance'), ExitStack() as locks:
            for scope in scopes:
                legacy = scope / '标注工程/writer.lock'
                if legacy.is_file():
                    lock = ProjectLock(legacy)
                    locks.callback(lock.close)
                    if not lock.acquired:
                        report['skipped'] = True
                        return report
            deadline, budget = time.monotonic() + seconds, [max_entries]
            for scope in scopes:
                if time.monotonic() >= deadline:
                    break
                _index_backup(scope, report)
                _empty_dirs(scope / '.归类缓存', root, report, deadline, budget)
    except OSError:
        report['skipped'] = True
    return report


def cleanup_session():
    """Called after the GUI has saved and drained its workers on normal exit."""
    from .dataset_access import _active, _locked_registry
    results = {}
    for root in sorted(_projects):
        results[str(root)] = clean_project(root)
    try:
        root, lock = _locked_registry()
        try:
            list(_active(root))  # OS locks decide stale ownership, never age/PID alone.
        finally:
            lock.close()
    except OSError:
        pass
    return results
