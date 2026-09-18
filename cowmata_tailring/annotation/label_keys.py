"""Current annotation choices; legacy-only records remain reviewable, not trainable."""
from copy import deepcopy

from cowmata_tailring.annotation.defaults import DEFAULT_LABELS
from cowmata_tailring.annotation.taxonomy import SCHEMA, canonical_code


def current_labels(old, category=None):
    labels = deepcopy(DEFAULT_LABELS)
    indices = {x['code']: i for i, x in enumerate(labels)}
    mapping = {}
    for i, label in enumerate(old):
        original = label.get('code') or label.get('name', '')
        code = canonical_code(original, category)
        if code not in indices:
            extra = deepcopy(label)
            extra.update(code=code, key='', historical=True, trainable=False)
            indices[code] = len(labels)
            labels.append(extra)
        mapping[i] = indices[code]
    return labels, mapping


def normalize_work(data):
    """Normalize a copy, leaving original label files and time coordinates intact."""
    result = deepcopy(data)
    project = result['project']
    old = project.get('labels', [])
    labels, mapping = current_labels(old, project.get('dataset_category'))
    from cowmata_tailring.annotation.core import Label
    canonical = [Label.from_dict(label).to_dict() for label in labels]
    if old == canonical:
        return result
    for entries, field in ((project.get('events', []), 'li'), (result.get('drafts', []), 'label_index')):
        for event in entries:
            index = event.get(field)
            if index not in mapping:
                continue
            event[field] = mapping[index]
            code = labels[mapping[index]]['code']
            event['label_code'] = code
            event['layer'] = labels[mapping[index]].get('layer', 'objective_event')
            original = old[index].get('code') or old[index].get('name', '')
            if original != code:
                event.setdefault('original_label_code', original)
    project['labels'] = labels
    project['label_schema'] = SCHEMA
    return result
