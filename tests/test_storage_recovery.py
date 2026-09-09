"""Windows file-sharing regressions at the durable annotation save boundary."""
import os
import threading
import time

import pytest

from cowmata_tailring.workspace import storage


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_atomic_save_retries_transient_windows_replace(tmp_path, monkeypatch, winerror):
    path = tmp_path / "annotation.json"
    storage.atomic_json(path, {"revision": 1})
    replace = storage.os.replace
    calls, sleeps = [], []

    def sharing_conflict(source, target):
        calls.append(target)
        if len(calls) < 3:
            error = PermissionError("temporarily in use")
            error.winerror = winerror
            raise error
        replace(source, target)

    monkeypatch.setattr(storage.os, "replace", sharing_conflict)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    storage.atomic_json(path, {"revision": 2})
    assert len(calls) == 3 and 0 < sum(sleeps) <= .3
    assert storage.read_json(path) == {"revision": 2}
    assert storage.read_json(path.with_suffix(".json.bak")) == {"revision": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_save_gives_up_with_original_and_backup_intact(tmp_path, monkeypatch):
    path = tmp_path / "annotation.json"
    storage.atomic_json(path, {"revision": 1})
    sleeps = []

    def denied(*args):
        error = PermissionError("permission denied")
        error.winerror = 5
        raise error

    monkeypatch.setattr(storage.os, "replace", denied)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    with pytest.raises(PermissionError):
        storage.atomic_json(path, {"revision": 2})
    assert len(sleeps) == 4 and sum(sleeps) <= .31
    assert storage.read_json(path) == {"revision": 1}
    assert storage.read_json(path.with_suffix(".json.bak")) == {"revision": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_save_does_not_retry_unrelated_disk_errors(tmp_path, monkeypatch):
    def full(*args):
        raise OSError(28, "disk full")
    monkeypatch.setattr(storage.os, "replace", full)
    monkeypatch.setattr(time, "sleep", lambda *_: pytest.fail("Retried disk-full error"))
    with pytest.raises(OSError, match="disk full"):
        storage.atomic_json(tmp_path / "annotation.json", {"revision": 1})


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing mode")
def test_atomic_save_survives_real_short_windows_read_handle(tmp_path):
    path = tmp_path / "annotation.json"
    storage.atomic_json(path, {"revision": 1})
    stream = path.open("rb")
    release = threading.Timer(.05, stream.close)
    release.start()
    try:
        storage.atomic_json(path, {"revision": 2})
        assert storage.read_json(path) == {"revision": 2}
    finally:
        release.join()
        stream.close()
