"""Window teardown chooses Save and finishes requested asynchronous closes."""
import sys
import time

import pytest


@pytest.fixture(autouse=True)
def save_when_test_closes_window(monkeypatch):
    module = sys.modules.get("cowmata_tailring.workspace.window")
    if module is not None:
        monkeypatch.setattr(module.MainWindow, "confirm_close", lambda self: "save")
    yield
    if module is None:
        return
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    # closeEvent first ignores the event while a durable snapshot is written,
    # then retries through QTimer. A bare window.close() at a test's end does
    # not finish that transaction. Keep pumping before monkeypatch is undone
    # or the next test allocates tmp_path and collects the previous Qt graph.
    # Windows deliberately kept open (for example Cancel/error tests) are not
    # asked to close here, and no save/discard choice is changed.
    pending = [window for window in app.topLevelWidgets()
               if isinstance(window, module.MainWindow)
               and window._closing_requested and not window._closed]
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
    for window in app.topLevelWidgets():
        if isinstance(window, module.MainWindow) and window._closing_requested and window._closed:
            window.deleteLater()
    # processEvents() alone does not deliver DeferredDelete outside app.exec().
    # Explicitly drain it here while fixture monkeypatches are still valid.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
