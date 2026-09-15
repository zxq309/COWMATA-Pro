"""One current row per source, shared by the CSV and both operator views."""
from __future__ import annotations

import csv
import io
import json
import ntpath
import os
import tempfile
import time
from collections import Counter
from pathlib import Path

from . import organization as core
from .storage import atomic_json

CSV_FIELDS = ['序号', '状态', '采集日期', '视角/设备', '来源文件', '归类位置', '耗时(秒)', '说明']
LABELS = {'pending': '待处理', 'ready': '待归类', 'existing': '待校验',
          'processing': '处理中', 'done': '已归类', 'empty_video': '无录像内容',
          'excluded_aux': '非采集文件', 'blocked': '异常', 'invalid': '异常',
          'skip': '已保留', 'deleted': '已删除', 'quarantine': '已隔离', 'junk': '待处理'}
UNAVAILABLE = {'empty_video', 'excluded_aux', 'skip', 'deleted', 'quarantine'}


def source_key(source):
    value = str(source)
    if ntpath.isabs(value) and (ntpath.splitdrive(value)[0] or '\\' in value):
        return ntpath.normcase(ntpath.normpath(value))
    return os.path.normcase(os.path.abspath(value))


def latest_rows(rows):
    result = {}
    for row in rows:
        if row.get('source'):
            result[source_key(row['source'])] = dict(row)
    return list(result.values())


def status_label(row):
    if row.get('status') == 'done' and (row.get('existing_verified') or row.get('resumed_complete')):
        return '已复用'
    return LABELS.get(row.get('status'), row.get('status', ''))


def counts(rows):
    rows = latest_rows(rows)
    states = Counter(r.get('status') for r in rows)
    unavailable = sum(states[s] for s in UNAVAILABLE)
    errors = states['blocked'] + states['invalid']
    archived, processing = states['done'], states['processing']
    return dict(total=len(rows), archived=archived, processing=processing,
                unavailable=unavailable, deleted=states['deleted'], errors=errors,
                pending=len(rows)-archived-processing-unavailable-errors,
                reused=sum(r.get('status') == 'done' and bool(r.get('existing_verified') or r.get('resumed_complete')) for r in rows))


def summary_text(value):
    return (f"总计 {value['total']} · 已归类 {value['archived']}（复用 {value['reused']}）"
            f" · 已删除 {value.get('deleted', 0)} · 非采集/保留 {value['unavailable']-value.get('deleted', 0)} · 处理中 {value['processing']}"
            f" · 待处理 {value['pending']} · 异常 {value['errors']}")


def csv_bytes(rows):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(CSV_FIELDS)
    for number, row in enumerate(latest_rows(rows), 1):
        writer.writerow([number, status_label(row), row.get('record_date', ''),
                         row.get('owner') or row.get('device_id') or row.get('device', ''),
                         row.get('source', ''), row.get('target', ''),
                         row.get('file_seconds', ''), row.get('message', '')])
    return ('\ufeff' + out.getvalue()).encode('utf-8')


def atomic_bytes(path, payload):
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_snapshot(job):
    return json.loads((Path(job) / 'report-state.json').read_text(encoding='utf-8'))


class LiveReport:
    def __init__(self, job, *, on_publish=lambda *_: None):
        self.job = Path(job)
        self.job.mkdir(parents=True, exist_ok=True)
        self.path = self.job / '归类记录.csv'
        self.rows = {}
        self.revision = time.time_ns()
        self.last_flush = 0.0
        self.dirty = True
        self.phase = 'running'
        self.started_at = time.time()
        self.started_monotonic = time.monotonic()
        self.on_publish = on_publish
        self.flush(force=True)

    def seed(self, rows):
        for row in rows:
            self.rows.setdefault(source_key(row['source']), dict(row))
        self.dirty = True
        self.flush(force=True)

    def row(self, row):
        value = {**row, 'updated_at': core.now()}
        core.append_journal(self.job / 'events.jsonl', value)
        self.rows[source_key(value['source'])] = value
        self.dirty = True
        self.flush()

    def flush(self, *, force=False):
        if not self.dirty or not force and time.monotonic()-self.last_flush < .25:
            return
        rows = list(self.rows.values())
        payload = csv_bytes(rows)
        for number in range(100):
            path = self.job / ('归类记录.csv' if not number else f'归类记录-实时-{number:03d}.csv')
            try:
                atomic_bytes(path, payload)
                self.path = path
                break
            except PermissionError:
                continue
        else:
            raise OSError('实时 CSV 被占用，请关闭已打开的 CSV 文件后继续')
        self.revision += 1
        state = dict(schema='classification-report-362', revision=self.revision,
                     updated_at=core.now(), phase=self.phase, csv_path=str(self.path),
                     started_at=self.started_at, elapsed_seconds=max(0, time.monotonic()-self.started_monotonic),
                     counts=counts(rows), rows=rows)
        atomic_json(self.job/'report-state.json', state, backup=False)
        self.dirty = False
        self.last_flush = time.monotonic()
        self.on_publish(str(self.job/'report-state.json'), self.revision)

    def finish(self, phase='completed'):
        self.phase = phase
        if phase != 'completed':
            for row in self.rows.values():
                if row.get('status') == 'processing':
                    row.update(status='pending', message='已暂停，继续任务后处理')
        self.dirty = True
        self.flush(force=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        self.finish('paused' if exc_type else 'completed')
