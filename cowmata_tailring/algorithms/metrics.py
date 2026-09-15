"""Event matching separates known-label recall from reviewed precision."""
from __future__ import annotations

import numpy as np
from scipy.optimize import linear_sum_assignment


def _range(event):
    a = float(event.get('start_ms', event.get('point_ms', 0)))
    b = event.get('end_ms')
    return a, a if b is None else float(b)


def evaluate_events(truth, predictions, *, observed_seconds, review_coverage=(), tolerance_ms=5000):
    cost = np.full((len(truth), len(predictions)), 1e6)
    for i, target in enumerate(truth):
        a, b = _range(target)
        for j, prediction in enumerate(predictions):
            c, d = _range(prediction)
            point = float(prediction.get('point_ms', (c+d)/2))
            overlap = max(0., min(b, d)-max(a, c))
            union = max(b, d)-min(a, c)
            if a-tolerance_ms <= point <= b+tolerance_ms or overlap > 0 and overlap/max(union, 1) >= .1:
                cost[i, j] = 1.-overlap/max(union, 1) + min(abs(point-(a+b)/2), 100000)/1e6
    pairs = []
    if cost.size:
        aa, bb = linear_sum_assignment(cost)
        pairs = [(int(a), int(b)) for a, b in zip(aa, bb) if cost[a, b] < 1e6]
    matched = {b for _, b in pairs}
    def covered(event):
        a, b = _range(event)
        return any(float(left) <= a and max(a+1, b) <= float(r) for left, r in review_coverage)
    reviewed_predictions = {j for j, p in enumerate(predictions) if covered(p)}
    reviewed_truth = {i for i, e in enumerate(truth) if covered(e)}
    fp = len(reviewed_predictions-matched)
    tp_reviewed = sum(i in reviewed_truth and j in reviewed_predictions for i, j in pairs)
    fn_reviewed = len(reviewed_truth)-sum(i in reviewed_truth for i, _ in pairs)
    precision = tp_reviewed/(tp_reviewed+fp) if tp_reviewed+fp else None
    recall_reviewed = tp_reviewed/(tp_reviewed+fn_reviewed) if tp_reviewed+fn_reviewed else None
    f1 = 2*precision*recall_reviewed/(precision+recall_reviewed) if precision is not None and recall_reviewed is not None and precision+recall_reviewed else None
    reviewed_seconds = sum(max(0., float(b)-float(a))/1000 for a, b in review_coverage)
    errors = [abs(_range(predictions[j])[0]-_range(truth[i])[0])/1000 for i, j in pairs]
    ends = [abs(_range(predictions[j])[1]-_range(truth[i])[1])/1000 for i, j in pairs if truth[i].get('end_ms') is not None and predictions[j].get('end_ms') is not None]
    return dict(known_events=len(truth), candidates=len(predictions), matched_events=len(pairs),
        known_recall=len(pairs)/len(truth) if truth else None,
        precision=precision, f1=f1, false_positives=fp if review_coverage else None,
        false_alarms_per_hour=fp/(reviewed_seconds/3600) if reviewed_seconds else None,
        unverified_candidates=len(set(range(len(predictions)))-matched-reviewed_predictions),
        candidate_rate_per_hour=len(predictions)/(observed_seconds/3600) if observed_seconds > 0 else None,
        start_error_median_s=float(np.median(errors)) if errors else None,
        end_error_median_s=float(np.median(ends)) if ends else None,
        reviewed_seconds=reviewed_seconds, observed_seconds=float(observed_seconds), pairs=pairs,
        evaluation_scope='reviewed_intervals_only' if review_coverage else 'partial_labels_known_recall_only')
