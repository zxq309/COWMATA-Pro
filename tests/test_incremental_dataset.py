import json
from pathlib import Path

from test_paired_dataset_v370 import fixture_farm

from cowmata_tailring.workspace import paired_dataset as d


def test_current_dataset_is_flat_reused_and_grows(tmp_path):
    farm, target = tmp_path/'farm', tmp_path/'datasets'
    fixture_farm(farm)
    first = d.build_dataset([farm], target, job=tmp_path/'one', layout='current')
    root = target/'COWMATA_Behavior_Dataset'
    assert Path(first['root']) == root
    stamps = {str(p):p.stat().st_mtime_ns for p in root.rglob('*_raw.json')}
    second = d.build_dataset([farm], root, job=tmp_path/'two', layout='current')
    assert Path(second['root']) == root and second['counts']['reused'] == 2
    assert {str(p):p.stat().st_mtime_ns for p in root.rglob('*_raw.json')} == stamps
    other = tmp_path/'other'
    _, label = fixture_farm(other)
    # A separate task/subset never removes existing entries.
    data = json.loads(label.read_text(encoding='utf-8'))
    data['work']['project']['events'] = data['work']['project']['events'][:1]
    label.write_text(json.dumps(data), encoding='utf-8')
    third = d.build_dataset([other], target, job=tmp_path/'three', layout='current')
    assert Path(third['root']) == root
    assert first['root'] == third['root']


def test_changed_annotation_updates_current_pair_and_keeps_history(tmp_path):
    farm, target = tmp_path/'farm', tmp_path/'datasets'
    _, label = fixture_farm(farm)
    first = d.build_dataset([farm], target, job=tmp_path/'one', layout='current')
    root = Path(first['root'])
    paired = next((root/'Standup/Motion/Label').glob('*.json'))
    old = paired.read_bytes()
    doc = json.loads(label.read_text(encoding='utf-8'))
    doc['work']['project']['events'][0]['t0'] = 20
    label.write_text(json.dumps(doc), encoding='utf-8')
    result = d.build_dataset([farm], target, job=tmp_path/'two', layout='current')
    assert result['counts']['errors'] == 0
    assert json.loads(paired.read_text(encoding='utf-8'))['work']['project']['events'][0]['t0'] == 20
    assert any(p.read_bytes() == old for p in (root/'.dataset-history').rglob('*.bak'))


def test_relabel_removes_stale_category_pair_but_keeps_raw_and_review(tmp_path):
    import hashlib

    from cowmata_tailring.workspace.review_store import save_review
    farm, target = tmp_path/'farm', tmp_path/'datasets'
    raw, _ = fixture_farm(farm)
    first = d.build_dataset([farm], target, job=tmp_path/'one', layout='current')
    root = Path(first['root'])
    label = next((root/'Standup/Motion/Label').glob('*.json'))
    doc = json.loads(label.read_text(encoding='utf-8'))
    li = next(i for i, v in enumerate(doc['work']['project']['labels']) if v['code'] == 'STRAINING_BOUT')
    doc['work']['project']['events'][0].update(li=li, label_code='STRAINING_BOUT')
    save_review(label, doc['work'], expected_sha=hashlib.sha256(label.read_bytes()).hexdigest())
    result = d.build_dataset([farm], target, job=tmp_path/'two', layout='current')
    assert result['counts']['errors'] == 0
    out = next((root/'Straining/Motion/Label').glob('*.json'))
    assert {e['id'] for e in json.loads(out.read_text(encoding='utf-8'))['work']['project']['events']} == {11,27}
    assert not label.exists()
    assert next((root/'Straining/Motion/Raw').glob('*.json')).read_bytes() == raw.read_bytes()
    assert raw.exists()


def test_repeated_export_preserves_review_without_masking_new_unreviewed_events(tmp_path):
    import hashlib

    from cowmata_tailring.workspace.review_store import save_review
    farm, target = tmp_path/'farm', tmp_path/'datasets'
    _, source_label = fixture_farm(farm)
    first = d.build_dataset([farm], target, job=tmp_path/'one')
    root = Path(first['root'])
    label = next((root/'Standup/Motion/Label').glob('*.json'))
    doc = json.loads(label.read_text(encoding='utf-8'))
    doc['work']['project']['events'][0]['t0'] = 22
    save_review(label, doc['work'], expected_sha=hashlib.sha256(label.read_bytes()).hexdigest())
    d.build_dataset([farm], target, job=tmp_path/'two')
    source = json.loads(source_label.read_text(encoding='utf-8'))
    source['work']['project']['events'][1]['t0'] = 90
    source_label.write_text(json.dumps(source), encoding='utf-8')
    d.build_dataset([farm], target, job=tmp_path/'three')
    stand = json.loads(label.read_text(encoding='utf-8'))['work']['project']['events']
    strain = json.loads(next((root/'Straining/Motion/Label').glob('*.json')).read_text(encoding='utf-8'))['work']['project']['events']
    assert [(e['id'],e['t0']) for e in stand] == [(11,22)]
    assert [(e['id'],e['t0']) for e in strain] == [(27,90)]
