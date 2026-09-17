"""Ordered lifecycle probes across real pytest fixture teardown boundaries."""
import gc
import threading
import weakref

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.storage import read_json
from cowmata_tailring.workspace.work import SessionWork

# Keep wrappers alive deliberately: teardown must release the native widget
# tree, independently of Python's nondeterministic cyclic garbage collection.
_windows = {}
_async_probe = {}


def test_completed_close_is_requested_for_next_test():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    _windows["closed"] = window
    window.close()
    app.processEvents()
    assert window._closing_requested and window._closed


def test_previous_completed_close_released_native_widgets(qt_window_registry):
    window = _windows.pop("closed")
    assert not isValid(window), "Closed test windows must not leave native Qt trees in later tests"
    assert id(window) not in qt_window_registry


def test_cancelled_close_is_left_open_for_next_test():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    _windows["cancelled"] = window
    window._close_choice = "cancel"
    window.close()
    app.processEvents()
    assert not window._closing_requested and not window._closed


def test_previous_cancelled_close_remains_usable(qt_window_registry):
    window = _windows.pop("cancelled")
    assert isValid(window)
    assert qt_window_registry[id(window)] is window
    assert not window._closed
    window.records.addItem("still usable after cancelled close")
    assert window.records.count() == 1
    window._close_choice = "save"
    window.close()


def test_teardown_does_not_enumerate_global_widgets(monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.close()
    app.processEvents()
    monkeypatch.setattr(QApplication, "topLevelWidgets", lambda *_: pytest.fail(
        "Teardown must use its owned window registry, not wrap global native widget pointers"))


def test_async_close_keeps_original_wrapper_through_worker_gc(tmp_path, qt_window_registry):
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.catalog = Catalog(tmp_path, stability_seconds=0)
    window.work = SessionWork("a" * 64)
    window.work.project.cow_id = "00047"
    window.dirty = True
    assert qt_window_registry[id(window)] is window
    window_ref = weakref.ref(window)
    release = threading.Event()
    write = window.snapshot_writer.write
    _async_probe.update(window=window_ref, key=id(window), gui_thread=threading.get_ident(),
                        path=window.catalog.work_path(window.work.asset_id), destroyed=[])
    window.destroyed.connect(lambda: _async_probe["destroyed"].append(threading.get_ident()))

    def write_after_gc(snapshot):
        assert release.wait(2)
        gc.collect()  # No Qt API on the writer; the registry owns the live wrapper.
        assert window_ref() is not None
        write(snapshot)

    window.snapshot_writer.write = write_after_gc
    window.close()
    assert window._closing_requested and not window._closed
    QTimer.singleShot(20, release.set)
    assert app is not None
    # Deliberately let the function-local window reference end while the
    # snapshot is pending. The following test checks the teardown boundary.


def test_async_close_saved_and_destroyed_on_gui_thread(qt_window_registry):
    window = _async_probe["window"]()
    assert window is None or not isValid(window)
    assert _async_probe["key"] not in qt_window_registry
    assert read_json(_async_probe["path"])["project"]["cow_id"] == "00047"
    assert _async_probe["destroyed"] == [_async_probe["gui_thread"]]
