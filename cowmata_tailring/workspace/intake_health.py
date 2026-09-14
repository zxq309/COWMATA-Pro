"""Confirm blank camera storage slots by reading every byte, then cache identity."""
import hashlib
import json
from pathlib import Path

from . import organization as core
from .storage import atomic_json


def blank_recording(path, cache, cancelled=lambda: False):
    path, cache = Path(path), Path(cache)
    stamp = core.file_stamp(path)
    key = hashlib.sha256(str(path).encode()).hexdigest()
    saved = cache / (key+'.blank-362.json')
    try:
        record = json.loads(saved.read_text(encoding='utf-8'))
        if record.get('stamp') == stamp and record.get('all_zero'):
            return record
    except (OSError, ValueError):
        pass
    with path.open('rb', buffering=0) as stream:
        head = stream.read(128*1024)
        if head.count(0) != len(head):
            return None
        digest = hashlib.sha256(head)
        checked = len(head)
        while block := stream.read(8*1024*1024):
            core.check_cancel(cancelled)
            if block.count(0) != len(block):
                return None
            digest.update(block)
            checked += len(block)
    core.check_cancel(cancelled)
    if core.file_stamp(path) != stamp:
        raise OSError('核验期间文件变化，稍后自动重试')
    record = dict(stamp=stamp, all_zero=True, checked_bytes=checked,
                  sha256=digest.hexdigest(), source=str(path))
    atomic_json(saved, record, backup=False)
    return record
