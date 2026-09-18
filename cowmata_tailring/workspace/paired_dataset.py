"""Versioned original JSON / annotation pairs in the operator's dataset layout."""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cowmata_tailring.annotation.defaults import DEFAULT_LABELS
from cowmata_tailring.annotation.taxonomy import REMOVED, SCHEMA, upgrade_document

from .catalog import digest_file, file_stamp
from .classification_report import atomic_bytes
from .fast_transfer import copy_verified
from .storage import ProjectLock, atomic_json

TZ = timezone(timedelta(hours=8))
FOLDERS = dict(zip([r['code'] for r in DEFAULT_LABELS], [
    'Standup', 'Liedown', 'StandingTailRaised', 'StandingTailWagging', 'LyingTailRaised',
    'LyingTailWagging', 'Straining', 'AmnioticSacFirstVisible', 'FetalPartFirstVisible',
    'CalfFullyExpelled', 'FetalMembranesFullyExpelled', 'Urination', 'Defecation', 'Mounting', 'Assistance']))
TASKS = {
    'behavior': ('行为识别数据集', 'COWMATA_Behavior_Dataset', None, tuple(FOLDERS)),
    'calving': ('产犊预测数据集', 'COWMATA_CalvingPred_Dataset', ('calving',),
                ('STANDING_UP', 'LYING_DOWN', 'STRAINING_BOUT', 'FETAL_PART_FIRST_VISIBLE', 'CALF_FULLY_EXPELLED')),
    'estrus': ('发情预测数据集', 'COWMATA_EstrusPred_Dataset', ('estrus',), tuple(FOLDERS)),
    'pregnancy': ('怀孕监测数据集', 'COWMATA_PregnancyMonitor_Dataset',
                  ('pregnancy', 'pregnancy_early', 'pregnancy_mid', 'pregnancy_late'), tuple(FOLDERS)),
    'disease': ('疫病监测数据集', 'COWMATA_DiseaseMonitor_Dataset', ('disease', 'illness'), tuple(FOLDERS)),
}
SKIP_DIRS = {'.edge-download', '录像', '科牧特_协作标注', 'Video', '.git', '.归类缓存', '归类附属文件', '.label-history', '.dataset-history', '.build', '__pycache__'}


def category_for(path, document=None):
    parts = Path(path).parts
    for name, code in [('产犊', 'calving'), ('发情', 'estrus'), ('孕早期', 'pregnancy_early'),
                       ('孕中期', 'pregnancy_mid'), ('孕晚期', 'pregnancy_late'), ('怀孕', 'pregnancy'),
                       ('疫病', 'disease'), ('正常', 'healthy')]:
        if name in parts:
            return code
    document = document or {}
    return document.get('dataset_category') or document.get('work', {}).get('project', {}).get('dataset_category', '')


def paired_label(raw):
    raw = Path(raw)
    if raw.parent.name == 'Raw' and raw.stem.endswith('_raw'):
        return raw.parent.parent/'Label'/(raw.stem[:-4]+'_label.json')
    for parent in raw.parents:
        if parent.name in {'Motion', 'PPG'}:
            root = parent.parent
            relative = raw.relative_to(root)
            return (root/'标注工程'/relative).with_name(raw.stem+'.标注.json')
    return raw.with_name(raw.stem+'.标注.json')


def local_source_root(label_path, document):
    label_path=Path(label_path).resolve()
    relative=document.get('source',{}).get('path','')
    if relative and not Path(relative).is_absolute():
        for root in label_path.parents:
            candidate=(root/relative).resolve()
            if candidate.is_relative_to(root) and candidate.is_file() and candidate!=label_path:
                return root
    for p in label_path.parents:
        if p.name=='标注工程':
            return p.parent
    return None


def _check(cancelled):
    if cancelled():
        raise InterruptedError('数据集构建已暂停，继续时复用已完成配对')


def discover(sources, task, cancelled=lambda: False):
    allowed = TASKS[task][2]
    result = []
    seen = set()
    for source in sources:
        source = Path(source).resolve()
        if not source.is_dir():
            if not source.is_file():
                raise ValueError('来源目录不存在：'+str(source))
            if source.name.endswith(('_label.json', '.标注.json')):
                doc = json.loads(source.read_text(encoding='utf-8-sig'))
                base = local_source_root(source, doc)
                if base is None:
                    raise ValueError('未找到标签对应的原始数据')
                source = base / doc['source']['path']
        entries = os.walk(source) if source.is_dir() else [(str(source.parent), [], [source.name])]
        for directory, dirs, names in entries:
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and d != '标注工程'
                       and not (Path(directory)/d).is_symlink() and not getattr(Path(directory)/d, 'is_junction', lambda: False)())
            for name in sorted(names):
                _check(cancelled)
                raw = Path(directory)/name
                if raw.suffix.lower() != '.json' or raw.name.endswith(('_label.json', '.标注.json')):
                    continue
                kind = next((k for k in ('Motion', 'PPG') if k in raw.parts), None)
                if kind is None or raw.stat().st_size == 0:
                    continue
                key = os.path.normcase(str(raw))
                if key in seen:
                    continue
                seen.add(key)
                label = paired_label(raw)
                document = json.loads(label.read_text(encoding='utf-8-sig')) if label.is_file() and label.stat().st_size else None
                category = category_for(raw, document)
                if allowed is not None and category not in allowed:
                    continue
                if document:
                    document = upgrade_document(document, category=category)
                result.append(dict(source=str(raw), kind=kind, category=category,
                                   label_source=str(label) if document else '', label_source_sha256=digest_file(label) if document else '', document=document))
    # Rebuilding a reviewed version can encounter the same raw record in
    # several behavior folders. Merge its existing event IDs before exporting.
    grouped = {}
    for item in result:
        raw = Path(item['source'])
        doc = item['document']
        if raw.parent.name != 'Raw' or not raw.stem.endswith('_raw') or not doc:
            grouped[('file', item['source'])] = item
            continue
        key = ('dataset', item['kind'], raw.stem, doc['work']['asset_id'])
        if key not in grouped:
            grouped[key] = item
            continue
        first = grouped[key]
        if not raw.samefile(first['source']) and digest_file(raw) != doc['work']['asset_id']:
            raise ValueError('数据集原始文件内容变化，不能合并标签')
        dest = first['document']['work']['project']
        by_code = {r['code']: i for i, r in enumerate(dest['labels'])}
        known = {e['id']: e for e in dest['events']}
        for event in doc['work']['project'].get('events', []):
            event = copy.deepcopy(event)
            code = event['label_code']
            if code not in by_code:
                by_code[code] = len(dest['labels'])
                dest['labels'].append(copy.deepcopy(doc['work']['project']['labels'][event['li']]))
            event['li'] = by_code[code]
            if event['id'] in known:
                old = known[event['id']]
                if (old['label_code'],old['t0'],old.get('t1')) != (code,event['t0'],event.get('t1')):
                    raise ValueError('同一记录存在冲突修订，请选择要使用的数据集版本')
            else:
                dest['events'].append(event)
                known[event['id']] = event
        first.setdefault('additional_labels', []).append(item['label_source'])
    return list(grouped.values())


def _counts(rows):
    values = Counter(r['status'] for r in rows)
    return dict(total=len(rows), done=values['done'], reused=values['reused'], errors=values['error'],
                pending=values['pending'], processing=values['processing'])


class BuildReport:
    def __init__(self, root, rows, callback):
        self.root, self.rows, self.callback = root, rows, callback
        self.started = time.monotonic()
        self.last = 0.0
        self.publish(force=True)

    def publish(self, *, force=False, phase='running'):
        if not force and time.monotonic()-self.last < 1:
            return
        text = io.StringIO(newline='')
        writer = csv.writer(text)
        writer.writerow(['序号', '状态', '类型', '行为', '来源文件', '原始数据', '标签数据', '耗时(秒)', '说明'])
        names = {'pending': '待处理', 'processing': '处理中', 'done': '已完成', 'reused': '已复用', 'error': '异常'}
        public = [{k: v for k, v in row.items() if not k.startswith('_')} for row in self.rows]
        for i, row in enumerate(public, 1):
            writer.writerow([i, names[row['status']], row['kind'], row['behavior'], row['source'],
                             row.get('raw_target', ''), row.get('label_target', ''), row.get('seconds', ''), row.get('message', '')])
        payload = ('\ufeff'+text.getvalue()).encode('utf-8')
        for i in range(100):
            csv_path = self.root/('构建记录.csv' if not i else f'构建记录-实时-{i:03d}.csv')
            try:
                atomic_bytes(csv_path, payload)
                break
            except PermissionError:
                continue
        else:
            raise OSError('构建记录 CSV 均被占用')
        snapshot = dict(schema='dataset-build-370', phase=phase, updated_at=datetime.now(TZ).isoformat(),
                        seconds=time.monotonic()-self.started, counts=_counts(public), rows=public, csv_path=str(csv_path))
        atomic_json(self.root/'构建状态.json', snapshot, backup=False)
        self.last = time.monotonic()
        self.callback(snapshot)


def _file_prefix(raw, metadata, document):
    if raw.stem.endswith('_raw') and raw.parent.name == 'Raw':
        return raw.stem[:-4]
    folder = raw.parent.name
    from .device_identity import parse_device_folder
    try:
        canonical = parse_device_folder(folder)
    except ValueError:
        canonical = None
    if canonical is not None and metadata.get('device') and canonical.device_id != str(metadata['device']).upper():
        raise ValueError('来源目录设备与原始数据不一致')
    identity = (document or {}).get('device_identity', {})
    parts = folder.split('-', 2)
    if len(parts) != 3:
        parts = [str(metadata.get('device') or identity.get('device_id') or 'NA'),
                 str(identity.get('cow_id') or metadata.get('cow_id') or 'NA'), str(identity.get('field_mark') or 'NA')]
    if parts[0].upper() == 'NA' and metadata.get('device'):
        parts[0] = str(metadata['device'])
    folder = canonical.folder_name if canonical is not None else '-'.join(parts)
    if re.search(r'[<>:"/\\|?*\x00-\x1f_]', folder):
        raise ValueError('设备、耳标或现场标记含文件名保留字符：'+folder)
    stamp = raw.stem.replace(' ', '_').replace(':', '-')
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})_(\d{2})[-_](\d{2})[-_](\d{2})', stamp)
    if match:
        stamp = f'{match[1]}_{match[2]}-{match[3]}-{match[4]}'
    else:
        value = int(metadata['create_time'])
        stamp = datetime.fromtimestamp(value/1000, TZ).strftime('%Y-%m-%d_%H-%M-%S-%f')[:-3]
    return folder+'_'+stamp


def _label_document(item, focus, raw_sha, raw_target, root, metadata):
    document = item.get('document')
    if document:
        result = copy.deepcopy(document)
        project = result['work']['project']
        project['events'] = [e for e in project.get('events', []) if e.get('label_code') == focus]
        result['work']['drafts'] = [d for d in result['work'].get('drafts', []) if d.get('label_code') == focus]
    else:
        result = dict(format='cowmata-annotation', version=1, coordinates='parent_imu_ms',
            work=dict(schema=1, asset_id=raw_sha, project=dict(_type='bovine-annotation-project', version=4,
                protocol='v5', label_schema=SCHEMA, labels=copy.deepcopy(DEFAULT_LABELS), events=[], source={}),
                clock={}, drafts=[], progress={}),
            view=dict(start_ms=0, end_ms=0, auto_full_record=True), source={}, video={}, embedded_imu=None)
    from .device_identity import parse_device_folder
    try:
        identity = parse_device_folder(raw_target.name.split('_',1)[0])
    except ValueError:
        identity = None
    if identity is not None:
        project = result['work']['project']
        if project.get('cow_id') and str(project['cow_id']) != identity.cow_id:
            raise ValueError('标签牛耳标与原始目录身份不一致，请先复核')
        project.setdefault('cow_id',identity.cow_id)
        project.setdefault('device_identity',dict(device_id=identity.device_id,cow_id=identity.cow_id,
                                                 field_mark=identity.field_mark,status='ready'))
    expected = result.get('source', {}).get('asset_id') or result['work'].get('asset_id')
    if expected and expected != raw_sha:
        raise ValueError('标签与原始数据的内容身份不一致，原文件保留')
    source = result.setdefault('source', {})
    source.update(asset_id=raw_sha, path=raw_target.relative_to(root).as_posix(), project_root_hint=str(root),
                  kind='ppg' if item['kind'] == 'PPG' else 'imu')
    result['work']['asset_id'] = raw_sha
    result['work']['project'].setdefault('source', {}).update(asset_id=raw_sha, name=raw_target.name,
        path=source['path'], project_root_hint=str(root), device=metadata.get('device', ''))
    from .shared_labels import CONTRACT
    result['work']['project']['shared_label_contract'] = copy.deepcopy(CONTRACT)
    result.update(dataset_category=item['category'], dataset=dict(schema='paired-dataset-370', focus_code=focus,
                  original_source=item['source'], original_label=item['label_source'], original_label_sha256=item.get('label_source_sha256',''), additional_labels=item.get('additional_labels', []), raw_sha256=raw_sha,
                  pending_annotation=focus in {'Unlabeled', 'Context'}, historical_only=focus=='HistoricalLabels'))
    if document and document.get('dataset', {}).get('review_revision'):
        result['dataset']['review_revision'] = document['dataset']['review_revision']
    if focus == 'HistoricalLabels' and document:
        result['work']['project']['events'] = [e for e in document['work']['project'].get('events', []) if e.get('label_code') not in FOLDERS and e.get('label_code') not in REMOVED]
    return result


def build_dataset(sources, target, task='behavior', *, job, layout='current', cancelled=lambda: False, on_report=lambda *_: None):
    from cowmata_security.client import require
    require('dataset')
    job = Path(job).resolve()
    job.mkdir(parents=True, exist_ok=True)
    lock = ProjectLock(job/'build.lock')
    target_lock = None
    try:
        if not lock.acquired:
            raise ValueError('Dataset job is already running in another window')
        if layout == 'current':
            from .dataset_incremental import dataset_root
            root = dataset_root(target, TASKS[task][1])
            root.mkdir(parents=True, exist_ok=True)
            target_lock = ProjectLock(root/'.dataset-update.lock')
            if not target_lock.acquired:
                raise ValueError('Another task is updating this dataset')
        return _build_dataset(sources, target, task, job=job, layout=layout, cancelled=cancelled, on_report=on_report)
    finally:
        lock.close()
        if target_lock:
            target_lock.close()


def _build_dataset(sources, target, task='behavior', *, job, layout='versioned', cancelled=lambda: False, on_report=lambda *_: None):
    from .dataset_incremental import (
        backup_label,
        dataset_root,
        existing_pairs,
        inherit_reviews,
        pair_index,
        retire_stale_pairs,
    )
    if layout not in {'current', 'versioned'}:
        raise ValueError('Unknown dataset layout')
    if task not in TASKS:
        raise ValueError('未知数据集任务')
    target, job = Path(target).resolve(), Path(job).resolve()
    sources = [Path(p).resolve() for p in sources]
    job.mkdir(parents=True, exist_ok=True)
    plan_file = job/'paired-plan.json'
    saved = json.loads(plan_file.read_text(encoding='utf-8')) if plan_file.is_file() else None
    items = discover(sources, task, cancelled)
    if not items:
        raise ValueError('所选来源没有可导出的 Motion 或 PPG 原始数据')
    identities = {item['source']: dict(raw=file_stamp(Path(item['source'])), label=item.get('label_source_sha256', ''), additional={p:digest_file(Path(p)) for p in item.get('additional_labels', [])}) for item in items}
    if saved and saved.get('input_identities') != identities:
        raise ValueError('来源数据或标签已变化，请开始新的版本；原版本保留')
    if saved:
        if saved['task'] != task or saved['sources'] != [str(p) for p in sources] or saved['target'] != str(target):
            raise ValueError('继续任务的来源或目标发生变化，请开始新的版本')
        root = Path(saved['root'])
    elif layout == 'current':
        root = dataset_root(target, TASKS[task][1])
    else:
        base = target/TASKS[task][1]
        base.mkdir(parents=True, exist_ok=True)
        while True:
            root = base/datetime.now(TZ).strftime('%Y-%m-%d_%H-%M-%S-%f')
            try:
                root.mkdir()
                break
            except FileExistsError:
                continue
    root.mkdir(parents=True, exist_ok=True)
    existing_manifest = {}
    history = root/'.dataset-history'/job.name
    if layout == 'current' and (root/'dataset-manifest.json').is_file():
        existing_manifest = json.loads((root/'dataset-manifest.json').read_text(encoding='utf-8'))
        if existing_manifest.get('task') != task:
            raise ValueError('Existing dataset task differs from requested task')
        if not saved:
            backup_label(root/'dataset-manifest.json', history)
    for code in TASKS[task][3]:
        for kind in ('Motion', 'PPG'):
            for part in ('Raw', 'Label'):
                (root/FOLDERS[code]/kind/part).mkdir(parents=True, exist_ok=True)
    rows = []
    prior_pairs = {}
    indexed_pairs = pair_index(root) if layout == 'current' else {}
    for item in items:
        _check(cancelled)
        if layout == 'current':
            raw = Path(item['source'])
            try:
                prefix = _file_prefix(raw, {}, item['document'])
            except KeyError:
                metadata = json.loads(raw.read_text(encoding='utf-8-sig'))
                prefix = _file_prefix(raw, metadata, item['document'])
            pairs = existing_pairs(root, prefix, item['kind'], indexed_pairs)
            prior_pairs[item['source']] = pairs
            inherit_reviews(item, pairs, root)
        events = (item['document'] or {}).get('work', {}).get('project', {}).get('events', [])
        found = {e.get('label_code') for e in events}
        focuses = [c for c in TASKS[task][3] if c in found]
        if any(c not in FOLDERS and c not in REMOVED for c in found):
            focuses.append('HistoricalLabels')
        if not focuses:
            focuses = ['Unlabeled' if task == 'behavior' else 'Context']
        for focus in focuses:
            key = hashlib.sha256((item['source']+'|'+focus).encode()).hexdigest()
            rows.append(dict(id=key, source=item['source'], kind=item['kind'], focus=focus,
                             behavior=FOLDERS.get(focus, focus), status='pending', _item=item))
    previous = {r['id']: dict(r) for r in existing_manifest.get('rows', [])}
    # Old manifests can retain absolute paths from a relocated timestamp folder.
    for old in previous.values():
        for part in ('raw', 'label'):
            relative = old.get(part+'_relative')
            if relative:
                value = (root/relative).resolve()
                if not value.is_relative_to(root):
                    raise ValueError('Dataset manifest member escapes its root')
                old[part+'_target'] = str(value)
    previous.update({r['id']:r for r in (saved or {}).get('rows', [])})
    journal = job/'paired-journal.jsonl'
    if journal.is_file():
        for line in journal.read_text(encoding='utf-8').splitlines():
            try:
                value = json.loads(line)
                previous[value['id']] = value
            except (ValueError, KeyError):
                continue
    manifest = dict(schema='paired-dataset-370', task=task, layout=layout, root=str(root), target=str(target),
                    sources=[str(p) for p in sources], source_count=len(items), input_identities=identities, status='building', rows=[])
    report = BuildReport(root, rows, on_report)
    donors = {}
    metadata_cache = {}
    checkpoint_at = 0.0
    retired = set()
    def checkpoint(status, force=False):
        nonlocal checkpoint_at
        if status == 'building' and not force and time.monotonic()-checkpoint_at < 15:
            return
        checkpoint_at = time.monotonic()
        public = [{k:v for k,v in r.items() if not k.startswith('_')} for r in rows]
        current_keys = {r.get('label_relative') for r in public if r.get('label_relative')}
        retained = [r for r in previous.values() if r['id'] not in {x['id'] for x in rows}
                    and r.get('label_relative') not in current_keys | retired]
        manifest.update(status=status, counts=_counts(rows), dataset_pairs=len(retained)+len(public), rows=retained+public)
        atomic_json(plan_file, manifest, backup=False)
        atomic_json(root/'dataset-manifest.json', manifest, backup=False)
    checkpoint('building', force=True)
    def process_row(row):
        _check(cancelled)
        started = time.monotonic()
        item = row['_item']
        raw = Path(item['source'])
        before = file_stamp(raw)
        old = previous.get(row['id'])
        signature = hashlib.sha256(json.dumps([identities[item['source']], item['document']], sort_keys=True).encode()).hexdigest()
        if old and old.get('source_stamp') == before and (layout != 'current' or old.get('input_signature') == signature) and all(Path(old.get(k, '')).is_file() for k in ('raw_target', 'label_target')):
            if ((old.get('raw_stamp') == file_stamp(Path(old['raw_target'])) and old.get('label_stamp') == file_stamp(Path(old['label_target'])))
                    or digest_file(Path(old['raw_target'])) == old.get('raw_sha256') and digest_file(Path(old['label_target'])) == old.get('label_sha256')):
                row.update({**old, 'status':'reused', 'seconds':0, 'message':'已完成配对，直接复用'})
                return
        row.update(status='processing', message='正在写入原始数据与对应标签')
        try:
            if str(raw) not in metadata_cache:
                metadata = json.loads(raw.read_text(encoding='utf-8-sig'))
                if not isinstance(metadata, dict):
                    raise ValueError('原始 JSON 顶层不是对象')
                metadata_cache[str(raw)] = {k:v for k,v in metadata.items() if k in {'device','cow_id','create_time'}}
            metadata = metadata_cache[str(raw)]
            if item.get('label_source') and digest_file(Path(item['label_source'])) != item['label_source_sha256']:
                raise ValueError('构建期间来源标签变化，请开始新的版本')
            prefix = _file_prefix(raw, metadata, item['document'])
            directory = root/row['behavior']/row['kind']
            raw_target, label_target = directory/'Raw'/(prefix+'_raw.json'), directory/'Label'/(prefix+'_label.json')
            for p in (raw_target, label_target):
                if not p.resolve().is_relative_to(root):
                    raise ValueError('数据集目标路径越界')
                p.parent.mkdir(parents=True, exist_ok=True)
            row.update(raw_target=str(raw_target), label_target=str(label_target), source_stamp=before,
                       input_signature=signature,
                       raw_relative=raw_target.relative_to(root).as_posix(), label_relative=label_target.relative_to(root).as_posix(),
                       label_source_sha256=item.get('label_source_sha256',''))
            donor = donors.get(str(raw))
            if raw_target.exists():
                sha = digest_file(raw)
                if digest_file(raw_target) != sha:
                    raise ValueError('同名原始数据内容不同，已有文件保留')
            elif donor:
                sha = donor[1]
                try:
                    os.link(donor[0], raw_target)
                except OSError:
                    shutil_source = Path(donor[0])
                    stats = copy_verified(shutil_source, raw_target.with_suffix('.partial'), sha, cancelled=cancelled)
                    os.replace(raw_target.with_suffix('.partial'), raw_target)
                    sha = stats['sha256']
            else:
                temporary = root/'.build'/(row['id']+'.partial')
                temporary.parent.mkdir(exist_ok=True)
                stats = copy_verified(raw, temporary, None, cancelled=cancelled)
                if file_stamp(raw) != before:
                    raise ValueError('构建期间来源变化，请重新导出')
                temporary.rename(raw_target)
                sha = stats['sha256']
                donors[str(raw)] = (raw_target, sha)
            doc = _label_document(item, row['focus'], sha, raw_target, root, metadata)
            if label_target.exists():
                prior = json.loads(label_target.read_text(encoding='utf-8'))
                if prior.get('dataset', {}).get('review_revision') and layout != 'current':
                    raise ValueError('该标签已有人工修订，保留原文件；请导出新的版本')
                if prior == doc:
                    row.update(status='reused', raw_sha256=sha, label_sha256=digest_file(label_target),
                        seconds=round(time.monotonic()-started, 3), raw_stamp=file_stamp(raw_target), label_stamp=file_stamp(label_target), message='已完成配对，直接复用')
                    return
                if layout == 'current':
                    backup_label(label_target, history)
            atomic_json(label_target, doc, backup=False)
            row.update(status='done', raw_sha256=sha, label_sha256=digest_file(label_target),
                       seconds=round(time.monotonic()-started, 3), raw_stamp=file_stamp(raw_target), label_stamp=file_stamp(label_target), message='原始数据与标签已配对')
        except InterruptedError:
            row['status'] = 'pending'
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            row.update(status='error', seconds=round(time.monotonic()-started,3), message=str(exc))

    groups = defaultdict(list)
    for row in rows:
        groups[row['source']].append(row)
    def process_group(group):
        for row in group:
            process_row(row)
        return group
    from .classification_resources import resource_budget
    slots = resource_budget().copy_workers
    iterator = iter(groups.values())
    pending = set()
    try:
        with ThreadPoolExecutor(max_workers=slots, thread_name_prefix='dataset-pair') as pool:
            while True:
                _check(cancelled)
                while len(pending) < slots:
                    group = next(iterator, None)
                    if group is None:
                        break
                    pending.add(pool.submit(process_group, group))
                if not pending:
                    break
                done, pending = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                for future in done:
                    completed = future.result()
                    with journal.open('a', encoding='utf-8') as stream:
                        for row in completed:
                            stream.write(json.dumps({k:v for k,v in row.items() if not k.startswith('_')}, ensure_ascii=False)+'\n')
                        stream.flush()
                        os.fsync(stream.fileno())
                checkpoint('building')
                report.publish()
        if layout == 'current':
            for source, group in groups.items():
                retired.update(retire_stale_pairs(prior_pairs.get(source, []), group, root, history))
        from cowmata_tailring.temperature import (
            export_temperature_sources,
            find_temperature_sources,
        )
        temperature_sources = [item['source'] for item in items if item['kind'] == 'Motion']
        allowed = TASKS[task][2]
        temperature_sources.extend(p for p in find_temperature_sources(sources)
                                   if allowed is None or category_for(p) in allowed)
        manifest['temperature'] = export_temperature_sources(temperature_sources, root, cancelled=cancelled)
        from cowmata_tailring.algorithms.dataset import scan_dataset
        shared = scan_dataset(root, cancelled=cancelled, collect_shared=True)
        atomic_json(root/'共享行为标签.json', dict(contract=shared['shared_label_contract'],
            events=shared['shared_labels'], issues=shared['issues'],
            dataset_fingerprint=shared['fingerprint'],
            note='派生快照；修改来源标签后由训练读取重新计算，温度按牛号与采集时刻引用，不修改原始 JSON'), backup=False)
        manifest['shared_labels'] = dict(contract=shared['shared_label_contract'],
            path='共享行为标签.json', events=len(shared['shared_labels']), issues=shared['issues'])
        checkpoint('completed' if not _counts(rows)['errors'] and not manifest['temperature']['issues'] and not shared['issues'] else 'needs_attention')
        report.publish(force=True, phase=manifest['status'])
        return manifest
    except InterruptedError:
        checkpoint('paused')
        report.publish(force=True, phase='paused')
        raise
    except Exception:
        checkpoint('needs_attention')
        report.publish(force=True, phase='needs_attention')
        raise
