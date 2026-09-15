"""Optimistic, backed-up editing of existing annotations; never creates new labels."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .paired_dataset import (  # noqa: F401 -- public compatibility export
    local_source_root,
    paired_label,
)
from .storage import ProjectLock, atomic_json


def resolve_label_path(path):
    path=Path(path).resolve()
    if path.stem.endswith('_raw') or ('标注工程' not in path.parts and any(p in path.parts for p in ('Motion','PPG')) and not path.name.endswith(('_label.json','.标注.json'))):
        candidate=paired_label(path)
        if not candidate.is_file():
            raise ValueError('没有找到与原始数据对应的既有标签；复核不会新建标签文件')
        return candidate
    return path




def save_review(path, work, *, expected_sha):
    path=Path(path).resolve()
    if not path.is_file() or path.stem.endswith('_raw'):
        raise ValueError('复核只能修改已存在的标签文件，不能新建标签或改写原始数据')
    lock=ProjectLock(path.with_name('.'+path.name+'.review.lock'))
    try:
        if not lock.acquired:
            raise ValueError('该标签正在由其他窗口修改')
        payload=path.read_bytes()
        if hashlib.sha256(payload).hexdigest()!=expected_sha:
            raise ValueError('标签文件已被其他窗口修改，请重新载入')
        original=json.loads(payload.decode('utf-8-sig'))
        if original.get('format')!='cowmata-annotation' or 'work' not in original:
            raise ValueError('只允许修改原有标注文件')
        before=original['work']
        work=work.to_dict() if hasattr(work,'to_dict') else copy.deepcopy(work)
        if before.get('asset_id')!=work.get('asset_id'):
            raise ValueError('原始数据身份发生变化，不能保存复核')
        old_ids={e['id'] for e in before['project'].get('events',[])}
        new_ids={e['id'] for e in work['project'].get('events',[])}
        if not new_ids<=old_ids:
            raise ValueError('复核不能新增标签记录，只能修改已有记录')
        if not {d['id'] for d in work.get('drafts',[])} <= {d['id'] for d in before.get('drafts',[])}:
            raise ValueError('复核不能新建视频草稿')
        labels=work['project'].get('labels',[])
        for event in work['project'].get('events',[]):
            if not 0<=event['li']<len(labels):
                raise ValueError('标签索引无效')
            start=float(event['t0'])
            end=event.get('t1')
            if not math.isfinite(start) or start<0 or end is not None and (not math.isfinite(float(end)) or float(end)<start):
                raise ValueError('标签时间范围无效')
        version=next((p for p in path.parents if (p/'dataset-manifest.json').is_file()),None)
        manifest=None
        if version:
            manifest=json.loads((version/'dataset-manifest.json').read_text(encoding='utf-8'))
            if manifest.get('status')=='building':
                raise ValueError('数据集仍在构建；请先暂停或完成构建再保存复核')
        home=version or next((p for p in path.parents if p.name=='标注工程'),path.parent)
        history=home/'.label-history'
        history.mkdir(exist_ok=True)
        token=uuid.uuid4().hex
        backup=history/(path.name+'.'+token+'.bak')
        backup.write_bytes(payload)
        result=copy.deepcopy(original)
        result['work']['project']=work['project']
        result['work']['drafts']=work.get('drafts',[])
        # Display-time automatic clocks are not saved as new calibration.
        revision=dict(id=token,at=datetime.now(timezone.utc).isoformat(),before_sha256=expected_sha,
                      event_ids=sorted(new_ids),backup=str(backup.relative_to(home)))
        result.setdefault('review_history',[]).append(revision)
        result['work']['project'].setdefault('review_history', []).append(revision)
        if 'dataset' in result:
            result['dataset']['review_revision']=token
        atomic_json(path,result,backup=False)
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if manifest is not None:
            for row in manifest.get('rows',[]):
                value=Path(row.get('label_target',''))
                same=value==path or row.get('label_relative')==path.relative_to(version).as_posix()
                if same:
                    row['label_sha256']=digest
                    row['review_revision']=token
            manifest['last_review']=revision
            atomic_json(version/'dataset-manifest.json',manifest,backup=False)
        return digest
    finally:
        lock.close()
