"""Regression: startup reminders and cooperative closure across app processes."""

import os
import subprocess
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMainWindow

from cowmata_tailring.app import update_ui as ui
from cowmata_tailring.app import update_worker as worker


@pytest.fixture
def controller(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    window.show()
    value = ui.UpdateController(window, automatic=False)
    value.settings = QSettings(str(tmp_path / "preferences.ini"), QSettings.Format.IniFormat)
    value.option = lambda *_: False
    yield value
    value.pending_job = None
    value.stop.set()
    value.timer.stop()
    value.close_timer.stop()
    if value.notification:
        value.notification.close()
    if value.dialog:
        value.dialog.close()
    window.close()
    app.processEvents()


def test_startup_notice_survives_saved_dismissal(controller):
    update = dict(version="3.9.3", sha256="a" * 64)
    controller.settings.setValue("updates/announced_package", "3.9.3:" + "a" * 64)
    controller._found(update)
    assert controller.notification is not None and controller.notification.isVisible()
    first = controller.notification
    controller._found(dict(update))
    assert controller.notification is first


def test_startup_check_ignores_previous_process_throttle(controller, monkeypatch):
    controller.settings.setValue("updates/last_auto_check", time.time())
    monkeypatch.setattr(
        ui.core, "check_update", lambda *a, **kw: dict(version="3.9.3", sha256="b" * 64)
    )
    controller.startup_check()
    end = time.monotonic() + 3
    while controller.busy and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    assert controller.notification is not None and controller.notification.isVisible()
    assert controller.update["version"] == "3.9.3"


def test_worker_requests_normal_close_only_when_job_authorized(tmp_path):
    calls = []
    probe = tmp_path / "COWMATA-Progress.exe"
    probe.touch()
    root = tmp_path / "app"
    root.mkdir()

    def run(command, timeout=0):
        calls.append([str(x) for x in command])
        return subprocess.CompletedProcess(command, 0, b"", b"")

    worker.request_app_close(root, tmp_path, run, authorized=False)
    assert calls == []
    worker.request_app_close(root, tmp_path, run, authorized=True)
    assert calls == [[str(probe), "--request-close", str(root)]]


@pytest.fixture(scope="module")
def native_launchers(tmp_path_factory):
    if os.name != "nt":
        pytest.skip("Windows launcher integration")
    base = tmp_path_factory.mktemp("native-close")
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        pytest.skip("Windows C# compiler is unavailable")
    repo = Path(__file__).resolve().parents[1]
    launcher = base / "COWMATA-Progress.exe"
    flags = dict(capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    result = subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:winexe",
            "/reference:System.Windows.Forms.dll",
            "/reference:System.Drawing.dll",
            "/reference:System.Web.Extensions.dll",
            "/out:" + str(launcher),
            str(repo / "packaging/Launcher.cs"),
        ],
        **flags,
    )
    assert result.returncode == 0, result.stdout.decode("utf-8", "replace")
    code = base / "Fixture.cs"
    code.write_text(
        """using System;using System.IO;using System.Windows.Forms;
class Fixture{[STAThread] static void Main(string[] args){var f=new Form();f.Text="COWMATA isolated closure test";f.Shown+=(s,e)=>File.WriteAllText(args[0]+".ready","ready");f.FormClosing+=(s,e)=>{File.AppendAllText(args[0]+".asked","close");if(File.Exists(args[0]+".cancel"))e.Cancel=true;else File.WriteAllText(args[0]+".saved","saved");};Application.Run(f);}}
""",
        encoding="utf-8",
    )
    dummy = base / "COWMATA.exe"
    result = subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:winexe",
            "/reference:System.Windows.Forms.dll",
            "/out:" + str(dummy),
            str(code),
        ],
        **flags,
    )
    assert result.returncode == 0, result.stdout.decode("utf-8", "replace")
    return launcher, dummy


@pytest.mark.parametrize("cancel", [False, True])
def test_native_close_preserves_cancel_and_other_installations(tmp_path, native_launchers, cancel):
    import shutil

    launcher, dummy = native_launchers
    targets = []
    for label in ("target", "unrelated"):
        root = tmp_path / label
        root.mkdir()
        shutil.copy2(dummy, root / "COWMATA.exe")
        (root / "package-manifest.json").write_text("{}")
        marker = root / "result"
        if label == "target" and cancel:
            marker.with_suffix(".cancel").touch()
        process = subprocess.Popen(
            [str(root / "COWMATA.exe"), str(marker)], creationflags=subprocess.CREATE_NO_WINDOW
        )
        targets.append((root, marker, process))
    try:
        deadline = time.monotonic() + 5
        while (
            not all(m.with_suffix(".ready").exists() for _, m, _ in targets)
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert all(m.with_suffix(".ready").exists() for _, m, _ in targets)
        root, marker, process = targets[0]
        result = subprocess.run(
            [str(launcher), "--request-close", str(root)],
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0
        deadline = time.monotonic() + 3
        while not marker.with_suffix(".asked").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.with_suffix(".asked").exists()
        if cancel:
            assert process.poll() is None and not marker.with_suffix(".saved").exists()
        else:
            assert (
                process.wait(timeout=3) == 0 and marker.with_suffix(".saved").read_text() == "saved"
            )
        assert targets[1][2].poll() is None
        assert not targets[1][1].with_suffix(".asked").exists()
    finally:
        for _, _, process in targets:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
