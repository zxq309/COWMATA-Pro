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
    window.close()
    first.close()
    app.processEvents()


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
