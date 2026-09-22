"""4.1.9 regressions: junk purge before update inventory, resume task picker."""
import hashlib
import json

import pytest

from cowmata_tailring.app import update_worker as worker
from cowmata_tailring.workspace import dahua_tasks as tasks


def test_purge_junk_removes_installer_droppings_only(tmp_path):
    cache = tmp_path / "%SystemDrive%" / "ProgramData" / "Microsoft" / "Windows" / "Caches"
    cache.mkdir(parents=True)
    (cache / "cversions.2.db").write_bytes(b"x")
    nvidia = tmp_path / "NVIDIA Corporation" / "umdlogs"
    nvidia.mkdir(parents=True)
    real = tmp_path / "cowmata_tailring" / "app"
    real.mkdir(parents=True)
    (real / "main.py").write_text("VERSION=1", encoding="utf-8")
    removed = worker.purge_junk(tmp_path)
    assert sorted(removed) == ["%SystemDrive%", "NVIDIA Corporation"]
    assert not list(tmp_path.glob("%SystemDrive%*"))
    assert not (tmp_path / "NVIDIA Corporation").exists()
    assert (real / "main.py").is_file()


def test_purge_junk_ignores_normal_names(tmp_path):
    keep = tmp_path / "runtime" / "python.exe"
    keep.parent.mkdir(parents=True)
    keep.write_bytes(b"x")
    plain = tmp_path / "NVIDIA Corporation"
    plain.mkdir()  # without umdlogs it is not the known droppings pattern
    assert worker.purge_junk(tmp_path) == []
    assert plain.exists() and keep.is_file()


def _write_job(job_dir, task_id, farm, *, mode="files", identity=None):
    job_dir.mkdir(parents=True)
    request = dict(target=str(farm))
    index = {} if mode == "files" else dict(mode="disk", disk=dict(identity=identity))
    (job_dir / "dahua-plan.json").write_text(json.dumps(dict(
        adapter=tasks.ADAPTER, id=task_id, request=request), ensure_ascii=False), encoding="utf-8")
    (job_dir / "dahua-index.json").write_text(json.dumps(index), encoding="utf-8")
    (job_dir / "records").mkdir()
    pending = dict(task_id=task_id, owner_id="owner", job=str(job_dir),
                   paths=[str(job_dir / "records"), str(farm)])
    (job_dir.parent / "pending").mkdir(exist_ok=True)
    (job_dir.parent / "pending" / (task_id + ".json")).write_text(
        json.dumps(pending), encoding="utf-8")


def test_resume_skips_task_whose_recorder_disk_is_absent(tmp_path, monkeypatch):
    farm = tmp_path / "farm"
    farm.mkdir()
    registry = tmp_path / "registry"
    from cowmata_tailring.workspace import dataset_access as access_module
    monkeypatch.setattr(access_module, "registry_root", lambda: registry)
    old = registry / "old-job"
    new = registry / "new-job"
    _write_job(old, "a" * 32, farm, mode="disk", identity="absent-disk")
    _write_job(new, "b" * 32, farm, mode="disk", identity="attached-disk")
    monkeypatch.setattr(tasks, "disks", lambda: [dict(identity="attached-disk")])
    chosen = tasks.pending_video_job(farm)
    assert chosen == new.resolve()


def test_resume_all_disks_absent_reports_clear_error(tmp_path, monkeypatch):
    farm = tmp_path / "farm"
    farm.mkdir()
    registry = tmp_path / "registry"
    from cowmata_tailring.workspace import dataset_access as access_module
    monkeypatch.setattr(access_module, "registry_root", lambda: registry)
    _write_job(registry / "old", "c" * 32, farm, mode="disk", identity="gone-1")
    _write_job(registry / "new", "d" * 32, farm, mode="disk", identity="gone-2")
    monkeypatch.setattr(tasks, "disks", lambda: [dict(identity="other")])
    with pytest.raises(ValueError) as exc:
        tasks.pending_video_job(farm)
    assert "来源磁盘都未连接" in str(exc.value)


def test_resume_single_files_task_unaffected(tmp_path, monkeypatch):
    farm = tmp_path / "farm"
    farm.mkdir()
    registry = tmp_path / "registry"
    from cowmata_tailring.workspace import dataset_access as access_module
    monkeypatch.setattr(access_module, "registry_root", lambda: registry)
    only = registry / "only"
    _write_job(only, "e" * 32, farm, mode="files")
    chosen = tasks.pending_video_job(farm)
    assert chosen == only.resolve()
