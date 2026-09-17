import json

from test_legacy_migration import sources


def test_dataset_uses_mother_coordinates_and_does_not_create_negatives(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    from cowmata_tailring.workspace.mother_dataset import export_dataset, read_dataset
    monkeypatch.setenv('COWMATA_ACCESS_DIR', str(tmp_path/'access'))
    raw, label = sources(tmp_path)
    execute_migration(plan_migration([label],[raw],tmp_path/'project',category='pregnancy_late'))
    result = export_dataset([tmp_path/'project/标注工程/导出标注',tmp_path/'project/标注工程/导出标注'],tmp_path/'dataset')
    assert result['positive_events'] == 1
    assert result['negative_events'] == 0
    dataset = read_dataset(tmp_path/'dataset')
    assert dataset['events'][0]['start_ms'] == 0
    assert dataset['events'][0]['end_ms'] == 40
    assert dataset['events'][0]['cow_id'] == '21100'
    assert dataset['events'][0]['dataset_category'] == 'pregnancy_late'
    assert dataset['manifest']['raw_resampling'] == 'none'
    assert len(dataset['sources']) == 1


def test_conflicting_legacy_identity_is_not_training_truth(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    from cowmata_tailring.workspace.mother_dataset import export_dataset, read_dataset
    monkeypatch.setenv('COWMATA_ACCESS_DIR', str(tmp_path/'access'))
    raw, label = sources(tmp_path)
    label.write_text(label.read_text(encoding='utf-8-sig').replace('21100-10','24178-11'),encoding='utf-8-sig')
    execute_migration(plan_migration([label],[raw],tmp_path/'project',category='pregnancy_late'))
    result = export_dataset(tmp_path/'project/标注工程/导出标注',tmp_path/'dataset')
    assert result['positive_events'] == 0
    assert result['review_events'] == 1
    assert not read_dataset(tmp_path/'dataset')['events'][0]['training_eligible']


def test_splits_are_by_cow_and_identical_for_all_event_heads():
    from cowmata_tailring.workspace.mother_dataset import cow_splits
    result = cow_splits(['21100','24178','23335','21100'])
    assert len(result) == 3
    assert set(result.values()) == {'train','validation','test'}
    assert result == cow_splits(['23335','24178','21100'])


def test_dataset_reader_detects_changed_annotation_table(tmp_path):
    import pytest

    from cowmata_tailring.workspace.mother_dataset import read_dataset
    root = tmp_path/'data'
    root.mkdir()
    (root/'dataset-manifest.json').write_text(json.dumps({'schema':'cowmata-mother-dataset-1','files':[{'path':'events.jsonl','sha256':'0'*64}]}))
    (root/'events.jsonl').write_text('{}\n')
    with pytest.raises(ValueError, match='校验'):
        read_dataset(root)


def test_project_export_reads_latest_saved_work_not_stale_snapshot(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    from cowmata_tailring.workspace.mother_dataset import export_dataset, read_dataset
    monkeypatch.setenv('COWMATA_ACCESS_DIR', str(tmp_path/'access'))
    raw, label = sources(tmp_path)
    project = tmp_path/'project'
    execute_migration(plan_migration([label],[raw],project,category='pregnancy_late'))
    saved = next((project/'标注工程/annotations').glob('*.json'))
    work = json.loads(saved.read_text(encoding='utf-8'))
    work['project']['events'][0]['t1'] = 50
    saved.write_text(json.dumps(work),encoding='utf-8')
    export_dataset(project,tmp_path/'data')
    assert [e['end_ms'] for e in read_dataset(tmp_path/'data')['events']] == [50]


def test_deleted_events_do_not_return_from_old_export(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    from cowmata_tailring.workspace.mother_dataset import _documents
    monkeypatch.setenv('COWMATA_ACCESS_DIR', str(tmp_path/'access'))
    raw, label = sources(tmp_path)
    project = tmp_path/'project'
    execute_migration(plan_migration([label],[raw],project,category='pregnancy_late'))
    saved = next((project/'标注工程/annotations').glob('*.json'))
    work = json.loads(saved.read_text(encoding='utf-8'))
    work['project']['events'] = []
    saved.write_text(json.dumps(work),encoding='utf-8')
    assert list(_documents(project)) == []
