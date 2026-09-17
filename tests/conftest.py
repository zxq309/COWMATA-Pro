"""Window teardown chooses Save and finishes requested asynchronous closes."""
import sys
import time

import pytest


@pytest.fixture(scope="session", autouse=True)
def qt_application():
    """Keep the Qt application alive across module fixtures and queued calls."""
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(scope="session")
def qt_window_registry():
    """Own Python wrappers until Qt-thread teardown, including cancelled closes."""
    return {}


@pytest.fixture(autouse=True)
def save_when_test_closes_window(monkeypatch, qt_window_registry):
    module = sys.modules.get("cowmata_tailring.workspace.window")
    if module is not None:
        monkeypatch.setattr(module.MainWindow, "confirm_close", lambda self: "save")
        original_init = module.MainWindow.__init__

        def tracked_init(window, *args, **kwargs):
            original_init(window, *args, **kwargs)
            qt_window_registry[id(window)] = window

        monkeypatch.setattr(module.MainWindow, "__init__", tracked_init)
    yield
    if module is None:
        return
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from shiboken6 import isValid

    app = QApplication.instance()
    if app is None:
        return
    # closeEvent first ignores the event while a durable snapshot is written,
    # then retries through QTimer. A bare window.close() at a test's end does
    # not finish that transaction. Keep pumping before monkeypatch is undone
    # or the next test allocates tmp_path and collects the previous Qt graph.
    # Windows deliberately kept open (for example Cancel/error tests) are not
    # asked to close here, and no save/discard choice is changed.
    # Keep the constructor's actual wrapper across the test-body boundary.
    # Enumerating topLevelWidgets() here must not recreate wrappers for global
    # native widget pointers while the final snapshot thread is still active.
    for key, window in list(qt_window_registry.items()):
        if not isValid(window):
            del qt_window_registry[key]
    pending = [window for window in qt_window_registry.values()
               if window._closing_requested and not window._closed]
    deadline = time.monotonic() + 5
    while pending and time.monotonic() < deadline:
        app.processEvents()
        pending = [window for window in pending if not window._closed]
        if pending:
            time.sleep(.001)
    if pending:
        states = [{"closing_requested": window._closing_requested,
                   "close_choice": window._close_choice,
                   "dirty": window.dirty,
                   "snapshot_pending": window.snapshot_writer.pending is not None,
                   "snapshot_done": window.snapshot_writer.pending.done()
                   if window.snapshot_writer.pending is not None else None,
                   "root": str(window.catalog.root) if window.catalog else None}
                  for window in pending]
        pytest.fail(f"Requested window close did not finish within 5 seconds: {states}")
    # QWidget.close() hides a completed window but does not destroy its native
    # tree. Leaving many closed windows to cyclic GC carried tens of thousands
    # of Qt objects into later tests. Dispose only completed requested closes;
    # windows deliberately left open after Cancel/error remain available.
    completed = [(key, window) for key, window in qt_window_registry.items()
                 if window._closing_requested and window._closed]
    for _, window in completed:
        window.deleteLater()
    # processEvents() alone does not deliver DeferredDelete outside app.exec().
    # Explicitly drain it here while fixture monkeypatches are still valid.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for key, window in completed:
        assert not isValid(window), "Completed test window was not destroyed on the Qt thread"
        del qt_window_registry[key]
