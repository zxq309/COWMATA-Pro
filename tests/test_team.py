import base64
import copy
import json
import struct
from pathlib import Path

import pytest

from cowmata_tailring.annotation.data import load_motion_json
from cowmata_tailring.workspace.catalog import Catalog, digest_file
from cowmata_tailring.workspace.label_file import build_label_file, save_label_file
from cowmata_tailring.workspace.storage import atomic_json, read_json
from cowmata_tailring.workspace.team import restore_label, return_folder_snapshot
from cowmata_tailring.workspace.work import SessionWork


@pytest.fixture
def returned(tmp_path):
    root = tmp_path/'project'
    root.mkdir()
    raw = root/'001.json'
    frames = b''.join(struct.pack('<I9h',t,*([1]*9)) for t in range(0,1001,20))
    atomic_json(raw, {'version':2,'device':'A','create_time':1785732300000,'imu':base64.b64encode(frames).decode()})
    motion = load_motion_json(raw)
    work = SessionWork(digest_file(raw))
    work.project.cow_id = 'COW-TEST'
    work.project.add_event(0,100,300)
    work.progress = {'status':'done','imu_ms':1000}
    catalog = Catalog(root, stability_seconds=0)
    catalog.scan(now=1)
    catalog.index_one(raw.name,lambda *_:{'duration_ms':motion.duration_ms},now=2)
    doc = build_label_file(work,motion,root,catalog.rows(),{})
    out = tmp_path/'inbox'/'person-A'/'result.cowmata.json'
    save_label_file(out,doc)
    yield catalog,raw,work,doc,out
    catalog.close()


def test_restore_and_idempotence_without_raw_or_video_writes(returned):
    cat,raw,work,doc,out=returned
    before=digest_file(raw)
    result=restore_label(cat,out,require_done=True)
    assert result['status']=='imported'
    assert read_json(cat.work_path(work.asset_id))==work.to_dict()
    assert result['progress']['status']=='done'
    assert restore_label(cat,out)['status']=='unchanged'
    assert digest_file(raw)==before
    assert list(cat.root.glob('*.json'))==[raw]


def test_conflict_preserves_local_and_archives_return(returned):
    cat,raw,work,doc,out=returned
    local=copy.deepcopy(work.to_dict())
    local['project']['events'][0]['t0']=120
    atomic_json(cat.work_path(work.asset_id),local)
    result=restore_label(cat,out)
    assert result['status']=='conflict' and result['work'] is None
    assert read_json(cat.work_path(work.asset_id))==local
    assert read_json(Path(result['archive']))==doc


def test_handback_matches_content_not_relative_name(returned):
    cat,raw,work,doc,out=returned
    doc['source']['path']='a-different-device/001.json'
    atomic_json(out,doc)
    assert restore_label(cat,out)['path']==raw.name


@pytest.mark.parametrize('change', ['payload','identity','partial','ongoing','source_replaced'])
def test_bad_or_incomplete_returns_never_mark_complete(returned,change):
    cat,raw,work,doc,out=returned
    if change=='payload':
        doc['embedded_imu']['original_json_base64']=base64.b64encode(b'{}').decode()
    elif change=='identity':
        doc['embedded_imu']['sha256']='0'*64
    elif change=='partial':
        doc['view']['start_ms']=100
    elif change=='ongoing':
        doc['work']['progress']['status']='in_progress'
    else:
        atomic_json(raw,{'imu':'changed'})
    atomic_json(out,doc)
    with pytest.raises(ValueError):
        restore_label(cat,out,require_done=True)
    assert not cat.work_path(work.asset_id).exists()


def test_inbox_stat_discovery_excludes_media_evidence_and_nested_data(returned):
    cat,raw,work,doc,out=returned
    inbox=out.parent.parent
    (inbox/'001.mp4').write_bytes(b'not opened')
    (inbox/'证据').mkdir()
    (inbox/'证据'/'ignored.json').write_text('{}')
    (out.parent/'nested').mkdir()
    (out.parent/'nested'/'ignored.json').write_text('{}')
    snap=return_folder_snapshot(inbox)
    assert list(snap)==[str(out)]
    assert json.loads(snap[str(out)])[0]==out.stat().st_size
    with pytest.raises(ValueError):
        return_folder_snapshot(inbox,limit=0)
