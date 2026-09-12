import base64
import json
from datetime import datetime
from pathlib import Path

import pytest

from cowmata_tailring.workspace import resource_import as org
from cowmata_tailring.workspace.device_identity import parse_device_folder
from cowmata_tailring.workspace.resource_layout import TZ, covered_days, overlaps_day


@pytest.fixture(autouse=True)
def access(tmp_path, monkeypatch):
    monkeypatch.setenv('COWMATA_ACCESS_DIR', str(tmp_path / 'access'))


@pytest.mark.parametrize('name,ear,mark', [
    ('546C50CA07D5-21100-10', '21100', '10'),
    ('546C50CA07D5-2110010', '21100', '10'),
    ('546C50CA07D5-21100-A-1', '21100', 'A1'),
    ('546C50CA07E8-y1-22207', '22207', 'y1'),
    ('546C50CA07E8-yA22207', '22207', 'yA'),
    ('546C50CA07D5-00123w1', '00123', 'w1'),
])
def test_identity(name, ear, mark):
    result = parse_device_folder(name)
    assert (result.cow_id, result.field_mark) == (ear, mark)


@pytest.mark.parametrize('name', ['546C50CA07D5-21100-', '546C50CA07E8-y1-22207-23335', '546C50CA07E8-y122207',
                                   '546C50CA07D5-牛01-A', '07D5-21100-10'])
def test_ambiguous_identity(name):
    with pytest.raises(ValueError):
        parse_device_folder(name)


def record(tmp_path):
    p = tmp_path / 'incoming/546C50CA07D5-2110010/one.json'
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'device': '546C50CA07D5', 'version': 0,
        'create_time': int(datetime(2026, 8, 3, 23, 59, 59, 980000, TZ).timestamp()*1000),
        'imu': base64.b64encode(bytes(18*4)).decode()}), encoding='utf-8')
    return p


def test_copy_and_repeat_resume_cross_midnight(tmp_path):
    p = record(tmp_path)
    target = tmp_path / 'resources'
    specs = [{'path': str(p.parent), 'kind': 'imu'}]
    plan = org.plan_import(target, specs, category='pregnancy_late', cache=tmp_path/'cache')
    assert [r['status'] for r in plan['rows']] == ['ready']
    row = plan['rows'][0]
    assert row['covered_dates'] == ['2026-08-03', '2026-08-04']
    assert '/Motion/2026-08-03/546C50CA07D5-21100-10/' in Path(row['target']).as_posix()
    result = org.execute(plan, tmp_path/'job')
    assert result['copied'] == 1 and p.exists()
    assert Path(row['target']).read_bytes() == p.read_bytes()
    assert org.execute(plan, tmp_path/'job')['copied'] == 0
    again = org.plan_import(target, specs, category='pregnancy_late', cache=tmp_path/'cache')
    assert again['rows'][0]['status'] == 'existing'
    assert org.execute(again, tmp_path/'job2')['copied'] == 0
    scope = Path(result['target'])
    assert (scope/'PPG/2026-08-04/占位说明.txt').is_file()
    assert len(list((scope/'Video/2026-08-04').iterdir())) == 8
    from cowmata_tailring.workspace.catalog import Catalog
    cat = Catalog(scope/'Motion/2026-08-04', stability_seconds=0)
    try:
        assert cat.root == scope
        assert cat.scan(fast=True).inspected == 1
    finally:
        cat.close()


def test_original_changed_after_plan_stops(tmp_path):
    p = record(tmp_path)
    plan = org.plan_import(tmp_path/'resources', [{'path': str(p), 'kind': 'imu'}], category='pregnancy_late', cache=tmp_path/'cache')
    p.write_text('{}')
    with pytest.raises(ValueError, match='来源已变化'):
        org.execute(plan, tmp_path/'job')
    assert not Path(plan['rows'][0]['target']).exists()


def test_half_open_days_and_video_predecessor():
    from cowmata_tailring.workspace.clocks import VideoInterval, VideoTimeline
    lo = datetime(2026,8,3,23,50,tzinfo=TZ).timestamp()*1000
    end = datetime(2026,8,4,tzinfo=TZ).timestamp()*1000
    assert covered_days(lo,end) == ['2026-08-03']
    assert not overlaps_day(lo,end,'2026-08-04')
    clip = VideoInterval('a','Video/2026-08-03/视角01/a.mp4','视角01',lo,end+60000,0,660000,True,())
    timeline = VideoTimeline([clip])
    assert timeline.locate('视角01',end+30000)[0] is clip
    assert timeline.locate('视角01',end+60000) is None


def test_ppg_placeholder_survives_save_and_restore():
    from cowmata_tailring.workspace.work import SessionWork
    work = SessionWork('a'*64)
    saved = work.to_dict()
    assert saved['project']['ppg']['available'] is False
    assert SessionWork.from_dict(saved).to_dict()['project']['ppg']['status'] == 'reserved'


def test_partial_import_keeps_invalid_original_and_reports_it(tmp_path):
    good = record(tmp_path)
    bad = good.with_name('truncated.json')
    value = json.loads(good.read_text())
    value['imu'] = base64.b64encode(bytes(19)).decode()
    bad.write_text(json.dumps(value))
    plan = org.plan_import(tmp_path/'out', [{'path': str(good.parent), 'kind': 'imu'}], category='pregnancy_late', cache=tmp_path/'cache')
    with pytest.raises(ValueError):
        org.execute(plan, tmp_path/'job')
    plan['allow_partial'] = True
    result = org.execute(plan, tmp_path/'job')
    assert result['unresolved'] == 1 and result['copied'] == 1
    assert bad.exists() and good.exists()
    assert 'truncated.json' in (Path(result['target'])/'整理异常.csv').read_text(encoding='utf-8-sig')


def test_cancelled_copy_holds_lease_and_resumes(tmp_path):
    from cowmata_tailring.workspace.dataset_access import ensure_available
    good = record(tmp_path)
    second = good.with_name('second.json')
    value = json.loads(good.read_text())
    value['create_time'] += 1000
    second.write_text(json.dumps(value))
    plan = org.plan_import(tmp_path/'out', [{'path': str(good.parent), 'kind': 'imu'}], category='pregnancy_late', cache=tmp_path/'cache')
    done = []
    with pytest.raises(InterruptedError):
        org.execute(plan, tmp_path/'job', cancelled=lambda: bool(done), progress=lambda *_: done.append(1))
    with pytest.raises(OSError, match='未完成'):
        ensure_available([plan['target']])
    assert org.execute(plan, tmp_path/'job')['copied'] == 1
    ensure_available([plan['target']])
    assert good.exists() and second.exists()


def test_legacy_writer_lock_and_annotations(tmp_path):
    from cowmata_tailring.workspace.catalog import digest_file
    from cowmata_tailring.workspace.storage import ProjectLock, atomic_json
    good = record(tmp_path)
    old = good.parent
    meta = old/'标注工程'
    meta.mkdir()
    sha = digest_file(good)
    saved = {'asset_id': sha, 'project': {'source': {'path': 'one.json'}, 'labels': [{'human': 'keep'}]}}
    atomic_json(meta/'annotations'/f'{sha}.json', saved)
    plan = org.plan_import(tmp_path/'out', [{'path': str(old), 'kind': 'imu'}], category='pregnancy_late', cache=tmp_path/'cache')
    lock = ProjectLock(meta/'writer.lock')
    try:
        with pytest.raises(OSError, match='仍在标注'):
            org.execute(plan, tmp_path/'job')
    finally:
        lock.close()
    result = org.execute(plan, tmp_path/'job')
    loaded = json.loads((Path(result['target'])/'标注工程/annotations'/f'{sha}.json').read_text(encoding='utf-8'))
    assert loaded['project']['labels'] == [{'human':'keep'}]
    assert loaded['project']['source']['path'].startswith('Motion/')
    assert json.loads((meta/'annotations'/f'{sha}.json').read_text()) == saved


def test_video_timestamp_naming_and_unverified_clock_blocks(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.probe import SourceInspector
    source = tmp_path/'cam/arbitrary.mp4'
    source.parent.mkdir()
    source.write_bytes(b'isolated synthetic media')
    from cowmata_tailring.workspace.clocks import wall_ms
    lo = wall_ms('2026-08-03 23:59:55')
    def verified(*_):
        return {'needs_review': False, 'intervals': [{'wall_start':lo, 'wall_end':lo+10000,
                 'media_start':0, 'media_end':10000, 'verified': True}]}
    monkeypatch.setattr(SourceInspector, 'video', verified)
    plan = org.plan_import(tmp_path/'out', [{'path':str(source),'kind':'video','camera':'视角08'}], category='pregnancy_late',cache=tmp_path/'cache')
    row = plan['rows'][0]
    assert row['covered_dates'] == ['2026-08-03','2026-08-04']
    assert Path(row['target']).name == '2026-08-03_23-59-55.mp4'
    assert row['metadata']['camera'] == '视角08'
    assert row['record_start_ms'] == datetime(2026,8,3,23,59,55,tzinfo=TZ).timestamp()*1000
    org.execute(plan,tmp_path/'verified-job')
    scope = Path(plan['target'])
    hint = SourceInspector(scope, scope/'标注工程').video_hint(Path(row['target']))
    assert hint['start_ms'] == lo and hint['end_ms'] == lo+10000
    monkeypatch.setattr(SourceInspector,'video',lambda *_: {'needs_review': True, 'intervals': []})
    bad = org.plan_import(tmp_path/'out', [{'path':str(source),'kind':'video','camera':'视角08'}], category='pregnancy_late',cache=tmp_path/'other-cache')
    assert bad['rows'][0]['status'] == 'blocked'
    with pytest.raises(ValueError):
        org.execute(bad,tmp_path/'job')
    assert source.exists()


def test_move_preserves_file_identity_and_redirects_old_project(tmp_path):
    from cowmata_tailring.workspace.organization import identity
    from cowmata_tailring.workspace.resource_layout import resource_context
    from cowmata_tailring.workspace.storage import atomic_json
    good = record(tmp_path)
    old = good.parent
    atomic_json(old/'标注工程/project.json', {'current_path':'one.json'})
    plan = org.plan_import(tmp_path/'out', [{'path':str(old),'kind':'imu'}],category='pregnancy_late',cache=tmp_path/'cache',transfer='move')
    before = identity(good)
    result = org.execute(plan,tmp_path/'job')
    assert result['same_volume_moved'] == 1 and result['copied'] == 0
    assert not good.exists()
    assert identity(Path(plan['rows'][0]['target'])) == before
    assert resource_context(old) == Path(result['target'])
    again = org.execute(plan,tmp_path/'job')
    assert again['same_volume_moved'] == 0 and again['existing'] == 1


def test_move_resumes_after_atomic_rename_before_journal(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import organization as core
    good = record(tmp_path)
    plan = org.plan_import(tmp_path/'out', [{'path':str(good),'kind':'imu'}],category='pregnancy_late',cache=tmp_path/'cache',transfer='move')
    original = core.move_no_replace
    def crash(source,destination):
        original(source,destination)
        raise OSError('simulated crash after rename')
    monkeypatch.setattr(core,'move_no_replace',crash)
    with pytest.raises(OSError,match='simulated'):
        org.execute(plan,tmp_path/'job')
    assert not good.exists()
    monkeypatch.setattr(core,'move_no_replace',original)
    assert org.execute(plan,tmp_path/'job')['completed']


def test_missing_ocr_second_segment_is_not_continuously_filled():
    from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
    from cowmata_tailring.workspace.probe import build_observed_intervals
    timeline=MediaTimelineIndex('source',100,1,0,40,(TimelineSegment(0,1000,0,1000), TimelineSegment(100000,101000,1000,2000)),())
    samples=[{'media_ms':0,'wall_ms':1000000},{'media_ms':900,'wall_ms':1000900},
             {'media_ms':1100,'wall_ms':5000100},{'media_ms':1960,'wall_ms':5000960}]
    intervals,_=build_observed_intervals(samples,timeline)
    assert not any(i['wall_start'] <= 3000000 < i['wall_end'] for i in intervals)


def test_float_rounding_does_not_duplicate_verified_end():
    from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
    from cowmata_tailring.workspace.probe import build_observed_intervals
    timeline=MediaTimelineIndex('source',100,1,0,66.667,(TimelineSegment(0,1066.6675,0,1066.6675),),())
    spans,warnings=build_observed_intervals([{'media_ms':0,'wall_ms':1000000}, {'media_ms':1000,'wall_ms':1001000}], timeline)
    assert all(i['verified'] for i in spans) and not warnings


def test_verified_hash_cache_invalidates_changed_source(tmp_path):
    import hashlib
    source = tmp_path/'source.bin'
    cache = tmp_path/'cache'
    cache.mkdir()
    source.write_bytes(b'first')
    first = org.verified_source_digest(source, cache, lambda: False)
    source.write_bytes(b'changed content')
    second = org.verified_source_digest(source, cache, lambda: False)
    assert first != second == hashlib.sha256(b'changed content').hexdigest()


def test_later_batch_keeps_previous_exception_report(tmp_path):
    import csv
    good = record(tmp_path)
    plan = org.plan_import(tmp_path/'out', [{'path':str(good),'kind':'imu'}], category='pregnancy_late',cache=tmp_path/'cache')
    plan['allow_partial'] = True
    plan['rows'].append({'source':str(tmp_path/'broken.json'),'status':'invalid','message':'broken original'})
    org.execute(plan,tmp_path/'job1')
    later = org.plan_import(tmp_path/'out', [{'path':str(good),'kind':'imu'}],category='pregnancy_late',cache=tmp_path/'cache')
    org.execute(later,tmp_path/'job2')
    with (Path(plan['target'])/'整理异常.csv').open(encoding='utf-8-sig',newline='') as f:
        assert [r['source'] for r in csv.DictReader(f)] == [str(tmp_path/'broken.json')]


@pytest.mark.parametrize('ocr_offset', [None, 0, 86400000])
def test_native_clock_preferred_pending_and_conflicting_ocr(tmp_path,monkeypatch,ocr_offset):
    from cowmata_tailring.workspace.clocks import wall_ms
    from cowmata_tailring.workspace.probe import SourceInspector
    source=tmp_path/'video.mp4'
    source.write_bytes(b'fake container for isolated clock test')
    lo=wall_ms('2026-08-03 23:59:59') + 987
    metadata={'needs_review':True,'timeline':{'native':{'wall_start':lo,'duration_ms':2000}},
              'samples':[] if ocr_offset is None else [{'media_ms':1000,'wall_ms':lo+1000+ocr_offset,'ocr':{'success':True}}],
              'intervals':[{'wall_start':lo,'wall_end':lo+2000,'media_start':0,'media_end':2000,'verified':False}]}
    monkeypatch.setattr(SourceInspector,'video',lambda *_:metadata)
    row=org.plan_import(tmp_path/'out',[{'path':str(source),'kind':'video','camera':'视角01'}],category='pregnancy_late',cache=tmp_path/'cache')['rows'][0]
    if ocr_offset==86400000:
        assert row['status']=='blocked'
    else:
        assert row['status']=='ready' and row['covered_dates']==['2026-08-03','2026-08-04']
        assert Path(row['target']).name == '2026-08-03_23-59-59.mp4'
        assert bool(row.get('timeline_review_required')) == (ocr_offset is None)
        assert row['metadata']['needs_review'] == (ocr_offset is None)
        assert row['metadata']['intervals'][0]['verified'] == (ocr_offset==0)


def test_zero_filled_video_is_reported_without_decoder(tmp_path):
    p=tmp_path/'blank.mp4'
    p.write_bytes(bytes(4096))
    row=org.plan_import(tmp_path/'out',[{'path':str(p),'kind':'video','camera':'视角01'}],category='pregnancy_late',cache=tmp_path/'cache')['rows'][0]
    assert row['status']=='blocked' and '零字节' in row['message']
    assert p.read_bytes()==bytes(4096)


def test_verified_ocr_uses_native_start_even_when_ocr_rounds_to_previous_second(tmp_path, monkeypatch):
    from cowmata_tailring.media import native_ps
    from cowmata_tailring.workspace.clocks import wall_ms
    from cowmata_tailring.workspace.probe import SourceInspector
    source = tmp_path/'video.mp4'
    source.write_bytes(b'isolated native priority example')
    lo = wall_ms('2026-08-03 12:00:00')
    monkeypatch.setattr(native_ps, 'native_hint', lambda _: {'start_ms': lo+1000})
    monkeypatch.setattr(SourceInspector, 'video', lambda *_: {
        'needs_review': False, 'intervals': [{'wall_start': lo, 'wall_end': lo+10000,
        'media_start': 0, 'media_end': 10000, 'verified': True}]})
    row = org.plan_import(tmp_path/'out', [{'path': str(source), 'kind': 'video', 'camera': '视角01'}],
                          category='pregnancy_late', cache=tmp_path/'cache')['rows'][0]
    assert row['status'] == 'ready'
    assert Path(row['target']).name == '2026-08-03_12-00-01.mp4'
    assert row['record_start_ms'] == lo+1000-28800000


@pytest.mark.parametrize('same_content', [True, False])
def test_second_precision_name_collision_never_silently_skips_different_video(tmp_path, monkeypatch, same_content):
    from cowmata_tailring.workspace.clocks import wall_ms
    from cowmata_tailring.workspace.probe import SourceInspector
    source = tmp_path / 'camera'
    source.mkdir()
    (source / 'first.mp4').write_bytes(b'first synthetic video')
    (source / 'second.mp4').write_bytes(b'first synthetic video' if same_content else b'different video')
    lo = wall_ms('2026-08-03 12:00:00') + 987
    monkeypatch.setattr(SourceInspector, 'video', lambda *_: {
        'needs_review': False, 'intervals': [{'wall_start': lo, 'wall_end': lo+2000,
        'media_start': 0, 'media_end': 2000, 'verified': True}]})
    plan = org.plan_import(tmp_path/'out', [{'path': str(source), 'kind': 'video', 'camera': '视角01'}],
                           category='pregnancy_late', cache=tmp_path/'cache')
    assert [r['status'] for r in plan['rows']] == ['ready', 'skip' if same_content else 'blocked']
    assert Path(plan['rows'][0]['target']).name == '2026-08-03_12-00-00.mp4'
    assert all(p.exists() for p in source.iterdir())


def test_same_second_edge_never_creates_zero_wall_extent():
    from cowmata_tailring.media.timeline import MediaTimelineIndex, TimelineSegment
    from cowmata_tailring.workspace.probe import build_observed_intervals
    timeline=MediaTimelineIndex('source',100,1,0,40,(TimelineSegment(0,1300,0,1300),),())
    samples=[{'media_ms':0,'wall_ms':1000000},{'media_ms':600,'wall_ms':None},
             {'media_ms':1100,'wall_ms':1000000}]
    spans,_=build_observed_intervals(samples,timeline)
    assert not spans
