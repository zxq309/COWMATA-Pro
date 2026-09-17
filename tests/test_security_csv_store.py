import pytest

import cowmata_security.csv_store as module
from cowmata_security.csv_store import CsvStore, record


def initialized(tmp_path):
    store = CsvStore(tmp_path / "authority.csv")
    with store.transaction(create=True) as rows:
        rows.append(record("setting", "value", value="before"))
    return store


def test_transient_windows_replace_failure_retries_without_losing_transaction(
    tmp_path, monkeypatch
):
    store = initialized(tmp_path)
    original = module.os.replace
    calls = []

    def replace(source, target):
        calls.append((source, target))
        if len(calls) == 1:
            raise PermissionError("temporary reader")
        return original(source, target)

    monkeypatch.setattr(module.os, "replace", replace)
    with store.transaction() as rows:
        rows[0]["value"] = "after"
    assert len(calls) == 2
    with store.transaction() as rows:
        assert rows[0]["value"] == "after"


def test_external_edit_during_replace_retry_is_not_overwritten(tmp_path, monkeypatch):
    store = initialized(tmp_path)
    original = module.os.replace
    calls = []
    external = store.path.read_bytes().replace(b"before", b"external")

    def replace(source, target):
        calls.append((source, target))
        if len(calls) == 1:
            store.path.write_bytes(external)
            raise PermissionError("temporary editor")
        return original(source, target)

    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(ValueError, match="外部修改"):
        with store.transaction() as rows:
            rows[0]["value"] = "ours"
    assert store.path.read_bytes() == external
    assert len(calls) == 1
