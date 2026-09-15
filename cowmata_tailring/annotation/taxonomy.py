"""Explicit label migration, preserving event IDs, times, clocks and source bytes."""
from __future__ import annotations

import copy

from cowmata_tailring.annotation.defaults import DEFAULT_LABELS, LEGACY_DEFAULT_LABELS

SCHEMA = 'cowmata-ethogram-370'
REMOVED = {'STANDING', 'LYING', 'WALKING', 'SYNC_ANCHOR'}
ALIASES = {r['name']: r['code'] for r in LEGACY_DEFAULT_LABELS}
ALIASES.update({r['name']: r['code'] for r in DEFAULT_LABELS})
ALIASES.update({'采食': 'FEEDING', '人工辅助产犊': 'MANUAL_CALVING_ASSISTANCE'})


def event_code(event, labels):
    index = event.get('li', event.get('label_index'))
    label = labels[index] if isinstance(index, int) and 0 <= index < len(labels) else {}
    code = event.get('label_code') or event.get('code') or label.get('code') or label.get('name', '')
    return ALIASES.get(code, code), label


def canonical_code(code, category):
    code = ALIASES.get(code, code)
    if code in {'TAIL_RAISED', 'TAIL_WAGGING'} and category:
        return ('LYING_' if category == 'calving' else 'STANDING_') + code
    return code


def upgrade_document(document, *, category=None, keep_onset=True):
    result = copy.deepcopy(document)
    work = result.get('work', result)
    project = work.get('project', work)
    if not isinstance(project.get('labels'), list):
        return result
    category = category or result.get('dataset_category') or project.get('dataset_category', '')
    old = project['labels']
    labels = copy.deepcopy(DEFAULT_LABELS)
    indices = {label['code']: index for index, label in enumerate(labels)}
    changed = project.get('label_schema') != SCHEMA
    removed = []
    ceiling = max((e.get('id', 0) for e in project.get('events', []) if isinstance(e.get('id'), int)), default=0)+1

    def remap(entries, field):
        nonlocal changed
        converted = []
        for event in entries:
            old_code, definition = event_code(event, old)
            code = canonical_code(old_code, category)
            if code in REMOVED or code == 'STRAINING_ONSET' and not keep_onset:
                removed.append({'id': event.get('id'), 'code': old_code})
                changed = True
                continue
            if code not in indices:
                indices[code] = len(labels)
                labels.append({**definition, 'code': code, 'name': definition.get('name') or code,
                               'key': '', 'historical': True, 'trainable': False})
            if event.get(field) != indices[code] or event.get('label_code') != code:
                changed = True
            event[field] = indices[code]
            event['label_code'] = code
            event['layer'] = labels[indices[code]].get('layer', event.get('layer', 'objective_event'))
            converted.append(event)
        return converted

    project['events'] = remap(project.get('events', []), 'li')
    if 'drafts' in work:
        work['drafts'] = remap(work['drafts'], 'label_index')
    project['labels'] = labels
    project['protocol'] = 'v5'
    project['label_schema'] = SCHEMA
    if changed:
        project['next_event_id_floor'] = max(project.get('next_event_id_floor', 1), ceiling)
        project['label_migration'] = {'schema': SCHEMA, 'category_rule': category,
                                     'removed_count': len(removed), 'removed_event_ids': [e['id'] for e in removed],
                                     'history_preserved': True}
    return result
