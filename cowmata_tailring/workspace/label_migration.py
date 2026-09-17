"""Backed-up in-place migration of saved operator annotations."""
from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

from cowmata_tailring.annotation.taxonomy import event_code, upgrade_document

from .paired_dataset import category_for
from .storage import ProjectLock, atomic_json
from .work import SessionWork


def migrate_labels(root, backup, *, apply=False):
    root, backup = Path(root).resolve(), Path(backup).resolve()
    paths = sorted(p for p in root.rglob('*.标注.json') if '.label-history' not in p.parts)
    prepared = []
    removed = kept = feeding = onset = 0
    for path in paths:
        data = path.read_bytes()
        old = json.loads(data.decode('utf-8-sig'))
        project = old.get('work',{}).get('project',{})
        labels = project.get('labels',[])
        for event in project.get('events',[]):
            code,_ = event_code(event,labels)
            onset += int(code=='STRAINING_ONSET')
        updated = upgrade_document(old,category=category_for(path,old))
        parsed = SessionWork.from_dict(updated)
        old_events = {e['id']:e for e in project.get('events',[])}
        for event in updated['work']['project']['events']:
            original = old_events[event['id']]
            if (event['t0'],event.get('t1')) != (original['t0'],original.get('t1')):
                raise ValueError('迁移不得改变原始标签起止时间')
        if old['work'].get('clock') != updated['work'].get('clock'):
            raise ValueError('迁移不得改变同步信息')
        count = len(project.get('events',[]))-len(parsed.project.events)
        removed += count
        kept += len(parsed.project.events)
        feeding += sum(parsed.project.labels[e.li].code=='FEEDING' for e in parsed.project.events)
        prepared.append((path,data,updated))
    result = dict(files=len(paths),removed=removed,kept=kept,feeding_preserved=feeding,onset_used=onset,applied=False,backup=str(backup),rows=[])
    if not apply:
        return result
    if onset:
        raise ValueError(f'发现 {onset} 条努责首次出现标签，需用户确认后处理')
    if backup.exists():
        raise ValueError('备份目录已存在，请使用新的备份目录')
    metadata = sorted({next(p for p in path.parents if p.name=='标注工程') for path in paths})
    with ExitStack() as stack:
        for meta in metadata:
            lock = ProjectLock(meta/'writer.lock')
            stack.callback(lock.close)
            if not lock.acquired:
                raise OSError('该工程仍被标注窗口占用，请关闭后再迁移：'+str(meta))
        backup.mkdir(parents=True)
        for path,data,updated in prepared:
            if path.read_bytes()!=data:
                raise OSError('标签在迁移前发生变化，已保留原件：'+str(path))
            saved=backup/path.relative_to(root)
            saved.parent.mkdir(parents=True,exist_ok=True)
            saved.write_bytes(data)
            atomic_json(path,updated,backup=False)
            result['rows'].append(dict(path=str(path),backup=str(saved),before_sha256=hashlib.sha256(data).hexdigest(),
                                       after_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            atomic_json(backup/'migration.json',result,backup=False)
        result['applied']=True
        atomic_json(backup/'migration.json',result,backup=False)
    return result
