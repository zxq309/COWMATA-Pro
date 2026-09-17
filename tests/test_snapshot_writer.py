import json
import threading
import time

import pytest

from cowmata_tailring.workspace.storage import SnapshotWriter


def test_autosave_is_bounded_and_explicit_save_cannot_be_overwritten(tmp_path, monkeypatch):
    writer = SnapshotWriter()
    entered, release = threading.Event(), threading.Event()
    actual = writer.write

    def slow(snapshot):
        entered.set()
        assert release.wait(3)
        actual(snapshot)

    monkeypatch.setattr(writer, "write", slow)
    path = tmp_path / "work.json"
    assert writer.submit([(path, {"event": 1})])
    assert entered.wait(3)
    assert not writer.poll()
    assert not writer.submit([(path, {"event": 2})])
    release.set()
    writer.flush()
    actual([(path, {"event": 3})])
    writer.close()
    assert json.loads(path.read_text()) == {"event": 3}
    assert json.loads(path.with_suffix(".json.bak").read_text()) == {"event": 1}


def test_failed_background_save_is_reported_then_retry_is_possible(tmp_path, monkeypatch):
    writer = SnapshotWriter()
    actual = writer.write

    def broken(_):
        raise OSError("disk unavailable")

    monkeypatch.setattr(writer, "write", broken)
    assert writer.submit([])
    with pytest.raises(OSError, match="disk unavailable"):
        writer.flush()
    monkeypatch.setattr(writer, "write", actual)
    path = tmp_path / "recovered.json"
    assert writer.submit([(path, {"event": 2})])
    writer.close()
    assert json.loads(path.read_text()) == {"event": 2}


def test_window_autosave_detaches_work_and_closes_with_latest_changes(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.catalog import Catalog
    from cowmata_tailring.workspace.modern_window import MainWindow
    from cowmata_tailring.workspace.work import SessionWork
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.catalog = Catalog(tmp_path)
    window.work = SessionWork("a" * 64)
    window.work.project.cow_id = "before"
    entered, release = threading.Event(), threading.Event()
    captured = []
    actual = window.snapshot_writer.write

    def slow(snapshot):
        captured.extend(snapshot)
        entered.set()
        assert release.wait(3)
        actual(snapshot)

    monkeypatch.setattr(window.snapshot_writer, "write", slow)
    window.dirty = True
    window.auto_save()
    assert entered.wait(3)
    window.work.project.cow_id = "after"
    window.dirty = True
    window.auto_save()  # no backlog or overwrite of the in-flight snapshot
    assert captured[0][1]["project"]["cow_id"] == "before"
    release.set()
    path = window.catalog.work_path(window.work.asset_id)
    window.close()
    deadline = time.monotonic() + 3
    while not window._closed and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert window._closed
    assert json.loads(path.read_text(encoding="utf-8"))["project"]["cow_id"] == "after"
    assert not window.dirty
    window.catalog.close()
    app.processEvents()


def test_failed_save_refuses_window_close(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.catalog import Catalog
    from cowmata_tailring.workspace.modern_window import MainWindow
    from cowmata_tailring.workspace.work import SessionWork
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.catalog = Catalog(tmp_path)
    window.work = SessionWork("b" * 64)
    actual = window.snapshot_writer.write

    def broken(_):
        raise OSError("test disk error")

    monkeypatch.setattr(window.snapshot_writer, "write", broken)
    window.dirty = True
    window.auto_save()
    deadline = time.monotonic() + 3
    while not window.snapshot_writer.pending.done() and time.monotonic() < deadline:
        time.sleep(.01)
    assert not window.close()
    assert not window._closed and window.dirty
    monkeypatch.setattr(window.snapshot_writer, "write", actual)
    window.close()
    deadline = time.monotonic() + 3
    while not window._closed and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert window._closed
    window.catalog.close()
    app.processEvents()
