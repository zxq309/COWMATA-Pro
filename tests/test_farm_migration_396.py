import json
from pathlib import Path

import pytest

from cowmata_tailring.workspace.catalog import Catalog, digest_file
from cowmata_tailring.workspace.farm_layout import shared_farm
from cowmata_tailring.workspace.farm_migration import migrate
from cowmata_tailring.workspace.storage import atomic_json


def old_farm(root):
    scope = root / '产犊'
    for kind, name in [('Motion', 'a.json'), ('PPG', 'b.json'), ('Video', '2026-09-18_00-00-00.mp4')]:
        path = scope / kind / '2026-09-18' / ('视角01' if kind == 'Video' else 'cow') / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'content')
    catalog = Catalog(scope, day='2026-09-18', stability_seconds=0)
    catalog.scan(fast=True)
    catalog.close()
    label = scope / '标注工程/Motion/2026-09-18/cow/a.标注.json'
    atomic_json(label, dict(source=dict(path='Motion/2026-09-18/cow/a.json', project_root_hint=str(scope)),
                           video=dict(rows=[dict(path='Video/2026-09-18/视角01/2026-09-18_00-00-00.mp4')]), note='说明不改'))
    return scope, label


def test_migration_relocates_video_and_rebases_index_and_annotations(tmp_path):
    root = tmp_path / 'farm'
    scope, label = old_farm(root)
    before = digest_file(scope / 'Motion/2026-09-18/cow/a.json')
    report = migrate(root)
    assert report['status'] == 'complete' and report['files'] == 1
    assert shared_farm(scope) == root
    assert not (scope / 'Video').exists()
    assert (root / '录像/2026-09-18/视角01/2026-09-18_00-00-00.mp4').read_bytes() == b'content'
    doc = json.loads(label.read_text(encoding='utf-8'))
    assert doc['source']['path'].startswith('产犊/Motion/')
    assert doc['video']['rows'][0]['path'].startswith('录像/')
    assert doc['note'] == '说明不改'
    assert digest_file(scope / 'Motion/2026-09-18/cow/a.json') == before
    catalog = Catalog(scope, day='2026-09-18', stability_seconds=0)
    try:
        assert all(not r['path'].startswith(('Motion/', 'Video/')) for r in catalog.rows())
    finally:
        catalog.close()


def test_conflicting_video_name_stops_before_moving(tmp_path):
    root = tmp_path / 'farm'
    scope, _ = old_farm(root)
    conflict = root / '录像/2026-09-18/视角01/2026-09-18_00-00-00.mp4'
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b'different')
    with pytest.raises(ValueError, match='冲突'):
        migrate(root)
    assert (scope / 'Video').exists() and shared_farm(scope) is None


def test_metadata_failure_rolls_back_video_and_keeps_raw(tmp_path, monkeypatch):
    root = tmp_path / 'farm'
    scope, label = old_farm(root)
    original = label.read_bytes()
    from cowmata_tailring.workspace import farm_migration
    real = farm_migration.atomic_json
    def fail(path, value, **kwargs):
        if Path(path) == root / '.cowmata-farm.json':
            raise OSError('injected')
        return real(path, value, **kwargs)
    monkeypatch.setattr(farm_migration, 'atomic_json', fail)
    with pytest.raises(OSError, match='injected'):
        migrate(root)
    assert (scope / 'Video/2026-09-18/视角01/2026-09-18_00-00-00.mp4').is_file()
    assert label.read_bytes() == original
    assert shared_farm(scope) is None


def test_interrupted_migration_recovers_before_retry(tmp_path):
    root = tmp_path / 'farm'
    scope, label = old_farm(root)
    def interrupt(*_):
        raise KeyboardInterrupt('power loss simulation')
    with pytest.raises(KeyboardInterrupt):
        migrate(root, progress=interrupt)
    result = migrate(root)
    assert result['status'] == 'complete'
    assert shared_farm(scope) == root
    assert json.loads(label.read_text(encoding='utf-8'))['source']['path'].startswith('产犊/Motion/')


@pytest.mark.parametrize('view', ['视角01', '视角02'])
def test_shared_date_across_categories_merges_without_overwriting(tmp_path, view):
    root = tmp_path / 'farm'
    old_farm(root)
    extra = root / '怀孕/孕晚期/Video/2026-09-18' / view / '2026-09-18_01-00-00.mp4'
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b'other recording')
    report = migrate(root)
    assert report['files'] == 2 and report['status'] == 'complete'
    assert (root / '录像/2026-09-18/视角01/2026-09-18_00-00-00.mp4').read_bytes() == b'content'
    assert (root / '录像/2026-09-18' / view / extra.name).read_bytes() == b'other recording'


def test_shared_empty_date_and_interruption_recover(tmp_path):
    root = tmp_path / 'farm'
    old_farm(root)
    empty = root / '怀孕/孕晚期/Video/2026-09-18'
    empty.mkdir(parents=True)
    def interrupt(*_):
        raise KeyboardInterrupt('power loss simulation')
    with pytest.raises(KeyboardInterrupt):
        migrate(root, progress=interrupt)
    assert migrate(root)['status'] == 'complete'
    assert not empty.exists()
    assert (root / '录像/2026-09-18/视角01/2026-09-18_00-00-00.mp4').is_file()
