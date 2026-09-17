from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_annotation_pipeline_v330 import case, window  # noqa: F401


def test_candidate_window_label_key_records_same_behavior(window,monkeypatch):  # noqa: F811
    from cowmata_tailring.workspace.candidate_window import CandidateWindow
    window.work.project.cow_id='20071'
    window.board.main_camera='A'
    position=[window.work.clock.map(100)]
    monkeypatch.setattr(window,'evidence',lambda:[{'camera':'A','frame_ready':True,'reference_ms':position[0]}])
    window.refresh_events()
    panel=CandidateWindow(window)
    try:
        panel.show()
        panel.activateWindow()
        panel.items.setFocus()
        QTest.qWait(30)
        QTest.keyClick(panel.items,Qt.Key.Key_F)
        QApplication.processEvents()
        assert window.active_event is not None
        assert window.work.project.labels[window.active_event['label']].code == 'MANUAL_CALVING_ASSISTANCE'
        position[0]+=200
        QTest.keyClick(panel.items,Qt.Key.Key_F)
        QApplication.processEvents()
        assert window.active_event is None
        assert window.work.project.labels[window.work.drafts[-1]['label_index']].code=='MANUAL_CALVING_ASSISTANCE'
    finally:
        panel.timer.stop()
        panel.close()
