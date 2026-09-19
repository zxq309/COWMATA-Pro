import json

import pytest


def _pending(access_root, job, status):
    job.mkdir(parents=True, exist_ok=True)
    (job / "dahua-run.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    pending = access_root / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    task_id = "a" * 32
    (pending / f"{task_id}.json").write_text(json.dumps({
        "task_id": task_id,
        "owner_id": "b" * 32,
        "job": str(job),
        "paths": [str(job.parent / "farm")],
    }), encoding="utf-8")
    return pending / f"{task_id}.json"


def test_paused_dahua_task_does_not_block_project_open(tmp_path, monkeypatch):
    access_root = tmp_path / "access"
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(access_root))
    farm = tmp_path / "farm"
    farm.mkdir()
    record = _pending(access_root, tmp_path / "job", "paused")

    from cowmata_tailring.workspace.dataset_access import ensure_available

    ensure_available([farm])
    assert record.exists(), "paused tasks remain resumable"


def test_completed_dahua_task_releases_project_open_guard(tmp_path, monkeypatch):
    access_root = tmp_path / "access"
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(access_root))
    farm = tmp_path / "farm"
    farm.mkdir()
    record = _pending(access_root, tmp_path / "job", "completed")

    from cowmata_tailring.workspace.dataset_access import ensure_available

    ensure_available([farm])
    assert not record.exists()


def test_running_dahua_task_still_blocks_project_open(tmp_path, monkeypatch):
    access_root = tmp_path / "access"
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(access_root))
    farm = tmp_path / "farm"
    farm.mkdir()
    _pending(access_root, tmp_path / "job", "running")

    from cowmata_tailring.workspace.dataset_access import ensure_available

    with pytest.raises(OSError, match="未完成"):
        ensure_available([farm])
