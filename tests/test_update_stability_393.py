"""Failures discovered by the 3.9.2 all-release upgrade audit."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_offline_upgrade import module
from test_portable_updates_390 import product, bundle
from test_updates import package
from cowmata_tailring.app import update_worker as worker
from cowmata_tailring.app import portable_update


def legacy_product(tmp_path, monkeypatch, version='3.4.3'):
    root=product(tmp_path/'old',version)
    file=root/'package-manifest.json'
    manifest=json.loads(file.read_bytes());manifest.pop('version')
    file.write_text(json.dumps(manifest),encoding='utf-8')
    digest=hashlib.sha256(file.read_bytes()).hexdigest()
    monkeypatch.setattr(worker,'LEGACY_MANIFEST_VERSIONS',{digest:version},raising=False)
    return root


def test_known_unversioned_manifest_is_read_without_rewriting(tmp_path,monkeypatch):
    root=legacy_product(tmp_path,monkeypatch)
    before=(root/'package-manifest.json').read_bytes()
    assert worker.is_product_installation(root)
    assert worker.installation_version(root)=='3.4.3'
    assert (root/'package-manifest.json').read_bytes()==before


@pytest.mark.parametrize('fault',['manifest_tamper','identity_mismatch','missing_runtime'])
def test_legacy_identity_rejects_unknown_or_incomplete_product(tmp_path,monkeypatch,fault):
    root=legacy_product(tmp_path,monkeypatch)
    if fault=='manifest_tamper':
        with (root/'package-manifest.json').open('ab') as stream:stream.write(b' ')
    elif fault=='identity_mismatch':(root/'COWMATA.install-id').write_text('COWMATA-3.5.0')
    else:(root/'runtime/python.exe').unlink()
    assert not worker.is_product_installation(root)


def test_known_unversioned_portable_completes_setup_transaction(tmp_path,monkeypatch):
    root=legacy_product(tmp_path,monkeypatch)
    bridge=module();setup=tmp_path/'setup.exe';setup.write_bytes(b'installer')
    payload=tmp_path/'payload';sha=package(payload,'3.9.3')
    job=bridge.prepare(root,setup,tmp_path/'jobs',dict(version='3.9.3',package_sha256=sha,unpacked_size=1000),False)
    def runner(command,timeout=0):
        if '/STAGE=1' in command:shutil.copytree(payload,Path(command[-1][3:]))
        return subprocess.CompletedProcess(command,0,b'3.9.3',b'')
    result=worker.install(job,runner=runner,unregister=lambda *_:None,restart=False)
    assert result['phase']=='complete' and result['old_version']=='3.4.3'


def test_known_unversioned_portable_completes_zip_transaction(tmp_path,monkeypatch):
    root=legacy_product(tmp_path,monkeypatch)
    archive=bundle(tmp_path,'3.9.3');job_dir=tmp_path/'job';job_dir.mkdir()
    monkeypatch.setattr(portable_update,'forget_install_registration',lambda *_:None)
    result=worker.install(dict(root=str(root),setup=str(archive),job_dir=str(job_dir),update=portable_update.local_update(archive)),runner=lambda args,timeout=0:subprocess.CompletedProcess(args,0,b'3.9.3',b''),restart=False)
    assert result['phase']=='complete' and result['old_version']=='3.4.3'


def test_legacy_setup_refuses_unowned_data_before_creating_job(tmp_path,monkeypatch):
    root=legacy_product(tmp_path,monkeypatch);(root/'my-label.json').write_text('KEEP')
    setup=tmp_path/'setup.exe';setup.write_bytes(b'installer')
    with pytest.raises(ValueError,match='非软件文件'):
        module().prepare(root,setup,tmp_path/'jobs',dict(version='3.9.3'),False)
    assert (root/'my-label.json').read_text()=='KEEP' and not (tmp_path/'jobs').exists()


def test_detached_probe_checks_requested_old_root(tmp_path):
    root=product(tmp_path/'old','3.4.3');job=tmp_path/'job';job.mkdir()
    probe=job/'COWMATA-Progress.exe';probe.write_bytes(b'new launcher')
    calls=[]
    def runner(command,timeout=0):
        calls.append(command)
        assert command[0]!=root/'COWMATA.exe','Must not execute an old launcher to check it'
        return subprocess.CompletedProcess(command,6,b'',b'')
    assert worker.running_check(root,job,runner)==6
    assert calls==[[probe,'--check-running',root]]


def test_first_rc_without_new_probe_is_not_started(tmp_path,monkeypatch):
    root=legacy_product(tmp_path,monkeypatch,'3.1.0-rc.1')
    with pytest.raises(ValueError,match='process probe'):
        worker.running_check(root,tmp_path/'job',lambda *_args,**_kwargs:pytest.fail('Old RC would launch its UI'))
