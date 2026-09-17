from __future__ import annotations

import json
import os
import shutil

import pytest

from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.storage import atomic_json, read_json, unique_batch


def inspector(path, kind, asset_id):
    return {"kind": kind, "text": path.read_text(), "needs_review": False}


@pytest.fixture
def cat(tmp_path):
    catalog = Catalog(tmp_path, stability_seconds=3)
    yield catalog
    catalog.close()


def publish(cat, path, now=100):
    cat.scan(now=now)
    return cat.index_one(path, inspector, now=now + 4)


def test_copy_stability_and_unchanged_ocr_cache(cat):
    path = cat.root / "001.mp4"
    path.write_text("source one")
    assert cat.scan(now=100).added == ["001.mp4"]
    assert not cat.pending(now=102)
    assert cat.index_one("001.mp4", inspector, now=102) is None
    result = cat.index_one("001.mp4", inspector, now=104)
    assert result["state"] == "ready"
    assert not cat.scan(now=105).changed
    assert not cat.pending(now=110)
    # A forced duplicate inspection still reuses content-derived OCR.
    assert cat.index_one("001.mp4", lambda *_: pytest.fail("OCR repeated"), now=110)


def test_duplicate_rename_replacement_retains_human_work(cat):
    first = cat.root / "001.mp4"
    first.write_text("original")
    original = publish(cat, "001.mp4")
    work = cat.work_path(original["asset_id"])
    atomic_json(work, {"events": ["human label"]})
    batch = unique_batch(cat.root, "batch")
    shutil.copy2(first, batch / "001.mp4")
    cat.scan(now=110)
    duplicate = cat.index_one("batch/001.mp4", lambda *_: pytest.fail("duplicate re-OCR"), now=114)
    assert duplicate["asset_id"] == original["asset_id"]
    first.rename(cat.root / "renamed.mp4")
    report = cat.scan(now=120)
    assert report.missing == ["001.mp4"]
    moved = cat.index_one("renamed.mp4", lambda *_: pytest.fail("rename re-OCR"), now=124)
    assert moved["asset_id"] == original["asset_id"]
    first.write_text("replaced")  # Same length/name, different immutable identity.
    replacement = publish(cat, "001.mp4", now=130)
    assert replacement["asset_id"] != original["asset_id"]
    assert not cat.work_path(replacement["asset_id"]).exists()
    assert read_json(work)["events"] == ["human label"]


def test_source_changes_during_inspection_not_published(cat):
    (cat.root / "001.mp4").write_text("not finished")
    cat.scan(now=100)

    def writing(path, *_):
        path.write_text("changed while parsing")
        return {"ok": True}

    assert cat.index_one("001.mp4", writing, now=104) is None
    row = cat.rows()[0]
    assert row["state"] == "pending"
    assert row["asset_id"] is None
    cat.scan(now=105)
    assert cat.index_one("001.mp4", inspector, now=109)


def test_parser_failure_isolated_and_retried(cat):
    (cat.root / "bad.json").write_text("{")
    (cat.root / "good.json").write_text("{}")
    cat.scan(now=100)
    def parse(p, *_):
        return json.loads(p.read_text()) or {"valid": True}
    assert cat.index_one("bad.json", parse, now=104) is None
    assert cat.index_one("good.json", parse, now=104)
    assert not cat.pending(now=150)
    assert len(cat.pending(now=170)) == 1


def test_incomplete_enumeration_never_marks_missing(cat, monkeypatch):
    (cat.root / "one.mp4").write_text("1")
    publish(cat, "one.mp4")

    def partial(*args, **kwargs):
        kwargs["onerror"](PermissionError("drive inaccessible"))
        return iter([])

    monkeypatch.setattr(os, "walk", partial)
    result = cat.scan(now=110)
    assert not result.complete and not result.missing
    assert cat.rows()[0]["state"] == "ready"


def test_move_entire_project_and_readonly_lock(cat, tmp_path):
    (cat.root / "one.mp4").write_text("1")
    result = publish(cat, "one.mp4")
    atomic_json(cat.work_path(result["asset_id"]), {"events": [1]})
    other = Catalog(cat.root)
    try:
        assert other.readonly
        assert other.rows()[0]["asset_id"] == result["asset_id"]
        with pytest.raises(PermissionError):
            other.scan()
    finally:
        other.close()
    destination = tmp_path.parent / (tmp_path.name + "_moved")
    cat.close()  # Move an entire project after closing its single writer.
    shutil.copytree(cat.root, destination)
    moved = Catalog(destination, stability_seconds=0)
    try:
        moved.scan(now=110)
        moved.index_one("one.mp4", lambda *_: pytest.fail("move re-OCR"), now=114)
        assert read_json(moved.work_path(result["asset_id"])) == {"events": [1]}
        assert moved.source_path("one.mp4").is_file()
    finally:
        moved.close()


def test_batches_sidecars_and_atomic_recovery(cat):
    one = unique_batch(cat.root, "08-03")
    two = unique_batch(cat.root, "08-03")
    assert one != two
    for folder in (one, two):
        (folder / "001.mp4").write_text(folder.name)
        (folder / "001.pic").write_text("sidecar")
    assert len(cat.scan(now=100).added) == 2
    settings = cat.meta / "project.json"
    atomic_json(settings, {"revision": 1})
    atomic_json(settings, {"revision": 2})
    settings.write_text("corrupt")
    assert read_json(settings) == {"revision": 1}
    with pytest.raises(ValueError):
        cat.source_path("../escape")
    with pytest.raises(ValueError):
        unique_batch(cat.root, "../escape")


def test_explicit_recheck_recomputes_derived_data_only(cat):
    (cat.root / "001.mp4").write_text("unchanged")
    original = publish(cat, "001.mp4")
    cat.recheck("001.mp4")
    updated = cat.index_one("001.mp4", lambda *_: {"new_index": True}, now=110)
    assert updated["asset_id"] == original["asset_id"]
    assert updated["metadata"] == {"new_index": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows copy write-sharing semantics")
def test_paused_preallocated_copy_with_open_writer_not_published(cat):
    path = cat.root / "preallocated.mp4"
    path.write_text("already a parseable prefix")
    cat.scan(now=100)
    with path.open("r+b"):
        # Neither length nor mtime changes while a copy is paused.
        assert cat.index_one("preallocated.mp4", inspector, now=110) is None
        assert cat.rows()[0]["state"] == "pending"
    assert cat.index_one("preallocated.mp4", inspector, now=114)


def test_reconciliation_stats_every_file_without_rereading_unchanged_media(cat, monkeypatch):
    (cat.root / "001.mp4").write_text("source")
    publish(cat, "001.mp4")
    import cowmata_tailring.workspace.catalog as module
    monkeypatch.setattr(module, "digest_file", lambda *_args, **_kwargs: pytest.fail("unchanged file reread"))
    result = cat.scan(now=110)
    assert result.complete and result.inspected == 1 and not result.changed
