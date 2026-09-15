import numpy as np


def test_split_groups_follow_user_protocol_and_never_split_a_record():
    from cowmata_tailring.algorithms.training import split_groups
    records = [dict(asset_id='a', cow_id='cow1'), dict(asset_id='b', cow_id='cow1'), dict(asset_id='c', cow_id='cow2')]
    assert split_groups(records, 'STANDING_UP').tolist() == ['a', 'b', 'c']
    assert split_groups(records, 'STRAINING_BOUT').tolist() == ['cow1', 'cow1', 'cow2']


def test_training_background_remains_explicitly_weak_not_confirmed_negative():
    from cowmata_tailring.algorithms.training import training_rows
    record = dict(asset_id='a', cow_id='cow1', events=[dict(code='STANDING_UP', start_ms=10000, end_ms=14000)])
    feature = dict(X=np.ones((60, 2), dtype=np.float32), seconds=np.arange(60)+.5, valid_context=np.ones(60, dtype=bool))
    x, y, weights = training_rows([record], [feature], 'STANDING_UP')
    assert len(x) == len(y) == len(weights) and set(y) == {0, 1}
    assert weights[y == 0].sum() < weights[y == 1].sum()


def test_small_suite_saves_three_numeric_models_and_honest_metrics(tmp_path, monkeypatch):
    from cowmata_tailring.algorithms import training
    codes = ['STANDING_UP', 'LYING_DOWN', 'STRAINING_BOUT']
    records = []
    for i in range(6):
        events = [dict(code=code, start_ms=(j*30+10)*1000, end_ms=(j*30+16)*1000) for j, code in enumerate(codes)]
        records.append(dict(asset_id=str(i), cow_id=str(i), raw=str(tmp_path/f'{i}.json'), events=events, review_coverage=[]))
    def features(record, cache):
        rng = np.random.default_rng(int(record['asset_id']))
        x = rng.normal(0, .01, size=(100, 3)).astype(np.float32)
        for j in range(3):
            x[j*30+10:j*30+16, j] += 1
        return dict(X=x, names=['x','y','z'], seconds=np.arange(100)+.5,
                    valid_context=np.ones(100,dtype=bool), duration_ms=100000, observed_seconds=100)
    monkeypatch.setattr(training, 'load_features', features)
    result = training.train_suite(dict(records=records, fingerprint='synthetic-test'), tmp_path/'cache', tmp_path/'suite')
    assert len(result['models']) == 3
    import json
    report = json.loads((tmp_path/'suite/评估报告.json').read_text(encoding='utf-8'))
    assert all(m['precision'] is None and m['f1'] is None for m in report['models'])
    assert (tmp_path/'suite/training-inputs.json').is_file()
