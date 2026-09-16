"""Keep current annotation keys while preserving historical event identities."""
from copy import deepcopy

from cowmata_tailring.annotation.defaults import DEFAULT_LABELS, LEGACY_DEFAULT_LABELS


def current_labels(old):
    known = {x['code'] for x in LEGACY_DEFAULT_LABELS + DEFAULT_LABELS}
    labels = deepcopy(DEFAULT_LABELS)
    indices = {x['code']: i for i, x in enumerate(labels)}
    used = {x['key'].upper() for x in labels}
    mapping = {}
    for i, label in enumerate(old):
        code = label.get('code') or label.get('name', '')
        if code not in indices:
            extra = deepcopy(label)
            key = str(extra.get('key', '')).strip().upper()
            if code in known or key in used:
                key = ''
            extra['key'] = key
            if key:
                used.add(key)
            indices[code] = len(labels)
            labels.append(extra)
        mapping[i] = indices[code]
    return labels, mapping
