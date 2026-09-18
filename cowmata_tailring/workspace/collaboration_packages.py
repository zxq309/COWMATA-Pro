"""Offline collaboration, immutable task manifests and fail-closed annotation import."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import zipfile
from contextlib import ExitStack
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from .catalog import assert_not_being_written, digest_file, file_stamp, previous_video_files
from .dataset_access import DatasetLease
from .farm_layout import CATEGORY_PATHS, COLLABORATION, MARKER, farm_identity
from .package_paths import check, safe_path
from .storage import ProjectLock, atomic_json, read_json

SCHEMA = 'cowmata-collaboration-v1'
MANIFEST = '协作清单.json'
ASSIGNMENT = '.cowmata-assignment.json'
CHUNK = 4 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def label_schema():
    from cowmata_tailring.annotation.defaults import DEFAULT_LABELS
    return hashlib.sha256(canonical(DEFAULT_LABELS)).hexdigest()


def _registry(root):
    return Path(root) / COLLABORATION / '派发记录'


def inventory(root, category, *, cancelled=lambda: False):
    root = Path(root).resolve(strict=True)
    if not farm_identity(root) or category not in CATEGORY_PATHS:
        raise ValueError('请选择已统一目录的牧场及健康类别')
    assigned, prior_stamps, prior_paths = {}, {}, {}
    for path in _registry(root).glob('*.json'):
        entry = read_json(path, {})
        if entry.get('status') == 'ready':
            for unit in entry['manifest']['units']:
                assigned.setdefault(unit['key'], []).append(entry['manifest']['package_id'])
                prior_paths.setdefault(unit['key'], set()).update(unit['paths'])
                prior_stamps.setdefault(unit['key'], {}).update(entry['manifest'].get('sensor_stamps', {}))
    groups = {}
    for kind in ('Motion', 'PPG', 'Temp'):
        directory = safe_path(root, category + '/' + kind)
        for day in sorted(directory.iterdir()) if directory.is_dir() else []:
            check(cancelled)
            if not day.is_dir() or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day.name):
                continue
            date.fromisoformat(day.name)
            for owner in sorted(day.iterdir()):
                if not owner.is_dir():
                    continue
                key = category + '/' + day.name + '/' + owner.name
                value = groups.setdefault(key, dict(key=key, category=category, day=day.name, owner=owner.name,
                                                    paths=[], bytes=0, modalities=[], dispatches=assigned.get(key, [])))
                paths = sorted(owner.rglob('*.json'))
                for path in paths:
                    check(cancelled)
                    if path.name.endswith('.标注.json'):
                        continue
                    relative = path.relative_to(root).as_posix()
                    safe_path(root, relative)
                    value['paths'].append(relative)
                    value['bytes'] += path.stat().st_size
                if paths:
                    value['modalities'].append(kind)
    result = []
    for value in groups.values():
        if not value['paths']:
            continue
        primary = [p for p in value['paths'] if '/Motion/' in p] or [p for p in value['paths'] if '/PPG/' in p]
        saved, completed = 0, 0
        for relative in primary:
            label = _annotation_path(root, relative)
            if not label.is_file():
                continue
            doc = read_json(label, {})
            work = doc.get('work', doc)
            done = work.get('progress', {}).get('status') == 'done'
            populated = bool(work.get('project', {}).get('events') or work.get('drafts') or done)
            saved += int(populated)
            completed += int(done)
        value.update(annotation_records=len(primary), annotated_records=saved, completed_records=completed,
                     annotation_status='done' if primary and completed == len(primary) else 'partial' if saved else 'new')
        prior = prior_paths.get(value['key'], set())
        stamps = prior_stamps.get(value['key'], {})
        value['new_records'] = len(set(value['paths']) - prior) if prior else 0
        value['changed_records'] = sum(p in stamps and file_stamp(safe_path(root, p)) != stamps[p]
                                       for p in value['paths'])
        value['dispatch_status'] = ('new' if not prior else 'supplement' if value['new_records']
                                    else 'changed' if value['changed_records'] else 'assigned')
        result.append(value)
    return result


def plan_dispatch(root, units, *, count=1, views=None, purpose='annotation', cancelled=lambda: False):
    from .package_readiness import download_guard
    with DatasetLease([root], 'annotation'), download_guard(root):
        return _plan_dispatch(root, units, count=count, views=views, purpose=purpose, cancelled=cancelled)


def _check_unit_files(root, units):
    for unit in units:
        current = set()
        for kind in ('Motion', 'PPG', 'Temp'):
            folder = safe_path(root, '/'.join((unit['category'], kind, unit['day'], unit['owner'])))
            current.update(p.relative_to(root).as_posix() for p in folder.rglob('*.json')
                           if not p.name.endswith('.标注.json'))
        if current != set(unit['paths']):
            raise ValueError('所选资料已有新增或删除，请重新扫描：' + unit['key'])


def _plan_dispatch(root, units, *, count=1, views=None, purpose='annotation', cancelled=lambda: False):
    from .package_readiness import validate_complete
    root = Path(root).resolve(strict=True)
    identity = farm_identity(root)
    if not identity or not units or not 1 <= count <= len(units):
        raise ValueError('请选择资料；分包数量不能超过设备日期条目数')
    if purpose not in {'annotation', 'review'}:
        raise ValueError('Invalid task purpose')
    if purpose == 'annotation' and any(u.get('annotation_status') == 'done' for u in units):
        raise ValueError('已完成标注的数据请使用复核任务，避免重复标注')
    if purpose == 'review' and any(not u.get('annotated_records') for u in units):
        raise ValueError('复核任务只选择已有标注或草稿的数据')
    if len({u['key'] for u in units}) != len(units):
        raise ValueError('同一设备日期被重复选择')
    if views == []:
        raise ValueError('请至少选择一个录像视角；没有录像不能派包')
    _check_unit_files(root, units)
    task_id = uuid4().hex
    buckets = [[] for _ in range(count)]
    # Distribute indivisible device-days evenly, then balance record volume.
    for unit in sorted(units, key=lambda u: (-len(u['paths']), u['day'], u['owner'])):
        group = min(buckets, key=lambda b: (len(b), sum(len(u['paths']) for u in b), sum(u['bytes'] for u in b)))
        group.append(unit)
    plans, probe_cache = [], {}
    for part, group in enumerate(buckets, 1):
        days = sorted({u['day'] for u in group})
        categories = sorted({u['category'] for u in group})
        video_paths = set()
        missing_days = []
        for day in days:
            found = []
            for path in sorted((root / '录像' / day).rglob('*.mp4')):
                if views is None or path.parent.name in views:
                    found.append(path)
            if not found:
                missing_days.append(day)
            found.extend(p for p in previous_video_files(root / '录像', day) if views is None or p.parent.name in views)
            for path in found:
                relative = path.relative_to(root).as_posix()
                safe_path(root, relative)
                video_paths.add(relative)
        sensors = sorted({p for u in group for p in u['paths']})
        readiness = validate_complete(root, group, sorted(video_paths), views=views,
                                      cancelled=cancelled, probe_cache=probe_cache)
        video_paths = set(readiness['video_paths'])
        paths = sorted({*sensors, *video_paths})
        entries = [dict(path=p, size=safe_path(root, p).stat().st_size, stamp=file_stamp(safe_path(root, p))) for p in paths]
        for entry in entries:
            if entry['path'] in readiness['sensor_sha256']:
                entry['sha256'] = readiness['sensor_sha256'][entry['path']]
        caption = categories[0].replace('/', '-') if len(categories) == 1 else '多类别'
        task_name = '复核' if purpose == 'review' else '标注'
        name = f'{root.name}_{caption}_{days[0]}至{days[-1]}_{task_name}_{task_id[:12]}_P{part:03}'
        plans.append(dict(schema=SCHEMA, kind='raw', task_id=task_id, package_id=f'{task_id}-P{part:03}',
                          farm_id=identity['farm_id'], farm_name=root.name, part=part, parts=count,
                          label_schema=label_schema(), units=copy.deepcopy(group), categories=categories, dates=days,
                          sensor_paths=sensors, entries=entries, base_name=name, missing_video_dates=missing_days,
                           estimated_bytes=sum(e['size'] for e in entries), selected_views=views,
                          capacity_policy='informational_only', purpose=purpose, readiness=readiness,
                          sensor_stamps={e['path']: e['stamp'] for e in entries if e['path'] in sensors}))
    return plans


def _write_zip(output, manifest, entries, *, cancelled, progress, before_publish=lambda _: None):
    """One streaming pass; SHA/CRC from the exact bytes written, no media recompression."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.' + uuid4().hex + '.partial')
    if output.exists():
        raise FileExistsError('同名包已存在，不能覆盖：' + str(output))
    total = sum(e['size'] for e in entries)
    if shutil.disk_usage(output.parent).free < total + 16 * 1024**2:
        raise OSError('磁盘剩余空间不足；请更换位置或减少所选范围')
    done, members = 0, []
    try:
        with zipfile.ZipFile(temporary, 'x', allowZip64=True, compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for entry in entries:
                check(cancelled)
                path, payload = entry.get('source'), entry.get('payload')
                before = None
                if path is not None:
                    path = Path(path)
                    assert_not_being_written(path)
                    before = file_stamp(path)
                    if entry.get('stamp') and before != entry['stamp']:
                        raise ValueError('打包前原始数据发生变化，请重新扫描：' + str(path))
                info = zipfile.ZipInfo(entry['path'])
                info.compress_type = zipfile.ZIP_STORED if entry['path'].lower().endswith(('.mp4', '.jpg', '.png')) else zipfile.ZIP_DEFLATED
                info._compresslevel = 1
                info.file_size = entry['size']
                digest = hashlib.sha256()
                size = 0
                import io
                with (path.open('rb') if path is not None else io.BytesIO(payload)) as source, archive.open(info, 'w', force_zip64=True) as target:
                    while block := source.read(CHUNK):
                        check(cancelled)
                        target.write(block)
                        digest.update(block)
                        size += len(block)
                        done += len(block)
                        progress(done, total, entry['path'])
                if size != entry['size'] or path is not None and file_stamp(path) != before:
                    raise ValueError('打包期间源文件发生变化')
                sha = digest.hexdigest()
                if entry.get('sha256') and entry['sha256'] != sha:
                    raise ValueError('文件内容与原始派发记录不一致')
                members.append(dict(path=entry['path'], size=size, sha256=sha))
            manifest = {**manifest, 'members': members, 'created_at': datetime.now(timezone.utc).isoformat()}
            archive.writestr(MANIFEST, canonical(manifest))
        with temporary.open('rb+') as stream:
            os.fsync(stream.fileno())
        validate_archive(temporary)
        check(cancelled)
        before_publish(manifest)
        if os.name == 'nt':
            temporary.rename(output)  # Windows rename refuses an existing destination.
        else:
            os.link(temporary, output)
            temporary.unlink()
        return manifest
    finally:
        temporary.unlink(missing_ok=True)


def dispatch(root, plans, *, cancelled=lambda: False, progress=lambda *_: None):
    from .package_readiness import download_guard, ledger_state, validate_complete
    root = Path(root).resolve(strict=True)
    identity = farm_identity(root)
    outputs = []
    home = root / COLLABORATION
    home.mkdir(exist_ok=True)
    with DatasetLease([root], 'annotation'), download_guard(root), ExitStack() as stack:
        lock = ProjectLock(home / '.dispatch.lock')
        stack.callback(lock.close)
        if not lock.acquired:
            raise ValueError('另一个派发任务正在运行')
        probe_cache = {}
        for plan in plans:
            check(cancelled)
            if plan['farm_id'] != identity['farm_id']:
                raise ValueError('派发方案不属于此牧场')
            _check_unit_files(root, plan['units'])
            proof = validate_complete(root, plan['units'], [e['path'] for e in plan['entries'] if e['path'].endswith('.mp4')],
                                      views=plan.get('selected_views'), cancelled=cancelled, probe_cache=probe_cache)
            if proof['cycle_sha256'] != plan.get('readiness', {}).get('cycle_sha256'):
                raise ValueError('下载检查点已变化，请重新预览派发方案')
            if set(proof['video_paths']) != {e['path'] for e in plan['entries'] if e['path'].endswith('.mp4')}:
                raise ValueError('录像范围已变化，请重新预览派发方案')
            entries = [dict(e, path=root.name + '/' + e['path'], source=safe_path(root, e['path'])) for e in plan['entries']]
            marker = canonical(identity)
            entries.append(dict(path=root.name + '/' + MARKER, payload=marker, size=len(marker)))
            manifest = {k: copy.deepcopy(v) for k, v in plan.items() if k not in {'entries', 'sensor_paths'}}
            baseline, evidence_entries = [], {}
            for relative in plan['sensor_paths']:
                if '/Motion/' not in relative and '/PPG/' not in relative:
                    continue
                label = _annotation_path(root, relative)
                if label.is_file():
                    doc = read_json(label, {})
                    rel = label.relative_to(root).as_posix()
                    baseline.append(rel)
                    entries.append(dict(path=root.name + '/' + rel, source=label, size=label.stat().st_size, stamp=file_stamp(label)))
                    evidence_entries.update(_evidence_entries(doc, label, root))
            for relative, entry in evidence_entries.items():
                entries.append({**entry, 'path': root.name + '/' + relative})
            manifest['baseline_annotations'] = baseline
            manifest['baseline_evidence'] = sorted(evidence_entries)
            output = home / '原始数据包' / (plan['base_name'] + '_原始.zip')
            def final_check(published_manifest):
                _check_unit_files(root, plan['units'])
                for relative in baseline:
                    label = safe_path(root, relative)
                    if _validate_document(read_json(label, {}), root, published_manifest) != label:
                        raise ValueError('标注目录与派发目录树不一致')
                for entry in entries:
                    if entry.get('source') and file_stamp(Path(entry['source'])) != entry['stamp']:
                        raise ValueError('打包期间源文件发生变化，请重新扫描')
                current, _ = ledger_state(root)
                if current.sources != proof['sources']:
                    raise ValueError('打包期间 CSV 已变化，请重新核对')
            result = _write_zip(output, manifest, entries, cancelled=cancelled, progress=progress, before_publish=final_check)
            atomic_json(_registry(root) / (plan['package_id'] + '.json'),
                        dict(status='ready', manifest=result, output=str(output)), backup=False)
            outputs.append(output)
    return outputs


def validate_archive(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = set()
        for info in infos:
            safe_path(Path.cwd(), info.filename)
            key = info.filename.casefold()
            if key in names or info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                raise ValueError('压缩包含重复路径、目录链接或加密条目')
            names.add(key)
        if MANIFEST not in archive.namelist() or archive.getinfo(MANIFEST).file_size > 64 * 1024**2:
            raise ValueError('缺少有效的协作清单')
        value = json.loads(archive.read(MANIFEST))
        if value.get('schema') != SCHEMA or value.get('kind') not in {'raw', 'annotations'}:
            raise ValueError('协作包格式或版本不支持')
        UUID(value['farm_id'])
        UUID(value['task_id'])
        if not re.fullmatch(re.escape(value['task_id']) + r'-P\d{3,}', value['package_id']):
            raise ValueError('任务与分包编号不一致')
        if value.get('label_schema') != label_schema():
            raise ValueError('协作包的标签范式与当前软件不一致')
        members = value.get('members', [])
        declared = {m['path'].casefold() for m in members}
        if len(declared) != len(members) or declared != names - {MANIFEST.casefold()}:
            raise ValueError('ZIP 内容与清单不一致')
        for member in members:
            info = archive.getinfo(member['path'])
            if info.file_size != member['size'] or not re.fullmatch(r'[0-9a-f]{64}', member['sha256']):
                raise ValueError('ZIP 文件大小或内容摘要无效')
        return value


def _extract(path, directory, manifest, cancelled, progress):
    total, done = sum(m['size'] for m in manifest['members']), 0
    if shutil.disk_usage(directory).free < total + 16 * 1024**2:
        raise OSError('解包磁盘空间不足')
    with zipfile.ZipFile(path) as archive:
        for member in manifest['members']:
            check(cancelled)
            destination = safe_path(directory, member['path'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with archive.open(member['path']) as source, destination.open('xb') as target:
                while block := source.read(CHUNK):
                    check(cancelled)
                    size += len(block)
                    if size > member['size']:
                        raise ValueError('解压大小超出清单')
                    digest.update(block)
                    target.write(block)
                    done += len(block)
                    progress(done, total, member['path'])
            if size != member['size'] or digest.hexdigest() != member['sha256']:
                raise ValueError('ZIP 内容校验失败：' + member['path'])


def open_raw_package(path, destination, *, cancelled=lambda: False, progress=lambda *_: None):
    manifest = validate_archive(path)
    if manifest['kind'] != 'raw':
        raise ValueError('请选择派发的原始数据包')
    name = manifest['farm_name']
    safe_path(Path.cwd(), name)
    if '/' in name:
        raise ValueError('牧场名称不能包含路径')
    for member in manifest['members']:
        relative = PurePosixPath(member['path'])
        if relative.parts[0] != name:
            raise ValueError('原始包目录树与牧场不一致')
        rel = PurePosixPath(*relative.parts[1:]).as_posix()
        is_annotation = rel in manifest.get('baseline_annotations', []) and '/标注工程/' in rel and rel.endswith('.标注.json')
        is_evidence = rel in manifest.get('baseline_evidence', []) and '/标注工程/证据/' in rel and rel.endswith('.jpg')
        if rel != MARKER and not _raw_relative(rel, manifest) and not is_annotation and not is_evidence:
            raise ValueError('原始包包含非授权资料路径')
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    final = destination / manifest['package_id']
    if final.exists():
        raise FileExistsError('此任务已解包，请直接打开已有工程：' + str(final / name))
    with tempfile.TemporaryDirectory(prefix='.cowmata-unpack-', dir=destination) as temporary:
        staging = Path(temporary)
        _extract(path, staging, manifest, cancelled, progress)
        farm = staging / name
        if farm_identity(farm)['farm_id'] != manifest['farm_id']:
            raise ValueError('牧场标识不一致')
        referenced_evidence = set()
        for relative in manifest.get('baseline_annotations', []):
            label = safe_path(farm, relative)
            doc = read_json(label, {})
            if _validate_document(doc, farm, manifest) != label:
                raise ValueError('标注目录与派发目录树不一致')
            referenced_evidence.update(_evidence_entries(doc, label, farm))
            doc['source']['project_root_hint'] = str(final / name)
            atomic_json(label, doc, backup=False)
        if referenced_evidence != set(manifest.get('baseline_evidence', [])):
            raise ValueError('清单存在未引用或缺失的证据图')
        for category in manifest['categories']:
            atomic_json(safe_path(farm, category + '/标注工程/annotation-layout.json'), {'schema': 'dated-annotations-v1'}, backup=False)
        atomic_json(farm / ASSIGNMENT, manifest, backup=False)
        (farm / COLLABORATION).mkdir(exist_ok=True)
        check(cancelled)
        staging.rename(final)
    return final / name


def _raw_relative(relative, manifest):
    parts = PurePosixPath(relative).parts
    if parts[:1] == ('录像',):
        return len(parts) == 4 and bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', parts[1])) and bool(re.fullmatch(r'视角\d{2}', parts[2])) and parts[-1].lower().endswith('.mp4')
    for unit in manifest['units']:
        prefix = PurePosixPath(unit['category']).parts
        tail = parts[len(prefix):]
        if (unit['category'] in CATEGORY_PATHS and parts[:len(prefix)] == prefix
                and len(tail) == 4 and tail[0] in {'Motion', 'PPG', 'Temp'}
                and tail[1] == unit['day'] and tail[2] == unit['owner']
                and tail[3].endswith('.json') and not tail[3].endswith('.标注.json')
                and relative in unit['paths']):
            date.fromisoformat(unit['day'])
            return True
    return False


def _raw_members(manifest):
    prefix = manifest['farm_name'] + '/'
    return {m['path'][len(prefix):]: m for m in manifest['members'] if m['path'].startswith(prefix) and _raw_relative(m['path'][len(prefix):], manifest)}


def _annotation_path(root, relative):
    from .annotation_store import dated_path
    for category in CATEGORY_PATHS:
        if relative.startswith(category + '/'):
            target = dated_path(Path(root) / category / '标注工程', relative)
            if target:
                return target
    raise ValueError('标注来源没有对应的健康类别及日期目录')


def _validate_document(doc, root, assignment):
    from cowmata_tailring.annotation.defaults import DEFAULT_LABELS

    from .device_identity import parse_device_folder
    from .sensor_records import load_sensor_json
    from .work import SessionWork
    if doc.get('format') != 'cowmata-annotation' or doc.get('version') not in {1, 2} or doc.get('coordinates') != 'parent_imu_ms' or doc.get('embedded_imu'):
        raise ValueError('回传必须是完整记录的纯标注文件，不能夹带原始数据')
    relative = doc['source']['path']
    raw_members = _raw_members(assignment)
    member = raw_members.get(relative)
    if not member or '/Motion/' not in relative and '/PPG/' not in relative:
        raise ValueError('标注来源不在本次派发范围内')
    raw = safe_path(root, relative)
    before = file_stamp(raw)
    assert_not_being_written(raw)
    if digest_file(raw) != member['sha256'] or doc['source']['asset_id'] != member['sha256']:
        raise ValueError('原始来源内容与派发时不一致')
    motion = load_sensor_json(raw)
    if before != file_stamp(raw):
        raise ValueError('核验期间原始来源发生变化')
    if doc['work']['asset_id'] != member['sha256']:
        raise ValueError('标签与来源内容标识不一致')
    labels = doc['work']['project'].get('labels', [])
    if [(r.get('key'), r.get('code'), r.get('name')) for r in labels] != [(r.get('key'), r.get('code'), r.get('name')) for r in DEFAULT_LABELS]:
        raise ValueError('标签列表与当前 COWMATA 范式不一致，请先核对历史标签')
    work = SessionWork.from_dict(doc['work'])
    identity = parse_device_folder(raw.parent.name)
    if work.project.cow_id != identity.cow_id or str(motion.device).upper() != identity.device_id:
        raise ValueError('牛号或设备编号与派发来源不一致，需人工核对')
    from .paired_dataset import category_for
    if doc.get('dataset_category') != category_for(raw) or work.category_fields()['dataset_category'] != category_for(raw):
        raise ValueError('健康类别与原始资料目录不一致')
    lo, hi = doc['view']['start_ms'], doc['view']['end_ms']
    if lo != 0 or not isinstance(hi, (int, float)) or not math.isfinite(hi) or abs(hi-motion.duration_ms) > .001:
        raise ValueError('标注时间范围与完整原始记录不一致')
    ids = set()
    for event in work.project.events:
        if event.id in ids or not event.id or not 0 <= event.li < len(DEFAULT_LABELS):
            raise ValueError('存在重复事件或无效标签索引')
        ids.add(event.id)
        end = event.t1 if event.t1 is not None else event.t0
        if not math.isfinite(event.t0+end) or not 0 <= event.t0 <= end <= hi + .001:
            raise ValueError('标签事件时间超出原始记录')
        code = event.extras.get('label_code')
        if code and code != DEFAULT_LABELS[event.li]['code']:
            raise ValueError('事件代码与标签索引不一致')
    for draft in work.drafts:
        if draft['id'] in ids or not 0 <= draft['label_index'] < len(DEFAULT_LABELS):
            raise ValueError('视频草稿身份或标签索引无效')
        ids.add(draft['id'])
        begin, end = draft['reference_start'], draft.get('reference_end')
        if not math.isfinite(begin) or end is not None and (not math.isfinite(end) or end < begin):
            raise ValueError('视频草稿时间无效')
    for row in doc.get('video', {}).get('rows', []):
        video = raw_members.get(row.get('path'))
        if not video or not row['path'].startswith('录像/') or row.get('asset_id') != video['sha256']:
            raise ValueError('Video reference does not match the assigned recording')
        row.pop('external_source', None)
        row.pop('resolved_source', None)
    doc.get('video', {}).pop('archive', None)
    return _annotation_path(root, relative)


def _evidence_entries(doc, label, root):
    from .evidence import read_image, safe_relative
    entries = {}
    for event in [*doc['work']['project']['events'], *doc['work'].get('drafts', [])]:
        for item in event.get('screenshots', {}).get('items', []):
            if item.get('status') == 'captured':
                read_image(label.parent, item)
                path = safe_relative(label.parent, item['path'])
                if not path.is_file():
                    meta = next(p for p in label.parents if p.name == '标注工程')
                    path = safe_relative(meta, item['path'])
                relative = path.relative_to(root).as_posix()
                entries[relative] = dict(path=relative, source=path, size=path.stat().st_size,
                                         sha256=item['sha256'], stamp=file_stamp(path))
    return entries


def make_return(root, *, cancelled=lambda: False, progress=lambda *_: None):
    root = Path(root).resolve(strict=True)
    assignment = read_json(root / ASSIGNMENT, {})
    if assignment.get('schema') != SCHEMA or assignment.get('kind') != 'raw':
        raise ValueError('此目录不是已派发任务；请从原始数据包打开工程')
    entries, annotations = {}, []
    with DatasetLease([root], 'review'):
        for relative in _raw_members(assignment):
            if '/Motion/' not in relative and '/PPG/' not in relative:
                continue
            check(cancelled)
            label = _annotation_path(root, relative)
            if not label.is_file():
                continue
            assert_not_being_written(label)
            doc = json.loads(label.read_text(encoding='utf-8-sig'))
            target = _validate_document(doc, root, assignment)
            if target != label:
                raise ValueError('标注目录与派发目录树不一致')
            doc['source']['project_root_hint'] = ''
            payload = canonical(doc)
            rel = label.relative_to(root).as_posix()
            annotations.append(rel)
            entries[rel] = dict(path=rel, payload=payload, size=len(payload))
            entries.update(_evidence_entries(doc, label, root))
        if not annotations:
            raise ValueError('本任务尚未保存标注结果')
        manifest = {k: copy.deepcopy(assignment[k]) for k in ('schema', 'task_id', 'package_id', 'farm_id', 'farm_name', 'label_schema', 'base_name')}
        manifest.update(kind='annotations', revision=uuid4().hex, annotations=annotations,
                        source_manifest_sha256=hashlib.sha256(canonical(assignment)).hexdigest(), purpose=assignment.get('purpose', 'annotation'))
        output = root / COLLABORATION / '标注数据包' / (manifest['base_name'] + '_标注_' + manifest['revision'][:12] + '.zip')
        _write_zip(output, manifest, list(entries.values()), cancelled=cancelled, progress=progress)
        return output


def _semantic_document(doc):
    doc = copy.deepcopy(doc)
    doc.get('source', {}).pop('project_root_hint', None)
    return doc


def _recover_receives(root):
    """Rollback interrupted publication only when its expected bytes still match."""
    for journal_path in (root / COLLABORATION / '接收记录').glob('*/事务.json'):
        journal = read_json(journal_path, {})
        if journal.get('status') != 'committing':
            continue
        entries = journal.get('publications', [])
        for entry in entries:
            target = safe_path(root, entry['path'])
            if target.exists() and digest_file(target) != entry['sha256']:
                raise ValueError('Interrupted import contains changed work; inspect ' + str(journal_path))
        for entry in reversed(entries):
            safe_path(root, entry['path']).unlink(missing_ok=True)
        journal['status'] = 'rolled_back'
        atomic_json(journal_path, journal, backup=False)


def _publish_annotation(root, relative, payload, source, journal, journal_path):
    """Publish a fully flushed file atomically; never expose a partial label."""
    target = safe_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name('.receive-' + uuid4().hex)
    try:
        with temporary.open('xb') as output:
            if source is not None:
                with source.open('rb') as incoming:
                    shutil.copyfileobj(incoming, output, CHUNK)
            else:
                output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        sha = digest_file(temporary)
        expected = digest_file(source) if source is not None else hashlib.sha256(payload).hexdigest()
        if sha != expected:
            raise OSError('Annotation staging verification failed')
        if target.exists():
            raise FileExistsError(str(target))
        journal['publications'].append(dict(path=relative, sha256=sha))
        atomic_json(journal_path, journal, backup=False)
        if os.name == 'nt':
            temporary.rename(target)  # Windows rename refuses an existing target.
        else:
            os.link(temporary, target)
        journal['created'].append(relative)
        if digest_file(target) != sha:
            raise OSError('写入标注后的二次核验失败')
        atomic_json(journal_path, journal, backup=False)
    finally:
        temporary.unlink(missing_ok=True)


def receive_return(root, path, *, cancelled=lambda: False, progress=lambda *_: None):
    root = Path(root).resolve(strict=True)
    manifest = validate_archive(path)
    if manifest['kind'] != 'annotations' or manifest['farm_id'] != farm_identity(root)['farm_id']:
        raise ValueError('标注包不属于此牧场')
    original = read_json(_registry(root) / (manifest['package_id'] + '.json'), {})
    assignment = original.get('manifest', {})
    if original.get('status') != 'ready' or hashlib.sha256(canonical(assignment)).hexdigest() != manifest.get('source_manifest_sha256'):
        raise ValueError('找不到对应的原始派发记录或任务清单不一致')
    annotations = manifest.get('annotations', [])
    if not annotations or len(set(annotations)) != len(annotations):
        raise ValueError('没有有效且唯一的标注记录')
    for member in manifest['members']:
        relative = member['path']
        safe_path(root, relative)
        if relative not in annotations and not ('/标注工程/' in relative and '/证据/' in relative and relative.endswith('.jpg')):
            raise ValueError('回传包含未授权文件或原始数据')
    home = root / COLLABORATION
    report = dict(package_id=manifest['package_id'], imported=0, unchanged=0, conflicts=[], files=[])
    with DatasetLease([root], 'organize'), ExitStack() as locks, tempfile.TemporaryDirectory(prefix='cowmata-receive-') as temporary:
        for category in {u['category'] for u in assignment['units']}:
            meta = safe_path(root, category + '/标注工程')
            meta.mkdir(parents=True, exist_ok=True)
            lock = ProjectLock(meta / 'writer.lock')
            locks.callback(lock.close)
            if not lock.acquired:
                raise ValueError('相关标注工程正在使用，请保存并关闭该工程后接收')
        _recover_receives(root)
        staging = Path(temporary)
        _extract(path, staging, manifest, cancelled, progress)
        pending, allowed_evidence = [], set()
        for relative in annotations:
            check(cancelled)
            saved = safe_path(staging, relative)
            doc = json.loads(saved.read_text(encoding='utf-8'))
            target = _validate_document(doc, root, assignment)
            if target.relative_to(root).as_posix() != relative:
                raise ValueError('标注包目录树与本地目录不一致')
            evidence = _evidence_entries(doc, saved, staging)
            allowed_evidence.update(evidence)
            doc['source']['project_root_hint'] = str(root)
            if target.exists():
                previous = json.loads(target.read_text(encoding='utf-8-sig'))
                if _semantic_document(previous) == _semantic_document(doc):
                    report['unchanged'] += 1
                else:
                    report['conflicts'].append(relative)
            else:
                pending.append((relative, canonical(doc)))
        declared = {m['path'] for m in manifest['members']}
        if declared != set(annotations) | allowed_evidence:
            raise ValueError('清单存在未引用或缺失的证据图')
        if report['conflicts']:
            # Entire return is held, never partially applied across conflicting records.
            archive_dir = home / '冲突待核对' / (manifest['package_id'] + '-' + uuid4().hex)
            archive_dir.mkdir(parents=True)
            shutil.copyfile(path, archive_dir / '待核对标注包.zip')
            report['archive'] = str(archive_dir)
            atomic_json(archive_dir / '核验报告.json', report, backup=False)
            return report
        for relative in allowed_evidence:
            target = safe_path(root, relative)
            source = safe_path(staging, relative)
            if target.exists() and digest_file(target) != digest_file(source):
                raise ValueError('本地证据图同名但内容不同，停止接收')
        check(cancelled)
        journal_dir = home / '接收记录' / (manifest['package_id'] + '-' + uuid4().hex)
        journal_dir.mkdir(parents=True)
        journal = dict(status='committing', package_id=manifest['package_id'], created=[], publications=[], planned=[r for r, _ in pending])
        atomic_json(journal_dir / '事务.json', journal, backup=False)
        try:
            for relative in sorted(allowed_evidence):
                target = safe_path(root, relative)
                if not target.exists():
                    _publish_annotation(root, relative, None, safe_path(staging, relative), journal, journal_dir / '事务.json')
            for relative, payload in pending:
                _publish_annotation(root, relative, payload, None, journal, journal_dir / '事务.json')
                report['imported'] += 1
                report['files'].append(relative)
            for category in {u['category'] for u in assignment['units']}:
                atomic_json(safe_path(root, category + '/标注工程/annotation-layout.json'), {'schema': 'dated-annotations-v1'}, backup=False)
            journal['status'] = 'complete'
            atomic_json(journal_dir / '事务.json', journal, backup=False)
            atomic_json(journal_dir / '核验报告.json', report, backup=False)
        except Exception:
            # Only paths exclusively created by this transaction, never pre-existing work.
            for relative in reversed(journal['created']):
                safe_path(root, relative).unlink()
            journal['status'] = 'rolled_back'
            atomic_json(journal_dir / '事务.json', journal, backup=False)
            raise
    return report
