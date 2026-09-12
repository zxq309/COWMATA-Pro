"""Algorithm-neutral mother labels with per-cow splits and lossless raw sources."""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

from .catalog import digest_file
from .label_file import build_label_file, read_index, read_label_file
from .legacy_labels import normalized_cow
from .resource_layout import dataset_filename
from .storage import atomic_json
from .work import SessionWork

SCHEMA = 'cowmata-mother-dataset-1'
EVENT_HEADS = {'STANDING_UP':'起立', 'LYING_DOWN':'卧倒', 'URINATION':'排尿',
               'TAIL_RAISED':'抬尾', 'TAIL_WAGGING':'甩尾'}


def cow_splits(cows):
    cows = sorted(set(cows), key=lambda cow: hashlib.sha256(('cowmata-split-1:'+cow).encode()).hexdigest())
    if len(cows) < 3:
        return dict.fromkeys(cows, 'unassigned')
    n = max(1, round(len(cows)*.1))
    return {cow: 'test' if i<n else 'validation' if i<2*n else 'train' for i,cow in enumerate(cows)}


def _write_table(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def _documents(source):
    source = Path(source).resolve(strict=True)
    current_assets = set()
    from .annotation_store import dated_documents
    for path in dated_documents(source):
        document=read_label_file(path)
        current_assets.add(document['source']['asset_id'])
        yield path,document
    work_directory = source/'标注工程/annotations'
    if source.is_dir() and work_directory.is_dir():
        from cowmata_tailring.annotation.data import load_motion_json
        rows, settings = read_index(source)
        locations = {r['asset_id']:r['path'] for r in rows if r.get('asset_id') and r['kind']=='imu'}
        registry = source/'资源索引.json'
        if registry.is_file():
            locations.update({r['sha256']:r['path'] for r in json.loads(registry.read_text(encoding='utf-8')).get('records',[]) if r['kind']=='imu'})
        for saved in sorted(work_directory.glob('*.json')):
            work = SessionWork.from_dict(json.loads(saved.read_text(encoding='utf-8')))
            if work.asset_id in current_assets:
                continue
            current_assets.add(work.asset_id)
            if not work.project.events:
                continue
            relative = locations.get(work.asset_id) or work.project.source.get('path','')
            raw = (source/relative).resolve()
            if not relative or not raw.is_relative_to(source) or not raw.is_file():
                raise ValueError('当前标注的母九轴缺失，不能回退到过期导出：'+str(saved))
            doc = build_label_file(work,load_motion_json(raw),source,rows,settings)
            current_assets.add(work.asset_id)
            yield saved, doc
    paths = sorted(source.rglob('*.json')) if source.is_dir() else [source]
    for path in paths:
        if any(p in {'旧标签原件','待核原始','迁移记录','datasets','runtime','assets'} for p in path.relative_to(source if source.is_dir() else source.parent).parts):
            continue
        # Never mistake sensor JSON or metadata for a mother label.
        with path.open('rb') as stream:
            header = stream.read(2048)
        if b'cowmata-annotation' not in header:
            continue
        document = read_label_file(path)
        if document['source']['asset_id'] not in current_assets:
            yield path, document


def _source_scopes(sources):
    """A farm selection uses each category's current saved work and raw registry."""
    result = []
    for source in sources:
        source = Path(source).resolve(strict=True)
        nested = sorted({p.parent for p in source.rglob('标注工程') if p.is_dir()}) if source.is_dir() and not (source/'标注工程').is_dir() else []
        result.extend(nested or [source])
    return list(dict.fromkeys(result))


def _all_documents(sources):
    seen = set()
    for source in _source_scopes(sources):
        for path, document in _documents(source):
            if path not in seen:
                seen.add(path)
                yield path, document


def export_dataset(source, target, *, split_map=None, views=True, progress=lambda *_:None, cancelled=lambda:False):
    target = Path(target).resolve()
    if target.exists():
        raise ValueError('数据集目标已存在；请使用新批次目录，避免覆盖旧训练集')
    source_roots = _source_scopes(source if isinstance(source, (list,tuple)) else [source])
    if any(p == target or p in target.parents for p in source_roots):
        raise ValueError('数据集不能导出到标注来源内部')
    target.mkdir(parents=True)
    sources, events, inputs = {}, [], []
    manual_bindings = {}
    manual_conflicts = set()
    for index, (path, doc) in enumerate(_all_documents(source_roots)):
        if cancelled():
            raise InterruptedError('数据集导出已暂停；未完成目录不可作为训练集')
        work = SessionWork.from_dict(doc['work'])
        asset = work.asset_id
        content = None
        embedded = doc.get('embedded_imu')
        if embedded and embedded.get('kind') == 'original_json':
            content = base64.b64decode(embedded['original_json_base64'], validate=True)
        if content is None and doc['coordinates'] == 'parent_imu_ms':
            root = doc.get('source', {}).get('project_root_hint')
            if root:
                candidate = (Path(root)/doc['source']['path']).resolve()
                if candidate.is_relative_to(Path(root).resolve()) and candidate.is_file():
                    content = candidate.read_bytes()
        if content is not None:
            if hashlib.sha256(content).hexdigest() != asset:
                raise ValueError('母九轴来源校验失败')
            if asset not in sources:
                destination = target/'原始JSON'/f'{asset}.json'
                destination.parent.mkdir(exist_ok=True)
                destination.write_bytes(content)
                sources[asset] = {'asset_id':asset, 'path':destination.relative_to(target).as_posix(),
                                  'sha256':asset, 'capture_timing':doc['source'].get('capture_timing', {})}
        inputs.append({'path':str(path), 'sha256':digest_file(path)})
        identity = work.project.extras.get('device_identity', {})
        if asset in sources:
            cow = normalized_cow(work.project.cow_id)
            prior = sources[asset]
            same_cow = not prior.get('cow_id') or prior['cow_id'] == cow
            timing = doc['source'].get('capture_timing', {})
            prior.update(cow_id=cow, device_id=identity.get('device_id') or doc['source'].get('device',''),
                field_mark=identity.get('field_mark',''),
                record_start_ms=timing.get('sample_start_epoch_ms'),
                record_end_ms=timing.get('sample_end_epoch_ms'),
                identity_eligible=prior.get('identity_eligible',True) and same_cow and bool(cow)
                    and identity.get('status') not in {'conflict','blocked'})
        if identity.get('status')=='manual_override' and identity.get('manual_cow_id')==normalized_cow(work.project.cow_id):
            if asset in manual_bindings and manual_bindings[asset]['cow_id']!=normalized_cow(work.project.cow_id):
                manual_conflicts.add(asset)
            manual_bindings[asset]={**identity,'cow_id':normalized_cow(work.project.cow_id)}
        for event in work.project.events:
            label = work.project.labels[event.li]
            legacy = event.extras.get('legacy', {})
            cow = normalized_cow(work.project.cow_id)
            identity_ok = bool(cow) and identity.get('status') not in {'conflict','blocked'} and legacy.get('identity_status') not in {'conflict','missing'}
            # A user's explicit later identity resolution supersedes old conflict status.
            if identity.get('status') == 'manual_override' and identity.get('manual_cow_id') == cow:
                identity_ok = True
            confirmation = event.extras.get('confirmation', 'unreviewed')
            reviewed = confirmation in {'confirmed','legacy_imported'} and event.ev != 'curve'
            valid_time = event.t1 is None or event.t1 > event.t0
            role = 'negative' if event.extras.get('dataset_role') == 'verified_negative' else 'positive'
            eligible = identity_ok and reviewed and valid_time and (asset in sources or doc['coordinates'] == 'unix_epoch_ms')
            key = [asset, cow, label.code, event.t0, event.t1, role]
            event_id = hashlib.sha256(json.dumps(key,ensure_ascii=False).encode()).hexdigest()
            reference_start = work.clock.map(event.t0)-480*60000 if work.clock.anchors else None
            reference_end = work.clock.map(event.t1)-480*60000 if work.clock.anchors and event.t1 is not None else None
            reason = '' if eligible else 'identity_review' if not identity_ok else 'annotation_review' if not reviewed else 'missing_source_or_invalid_interval'
            events.append({'event_id':event_id, 'source_asset_id':asset, 'cow_id':cow,
                'device_id':identity.get('device_id') or legacy.get('device_id',''),
                'field_mark':identity.get('field_mark') or legacy.get('field_mark',''),
                'dataset_category':work.project.extras.get('dataset_category',doc.get('dataset_category','')),
                'code':label.code, 'label':label.name, 'layer':label.layer, 'type':label.type,
                'coordinates':doc['coordinates'], 'start_ms':event.t0, 'end_ms':event.t1,
                'start_epoch_ms':event.t0 if doc['coordinates']=='unix_epoch_ms' else reference_start,
                'end_epoch_ms':event.t1 if doc['coordinates']=='unix_epoch_ms' else reference_end,
                'time_basis':work.clock.basis, 'role':role, 'training_eligible':eligible, 'exclusion_reason':reason,
                'confirmation':confirmation, 'negative_for':event.extras.get('negative_for',[]),
                'source_annotation_sha256':inputs[-1]['sha256'], 'source_event_id':event.id,
                'legacy_sources':event.extras.get('legacy_sources', []), 'note':event.note})
        progress(index+1, 0, str(path))
    if not inputs:
        raise ValueError('未找到新版单文件母标签；请先保存并导出标注')
    # Decision models need continuous recordings, including unlabelled packets.
    # Only canonical registered sources are included; review archives stay separate.
    conflict_assets = ({e['source_asset_id'] for e in events if e['exclusion_reason']=='identity_review'} |
        {asset for asset,row in sources.items() if row.get('identity_eligible') is False} | manual_conflicts)
    for scope in source_roots:
        registry = scope/'资源索引.json'
        if not registry.is_file():
            continue
        for record in json.loads(registry.read_text(encoding='utf-8')).get('records',[]):
            if record.get('kind') != 'imu':
                continue
            if cancelled():
                raise InterruptedError('连续九轴导出已暂停')
            asset = record['sha256']
            if asset in manual_bindings and asset not in manual_conflicts:
                record={**record,**{k:manual_bindings[asset].get(k,record.get(k)) for k in ('cow_id','device_id','field_mark')},
                        'identity_status':'manual_override'}
            original = (scope/record['path']).resolve()
            if not original.is_relative_to(scope) or digest_file(original) != asset:
                raise ValueError('连续九轴来源校验失败：'+str(original))
            destination = target/'原始JSON'/f'{asset}.json'
            destination.parent.mkdir(exist_ok=True)
            if asset not in sources:
                shutil.copyfile(original,destination)
                if digest_file(destination) != asset:
                    raise ValueError('连续九轴副本校验失败')
                sources[asset] = {'asset_id':asset,'path':destination.relative_to(target).as_posix(),'sha256':asset}
            prior_cow=sources[asset].get('cow_id')
            if prior_cow and record.get('cow_id') and prior_cow!=record['cow_id']:
                conflict_assets.add(asset)
            sources[asset].update({k:record.get(k) for k in ('cow_id','device_id','field_mark','record_start_ms','record_end_ms')})
            sources[asset]['identity_eligible'] = bool(record.get('cow_id')) and asset not in conflict_assets and record.get('identity_status')!='conflict'
            sources[asset]['source_project'] = str(scope)
            sources[asset]['source_path'] = record['path']
            progress(len(sources),0,record['path'])
    unique = {}
    for event in events:
        binding=manual_bindings.get(event['source_asset_id'])
        if (event['source_asset_id'] in conflict_assets or
                sources.get(event['source_asset_id'],{}).get('identity_eligible') is False or
                binding and event['cow_id']!=binding['cow_id']):
            event['training_eligible']=False
            event['exclusion_reason']='identity_review'
        if event['event_id'] in unique:
            prior = unique[event['event_id']]
            # A conflict in any copy is never silently promoted by another copy.
            prior['training_eligible'] &= event['training_eligible']
            prior['legacy_sources'].extend(event['legacy_sources'])
            if event['exclusion_reason']:
                prior['exclusion_reason'] = event['exclusion_reason']
        else:
            unique[event['event_id']] = event
    events = sorted(unique.values(), key=lambda e:(e['cow_id'],e['start_epoch_ms'] or 0,e['event_id']))
    splits = dict(split_map) if split_map is not None else cow_splits(e['cow_id'] for e in events if e['training_eligible'])
    if any(v not in {'train','validation','test','unassigned'} for v in splits.values()):
        raise ValueError('Invalid cow split')
    for event in events:
        event['split'] = splits.get(event['cow_id'],'unassigned') if event['training_eligible'] else 'review'
    for record in sources.values():
        start=record.get('record_start_ms') or record.get('capture_timing',{}).get('sample_start_epoch_ms')
        if start is None or not record.get('device_id'):
            from cowmata_tailring.annotation.data import load_motion_json
            motion=load_motion_json(target/record['path'])
            if start is None:
                start=motion.epoch_at(0)
            record['device_id']=record.get('device_id') or motion.device
        destination=target/'原始JSON'/dataset_filename(record.get('cow_id'),record.get('device_id'),start,'.json')
        original=target/record['path']
        if original!=destination:
            if destination.exists():
                raise ValueError('耳标、设备和起始时间相同的九轴存在不同版本，请先复核')
            original.rename(destination)
            record['path']=destination.relative_to(target).as_posix()
    for filename, rows in [('events.jsonl',events),('sources.jsonl',list(sources.values()))]:
        with (target/filename).open('w',encoding='utf-8') as stream:
            for row in rows:
                stream.write(json.dumps(row,ensure_ascii=False)+'\n')
    fields = ['event_id','source_asset_id','cow_id','device_id','field_mark','dataset_category','code','label','type',
              'coordinates','start_ms','end_ms','start_epoch_ms','end_epoch_ms','role','split','training_eligible','exclusion_reason']
    _write_table(target/'母标签.csv',events,fields)
    label_tables={}
    for event in events:
        record=sources.get(event['source_asset_id'],{})
        start=record.get('record_start_ms') or record.get('capture_timing',{}).get('sample_start_epoch_ms') or event['start_epoch_ms']
        if start is None:
            continue
        name=dataset_filename(record.get('cow_id') or event['cow_id'],record.get('device_id') or event['device_id'],start,'.标注.csv')
        label_tables.setdefault(name,[]).append(event)
    for name,values in label_tables.items():
        _write_table(target/'标注'/name,values,fields)
    for code,title in EVENT_HEADS.items() if views else []:
        selected = [e for e in events if e['code']==code or code in e['negative_for']]
        _write_table(target/'事件识别'/title/'events.csv',selected,fields)
    if views:
        _write_table(target/'综合决策/产犊与行为.csv',events,fields)
    atomic_json(target/'cow-splits.json',splits)
    atomic_json(target/'PPG/预留.json',{'status':'reserved','channels':[],'annotations':[]})
    counts = Counter(e['role'] if e['training_eligible'] else 'review' for e in events)
    result = {'positive_events':counts['positive'],'negative_events':counts['negative'],'review_events':counts['review'],
              'independent_cows':len(splits),'raw_sources':len(sources),'target':str(target)}
    (target/'README.md').write_text('# COWMATA 母标签数据集\n\n原始 JSON 按 SHA-256 保存，未切窗、未重采样。'
        'events.jsonl 保存点事件与区间，事件可重叠。算法窗口由各算法适配器生成。\n\n'
        'training_eligible=false 的记录仅供复核；未标注区间是未知，不能自动转为负样本。'
        'cow-splits.json 对所有行为共用，同一牛不可跨训练、验证、测试。少于三牛不自动划分。\n\n'
        '缺少九轴的产犊绝对时刻仍保留，但不能作为九轴模型输入。PPG 保留占位。\n',encoding='utf-8')
    files = [{'path':p.relative_to(target).as_posix(),'sha256':digest_file(p),'size':p.stat().st_size}
             for p in sorted(target.rglob('*')) if p.is_file()]
    atomic_json(target/'dataset-manifest.json',{'schema':SCHEMA,'raw_resampling':'none','window_policy':'algorithm_specific',
                'source_annotations':inputs,'files':files,'counts':result,'unknown_is_negative':False})
    return result


def read_dataset(root):
    root = Path(root).resolve()
    manifest = json.loads((root/'dataset-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != SCHEMA:
        raise ValueError('不支持的数据集格式')
    if manifest.get('pipeline_complete') is False:
        raise ValueError('数据集导出已暂停；未完成目录不可作为训练集')
    for row in manifest['files']:
        path = (root/row['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file() or digest_file(path) != row['sha256']:
            raise ValueError('数据集文件校验失败：'+row['path'])
    result = {'manifest':manifest}
    for key in ('events','sources'):
        result[key] = [json.loads(line) for line in (root/(key+'.jsonl')).read_text(encoding='utf-8').splitlines() if line.strip()]
    return result
