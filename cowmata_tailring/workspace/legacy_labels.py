"""Lossless legacy CSV readers. No inferred calving time or implicit negatives."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import re
from datetime import datetime
from pathlib import Path

from cowmata_tailring.annotation.core import Event, Label

from .clocks import Anchor, ClockMap
from .data_category import category_fields
from .device_identity import parse_device_folder
from .resource_layout import TZ
from .work import SessionWork

LABEL_CODES = {'站立': 'STANDING', '躺卧': 'LYING', '行走': 'WALKING', '采食': 'FEEDING',
               '起立过程': 'STANDING_UP', '起立': 'STANDING_UP', '卧倒过程': 'LYING_DOWN',
               '卧倒': 'LYING_DOWN', '排尿': 'URINATION', '排便': 'DEFECATION',
               '抬尾': 'TAIL_RAISED', '甩尾': 'TAIL_WAGGING', '露蹄': 'FETAL_PART_FIRST_VISIBLE',
               '犊牛完全娩出': 'CALF_FULLY_EXPELLED', '人工开始干预': 'HUMAN_INTERVENTION_START'}


def normalized_cow(value):
    match = re.match(r'^([0-9]{5})(?![0-9])', str(value or '').strip())
    return match.group(1) if match else ''


def local_epoch(value):
    value = str(value).strip().replace('年', '-').replace('月', '-').replace('日', '')
    value = re.sub(r'^(\d{4})-(\d{1,2})-(\d{1,2})(?=\s|$)',
                   lambda m: f'{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}', value)
    value = re.sub(r'(\d{2})_(\d{2})_(\d{2})', r'\1:\2:\3', value)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.timestamp() * 1000


def _finite(value, *, relative=False):
    result = float(value)
    if not math.isfinite(result) or relative and result < 0:
        raise ValueError('时间必须是有效非负数')
    return result


def folder_identity(path):
    for directory in Path(path).parents:
        try:
            found = parse_device_folder(directory.name)
        except ValueError:
            continue
        return {'device_id': found.device_id, 'cow_id': found.cow_id, 'field_mark': found.field_mark,
                'source_folder': directory.name, 'folder_name': found.folder_name}
    return {'device_id': '', 'cow_id': '', 'field_mark': '', 'source_folder': '', 'folder_name': ''}


def read_legacy_table(path, *, identity_policy='review'):
    path = Path(path)
    if identity_policy not in {'review', 'folder'}:
        raise ValueError('Unknown identity policy')
    content = path.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    meta_path = path.with_name(path.name.replace('.events.csv', '.events_meta.json'))
    metadata = json.loads(meta_path.read_text(encoding='utf-8-sig')) if meta_path != path and meta_path.is_file() else {}
    folder = folder_identity(path)
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
    if not reader.fieldnames or not ({'label', 'Behavior'} & set(reader.fieldnames)):
        raise ValueError('不支持的旧标签表头')
    result = {'source': str(path.resolve()), 'sha256': sha, 'metadata': metadata,
              'folder_identity': folder, 'events': [], 'unresolved': [], 'empty_rows': 0}
    for line, row in enumerate(reader, 2):
        if not any(str(v or '').strip() for v in row.values()):
            result['empty_rows'] += 1
            continue
        label = str(row.get('label') or row.get('Behavior') or '').strip()
        code = str(row.get('code') or LABEL_CODES.get(label) or label).strip()
        session = str(row.get('session_id') or row.get('Observation id') or metadata.get('session_id') or '')
        label_cow = normalized_cow(row.get('cow_id') or metadata.get('cow_id'))
        conflict = bool(folder['cow_id'] and label_cow and folder['cow_id'] != label_cow)
        cow = folder['cow_id'] if identity_policy == 'folder' else '' if conflict else label_cow or folder['cow_id']
        item = {'source_file': str(path.resolve()), 'source_file_sha256': sha, 'source_line': line,
                'original_row': dict(row), 'session_id': session, 'label': label, 'code': code,
                'cow_id': cow, 'label_cow_id': label_cow, 'folder_cow_id': folder['cow_id'],
                'device_id': str(metadata.get('device') or folder['device_id']).upper(),
                'field_mark': folder['field_mark'], 'identity_status': 'folder_confirmed' if conflict and identity_policy == 'folder' else 'conflict' if conflict else 'resolved' if cow else 'missing',
                'evidence': row.get('evidence', ''), 'annotator': row.get('annotator', metadata.get('annotator', '')),
                'note': row.get('note', ''), 'type': row.get('type') or ('interval' if 'Behavior' in row else 'point')}
        try:
            if not code or label in {'无', '未知'}:
                raise ValueError('原表未提供有效事件或准确时间')
            if 't_start_rel_ms' in row:
                start = _finite(row['t_start_rel_ms'], relative=True)
                end = _finite(row['t_end_rel_ms'], relative=True) if item['type'] != 'point' else None
                coordinates = 'parent_imu_ms'
            elif 'Start (s)' in row:
                start, end = (_finite(row[k], relative=True)*1000 for k in ('Start (s)', 'Stop (s)'))
                coordinates = 'parent_imu_ms'
            elif 'time' in row:
                date = str(row.get('date') or '').strip()
                if not date:
                    found = re.search(r'\d{4}-\d{2}-\d{2}', session)
                    if not found:
                        raise ValueError('缺少事件日期，不能用目录或其他牛的日期补造')
                    date = found.group()
                clock = str(row['time']).strip()
                if not re.fullmatch(r'\d{1,2}:\d{2}:\d{2}(?:\.\d+)?', clock):
                    raise ValueError('事件时间未知或格式无效')
                hour, remainder = clock.split(':', 1)
                start, end = local_epoch(date + ' ' + hour.zfill(2) + ':' + remainder), None
                coordinates = 'unix_epoch_ms'
            else:
                raise ValueError('旧表未提供可识别的时间字段')
            if end is not None and end <= start:
                raise ValueError('区间结束必须晚于开始，原行保留待核')
            item.update(start_ms=start, end_ms=end, coordinates=coordinates)
            signature = [sha, line, code, start, end]
            item['legacy_event_id'] = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
            result['events'].append(item)
        except (ValueError, TypeError, OverflowError) as exc:
            result['unresolved'].append({**item, 'reason': str(exc)})
    return result


def append_event(work, item, *, start=None, end=None, extras=None):
    code = item['code']
    li = next((i for i, label in enumerate(work.project.labels) if label.code == code), None)
    if li is None:
        li = len(work.project.labels)
        work.project.labels.append(Label.from_dict({'name':item['label'], 'code':code,
            'type':'point' if item['end_ms'] is None else 'interval', 'layer':item['original_row'].get('layer', 'objective_event')}))
    event = Event(len(work.project.events)+1, li, item['start_ms'] if start is None else start,
                  item['end_ms'] if start is None else end, item.get('note', ''),
                  {'两者':'both','仅视频':'video','仅曲线':'curve'}.get(item.get('evidence'), 'video'),
                  extras={'confirmation':'legacy_imported', 'legacy':item, **work.category_fields(), **(extras or {})})
    work.project.events.append(event)
    return event


def build_absolute_annotation(path, *, category, identity_policy='review'):
    table = read_legacy_table(path, identity_policy=identity_policy)
    work = SessionWork(table['sha256'])
    work.set_category(category)
    cows = {e['cow_id'] for e in table['events'] if e['cow_id']}
    work.project.cow_id = next(iter(cows)) if len(cows) == 1 else ''
    unresolved = list(table['unresolved'])
    for item in table['events']:
        if item['coordinates'] != 'unix_epoch_ms':
            unresolved.append({**item, 'reason':'相对时间尚未连接九轴原件'})
        else:
            append_event(work, item)
    times = [e.t0 for e in work.project.events]
    lo, hi = (min(times), max(times)) if times else (0, 0)
    work.clock = ClockMap([Anchor(lo, lo+480*60000, {'source':'legacy_absolute_annotation'})], basis='legacy_estimate') if times else ClockMap()
    work.project.extras['legacy_unresolved'] = unresolved
    content = Path(path).read_bytes()
    return {'format':'cowmata-annotation', 'version':3, 'coordinates':'unix_epoch_ms',
            **category_fields(category), 'work':work.to_dict(), 'view':{'start_ms':lo, 'end_ms':hi},
            'source':{'asset_id':work.asset_id, 'kind':'annotation_only', 'path':'', 'project_root_hint':''},
            'video':{}, 'embedded_imu':None, 'embedded_labels':{'name':Path(path).name,
                'sha256':work.asset_id, 'original_base64':base64.b64encode(content).decode('ascii')},
            'legacy_import':{'unresolved':unresolved, 'source_file':str(Path(path).resolve()),
                             'note':'只有原始人工标签，不包含或虚构九轴；未知时间未生成事件。'}}
