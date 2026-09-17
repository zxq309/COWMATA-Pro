"""Long jobs renew expired sessions without bypassing device revocation."""
import io
import json
import os
import types

import pytest

from cowmata_security.authority import Denied
from cowmata_security.simple_authority import SimpleAuthority, initialize_registry, new_credential, encode_registry
from cowmata_security.client import AccessDenied, Session
from cowmata_security.device_store import DeviceStore
from cowmata_security.protocol import dispatch
import cowmata_security.worker as worker


@pytest.fixture
def activated_worker(tmp_path, monkeypatch, request):
    if os.name != "nt":
        pytest.skip("DeviceStore is bound to Windows DPAPI")
    clock = [10000.]
    credential = new_credential("admin")
    initialize_registry(tmp_path / "authority.csv", "pro", [credential])
    authority = SimpleAuthority(tmp_path / "authority.csv", clock=lambda: clock[0])
    code = credential["code"]
    def transport(request):
        try:
            return dispatch(authority, request)
        except Denied as error:
            raise AccessDenied(str(error)) from error
    store = DeviceStore("pro", tmp_path / "device.bin")
    parent = Session(transport, "pro", device_store=store)
    parent.login_admin("admin", credential["password"], remember_device=getattr(request, "param", True))
    monkeypatch.setattr(worker, "read_config", lambda root: transport)
    monkeypatch.setattr(worker, "DeviceStore", lambda product: store, raising=False)
    monkeypatch.setattr(worker.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(worker.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(
        json.dumps({"session": parent.token}).encode() + b"\n")))
    from cowmata_security import client
    previous = client._current
    yield authority, parent, store, clock
    client.install_session(previous)


def test_long_worker_renews_expired_session_using_real_dpapi_device(activated_worker):
    authority, parent, store, clock = activated_worker
    session = worker.authorize_worker("unused-test-root", "dataset")
    original = session.token
    assert b"device_secret" not in store.path.read_bytes()
    clock[0] += 8 * 3600
    assert session.refresh()
    assert session.token != original
    session.require("dataset")


def test_worker_device_revocation_has_no_grace(activated_worker):
    authority, parent, store, clock = activated_worker
    session = worker.authorize_worker("unused-test-root", "dataset")
    registry = store.path.with_name("authority.csv")
    registry.write_bytes(encode_registry([new_credential("admin")]))
    with pytest.raises(AccessDenied):
        session.refresh()
    assert not session.token


def test_valid_device_does_not_allow_worker_without_pipe_token(activated_worker, monkeypatch):
    monkeypatch.setattr(worker.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"")))
    with pytest.raises(AccessDenied, match="Missing worker authorization"):
        worker.authorize_worker("unused-test-root", "dataset")


def test_expired_pipe_token_cannot_bootstrap_from_device(activated_worker):
    authority, parent, store, clock = activated_worker
    clock[0] += 8 * 3600
    with pytest.raises(AccessDenied):
        worker.authorize_worker("unused-test-root", "dataset")


def test_worker_cannot_renew_as_a_different_account(activated_worker):
    authority, parent, store, clock = activated_worker
    session = worker.authorize_worker("unused-test-root", "dataset")
    other = authority.batch_create(parent.token, 1)["credentials"][-1]
    # A later login under the same Windows user replaces the stored device.
    another = Session(parent.transport, "pro", device_store=store)
    another.login(other["account"], other["code"], password=other["password"])
    clock[0] += 8 * 3600
    with pytest.raises(AccessDenied):
        session.refresh()
    assert not session.token


@pytest.mark.parametrize("activated_worker", [False], indirect=True)
def test_worker_runs_24_hours_without_saved_password_or_device(activated_worker):
    authority, parent, store, clock = activated_worker
    session = worker.authorize_worker("unused-test-root", "dataset")
    original = session.token
    assert not store.path.exists()
    for _ in range(24):
        clock[0] += 3600
        assert session.refresh()
        session.require("dataset")
    assert session.token == original
    assert not store.path.exists()
    registry = store.path.with_name("authority.csv")
    registry.write_bytes(encode_registry([new_credential("admin")]))
    with pytest.raises(AccessDenied):
        session.refresh()
    assert not session.token
