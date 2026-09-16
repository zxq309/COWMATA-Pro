"""One current Setup must repair both registered and portable 3.8+ clients."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from test_offline_upgrade import module
from test_portable_updates_390 import product
from test_updates import package


@pytest.fixture
def tmp_path():
    base = Path(tempfile.mkdtemp(prefix="cma392-")).resolve()
    yield base
    assert base.parent == Path(tempfile.gettempdir()).resolve() and base.name.startswith("cma392-")
    shutil.rmtree(base)


VERSIONS = ["3.8.0", "3.8.1", "3.8.2", "3.8.3", "3.8.4", "3.9.0", "3.9.1"]


@pytest.mark.parametrize("version", VERSIONS)
def test_setup_repairs_legacy_portable_without_old_updater(tmp_path, monkeypatch, version):
    m = module()
    root = product(tmp_path / "旧版 软件", version)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"new installer")
    payload = tmp_path / "payload"
    sha = package(payload, "3.9.2")
    monkeypatch.setattr(
        m.worker, "registered", lambda *_: pytest.fail("Portable copies have no registration")
    )
    job = m.prepare(
        root,
        setup,
        tmp_path / "jobs",
        dict(version="3.9.2", package_sha256=sha, unpacked_size=1000),
        False,
    )
    assert job["portable_origin"] is True

    def runner(command, timeout=0):
        if "/STAGE=1" in command:
            shutil.copytree(payload, Path(command[-1][3:]))
        return subprocess.CompletedProcess(command, 0, b"3.9.2", b"")

    result = m.worker.install(
        job,
        runner=runner,
        registration=lambda *_: pytest.fail("No old registration"),
        unregister=lambda *_: pytest.fail("Do not remove unrelated registration"),
        restart=False,
    )
    assert result["phase"] == "complete"
    assert (root / "COWMATA.install-id").read_text() == "COWMATA-3.9.2"


@pytest.mark.parametrize("fault", ["foreign_file", "downgrade", "invalid_identity"])
def test_portable_repair_preserves_user_files_and_invalid_targets(tmp_path, fault):
    m = module()
    root = product(tmp_path / "portable", "3.9.1")
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"new installer")
    original = (root / "COWMATA.exe").read_bytes()
    if fault == "foreign_file":
        (root / "客户标签.json").write_text("{}")
    if fault == "invalid_identity":
        (root / "runtime/python.exe").unlink()
    with pytest.raises(ValueError):
        m.prepare(
            root,
            setup,
            tmp_path / "jobs",
            dict(version="3.9.0" if fault == "downgrade" else "3.9.2"),
            False,
        )
    assert (root / "COWMATA.exe").read_bytes() == original
    assert not (tmp_path / "jobs").exists()
    if fault == "foreign_file":
        assert (root / "客户标签.json").read_text() == "{}"


def test_setup_portable_failed_registration_rolls_back(tmp_path):
    m = module()
    root = product(tmp_path / "old", "3.8.0")
    before = (root / "package-manifest.json").read_bytes()
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"new installer")
    payload = tmp_path / "payload"
    sha = package(payload, "3.9.2")
    job = m.prepare(
        root,
        setup,
        tmp_path / "jobs",
        dict(version="3.9.2", package_sha256=sha, unpacked_size=1000),
        False,
    )
    removed = []

    def runner(command, timeout=0):
        if "/STAGE=1" in command:
            shutil.copytree(payload, Path(command[-1][3:]))
        return subprocess.CompletedProcess(
            command, 7 if "/REGISTERONLY=1" in command else 0, b"3.9.2", b""
        )

    with pytest.raises(RuntimeError, match="新版注册失败"):
        m.worker.install(
            job, runner=runner, unregister=lambda *args: removed.append(args), restart=False
        )
    assert (root / "package-manifest.json").read_bytes() == before
    assert not (root / "COWMATA.install-id").exists()
    assert removed == [(root, "3.9.2")]
