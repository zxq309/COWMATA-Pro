from pathlib import Path
import pytest
from cowmata_tailring.workspace import probe
from cowmata_tailring.workspace.video_filename import filename_wall, SIGNATURE
from cowmata_tailring.workspace.clocks import wall_ms

@pytest.mark.parametrize('name', ['2026-08-18_16-05-16.mp4','2026-08-18_16-05-16__001.MP4'])
def test_archive_filename_is_a_strict_calendar(name):
    assert filename_wall(name)==wall_ms('2026-08-18 16:05:16')

@pytest.mark.parametrize('name', ['2026-02-30_16-05-16.mp4','2026-08-18_26-05-16.mp4','copy_2026-08-18_16-05-16.mp4','2026-08-18_16-05-16.dav','clip.mp4'])
def test_nonstandard_names_do_not_gain_filename_time(name):
    assert filename_wall(name) is None

def inspector(tmp_path):
    meta=tmp_path/'meta';meta.mkdir(exist_ok=True)
    return probe.SourceInspector(tmp_path,meta)

def media_info():
    return {'streams':[{'codec_type':'video','codec_name':'h264','duration':'12.5','avg_frame_rate':'30000/1001','width':1920,'height':1080}], 'format':{'format_name':'mov,mp4,m4a,3gp,3g2,mj2','duration':'12.6'}}

def test_named_mp4_never_calls_ocr_frame_decode_or_packet_scan(tmp_path,monkeypatch):
    path=tmp_path/'2026-08-18_16-05-16.mp4';path.write_bytes(b'fixture')
    def forbidden(*a,**k):raise AssertionError('expensive timing path must be skipped')
    for name in ['TimestampOCR','extract_frame','read_native_index','native_hint','probe_media_timeline']:
        monkeypatch.setattr(probe,name,forbidden)
    monkeypatch.setattr(probe,'probe_media',lambda *a,**k:media_info())
    p=inspector(tmp_path)
    assert p.video_hint(path)['start_ms']==filename_wall(path)
    value=p.video(path,'a'*64)
    assert value['time_engine']==SIGNATURE and value['duration_ms']==12500
    assert value['intervals'][0]['wall_end']==filename_wall(path)+12500
    assert value['intervals'][0]['verified'] and not value['needs_review']
    assert value['samples']==[] and p.ocr is None

def test_nonstandard_name_still_uses_existing_verification(tmp_path,monkeypatch):
    path=tmp_path/'camera.mp4';path.write_bytes(b'fixture')
    monkeypatch.setattr(probe,'probe_media',lambda *a,**k:media_info())
    class FallbackReached(Exception):pass
    def fallback(*a,**k):raise FallbackReached
    monkeypatch.setattr(probe,'read_native_index',fallback)
    with pytest.raises(FallbackReached):inspector(tmp_path).video(path,'b'*64)

def test_identical_bytes_different_names_keep_distinct_calendar_and_camera(tmp_path,monkeypatch):
    from cowmata_tailring.workspace.catalog import Catalog
    monkeypatch.setattr(probe,'probe_media',lambda *a,**k:media_info())
    names=['Video/2026-08-18/视角01/2026-08-18_16-05-16.mp4','Video/2026-08-18/视角02/2026-08-18_17-05-16.mp4']
    for name in names:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'identical fixture bytes')
    cat=Catalog(tmp_path,stability_seconds=0)
    try:
        cat.scan();p=probe.SourceInspector(cat.root,cat.meta)
        for name in names:assert cat.index_one(name,p,eager=True)['state']=='ready'
        rows=cat.rows(kind='video')
        assert len({r['asset_id'] for r in rows})==1
        assert {r['metadata']['camera'] for r in rows}=={'视角01','视角02'}
        assert {r['metadata']['intervals'][0]['wall_start'] for r in rows}=={filename_wall(n) for n in names}
        assert cat.queue_ocr_upgrade('new OCR',time_signature='new native')==0
    finally:cat.close()


def test_named_legacy_mp4_reads_native_duration_without_ocr(tmp_path,monkeypatch):
    path=tmp_path/'2026-08-18_16-05-16.mp4';path.write_bytes(b'legacy PS in mp4 extension')
    info=media_info();info['streams'][0].pop('duration');info['format']={'format_name':'mpeg'}
    monkeypatch.setattr(probe,'probe_media',lambda *a,**k:info)
    native=dict(duration_ms=12500,first_pts_ms=9000,frame_ms=40,source_size=path.stat().st_size,source_mtime_ns=path.stat().st_mtime_ns)
    monkeypatch.setattr(probe,'read_native_index',lambda *a,**k:native)
    def forbidden(*a,**k):raise AssertionError('No OCR or frame extraction for trusted name')
    monkeypatch.setattr(probe,'TimestampOCR',forbidden);monkeypatch.setattr(probe,'extract_frame',forbidden)
    value=inspector(tmp_path).video(path,'c'*64)
    assert value['duration_ms']==12500 and value['timeline']['native']==native
    assert value['intervals'][0]['wall_start']==filename_wall(path)


def test_cached_discontinuous_timing_is_not_certified_by_filename(tmp_path):
    from cowmata_tailring.media.timeline import MediaTimelineIndex,TimelineSegment,TimelineDiscontinuity
    from cowmata_tailring.workspace.video_filename import metadata_from_name,bind_filename_location
    from cowmata_tailring.workspace.catalog import file_stamp
    path=tmp_path/'2026-08-18_16-05-16.mp4';path.write_bytes(b'fixture')
    timeline=MediaTimelineIndex(str(path),7,path.stat().st_mtime_ns,0,40,(TimelineSegment(0,1000,0,1000),TimelineSegment(9000,10000,1000,2000)),(TimelineDiscontinuity(5,1000,9000,8000),))
    value=metadata_from_name(path,path.name,media_info(),timeline)
    restored=bind_filename_location(value,path.name,file_stamp(path))
    assert restored['needs_review'] and not restored['intervals'][0]['verified']
