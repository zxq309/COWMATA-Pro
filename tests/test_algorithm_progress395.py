from cowmata_tailring.algorithms.worker import progress_writer
from cowmata_tailring.workspace import storage


def test_many_small_files_do_not_trigger_one_disk_write_per_file(monkeypatch, tmp_path):
    now = [0.0]
    writes = []
    monkeypatch.setattr(
        storage, "atomic_json", lambda path, value, **kw: writes.append((path, value, kw))
    )
    report = progress_writer(tmp_path / "progress.json", clock=lambda: now[0])
    for n in range(1, 5001):
        report(n, 5000, "Reading JSON")
    assert [w[1]["done"] for w in writes] == [1, 5000]
    assert all(w[2] == {"backup": False} for w in writes)


def test_phase_changes_and_completion_are_never_throttled(monkeypatch, tmp_path):
    now = [0.0]
    writes = []
    monkeypatch.setattr(storage, "atomic_json", lambda path, value, **kw: writes.append(value))
    report = progress_writer(tmp_path / "progress.json", clock=lambda: now[0])
    report(0, 100, "Read")
    report(1, 100, "Read")
    now[0] = 0.3
    report(30, 100, "Read")
    report(31, 100, "Read")
    report(0, 20, "Analyze")
    report(20, 20, "Analyze")
    assert [(x["message"], x["done"]) for x in writes] == [
        ("Read", 0),
        ("Read", 30),
        ("Analyze", 0),
        ("Analyze", 20),
    ]


def test_progress_write_failure_propagates(monkeypatch, tmp_path):
    import pytest

    def fail(*a, **kw):
        raise OSError("Disk unavailable")

    monkeypatch.setattr(storage, "atomic_json", fail)
    with pytest.raises(OSError, match="Disk unavailable"):
        progress_writer(tmp_path / "progress.json")(1, 2, "Read")
