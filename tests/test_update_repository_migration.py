import json
from pathlib import Path

import pytest

from cowmata_tailring.app import update_core as core
from cowmata_tailring.app import update_worker as worker


@pytest.mark.parametrize('url', [
    'https://api.github.com/repos/zxq309/cattle-tail-ring-annotator/releases?per_page=100',
    'https://api.github.com/repositories/1318095307/releases?per_page=100',
    'https://github.com/zxq309/cattle-tail-ring-annotator/releases/download/v3.8.0/COWMATA-Pro-3.8.0-Setup.exe',
])
def test_known_rename_urls_stay_inside_the_same_product_repository(url):
    assert core.valid_url(url, redirect=True) == url


@pytest.mark.parametrize('url', [
    'https://api.github.com/repositories/999/releases',
    'https://api.github.com/repos/zxq309/COWMATA-Pro/releases-other',
    'https://api.github.com/repos/zxq309/COWMATA-Pro/releases/../../contents',
    'https://github.com/another/COWMATA-Pro/releases/download/v3.8.0/x.exe',
])
def test_migration_does_not_allow_other_repositories_or_path_prefix_tricks(url):
    with pytest.raises(ValueError):
        core.valid_url(url, redirect=True)


def test_root_level_installation_requires_a_matching_product_identity(tmp_path):
    identity = tmp_path/'COWMATA.install-id'
    manifest = tmp_path/'package-manifest.json'
    assert hasattr(worker, 'is_product_installation'), 'Registered drive-root installs are still rejected'
    assert not worker.is_product_installation(tmp_path)
    identity.write_text('COWMATA-3.6.2', encoding='utf-8')
    manifest.write_text(json.dumps({'version': '3.6.2', 'files': [{'path': 'COWMATA.exe'}]}), encoding='utf-8')
    assert worker.is_product_installation(tmp_path)
    manifest.write_text(json.dumps({'version': '3.5.3', 'files': []}), encoding='utf-8')
    assert not worker.is_product_installation(tmp_path)
    with pytest.raises(ValueError):
        worker.safe_path(Path(tmp_path.anchor))


def test_progress_writer_retries_temporary_windows_reader_locks(tmp_path, monkeypatch):
    real = worker.os.replace
    calls = []
    def replace(source, target):
        calls.append(1)
        if len(calls) < 3:
            error = PermissionError('progress reader holds the destination')
            error.winerror = 5
            raise error
        return real(source, target)
    monkeypatch.setattr(worker.os, 'replace', replace)
    target = tmp_path/'result.json'
    target.write_text('{"phase":"old"}', encoding='utf-8')
    worker.write_json(target, {'phase': 'verifying'})
    assert json.loads(target.read_text(encoding='utf-8')) == {'phase': 'verifying'}
    assert len(calls) == 3
