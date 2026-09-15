"""Incremental signal summaries and descriptive, per-cow pattern reports."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

from cowmata_tailring.annotation.data import GRAVITY_MS2, parse_motion_object
from cowmata_tailring.workspace.storage import atomic_json

from .dataset import stamp
from .features import FEATURE_VERSION, second_features


def write_table(path, rows, fields=None):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or list(rows[0]) if rows else (fields or [])
    temp = path.with_name(path.name+'.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, path)


def load_features(record, cache):
    if not re.fullmatch(r'[0-9a-f]{64}', str(record['asset_id'])):
        raise ValueError('Invalid original content identity')
    source, cache = Path(record['raw']).resolve(), Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    before = stamp(source)
    key = FEATURE_VERSION+'-'+record['asset_id']
    meta_path, array_path = cache/(key+'.json'), cache/(key+'.npz')
    if meta_path.is_file() and array_path.is_file():
        saved = json.loads(meta_path.read_text(encoding='utf-8'))
        if saved.get('source') == str(source) and saved.get('stamp') == before:
            with np.load(array_path, allow_pickle=False) as arrays:
                result = {k: arrays[k] for k in arrays.files}
            return {**result, **saved, 'cached': True}
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != record['asset_id'] or stamp(source) != before:
        raise ValueError('Original content identity changed: '+str(source))
    document = json.loads(content.decode('utf-8-sig'))
    if 'imu' not in document:
        from .ppg_features import extract
        return extract(document, source)
    motion = parse_motion_object(document, source_path=source)
    acc = np.column_stack([motion.channels[k] for k in ('ax', 'ay', 'az')])/GRAVITY_MS2
    gyro = np.column_stack([motion.channels[k] for k in ('gx', 'gy', 'gz')])
    sample_valid = (np.linalg.norm(acc, axis=1) > .01) & (np.max(np.abs(gyro), axis=1) < 1023.5)
    f = second_features(motion.times_ms, acc, gyro, valid_samples=sample_valid)
    metadata = {k: v for k, v in f.items() if not isinstance(v, np.ndarray)}
    metadata.update(source=str(source), stamp=before, asset_id=record['asset_id'],
        create_time_ms=motion.create_time_ms, update_time_ms=motion.update_time_ms,
        duration_ms=motion.duration_ms, epoch_offset_ms=motion.epoch_at(0),
        warnings=list(motion.warnings), temperature_count=len(motion.temperature),
        sample_count=motion.sample_count, sample_rate_hz=motion.sample_rate_hz,
        gap_count=motion.gap_count, acc_scale=motion.acc_scale)
    arrays = {k: v for k, v in f.items() if isinstance(v, np.ndarray)}
    arrays['temperature_c'] = motion.temperature
    arrays['temperature_ms'] = motion.temperature_times_ms
    temp = array_path.with_suffix('.tmp')
    with temp.open('wb') as stream:
        np.savez_compressed(stream, **arrays)
    os.replace(temp, array_path)
    atomic_json(meta_path, metadata)
    return {**arrays, **metadata, 'cached': False}


def analyze_patterns(index, cache, output, *, progress=lambda *_: None, cancelled=lambda: False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    rows, issues, patterns = [], list(index.get('issues', [])), []
    records = [r for r in index['records'] if r['events']]
    for i, record in enumerate(records):
        if cancelled():
            raise InterruptedError('Pattern analysis cancelled')
        try:
            f = load_features(record, cache)
            name_to_index = {k: j for j, k in enumerate(f['names'])}
            for event in record['events']:
                a, b = event['start_ms'], event.get('end_ms')
                b = a if b is None else b
                mask = (f['seconds']*1000 >= a) & (f['seconds']*1000 <= b) & f['valid_context']
                if not mask.any():
                    near = int(np.clip((a+b)/2000, 0, len(f['X'])-1))
                    mask[near] = bool(f['valid_context'][near])
                row = dict(asset_id=record['asset_id'], cow_id=record['cow_id'], code=event['code'],
                    start_ms=a, end_ms=event.get('end_ms'), duration_s=(b-a)/1000,
                    usable_seconds=int(mask.sum()), source=record['raw'])
                for name in ('orientation_change_deg', 'gyro_burst_logratio', 'gyro_settle_logratio',
                             'core_gyro', 'core_dynamic', 'long_orientation_noise', 'gyro_repeat_2s'):
                    values = f['X'][mask, name_to_index[name]]
                    values = values[np.isfinite(values)]
                    row[name] = float(np.median(values)) if len(values) else None
                rows.append(row)
        except (OSError, ValueError, KeyError) as exc:
            issues.append(dict(path=record['raw'], reason=str(exc)))
        progress(i+1, len(records), '提取事件信号规律')
        if i % 10 == 0:
            write_table(output/'事件特征-实时.csv', rows)
    by_code = defaultdict(list)
    for row in rows:
        by_code[row['code']].append(row)
    for code, values in by_code.items():
        usable = [r for r in values if r['usable_seconds']]
        durations = [r['duration_s'] for r in values]
        def share(key, predicate):
            data = [r[key] for r in usable if r[key] is not None]
            return sum(predicate(v) for v in data)/len(data) if data else None
        patterns.append(dict(code=code, events=len(values), cows=len({r['cow_id'] for r in values}),
            usable_events=len(usable), duration_median_s=float(np.median(durations)),
            duration_p90_s=float(np.quantile(durations, .9)), duration_max_s=max(durations),
            orientation_change_ge10_share=share('orientation_change_deg', lambda v: v >= 10),
            gyro_burst_ge1_5_share=share('gyro_burst_logratio', lambda v: v >= np.log(1.5)),
            settled_to_half_share=share('gyro_settle_logratio', lambda v: v <= np.log(.5)),
            repeated_envelope_share=share('gyro_repeat_2s', lambda v: v >= .3)))
    write_table(output/'事件特征-实时.csv', rows)
    write_table(output/'共同规律.csv', patterns)
    write_table(output/'数据问题.csv', issues, ['path', 'reason'])
    result = dict(schema='event-pattern-report-1', fingerprint=index['fingerprint'], patterns=patterns,
        source_summary=index['summary'], records=len(records), issues=len(issues),
        interpretation='Descriptive shares, not universal laws or predictive accuracy. Unlabeled periods remain unknown.')
    atomic_json(output/'规律报告.json', result)
    return result
