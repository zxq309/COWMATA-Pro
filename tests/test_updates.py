import hashlib
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from cowmata_tailring.app import update_core as core
from cowmata_tailring.app import update_worker as worker


class Response(io.BytesIO):
    def __init__(self, content, status=200, headers=None):
        super().__init__(content)
        self.status = status
        self.headers = headers or {}


def release(version="3.1.0-rc.3", preview=True):
    name = f"COWMATA-Annotator-{version}-Setup.exe"
    exe = b"test installer"
    doc = {"schema": 1, "product": "cowmata-annotator", "version": version, "installer": name,
           "size": len(exe), "sha256": hashlib.sha256(exe).hexdigest(),
           "package_sha256": "a" * 64, "unpacked_size": 400}
    descriptor = json.dumps(doc).encode()
    base = f"https://github.com/{core.REPO}/releases/download/v{version}/"
    assets = [{"name": n, "size": len(b), "digest": "sha256:" + hashlib.sha256(b).hexdigest(),
               "state": "uploaded", "browser_download_url": base + n}
              for n, b in ((name, exe), (core.DESCRIPTOR, descriptor))]
    return {"tag_name": "v" + version, "prerelease": preview, "draft": False, "assets": assets}, descriptor, exe


def transport(releases, descriptor):
    return lambda url: Response(json.dumps(releases).encode() if "api.github.com" in url else descriptor)


@pytest.mark.parametrize("older,newer", [
    ("3.1.0rc2", "3.1.0-rc.3"), ("3.1.0-rc.99", "3.1.0"),
    ("3.1.0-rc.1-r3", "3.1.0rc2"),
    ("3.1.0", "3.1.1"), ("3.9.9", "3.10.0"), ("3.1.0beta9", "3.1.0rc1"),
])
def test_semantic_versions_not_filenames_or_upload_dates(older, newer):
    assert core.version_key(older) < core.version_key(newer)


def test_release_protocol_channel_and_downgrade():
    row, desc, _ = release()
    opener = transport([row], desc)
    assert core.check_update("3.1.0rc2", opener=opener)["version"] == "3.1.0-rc.3"
    assert core.check_update("3.1.0rc3", opener=opener) is None
    assert core.check_update("3.1.0", opener=opener) is None
    assert core.check_update("3.1.0rc2", "stable", opener=opener) is None
    row["draft"] = True
    assert core.check_update("3.0.0", opener=transport([row], desc)) is None


def test_old_client_skips_all_intermediate_releases_and_never_falls_back():
    old, _, _ = release("3.1.0", False)
    middle, _, _ = release("3.2.0", False)
    newest, desc, _ = release("3.10.0", False)
    preview, _, _ = release("4.0.0-rc.1", True)
    rows = [middle, preview, newest, old]  # API order is not version order.
    assert core.check_update("3.0.0", "stable", opener=transport(rows, desc))["version"] == "3.10.0"
    newest["assets"] = []
    with pytest.raises(ValueError, match="自动更新清单"):
        core.check_update("3.0.0", "stable", opener=transport(rows, desc))


@pytest.fixture(autouse=True)
def isolated_update_settings(monkeypatch, tmp_path):
    from PySide6.QtCore import QSettings

    from cowmata_tailring.app import update_ui
    settings = QSettings(str(tmp_path / "updates.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(update_ui, "QSettings", lambda: settings)


@pytest.mark.parametrize("change", ["missing", "digest", "url", "size", "descriptor", "version"])
def test_invalid_releases_never_produce_install_job(change):
    row, desc, _ = release()
    if change == "missing":
        row["assets"] = row["assets"][:1]
    elif change == "digest":
        row["assets"][0]["digest"] = None
    elif change == "url":
        row["assets"][0]["browser_download_url"] = "https://evil.invalid/setup.exe"
    elif change == "size":
        row["assets"][0]["size"] += 1
    elif change == "version":
        row["tag_name"] = "v4.0.0"
    else:
        desc += b"tamper"
    with pytest.raises(ValueError):
        core.check_update("3.0.0", opener=transport([row], desc))


@pytest.mark.parametrize("url", [
    "http://github.com/zxq309/cattle-tail-ring-annotator/releases/download/v1/a.exe",
    "https://github.com/other/repo/releases/download/v1/a.exe",
    "https://github.com.evil.invalid/zxq309/cattle-tail-ring-annotator/releases/download/v1/a.exe",
    "file:///C:/setup.exe", "https://user:pass@github.com/a", "https://api.github.com:444/repos/a",
])
def test_untrusted_hosts_and_schemes_refused(url):
    with pytest.raises(ValueError):
        core.valid_url(url, redirect=True)


def update_for(data):
    return {"name": "COWMATA-Annotator-3.1.0-rc.3-Setup.exe", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "url": f"https://github.com/{core.REPO}/releases/download/v3.1.0-rc.3/setup.exe"}


@pytest.mark.parametrize("range_supported", [True, False])
def test_partial_download_resume_or_safe_restart(tmp_path, range_supported):
    data = b"installer bytes" * 30
    update = update_for(data)
    (tmp_path / (update["name"] + ".part")).write_bytes(data[:10])
    calls = []
    def opener(url, headers):
        calls.append(headers)
        return Response(data[10:] if range_supported else data, 206 if range_supported else 200,
                        {"Content-Range": f"bytes 10-{len(data)-1}/{len(data)}"} if range_supported else {})
    path = core.download(update, tmp_path, opener=opener)
    assert path.read_bytes() == data and calls == [{"Range": "bytes=10-"}]
    assert core.download(update, tmp_path, opener=lambda *_: pytest.fail("Already downloaded")) == path


@pytest.mark.parametrize("failure", ["corrupt", "truncated", "oversized", "range", "cancelled"])
def test_download_failures_never_execute_or_publish(tmp_path, failure):
    data = b"real installer"
    update = update_for(data)
    payload = {"corrupt": b"x" * len(data), "truncated": data[:3], "oversized": data + b"x"}.get(failure, data)
    def opener(*_):
        return Response(payload, 206 if failure == "range" else 200)
    with pytest.raises((ValueError, InterruptedError)):
        core.download(update, tmp_path, cancelled=lambda: failure == "cancelled", opener=opener)
    assert not (tmp_path / update["name"]).exists()


def package(root, version):
    root.mkdir(parents=True)
    (root / "runtime").mkdir()
    (root / "runtime/python.exe").write_bytes(b"fixture Python")
    (root / "COWMATA.exe").write_bytes(b"fixture EXE")
    (root / "code.py").write_text("VERSION=" + repr(version))
    rows = [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size, "sha256": core.digest(p)}
            for p in root.rglob("*") if p.is_file()]
    worker.write_json(root / "package-manifest.json", {"files": rows})
    (root / "COWMATA.install-id").write_text("COWMATA-" + version)
    (root / "Uninstall.exe").write_bytes(b"fixture uninstall")
    return core.digest(root / "package-manifest.json")


@pytest.fixture
def transaction():
    base = Path(tempfile.mkdtemp(prefix="cma-transaction-")).resolve()
    target, payload, job_dir = base / "原位置 标注工具", base / "payload", base / "job"
    package(target, "3.1.0-rc.2")
    sha = package(payload, "3.1.0-rc.3")
    job_dir.mkdir()
    setup = base / "setup.exe"
    setup.write_bytes(b"fixture setup")
    job = {"root": str(target), "setup": str(setup), "job_dir": str(job_dir), "desktop": False,
           "update": {"version": "3.1.0-rc.3", "size": setup.stat().st_size, "sha256": core.digest(setup),
                      "package_sha256": sha, "unpacked_size": 100}}
    def runner(command, timeout=0):
        if "/STAGE=1" in command:
            dest = Path(command[-1][3:])
            shutil.copytree(payload, dest)
        return subprocess.CompletedProcess(command, 0, b"3.1.0rc3\n", b"")
    return base, target, payload, job, runner


def execute(job, runner):
    return worker.install(job, runner=runner, registration=lambda *_: True, unregister=lambda *_: None, restart=False)


def test_in_place_transaction_removes_old_owned_files_only(transaction):
    base, target, _, job, runner = transaction
    data = base / "customer-label.json"
    data.write_bytes(b"customer annotation data")
    before = data.read_bytes()
    result = execute(job, runner)
    assert result["phase"] == "complete"
    assert "3.1.0-rc.3" in (target / "code.py").read_text()
    assert not Path(result["backup"]).exists()
    assert not (target / worker.LOCK).exists()
    assert data.read_bytes() == before


@pytest.mark.parametrize("case", ["corrupt_payload", "registration", "unowned", "bad_installer", "bad_smoke"])
def test_transaction_failure_preserves_old_version(transaction, case):
    _, target, payload, job, runner = transaction
    before = (target / "code.py").read_bytes()
    if case == "corrupt_payload":
        (payload / "code.py").write_text("corrupt")
    elif case == "unowned":
        (target / "客户标签.json").write_text("KEEP")
    elif case == "bad_installer":
        Path(job["setup"]).write_bytes(b"CORRUPT")
    def fail(command, timeout=0):
        if (case == "registration" and "/REGISTERONLY=1" in command) or (case == "bad_smoke" and "-c" in command):
            return subprocess.CompletedProcess(command, 7, b"", b"failure")
        return runner(command, timeout)
    with pytest.raises((ValueError, RuntimeError)):
        execute(job, fail)
    assert target.exists() and (target / "code.py").read_bytes() == before
    assert not (target / worker.LOCK).exists()
    if case == "unowned":
        assert (target / "客户标签.json").read_text() == "KEEP"
    if case == "registration":
        assert json.loads((Path(job["job_dir"]) / "result.json").read_text(encoding="utf-8"))["phase"] == "rolled_back"
    if case in {"bad_smoke", "corrupt_payload"}:
        assert json.loads((Path(job["job_dir"]) / "result.json").read_text(encoding="utf-8"))["phase"] == "failed_before_swap"


def test_prepare_and_exit_do_not_launch_installer_before_quit(monkeypatch):
    from PySide6.QtWidgets import QApplication, QMainWindow

    from cowmata_tailring.app.update_ui import UpdateController
    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    updater = UpdateController(window, automatic=False)
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a))
    updater.pending_job = Path("C:/test/job/job.json")
    assert not calls
    updater.on_exit()
    assert len(calls) == 1 and "pythonw.exe" in calls[0][0][0]
    updater.pending_job = None
    window.close()
    assert app is not None


def test_ready_download_does_not_stop_new_release_checks(monkeypatch):
    from PySide6.QtWidgets import QApplication, QMainWindow

    from cowmata_tailring.app.update_ui import UpdateController
    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    updater = UpdateController(window, automatic=False)
    updater.option = lambda *_: True
    calls = []
    monkeypatch.setattr(updater, "check", lambda: calls.append("check"))
    updater.update = {"version": "3.1.0rc3", "sha256": "a" * 64}
    updater.setup = Path("C:/cache/installer.exe")
    updater.auto_check()
    assert calls == ["check"]
    updater._found(dict(updater.update))
    assert updater.setup is not None  # No redundant same-version download.
    updater.option = lambda *_: False
    updater._found({"version": "3.1.0rc4", "sha256": "b" * 64})
    assert updater.setup is None and updater.update["version"] == "3.1.0rc4"
    window.close()
    assert app is not None


def test_new_version_reminder_is_nonmodal_and_not_repeated(monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMainWindow

    from cowmata_tailring.app.update_ui import UpdateController
    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    updater = UpdateController(window, automatic=False)
    updater.option = lambda *_: False
    update = {"version": "3.1.1", "sha256": "a" * 64}
    updater._found(update)
    first = updater.notification
    assert first.isVisible() and first.windowModality() == Qt.WindowModality.NonModal
    assert "3.1.1" in first.text()
    updater._found(dict(update))
    assert updater.notification is first
    second_window = QMainWindow()
    second = UpdateController(second_window, automatic=False)
    second.option = lambda *_: False
    second._found(dict(update))
    assert second.notification is None  # Includes reopening the application.
    second_window.close()
    window.close()
    first.close()
    app.processEvents()


@pytest.fixture
def update_controller(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication, QMainWindow

    from cowmata_tailring.app import update_ui
    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    updater = update_ui.UpdateController(window, automatic=False)
    updater.cache = tmp_path
    updater.option = lambda *_: False
    monkeypatch.setattr(updater, "announce", lambda *_: None)

    def run(function, signal):
        updater.busy = True
        try:
            signal.emit(function())
        except Exception as exc:
            updater._failed(str(exc))

    monkeypatch.setattr(updater, "_task", run)
    updater.update = {"version": "3.3.0", "sha256": "a" * 64, "name": "old.exe"}
    yield updater
    updater.pending_job = None
    window.close()
    app.processEvents()


@pytest.mark.parametrize("newer_at", ["before_download", "after_download", "same_version_repack", "withdrawn", "unchanged"])
def test_download_only_marks_current_latest_package_ready(update_controller, monkeypatch, tmp_path, newer_at):
    updater = update_controller
    original = dict(updater.update)
    latest = {"version": "3.4.0", "sha256": "b" * 64, "name": "latest.exe"}
    if newer_at == "same_version_repack":
        latest["version"] = original["version"]
    if newer_at == "withdrawn":
        latest = None
    if newer_at == "unchanged":
        latest = original
    checks = iter([latest] if newer_at == "before_download" else [original, latest])
    monkeypatch.setattr(core, "check_update", lambda *_: next(checks))
    downloads = []

    def download(update, *_):
        downloads.append(update)
        return tmp_path / update["name"]

    monkeypatch.setattr(core, "download", download)
    updater.start_download()
    assert updater.update == latest
    assert bool(updater.setup) == (newer_at == "unchanged")
    assert len(downloads) == (0 if newer_at == "before_download" else 1)
    assert not updater.busy and updater.pending_job is None


@pytest.mark.parametrize("check_result", ["newer", "offline", "unchanged"])
def test_install_rechecks_latest_before_preparing_or_closing(update_controller, monkeypatch, tmp_path, check_result):
    from PySide6.QtWidgets import QMessageBox

    from cowmata_tailring.app import update_ui
    updater = update_controller
    updater.setup = tmp_path / "old.exe"
    latest = dict(updater.update) if check_result == "unchanged" else {
        "version": "3.4.0", "sha256": "b" * 64, "name": "newest.exe"}

    def check(*_):
        if check_result == "offline":
            raise OSError("offline")
        return latest

    monkeypatch.setattr(core, "check_update", check)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    prepared, closed = [], []
    monkeypatch.setattr(update_ui, "prepare_job", lambda *args: prepared.append(args[2]) or tmp_path / "job.json")
    monkeypatch.setattr(updater, "_prepared", lambda job: closed.append(job))
    updater.install()
    assert len(prepared) == len(closed) == (1 if check_result == "unchanged" else 0)
    if check_result == "newer":
        assert updater.update == latest and updater.setup is None
    assert not updater.busy


def test_cancelled_window_close_does_not_queue_installer_for_later(update_controller, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication
    updater = update_controller
    updater.window.show()
    updater.pending_job = tmp_path / "job.json"
    monkeypatch.setattr(QApplication, "closeAllWindows", lambda: None)  # User chose to continue annotating.
    updater._close_for_update()
    assert updater.pending_job is None
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a))
    updater.on_exit()
    assert not calls


@pytest.fixture
def startup_gate(monkeypatch):
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.app import update_ui
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(update_ui.QTimer, "singleShot", lambda *_: None)
    dialog = update_ui.StartupUpdateDialog()
    updater = dialog.updater

    def run(function, signal):
        updater.busy = True
        try:
            signal.emit(function())
        except Exception as exc:
            updater._failed(str(exc))

    monkeypatch.setattr(updater, "_task", run)
    yield dialog
    updater.pending_job = None
    updater.stop.set()
    app.aboutToQuit.disconnect(updater.on_exit)
    dialog.close()


def test_startup_allows_annotation_only_after_latest_check(startup_gate, monkeypatch):
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(core, "check_update", lambda *_: None)
    startup_gate.updater.check()
    assert startup_gate.result() == QDialog.DialogCode.Accepted
    assert startup_gate.updater.pending_job is None


def test_startup_forces_automatic_latest_update_despite_old_preferences(startup_gate, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QDialog, QMessageBox

    from cowmata_tailring.app import update_ui
    updater = startup_gate.updater
    update = {"version": "3.9.0", "sha256": "a" * 64, "name": "latest.exe"}
    updater.settings.setValue("updates/auto_check", False)
    updater.settings.setValue("updates/auto_download", False)
    updater.settings.setValue("updates/announced_package", update["version"] + ":" + update["sha256"])
    monkeypatch.setattr(core, "check_update", lambda *_: dict(update))
    calls = []
    monkeypatch.setattr(core, "download", lambda *args: calls.append(args[0]) or tmp_path / "latest.exe")
    monkeypatch.setattr(update_ui, "prepare_job", lambda *args: calls.append(args[2]) or tmp_path / "job.json")
    monkeypatch.setattr(QMessageBox, "question", lambda *_: pytest.fail("Startup must update automatically"))
    updater.check()
    assert calls == [update, update]
    assert updater.pending_job == tmp_path / "job.json"
    assert startup_gate.result() == QDialog.DialogCode.Rejected  # Exit to install, never enter old workspace.


@pytest.mark.parametrize("failure_at", ["check", "download", "prepare"])
def test_startup_errors_cannot_unlock_old_annotation(startup_gate, monkeypatch, tmp_path, failure_at):
    from PySide6.QtWidgets import QDialog

    from cowmata_tailring.app import update_ui
    update = {"version": "3.9.0", "sha256": "a" * 64, "name": "latest.exe"}

    def fail(*_):
        raise OSError("network or installation failed")

    monkeypatch.setattr(core, "check_update", fail if failure_at == "check" else lambda *_: update)
    monkeypatch.setattr(core, "download", fail if failure_at == "download" else lambda *_: tmp_path / "latest.exe")
    monkeypatch.setattr(update_ui, "prepare_job", fail)
    startup_gate.show()
    startup_gate.updater.check()
    assert startup_gate.isVisible() and startup_gate.result() != QDialog.DialogCode.Accepted
    assert startup_gate.retry.isEnabled() and startup_gate.updater.pending_job is None


def test_main_does_not_construct_workspace_or_load_project_when_startup_is_blocked(tmp_path):
    import sys
    source = Path(__file__).resolve().parents[1]
    script = ("import sys;sys.path.insert(0,sys.argv[1]);"
              "from cowmata_tailring.app import main,update_ui;"
              "update_ui.verify_startup_update=lambda:False;"
              "sys.modules['cowmata_tailring.workspace.modern_window']=None;"
              "raise SystemExit(main.main(['--mode','workspace','--project',sys.argv[2]]))")
    result = subprocess.run([sys.executable, "-B", "-c", script, str(source), str(tmp_path)],
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert not list(tmp_path.iterdir())


def test_directory_check_cache_does_not_hide_a_redirect_on_next_pass(transaction):
    import os
    if os.name != "nt":
        pytest.skip("Windows junction regression")
    base, target, _, _, _ = transaction
    worker.inventory(target)
    runtime = target / "runtime"
    outside = base / "outside-runtime"
    runtime.rename(outside)
    original = (outside / "python.exe").read_bytes()
    command = "New-Item -ItemType Junction -Path '" + str(runtime) + "' -Value '" + str(outside) + "' | Out-Null"
    subprocess.run(["powershell", "-NoProfile", "-Command", command], check=True,
                   creationflags=0x08000000, capture_output=True)
    try:
        with pytest.raises(ValueError, match="Linked/redirected"):
            worker.inventory(target)
        with pytest.raises(ValueError, match="Linked/redirected"):
            worker.remove_owned(target)
        assert (outside / "python.exe").read_bytes() == original
        assert (target / "COWMATA.exe").exists()
    finally:
        runtime.rmdir()  # Remove only this fixture's junction, never its target.
        outside.rename(runtime)
