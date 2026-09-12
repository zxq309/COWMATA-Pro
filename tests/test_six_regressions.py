import pytest

from cowmata_tailring.workspace.clocks import Anchor, ClockMap
from cowmata_tailring.workspace.work import SessionWork


def work():
    value = SessionWork('a'*64)
    value.project.cow_id = '21100'
    value.clock = ClockMap([Anchor(0,10000),Anchor(1000,11000)])
    return value


def test_one_camera_does_not_split_by_resolution_or_ocr_corner():
    from cowmata_tailring.workspace.demand import camera_inventory, resolve_camera_choices
    names = ['', '乐橙 · 2560×1440 · top_right', '乐橙 · 2560×1440 · unknown']
    rows = [dict(path=f'乐橙/imou{i:05d}.mp4',kind='video',state='review',asset_id=str(i),
                 metadata={'camera':name}) for i,name in enumerate(names)]
    inventory = camera_inventory(rows)
    assert list(inventory)==['乐橙']
    assert resolve_camera_choices(names[1:],inventory)==['乐橙']


def test_manual_reading_does_not_make_old_generated_name_a_new_camera():
    from cowmata_tailring.workspace.demand import camera_inventory
    row = dict(path='右1/imou00020.mp4',kind='video',state='review',asset_id='a',
               metadata={'camera':'右1 · 1920×1080 · bottom_right','manual_readings':[{}]})
    assert list(camera_inventory([row],{'a':row['metadata']['camera']}))==['右1']
    assert list(camera_inventory([row],{'a':'独立相机'}))==['独立相机']


def test_recalibration_preserves_unconfirmed_draft_imu_coordinates_and_undo():
    value = work()
    draft = value.add_draft(0,10100,10200,[])
    before = value.project_draft(draft,1000)
    value.calibrate(500,10600,{})
    assert value.project_draft(draft,1000)==before
    assert draft['confirmation']=='needs_review'
    restored=SessionWork.from_dict(value.to_dict())
    assert restored.project_draft(restored.drafts[0],1000)==before
    assert value.undo_once()
    assert value.drafts[0]['reference_start']==10100


@pytest.mark.parametrize('label',[0,1,2])
def test_body_state_draft_rejects_overlaps_before_mutation(label):
    value=work()
    value.add_draft(0,10100,10300,[])
    with pytest.raises(ValueError,match='互斥'):
        value.add_draft(label,10200,10400,[])
    assert len(value.drafts)==1
    value.add_draft(label,10300,10400,[])
    tail=next(i for i,label in enumerate(value.project.labels) if label.code=='TAIL_WAGGING')
    value.add_draft(tail,10150,10250,[])
    assert len(value.drafts)==3


def test_sorted_labels_mix_events_and_drafts_preserving_selection():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.modern_window import MainWindow
    app=QApplication.instance() or QApplication([])
    window=MainWindow()
    window.work=work()
    a=window.work.add_draft(0,10800,10900,[])
    window.work.add_draft(0,10100,10200,[])
    event=window.work.project.add_event(0,400,500)
    window.refresh_events(preferred=('draft',a['id']))
    try:
        kinds=[window.events.item(i,0).data(Qt.ItemDataRole.UserRole) for i in range(3)]
        assert kinds[1]==('event',event.id)
        assert kinds[-1]==('draft',a['id'])
        assert window.selected_entry()==('draft',a['id'])
    finally:
        window.work=None
        window.close()
        app.processEvents()


def test_tile_duration_is_full_file_even_if_only_middle_verified(tmp_path):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.clocks import VideoInterval, VideoTimeline
    from cowmata_tailring.workspace.playback import VideoBoard
    app=QApplication.instance() or QApplication([])
    board=VideoBoard()
    span=VideoInterval('a','one.mp4','右1',101000,121000,1000,21000,True)
    board.timeline=VideoTimeline([span])
    board.catalog=SimpleNamespace(source_path=lambda p:tmp_path/p)
    board.metadata={str(tmp_path/'one.mp4'):{'duration_ms':3600000}}
    board.reference_ms=111000
    tile=board._new_tile()
    tile.asset_id='a'
    tile.interval=span
    board.tiles={'右1':tile}
    board._update_controls()
    try:
        assert tile.seek_clock.text()=='00:00:11 / 01:00:00'
    finally:
        board.catalog=None
        board.close()
        app.processEvents()


def test_manual_readings_keep_entire_continuous_clip_browsable():
    from cowmata_tailring.workspace.clocks import (
        VideoTimeline,
        intervals_from_rows,
        manual_video_metadata,
    )
    metadata={'duration_ms':60000,'recheck':1,'native_check_pending':True}
    updated=manual_video_metadata(metadata,[{'media_ms':10000,'wall_ms':110000},
        {'media_ms':30000,'wall_ms':130000}])
    row=dict(kind='video',state='review',path='右1/one.mp4',asset_id='a',metadata=updated)
    timeline=VideoTimeline(intervals_from_rows([row]))
    assert timeline.locate('右1',105000)[1]==5000
    assert not timeline.locate('右1',105000)[0].verified
    assert timeline.locate('右1',120000)[0].verified
    assert timeline.locate('右1',150000)[1]==50000
    assert not updated.get('recheck') and not updated.get('native_check_pending')
    single=manual_video_metadata(metadata,[{'media_ms':0,'wall_ms':100000}])
    assert single['intervals'][0]['media_end']==60000 and not single['intervals'][0]['verified']


def test_body_state_edit_cannot_overwrite_other_state():
    value=work()
    first=value.add_draft(0,10100,10200,[])
    second=value.add_draft(1,10300,10400,[])
    with pytest.raises(ValueError,match='互斥'):
        value.edit_draft(second['id'],10150,10400)
    assert second['reference_start']==10300
    event=value.project.add_event(0,600,700)
    with pytest.raises(ValueError,match='互斥'):
        value.edit_event(event.id,100,400,1000)
    assert event.t0==600 and first['reference_start']==10100


def test_old_generated_camera_mapping_is_preserved_after_grouping():
    from cowmata_tailring.workspace.clocks import VideoInterval, VideoTimeline
    span=VideoInterval('a','右1/a.mp4','右1',10000,20000,0,10000,True)
    mapping=ClockMap([Anchor(10000,15000)])
    timeline=VideoTimeline([span],{'右1 · 1920×1080 · unknown':mapping})
    assert timeline.locate('右1',16000)[1]==1000
    conflict=VideoTimeline([span],{'右1 · 1920×1080 · unknown':mapping,
        '右1 · 1920×1080 · bottom_right':ClockMap([Anchor(10000,17000)])})
    assert not conflict.locate('右1',16000)[0].verified


def test_standard_video_export_never_overwrites_original(tmp_path):
    from cowmata_tailring.media.standard_video import export_standard_video
    source=tmp_path/'one.mp4'
    source.write_bytes(b'original')
    with pytest.raises(ValueError,match='覆盖'):
        export_standard_video(source,source)
    assert source.read_bytes()==b'original'


def test_tile_clock_shows_file_seconds_and_clears_stale_duration():
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.playback import VideoTile
    app=QApplication.instance() or QApplication([])
    tile=VideoTile()
    tile.update_seek(100000,136000,118000,media_duration_ms=60000)
    assert tile.seek_clock.text()=='00:00:30 / 00:01:00'
    tile.update_seek(0,0,0)
    assert '--:--:--' in tile.seek_clock.text()
    tile.close()
    app.processEvents()


def test_manual_save_wins_over_inflight_index_publish(tmp_path,monkeypatch):
    from cowmata_tailring.workspace import catalog as module
    from cowmata_tailring.workspace.catalog import Catalog
    from cowmata_tailring.workspace.storage import atomic_json
    source=tmp_path/'a.mp4'
    source.write_bytes(b'video')
    cat=Catalog(tmp_path,stability_seconds=0)
    cat.scan(fast=True)
    cat.index_one('a.mp4',lambda *_:dict(camera='A',duration_ms=60000,needs_review=True),eager=True)
    row=cat.rows()[0]
    readings=[dict(media_ms=0,wall_ms=100000)]
    calls=[]
    def human_save_during_last_stat(path):
        calls.append(path)
        if len(calls)==2:
            correction=dict(asset_id=row['asset_id'],camera='A',readings=readings,roi=None,intervals=[])
            atomic_json(cat.meta/'video_corrections'/(row['asset_id']+'.json'),correction)
    monkeypatch.setattr(module,'assert_not_being_written',human_save_during_last_stat)
    cat.recheck('a.mp4')
    cat.index_one('a.mp4',lambda *_:dict(camera='A',duration_ms=60000,needs_review=True),eager=True)
    try:
        updated=cat.rows()[0]['metadata']
        assert updated.get('manual_readings')==readings
        assert updated['intervals'][0]['wall_start']==100000
    finally:
        cat.close()


def test_progress_slider_rounding_does_not_subtract_a_displayed_second():
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.playback import VideoTile
    app=QApplication.instance() or QApplication([])
    tile=VideoTile()
    tile.update_seek(100000,160000,105000)
    assert tile.seek_clock.text()=='00:00:05 / 00:01:00'
    tile.close()
    app.processEvents()
