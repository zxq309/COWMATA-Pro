"""Keep current annotation keys while preserving historical event identities."""
from copy import deepcopy

from cowmata_tailring.annotation.defaults import DEFAULT_LABELS

LEGACY_KEYS = {
    "STANDING": "G", "LYING": "H", "WALKING": "I",
    "STRAINING_ONSET": "J", "TAIL_RAISED": "Q", "TAIL_WAGGING": "W",
    "SYNC_ANCHOR": "Ctrl+Alt+0",
}


def current_labels(old):
    labels = deepcopy(DEFAULT_LABELS)
    indices = {x['code']: i for i, x in enumerate(labels)}
    used = {x['key'].upper() for x in labels}
    reserved = {key.upper() for code, key in LEGACY_KEYS.items() if any(x.get('code') == code for x in old)}
    mapping = {}
    for i, label in enumerate(old):
        code = label.get('code') or label.get('name', '')
        if code not in indices:
            extra = deepcopy(label)
            key = LEGACY_KEYS.get(code, str(extra.get('key', '')).strip().upper())
            if code not in LEGACY_KEYS and (key.upper() in used or key.upper() in reserved):
                key = ''
            extra['key'] = key
            if key:
                used.add(key)
            indices[code] = len(labels)
            labels.append(extra)
        mapping[i] = indices[code]
    return labels, mapping
