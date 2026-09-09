"""Failure recovery must leave a retryable download and a usable old install."""
import json
import subprocess
from pathlib import Path

import pytest
from test_updates import Response, update_for
from test_updates import transaction as transaction

from cowmata_tailring.app import update_core as core
from cowmata_tailring.app import update_worker as worker


def test_bad_complete_download_can_recover_using_retry(tmp_path):
    data = b"verified new installer"
    update = update_for(data)
    with pytest.raises(ValueError, match="校验失败"):
        core.download(update, tmp_path, opener=lambda *_: Response(b"x" * len(data)))
    # The mandatory startup dialog offers retry. It must fetch bytes again
    # instead of hashing the same corrupt, fully downloaded .part forever.
    path = core.download(update, tmp_path, opener=lambda *_: Response(data))
    assert path.read_bytes() == data


def test_registration_cleanup_error_still_restores_old_application(transaction):
    _, target, _, job, runner = transaction
    before = (target / "code.py").read_bytes()

    def fail_registration(command, timeout=0):
        if "/REGISTERONLY=1" in command:
            return subprocess.CompletedProcess(command, 7, b"", b"registration failed")
        return runner(command, timeout)

    def fail_cleanup(*_):
        raise PermissionError("shortcut is temporarily locked")

    with pytest.raises(RuntimeError, match="新版注册失败"):
        worker.install(job, runner=fail_registration, registration=lambda *_: True,
                       unregister=fail_cleanup, restart=False)
    assert (target / "code.py").read_bytes() == before
    assert not (target / worker.LOCK).exists()
    result = json.loads((Path(job["job_dir"]) / "result.json").read_text(encoding="utf-8"))
    assert result["phase"] == "rolled_back"
    assert "shortcut is temporarily locked" in result["rollback_warning"]

def test_two_real_updaters_cannot_replace_the_same_installation_concurrently(transaction):
    import sys
    import time

    _, target, payload, job, _ = transaction
    job_dir = Path(job["job_dir"])
    script = job_dir / "concurrent-updater.py"
    script.write_text(
        "import json, shutil, subprocess, sys, time\n"
        "from pathlib import Path\n"
        + "sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) + ")\n"
        + "from cowmata_tailring.app.update_worker import install\n"
        "job=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
        "payload=Path(sys.argv[2]); gate=Path(sys.argv[3]) if len(sys.argv)>3 else None\n"
        "def runner(command, timeout=0):\n"
        "    if '/STAGE=1' in command:\n"
        "        if gate:\n"
        "            gate.with_suffix('.ready').touch()\n"
        "            deadline=time.monotonic()+15\n"
        "            while not gate.exists():\n"
        "                if time.monotonic()>deadline: raise TimeoutError('test release timeout')\n"
        "                time.sleep(.01)\n"
        "        shutil.copytree(payload, Path(command[-1][3:]))\n"
        "    return subprocess.CompletedProcess(command, 0, b'3.1.0rc3', b'')\n"
        "try:\n"
        "    result=install(job, runner=runner, registration=lambda *_:True, unregister=lambda *_:None, restart=False)\n"
        "    print(json.dumps(result))\n"
        "except Exception as error:\n"
        "    print(json.dumps({'error_type':type(error).__name__,'error':str(error)}))\n"
        "    raise SystemExit(23)\n",
        encoding="utf-8",
    )
    first_job = job_dir / "first.json"
    first_job.write_text(json.dumps(job), encoding="utf-8")
    other_dir = job_dir.parent / "second-job"
    other_dir.mkdir()
    other_job = other_dir / "second.json"
    other_job.write_text(json.dumps({**job, "job_dir": str(other_dir)}), encoding="utf-8")
    gate = job_dir / "release"
    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    first = subprocess.Popen([sys.executable, "-B", str(script), str(first_job), str(payload), str(gate)], **kwargs)
    try:
        deadline = time.monotonic() + 8
        while not gate.with_suffix(".ready").exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert gate.with_suffix(".ready").exists(), "first updater failed to reach extraction"
        second = subprocess.run([sys.executable, "-B", str(script), str(other_job), str(payload)],
                                timeout=8, **kwargs)
        assert second.returncode == 23, second.stdout.decode("utf-8", "replace")
        assert json.loads(second.stdout)["error_type"] == "UpdateInProgress"
        assert (target / "code.py").read_text().endswith("'3.1.0-rc.2'")
    finally:
        gate.touch()
        stdout, stderr = first.communicate(timeout=12)
    assert first.returncode == 0, stderr.decode("utf-8", "replace")
    assert json.loads(stdout)["phase"] == "complete"
    assert (target / "code.py").read_text().endswith("'3.1.0-rc.3'")


def test_duplicate_update_main_reports_existing_upgrade_without_error_dialog(transaction, monkeypatch):
    import ctypes
    import sys

    _, _, _, job, _ = transaction
    path = Path(job["job_dir"]) / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["update_worker.py", str(path)])
    def busy(_):
        raise worker.UpdateInProgress("另一个更新正在进行")
    monkeypatch.setattr(worker, "install", busy)
    messages = []
    if sys.platform == "win32":
        monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", lambda *args: messages.append(args))
    assert worker.main() == 0
    state = json.loads((path.parent / "result.json").read_text(encoding="utf-8"))
    assert state["phase"] == "already_updating"
    assert not (path.parent / "error.json").exists()
    if sys.platform == "win32":
        assert messages[0][-1] == 0x40


def test_dead_updater_process_does_not_leave_transaction_permanently_locked(transaction):
    import sys
    import time

    _, target, _, job, runner = transaction
    ready = Path(job["job_dir"]) / "owner-ready"
    program = (
        "import sys,time\nfrom pathlib import Path\n"
        + "sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) + ")\n"
        + "from cowmata_tailring.app.update_worker import installation_lock\n"
        "with installation_lock(Path(sys.argv[1])):\n"
        "    Path(sys.argv[2]).touch()\n"
        "    time.sleep(20)\n"
    )
    owner = subprocess.Popen([sys.executable, "-B", "-c", program, str(target), str(ready)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), "isolated lock owner did not start"
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)
    result = worker.install(job, runner=runner, registration=lambda *_: True,
                            unregister=lambda *_: None, restart=False)
    assert result["phase"] == "complete"
