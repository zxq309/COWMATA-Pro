import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_annotation_pipeline_v330 import case, window  # noqa: F401

from cowmata_tailring.annotation.core import Label
from cowmata_tailring.annotation.defaults import LEGACY_DEFAULT_LABELS


def test_old_project_shows_current_keys_without_losing_old_events(window):  # noqa: F811
    window.work.project.labels = [Label.from_dict(x) for x in LEGACY_DEFAULT_LABELS]
    event = window.work.project.add_event(0, 10, 20)
    event_id = event.id
    window.refresh_events()
    assert window.labels.itemText(0) == '[1] 起立过程'
    saved = window.work.project.event_by_id(event_id)
    assert window.work.project.labels[saved.li].code == 'STANDING'
    assert (saved.t0, saved.t1) == (10, 20)
    assert not window.work.project.labels[saved.li].key


@pytest.mark.parametrize('key,code', [('1','STANDING_UP'),('2','LYING_DOWN'),('3','STANDING_TAIL_RAISED'),('4','STANDING_TAIL_WAGGING'),('5','LYING_TAIL_RAISED'),('6','LYING_TAIL_WAGGING'),('7','STRAINING_BOUT'),('8','AMNIOTIC_SAC_FIRST_VISIBLE'),('9','FETAL_PART_FIRST_VISIBLE'),('A','CALF_FULLY_EXPELLED'),('B','FETAL_MEMBRANES_FULLY_EXPELLED'),('C','URINATION'),('D','DEFECATION'),('E','MOUNTING'),('F','MANUAL_CALVING_ASSISTANCE')])
def test_repeated_physical_keys_match_display_after_old_project_load(window,monkeypatch,key,code):  # noqa: F811
    window.work.project.labels = [Label.from_dict(x) for x in LEGACY_DEFAULT_LABELS]
    window.work.project.cow_id = '20071'
    window.board.main_camera = 'A'
    position = [window.work.clock.map(100)]
    monkeypatch.setattr(window,'evidence',lambda:[{'camera':'A','frame_ready':True,'reference_ms':position[0]}])
    window.refresh_events()
    window.show()
    window.activateWindow()
    window.labels.setFocus()
    QTest.qWait(30)
    for _index in range(3):
        QTest.keyClick(window.labels, ord(key))
        QApplication.processEvents()
        if code in {'AMNIOTIC_SAC_FIRST_VISIBLE','FETAL_PART_FIRST_VISIBLE','CALF_FULLY_EXPELLED','FETAL_MEMBRANES_FULLY_EXPELLED'}:
            assert window.active_event is None
            assert window.work.project.labels[window.labels.currentIndex()].code == code
            position[0] += 100
            continue
        assert window.active_event is not None
        assert window.work.project.labels[window.active_event['label']].code == code
        assert window.work.project.labels[window.labels.currentIndex()].code == code
        position[0] += 100
        QTest.keyClick(window.labels, ord(key))
        QApplication.processEvents()
        assert window.active_event is None
        position[0] += 100
    assert len(window.work.drafts) == 3
    assert all(window.work.project.labels[x['label_index']].code == code for x in window.work.drafts)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows VLC keyboard integration")
def test_candidate_editor_f_is_assistance_not_waveform_fit(monkeypatch):
    from cowmata_tailring.app.model_assist_window import MainWindow
    from cowmata_tailring.media.engine import MediaEngine, MediaEngineError
    try:
        MediaEngine.find_vlc_directory()
    except MediaEngineError as exc:
        pytest.skip(str(exc))
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    try:
        w.data = object()
        w.playhead_ms = 100
        monkeypatch.setattr(w,'_refresh_playhead_for_user_action',lambda:None)
        monkeypatch.setattr(w,'_refresh_events',lambda:None)
        w.show()
        w.activateWindow()
        w.plot.setFocus()
        QTest.qWait(30)
        QTest.keyClick(w.plot,Qt.Key.Key_F)
        app.processEvents()
        assert w.pending_intervals == {14:100}
    finally:
        w.data = None
        w.pending_intervals = {}
        w.close()
        app.processEvents()


def test_cross_record_action_uses_code_not_other_records_label_index(window,monkeypatch):  # noqa: F811
    from cowmata_tailring.workspace.storage import atomic_json, read_json
    from cowmata_tailring.workspace.work import SessionWork
    window.work.project.cow_id='20071'
    window.board.main_camera='A'
    window.refresh_events()
    other=SessionWork('0'*64)
    other.project.labels=[Label.from_dict(x) for x in LEGACY_DEFAULT_LABELS]
    other.project.cow_id='20071'
    atomic_json(window.catalog.work_path(other.asset_id),other.to_dict())
    start=window.work.clock.map(100)
    window.active_event={'label':0,'label_code':'STANDING_UP','start':start,'evidence':[],
        'group_id':'cross-record','assets':{window.work.asset_id,other.asset_id},'cow_id':'20071'}
    monkeypatch.setattr(window,'evidence',lambda:[{'camera':'A','frame_ready':True,'reference_ms':start+100}])
    window.mark_code('STANDING_UP')
    assert window.active_event is None
    saved=SessionWork.from_dict(read_json(window.catalog.work_path(other.asset_id)))
    assert saved.project.labels[saved.drafts[0]['label_index']].code=='STANDING_UP'
