"""Classification task discovery and ancillary-file relocation."""
from __future__ import annotations

import json
from pathlib import Path

from . import organization as core
from .classification_report import LiveReport  # noqa: F401 -- compatibility import


def validate_resume(plan, request):
    """A paused transaction keeps its destination and source-to-view mapping."""
    def path_key(value):
        return core.safe_path(value)

    previous_root = plan.get('resource_root', plan['target'])
    if path_key(previous_root) != path_key(request['target']):
        raise ValueError('未完成任务的输出目录与当前选择不同，请先继续原任务：' + previous_root)
    previous_farm = plan.get('farm_path')
    requested_farm = request.get('farm')
    if previous_farm and requested_farm and Path(requested_farm).is_absolute():
        if path_key(previous_farm) != path_key(requested_farm):
            raise ValueError('未完成任务的输出目录（牧场）已改变，请先继续原任务')
    for old_key, new_key, default, label in (
        ('category', 'category', None, '类别'),
        ('scenario', 'scenario', 'mixed', '归类方式'),
        ('transfer', 'transfer', 'copy', '复制或移动方式'),
        ('requested_start', 'start', '', '开始日期'),
        ('requested_end', 'end', '', '结束日期'),
    ):
        if (plan.get(old_key, default) or default) != (request.get(new_key, default) or default):
            raise ValueError('未完成任务的' + label + '与当前选择不同，请先继续原任务')
    old_sources = {path_key(s['path']): s.get('camera') or 'auto' for s in plan.get('sources', [])}
    for source in request['sources']:
        path = path_key(source['path'])
        if path in old_sources and old_sources[path] != (source.get('camera') or 'auto'):
            raise ValueError('未完成任务的视角映射已改变，请先按原映射继续任务：' + str(path))
    root = path_key(plan['target'])
    for row in plan.get('rows', []):
        if row.get('target') and not path_key(row['target']).is_relative_to(root):
            raise ValueError('旧任务包含当前输出目录以外的归类记录，停止复用：' + row['target'])


def pending_job(paths):
    from .dataset_access import overlaps, registry_root
    matches = []
    for path in (registry_root() / 'pending').glob('*.json'):
        value = json.loads(path.read_text(encoding='utf-8'))
        if any(overlaps(a, b) for a in paths for b in value['paths']):
            job = Path(value['job'])
            if (job / 'plan.json').is_file():
                matches.append(job)
    matches = list(dict.fromkeys(matches))
    if len(matches) > 1:
        raise ValueError('存在多个未完成任务，请分别使用继续归类：' + '；'.join(map(str, matches)))
    return matches[0] if matches else None


def clean_modalities(root, job, cancelled=lambda: False):
    """Keep material trees pure; relocate ancillary files outside them."""
    root, job = Path(root).resolve(), Path(job).resolve()
    for modality, allowed in [('Motion', {'.json'}), ('PPG', {'.json'}), ('Video', core.VIDEO_SUFFIXES)]:
        directory = root / modality
        if not directory.is_dir():
            continue
        for path in core.walk_files(directory, cancelled):
            if path.suffix.lower() in allowed:
                continue
            relative = path.relative_to(root)
            target = root / '归类附属文件' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            counter = 0
            while target.exists():
                counter += 1
                target = target.with_name(path.name + f'.{counter}')
            core.append_journal(job / 'cleanup.jsonl', {'source': str(path), 'target': str(target), 'phase': 'intent'})
            core.move_no_replace(path, target)
            core.append_journal(job / 'cleanup.jsonl', {'source': str(path), 'target': str(target), 'phase': 'done'})
