"""Reproduce reported keyboard, duration, selection and drag interactions."""
# Reuse the existing integration fixtures; test parameters request these fixtures.
# ruff: noqa: F811
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_annotation_pipeline_v330 import case, window  # noqa: F401

from cowmata_tailring.workspace.algorithm_catalog import BEHAVIORS
from cowmata_tailring.workspace.work import SessionWork


@pytest.mark.parametrize('control_name', ['speed', 'labels'])
def test_space_after_combo_selection_controls_playback(window, monkeypatch, control_name):
    calls = []
    monkeypatch.setattr(window.board, 'play', calls.append)
    window.show()
    window.activateWindow()
    control = getattr(window, control_name)
    control.setCurrentIndex(1)
    control.setFocus()
    QTest.qWait(30)
    QTest.keyClick(control, Qt.Key.Key_Space)
    control.hidePopup()
    assert calls == [True]


def test_space_remains_text_input_in_cow_id(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window.board, 'play', calls.append)
    window.show()
    window.activateWindow()
    window.source_panel.show()
    window.cow.setFocus()
    window.cow.setText('A')
    QTest.keyClick(window.cow, Qt.Key.Key_Space)
    assert window.cow.text() == 'A ' and calls == []


def test_equal_interval_rejected_before_creating_draft_or_undo_state():
    work = SessionWork('source')
    before = work.to_dict()
    with pytest.raises(ValueError):
        work.add_draft(0, 1000, 1000, [])
    assert work.to_dict() == before
    assert work.add_draft(0, 1000, None, [])['reference_end'] is None


def test_equal_end_keeps_action_open_and_can_finish_later(window, monkeypatch):
    position = [10000]
    window.board.main_camera = 'A'
    monkeypatch.setattr(window, 'evidence', lambda: [{'camera': 'A', 'frame_ready': True, 'reference_ms': position[0]}])
    window.mark(0)
    window.mark(0)
    assert window.active_event is not None and not window.work.drafts
    assert 'end' not in window.active_event
    position[0] += 200
    window.mark(0)
    assert window.active_event is None and len(window.work.drafts) == 1


def test_new_annotation_replaces_old_table_selection(window, monkeypatch):
    old = window.work.add_draft(0, 10000, 10100, [])
    window.refresh_events()
    window.events.selectRow(0)
    position = [10200]
    window.board.main_camera = 'A'
    monkeypatch.setattr(window, 'evidence', lambda: [{'camera': 'A', 'frame_ready': True, 'reference_ms': position[0]}])
    window.mark(0)
    position[0] += 200
    window.mark(0)
    latest = window.work.drafts[-1]
    assert latest['id'] != old['id']
    assert window.selected_entry() == ('draft', latest['id'])


def test_clicking_table_selects_waveform_handles_without_extra_refine_step(window):
    event = window.work.project.add_event(0, 100, 400)
    window.refresh_events()
    window.events.selectRow(0)
    assert window.plot.wave._selected_event_id == event.id


@pytest.mark.parametrize('edge', ['left', 'right', 'move'])
def test_label_strip_drag_edits_event_and_invalidates_confirmation(window, edge):
    window.plot.wave._duration_ms = 1000
    window.plot.wave.set_view(0, 1000)
    event = window.work.project.add_event(0, 200, 600)
    event.extras['confirmation'] = 'confirmed'
    window.refresh_events()
    window.show()
    QApplication.processEvents()
    track = window.plot.track
    track.repaint()
    rectangle = next(rect for rect, identifier in track.hits if identifier == event.id)
    x = {'left': rectangle.left()+1, 'right': rectangle.right()-1, 'move': rectangle.center().x()}[edge]
    point = QPoint(round(x), round(rectangle.center().y()))
    QTest.mousePress(track, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(track, point + QPoint(30, 0), delay=20)
    QTest.mouseRelease(track, Qt.MouseButton.LeftButton, pos=point + QPoint(30, 0))
    assert (event.t0, event.t1) != (200, 600)
    assert event.t1 > event.t0 and event.extras['confirmation'] == 'needs_review'
    assert window.selected_entry() == ('event', event.id)


def test_algorithm_label_button_has_visible_steps_and_updates_action(window):
    window.show()
    window.open_algorithm(BEHAVIORS[0])
    QApplication.processEvents()
    panel = window.algorithm_panel
    assert hasattr(panel, 'label_help') and panel.label_help.isVisible()
    assert '开始' in panel.label_help.text() and '结束' in panel.label_help.text()
    panel.choose_label()
    assert window.work.project.labels[window.labels.currentIndex()].code == BEHAVIORS[0].code
    assert window.work.project.labels[window.labels.currentIndex()].name in window.mark_button.text()
    assert not window.work.drafts and not window.work.project.events


def test_zero_duration_edit_rejected_without_mutation():
    work = SessionWork('source')
    event = work.project.add_event(0, 100, 200)
    before = work.to_dict()
    with pytest.raises(ValueError):
        work.edit_event(event.id, 150, 150, 1000)
    assert work.to_dict() == before


def test_clicking_label_without_drag_does_not_invalidate_confirmation(window):
    window.plot.wave._duration_ms = 1000
    window.plot.wave.set_view(0, 1000)
    event = window.work.project.add_event(0, 200, 600)
    event.extras['confirmation'] = 'confirmed'
    window.refresh_events()
    window.show()
    QApplication.processEvents()
    track = window.plot.track
    track.repaint()
    rectangle = next(rect for rect, identifier in track.hits if identifier == event.id)
    QTest.mouseClick(track, Qt.MouseButton.LeftButton, pos=rectangle.center().toPoint())
    assert event.extras['confirmation'] == 'confirmed'
