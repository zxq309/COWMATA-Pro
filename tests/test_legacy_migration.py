import csv
import hashlib
import json

from test_resources_v34 import record


def sources(tmp_path):
    raw = record(tmp_path)
    folder = tmp_path/'labels/546C50CA07D5-21100-10'
    folder.mkdir(parents=True)
    label = folder/'one.events.csv'
    fields = ['session_id','cow_id','code','label','type','t_start_rel_ms','t_end_rel_ms']
    row = dict(session_id='one',cow_id='21100-10',code='URINATION',label='排尿',type='interval',t_start_rel_ms='0',t_end_rel_ms='40')
    with label.open('w', encoding='utf-8-sig', newline='') as stream:
        w = csv.DictWriter(stream, fieldnames=fields)
        w.writeheader()
        w.writerow(row)
    return raw, label


def test_migration_preserves_bytes_and_loadable_work(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.catalog import Catalog
    from cowmata_tailring.workspace.label_file import load_history
    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    monkeypatch.setenv('COWMATA_ACCESS_DIR', str(tmp_path/'access'))
    raw, label = sources(tmp_path)
    before = raw.read_bytes()
    plan = plan_migration([label], [raw], tmp_path/'out', category='pregnancy_late')
    assert not (tmp_path/'out').exists()
    result = execute_migration(plan)
    assert result['events'] == 1
    assert raw.read_bytes() == before
    exported = list((tmp_path/'out/标注工程/导出标注').glob('*.json'))
    assert len(exported) == 1
    history = load_history(exported[0])
    assert history.motion.sample_count == 4
    assert history.work.project.events[0].t0 == 0
    assert history.work.project.events[0].t1 == 40
    assert history.document['dataset_category'] == 'pregnancy_late'
    catalog = Catalog(tmp_path/'out')
    try:
        saved = json.loads(catalog.work_path(hashlib.sha256(before).hexdigest()).read_text(encoding='utf-8'))
        assert len(saved['project']['events']) == 1
    finally:
        catalog.close()
    assert execute_migration(plan)['events'] == 1
    assert len(list((tmp_path/'out/标注工程/导出标注').glob('*.json'))) == 1


def test_changed_source_is_rejected_after_preview(tmp_path):
    import pytest

    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    raw, label = sources(tmp_path)
    plan = plan_migration([label], [raw], tmp_path/'out', category='calving')
    label.write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='变化'):
        execute_migration(plan)


def test_completed_migration_rechecks_raw_without_overwriting_edits(tmp_path, monkeypatch):
    import pytest

    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    monkeypatch.setenv('COWMATA_ACCESS_DIR',str(tmp_path/'access'))
    raw,label = sources(tmp_path)
    plan = plan_migration([label],[raw],tmp_path/'out',category='calving')
    execute_migration(plan)
    from pathlib import Path
    Path(plan['assets'][0]['target']).write_text('damaged',encoding='utf-8')
    with pytest.raises(ValueError,match='校验'):
        execute_migration(plan)
