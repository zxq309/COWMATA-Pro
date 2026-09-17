"""The public installer must repair legacy clients without trusting their updater."""
import importlib.util
import json
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / 'packaging/offline_upgrade.py'
    spec = importlib.util.spec_from_file_location('offline_upgrade', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def fixture(tmp_path):
    root = tmp_path / 'old app'
    root.mkdir()
    (root / 'COWMATA.install-id').write_text('COWMATA-3.6.2', encoding='utf-8')
    (root / 'COWMATA.exe').write_bytes(b'old')
    (root / 'package-manifest.json').write_text(json.dumps(dict(version='3.6.2', files=[
        dict(path='COWMATA.exe', size=3, sha256='unused')
    ])), encoding='utf-8')
    setup = tmp_path / 'new setup.exe'
    setup.write_bytes(b'new setup')
    return root, setup


def test_old_client_uses_embedded_worker_and_verified_setup(tmp_path, monkeypatch):
    m = module()
    root, setup = fixture(tmp_path)
    monkeypatch.setattr(m.worker, 'registered', lambda r, v: r == root and v == '3.6.2')
    job = m.prepare(root, setup, tmp_path / 'jobs', dict(version='3.8.1', package_sha256='a'*64, unpacked_size=100), False)
    assert job['update']['sha256'] == m.worker.digest(setup)
    assert job['update']['size'] == len(b'new setup')
    assert job['root'] == str(root) and job['desktop'] is False
    assert Path(job['job_dir']).parent == tmp_path / 'jobs'
    assert json.loads((Path(job['job_dir']) / 'job.json').read_text(encoding='utf-8')) == job


@pytest.mark.parametrize('fault', ['unregistered', 'downgrade', 'unowned', 'setup_inside'])
def test_offline_upgrade_refuses_unsafe_replacement(tmp_path, monkeypatch, fault):
    m = module()
    root, setup = fixture(tmp_path)
    before = (root/'COWMATA.exe').read_bytes()
    monkeypatch.setattr(m.worker, 'registered', lambda *_: fault != 'unregistered')
    if fault == 'unowned':
        (root/'user-label.json').write_text('{}')
    if fault == 'setup_inside':
        setup = root/'COWMATA.exe'
    with pytest.raises(ValueError):
        m.prepare(root, setup, tmp_path/'jobs', dict(version='3.6.1' if fault == 'downgrade' else '3.8.1',
                  package_sha256='a'*64, unpacked_size=100), True)
    assert (root/'COWMATA.exe').read_bytes() == before
    assert not (tmp_path/'jobs').exists()
