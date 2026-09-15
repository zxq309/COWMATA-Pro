import copy
from types import SimpleNamespace

import pytest

from cowmata_tailring.workspace.clocks import Anchor, ClockMap
from cowmata_tailring.workspace.work import SessionWork


def aligned_work():
    work = SessionWork("a" * 64)
    work.project.cow_id = "24178"
    work.align_once(500, 10500, {"frame_ready": True}, 2000)
    return work


def test_one_observation_confirms_and_exports_across_current_record():
    work = aligned_work()
    assert len(work.clock.anchors) == 1
    assert work.clock.map(1700) == 11700
    assert work.clock.map(11700, inverse=True) == 1700
    for start, end in [(10000, 10400), (11600, 12000)]:
        draft = work.add_draft(0, start, end, [{"frame_ready": True, "verified_interval": True}])
        event = work.confirm_draft(draft["id"], 2000)
        assert event.extras["alignment_quality"] == "offset"
    restored = SessionWork.from_dict(work.to_dict())
    assert restored.clock.quality(1900) == "offset"
    assert len(restored.training_project().events) == 2
    assert restored.training_project().align["workspaceClock"]["offset_range"] == [0, 2000]
    assert not restored.clock.is_calibrated(-1)
    assert not restored.clock.is_calibrated(2001)


@pytest.mark.parametrize("reason", ["break", "no_frame", "unverified", "device", "old_single"])
def test_one_point_does_not_bypass_unconfirmed_data(reason):
    work = aligned_work()
    if reason == "break":
        work.clock.breaks = [(300, 600)]
    if reason in {"device", "old_single"}:
        work.clock = ClockMap([Anchor(0, 10000)], basis="device_clock" if reason == "device" else "manual")
    draft = work.add_draft(0, 10100, 10900,
        [{"frame_ready": reason != "no_frame", "verified_interval": reason != "unverified"}])
    with pytest.raises(ValueError):
        work.confirm_draft(draft["id"], 2000)


def test_realign_replaces_offset_and_preserves_labels_history_and_undo():
    work = aligned_work()
    draft = work.add_draft(0, 10100, 10200, [{"frame_ready": True, "verified_interval": True}])
    event = work.confirm_draft(draft["id"], 2000)
    before = copy.deepcopy(work.to_dict())
    work.align_once(1700, 12000, {}, 2000)
    assert len(work.clock.anchors) == 1
    assert (event.t0, event.t1) == (100, 200)
    assert event.extras["confirmation"] == "needs_review"
    assert work.mapping_history[-1] == before["clock"]
    assert work.undo_once()
    assert work.to_dict() == before


def test_optional_drift_keeps_observed_point_but_requires_two_real_points():
    clock = aligned_work().clock.with_anchor(1800, 11820, {})
    assert clock.offset_range is None
    assert len(clock.anchors) == 2
    assert clock.quality(1500) == "interpolated"
    assert clock.quality(0) == "extrapolated"
    assert ClockMap.from_dict(clock.to_dict()).map(1500) == clock.map(1500)


@pytest.fixture
def window(qt_application, monkeypatch):
    from cowmata_tailring.workspace.modern_window import MainWindow
    window = MainWindow()
    window.board.timer.stop()
    window.save_timer.stop()
    window.source_timer.stop()
    window.work = SessionWork("a" * 64)
    window.work.clock = ClockMap([Anchor(0, 10000)], basis="device_clock")
    window.motion = SimpleNamespace(duration_ms=2000)
    window.imu_ms = 500
    window.board.main_camera = "A"
    monkeypatch.setattr(window, "writable_work", lambda: True)
    monkeypatch.setattr(window, "refresh_events", lambda: None)
    monkeypatch.setattr(window, "save_current", lambda *a, **k: None)
    monkeypatch.setattr(window, "request_record_videos", lambda: None)
    monkeypatch.setattr(window.board, "seek", lambda *a: None)
    monkeypatch.setattr(window.board, "play", lambda *a: None)
    monkeypatch.setattr(window.board, "evidence", lambda: [{"camera": "A", "frame_ready": True, "reference_ms": 10700}])
    window.link.setChecked(True)
    yield window
    window.work = window.motion = None
    window.close()
    qt_application.processEvents()


def test_actual_alignment_button_unlocks_controls_and_finishes_with_one_point(window):
    before = window.work.clock.to_dict()
    window.alignment_button.click()
    assert not window.linked and not window.link.isEnabled()
    assert not window.alignment_controls.isHidden()
    assert window.work.clock.to_dict() == before
    window.alignment_apply.click()
    assert len(window.work.clock.anchors) == 1
    assert window.work.clock.map(1000) == 11200
    assert window.work.clock.is_calibrated(1900)
    assert window.linked and window.link.isEnabled()
    assert window.alignment_controls.isHidden()
    assert window.alignment_button.text() == "重新对齐"


def test_cancel_alignment_restores_link_without_changing_calibration(window):
    before = window.work.clock.to_dict()
    window.pin()
    window.cancel_alignment()
    assert window.work.clock.to_dict() == before
    assert window.linked and window.link.isEnabled()


def test_waiting_frame_cannot_commit_or_leave_alignment_mode(window, monkeypatch):
    before = window.work.clock.to_dict()
    monkeypatch.setattr(window.board, "evidence", lambda: [])
    window.pin()
    window.complete_alignment()
    assert window.work.clock.to_dict() == before
    assert not window.alignment_controls.isHidden()
    assert "尚未到位" in window.alignment_tip.text()


def test_changed_record_cannot_receive_previous_alignment(window):
    window.pin()
    replacement = SessionWork("b" * 64)
    window.work = replacement
    window.complete_alignment()
    assert not replacement.clock.anchors
    assert window.alignment_controls.isHidden()


def test_advanced_alignment_table_preserves_one_point_mode(qt_application):
    from cowmata_tailring.workspace.dialogs import MappingDialog
    clock = aligned_work().clock
    dialog = MappingDialog(clock)
    dialog.validate()
    assert dialog.value.offset_range == clock.offset_range
    assert dialog.value.map(1900) == clock.map(1900)
    dialog.close()


def test_camera_table_preserves_distinct_subsecond_points(qt_application):
    from cowmata_tailring.workspace.dialogs import MappingDialog
    clock = ClockMap([Anchor(500, 10500, {"frame": 1}), Anchor(800, 10800, {"frame": 2})])
    dialog = MappingDialog(clock, camera=True)
    dialog.validate()
    assert dialog.value.anchors == clock.anchors
    dialog.close()


def test_current_one_point_record_can_continue_but_does_not_calibrate_the_next():
    from cowmata_tailring.workspace.coverage import continuation_target
    work = aligned_work()
    row = {"kind": "imu", "state": "ready", "asset_id": work.asset_id,
           "metadata": {"device": "D", "duration_ms": 2000}}
    target, reason = continuation_target([row], "previous", "D", "24178", 11900, lambda _: work.to_dict())
    assert target == row and reason == "ready"
    work.clock = ClockMap([Anchor(0, 10000)], basis="device_clock")
    target, _ = continuation_target([row], "previous", "D", "24178", 11900, lambda _: work.to_dict())
    assert target is None
