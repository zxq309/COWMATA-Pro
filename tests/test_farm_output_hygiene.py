import json

import pytest

from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.farm_layout import initialize_farm
from cowmata_tailring.workspace.paired_dataset import discover


@pytest.mark.parametrize('bucket', ['invalid', 'duplicates', 'replaced_less_complete'])
def test_catalog_keeps_recovery_files_out_of_material_scan(tmp_path, monkeypatch, bucket):
    monkeypatch.setenv('COWMATA_ACCESS_DIR', str(tmp_path/'access'))
    root = tmp_path/'farm'
    initialize_farm(root)
    live = root/'产犊/Motion/2026-09-18/device/2026-09-18_00-00-00.json'
    live.parent.mkdir(parents=True)
    live.write_text('{}', encoding='utf-8')
    recovery = root/'.edge-download/cleanup'/bucket/live.relative_to(root)
    recovery.parent.mkdir(parents=True)
    recovery.write_text('{"old": true}', encoding='utf-8')
    catalog = Catalog(root, stability_seconds=0)
    try:
        result = catalog.scan(fast=True)
        assert result.added == [live.relative_to(root).as_posix()]
    finally:
        catalog.close()
    assert recovery.read_text(encoding='utf-8') == '{"old": true}'
    assert (root/'.cowmata-farm.json').is_file()


@pytest.mark.parametrize('bucket', ['invalid', 'duplicates', 'replaced_less_complete'])
def test_dataset_discovers_live_material_only(tmp_path, bucket):
    root = tmp_path/'farm'
    live = root/'产犊/Motion/2026-09-18/device/2026-09-18_00-00-00.json'
    live.parent.mkdir(parents=True)
    live.write_text(json.dumps({'create_time': 1}), encoding='utf-8')
    recovery = root/'.edge-download/cleanup'/bucket/live.relative_to(root)
    recovery.parent.mkdir(parents=True)
    recovery.write_bytes(live.read_bytes())
    assert [row['source'] for row in discover([root], 'behavior')] == [str(live.resolve())]
    assert recovery.exists()


def test_legacy_import_does_not_write_placeholder_text(tmp_path, monkeypatch):
    from test_legacy_migration import sources

    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration
    monkeypatch.setenv('COWMATA_ACCESS_DIR', str(tmp_path/'access'))
    raw, label = sources(tmp_path)
    plan = plan_migration([label], [raw], tmp_path/'out', category='pregnancy_late')
    result = execute_migration(plan)
    assert result['events'] == 1
    assert not list((tmp_path/'out/PPG').rglob('占位说明.txt'))
    assert (tmp_path/'out/PPG').is_dir()
