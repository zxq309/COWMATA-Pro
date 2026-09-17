import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from legacy_organization import plan_import as legacy_plan_import

from cowmata_tailring.workspace import organization as org
from cowmata_tailring.workspace.catalog import Catalog
from cowmata_tailring.workspace.dataset_access import DatasetLease, ensure_available


@pytest.fixture(autouse=True)
def isolated_access(monkeypatch, tmp_path):
    # Exercise replay of the saved 3.3 move-plan format. New GUI uses resource_import.
    monkeypatch.setattr(org, "plan_import", legacy_plan_import)
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))


def imu(path, device="546C50CA07D5"):
    path = path.parent / (device + "-00123-w1") / path.name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"device": device, "version": 0, "create_time": 1785732298000,
                               "imu": base64.b64encode(bytes(18 * 4)).decode()}), encoding="utf-8")
    return path


def video(path, data=b"\x00\x00\x01\xba" + b"original-video" * 16):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def spec(path, kind="video", camera="视角01"):
    return {"path": str(path), "kind": kind, "camera": camera}


@pytest.mark.parametrize("end", [None, "2026-08-07"])
def test_single_device_or_period_and_nested_video_flatten_without_copy(tmp_path, monkeypatch, end):
    one = imu(tmp_path / "download/深层/设备/one.json")
    clip = video(tmp_path / "camera/各种/嵌套/001.mp4")
    another = video(tmp_path / "camera/别处/002.mp4")
    junk = tmp_path / "camera/各种/readme.txt"
    junk.write_text("camera companion metadata")
    target = tmp_path / "farm/数据工程"
    originals = {p.name: (org.identity(p), hashlib.sha256(p.read_bytes()).hexdigest()) for p in (one, clip, another, junk)}
    plan = org.plan_import(target, [spec(one, "imu"), spec(tmp_path / "camera")], "2026-08-03", end, category="healthy")
    assert not target.exists() and one.is_file() and clip.is_file()
    assert sum(r["status"] == "quarantine" for r in plan["rows"]) == 1
    monkeypatch.setattr(org, "move_no_replace", org.move_no_replace)
    result = org.execute(plan, tmp_path / "job")
    assert result["completed"] and result["moved"] == 4
    period = "2026-08-03" if not end else "2026-08-03至2026-08-07"
    assert (target / "九轴/546C50CA07D5-00123-w1/2026-08-03/one.json").is_file()
    assert sorted(p.name for p in (target / "视角01" / period).iterdir()) == ["001.mp4", "002.mp4"]
    assert all((target / name).is_dir() for name in org.VIEWS)
    assert not one.exists() and not clip.exists() and not junk.exists()
    assert not list((tmp_path / "camera").iterdir())
    for row in plan["rows"]:
        moved = Path(row["target"])
        assert org.identity(moved) == originals[moved.name][0]
        assert hashlib.sha256(moved.read_bytes()).hexdigest() == originals[moved.name][1]
    with DatasetLease([target]):
        pass


def test_duplicate_names_use_distinct_date_batches_without_overwrite(tmp_path):
    first = video(tmp_path / "input/card1/001.mp4", b"a")
    second = video(tmp_path / "input/card2/deep/001.mp4", b"b")
    target = tmp_path / "target"
    plan = org.plan_import(target, [spec(tmp_path / "input")], "2026-08-03", category="healthy")
    destinations = [r["target"] for r in plan["rows"]]
    assert len(set(destinations)) == 2
    assert {Path(p).parent.name for p in destinations} == {"2026-08-03", "2026-08-03_001"}
    org.execute(plan, tmp_path / "job")
    assert {Path(p).read_bytes() for p in destinations} == {b"a", b"b"}
    assert not first.exists() and not second.exists()


def test_cross_volume_and_new_conflicts_never_fall_back_to_copy(tmp_path, monkeypatch):
    clip = video(tmp_path / "input/001.mp4")
    target = tmp_path / "target"
    actual = org.volume
    monkeypatch.setattr(org, "volume", lambda path: -1 if path == target else actual(path))
    plan = org.plan_import(target, [spec(clip)], "2026-08-03", category="healthy")
    assert plan["rows"][0]["status"] == "blocked"
    with pytest.raises(ValueError):
        org.execute(plan, tmp_path / "job")
    assert clip.exists() and not target.exists()
    monkeypatch.setattr(org, "volume", actual)
    plan = org.plan_import(target, [spec(clip)], "2026-08-03", category="healthy")
    destination = video(Path(plan["rows"][0]["target"]), b"DO NOT OVERWRITE")
    with pytest.raises(ValueError):
        org.execute(plan, tmp_path / "job")
    assert destination.read_bytes() == b"DO NOT OVERWRITE" and clip.exists()


def test_interrupted_move_blocks_annotation_then_resumes_without_moving_twice(tmp_path):
    clips = [video(tmp_path / "input" / f"{i}.mp4") for i in range(3)]
    target = tmp_path / "target"
    plan = org.plan_import(target, [spec(tmp_path / "input")], "2026-08-03", category="healthy")
    progress = []
    with pytest.raises(InterruptedError):
        org.execute(plan, tmp_path / "job", cancelled=lambda: bool(progress), progress=lambda *args: progress.append(args))
    assert sum(p.exists() for p in clips) == 2
    with pytest.raises(OSError, match="未完成"):
        Catalog(target)
    result = org.execute(plan, tmp_path / "job")
    assert result["moved"] == 3 and all(not p.exists() for p in clips)
    journal = [json.loads(line) for line in (tmp_path / "job/journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(r["phase"] == "moved" for r in journal) == 3
    catalog = Catalog(target)
    assert len(catalog.rows()) == 3
    catalog.close()


def test_files_changed_after_preview_stop_before_any_move(tmp_path):
    clip = video(tmp_path / "input/001.mp4")
    plan = org.plan_import(tmp_path / "target", [spec(clip)], "2026-08-03", category="healthy")
    clip.write_bytes(b"changed")
    with pytest.raises(ValueError):
        org.execute(plan, tmp_path / "job")
    assert clip.read_bytes() == b"changed" and not (tmp_path / "target").exists()


def test_protected_labels_and_bad_imu_never_become_junk(tmp_path):
    label = tmp_path / "label.json"
    label.write_text(json.dumps({"format": "cowmata-annotation", "imu": "fake"}))
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"device": "A", "imu": "malformed!"}))
    protected = tmp_path / "标签.标注.json"
    protected.write_text("{}")
    report = org.audit([tmp_path])
    by_name = {Path(r["source"]).name: r for r in report["rows"]}
    assert by_name[label.name]["protected"]
    assert by_name[broken.name]["status"] == "invalid"
    assert not by_name[broken.name]["quarantine"]
    plan = org.plan_import(tmp_path.parent / (tmp_path.name + "-target"), [spec(tmp_path)], "2026-08-03", category="healthy")
    assert not any(r["status"] in {"ready", "quarantine"} for r in plan["rows"])


def test_annotation_and_organizing_lease_overlap_in_both_directions(tmp_path):
    parent = tmp_path / "farm"
    with DatasetLease([parent]):
        with pytest.raises(OSError):
            DatasetLease([parent / "数据工程"], "organize")
        with DatasetLease([tmp_path / "other"], "organize"):
            pass
    with DatasetLease([parent / "数据工程"], "organize"):
        with pytest.raises(OSError):
            DatasetLease([parent])


def test_other_process_obeys_live_dataset_lease(tmp_path):
    target = tmp_path / "farm"
    source = Path(__file__).resolve().parents[1]
    script = ("import sys;sys.path.insert(0,sys.argv[1]);"
              "from cowmata_tailring.workspace.dataset_access import DatasetLease;"
              "DatasetLease([sys.argv[2]],'organize')")
    with DatasetLease([target]):
        result = subprocess.run([sys.executable, "-B", "-c", script, str(source), str(target)],
                                capture_output=True, timeout=20)
    assert result.returncode != 0
    ensure_available([target], "organize")


@pytest.mark.skipif(os.name != "nt", reason="Windows file share protection")
def test_active_writer_prevents_move(tmp_path):
    import ctypes
    from ctypes import wintypes
    clip = video(tmp_path / "input/001.mp4")
    plan = org.plan_import(tmp_path / "target", [spec(clip)], "2026-08-03", category="healthy")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(clip), 0x40000000, 7, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        with pytest.raises(OSError):
            org.execute(plan, tmp_path / "job")
        assert clip.exists()
    finally:
        kernel.CloseHandle(handle)


def test_normalizing_names_preserves_annotation_coordinates_and_index_identity(tmp_path):
    root = tmp_path / "project"
    clip = video(root / "视角01_海康/2026-08-03_001/001.mp4")
    catalog = Catalog(root, stability_seconds=0)
    catalog.scan(fast=True, now=0)
    relative = clip.relative_to(root).as_posix()
    sha = hashlib.sha256(clip.read_bytes()).hexdigest()
    catalog.db.execute("INSERT INTO assets VALUES(?,?,?,?)", (sha, "video", json.dumps({"camera": "视角01_海康"}), 0))
    catalog.db.execute("UPDATE locations SET asset_id=?,state='ready' WHERE path=?", (sha, relative))
    catalog.db.commit()
    catalog.save_settings({"camera_maps": {"视角01_海康": {"anchors": [[0, 1000], [1000, 2000]]}}, "current_path": relative})
    work = catalog.meta / "annotations" / (sha + ".json")
    work.parent.mkdir()
    value = {"asset_id": sha, "labels": [{"start": 123.45, "end": 345.67}], "source_path": relative}
    work.write_text(json.dumps(value))
    catalog.close()
    plan = org.plan_normalize(root)
    org.execute(plan, tmp_path / "job")
    updated = json.loads(work.read_text(encoding="utf-8"))
    assert updated["labels"] == value["labels"]
    assert updated["asset_id"] == sha and updated["source_path"].startswith("视角01/")
    catalog = Catalog(root)
    assert catalog.rows()[0]["asset_id"] == sha and catalog.rows()[0]["state"] == "ready"
    assert catalog.settings()["camera_maps"]["视角01"]["anchors"] == [[0, 1000], [1000, 2000]]
    catalog.close()


def test_qprocess_audit_keeps_event_loop_alive_and_preserves_sources(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtWidgets import QApplication, QMainWindow

    from cowmata_tailring.workspace import organization_ui
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(organization_ui, "task_root", lambda: tmp_path / "jobs")
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(organization_ui, "QSettings", lambda: settings)
    owner = QMainWindow()
    owner.tell = lambda *_: None
    dialog = organization_ui.OrganizationWindow(owner)
    path = imu(tmp_path / "source/one.json")
    dialog.add_source("imu", path)
    beats = []
    timer = QTimer()
    timer.timeout.connect(lambda: beats.append(time.monotonic()))
    timer.start(10)
    dialog.start_audit()
    deadline = time.monotonic() + 30
    while dialog.running and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.002)
    app.processEvents()
    timer.stop()
    assert not dialog.running and dialog.report is not None, dialog.status.text()
    assert len(beats) >= 2 and path.exists()
    assert dialog.model.rows[0]["device"] == "546C50CA07D5"
    dialog.close()
    owner.close()


def test_empty_video_is_isolated_and_bad_json_and_bindings_are_retained(tmp_path):
    empty = video(tmp_path / "input/deep/empty.mp4", b"")
    label = tmp_path / "input/绑定.csv"
    label.write_text("animal metadata")
    broken = tmp_path / "input/deep/broken.json"
    broken.write_text('{"imu":"bad",')
    plan = org.plan_import(tmp_path / "target", [spec(tmp_path / "input")], "2026-08-03", category="healthy")
    result = org.execute(plan, tmp_path / "job")
    assert result["moved"] == 1
    assert not empty.exists() and label.exists() and broken.exists()
    assert not list((tmp_path / "target/视角01").rglob("*.mp4"))


def test_normalize_refuses_colliding_calibrations_before_moving(tmp_path):
    clip = video(tmp_path / "project/视角01_海康/day/one.mp4")
    catalog = Catalog(tmp_path / "project")
    maps = {"视角01_海康": {"anchors": [[0, 100]]}, "视角01": {"anchors": [[0, 200]]}}
    catalog.save_settings({"camera_maps": maps})
    catalog.close()
    plan = org.plan_normalize(tmp_path / "project")
    with pytest.raises(ValueError, match="重名"):
        org.execute(plan, tmp_path / "job")
    assert clip.exists()
    ensure_available([tmp_path / "project"], "organize")


def test_bad_manifest_stops_before_moves_and_does_not_leave_pending_task(tmp_path):
    clip = video(tmp_path / "input/one.mp4")
    target = tmp_path / "target"
    target.mkdir()
    (target / "整理清单.csv").write_text("unexpected header")
    plan = org.plan_import(target, [spec(clip)], "2026-08-03", category="healthy")
    with pytest.raises(ValueError, match="表头"):
        org.execute(plan, tmp_path / "job")
    ensure_available([target])
    assert clip.exists()


def test_crash_after_manifest_write_resumes_without_duplicate_entries(tmp_path, monkeypatch):
    import csv
    clip = video(tmp_path / "input/one.mp4")
    target = tmp_path / "target"
    plan = org.plan_import(target, [spec(clip)], "2026-08-03", category="healthy")
    original = org.append_journal
    def fail_done(path, entry):
        if entry["phase"] == "done":
            with path.open("ab") as stream:
                stream.write(b'{"phase":')
            raise OSError("simulated crash while appending")
        original(path, entry)
    monkeypatch.setattr(org, "append_journal", fail_done)
    with pytest.raises(OSError):
        org.execute(plan, tmp_path / "job")
    assert not clip.exists()
    monkeypatch.setattr(org, "append_journal", original)
    assert org.execute(plan, tmp_path / "job")["completed"]
    with (target / "整理清单.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 1


def test_ui_form_edits_invalidate_previous_plan(tmp_path):
    from PySide6.QtWidgets import QApplication, QMainWindow

    from cowmata_tailring.workspace.organization_ui import OrganizationWindow
    app = QApplication.instance() or QApplication([])
    owner = QMainWindow()
    dialog = OrganizationWindow(owner)
    dialog.plan = {"rows": [{"status": "ready"}]}
    dialog.render()
    assert dialog.execute_button.isEnabled()
    dialog.add_source("video", tmp_path / "changed")
    assert dialog.plan is None and not dialog.execute_button.isEnabled()
    dialog.close()
    owner.close()
    app.processEvents()


def test_save_pause_drains_earlier_autosave_and_releases_dataset(tmp_path, monkeypatch):
    from concurrent.futures import Future

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from cowmata_tailring.workspace.modern_window import MainWindow
    app = QApplication.instance() or QApplication([])
    root = tmp_path / "project"
    root.mkdir()
    window = MainWindow()
    window.catalog = Catalog(root)
    window.settings["organization_test"] = "final snapshot"
    window.dirty = True
    old = Future()
    window.snapshot_writer.pending = old
    QTimer.singleShot(100, lambda: old.set_result(None))
    messages = []
    window.pause_for_organization(messages.append)
    assert window.catalog is not None
    with pytest.raises(OSError):
        ensure_available([root], "organize")
    deadline = time.monotonic() + 8
    while not messages and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert messages and window.catalog is None, messages
    settings = json.loads((root / "标注工程/project.json").read_text(encoding="utf-8"))
    assert settings["organization_test"] == "final snapshot"
    ensure_available([root], "organize")
    window.close()
    app.processEvents()


def test_category_required_and_separate_batches_keep_their_own_category(tmp_path):
    from cowmata_tailring.workspace.data_category import read_context
    first = imu(tmp_path / "batch1/one.json")
    second = imu(tmp_path / "batch2/two.json")
    target = tmp_path / "project"
    with pytest.raises(ValueError, match="数据类别"):
        org.plan_import(target, [spec(first, "imu")], "2026-08-03")
    for path, category in ((first, "healthy"), (second, "calving")):
        plan = org.plan_import(target, [spec(path, "imu")], "2026-08-03", category=category)
        org.execute(plan, tmp_path / ("job-" + category))
        relative = Path(plan["rows"][0]["target"]).relative_to(target).as_posix()
        assert read_context(target, relative)["dataset_category"] == category
        # Opening an ancestor project still resolves the same per-file context.
        assert read_context(tmp_path, "project/" + relative)["dataset_category"] == category
    assert read_context(target, "unknown.json") == {}


@pytest.mark.parametrize("category", ["healthy", "estrus", "pregnancy_early", "pregnancy_mid", "pregnancy_late", "calving", "disease"])
def test_collection_category_does_not_restrict_events_and_survives_exports(category):
    import csv
    import io

    from cowmata_tailring.annotation.core import (
        build_events_csv,
        build_meta,
        build_sample_multihot_csv,
    )
    from cowmata_tailring.workspace.work import SessionWork
    work = SessionWork("source")
    before = [label.to_dict() for label in work.project.labels]
    work.set_category(category)
    for index in range(len(work.project.labels)):
        event = work.project.add_event(index, index * 10, index * 10 + 5)
        assert event.li == index
    work.set_category(category)
    restored = SessionWork.from_dict(work.to_dict())
    assert [label.to_dict() for label in restored.project.labels] == before
    assert all(e.extras["dataset_category"] == category for e in restored.project.events)
    events = list(csv.DictReader(io.StringIO(build_events_csv(restored.project))))
    assert events and all(e["dataset_category"] == category for e in events)
    samples = list(csv.DictReader(io.StringIO(build_sample_multihot_csv(restored.project, [0, 10, 20]))))
    assert all(s["dataset_category"] == category for s in samples)
    assert build_meta(restored.project)["dataset_category"] == category
