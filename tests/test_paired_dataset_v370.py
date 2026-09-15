import base64
import hashlib
import json
from pathlib import Path


def fixture_farm(root):
    p=root/'产犊/Motion/2026-08-21/ABCDEF123456-10001-A/2026-08-21_01-06-47.json'
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(dict(device='ABCDEF123456',create_time=1787245607000,version=0,imu=base64.b64encode(bytes(18*10)).decode())),encoding='utf-8')
    label=root/'产犊/标注工程'/p.relative_to(root/'产犊')
    label=label.with_name(label.stem+'.标注.json')
    label.parent.mkdir(parents=True)
    doc=dict(format='cowmata-annotation',version=1,coordinates='parent_imu_ms',dataset_category='calving',
        source=dict(asset_id=hashlib.sha256(p.read_bytes()).hexdigest(),path=p.relative_to(root/'产犊').as_posix()),view=dict(start_ms=0,end_ms=180),
        work=dict(asset_id=hashlib.sha256(p.read_bytes()).hexdigest(),clock={},drafts=[],project=dict(cow_id='10001',source={},
            labels=[dict(name='起立过程',code='STANDING_UP',type='interval'),dict(name='努责区间',code='STRAINING_BOUT',type='interval')],
            events=[dict(id=11,li=0,label_code='STANDING_UP',t0=10,t1=60),dict(id=27,li=1,label_code='STRAINING_BOUT',t0=70,t1=150)])))
    label.write_text(json.dumps(doc),encoding='utf-8')
    return p,label


def test_dataset_uses_raw_label_pairs_and_timestamp_versions(tmp_path):
    from cowmata_tailring.workspace import paired_dataset as d
    raw,label=fixture_farm(tmp_path/'farm')
    original=label.read_bytes()
    result=d.build_dataset([tmp_path/'farm'],tmp_path/'datasets','behavior',job=tmp_path/'job',layout='versioned')
    root=Path(result['root'])
    assert root.parent.name=='COWMATA_Behavior_Dataset'
    prefix='ABCDEF123456-10001-A_2026-08-21_01-06-47'
    for folder,code,event_id in [('Standup','STANDING_UP',11),('Straining','STRAINING_BOUT',27)]:
        assert (root/folder/'Motion/Raw'/f'{prefix}_raw.json').read_bytes()==raw.read_bytes()
        doc=json.loads((root/folder/'Motion/Label'/f'{prefix}_label.json').read_text(encoding='utf-8'))
        assert [e['id'] for e in doc['work']['project']['events']]==[event_id]
        assert doc['work']['project']['events'][0]['label_code']==code
    assert label.read_bytes()==original
    second=d.build_dataset([tmp_path/'farm'],tmp_path/'datasets','behavior',job=tmp_path/'job2',layout='versioned')
    assert second['root']!=result['root'] and root.exists()


def test_calving_task_has_only_the_five_requested_classes(tmp_path):
    from cowmata_tailring.workspace import paired_dataset as d
    fixture_farm(tmp_path/'farm')
    result=d.build_dataset([tmp_path/'farm'],tmp_path/'datasets','calving',job=tmp_path/'job',layout='versioned')
    root=Path(result['root'])
    assert root.parent.name=='COWMATA_CalvingPred_Dataset'
    assert {'Standup','Liedown','Straining','FetalPartFirstVisible','CalfFullyExpelled'} <= {p.name for p in root.iterdir() if p.is_dir()}
    assert not (root/'StandingTailRaised').exists()


def test_dataset_resume_reuses_completed_pairs(tmp_path):
    from cowmata_tailring.workspace import paired_dataset as d
    fixture_farm(tmp_path/'farm')
    options=dict(job=tmp_path/'job')
    first=d.build_dataset([tmp_path/'farm'],tmp_path/'datasets','behavior',**options)
    before={p:str(p.stat().st_mtime_ns) for p in Path(first['root']).rglob('*_raw.json')}
    again=d.build_dataset([tmp_path/'farm'],tmp_path/'datasets','behavior',**options)
    assert again['root']==first['root']
    assert all(str(p.stat().st_mtime_ns)==stamp for p,stamp in before.items())
    assert again['counts']['reused']==2


def test_reviewed_dataset_rebuild_merges_labels_without_overwriting(tmp_path):
    from cowmata_tailring.workspace import paired_dataset as d
    from cowmata_tailring.workspace.review_store import save_review
    fixture_farm(tmp_path/'farm')
    first=d.build_dataset([tmp_path/'farm'],tmp_path/'datasets','behavior',job=tmp_path/'job1',layout='versioned')
    root=Path(first['root'])
    label=next((root/'Standup/Motion/Label').glob('*.json'))
    doc=json.loads(label.read_text(encoding='utf-8'))
    index=next(i for i,r in enumerate(doc['work']['project']['labels']) if r['code']=='STRAINING_BOUT')
    doc['work']['project']['events'][0].update(li=index,label_code='STRAINING_BOUT')
    save_review(label,doc['work'],expected_sha=hashlib.sha256(label.read_bytes()).hexdigest())
    second=d.build_dataset([root],tmp_path/'datasets','behavior',job=tmp_path/'job2',layout='versioned')
    result=json.loads(next((Path(second['root'])/'Straining/Motion/Label').glob('*.json')).read_text(encoding='utf-8'))
    assert {e['id'] for e in result['work']['project']['events']}=={11,27}


def test_resume_rejects_changed_source_labels_without_modifying_version(tmp_path):
    import pytest

    from cowmata_tailring.workspace import paired_dataset as d
    _,label=fixture_farm(tmp_path/'farm')
    first=d.build_dataset([tmp_path/'farm'],tmp_path/'datasets',job=tmp_path/'job')
    version=Path(first['root'])
    before={str(p):p.read_bytes() for p in version.rglob('*_label.json')}
    doc=json.loads(label.read_text(encoding='utf-8'))
    doc['work']['project']['events'][0]['t0']=12
    label.write_text(json.dumps(doc),encoding='utf-8')
    with pytest.raises(ValueError,match='新的版本'):
        d.build_dataset([tmp_path/'farm'],tmp_path/'datasets',job=tmp_path/'job')
    assert before=={str(p):p.read_bytes() for p in version.rglob('*_label.json')}
