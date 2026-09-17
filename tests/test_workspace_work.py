import pytest

from cowmata_tailring.workspace.clocks import Anchor, ClockMap
from cowmata_tailring.workspace.work import SessionWork


def work():
    value = SessionWork("a" * 64)
    value.project.cow_id = "21100"
    value.clock = ClockMap([Anchor(0, 10000), Anchor(1000, 11000)])
    return value


def test_draft_confirmation_and_recalibration_never_moves_labels():
    value = work()
    draft = value.add_draft(0, 10100, 10200, [{"frame_ready": True, "verified_interval": True}])
    event = value.confirm_draft(draft["id"], 1000)
    assert event.t0 == 100 and event.t1 == 200
    assert len(value.training_project().events) == 1
    value.calibrate(500, 10550, {"manual": True})
    assert event.t0 == 100 and event.t1 == 200
    assert event.extras["confirmation"] == "needs_review"
    with pytest.raises(ValueError):
        value.training_project()
    assert value.undo_once()
    assert value.project.events[0].extras["confirmation"] == "confirmed"
    assert value.undo_once(redo=True)
    assert value.project.events[0].extras["confirmation"] == "needs_review"


def test_approximate_draft_survives_without_alignment():
    value = work()
    value.clock = ClockMap()
    draft = value.add_draft(0, 10100, None, [])
    assert SessionWork.from_dict(value.to_dict()).drafts == value.drafts
    with pytest.raises(ValueError):
        value.confirm_draft(draft["id"], 1000)
    assert len(value.drafts) == 1


@pytest.mark.parametrize("reason", ["missing", "cow", "single", "break", "no_frame"])
def test_training_gate(reason):
    value = work()
    evidence = [{"frame_ready": reason != "no_frame", "verified_interval": True}]
    draft = value.add_draft(0, 10100, 10900, evidence)
    if reason == "cow":
        value.project.cow_id = ""
    if reason == "single":
        value.clock = ClockMap([Anchor(0, 10000)])
    if reason == "break":
        value.clock.breaks = [(300, 600)]
    with pytest.raises(ValueError):
        value.confirm_draft(draft["id"], 1000, source_available=reason != "missing")


def test_cross_record_group_preserves_raw_per_source_bounds():
    first, second = work(), work()
    second.asset_id = "b" * 64
    second.clock = ClockMap([Anchor(0, 11000), Anchor(1000, 12000)])
    evidence = [{"frame_ready": True, "verified_interval": True}]
    for value in (first, second):
        draft = value.add_draft(0, 10900, 11100, evidence, group_id="one-action")
        value.confirm_draft(draft["id"], 1000)
    assert (first.project.events[0].t0, first.project.events[0].t1) == (900, 1000)
    assert (second.project.events[0].t0, second.project.events[0].t1) == (0, 100)
    assert first.project.events[0].extras["group_id"] == second.project.events[0].extras["group_id"]
