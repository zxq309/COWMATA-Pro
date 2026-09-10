"""Customer action controls and video-draft-to-IMU review regressions."""
import copy
import os
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
from test_annotation_pipeline_v330 import case  # noqa: F401
from test_annotation_pipeline_v330 import window as workflow_window  # noqa: F401

from cowmata_tailring.ui.widgets import PlotSeries
from cowmata_tailring.workspace.clocks import Anchor, ClockMap
from cowmata_tailring.workspace.signal_panel import TimePositionSpinBox
from cowmata_tailring.workspace.storage import read_json
from cowmata_tailring.workspace.work import SessionWork


@pytest.fixture
def observed(request, monkeypatch):
    window = request.getfixturevalue("workflow_window")
    position = [window.work.clock.map(100)]
    window.work.project.cow_id = "00047"
    window.cow.setText("00047")
    window.board.main_camera = "view01"
    monkeypatch.setattr(window, "evidence", lambda: [{"camera": "view01", "frame_ready": True,
        "verified_interval": True, "reference_ms": position[0]}])
    window.refresh_events()
    return window, position


def test_start_action_exposes_label_and_timestamp_immediately(observed):
    window, _ = observed
    window.mark_button.click()
    assert window.active_event is not None
    assert not window.event_status.isHidden()
    assert "站立" in window.event_status.text()
    assert "2026-" in window.event_status.text()
    assert "结束" in window.mark_button.text() and "站立" in window.mark_button.text()


def test_selection_hint_uses_timestamps_and_keeps_active_action_visible(observed):
    window, _ = observed
    window.select_range(100, 200)
    assert "2026-" in window.event_status.text()
    window.mark(0)
    window.select_range(250, 400)
    assert "正在记录：站立" in window.event_status.text()
    assert not window.event_status.isHidden()


def test_button_ends_original_action_after_combo_changes(observed):
    window, position = observed
    window.mark_button.click()
    window.labels.setCurrentIndex(1)
    position[0] = window.work.clock.map(800)
    window.mark_button.click()
    assert window.active_event is None
    assert len(window.work.drafts) == 1 and window.work.drafts[0]["label_index"] == 0
    assert read_json(window.catalog.meta / "project.json")["active_event"] is None


def test_wrong_numeric_label_names_original_action_and_preserves_it(observed):
    window, _ = observed
    window.mark(0)
    original = copy.deepcopy(window.active_event)
    window.mark_code("LYING")
    assert window.active_event == original
    assert "站立" in window.banner.text() and "1" in window.banner.text()
    assert not window.event_status.isHidden()


def test_cancel_action_requires_confirmation_and_keeps_finished_work(observed, monkeypatch):
    window, _ = observed
    window.work.add_draft(0, window.work.clock.map(20), window.work.clock.map(40), [])
    window.mark(0)
    original = copy.deepcopy(window.active_event)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.No)
    window.cancel_action_button.click()
    assert window.active_event == original
    assert read_json(window.catalog.meta / "project.json")["active_event"]["start"] == original["start"]
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    window.cancel_action_button.click()
    assert window.active_event is None and len(window.work.drafts) == 1
    assert read_json(window.catalog.meta / "project.json")["active_event"] is None


def test_restored_active_action_is_visible_and_named(observed):
    window, _ = observed
    window.mark(0)
    saved = read_json(window.catalog.meta / "project.json")["active_event"]
    window.active_event = None
    window.refresh_events()
    saved["assets"] = set(saved["assets"])
    window.active_event = saved
    window.refresh_events()
    assert not window.event_status.isHidden()
    assert "站立" in window.event_status.text()
    assert "结束" in window.mark_button.text() and "站立" in window.mark_button.text()


@pytest.mark.parametrize("conflict", ["cow", "frame"])
def test_explicit_end_button_preserves_cow_and_frame_guards(observed, monkeypatch, conflict):
    window, _ = observed
    window.mark(0)
    original = copy.deepcopy(window.active_event)
    if conflict == "cow":
        window.work.project.cow_id = "another-cow"
    else:
        monkeypatch.setattr(window, "evidence", lambda: [])
    window.mark_button.click()
    assert window.active_event == original and not window.work.drafts
    assert "牛号" in window.banner.text() or "画面" in window.banner.text()


def test_cancelling_window_close_does_not_cancel_active_action(observed, monkeypatch):
    window, _ = observed
    window.mark(0)
    original = copy.deepcopy(window.active_event)
    monkeypatch.setattr(window, "confirm_close", lambda: "cancel")
    window.close()
    assert not window._closed and window.active_event == original
    monkeypatch.setattr(window, "confirm_close", lambda: "save")


@pytest.mark.parametrize("focus_name, should_mark", [("labels", True), ("cow", False)])
def test_numeric_shortcut_works_from_label_combo_but_preserves_typing(observed, focus_name, should_mark):
    window, _ = observed
    window.show()
    window.activateWindow()
    window.source_panel.show()
    control = window.labels if focus_name == "labels" else window.cow
    control.setFocus()
    QTest.qWait(30)
    QTest.keyClick(control, Qt.Key.Key_1)
    QApplication.processEvents()
    assert bool(window.active_event) is should_mark


def draft_for_review(window):
    draft = window.work.add_draft(0, window.work.clock.map(100), window.work.clock.map(800),
                                 [{"frame_ready": True, "verified_interval": True}])
    window.refresh_events()
    window.events.selectRow(0)
    return draft


def test_draft_imu_preview_drag_stays_unconfirmed_and_undoable(observed):
    window, _ = observed
    draft = draft_for_review(window)
    original = copy.deepcopy(draft)
    window.refine_selected()
    preview = next(e for e in window.plot.wave._events if e["id"] < 0)
    assert (preview["t0"], preview["t1"]) == (100, 800)
    assert not window.work.project.events
    window.plot.eventChanged.emit(preview["id"], 250, 750)
    changed = window.work.drafts[0]
    assert (changed["reference_start"], changed["reference_end"]) == (window.work.clock.map(250), window.work.clock.map(750))
    assert changed["confirmation"] != "confirmed" and changed["video_evidence"] == []
    assert not read_json(window.catalog.work_path(window.work.asset_id))["project"]["events"]
    window.undo()
    assert window.work.drafts[0] == original
    window.undo(redo=True)
    assert window.work.drafts[0]["reference_start"] == window.work.clock.map(250)
    assert not window.work.project.events


@pytest.mark.parametrize("missing_clock", [True, False])
def test_draft_preview_rejects_missing_clock_or_outside_record(observed, missing_clock):
    window, _ = observed
    draft = draft_for_review(window)
    if missing_clock:
        window.work.clock = ClockMap()
    else:
        draft["reference_start"] = window.work.clock.map(2000)
        draft["reference_end"] = window.work.clock.map(3000)
    original = copy.deepcopy(window.work.to_dict())
    window.refine_selected()
    assert window.work.to_dict() == original
    assert not any(e["id"] < 0 for e in window.plot.wave._events)
    assert "对应" in window.banner.text() or "范围" in window.banner.text()


def test_draft_editor_has_two_timestamp_controls_and_independent_steps(observed):
    window, _ = observed
    draft_for_review(window)
    inspected = []

    def edit():
        dialog = QApplication.activeModalWidget()
        try:
            start = dialog.findChild(TimePositionSpinBox, "event_start")
            end = dialog.findChild(TimePositionSpinBox, "event_end")
            assert start is not None and end is not None
            assert "2026-" in start.text() and "2026-" in end.text()
            next(b for b in dialog.findChildren(QPushButton) if b.text() == "开始 +0.1 秒").click()
            next(b for b in dialog.findChildren(QPushButton) if b.text() == "结束 −0.1 秒").click()
            assert (start.value(), end.value()) == pytest.approx((.2, .7))
            inspected.append(True)
            dialog.accept()
        finally:
            if not inspected:
                dialog.reject()

    QTimer.singleShot(0, edit)
    window.edit_selected()
    assert inspected
    draft = window.work.drafts[0]
    assert (draft["reference_start"], draft["reference_end"]) == (window.work.clock.map(200), window.work.clock.map(700))
    assert draft["confirmation"] != "confirmed" and not window.work.project.events


def test_modified_draft_still_requires_new_evidence(observed):
    window, _ = observed
    window.work.clock = ClockMap([Anchor(0, window.work.clock.map(0)), Anchor(1000, window.work.clock.map(1000))])
    draft = draft_for_review(window)
    window.refine_selected()
    preview = next(e for e in window.plot.wave._events if e["id"] < 0)
    window.edit_plot_event(preview["id"], 250, 750)
    with pytest.raises(ValueError, match="视频画面"):
        window.work.confirm_draft(draft["id"], window.motion.duration_ms)
    assert not window.work.project.events


def test_real_waveform_mouse_drag_adjusts_both_draft_edges(observed):
    window, _ = observed
    draft_for_review(window)
    window.plot.set_data([PlotSeries(**s) for s in window.motion.plot_series()], window.motion.duration_ms)
    window.set_presentation("C", persist=False)
    window.show()
    window.refine_selected()
    QTest.qWait(30)
    wave = window.plot.wave
    y = round(wave._plot_rect().center().y())
    for before, after in ((100, 250), (800, 700)):
        QTest.mousePress(wave, Qt.MouseButton.LeftButton, pos=QPoint(round(wave._x_for_time(before)), y))
        QTest.mouseMove(wave, QPoint(round(wave._x_for_time(after)), y))
        QTest.mouseRelease(wave, Qt.MouseButton.LeftButton, pos=QPoint(round(wave._x_for_time(after)), y))
    saved = SessionWork.from_dict(read_json(window.catalog.work_path(window.work.asset_id)))
    assert not saved.project.events
    start = saved.clock.map(saved.drafts[0]["reference_start"], inverse=True)
    end = saved.clock.map(saved.drafts[0]["reference_end"], inverse=True)
    assert (start, end) == pytest.approx((250, 700), abs=3)


def test_draft_preview_preserves_nonzero_source_clock_and_event_coordinates(observed):
    window, _ = observed
    reference = window.work.clock.map(0)
    window.work.clock = ClockMap([Anchor(100, reference), Anchor(900, reference + 800)])
    confirmed = window.work.project.add_event(0, 20, 40)
    original = copy.deepcopy(confirmed.to_dict())
    draft = window.work.add_draft(0, reference + 100, reference + 600, [])
    window.refresh_events()
    window.events.selectRow(0)
    window.refine_selected()
    preview = next(e for e in window.plot.wave._events if e["id"] < 0)
    assert (preview["t0"], preview["t1"]) == (200, 700)
    window.edit_plot_event(preview["id"], 250, 650)
    assert (draft["reference_start"], draft["reference_end"]) == (reference + 150, reference + 550)
    assert confirmed.to_dict() == original


@pytest.mark.parametrize("width,height", [(1280, 720), (1600, 1000)])
def test_action_and_save_controls_remain_visible_at_customer_sizes(observed, width, height):
    window, _ = observed
    window.resize(width, height)
    window.show()
    window.event_panel.show()
    window.mark(0)
    QTest.qWait(40)
    controls = [window.mark_button, window.cancel_action_button, window.event_status]
    controls += [b for b in window.findChildren(QPushButton) if b.text() in {"保存", "九轴起止微调", "编辑起止"} and b.isVisible()]
    assert any(b.text() == "保存" for b in controls)
    for control in controls:
        assert control.isVisible() and not control.visibleRegion().isEmpty()
        top_left = control.mapTo(window, QPoint(0, 0))
        bottom_right = control.mapTo(window, control.rect().bottomRight())
        assert window.rect().contains(top_left) and window.rect().contains(bottom_right)
    if folder := os.environ.get("COWMATA_ACTION_TEST_ARTIFACTS"):
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(target / f"action-{width}x{height}.png"))
