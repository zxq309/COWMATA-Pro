"""Whole-record handback. Preserve both versions instead of merging human truth."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from .catalog import assert_not_being_written, digest_file, file_stamp
from .evidence import copy_evidence
from .label_file import contained, read_label_file
from .probe import SourceInspector
from .storage import atomic_json, read_json


def substantive(work):
    project = work.get('project', {})
    return bool(project.get('events') or work.get('drafts') or project.get('cow_id') or
                work.get('clock', {}).get('basis') == 'manual' or work.get('progress', {}).get('status') == 'done' or
                float(work.get('progress', {}).get('imu_ms', 0)) > 0)


def restore_label(catalog, path, *, require_done=False):
    """No raw source writes; no video inspection. Caller saves active work first."""
    catalog._write_check()
    path = Path(path).resolve()
    assert_not_being_written(path)
    return_stamp = file_stamp(path)
    if path.stat().st_size > 128 * 1024**2:
        raise ValueError('回传文件过大，请按整份九轴分别导出')
    document = read_label_file(path)
    if require_done and document['work'].get('progress', {}).get('status') != 'done':
        raise ValueError('自动接收仅处理明确标为已完成的整份成果；进行中成果请手动接收')
    embedded = document.get('embedded_imu') or {}
    if embedded.get('kind') != 'original_json':
        raise ValueError('协作回传需要“导出当前成果”的完整记录；片段或旧标签请用历史回看打开')
    asset = document['source']['asset_id']
    payload = base64.b64decode(embedded['original_json_base64'], validate=True)
    if hashlib.sha256(payload).hexdigest() != asset or embedded.get('sha256') != asset:
        raise ValueError('回传九轴身份校验失败，未写入工程')
    rows = [r for r in catalog.rows(kind='imu') if r['state'] not in {'missing','ignored'}]
    candidates = [r for r in rows if r['asset_id'] == asset]
    hint = document['source'].get('path','')
    if not candidates:
        # Relative path / basename are only lookup hints, never identity proof.
        candidates = [r for r in rows if r['path'] == hint]
        candidates += [r for r in rows if r['path'] != hint and Path(r['path']).name == Path(hint).name]
    selected = None
    for row in candidates:
        source = contained(catalog.root, row['path'])
        assert_not_being_written(source)
        before = file_stamp(source)
        if before == row['stamp'] and digest_file(source) == asset and file_stamp(source) == before:
            selected = row
            break
    if selected is None:
        raise ValueError('当前工程未找到相同内容的原始九轴；请先打开对应 JSON，再导入（不会仅按文件名匹配）')
    if selected['asset_id'] != asset or selected['state'] != 'ready':
        result = catalog.index_one(selected['path'], SourceInspector(catalog.root,catalog.meta))
        if not result or result['asset_id'] != asset or result['state'] != 'ready':
            raise ValueError('原始九轴尚未稳定或未通过解析，请稍后重试')
        selected = next(r for r in catalog.rows(kind='imu') if r['path'] == selected['path'])
    duration = selected['metadata']['duration_ms']
    if document['view']['start_ms'] != 0 or abs(document['view']['end_ms']-duration) > 1:
        raise ValueError('不能把局部片段当成整份已完成记录导入')
    incoming = document['work']
    for event in incoming['project'].get('events', []):
        end = event.get('t1')
        if event['t0'] < 0 or event['t0'] > duration or end is not None and (end < event['t0'] or end > duration):
            raise ValueError('回传标签超出原始九轴范围，请回传人员核对后重新导出')
    destination = catalog.work_path(asset)
    existing = read_json(destination,{})
    same = existing == incoming
    conflict = bool(existing and not same and substantive(existing))
    # Keep a portable original handback for audit and conflict comparison.
    archive = catalog.meta/'回传'
    digest = hashlib.sha256(json.dumps(document,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    returned = archive/f'{asset[:16]}_{digest[:16]}.cowmata.json'
    copy_evidence(document,path.parent,archive)
    assert_not_being_written(path)
    if file_stamp(path) != return_stamp:
        raise ValueError('回传文件在核验期间变化，请等待复制完成后重试')
    if not returned.exists():
        atomic_json(returned,document)
    if not conflict and not same:
        copy_evidence(document,path.parent,catalog.meta)
        atomic_json(destination,incoming)
    return {'status':'conflict' if conflict else 'unchanged' if same else 'imported',
            'asset_id':asset,'path':selected['path'],'stamp':selected['stamp'],
            'archive':str(returned),'progress':incoming.get('progress',{}), 'work':incoming if not conflict else None}


def return_folder_snapshot(folder, *, limit=10000):
    """Stat only; one inbox plus contributor subfolders, never media or links."""
    root = Path(folder).resolve(strict=True)
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in {'证据', '标注工程'}
                   and not (parent/d).is_symlink() and not getattr(parent/d, 'is_junction', lambda:False)()]
        if parent != root:
            dirs.clear()
        for name in files:
            if not name.lower().endswith('.json'):
                continue
            path = parent/name
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            result[str(path)] = file_stamp(path)
            if len(result) > limit:
                raise ValueError('回传文件夹超过一万份 JSON，请按批次选择；不要选择原始数据总目录')
    return result
