"""Preview and copy legacy labels into editable work and portable mother labels."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path

from cowmata_tailring.annotation.data import load_motion_json, parse_motion_object

from .catalog import Catalog, digest_file
from .clocks import ClockMap
from .data_category import category_fields, update_context
from .device_identity import resolve_device_identity
from .label_file import build_label_file, save_label_file
from .legacy_labels import append_event, build_absolute_annotation, read_legacy_table
from .resource_layout import MODALITIES, covered_days, day_at, stamp_at
from .storage import atomic_json
from .work import SessionWork


def _files(sources, suffix):
    found = set()
    for source in sources:
        path = Path(source).resolve(strict=True)
        found.update(path.rglob('*'+suffix) if path.is_dir() else [path])
    return sorted(p for p in found if p.is_file() and p.suffix.lower() == suffix)


def legacy_identity(path, device):
    identity = resolve_device_identity(path, device)
    if identity['status'] == 'ready':
        return identity
    # Short historical device prefixes can be expanded only from this JSON.
    for directory in Path(path).parents:
        match = re.fullmatch(r'([0-9A-Fa-f]{4})-([0-9]{5})-([A-Za-z0-9]+)', directory.name)
        if match and re.fullmatch(r'[0-9A-Fa-f]{12}', str(device)) and str(device).upper().endswith(match[1].upper()):
            return {**identity, 'status':'ready', 'device_id':str(device).upper(), 'cow_id':match[2],
                    'field_mark':match[3], 'source_folder':directory.name,
                    'folder_name':f'{str(device).upper()}-{match[2]}-{match[3]}',
                    'identity_provenance':'legacy_short_device_suffix_and_json'}
    return identity


def inventory_raw(sources, *, progress=lambda *_: None, cancelled=lambda: False):
    records = []
    files = _files(sources, '.json')
    for index, path in enumerate(files):
        if cancelled():
            raise InterruptedError('旧数据审查已暂停')
        row = {'path':str(path)}
        try:
            content = path.read_bytes()
            obj = json.loads(content.decode('utf-8-sig'))
            if not isinstance(obj, dict) or 'imu' not in obj:
                continue
            row.update(sha256=hashlib.sha256(content).hexdigest(), size=len(content), device=obj.get('device'), create_time=obj.get('create_time'))
            motion = parse_motion_object(obj, source_path=path)
            row.update(status='valid', samples=motion.sample_count, duration_ms=motion.duration_ms,
                       timing=motion.capture_timing(), first_epoch_ms=motion.epoch_at(0),
                       last_epoch_ms=motion.epoch_at(motion.duration_ms), identity=legacy_identity(path, obj.get('device')))
        except (ValueError, OSError, TypeError) as exc:
            row.update(status='invalid', error=str(exc))
        records.append(row)
        progress(index+1, len(files), str(path))
    return records


def plan_migration(label_sources, raw_sources, target, *, category, identity_policy='review',
                   raw_inventory=None, progress=lambda *_: None, cancelled=lambda: False):
    category_fields(category)
    target = Path(target).resolve()
    raw = copy.deepcopy(raw_inventory) if raw_inventory is not None else inventory_raw(raw_sources, progress=progress, cancelled=cancelled)
    for row in raw:
        if target == Path(row['path']) or target in Path(row['path']).parents:
            raise ValueError('迁移来源不能位于目标目录内')
        if row.get('status') == 'valid':
            row['identity'] = legacy_identity(row['path'], row['device'])
    tables = [read_legacy_table(path, identity_policy=identity_policy) for path in _files(label_sources, '.csv')]
    groups = defaultdict(list)
    for row in raw:
        if row.get('sha256'):
            groups[row['sha256']].append(row)
    assets = {}
    for sha, candidates in groups.items():
        ready = [r for r in candidates if r.get('identity', {}).get('status') == 'ready']
        row = copy.deepcopy((ready or candidates)[0])
        identities = {r['identity']['cow_id'] for r in ready}
        row['aliases'] = [r['path'] for r in candidates]
        if len(identities) > 1:
            row['identity']['status'] = 'conflict'
            row['identity']['message'] = '相同九轴内容在多个牛号目录出现，待核'
        ident = row.get('identity', {})
        if row['status'] == 'valid' and ident.get('status') == 'ready':
            lo, hi = row['first_epoch_ms'], row['last_epoch_ms'] + 1
            filename = f'{stamp_at(lo)}__{stamp_at(hi)}__{sha[:12]}.json'
            row['target'] = str(target/'Motion'/day_at(lo)/ident['folder_name']/filename)
            row['covered_dates'] = covered_days(lo, hi)
        else:
            row['target'] = str(target/'标注工程/待核原始'/f'{sha}.json')
            row['covered_dates'] = []
        assets[sha] = row
    events, unresolved = [], []
    for table in tables:
        unresolved.extend(table['unresolved'])
        for original in table['events']:
            item = copy.deepcopy(original)
            if item['coordinates'] == 'parent_imu_ms':
                names = {item['session_id']+'.json', str(table['metadata'].get('source_json_name') or '')}
                matches = [a for a in assets.values() if a.get('status') == 'valid'
                           and (not item['device_id'] or a.get('device') == item['device_id'])
                           and any(Path(alias).name in names for alias in a['aliases'])]
            else:
                matches = [a for a in assets.values() if a.get('status') == 'valid'
                           and a.get('identity', {}).get('cow_id') == item['cow_id'] and item['cow_id']
                           and a['first_epoch_ms'] <= item['start_ms'] <= a['last_epoch_ms']
                           and (not item['device_id'] or a.get('device') == item['device_id'])]
            if len(matches) != 1:
                item['asset_id'] = ''
                item['reason'] = '未找到唯一身份与时间匹配的九轴原件'
                unresolved.append(item)
            else:
                asset = matches[0]
                item['asset_id'] = asset['sha256']
                if item['coordinates'] == 'unix_epoch_ms':
                    item.update(parent_start_ms=item['start_ms']-asset['first_epoch_ms'], parent_end_ms=None)
                else:
                    item.update(parent_start_ms=item['start_ms'], parent_end_ms=item['end_ms'])
                if item['parent_start_ms'] > asset['duration_ms'] or item['parent_end_ms'] is not None and item['parent_end_ms'] > asset['duration_ms']:
                    item['reason'] = '标签超出母九轴范围，保持原值待核'
                    item['asset_id'] = ''
                    unresolved.append(item)
                else:
                    events.append(item)
    signature = {'target':str(target), 'category':category, 'identity_policy':identity_policy,
                 'labels':sorted(t['sha256'] for t in tables), 'raw':sorted(assets)}
    return {'schema':'cowmata-legacy-migration-3.4.2', 'id':hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()[:24],
            **signature, 'assets':list(assets.values()), 'tables':tables, 'events':events, 'unresolved':unresolved,
            'summary':{'raw_files':len(raw),'unique_raw':len(assets),'labels':len(tables),'matched_events':len(events),
                       'unresolved':len(unresolved),'identity_conflicts':sum(e['identity_status']=='conflict' for e in events)}}


def _copy_checked(source, target, sha):
    source, target = Path(source), Path(target)
    if digest_file(source) != sha:
        raise ValueError('来源在预览后发生变化：'+str(source))
    if target.exists():
        if digest_file(target) != sha:
            raise ValueError('目标存在不同内容，停止覆盖：'+str(target))
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name+'.migration.partial')
    shutil.copyfile(source, temporary)
    if digest_file(temporary) != sha:
        raise ValueError('复制后校验失败')
    # Windows rename refuses replacement; POSIX link also refuses an occupied name.
    if os.name == 'nt':
        temporary.rename(target)
    else:
        os.link(temporary, target)
        temporary.unlink()


def execute_migration(plan, *, progress=lambda *_: None, cancelled=lambda: False):
    if plan.get('schema') != 'cowmata-legacy-migration-3.4.2':
        raise ValueError('Unsupported legacy migration plan')
    root = Path(plan['target']).resolve()
    for table in plan['tables']:
        if digest_file(Path(table['source'])) != table['sha256']:
            raise ValueError('标签在预览后发生变化，请重新审查')
    for asset in plan['assets']:
        if not Path(asset['target']).resolve().is_relative_to(root):
            raise ValueError('迁移目标越界')
    completed = root/'标注工程/迁移记录'/plan['id']/'result.json'
    if completed.is_file():
        for asset in plan['assets']:
            if digest_file(Path(asset['target'])) != asset['sha256']:
                raise ValueError('母九轴来源校验失败')
        return json.loads(completed.read_text(encoding='utf-8'))
    root.mkdir(parents=True, exist_ok=True)
    for name in MODALITIES:
        (root/name).mkdir(exist_ok=True)
    catalog = Catalog(root, stability_seconds=0)
    if catalog.readonly:
        catalog.close()
        raise ValueError('请先保存并关闭此目标工程，再执行标签迁移')
    result = {'events':0,'documents':0,'unique_raw':len(plan['assets']),'unresolved':len(plan['unresolved']), 'target':str(root)}
    try:
        for index, asset in enumerate(plan['assets']):
            if cancelled():
                raise InterruptedError('迁移已暂停，原件保留')
            _copy_checked(asset['path'], asset['target'], asset['sha256'])
            progress(index+1, len(plan['assets']), asset['target'])
        for table in plan['tables']:
            _copy_checked(table['source'], root/'标注工程/旧标签原件'/table['sha256']/Path(table['source']).name, table['sha256'])
        grouped = defaultdict(list)
        for event in plan['events']:
            grouped[event['asset_id']].append(event)
        for asset in plan['assets']:
            items = grouped[asset['sha256']]
            if not items:
                continue
            source = Path(asset['target'])
            motion = load_motion_json(source)
            work = SessionWork(asset['sha256'])
            work.set_category(plan['category'])
            work.clock = ClockMap.from_capture(motion)
            work.project.source = {'path':source.relative_to(root).as_posix(), 'asset_id':asset['sha256'], 'durationMs':motion.duration_ms}
            work.project.extras['legacy_migration_id'] = plan['id']
            work.project.extras['device_identity'] = copy.deepcopy(asset.get('identity', {}))
            cows = {i['cow_id'] for i in items if i['cow_id']}
            work.project.cow_id = next(iter(cows)) if len(cows) == 1 else ''
            if any(i['identity_status'] == 'conflict' for i in items):
                hints = {i['label_cow_id'] for i in items if i['label_cow_id']}
                work.project.cow_id = next(iter(hints)) if len(hints) == 1 else ''
                work.project.extras['device_identity'].update(status='conflict',
                    message='旧标签牛号与来源目录冲突；保留原值，训练前须人工核对。')
            seen = {}
            for item in items:
                key = (item['code'], item['parent_start_ms'], item['parent_end_ms'], item['cow_id'], item['identity_status'])
                if key in seen:
                    seen[key].extras['legacy_sources'].append(item)
                    continue
                event = append_event(work, item, start=item['parent_start_ms'], end=item['parent_end_ms'],
                    extras={'legacy_sources':[item], 'time_basis':'parent_imu_ms',
                            'original_wall_time_preserved':True, 'first_frame_offset_ms':motion.first_frame_elapsed_ms})
                seen[key] = event
            path = catalog.work_path(work.asset_id)
            if path.exists():
                prior = json.loads(path.read_text(encoding='utf-8'))
                if prior.get('project', {}).get('legacy_migration_id') != plan['id']:
                    raise ValueError('目标已有人工工作，不能用旧标签覆盖：'+str(path))
            atomic_json(path, work.to_dict())
            doc = build_label_file(work, motion, root, [], {})
            doc['legacy_import'] = {'migration_id':plan['id'], 'original_source_paths':asset['aliases'],
                                    'time_note':'相对时间和源帧保持不变；绝对时间采用新版首帧计时；旧绝对时间随原行保留。'}
            destination = root/'标注工程/导出标注'/f'{work.asset_id}.标注.json'
            save_label_file(destination, doc, protected=[source,path])
            result['events'] += len(work.project.events)
            result['documents'] += 1
        orphan_sources = {e['source_file'] for e in plan['unresolved'] if e.get('coordinates') == 'unix_epoch_ms' or e.get('type') == 'point'}
        for table in plan['tables']:
            if table['source'] not in orphan_sources:
                continue
            doc = build_absolute_annotation(table['source'], category=plan['category'], identity_policy=plan['identity_policy'])
            # Include only source rows not already attached to a parent record.
            linked = {e['legacy_event_id'] for e in plan['events'] if e['source_file'] == table['source']}
            doc['work']['project']['events'] = [e for e in doc['work']['project']['events'] if e.get('legacy',{}).get('legacy_event_id') not in linked]
            destination = root/'标注工程/导出标注'/f'{table["sha256"]}.绝对时间标注.json'
            save_label_file(destination, doc)
            result['documents'] += 1
            result['events'] += len(doc['work']['project']['events'])
        records, context_rows = [], []
        for asset in plan['assets']:
            if not asset['covered_dates']:
                continue
            ident = asset['identity']
            relative = Path(asset['target']).relative_to(root).as_posix()
            lo, hi = asset['first_epoch_ms'], asset['last_epoch_ms']+1
            records.append({'path':relative,'kind':'imu','sha256':asset['sha256'],'size':asset['size'],
                'owner':ident['folder_name'],'record_start_ms':lo,'record_end_ms':hi,'covered_dates':asset['covered_dates'],
                'metadata':{'capture_timing':asset['timing'], 'duration_ms':asset['duration_ms']},
                'source':asset['path'],'device_id':ident['device_id'],'cow_id':ident['cow_id'],'field_mark':ident['field_mark']})
            context_rows.append({'status':'ready','source':asset['path'],'target':asset['target'],**ident,
                                 'record_date':day_at(lo),'record_start_ms':lo})
        index_path = root/'资源索引.json'
        previous = json.loads(index_path.read_text(encoding='utf-8')) if index_path.is_file() else {}
        by_path = {r['path']:r for r in previous.get('records', [])}
        by_path.update({r['path']:r for r in records})
        days = sorted({d for r in by_path.values() for d in r['covered_dates']})
        for day in days:
            for name in MODALITIES:
                (root/name/day).mkdir(parents=True, exist_ok=True)
            for view in range(1,9):
                (root/'Video'/day/f'视角{view:02d}').mkdir(exist_ok=True)
            (root/'PPG'/day/'占位说明.txt').write_text('PPG 原始通道与标注预留；当前没有可解析 PPG 数据。\n', encoding='utf-8')
        atomic_json(index_path, {**previous,'schema':'cowmata-resources-3.4','records':list(by_path.values()),'dates':days,
                                **category_fields(plan['category'])})
        update_context(root, {'mode':'import','category':plan['category'],'start':days[0] if days else '',
                       'end':days[-1] if days else '', 'id':plan['id'],'created_at':'legacy_migration','rows':context_rows})
        atomic_json(completed.parent/'review.json', {'summary':plan['summary'],'unresolved':plan['unresolved'],
                    'raw_review':[a for a in plan['assets'] if not a['covered_dates']]})
        atomic_json(completed.parent/'plan.json', plan)
        catalog.scan(fast=True)
        atomic_json(completed, result)
        return result
    finally:
        catalog.close()
