"""One current paired dataset; small, recoverable annotation history only."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path

from .catalog import digest_file


def dataset_root(target, name):
    target = Path(target).resolve()
    return target if target.name == name else target/name


def pair_index(root):
    """Enumerate each class directory once, not once per input recording."""
    result = {}
    if not root.is_dir():
        return result
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name.startswith('.') or folder.is_symlink() or getattr(folder, 'is_junction', lambda: False)():
            continue
        for kind in ('Motion', 'PPG'):
            for label in (folder/kind/'Label').glob('*_label.json'):
                prefix = label.name.removesuffix('_label.json')
                raw = folder/kind/'Raw'/(prefix+'_raw.json')
                result.setdefault((prefix, kind), []).append((raw, label))
    return result


def existing_pairs(root, prefix, kind, index):
    result = []
    for raw, label in index.get((prefix, kind), []):
        if label.is_file() and raw.is_file() and label.resolve().is_relative_to(root) and raw.resolve().is_relative_to(root):
            document = json.loads(label.read_text(encoding='utf-8-sig'))
            if document.get('format') == 'cowmata-annotation' and document.get('dataset', {}).get('raw_sha256'):
                result.append((raw, label, document))
    return result


def inherit_reviews(item, pairs, root):
    """Human revisions in the dataset take precedence over an older farm copy.

    Recover the original affected event IDs from review backups, so deletion
    and relabeling do not resurrect events on the next incremental export.
    Newly added source events outside that reviewed set are retained.
    """
    reviewed = [(label, doc) for _, label, doc in pairs if doc.get('dataset', {}).get('review_revision')]
    if not reviewed:
        return
    document = copy.deepcopy(item['document'] or reviewed[0][1])
    project = document['work']['project']
    events = {e['id']:copy.deepcopy(e) for e in project.get('events', [])}
    labels = project['labels']
    indices = {v['code']:i for i,v in enumerate(labels)}
    histories = list(document.get('review_history', []))
    affected = set()
    revisions = []
    for _, doc in sorted(reviewed, key=lambda p: str((p[1].get('review_history') or [{}])[-1].get('at', ''))):
        if doc['work']['asset_id'] != document['work']['asset_id']:
            raise ValueError('Dataset review belongs to different original data')
        prior = doc['work']['project']
        backed_up = False
        for revision in doc.get('review_history', []):
            backup = (root/revision.get('backup', '')).resolve()
            if backup.is_relative_to(root) and backup.is_file():
                old = json.loads(backup.read_text(encoding='utf-8-sig'))
                if old.get('work', {}).get('asset_id') == document['work']['asset_id']:
                    backed_up = True
                    affected.update(e['id'] for e in old['work']['project'].get('events', []))
            if revision not in histories:
                histories.append(revision)
        if not backed_up:
            affected.update(e['id'] for e in prior.get('events', []))
        revisions.append(prior)
    for event_id in affected:
        events.pop(event_id, None)
    # Apply the union once: a filtered class copy must not erase a reviewed
    # event merely because that event is stored in another class copy.
    for prior in revisions:
        for event in prior.get('events', []):
            if event['id'] not in affected:
                continue
            event = copy.deepcopy(event)
            code = event['label_code']
            if code not in indices:
                indices[code] = len(labels)
                labels.append(copy.deepcopy(prior['labels'][event['li']]))
            event['li'] = indices[code]
            events[event['id']] = event
    project['events'] = sorted(events.values(), key=lambda e: (e['t0'], str(e['id'])))
    document['review_history'] = histories
    document.setdefault('dataset', {})['review_revision'] = hashlib.sha256(
        json.dumps(histories, sort_keys=True).encode()).hexdigest()
    item['document'] = document


def backup_label(path, history):
    payload = path.read_bytes()
    history.mkdir(parents=True, exist_ok=True)
    backup = history/(path.name+'.'+uuid.uuid4().hex+'.bak')
    with backup.open('xb') as stream:
        stream.write(payload)
    return backup


def retire_stale_pairs(pairs, current_rows, root, history):
    """Remove obsolete class copies only after replacement pairs are committed.

    A source absent from this batch is never removed. Original raw bytes still
    exist in a completed current pair with the same SHA-256.
    """
    if any(row['status'] not in {'done', 'reused'} for row in current_rows):
        return set()
    keep = {Path(r['label_target']).resolve() for r in current_rows}
    new_raw = {r['raw_sha256']:Path(r['raw_target']) for r in current_rows}
    removed = set()
    for raw, label, document in pairs:
        if label.resolve() in keep:
            continue
        sha = document['dataset']['raw_sha256']
        donor = new_raw.get(sha)
        if donor is None or donor.resolve() == raw.resolve():
            continue
        if not raw.resolve().is_relative_to(root) or not label.resolve().is_relative_to(root):
            raise ValueError('Dataset cleanup escapes its root')
        if digest_file(raw) != sha or digest_file(donor) != sha:
            raise ValueError('Original data changed during category update')
        if json.loads(label.read_text(encoding='utf-8-sig')) != document:
            raise ValueError('Annotation changed during category update')
        # Preserve the exact old label before unlinking a redundant class pair.
        backup_label(label, history)
        label.unlink()
        raw.unlink()
        removed.add(label.relative_to(root).as_posix())
    return removed
