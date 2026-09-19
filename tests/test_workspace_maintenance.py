import json
from pathlib import Path

import pytest


def test_index_recovery_is_external_and_still_recovers(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.storage import atomic_json, read_json, recovery_path
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "app"))
    path = tmp_path / "farm/资源索引.json"
    atomic_json(path, {"records": [1]})
    atomic_json(path, {"records": [2]})
    assert not path.with_suffix('.json.bak').exists()
    assert recovery_path(path).is_relative_to(tmp_path / 'app')
    path.write_text('broken', encoding='utf-8')
    assert read_json(path) == {"records": [1]}


def test_cleanup_removes_empty_staging_and_migrates_backup_only(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.maintenance import clean_project
    from cowmata_tailring.workspace.storage import read_json, recovery_path
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "app"))
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    root = tmp_path / "farm"
    empty = root / '.归类缓存' / ('a' * 32) / 'prepared'
    empty.mkdir(parents=True)
    retained = root / '.归类缓存' / ('b' * 32) / 'prepared.mp4'
    retained.parent.mkdir(); retained.write_bytes(b'resume')
    edge = root / '.edge-download/csv-completed.sqlite3'
    edge.parent.mkdir(); edge.write_bytes(b'progress')
    index = root / '资源索引.json'
    index.write_text('{"records": []}', encoding='utf-8')
    backup = root / '资源索引.json.bak'
    backup.write_text('{"records": [1]}', encoding='utf-8')
    note = root / '标注工程/session.json'
    note.parent.mkdir(); note.write_bytes(b'annotations')
    result = clean_project(root)
    assert result['removed_dirs'] == 2
    assert not empty.parent.exists()
    assert retained.read_bytes() == b'resume'
    assert edge.read_bytes() == b'progress'
    assert note.read_bytes() == b'annotations'
    assert not backup.exists()
    assert json.loads(recovery_path(index).read_text(encoding='utf-8')) == {'records': [1]}
    index.write_text('bad', encoding='utf-8')
    assert read_json(index) == {'records': [1]}


def test_live_project_is_not_cleaned(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.maintenance import clean_project
    from cowmata_tailring.workspace.dataset_access import DatasetLease
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    root = tmp_path / 'farm'; cache = root / '.归类缓存/empty'
    cache.mkdir(parents=True)
    with DatasetLease([root]):
        result = clean_project(root)
    assert result['skipped']
    assert cache.exists()


def test_corrupt_primary_keeps_legacy_backup(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.maintenance import clean_project
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    root = tmp_path / 'farm'; root.mkdir()
    (root / '资源索引.json').write_text('broken')
    backup = root / '资源索引.json.bak'
    backup.write_text('{"records": []}')
    clean_project(root)
    assert backup.exists()


def test_invalid_newer_recovery_keeps_legacy_backup(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.maintenance import clean_project
    from cowmata_tailring.workspace.storage import recovery_path
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'app'))
    monkeypatch.setenv('COWMATA_ACCESS_DIR', str(tmp_path / 'access'))
    root = tmp_path / 'farm'; root.mkdir()
    index = root / '资源索引.json'
    index.write_text('{"records": []}', encoding='utf-8')
    backup = root / '资源索引.json.bak'
    backup.write_text('{"records": [1]}', encoding='utf-8')
    recovery = recovery_path(index)
    recovery.parent.mkdir(parents=True)
    recovery.write_text('[]', encoding='utf-8')
    clean_project(root)
    assert backup.exists()


def test_cleanup_does_not_follow_directory_links(tmp_path, monkeypatch):
    from cowmata_tailring.workspace.maintenance import clean_project
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    root = tmp_path / 'farm'; root.mkdir()
    external = tmp_path / 'external'; external.mkdir()
    (external / 'empty').mkdir()
    try:
        (root / '.归类缓存').symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip('symlink privilege unavailable')
    clean_project(root)
    assert (external / 'empty').exists()


def test_exit_cleans_only_registered_projects_and_preserves_resume(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import maintenance
    from cowmata_tailring.app.main import run_event_loop
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'app'))
    monkeypatch.setenv('COWMATA_ACCESS_DIR', str(tmp_path / 'access'))
    monkeypatch.setattr(maintenance, '_projects', set())
    root = tmp_path / 'farm'; cache = root / '.归类缓存' / ('c' * 32)
    cache.mkdir(parents=True)
    (root / '资源索引.json').write_text('{"records": []}', encoding='utf-8')
    other = tmp_path / 'unrelated/.归类缓存/empty'; other.mkdir(parents=True)
    resume = tmp_path / 'job/dahua-run.json'; resume.parent.mkdir()
    resume.write_text('{"status": "paused"}')
    maintenance.remember_project(root)
    class App:
        def exec(self):
            return 7
    assert run_event_loop(App()) == 7
    assert not cache.parent.exists()
    assert other.exists()
    assert resume.exists()
