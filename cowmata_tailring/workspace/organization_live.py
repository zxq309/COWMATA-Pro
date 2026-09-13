"""Durable, append-only CSV events, readable throughout a classification task."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

from . import organization as core

FIELDS = ['updated_at', 'source', 'target', 'kind', 'status', 'record_date',
          'message', 'size', 'recognition_seconds', 'transfer_seconds', 'file_seconds',
          'task_seconds', 'sha256', 'device', 'source_folder', 'suggested_folder',
          'device_id', 'cow_id', 'field_mark', 'record_start_ms', 'identity_provenance',
          'retry_reason', 'existing_verified']


def append_shared(path, text):
    """Windows reader/writer/delete sharing; never truncate an operator's CSV."""
    payload = text.encode('utf-8')
    if os.name != 'nt':
        with path.open('ab') as stream:
            stream.write(payload)
            stream.flush()
        return
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.WriteFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
    k.CloseHandle.argtypes = [w.HANDLE]
    handle = k.CreateFileW(str(path), 4, 7, None, 4, 0x80, None)  # FILE_APPEND_DATA / OPEN_ALWAYS
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(payload)
        written = w.DWORD()
        if not k.WriteFile(handle, buffer, len(payload), ctypes.byref(written), None) or written.value != len(payload):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k.CloseHandle(handle)


class LiveReport:
    def __init__(self, job):
        self.job = Path(job)
        self.job.mkdir(parents=True, exist_ok=True)
        self.path = self.job / 'report.csv'
        if not self.path.exists() or not self.path.stat().st_size:
            buffer = io.StringIO(newline='')
            csv.writer(buffer).writerow(FIELDS)
            append_shared(self.path, '\ufeff' + buffer.getvalue())
        else:
            with self.path.open(encoding='utf-8-sig', newline='') as stream:
                if next(csv.reader(stream), []) != FIELDS:
                    # Preserve the previous-version report verbatim.
                    self.path = self.job / 'report-live.csv'
                    if not self.path.exists():
                        buffer = io.StringIO(newline='')
                        csv.writer(buffer).writerow(FIELDS)
                        append_shared(self.path, '\ufeff' + buffer.getvalue())

    def row(self, row):
        value = {**row, 'updated_at': core.now()}
        core.append_journal(self.job / 'events.jsonl', value)
        buffer = io.StringIO(newline='')
        csv.DictWriter(buffer, fieldnames=FIELDS, extrasaction='ignore').writerow(value)
        for attempt in range(100):
            try:
                if self.path.exists() and self.path.stat().st_size:
                    with self.path.open(encoding='utf-8-sig', newline='') as stream:
                        if next(csv.reader(stream), []) != FIELDS:
                            raise PermissionError('A previous CSV schema must be retained separately')
                append_shared(self.path, buffer.getvalue())
                return
            except PermissionError:
                # Every opened Excel snapshot can hold its own exclusive lock.
                # Keep advancing to an available live file, retaining all events.
                suffix = '' if attempt == 0 else f'-{attempt:03d}'
                self.path = self.job / ('report-live' + suffix + '.csv')
                if not self.path.exists():
                    header = io.StringIO(newline='')
                    writer = csv.DictWriter(header, fieldnames=FIELDS, extrasaction='ignore')
                    writer.writeheader()
                    journal = self.job / 'events.jsonl'
                    # Last event is appended by the next loop iteration.
                    prior = journal.read_text(encoding='utf-8').splitlines()[:-1]
                    for line in prior:
                        writer.writerow(json.loads(line))
                    append_shared(self.path, '\ufeff' + header.getvalue())
        raise OSError('实时 CSV 均被独占，请关闭已打开的历史 CSV 后继续；完整记录保存在 events.jsonl')


def pending_job(paths):
    from .dataset_access import overlaps, registry_root
    matches = []
    for path in (registry_root() / 'pending').glob('*.json'):
        value = json.loads(path.read_text(encoding='utf-8'))
        if any(overlaps(a, b) for a in paths for b in value['paths']):
            job = Path(value['job'])
            if (job / 'plan.json').is_file():
                matches.append(job)
    matches = list(dict.fromkeys(matches))
    if len(matches) > 1:
        raise ValueError('存在多个未完成任务，请分别使用继续归类：' + '；'.join(map(str, matches)))
    return matches[0] if matches else None


def clean_modalities(root, job, cancelled=lambda: False):
    """Keep material trees pure; relocate ancillary files outside them."""
    root, job = Path(root).resolve(), Path(job).resolve()
    for modality, allowed in [('Motion', {'.json'}), ('PPG', {'.json'}), ('Video', core.VIDEO_SUFFIXES)]:
        directory = root / modality
        if not directory.is_dir():
            continue
        for path in core.walk_files(directory, cancelled):
            if path.suffix.lower() in allowed:
                continue
            relative = path.relative_to(root)
            target = root / '归类附属文件' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            counter = 0
            while target.exists():
                counter += 1
                target = target.with_name(path.name + f'.{counter}')
            core.append_journal(job / 'cleanup.jsonl', {'source': str(path), 'target': str(target), 'phase': 'intent'})
            core.move_no_replace(path, target)
            core.append_journal(job / 'cleanup.jsonl', {'source': str(path), 'target': str(target), 'phase': 'done'})
