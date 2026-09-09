"""End-to-end organization regressions use isolated synthetic media only."""
import base64
import csv
import hashlib
import json
import time
from pathlib import Path

import pytest

from cowmata_tailring.workspace import organization as org
from cowmata_tailring.workspace.catalog import Catalog, ScanResult
from cowmata_tailring.workspace.dataset_access import ensure_available
from cowmata_tailring.workspace.storage import ProjectLock


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "registry"))


def clip(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"isolated-test-video")
    return path


def import_plan(tmp_path):
    source = clip(tmp_path / "incoming/day/one.mp4")
    target = tmp_path / "project"
    plan = org.plan_import(target, [{"path": str(source.parent.parent), "kind": "video", "camera": "视角01"}],
                           "2026-09-01", category="healthy")
    return source, target, plan


def test_normalize_legacy_v31_index_without_hint_table(tmp_path):
    root = tmp_path / "project"
    source = clip(root / "视角01_海康/day/one.mp4")
    catalog = Catalog(root, stability_seconds=0)
    catalog.scan(fast=True)
    relative = source.relative_to(root).as_posix()
    asset = hashlib.sha256(source.read_bytes()).hexdigest()
    catalog.db.execute("INSERT INTO assets VALUES(?,?,?,?)",
                       (asset, "video", json.dumps({"camera": "视角01_海康"}), 0))
    catalog.db.execute("UPDATE locations SET asset_id=?,state='ready' WHERE path=?", (asset, relative))
    # This table was introduced after the published v3.1 schema.
    catalog.db.execute("DROP TABLE video_hints")
    catalog.db.commit()
    catalog.save_settings({"current_path": relative, "camera_maps": {"视角01_海康": {"anchors": [[0, 100], [1000, 1100]]}}})
    catalog.close()

    plan = org.plan_normalize(root)
    result = org.execute(plan, tmp_path / "job")
    assert result["completed"]
    destination = root / "视角01/day/one.mp4"
    assert destination.is_file() and not source.exists()
    reopened = Catalog(root)
    try:
        assert reopened.rows()[0]["asset_id"] == asset
        assert reopened.settings()["current_path"] == "视角01/day/one.mp4"
        assert reopened.settings()["camera_maps"]["视角01"]["anchors"] == [[0, 100], [1000, 1100]]
    finally:
        reopened.close()


def test_parent_input_honors_nested_legacy_project_writer_lock(tmp_path):
    source, target, plan = import_plan(tmp_path)
    nested_meta = source.parent / "标注工程"
    with_lock = ProjectLock(nested_meta / "writer.lock")
    assert with_lock.acquired
    try:
        with pytest.raises(OSError, match="相关工程仍在标注"):
            org.execute(plan, tmp_path / "job")
        assert source.is_file() and not target.exists()
    finally:
        with_lock.close()
    assert org.execute(plan, tmp_path / "job")["completed"]


def test_normalize_reconciles_missing_destination_cache_without_losing_history(tmp_path):
    root = tmp_path / "project"
    source = clip(root / "视角01_海康/day/one.mp4")
    destination = clip(root / "视角01/day/one.mp4")
    destination.write_bytes(b"previous missing asset")
    catalog = Catalog(root, stability_seconds=0)
    catalog.scan(fast=True)
    assets = []
    for path in (source, destination):
        asset = hashlib.sha256(path.read_bytes()).hexdigest()
        assets.append(asset)
        catalog.db.execute("INSERT INTO assets VALUES(?,?,?,?)", (asset, "video", "{}", 0))
        catalog.db.execute("UPDATE locations SET asset_id=?,state='ready' WHERE path=?",
                           (asset, path.relative_to(root).as_posix()))
    catalog.db.commit()
    destination.unlink()
    catalog.scan(fast=True)
    catalog.close()
    plan = org.plan_normalize(root)
    assert org.execute(plan, tmp_path / "job")["completed"]
    reopened = Catalog(root)
    try:
        rows = [row for row in reopened.rows() if row["path"] == "视角01/day/one.mp4"]
        assert len(rows) == 1 and rows[0]["asset_id"] == assets[0]
        assert reopened.db.execute("SELECT 1 FROM assets WHERE id=?", (assets[1],)).fetchone()
        assert reopened.db.execute("SELECT 1 FROM revisions WHERE path=? AND asset_id=?",
                                   ("视角01/day/one.mp4", assets[1])).fetchone()
    finally:
        reopened.close()


def test_incomplete_final_scan_remains_resumable_and_blocks_annotation(tmp_path, monkeypatch):
    source, target, plan = import_plan(tmp_path)
    actual_scan = Catalog.scan

    def incomplete_scan(*_args, **_kwargs):
        return ScanResult(complete=False, errors=["simulated directory access failure"])

    monkeypatch.setattr(Catalog, "scan", incomplete_scan)
    with pytest.raises(OSError, match="索引"):
        org.execute(plan, tmp_path / "job")
    assert not source.exists()
    with pytest.raises(OSError, match="未完成"):
        ensure_available([target])
    monkeypatch.setattr(Catalog, "scan", actual_scan)
    assert org.execute(plan, tmp_path / "job")["completed"]
    ensure_available([target])
    with (target / "整理清单.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 1


def test_interrupted_manifest_append_is_recovered_on_resume(tmp_path, monkeypatch):
    source, target, plan = import_plan(tmp_path)
    original = org.append_manifest

    def interrupted_append(root, row):
        with (root / "整理清单.csv").open("a", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="")
            writer.writerow([row["source"], Path(row["target"]).relative_to(root).as_posix(), row["kind"]])
        raise OSError("simulated interrupted manifest write")

    monkeypatch.setattr(org, "append_manifest", interrupted_append)
    with pytest.raises(OSError):
        org.execute(plan, tmp_path / "job")
    assert not source.exists()
    monkeypatch.setattr(org, "append_manifest", original)
    assert org.execute(plan, tmp_path / "job")["completed"]
    with (target / "整理清单.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["status"] == "moved" and rows[0]["at"]
    assert int(rows[0]["size"]) == Path(plan["rows"][0]["target"]).stat().st_size


@pytest.mark.parametrize("category", ["calving", "pregnancy_early", "pregnancy_mid", "pregnancy_late"])
def test_gui_organize_then_open_preserves_collection_category(tmp_path, monkeypatch, category):
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from cowmata_tailring.workspace import organization_ui
    from cowmata_tailring.workspace import window as workspace_window
    from cowmata_tailring.workspace.modern_window import MainWindow

    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(organization_ui, "QSettings", lambda: settings)
    monkeypatch.setattr(workspace_window, "QSettings", lambda: settings)
    monkeypatch.setattr(organization_ui, "task_root", lambda: tmp_path / "jobs")
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(MainWindow, "confirm_close", lambda _self: "save")
    source = tmp_path / "incoming/0C3D5EA22E36-21100-w1/nested/one.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"device": "0C3D5EA22E36", "version": 0, "create_time": 1788219681000,
                                  "imu": base64.b64encode(bytes(18 * 400)).decode()}), encoding="utf-8")
    target = tmp_path / "project"
    window = MainWindow()
    dialog = organization_ui.OrganizationWindow(window)
    window._organization_window = dialog
    dialog.target.setText(str(target))
    dialog.start_date.setText("2026-09-01")
    dialog.end_date.setText("2026-09-01")
    dialog.category.setCurrentIndex(dialog.category.findData(category))
    dialog.add_source("imu", source.parent.parent)
    beats = []
    timer = QTimer()
    timer.timeout.connect(lambda: beats.append(time.monotonic()))
    timer.start(10)

    def wait_for(condition, seconds=20):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.002)
        app.processEvents()
        assert condition(), dialog.status.text()

    try:
        dialog.preview_import()
        wait_for(lambda: not dialog.running)
        assert dialog.plan is not None and source.exists()
        dialog.execute_plan()
        wait_for(lambda: not dialog.running)
        assert dialog.open_button.isEnabled(), dialog.status.text()
        destination = target / "九轴/0C3D5EA22E36-21100-w1/2026-09-01/one.json"
        assert destination.is_file() and not source.exists()
        dialog.open_result()
        wait_for(lambda: window.work is not None)
        assert window.catalog.root == target
        assert window.work.project.extras["dataset_category"] == category
        assert window.data_category.currentData() == category
        assert len(beats) >= 2
        window.save_current()
        saved = json.loads(window.catalog.work_path(window.work.asset_id).read_text(encoding="utf-8"))
        assert saved["project"]["dataset_category"] == category
    finally:
        timer.stop()
        if dialog.running:
            dialog.cancel()
            wait_for(lambda: not dialog.running)
        window.close()
        wait_for(lambda: window._closed)
