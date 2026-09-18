"""Journalled same-volume relocation; source sensor bytes never change."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from uuid import uuid4

from .catalog import digest_file, file_stamp
from .dataset_access import DatasetLease
from .farm_layout import CATEGORY_PATHS, COLLABORATION, MARKER, SCHEMA, farm_identity
from .package_paths import safe_path
from .package_readiness import download_guard
from .storage import ProjectLock, atomic_json, read_json


def audit(root):
    root = Path(root).resolve(strict=True)
    moves, files, collisions = [], [], []
    def collect(source, target):
        safe_path(root, source.relative_to(root).as_posix())
        safe_path(root, target.relative_to(root).as_posix())
        if target.exists():
            if source.is_dir() and target.is_dir():
                for child in sorted(source.iterdir()):
                    collect(child, target / child.name)
            else:
                collisions.append(str(target))
        else:
            moves.append(dict(source=source.relative_to(root).as_posix(), target=target.relative_to(root).as_posix()))
    reserved = set()
    for category in CATEGORY_PATHS:
        video = root / category / 'Video'
        if not video.is_dir():
            continue
        safe_path(root, category + '/Video')
        for path in video.rglob('*'):
            relative = path.relative_to(root).as_posix()
            safe_path(root, relative)
            if path.is_file():
                target = (Path('录像') / path.relative_to(video)).as_posix()
                key = target.casefold()
                if key in reserved:
                    collisions.append(target)
                reserved.add(key)
                files.append(dict(source=relative, target=target, stamp=file_stamp(path)))
        for child in sorted(video.iterdir()):
            collect(child, root / '录像' / child.name)
    return dict(root=str(root), moves=moves, assets=files, files=len(files),
                bytes=sum(json.loads(r['stamp'])[0] for r in files), collisions=collisions)


def rebase(value, category, root, *, key=''):
    if isinstance(value, dict):
        return {rebase(k, category, root, key='path'): rebase(v, category, root, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [rebase(v, category, root, key=key) for v in value]
    if not isinstance(value, str) or key in {'note', 'notes', 'message', 'description', 'text'}:
        return value
    normalized = value.replace('\\', '/')
    old = (root / category).as_posix()
    if normalized == old and key in {'project_root_hint', 'archive_root_hint'}:
        return str(root)
    if normalized.startswith(old + '/Video/'):
        return (root / '录像' / normalized[len(old+'/Video/'):]).as_posix()
    if normalized.startswith('Video/'):
        return '录像/' + normalized[len('Video/'):]
    if normalized.startswith(('Motion/', 'PPG/', 'Temp/')):
        return category + '/' + normalized
    return value


def _prepare_metadata(root, staging):
    changes = []
    for category in CATEGORY_PATHS:
        scope = root / category
        meta = scope / '标注工程'
        candidates = list(meta.rglob('*.json')) if meta.is_dir() else []
        candidates += [p for p in (scope / '资源索引.json',) if p.is_file()]
        for path in candidates:
            relative = path.relative_to(root).as_posix()
            safe_path(root, relative)
            original = path.read_bytes()
            value = json.loads(original.decode('utf-8-sig'))
            updated = rebase(value, category, root)
            if updated == value:
                continue
            old, new = staging / 'before' / relative, staging / 'after' / relative
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_bytes(original)
            atomic_json(new, updated, backup=False)
            changes.append(relative)
        index = meta / 'index.sqlite'
        if index.is_file():
            relative = index.relative_to(root).as_posix()
            old, new = staging / 'before' / relative, staging / 'after' / relative
            old.parent.mkdir(parents=True, exist_ok=True)
            new.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(index)) as db, closing(sqlite3.connect(old)) as backup:
                db.backup(backup)
            shutil.copyfile(old, new)
            with closing(sqlite3.connect(new)) as db, db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ('locations', 'revisions', 'video_hints'):
                    if table not in tables:
                        continue
                    for rowid, path in db.execute('SELECT rowid,path FROM ' + table).fetchall():
                        db.execute('UPDATE ' + table + ' SET path=? WHERE rowid=?', (rebase(path, category, root), rowid))
                for table in ('assets', 'video_hints'):
                    if table not in tables:
                        continue
                    for rowid, metadata in db.execute('SELECT rowid,metadata FROM ' + table).fetchall():
                        value = rebase(json.loads(metadata), category, root)
                        db.execute('UPDATE ' + table + ' SET metadata=? WHERE rowid=?', (json.dumps(value, ensure_ascii=False), rowid))
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('迁移后的素材索引检查失败')
            changes.append(relative)
    return changes


def _recover(root):
    """Recover a stopped migration before beginning another one."""
    for record in (root / COLLABORATION / '目录迁移记录').glob('*/事务.json'):
        journal = read_json(record, {})
        if journal.get('status') in {'complete', 'rolled_back'}:
            continue
        folder = record.parent
        marker = root / MARKER
        if marker.exists() and farm_identity(root) != journal.get('identity'):
            raise ValueError('Migration recovery found another farm identity')
        for relative in journal.get('metadata', []):
            target = safe_path(root, relative)
            before = safe_path(folder / 'before', relative)
            after = safe_path(folder / 'after', relative)
            if digest_file(target) not in (digest_file(before), digest_file(after), journal.get('original_hashes', {}).get(relative)):
                raise ValueError('Migration recovery found modified metadata: ' + str(target))
        for entry in journal['moves']:
            source, target = safe_path(root, entry['source']), safe_path(root, entry['target'])
            if source.exists() == target.exists():
                raise ValueError('Migration recovery found ambiguous paths: ' + str(record))
        for relative in journal.get('metadata', []):
            target = safe_path(root, relative)
            temporary = target.with_name('.recover-' + uuid4().hex)
            shutil.copyfile(safe_path(folder / 'before', relative), temporary)
            os.replace(temporary, target)
        for entry in reversed(journal['moves']):
            source, target = safe_path(root, entry['source']), safe_path(root, entry['target'])
            if target.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                target.rename(source)
        marker = root / MARKER
        if marker.exists():
            if farm_identity(root) != journal.get('identity'):
                raise ValueError('Migration recovery found another farm identity')
            marker.unlink()
        journal['status'] = 'rolled_back'
        atomic_json(record, journal, backup=False)


def migrate(root, *, progress=lambda *_: None):
    root = Path(root).resolve(strict=True)
    with DatasetLease([root], 'organize'), download_guard(root), ExitStack() as locks:
        for category in CATEGORY_PATHS:
            meta = root / category / '标注工程'
            if meta.is_dir():
                lock = ProjectLock(meta / 'writer.lock')
                locks.callback(lock.close)
                if not lock.acquired:
                    raise ValueError('相关标注工程正在使用，请先保存并关闭：' + str(meta))
        _recover(root)
        if farm_identity(root):
            return dict(status='already_current', files=0, root=str(root))
        report = audit(root)
        if report['collisions']:
            raise ValueError('录像目标存在冲突，未移动文件：' + '\n'.join(report['collisions'][:20]))
        journal_dir = root / COLLABORATION / '目录迁移记录' / uuid4().hex
        journal_dir.mkdir(parents=True)
        changes = _prepare_metadata(root, journal_dir)
        identity = dict(schema=SCHEMA, farm_id=str(uuid4()), recordings='录像')
        journal = dict(status='prepared', root=str(root), moves=report['moves'], metadata=changes, moved=[], written=[], identity=identity)
        journal['original_hashes'] = {r: digest_file(safe_path(root, r)) for r in changes}
        atomic_json(journal_dir / '事务.json', journal, backup=False)
        moved, written = [], []
        try:
            for index, entry in enumerate(report['moves']):
                source, target = safe_path(root, entry['source']), safe_path(root, entry['target'])
                if source.stat().st_dev != root.stat().st_dev:
                    raise ValueError('迁移来源不在同一磁盘，停止自动移动')
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise FileExistsError('目标在迁移期间出现，停止覆盖：' + str(target))
                journal['next_move'] = entry
                atomic_json(journal_dir / '事务.json', journal, backup=False)
                source.rename(target)
                moved.append(entry)
                journal['moved'] = list(moved)
                journal['status'] = 'moving'
                atomic_json(journal_dir / '事务.json', journal, backup=False)
                progress(index + 1, len(report['moves']), entry['target'])
            for asset in report['assets']:
                before = json.loads(asset['stamp'])
                after = json.loads(file_stamp(safe_path(root, asset['target'])))
                if before[:2] != after[:2] or before[3] != after[3]:
                    raise ValueError('迁移后文件大小、修改时间或文件身份发生变化')
            for relative in changes:
                target = safe_path(root, relative)
                staged = safe_path(journal_dir / 'after', relative)
                # All metadata was parsed/prepared before the first rename.
                temporary = target.with_name(target.name + '.migration-' + uuid4().hex)
                shutil.copyfile(staged, temporary)
                journal['next_metadata'] = relative
                atomic_json(journal_dir / '事务.json', journal, backup=False)
                os.replace(temporary, target)
                written.append(relative)
                journal['written'] = list(written)
                atomic_json(journal_dir / '事务.json', journal, backup=False)
            atomic_json(root / MARKER, identity, backup=False)
            (root / '录像').mkdir(exist_ok=True)
            # Remove only verified empty old video directories.
            for category in CATEGORY_PATHS:
                video = root / category / 'Video'
                if video.is_dir():
                    for folder in sorted((p for p in video.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                        if not any(folder.iterdir()):
                            folder.rmdir()
                    if not any(video.iterdir()):
                        video.rmdir()
            journal['status'] = 'complete'
            atomic_json(journal_dir / '事务.json', journal, backup=False)
            result = {k: v for k, v in report.items() if k != 'assets'}
            result.update(status='complete', journal=str(journal_dir), metadata_files=len(changes), farm_id=identity['farm_id'])
            atomic_json(journal_dir / '核验报告.json', result, backup=False)
            return result
        except Exception:
            for relative in reversed(written):
                shutil.copyfile(safe_path(journal_dir / 'before', relative), safe_path(root, relative))
            for entry in reversed(moved):
                source, target = safe_path(root, entry['source']), safe_path(root, entry['target'])
                source.parent.mkdir(parents=True, exist_ok=True)
                target.rename(source)
            (root / MARKER).unlink(missing_ok=True)
            journal['status'] = 'rolled_back'
            atomic_json(journal_dir / '事务.json', journal, backup=False)
            raise
