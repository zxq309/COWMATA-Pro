import pytest


def test_batch_delete_and_undo_preserve_remaining_ids():
    from cowmata_tailring.workspace.work import SessionWork

    work = SessionWork("a" * 64)
    for i in range(3):
        work.project.add_event(0, i * 100, i * 100 + 80, event_id=i + 10)
    work.delete_entries([("event", 10), ("event", 12)])
    assert [e.id for e in work.project.events] == [11]
    assert work.undo_once()
    assert [e.id for e in work.project.events] == [10, 11, 12]


def test_batch_relabel_does_not_change_bounds_or_partially_apply():
    from cowmata_tailring.workspace.work import SessionWork

    work = SessionWork("a" * 64)
    work.project.add_event(0, 10, 90, event_id=77)
    work.project.add_event(0, 110, 190, event_id=88)
    work.relabel_entries([("event", 77), ("event", 88)], 1)
    assert [(e.id, e.li, e.t0, e.t1) for e in work.project.events] == [
        (77, 1, 10, 90),
        (88, 1, 110, 190),
    ]
    with pytest.raises(ValueError):
        work.relabel_entries([("event", 77), ("event", 999)], 0)
    assert [e.li for e in work.project.events] == [1, 1]
