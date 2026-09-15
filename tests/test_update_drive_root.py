"""The 3.8.0 Save-and-update failure on a drive-root folder must not recur."""
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from cowmata_tailring.app import update_ui, update_worker


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-root installation")
def test_prepare_save_and_update_for_registered_drive_root_install(tmp_path, monkeypatch):
    root = Path(tmp_path.anchor) / ("COWMATA-QA-" + uuid.uuid4().hex)
    root.mkdir()
    try:
        (root / "COWMATA.install-id").write_text("COWMATA-3.8.0", encoding="utf-8")
        (root / "COWMATA.exe").write_bytes(b"launcher fixture")
        runtime = root / "runtime"
        runtime.mkdir()
        (runtime / "python.exe").write_bytes(b"private runtime fixture")
        (runtime / "python313.zip").write_bytes(b"stdlib fixture")
        code = root / "cowmata_tailring/app"
        code.mkdir(parents=True)
        for name in ("update_core.py", "update_worker.py"):
            (code / name).write_text("# updater fixture", encoding="utf-8")
        rows = [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size, "sha256": update_worker.digest(p)}
                for p in root.rglob("*") if p.is_file() and p.name != "COWMATA.install-id"]
        (root / "package-manifest.json").write_text(json.dumps({"version": "3.8.0", "files": rows}), encoding="utf-8")
        registrations = []
        monkeypatch.setattr(update_worker, "registered", lambda p, v: registrations.append((p, v)) or True)
        monkeypatch.setattr(update_worker, "desktop_enabled", lambda *a: False)
        commands = []
        def runner(command, timeout):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)
        monkeypatch.setattr(update_worker, "run", runner)
        job_file = update_ui.prepare_job(root, tmp_path / "Setup.exe", {"version": "3.8.3"}, tmp_path)
        job = json.loads(job_file.read_text(encoding="utf-8"))
        assert Path(job["root"]) == root
        assert registrations == [(root, "3.8.0")]
        assert len(commands) == 1 and commands[0][-1] == "--help"
        assert not any("Setup.exe" == Path(str(arg)).name for arg in commands[0])
        with pytest.raises(ValueError, match="broad installation target"):
            update_worker.safe_path(Path(tmp_path.anchor))
        # A shallow arbitrary directory still fails without matching identity.
        (root / "COWMATA.install-id").write_text("COWMATA-9.9.9", encoding="utf-8")
        with pytest.raises(ValueError, match="broad installation target"):
            update_worker.safe_path(root)
    finally:
        checked = root.resolve()
        assert checked.parent == Path(tmp_path.anchor) and checked.name.startswith("COWMATA-QA-")
        shutil.rmtree(checked)
