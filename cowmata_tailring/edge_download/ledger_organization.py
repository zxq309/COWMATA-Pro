"""Byte-preserving, journalled categorization within the managed data root."""
import hashlib
import json
from pathlib import Path

from .core import Cancelled
from .deduplication import _move_noreplace, _reader
from .deduplication import _safe as _is_safe
from .ledger import CATEGORY_NAMES


def prepare_local(job,cancel,log):
    """Classify local files even when the download server is unavailable."""
    import sqlite3

    from .deduplication import RootSyncLock
    from .ledger import load_plan
    if not job.ledger_directory:
        return
    with RootSyncLock(job.farm,cancel):
        state = _safe(job.farm,job.farm/'.edge-download',missing=True)
        state.mkdir(exist_ok=True)
        dbpath = _safe(job.farm,state/'automatic.sqlite3',missing=True)
        db=sqlite3.connect(dbpath,timeout=5,isolation_level=None)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS completed (key TEXT PRIMARY KEY, revision TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL, identity TEXT NOT NULL, create_time INTEGER NOT NULL)')
            organize(job.farm,load_plan(job.ledger_directory,state,log),db,cancel,log)
        finally:
            db.rollback()
            db.close()

def _safe(root, path, missing=False):
    if not _is_safe(root,path,missing=missing):
        raise OSError(f'分类拒绝不安全路径：{path}')
    return path

def organize(root, plan, db, cancel, log):
    if not plan.value:
        return 0
    root = Path(root).resolve()
    db.execute('CREATE TABLE IF NOT EXISTS ledger_moves_v1 (source TEXT PRIMARY KEY, destination TEXT, sha256 TEXT, proof TEXT)')
    # Replay any move completed before a crash but not reflected in the cache.
    for source, destination, digest in db.execute('SELECT source,destination,sha256 FROM ledger_moves_v1').fetchall():
        old, new = _safe(root, root/source, missing=True), _safe(root, root/destination, missing=True)
        if not old.exists() and new.is_file():
            with _reader(new) as stream:
                if stream_digest(stream) == digest:
                    db.execute('UPDATE completed SET path=? WHERE path=?', (destination, source))
    marker = plan.value['sha256']
    db.execute('CREATE TABLE IF NOT EXISTS ledger_pass_v1 (fingerprint TEXT PRIMARY KEY)')
    if db.execute('SELECT 1 FROM ledger_pass_v1 WHERE fingerprint=?', (marker,)).fetchone():
        return 0
    moved = 0
    for category in CATEGORY_NAMES:
        base = _safe(root, root/category/'Motion', missing=True)
        if not base.is_dir():
            continue
        for path in list(base.rglob('*.json')):
            if cancel.is_set():
                raise Cancelled()
            path = _safe(root, path)
            with _reader(path) as stream:
                raw = stream.read()
            try:
                data = json.loads(raw)
                if not isinstance(data, dict) or not data.get('device') or not data.get('create_time'):
                    continue
                decision = plan.classify(data)
            except (ValueError, TypeError):
                log(f'分类跳过：JSON无法识别 {path.name}')
                continue
            target_category = decision['category']
            if category == target_category:
                continue
            relative = path.relative_to(root)
            destination = _safe(root, root/target_category/path.relative_to(root / category), missing=True)
            digest = hashlib.sha256(raw).hexdigest()
            if destination.exists():
                # Never overwrite or remove either copy. Deduplication remains separate.
                destination = destination.with_name(destination.stem+'-'+digest[:16]+destination.suffix)
                if destination.exists():
                    log(f'分类目标冲突，保留原文件：{relative}')
                    continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination = _safe(root, destination, missing=True)
            src, dst = str(relative), str(destination.relative_to(root))
            db.execute('INSERT OR REPLACE INTO ledger_moves_v1 VALUES (?,?,?,?)',
                       (src,dst,digest,json.dumps(decision,ensure_ascii=False)))
            # Verify source again immediately before rename; no sensor bytes are rewritten.
            with _reader(path) as stream:
                if stream_digest(stream) != digest:
                    raise OSError('分类期间文件发生变化，请下次重试')
            _move_noreplace(path,destination)
            db.execute('UPDATE completed SET path=? WHERE path=?',(dst,src))
            moved += 1
    db.execute('DELETE FROM ledger_pass_v1')
    db.execute('INSERT INTO ledger_pass_v1 VALUES (?)',(marker,))
    log(f'台账分类完成：移动 {moved} 个文件；原始JSON字节保持不变。')
    return moved


def stream_digest(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(block)
    return digest.hexdigest()
