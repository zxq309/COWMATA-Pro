from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import pytest

from cowmata_tailring.workspace import dahua_tasks as tasks
from cowmata_tailring.workspace.farm_layout import initialize_farm


@pytest.fixture
def incremental(tmp_path, monkeypatch):
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "locks"))
    farm, job = tmp_path / "farm", tmp_path / "job"
    initialize_farm(farm)
    sources = []
    for n in (1, 2):
        p = tmp_path / f"raw/channel-{n:02}.dav"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(f"original-{n}".encode())
        sources.append(p)
    index = tasks.scan(dict(mode="files", files=[str(p) for p in sources]), job)
    request = dict(target=str(farm), category="calving", scenario="mixed",
        mapping={r["group"]: f"视角{i:02}" for i, r in enumerate(index["rows"], 1)},
        json_sources=[], start="", end="", split_midnight=True)
    return farm, job, index, request, sources


def fake_prepared(row, index, request, job, cancelled, *args):
    # Substitute only the codec; archive/index/checksum/pause paths stay real.
    config = tasks.read_json(job / "dahua-storage.json", {})
    folder = Path(config.get("media_root", job)) / "records" / row["id"][:24]
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "normalized.dav"
    source.write_bytes(Path(row["source"]).read_bytes())
    p = folder / "prepared.mp4"
    p.write_bytes(b"verified media " + row["id"].encode())
    return [dict(path=str(p), sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
        start_ms=1789272000000, duration_ms=1000,
        metadata={"dahua": {"source_id": row["id"], "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}})]


def test_first_video_is_archived_before_second_record_is_read(incremental, monkeypatch):
    farm, job, index, request, sources = incremental
    entered = []
    def prepare(row, *args):
        if entered:
            assert len(list((farm / "录像").rglob("*.mp4"))) == 1
            resource_index = tasks.read_json(farm / "资源索引.json")
            assert any(r["path"].startswith("录像/") for r in resource_index["records"])
            assert not list((farm / ".归类缓存").rglob("normalized.dav"))
        entered.append(row["id"])
        return fake_prepared(row, *args)
    monkeypatch.setattr(tasks, "prepare_record", prepare)
    result = tasks.organize(request, job)
    assert result["status"] == "completed"
    assert len(list((farm / "录像").rglob("*.mp4"))) == 2
    assert not list((job / "records").rglob("*.mp4"))
    assert not list((farm / ".归类缓存").rglob("*.mp4"))
    assert result["result"]["same_volume_moved"] == 2
    assert [p.read_bytes() for p in sources] == [b"original-1", b"original-2"]


def test_pause_keeps_completed_video_and_resume_does_not_reencode(incremental, monkeypatch):
    farm, job, index, request, _ = incremental
    def prepare(row, *args):
        if row["id"] == index["rows"][1]["id"]:
            raise InterruptedError("test pause")
        return fake_prepared(row, *args)
    monkeypatch.setattr(tasks, "prepare_record", prepare)
    with pytest.raises(InterruptedError):
        tasks.organize(request, job)
    videos = list((farm / "录像").rglob("*.mp4"))
    assert len(videos) == 1
    before = videos[0].stat()
    def resumed(row, *args):
        assert row["id"] != index["rows"][0]["id"], "Already archived record was re-encoded"
        return fake_prepared(row, *args)
    monkeypatch.setattr(tasks, "prepare_record", resumed)
    result = tasks.organize(request, job)
    assert result["status"] == "completed"
    assert len(list((farm / "录像").rglob("*.mp4"))) == 2
    assert videos[0].stat().st_mtime_ns == before.st_mtime_ns


def test_each_record_has_persistent_status_and_elapsed_times(incremental, monkeypatch):
    farm, job, _, request, _ = incremental
    monkeypatch.setattr(tasks, "prepare_record", fake_prepared)
    events = []
    tasks.organize(request, job, on_row=lambda r: events.append(dict(r)))
    report = tasks.read_json(job / "dahua-run.json")
    assert report["status"] == "completed"
    assert report["elapsed_seconds"] >= 0
    assert len(report["records"]) == 2
    assert all(r["status"] in {"done", "existing"} and r["file_seconds"] >= 0 for r in report["records"])
    assert all(Path(r["targets"][0]).is_file() for r in report["records"])
    with (job / "视频任务记录.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert [r["状态"] for r in csv.DictReader(stream)] == ["已归档", "已归档"]
    assert any(r.get("phase") == "archive" for r in events)


def test_failed_record_does_not_block_next_and_never_becomes_complete(incremental, monkeypatch):
    farm, job, index, request, _ = incremental
    def prepare(row, *args):
        if row["id"] == index["rows"][0]["id"]:
            raise ValueError("bad clock")
        return fake_prepared(row, *args)
    monkeypatch.setattr(tasks, "prepare_record", prepare)
    result = tasks.organize(request, job)
    assert len(list((farm / "录像").rglob("*.mp4"))) == 1
    report = tasks.read_json(job / "dahua-run.json")
    assert [r["status"] for r in report["records"]] == ["blocked", "done"]
    assert result["issues"][0]["message"] == "bad clock"


def test_shared_farm_output_hint_and_open_button_use_same_recording_folder(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.dahua_ui import DahuaPanel, QDesktopServices
    app = QApplication.instance() or QApplication([])  # noqa: F841 - retain Qt application
    farm = tmp_path / "farm"
    initialize_farm(farm)
    panel = DahuaPanel()
    try:
        panel.target.setText(str(farm))
        assert str(farm / "录像") in panel.output_hint.text()
        assert "/ Video /" not in panel.output_hint.text()
        opened = []
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))
        panel.open_output()
        assert Path(opened[0]) == farm / "录像"
    finally:
        panel.close()


def test_changed_completed_output_is_reported_without_overwrite(incremental, monkeypatch):
    farm, job, _, request, _ = incremental
    monkeypatch.setattr(tasks, "prepare_record", fake_prepared)
    tasks.organize(request, job)
    target = sorted((farm / "录像").rglob("*.mp4"))[0]
    target.write_bytes(b"changed by another program")
    result = tasks.organize(request, job)
    assert target.read_bytes() == b"changed by another program"
    assert any("变化" in r["message"] for r in result["issues"])


def test_hardware_failure_falls_back_without_leaving_partial(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import dahua_media as media
    target = tmp_path / "out.mp4"
    monkeypatch.setattr(media, "available_encoder", lambda *a: "h264_nvenc")
    def encode(source, path, offset, duration, cancelled, *, encoder="libx264", stage=lambda *a, **k: None):
        path = Path(path)
        path.write_bytes(encoder.encode())
        if encoder != "libx264":
            raise ValueError("GPU unavailable")
        return {"info": {"format": {}}, "settings": {"encoder": encoder}}
    monkeypatch.setattr(media, "_encode", encode)
    result = media.transcode("unused.dav", target, 1000, 2000)
    assert result["settings"]["encoder"] == "libx264"
    assert target.read_bytes() == b"libx264"
    assert list(tmp_path.glob("*.mp4")) == [target]


def test_known_raw_disk_revalidation_does_not_enumerate_other_disks(monkeypatch):
    saved = dict(number=3, identity="stable", path=r"\\.\PhysicalDrive3")
    from cowmata_tailring.workspace import dahua_source
    monkeypatch.setattr(dahua_source, "disk_info", lambda number: dict(saved))
    monkeypatch.setattr(tasks, "disks", lambda: pytest.fail("All disks were scanned again"))
    assert tasks.fresh_disk(saved)["identity"] == "stable"


def test_2000_live_rows_are_batched_and_repeat_updates_do_not_duplicate():
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.dahua_run_ui import DahuaRunTables
    app = QApplication.instance() or QApplication([])  # noqa: F841 - retain Qt application
    view = DahuaRunTables()
    try:
        view.begin()
        for i in range(2000):
            row = dict(event_kind="task_record", source_id=str(i), status="processing",
                       source=f"channel:{i%20}", owner=f"视角{i%20+1:02}", targets=[],
                       phase="convert", message="硬件编码", file_seconds=i)
            view.accept(row)
            view.accept(dict(row, file_seconds=i+1))
        while view.pending:
            view.flush()
        assert view.live.rowCount() == 2000
        assert view.timing.rowCount() == 2000
        assert view.live.item(1999,5).text() == "2000"
    finally:
        view.close()


def test_resume_after_archive_receipt_interruption_does_not_duplicate(incremental, monkeypatch):
    farm, job, index, request, _ = incremental
    monkeypatch.setattr(tasks, "prepare_record", fake_prepared)
    def stop_after_move(row):
        if row.get("event_kind") == "archive_record" and row.get("status") == "done":
            raise InterruptedError("crash after move before receipt")
    with pytest.raises(InterruptedError):
        tasks.organize(request, job, on_row=stop_after_move)
    first = list((farm / "录像").rglob("*.mp4"))
    assert len(first) == 1
    before = first[0].stat().st_mtime_ns
    def resumed(row, *args):
        assert row["id"] != index["rows"][0]["id"]
        return fake_prepared(row, *args)
    monkeypatch.setattr(tasks, "prepare_record", resumed)
    tasks.organize(request, job)
    assert len(list((farm / "录像").rglob("*.mp4"))) == 2
    assert first[0].stat().st_mtime_ns == before


def test_archive_result_elapsed_includes_conversion(incremental, monkeypatch):
    import time
    _, job, _, request, _ = incremental
    def prepare(*args):
        time.sleep(0.04)
        return fake_prepared(*args)
    monkeypatch.setattr(tasks, "prepare_record", prepare)
    tasks.organize(request, job)
    report = tasks.read_json(job / "dahua-run.json")
    assert all(row["file_seconds"] >= row["transfer_seconds"] + 0.039 for row in report["outputs"])


def test_legacy_cache_migrates_verified_and_preview_keeps_prepared(incremental):
    from cowmata_tailring.workspace.dahua_run import configure_storage, record_folder, release_media
    farm, job, index, _, sources = incremental
    source_id = index["rows"][0]["id"]
    old = job / "records" / source_id[:24]
    old.mkdir(parents=True)
    (old / "normalized.dav").write_bytes(b"cached raw")
    (old / "prepared.mp4").write_bytes(b"completed encode")
    configure_storage(job, farm)
    adopted = record_folder(job, source_id, lambda: False)
    assert adopted.is_relative_to(farm)
    assert (adopted / "normalized.dav").read_bytes() == b"cached raw"
    assert (adopted / "prepared.mp4").read_bytes() == b"completed encode"
    assert not (old / "prepared.mp4").exists()
    release_media(job, source_id, normalized_only=True)
    assert (adopted / "prepared.mp4").read_bytes() == b"completed encode"
    assert all(source.is_file() for source in sources)
