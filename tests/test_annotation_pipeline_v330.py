"""Integration regressions at annotation import/export/category boundaries."""
import base64
import copy
import csv
import hashlib
import io
import struct
import time

import pytest
from PIL import Image
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from cowmata_tailring.annotation.core import (
    build_boris_csv,
    build_events_csv,
    build_meta,
    build_sample_multihot_csv,
    new_project,
    save_project,
)
from cowmata_tailring.annotation.data import load_motion_json
from cowmata_tailring.workspace.catalog import Catalog, digest_file
from cowmata_tailring.workspace.clocks import Anchor, ClockMap
from cowmata_tailring.workspace.evidence import evidence_summary, store_bundle
from cowmata_tailring.workspace.label_file import build_label_file, load_history, save_label_file
from cowmata_tailring.workspace.modern_window import MainWindow
from cowmata_tailring.workspace.storage import atomic_json, read_json
from cowmata_tailring.workspace.team import restore_label
from cowmata_tailring.workspace.work import SessionWork


def test_finishing_cross_record_action_survives_save_failure_without_duplicate_drafts(window, monkeypatch):
    from cowmata_tailring.workspace import window as module

    window.board.main_camera = "A"
    position = [10200]
    monkeypatch.setattr(window, "evidence", lambda: [{"camera": "A", "frame_ready": True, "reference_ms": position[0]}])
    other_id = "0" * 64
    other = SessionWork(other_id)
    other.project.cow_id = window.work.project.cow_id
    atomic_json(window.catalog.work_path(other_id), other.to_dict())
    window.active_event = {"label": 0, "start": 10100, "evidence": [], "group_id": "stable-action",
                           "assets": {other_id, window.work.asset_id}, "cow_id": window.work.project.cow_id}
    write = module.atomic_json
    failures = [1]
    messages = []
    monkeypatch.setattr(window, "tell", messages.append)

    def fail_current_once(path, value):
        if path == window.catalog.work_path(window.work.asset_id) and failures[0]:
            failures[0] -= 1
            raise PermissionError("annotation locked")
        write(path, value)

    monkeypatch.setattr(module, "atomic_json", fail_current_once)
    window.mark(0)
    assert window.dirty and window.active_event is not None
    assert len(window.work.drafts) == 1
    assert any("保存失败" in message for message in messages)
    position[0] = 10800  # Retry must preserve the originally observed end frame.
    window.mark(0)
    assert window.active_event is None
    for asset_id in (other_id, window.work.asset_id):
        saved = SessionWork.from_dict(read_json(window.catalog.work_path(asset_id)))
        assert len(saved.drafts) == 1
        assert saved.drafts[0]["reference_end"] == 10200
        assert saved.drafts[0]["group_id"] == "stable-action"



@pytest.fixture
def case(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    raw = root / "record.json"
    frames = b"".join(struct.pack("<I9h", t, *([1] * 9)) for t in range(0, 1001, 20))
    atomic_json(raw, {"version": 2, "device": "A", "create_time": 1785732300000,
                      "imu": base64.b64encode(frames).decode()})
    video = root / "view01.mp4"
    video.write_bytes(b"synthetic video identity; decoding tested separately")
    motion = load_motion_json(raw)
    catalog = Catalog(root, stability_seconds=0)
    catalog.scan(now=1)
    catalog.index_one(raw.name, lambda *_: {"duration_ms": motion.duration_ms, "device": "A"}, now=2)
    catalog.index_one(video.name, lambda *_: {"camera": "A", "intervals": [
        {"wall_start": 10000, "wall_end": 11000, "media_start": 0, "media_end": 1000, "verified": True}]}, now=2)
    yield catalog, motion
    catalog.close()


@pytest.fixture
def window(case, monkeypatch):
    app = QApplication.instance() or QApplication([])
    catalog, motion = case
    window = MainWindow()
    for timer in (window.save_timer, window.source_timer, window.board.timer):
        timer.stop()
    window.catalog, window.motion = catalog, motion
    window.rows = catalog.rows()
    window.current_row = next(r for r in window.rows if r["kind"] == "imu")
    window.current_stamp = window.current_row["stamp"]
    window.source_available = True
    window.work = SessionWork(digest_file(motion.source_path))
    window.work.clock = ClockMap.from_capture(motion)
    monkeypatch.setattr(window, "confirm_close", lambda: "save")
    yield window
    window.close()
    app.processEvents()


@pytest.mark.parametrize("with_clock", [True, False])
def test_event_table_shows_reference_timestamps_without_changing_coordinates(window, with_clock):
    from cowmata_tailring.workspace.clocks import wall_ms

    window.work.clock = ClockMap([Anchor(0, wall_ms("2026-09-10 12:34:56"))] if with_clock else [])
    window.work.project.add_event(0, 20, 40)
    window.refresh_events()
    expected = ("2026-09-10 12:34:56.020", "2026-09-10 12:34:56.040") if with_clock else ("相对 0.020 秒", "相对 0.040 秒")
    assert (window.events.item(0, 2).text(), window.events.item(0, 3).text()) == expected
    assert [(event.t0, event.t1) for event in window.work.project.events] == [(20, 40)]
    assert window.events.horizontalHeaderItem(2).text() == "开始时间"


def test_category_only_local_review_is_not_overwritten_by_team_return(case, tmp_path):
    catalog, motion = case
    local = SessionWork(digest_file(motion.source_path))
    local.clock = ClockMap.from_capture(motion)
    local.set_category("estrus", context={"assigned_by": "manual_record_review"})
    atomic_json(catalog.work_path(local.asset_id), local.to_dict())
    incoming = SessionWork(local.asset_id)
    incoming.project.cow_id = "COW-A"
    incoming.progress = {"status": "done", "imu_ms": 1000}
    incoming.set_category("healthy")
    path = tmp_path / "return.json"
    save_label_file(path, build_label_file(incoming, motion, catalog.root, catalog.rows(), {}))
    result = restore_label(catalog, path)
    assert result["status"] == "conflict"
    assert read_json(catalog.work_path(local.asset_id))["project"]["dataset_category"] == "estrus"


@pytest.mark.parametrize("category", ["healthy", "pregnancy_early", "pregnancy_mid", "pregnancy_late"])
def test_accept_return_refreshes_current_category_control(window, tmp_path, category):
    incoming = copy.deepcopy(window.work)
    incoming.project.cow_id = "COW-A"
    incoming.set_category(category)
    incoming.progress = {"status": "done", "imu_ms": 1000}
    path = tmp_path / "return.json"
    save_label_file(path, build_label_file(incoming, window.motion, window.catalog.root, window.rows, {}))
    window.import_team_labels(paths=[str(path)], automatic=True)
    assert window.work.project.extras["dataset_category"] == category
    assert window.data_category.currentData() == category


def test_import_legacy_keeps_organized_category_and_updates_cow_control(window, tmp_path, monkeypatch):
    window.work.set_category("estrus", context={"task_id": "organized-batch"})
    window.cow.setText("PREVIOUS-COW")
    legacy = new_project()
    legacy.cow_id = "LEGACY-COW"
    legacy.add_event(0, 100, 200)
    path = tmp_path / "legacy.json"
    save_project(legacy, path)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), "JSON"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.import_legacy()
    assert window.work.project.extras["dataset_category"] == "estrus"
    assert window.work.project.extras["collection_context"] == {"task_id": "organized-batch"}
    assert window.work.project.events[0].extras["dataset_category"] == "estrus"
    assert window.work.project.events[0].extras["confirmation"] == "legacy_unreviewed"
    assert window.cow.text() == "LEGACY-COW"
    assert window.data_category.currentData() == "estrus"
    saved = read_json(window.catalog.work_path(window.work.asset_id))
    assert saved["project"]["dataset_category"] == "estrus"


def prepare_export(window, tmp_path, monkeypatch, *, category="calving"):
    window.work.project.cow_id = "COW-A"
    window.work.clock = ClockMap([Anchor(0, 10000), Anchor(1000, 11000)])
    window.work.set_category(category)
    row = next(r for r in window.rows if r["kind"] == "video")
    proof = [{"camera": "A", "asset_id": row["asset_id"], "frame_ready": True, "verified_interval": True,
              "camera_mapping_revision": "uncalibrated", "video_revision": window.video_revision(row)}]
    draft = window.work.add_draft(0, 10100, 10200, proof)
    event = window.work.confirm_draft(draft["id"], 1000)
    stream = io.BytesIO()
    Image.new("RGB", (24, 16), "teal").save(stream, format="JPEG")
    payload = stream.getvalue()
    sha = hashlib.sha256(payload).hexdigest()
    bundle = {"schema": 1, "human_checked": True, "items": [
        {"status": "captured", "sha256": sha, "path": f"证据/{sha}.jpg", "bytes": len(payload),
         "width": 24, "height": 16}]}
    event.extras["screenshots"] = store_bundle(window.catalog.meta, bundle, {sha: payload})
    output = tmp_path / "export"
    output.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(output))
    window.export_training()
    return output / ("COWMATA_" + window.work.asset_id[:8])


def test_training_bundle_keeps_human_evidence_portable(window, tmp_path, monkeypatch):
    output = prepare_export(window, tmp_path, monkeypatch)
    document = {"work": read_json(output / "全部人工成果.json")}
    assert evidence_summary(document, output)["saved"] == 1
    history = load_history(output / "全部人工成果.json", root=window.catalog.root)
    assert any("已校验证据图 1 张" in warning for warning in history.warnings)


@pytest.mark.parametrize("category,label", [("calving", "产犊"), ("pregnancy_early", "孕早期"),
                                             ("pregnancy_mid", "孕中期"), ("pregnancy_late", "孕晚期")])
def test_reference_evidence_export_has_collection_category(window, tmp_path, monkeypatch, category, label):
    output = prepare_export(window, tmp_path, monkeypatch, category=category)
    with (output / "参考时间与视频证据.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["dataset_category"] == category
    assert rows[0]["dataset_category_label"] == label


def test_training_export_keeps_gui_responsive_and_close_preserves_catalog(window, tmp_path, monkeypatch):
    from cowmata_tailring.workspace import window as module
    write = module.atomic_json
    in_write = [False]
    ticks = []
    def slow_write(path, value):
        in_write[0] = True
        time.sleep(.25)  # Controlled filesystem latency at the I/O boundary.
        try:
            return write(path, value)
        finally:
            in_write[0] = False
    def tick():
        if in_write[0]:
            window.close()
            ticks.append((window._closed, window.catalog.lock.file.closed))
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(tick)
    monkeypatch.setattr(module, "atomic_json", slow_write)
    timer.start()
    try:
        output = prepare_export(window, tmp_path, monkeypatch)
    finally:
        timer.stop()
    assert ticks and all(value == (False, False) for value in ticks)
    assert (output / "逐样本标签.csv").is_file()


def test_team_receive_keeps_gui_responsive_during_full_source_hash(window, tmp_path, monkeypatch):
    from cowmata_tailring.workspace import team
    incoming = copy.deepcopy(window.work)
    incoming.project.cow_id = "COW-A"
    incoming.progress = {"status": "done", "imu_ms": 1000}
    path = tmp_path / "return.json"
    save_label_file(path, build_label_file(incoming, window.motion, window.catalog.root, window.rows, {}))
    digest = team.digest_file
    in_hash, ticks = [False], []
    def slow_hash(path):
        in_hash[0] = True
        time.sleep(.25)  # Controlled source-read latency; retain the full content check.
        try:
            return digest(path)
        finally:
            in_hash[0] = False
    def tick():
        if in_hash[0]:
            window.close()
            ticks.append((window._closed, window.catalog.lock.file.closed))
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(tick)
    monkeypatch.setattr(team, "digest_file", slow_hash)
    timer.start()
    try:
        window.import_team_labels(paths=[str(path)], automatic=True)
    finally:
        timer.stop()
    assert ticks and all(value == (False, False) for value in ticks)
    assert window.work.project.cow_id == "COW-A"


def test_failed_background_export_releases_guard_and_keeps_active_work(window, tmp_path, monkeypatch):
    from cowmata_tailring.workspace import window as module
    def failed(*args, **kwargs):
        raise OSError("synthetic disk full")
    monkeypatch.setattr(module, "export_sample_multihot_csv", failed)
    prepare_export(window, tmp_path, monkeypatch)
    assert not window._export_running
    assert not window._closed and not window.catalog.lock.file.closed
    assert window.work.project.cow_id == "COW-A" and window.work.project.events
    assert "synthetic disk full" in window._status_history[-1][1]


def test_event_csv_uses_capture_counter_and_workspace_timestamp():
    project = new_project()
    project.source = {"createTimeMs": 1785732300000,
                      "capture_timing": {"first_frame_elapsed_ms": 2500, "coordinate_offset_ms": 0}}
    project.add_event(0, 100, 200)
    row = next(csv.DictReader(io.StringIO(build_events_csv(project))))
    assert row["t_start_wall_bj"] == "2026-08-03 12:45:02.600"
    assert row["t_end_wall_bj"] == "2026-08-03 12:45:02.700"


def identity(cow="00123", mark="A03"):
    return {"status": "ready", "device_id": "0C3D5EA22E36", "cow_id": cow, "field_mark": mark,
            "source_folder": f"0C3D5EA22E36-{cow}-{mark}", "message": ""}


@pytest.mark.parametrize("category", ["healthy", "pregnancy_early", "pregnancy_mid", "pregnancy_late"])
def test_record_identity_survives_all_label_and_csv_outputs(case, tmp_path, category):
    catalog, motion = case
    work = SessionWork(digest_file(motion.source_path))
    work.clock = ClockMap([Anchor(0, 10000), Anchor(1000, 11000)])
    work.bind_device_identity(identity(), source_path="九轴/0C3D5EA22E36-00123-A03/2026-08-03/record.json",
                              capture_timing=motion.capture_timing())
    work.set_category(category)
    draft = work.add_draft(0, 10100, 10200, [{"frame_ready": True, "verified_interval": True}])
    work.confirm_draft(draft["id"], 1000)
    work.checkpoint()
    work.set_category("disease")
    assert work.undo_once()
    work = SessionWork.from_dict(copy.deepcopy(work.to_dict()))
    assert work.drafts[0]["dataset_category"] == category
    assert work.project.events[0].extras["dataset_category"] == category
    assert work.project.cow_id == "00123"
    assert work.project.events[0].extras["device_identity"]["field_mark"] == "A03"
    for selection in (None, (80, 220)):
        doc = build_label_file(work, motion, catalog.root, catalog.rows(), {}, selection=selection)
        assert doc["device_identity"]["device_id"] == "0C3D5EA22E36"
        assert doc["device_identity"]["cow_id"] == "00123"
        assert doc["device_identity"]["field_mark"] == "A03"
        assert doc["dataset_category"] == category
        path = tmp_path / ("full.json" if selection is None else "snippet.json")
        save_label_file(path, doc)
        assert load_history(path).work.project.extras["device_identity"]["field_mark"] == "A03"
    for text in (build_events_csv(work.project), build_sample_multihot_csv(work.project, [100, 120]), build_boris_csv(work.project)):
        row = next(csv.DictReader(io.StringIO(text)))
        assert row["dataset_category"] == category
        assert (row["cow_id"], row["device_id"], row["field_mark"]) == ("00123", "0C3D5EA22E36", "A03")
    assert build_meta(work.project)["device_identity"]["field_mark"] == "A03"
    assert build_meta(work.project)["dataset_category"] == category


def test_auto_loaded_matching_identity_and_category_do_not_block_team_return(case, tmp_path):
    catalog, motion = case
    local = SessionWork(digest_file(motion.source_path))
    local.clock = ClockMap.from_capture(motion)
    local.set_category("healthy", context={"task_id": "organized"})
    local.bind_device_identity(identity(), source_path="record.json", capture_timing=motion.capture_timing())
    atomic_json(catalog.work_path(local.asset_id), local.to_dict())
    incoming = SessionWork.from_dict(copy.deepcopy(local.to_dict()))
    incoming.project.add_event(0, 100, 200)
    incoming.progress = {"status": "done", "imu_ms": 1000}
    path = tmp_path / "return.json"
    save_label_file(path, build_label_file(incoming, motion, catalog.root, catalog.rows(), {}))
    assert restore_label(catalog, path)["status"] == "imported"


def test_old_nonstandard_folder_keeps_manual_labels_and_warns(window, monkeypatch):
    window.work.project.cow_id = "LEGACY-COW"
    window.work.project.add_event(0, 100, 200)
    window.save_current()
    row = window.current_row
    monkeypatch.setattr(window.board, "seek", lambda value: None)
    window._motion_loaded((window.load_generation, row, window.motion, row["stamp"], False))
    assert window.cow.text() == "LEGACY-COW"
    assert len(window.work.project.events) == 1
    assert window.work.project.extras["device_identity"]["status"] == "blocked"
    assert window.identity_label.text()
    assert window.source_available


def test_saved_cow_conflict_is_preserved_until_explicit_review(case):
    _, motion = case
    work = SessionWork(digest_file(motion.source_path))
    work.project.cow_id = "00999"
    event = work.project.add_event(0, 100, 200)
    event.extras["confirmation"] = "confirmed"
    work.progress["status"] = "done"
    work.bind_device_identity(identity(), source_path="record.json", capture_timing=motion.capture_timing())
    assert work.project.cow_id == "00999"
    assert work.project.extras["device_identity"]["status"] == "conflict"
    assert event.extras["confirmation"] == "needs_review"
    assert work.progress["status"] == "in_progress"
    work.confirm_cow("00999")
    work = SessionWork.from_dict(copy.deepcopy(work.to_dict()))
    work.bind_device_identity(identity(), source_path="record.json", capture_timing=motion.capture_timing())
    assert work.project.cow_id == "00999"
    assert work.project.extras["device_identity"]["status"] == "manual_override"
    assert work.project.extras["device_identity"]["folder_cow_id"] == "00123"


def test_cow_conflict_cannot_be_confirmed_without_reviewing_identity(case):
    _, motion = case
    work = SessionWork(digest_file(motion.source_path))
    work.clock = ClockMap([Anchor(0, 10000), Anchor(1000, 11000)])
    work.project.cow_id = "00999"
    draft = work.add_draft(0, 10100, 10200, [{"frame_ready": True, "verified_interval": True}])
    work.bind_device_identity(identity(), source_path="record.json", capture_timing=motion.capture_timing())
    with pytest.raises(ValueError, match="牛号|耳标"):
        work.confirm_draft(draft["id"], 1000)
    work.confirm_cow("00999")
    work.confirm_draft(draft["id"], 1000)
    assert work.training_project().events


def test_legacy_relocation_never_erases_saved_record_identity(case):
    _, motion = case
    work = SessionWork(digest_file(motion.source_path))
    work.bind_device_identity(identity(), source_path="original/record.json", capture_timing=motion.capture_timing())
    moved = {"status": "blocked", "device_id": "0C3D5EA22E36", "cow_id": "", "field_mark": "",
             "source_folder": "old-name", "message": "目录命名待规范"}
    for _ in range(2):
        work = SessionWork.from_dict(copy.deepcopy(work.to_dict()))
        work.bind_device_identity(moved, source_path="old-name/record.json", capture_timing=motion.capture_timing())
        assert work.project.cow_id == "00123"
        assert work.project.extras["device_identity"]["field_mark"] == "A03"
        assert work.project.extras["device_identity"]["saved_identity"]["source_folder"] == "0C3D5EA22E36-00123-A03"


def test_same_device_reused_on_different_dates_keeps_each_cow_on_gui_reopen(window, monkeypatch):
    catalog = window.catalog
    records = []
    for date, cow, mark, epoch in (("2026-08-03", "00123", "A03", 1785732300000),
                                    ("2026-08-04", "00999", "b2", 1785818700000)):
        path = catalog.root / "九轴" / f"0C3D5EA22E36-{cow}-{mark}" / date / "motion" / "record.json"
        data = read_json(window.motion.source_path)
        data.update(device="0C3D5EA22E36", create_time=epoch)
        atomic_json(path, data)
        records.append((path, cow, mark))
    catalog.scan(now=10)
    for path, _, _ in records:
        catalog.index_one(path.relative_to(catalog.root).as_posix(), lambda *_: {"duration_ms": 1000, "device": "0C3D5EA22E36"}, now=11)
    window.rows = catalog.rows()
    monkeypatch.setattr(window.board, "seek", lambda value: None)  # No encoded video is involved in record identity loading.
    for path, cow, mark in [*records, *reversed(records)]:
        row = next(r for r in window.rows if r["path"] == path.relative_to(catalog.root).as_posix())
        motion = load_motion_json(path)
        window._motion_loaded((window.load_generation, row, motion, row["stamp"], False))
        assert window.cow.text() == cow
        assert window.work.project.extras["device_identity"]["field_mark"] == mark
        window.save_current()
        saved = read_json(catalog.work_path(row["asset_id"]))
        assert saved["project"]["cow_id"] == cow
        doc = build_label_file(window.work, motion, catalog.root, window.rows, {})
        assert (doc["device_identity"]["cow_id"], doc["device_identity"]["field_mark"]) == (cow, mark)
