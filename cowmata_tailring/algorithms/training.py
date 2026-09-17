"""Shared event models with explicit weak background and group-held-out checks."""
from __future__ import annotations

import time
import warnings
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import GroupKFold, GroupShuffleSplit

from cowmata_tailring.workspace.storage import atomic_json

from . import EVENT_CODES, EVENT_TITLES, canonical_event_code, validation_unit
from .analysis import load_features, write_table
from .metrics import evaluate_events
from .models import export_forest, predict_forest, score_events

TRAINING_VERSION = 'partial-label-record-pool-1'


def split_groups(records, code):
    key = 'cow_id' if validation_unit(code) == 'cow' else 'asset_id'
    return np.asarray([str(r[key]) for r in records])


def training_rows(records, features, code):
    xx, yy, ww = [], [], []
    for record, f in zip(records, features):
        t = f['seconds']*1000
        positive = np.zeros(len(t), dtype=bool)
        protected = np.zeros(len(t), dtype=bool)
        for event in record['events']:
            if canonical_event_code(event['code']) != code:
                continue
            start = event['start_ms']
            end = event.get('end_ms') or start
            region = np.flatnonzero((t >= start-500) & (t <= end+500) & f['valid_context'])
            if len(region) > 80:
                region = region[np.linspace(0, len(region)-1, 80).astype(int)]
            positive[region] = True
            protected |= (t >= start-10000) & (t <= end+10000)
        pos = np.flatnonzero(positive)
        negative = np.flatnonzero(f['valid_context'] & ~protected)
        if len(negative) > 160:
            negative = negative[np.linspace(0, len(negative)-1, 160).astype(int)]
        selection = np.r_[pos, negative]
        if not len(selection):
            continue
        xx.append(f['X'][selection])
        yy.extend([1]*len(pos)+[0]*len(negative))
        ww.extend([1/max(1, len(pos))]*len(pos)+[.25/max(1, len(negative))]*len(negative))
    if not xx:
        raise ValueError('No usable signal windows for this event')
    x, y, weights = np.vstack(xx), np.asarray(yy), np.asarray(ww)
    if set(y) != {0, 1}:
        raise ValueError('Need both known events and weak background windows')
    weights[y == 1] /= weights[y == 1].sum()
    weights[y == 0] *= .35/weights[y == 0].sum()
    return x, y, weights


def _fit(records, features, code, seed=38):
    x, y, weights = training_rows(records, features, code)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        median = np.nanmedian(x, axis=0)
    median = np.nan_to_num(median)
    x = np.where(np.isfinite(x), x, median)
    model = RandomForestClassifier(n_estimators=64, max_depth=8, min_samples_leaf=5,
                                   max_features='sqrt', random_state=seed, n_jobs=1)
    model.fit(x, y, sample_weight=weights)
    return model, median, dict(positive_windows=int(y.sum()), weak_background_windows=int((y == 0).sum()))


def _scores(model, median, feature):
    x = np.where(np.isfinite(feature['X']), feature['X'], median)
    return model.predict_proba(x)[:, 1]


def _truth(record, feature, code, *, observable_only=False):
    truth = [dict(e, code=code) for e in record['events'] if canonical_event_code(e['code']) == code]
    if not observable_only:
        return truth
    t = feature['seconds']*1000
    return [e for e in truth if np.any(feature['valid_context'] & (t >= e['start_ms']-5000)
           & (t <= (e.get('end_ms') or e['start_ms'])+5000))]


def _threshold(model, median, records, features, code):
    data = [(r, f, _scores(model, median, f)) for r, f in zip(records, features)]
    curves = []
    for threshold in np.linspace(.2, .95, 16):
        hits, total, candidates, hours = 0, 0, 0, 0.
        for r, f, score in data:
            predictions = score_events(score, f['valid_context'], code=code,
                                       threshold=float(threshold), duration_ms=f['duration_ms'])
            truth = _truth(r, f, code, observable_only=True)
            metric = evaluate_events(truth, predictions, observed_seconds=f['observed_seconds'])
            hits += metric['matched_events']
            total += len(truth)
            candidates += len(predictions)
            hours += f['observed_seconds']/3600
        curves.append(dict(threshold=float(threshold), observable_known_recall=hits/total if total else 0.,
                           candidates_per_hour=candidates/max(hours, 1e-6)))
    good = [r for r in curves if r['observable_known_recall'] >= .85]
    best = max(good, key=lambda r: r['threshold']) if good else max(curves,
        key=lambda r: r['observable_known_recall']-.002*r['candidates_per_hour'])
    return best['threshold'], curves


def train_suite(index, cache, output, *, codes=None, modality='motion', progress=lambda *_: None, cancelled=lambda: False):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    records, features, issues = [], [], []
    codes = tuple(codes or ('STANDING_UP','LYING_DOWN','STRAINING_BOUT'))
    if not codes or any(code not in EVENT_CODES for code in codes):
        raise ValueError('Unknown behavior algorithm')
    original = [r for r in index['records'] if r['events'] and r.get('modality','motion') == modality]
    for i, record in enumerate(original):
        if cancelled():
            raise InterruptedError('Training cancelled')
        try:
            feature = load_features(record, cache)
            if feature['valid_context'].any():
                records.append(record)
                features.append(feature)
            else:
                issues.append(dict(path=record['raw'], reason='no_valid_context'))
        except (OSError, ValueError) as exc:
            issues.append(dict(path=record['raw'], reason=str(exc)))
        progress(i+1, len(original), '加载已缓存的训练特征')
    if not records:
        raise ValueError('No usable labeled records')
    metrics, fold_rows, prediction_rows, models = [], [], [], []
    for code in codes:
        selected = [i for i, r in enumerate(records) if code != 'STRAINING_BOUT' or r.get('identity_eligible', True)]
        rr, ff = [records[i] for i in selected], [features[i] for i in selected]
        groups = split_groups(rr, code)
        group_count = len(set(groups))
        if group_count < 3:
            raise ValueError('At least three independent validation groups are required')
        local, thresholds = [], []
        for fold, (train, test) in enumerate(GroupKFold(n_splits=min(5, group_count)).split(rr, groups=groups), 1):
            if cancelled():
                raise InterruptedError('Training cancelled')
            assert not set(groups[train]) & set(groups[test])
            train_r, train_f = [rr[i] for i in train], [ff[i] for i in train]
            inner_groups = groups[train]
            inner_train, inner_test = next(GroupShuffleSplit(n_splits=1, test_size=.25, random_state=38+fold).split(train_r, groups=inner_groups))
            try:
                inner_model, inner_median, _ = _fit([train_r[i] for i in inner_train], [train_f[i] for i in inner_train], code)
                threshold, _ = _threshold(inner_model, inner_median, [train_r[i] for i in inner_test], [train_f[i] for i in inner_test], code)
            except ValueError:
                threshold = .55
            thresholds.append(threshold)
            model, median, _ = _fit(train_r, train_f, code)
            for i in test:
                record, feature = rr[i], ff[i]
                predictions = score_events(_scores(model, median, feature), feature['valid_context'],
                    code=code, threshold=threshold, duration_ms=feature['duration_ms'])
                result = evaluate_events(_truth(record, feature, code), predictions,
                    observed_seconds=feature['observed_seconds'], review_coverage=record.get('review_coverage', []))
                result.pop('pairs')
                row = dict(code=code, fold=fold, asset_id=record['asset_id'], group=str(groups[i]),
                           validation_unit=validation_unit(code), threshold=threshold, **result)
                local.append(row)
                prediction_rows.extend(dict(asset_id=record['asset_id'], fold=fold,
                                            source=record['raw'], **p) for p in predictions)
            progress(fold, min(5, group_count), EVENT_TITLES[code]+'：验证未参与本次拟合的记录')
            write_table(output/'逐记录评估-实时.csv', fold_rows+local)
        fold_rows.extend(local)
        known, hit, candidates = (sum(r[k] for r in local) for k in ('known_events', 'matched_events', 'candidates'))
        seconds = sum(r['observed_seconds'] for r in local)
        summary = dict(code=code, title=EVENT_TITLES[code], validation_unit=validation_unit(code),
            validation_groups=group_count, records=len(rr), known_events=known, matched_events=hit,
            known_recall=hit/known if known else None, candidates=candidates,
            candidates_per_hour=candidates/(seconds/3600) if seconds else None,
            precision=None, f1=None, false_alarms_per_hour=None,
            reason='未完整审核，未命中既有标签的候选不能直接算误报',
            threshold=float(np.median(thresholds)), score_is_probability=False)
        model, median, fit_stats = _fit(rr, ff, code)
        summary['feature_importance'] = dict(zip(ff[0]['names'],[float(v) for v in model.feature_importances_]))
        summary['fold_thresholds'] = thresholds
        payload = export_forest(model, ff[0]['names'], median)
        # Ensure the numeric-only deployment produces the same scores as training.
        check = ff[0]['X'][:200]
        assert np.allclose(predict_forest(payload, check), model.predict_proba(np.where(np.isfinite(check), check, median))[:, 1], atol=1e-6)
        payload.update(code=code, title=EVENT_TITLES[code], threshold=summary['threshold'],
            feature_version=ff[0]['feature_version'], modality=modality, training_version=TRAINING_VERSION,
            dataset_fingerprint=index['fingerprint'], score_is_probability=False,
            background_policy='unreviewed_weak_background_weight_0.35', fit_stats=fit_stats)
        name=code.lower()+'.json'
        atomic_json(output/name, payload)
        models.append(dict(code=code, title=EVENT_TITLES[code], file=name, modality=modality, threshold=payload['threshold']))
        metrics.append(summary)
        write_table(output/'事件识别评估.csv', metrics)
        atomic_json(output/'评估报告.json', dict(models=metrics, validation_records=fold_rows, settings=dict(n_estimators=64,max_depth=8,min_samples_leaf=5,seed=38,modality=modality,codes=codes), issues=issues, elapsed_seconds=time.monotonic()-started))
    write_table(output/'逐记录评估-实时.csv', fold_rows)
    write_table(output/'留出记录候选.csv', prediction_rows)
    write_table(output/'训练问题.csv', issues, ['path', 'reason'])
    # This snapshot is small: labels, references and fingerprints; no raw copies.
    atomic_json(output/'training-inputs.json', index)
    import hashlib
    for model in models:
        model['sha256'] = hashlib.sha256((output/model['file']).read_bytes()).hexdigest()
    from cowmata_tailring.workspace.shared_labels import CONTRACT as SHARED_LABEL_CONTRACT
    manifest = dict(shared_label_contract=dict(SHARED_LABEL_CONTRACT), schema='cowmata-event-suite-1', version=output.name, feature_version=features[0]['feature_version'], modality=modality,
        training_version=TRAINING_VERSION, dataset_fingerprint=index['fingerprint'], models=models,
        report='评估报告.json', complete=True, elapsed_seconds=time.monotonic()-started)
    atomic_json(output/'suite.json', manifest)
    return manifest
