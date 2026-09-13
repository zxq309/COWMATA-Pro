# ruff: noqa: F811 -- pytest fixture injection
import os
import sys
import time

import pytest
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication
from test_updates import transaction  # noqa: F401


def pump(predicate, timeout=5):
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    assert predicate()


def test_closing_hidden_organizer_stops_child_and_allows_main_exit(tmp_path):
    from cowmata_tailring.workspace.modern_window import MainWindow

    window = MainWindow()
    window.confirm_close = lambda: "save"
    window.open_organization(1)
    dialog = window._organization_window
    dialog.job = tmp_path
    dialog._active_task = True
    process = QProcess(dialog)
    dialog.process = process
    process.finished.connect(dialog.process_finished)
    script = "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]);\nwhile not p.exists(): time.sleep(.02)"
    process.start(sys.executable, ["-B", "-c", script, str(tmp_path / "cancel")])
    assert process.waitForStarted(3000)
    try:
        dialog.close()
        assert (tmp_path / "cancel").exists(), "Closing only hid the running task"
        window.close()
        pump(lambda: window._closed and process.state() == QProcess.ProcessState.NotRunning)
    finally:
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()
            process.waitForFinished(3000)
        dialog._active_task = False
        window.close()
        pump(lambda: window._closed)


def test_restart_occurs_before_slow_backup_cleanup(transaction, monkeypatch):
    from cowmata_tailring.app import update_worker as w

    _, root, _, job, runner = transaction
    calls = []
    launch = getattr(w, "start_updated_app", None)
    assert callable(launch), "No early restart stage exists"
    monkeypatch.setattr(w, "start_updated_app", lambda *a: calls.append("restart"))
    original = w.remove_owned

    def cleanup(path):
        assert calls == ["restart"], "Backup deletion still blocks reopening"
        calls.append("cleanup")
        original(path)

    monkeypatch.setattr(w, "remove_owned", cleanup)
    result = w.install(job, runner=runner, registration=lambda *_: True, unregister=lambda *_: None)
    assert result["phase"] == "complete" and calls == ["restart", "cleanup"]


def test_post_update_launch_skips_network_gate_once(monkeypatch):
    from cowmata_tailring import __version__
    from cowmata_tailring.app import update_ui as ui

    monkeypatch.setenv("COWMATA_POST_UPDATE_VERSION", __version__)
    monkeypatch.setattr(
        ui, "StartupUpdateDialog", lambda: pytest.fail("Update launch repeated the network gate")
    )
    assert ui.verify_startup_update()
    assert "COWMATA_POST_UPDATE_VERSION" not in os.environ


def test_update_integrity_check_reports_file_progress(transaction):
    from cowmata_tailring.app import update_worker as w

    _, _, payload, _, _ = transaction
    states = []
    w.inventory(payload, verify=True, progress=lambda n, total, path: states.append((n, total)))
    assert states and states[-1][0] == states[-1][1] and states[-1][1] > 0


def test_pending_async_save_does_not_cancel_scheduled_update(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMainWindow

    from cowmata_tailring.app.update_ui import UpdateController

    window = QMainWindow()
    window.show()
    window._closing_requested = True
    controller = UpdateController(window, automatic=False)
    controller.pending_job = tmp_path / "job.json"
    monkeypatch.setattr(QApplication, "closeAllWindows", lambda: None)
    monkeypatch.setattr(QApplication, "topLevelWidgets", lambda: [window])
    controller._close_for_update()
    assert controller.pending_job == tmp_path / "job.json"
    assert controller.close_timer.isActive()
    controller.pending_job = None
    controller.close_timer.stop()
    window.close()


def test_stuck_child_can_be_stopped_without_waiting_forever(tmp_path):
    from cowmata_tailring.workspace.modern_window import MainWindow

    window = MainWindow()
    window.confirm_close = lambda: "save"
    window.open_organization(1)
    dialog = window._organization_window
    dialog.job = tmp_path
    dialog._active_task = True
    process = QProcess(dialog)
    dialog.process = process
    process.finished.connect(dialog.process_finished)
    process.start(sys.executable, ["-B", "-c", "import time;time.sleep(60)"])
    assert process.waitForStarted(3000)
    try:
        dialog.close()
        dialog._shutdown_started = time.monotonic() - 6
        window.close()
        pump(lambda: window._closed and process.state() == QProcess.ProcessState.NotRunning, 8)
        assert (tmp_path / "cancel").exists()
    finally:
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()
            process.waitForFinished(3000)
        dialog._active_task = False
        window.close()
        pump(lambda: window._closed)
