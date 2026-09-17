"""Classification task discovery and ancillary-file relocation."""
from __future__ import annotations

import json
from pathlib import Path

from . import organization as core
from .classification_report import LiveReport  # noqa: F401 -- compatibility import


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
