import hashlib
import json

import numpy as np
import pytest


def pair(root, folder, events, *, cow='001', raw=b'{"version":2}', reviewed=None):
    dest = root/folder/'Motion'
    (dest/'Raw').mkdir(parents=True, exist_ok=True)
    (dest/'Label').mkdir(exist_ok=True)
    stem = f'DEV-{cow}-A_2026-08-20_00-00-00'
    (dest/'Raw'/(stem+'_raw.json')).write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    doc = {'work': {'asset_id': sha, 'project': {'cow_id': cow, 'events': events,
            'device_identity': {'device_id': 'DEV', 'cow_id': cow, 'field_mark': 'A'}}},
           'dataset': {'raw_sha256': sha, 'pending_annotation': not events}}
    if reviewed is not None:
        doc['review_coverage'] = reviewed
    label = dest/'Label'/(stem+'_label.json')
    label.write_text(json.dumps(doc), encoding='utf-8')
    return label


def test_algorithm_index_merges_class_copies_without_inventing_review_coverage(tmp_path):
    from cowmata_tailring.algorithms.dataset import scan_dataset
    e = {'id': 4, 'label_code': 'STANDING_UP', 't0': 12000, 't1': 15000}
    pair(tmp_path, 'Standup', [e])
    pair(tmp_path, 'Other', [e, {'id': 9, 'label_code': 'LYING_DOWN', 't0': 22000, 't1': 25000}])
    result = scan_dataset(tmp_path)
    assert len(result['records']) == 1
    assert len(result['records'][0]['events']) == 2
    assert result['records'][0]['review_coverage'] == []
    assert result['summary']['events']['STANDING_UP'] == 1


def test_algorithm_index_excludes_conflicting_animal_identity(tmp_path):
    from cowmata_tailring.algorithms.dataset import scan_dataset
    pair(tmp_path, 'A', [], cow='001')
    pair(tmp_path, 'B', [], cow='002')
    result = scan_dataset(tmp_path)
    assert not result['records']
    assert result['issues'][0]['reason'] == 'conflicting_cow_identity'


def test_general_pool_can_use_source_verified_events_without_assigning_conflicting_cows(tmp_path):
    from cowmata_tailring.algorithms.dataset import scan_dataset
    events = [{'id': 1, 'label_code': 'STANDING_UP', 't0': 10000, 't1': 14000},
              {'id': 2, 'label_code': 'STRAINING_BOUT', 't0': 20000, 't1': 30000}]
    pair(tmp_path, 'A', events, cow='001')
    pair(tmp_path, 'B', events, cow='002')
    result = scan_dataset(tmp_path, pool_general_events=True)
    assert len(result['records']) == 1
    record = result['records'][0]
    assert record['identity_eligible'] is False
    assert [e['code'] for e in record['events']] == ['STANDING_UP']
    assert record['split_group'] == record['asset_id']


def test_partial_labels_cannot_create_precision_false_alarm_or_f1():
    from cowmata_tailring.algorithms.metrics import evaluate_events
    truth = [{'start_ms': 10000, 'end_ms': 15000}]
    predictions = [{'start_ms': 11000, 'end_ms': 15000}, {'start_ms': 60000, 'end_ms': 64000}]
    result = evaluate_events(truth, predictions, observed_seconds=100)
    assert result['known_recall'] == 1
    assert result['matched_events'] == 1 and result['unverified_candidates'] == 1
    assert result['precision'] is None and result['f1'] is None and result['false_alarms_per_hour'] is None
    verified = evaluate_events(truth, predictions, observed_seconds=100, review_coverage=[[0, 100000]])
    assert verified['precision'] == .5 and verified['false_positives'] == 1


def test_event_matching_is_one_to_one():
    from cowmata_tailring.algorithms.metrics import evaluate_events
    result = evaluate_events([{'start_ms': 10000, 'end_ms': 15000}],
        [{'start_ms': 10000, 'end_ms': 15000}, {'start_ms': 10000, 'end_ms': 15000}], observed_seconds=30)
    assert result['matched_events'] == 1 and result['unverified_candidates'] == 1


def test_posture_occupancy_retains_unknown_start_gaps_and_transition_time():
    from cowmata_tailring.algorithms.posture import occupancy
    events = [{'code': 'LYING_DOWN', 'start_ms': 10000, 'end_ms': 15000},
              {'code': 'STANDING_UP', 'start_ms': 30000, 'end_ms': 35000}]
    result = occupancy(events, 0, 60000, observed_intervals=[[0, 40000], [50000, 60000]])
    assert result['lying_seconds'] == 15
    assert result['standing_seconds'] == 5
    assert result['transition_seconds'] == 10
    assert result['unknown_seconds'] == 20
    assert result['missing_seconds'] == 10
    assert result['lying_fraction_known'] == .75
    assert result['known_posture_coverage'] == .4
    assert result['lying_fraction_lower'] == .25
    assert result['lying_fraction_upper'] == pytest.approx(45/60)


def test_same_direction_transition_does_not_silently_reset_or_fill_the_interval():
    from cowmata_tailring.algorithms.posture import occupancy
    events = [{'code': 'LYING_DOWN', 'start_ms': 10000, 'end_ms': 15000},
              {'code': 'LYING_DOWN', 'start_ms': 30000, 'end_ms': 35000}]
    result = occupancy(events, 0, 60000, observed_intervals=[[0, 60000]])
    assert result['conflicts'] == 1
    assert result['unknown_seconds'] >= 25


def test_signal_features_never_bridge_missing_seconds():
    from cowmata_tailring.algorithms.features import second_features
    times = np.r_[np.arange(0, 5000, 20), np.arange(20000, 25000, 20)]
    acc = np.tile([0., 0., 1.], (len(times), 1))
    gyro = np.zeros_like(acc)
    f = second_features(times, acc, gyro)
    assert not f['valid'][5:20].any()
    assert not f['valid_context'][4:21].any()
    assert np.isnan(f['X'][8]).all()


def test_long_context_stops_at_the_same_gap_as_short_context():
    from cowmata_tailring.algorithms.features import second_features
    times = np.r_[np.arange(0, 20000, 20), np.arange(22000, 60000, 20)]
    acc = np.tile([0., 0., 1.], (len(times), 1))
    gyro = np.zeros_like(acc)
    gyro[times >= 22000, 0] = 100
    f = second_features(times, acc, gyro)
    assert f['valid_context'][34]
    assert f['X'][34, f['names'].index('long_gyro')] == pytest.approx(100)


def test_feature_cache_rejects_a_changed_original_before_using_saved_features(tmp_path):
    from cowmata_tailring.algorithms.analysis import load_features
    raw = tmp_path/'raw.json'
    raw.write_bytes(b'{"version":2}')
    record = dict(raw=str(raw), asset_id='0'*64)
    with pytest.raises(ValueError, match='identity'):
        load_features(record, tmp_path/'cache')


def test_portable_forest_predictions_match_fitted_model_without_pickle():
    from sklearn.ensemble import RandomForestClassifier

    from cowmata_tailring.algorithms.models import export_forest, predict_forest
    rng = np.random.default_rng(3)
    x = rng.normal(size=(100, 3))
    y = (x[:, 0]+x[:, 1] > 0).astype(int)
    model = RandomForestClassifier(n_estimators=8, max_depth=4, random_state=3).fit(x, y)
    saved = export_forest(model, ['a', 'b', 'c'], np.zeros(3))
    saved = json.loads(json.dumps(saved, allow_nan=False))
    assert np.allclose(predict_forest(saved, x), model.predict_proba(x)[:, 1])


def test_prediction_intervals_do_not_join_across_a_signal_gap():
    from cowmata_tailring.algorithms.models import score_events
    score = np.array([0., 0., .9, .9, 0., .9, .9, .9, 0., 0.])
    valid = np.ones(10, dtype=bool)
    valid[4] = False
    events = score_events(score, valid, code='STRAINING_BOUT', threshold=.5, duration_ms=10000)
    assert len(events) == 2
    assert events[0]['end_ms'] <= 4000 and events[1]['start_ms'] >= 5000


def test_user_grouping_protocol_retains_individual_reproductive_events():
    from cowmata_tailring.algorithms import validation_unit
    assert validation_unit('STANDING_UP') == 'record'
    assert validation_unit('LYING_DOWN') == 'record'
    assert validation_unit('STRAINING_BOUT') == 'cow'
    assert validation_unit('MOUNTING') == 'cow'
    assert validation_unit('MANUAL_CALVING_ASSISTANCE') == 'cow'
def test_overlapping_transitions_conserve_time_and_reset_posture():
    from cowmata_tailring.algorithms.posture import occupancy
    result = occupancy([
        dict(code='LYING_DOWN', start_ms=1000, end_ms=3000),
        dict(code='STANDING_UP', start_ms=2000, end_ms=4000),
    ], 0, 10000, observed_intervals=[(0,10000)])
    assert sum(result[k] for k in ('lying_seconds','standing_seconds','transition_seconds','unknown_seconds')) == 10
    assert result['unknown_seconds'] == 10
    assert result['lying_fraction_known'] is None
