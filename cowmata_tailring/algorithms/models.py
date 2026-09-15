"""Numeric-only forests and interval proposals; no executable pickle at inference."""
from __future__ import annotations

import numpy as np


def export_forest(model, feature_names, median):
    if list(model.classes_) != [0, 1]:
        raise ValueError('Both positive and background training examples are required')
    trees = []
    for estimator in model.estimators_:
        tree = estimator.tree_
        values = tree.value[:, 0]
        prob = values[:, 1]/np.maximum(values.sum(axis=1), 1e-12)
        trees.append(dict(left=tree.children_left.tolist(), right=tree.children_right.tolist(),
            feature=tree.feature.tolist(), threshold=tree.threshold.tolist(), probability=prob.tolist()))
    return dict(schema='numeric-forest-1', features=list(feature_names), median=np.asarray(median).tolist(), trees=trees)


def predict_forest(model, x):
    x = np.asarray(x, dtype=np.float32).copy()
    if model.get('schema') != 'numeric-forest-1' or x.ndim != 2 or x.shape[1] != len(model['features']):
        raise ValueError('Incompatible algorithm feature schema')
    median = np.asarray(model['median'], dtype=np.float32)
    if median.shape != (x.shape[1],) or not np.isfinite(median).all():
        raise ValueError('Invalid model imputation values')
    bad = ~np.isfinite(x)
    x[bad] = np.broadcast_to(median, x.shape)[bad]
    trees = model['trees']
    if not 0 < len(trees) <= 256:
        raise ValueError('Invalid forest size')
    result = np.zeros(len(x))
    for tree in trees:
        left, right = np.asarray(tree['left']), np.asarray(tree['right'])
        feature, threshold = np.asarray(tree['feature']), np.asarray(tree['threshold'])
        probability = np.asarray(tree['probability'])
        n = len(left)
        if not 0 < n <= 20000 or any(len(a) != n for a in (right, feature, threshold, probability)):
            raise ValueError('Invalid tree size')
        inner = left >= 0
        if (np.any(right[~inner] != -1) or np.any(left[inner] <= np.flatnonzero(inner))
                or np.any(right[inner] <= np.flatnonzero(inner)) or np.any(left[inner] >= n)
                or np.any(right[inner] >= n) or np.any(feature[inner] < 0)
                or np.any(feature[inner] >= x.shape[1]) or not np.isfinite(threshold).all()
                or not np.isfinite(probability).all() or np.any((probability < 0) | (probability > 1))):
            raise ValueError('Invalid or cyclic numeric tree')
        node = np.zeros(len(x), dtype=int)
        while np.any(left[node] >= 0):
            active = np.flatnonzero(left[node] >= 0)
            current = node[active]
            node[active] = np.where(x[active, feature[current]] <= threshold[current], left[current], right[current])
        result += probability[node]
    return result/len(trees)


def score_events(scores, valid, *, code, threshold, duration_ms):
    scores, valid = np.asarray(scores), np.asarray(valid, dtype=bool)
    if scores.shape != valid.shape or not 0 < threshold <= 1:
        raise ValueError('Invalid event score grid')
    active = valid & np.isfinite(scores) & (scores >= threshold)
    # Fill only short, observed holes; a sensor gap always breaks a bout.
    for i in range(1, len(active)-1):
        if valid[i] and active[i-1] and active[i+1]:
            active[i] = True
    changes = np.diff(np.r_[False, active, False].astype(int))
    events = []
    for start, stop in zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)):
        if code == 'STRAINING_BOUT' and stop-start < 2:
            continue
        if code != 'STRAINING_BOUT' and stop-start > 40:
            # Long elevated runs are candidate regions; split at score peaks,
            # never present a minutes-long posture transition as a precise event.
            from scipy.signal import find_peaks
            peaks = find_peaks(scores[start:stop], distance=8, prominence=.05)[0]+start
            if not len(peaks):
                peaks = [start+int(np.argmax(scores[start:stop]))]
            parts = [(max(start, p-3), min(stop, p+4)) for p in peaks]
        else:
            parts = [(start, stop)]
        for lo, hi in parts:
            peak = lo+int(np.argmax(scores[lo:hi]))
            events.append(dict(code=code, start_ms=float(lo*1000), end_ms=min(float(hi*1000), duration_ms),
                point_ms=min((float(peak)+.5)*1000, duration_ms), score=float(scores[peak]),
                time_semantics='proposed_interval', available_ms=min(float(hi*1000)+20000, duration_ms),
                review_status='pending'))
    return events
