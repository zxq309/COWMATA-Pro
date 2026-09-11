"""Device-folder identity is per record; reuse never deduplicates recordings."""
import base64
import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cowmata_tailring.workspace import organization as org


@pytest.fixture(autouse=True)
def isolated_access(monkeypatch, tmp_path):
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))


@pytest.mark.parametrize("mark", ["0", "9", "10", "11", "A", "z", "A1", "Z9", "w1", "w2"])
def test_three_parts_keep_ear_tag_zeros_and_field_mark_case(mark):
    from cowmata_tailring.workspace.device_identity import parse_device_folder
    identity = parse_device_folder("546c50ca07d5-00123-" + mark)
    assert identity.device_id == "546C50CA07D5"
    assert identity.cow_id == "00123"
    assert identity.field_mark == mark
    assert identity.folder_name == "546C50CA07D5-00123-" + mark


@pytest.mark.parametrize("name", [
                                  "07D5-21014-R", "546C50CA07D5-21100-",
                                  "546C50CA07D5-牛01-A", "546C50CA07D5-21100-Ａ1",
                                  "546C50CA07D5-21100-w1 "])
def test_nonstandard_names_are_not_accepted_as_identity(name):
    from cowmata_tailring.workspace.device_identity import parse_device_folder
    with pytest.raises(ValueError):
        parse_device_folder(name)


@pytest.mark.parametrize(("name", "device", "suggestion"), [
    ("546C50CA07E8-y1-22207", "546C50CA07E8", "546C50CA07E8-22207-y1"),
    ("546C50CA07F8-23077E", "546C50CA07F8", "546C50CA07F8-23077-E"),
    ("07D5-21014-R", "546C50CA07D5", "546C50CA07D5-21014-R"),
    ("07D5-21014-R", "546C50CA07F8", ""),
])
def test_advice_is_explained_without_accepting_or_renaming(tmp_path, name, device, suggestion):
    from cowmata_tailring.workspace.device_identity import resolve_device_identity
    path = tmp_path / name / "2026-09-01/motion/record.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    result = resolve_device_identity(path, device)
    automatic = name in {"546C50CA07E8-y1-22207", "546C50CA07F8-23077E"}
    assert result["status"] == ("ready" if automatic else "blocked")
    if automatic:
        assert result["folder_name"] == suggestion and path.exists()
        return
    assert result["source_folder"] == name
    assert result["suggested_folder"] == suggestion
    assert result["message"] and path.exists()


def test_directory_device_must_agree_with_json(tmp_path):
    from cowmata_tailring.workspace.device_identity import resolve_device_identity
    path = tmp_path / "546C50CA07D5-21100-10/day/one.json"
    result = resolve_device_identity(path, "546C50CA07F8")
    assert result["status"] == "blocked"
    assert not result["suggested_folder"]


def make_record(path, device="546C50CA07D5", day="2026-09-01"):
    path.parent.mkdir(parents=True, exist_ok=True)
    start = datetime.fromisoformat(day).replace(tzinfo=timezone(timedelta(hours=8)))
    path.write_text(json.dumps({"device": device, "version": 0, "create_time": int(start.timestamp() * 1000),
                               "imu": base64.b64encode(bytes(18 * 4)).decode()}), encoding="utf-8")
    return path


def test_device_reused_for_another_cow_on_another_day_keeps_both_records(tmp_path):
    source = tmp_path / "incoming"
    first = make_record(source / "546C50CA07D5-00123-w1/deep/one.json", day="2026-09-01")
    second = make_record(source / "546C50CA07D5-21100-10/nested/one.json", day="2026-09-02")
    target = tmp_path / "project"
    plan = org.plan_import(target, [{"path": str(source), "kind": "imu"}], "2026-09-01", "2026-09-02", category="healthy")
    assert len(plan["rows"]) == 2 and all(row["status"] == "ready" for row in plan["rows"])
    assert org.execute(plan, tmp_path / "job")["moved"] == 2
    assert first.exists() and second.exists()
    assert all(Path(r["target"]).is_file() for r in plan["rows"])
    target = Path(plan["target"])
    with (target / "数据分类.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert {(r["device_id"], r["cow_id"], r["field_mark"], r["record_date"]) for r in rows} == {
        ("546C50CA07D5", "00123", "w1", "2026-09-01"), ("546C50CA07D5", "21100", "10", "2026-09-02")}
    assert all(r["source_folder"] in {"546C50CA07D5-00123-w1", "546C50CA07D5-21100-10"} for r in rows)


def test_nonstandard_valid_imu_blocks_whole_batch_without_quarantine(tmp_path):
    source = make_record(tmp_path / "incoming/07D5-21014-R/day/one.json")
    target = tmp_path / "project"
    plan = org.plan_import(target, [{"path": str(source.parent.parent), "kind": "imu"}], "2026-09-01", category="healthy")
    row = plan["rows"][0]
    assert row["status"] == "blocked" and not row["quarantine"]
    assert row["suggested_folder"] == "546C50CA07D5-21014-R"
    with pytest.raises(ValueError):
        org.execute(plan, tmp_path / "job")
    assert source.exists() and not target.exists()


def test_short_folder_with_multiple_matching_devices_is_ambiguous(tmp_path):
    source = tmp_path / "incoming/07D5-21014-R"
    make_record(source / "one.json", device="546C50CA07D5")
    make_record(source / "two.json", device="1234567807D5")
    plan = org.plan_import(tmp_path / "target", [{"path": str(source), "kind": "imu"}], "2026-09-01", category="healthy")
    assert all(row["status"] == "blocked" and not row["suggested_folder"] for row in plan["rows"])
    assert all("多个" in row["message"] for row in plan["rows"])
    report = org.audit([source])
    assert all(row["status"] == "blocked" and not row["suggested_folder"] for row in report["rows"])


def test_unrepresentable_record_timestamp_is_reported_without_removing_imu(tmp_path):
    source = make_record(tmp_path / "546C50CA07D5-00123-w1/one.json")
    value = json.loads(source.read_text(encoding="utf-8"))
    value["create_time"] = 10**30
    source.write_text(json.dumps(value), encoding="utf-8")
    result = org.audit([source])
    assert result["rows"][0]["status"] == "invalid"
    assert not result["rows"][0]["quarantine"] and source.exists()


def test_new_fields_upgrade_legacy_category_table_without_losing_old_assignment(tmp_path):
    from cowmata_tailring.workspace.data_category import CONTEXT_FILE
    source = make_record(tmp_path / "incoming/546C50CA07D5-00123-w1/one.json")
    target = tmp_path / "target/扬大_高邮牧场/正常"
    target.mkdir(parents=True)
    with (target / CONTEXT_FILE).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["target_relative_path", "dataset_category", "dataset_category_label", "collection_start",
                         "collection_end", "task_id", "confirmed_at", "note"])
        writer.writerow(["legacy/old.json", "calving", "产犊", "", "", "prior", "", "保留原标注"])
    plan = org.plan_import(target, [{"path": str(source), "kind": "imu"}], "2026-09-01", category="healthy")
    assert org.execute(plan, tmp_path / "job")["completed"]
    with (target / CONTEXT_FILE).open(encoding="utf-8-sig", newline="") as stream:
        rows = {row["target_relative_path"]: row for row in csv.DictReader(stream)}
    assert rows["legacy/old.json"]["dataset_category"] == "calving"
    assert rows["legacy/old.json"]["note"] == "保留原标注"
    assert rows[Path(plan["rows"][0]["target"]).relative_to(target).as_posix()]["cow_id"] == "00123"


def test_gui_lists_naming_advice_and_blocks_move_before_annotation(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMainWindow

    from cowmata_tailring.workspace import organization_ui

    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(organization_ui, "QSettings", lambda: settings)
    monkeypatch.setattr(organization_ui, "task_root", lambda: tmp_path / "jobs")
    source = make_record(tmp_path / "incoming/07D5-21014-R/day/one.json")
    owner = QMainWindow()
    owner.tell = lambda *_: None
    dialog = organization_ui.OrganizationWindow(owner)
    dialog.target.setText(str(tmp_path / "target"))
    dialog.start_date.setText("2026-09-01")
    dialog.end_date.setText("2026-09-01")
    dialog.category.setCurrentIndex(dialog.category.findData("healthy"))
    dialog.add_source("imu", source.parent.parent)
    dialog.preview_import()
    deadline = time.monotonic() + 20
    while dialog.running and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.002)
    app.processEvents()
    try:
        assert not dialog.running and dialog.plan, dialog.status.text()
        assert not dialog.execute_button.isEnabled() and not dialog.open_button.isEnabled()
        assert dialog.model.data(dialog.model.index(0, 0)) == "07D5-21014-R"
        assert dialog.model.data(dialog.model.index(0, 2)) == "546C50CA07D5-21014-R"
        with (dialog.job / "report.csv").open(encoding="utf-8-sig", newline="") as stream:
            report = list(csv.DictReader(stream))
        assert report[0]["source_folder"] == "07D5-21014-R"
        assert report[0]["suggested_folder"] == "546C50CA07D5-21014-R"
        assert source.exists() and not (tmp_path / "target").exists()
    finally:
        dialog.close()
        owner.close()
        app.processEvents()


def test_three_pregnancy_stages_keep_explicit_batch_categories_without_guessing(tmp_path):
    from cowmata_tailring.workspace.data_category import read_context
    target = tmp_path / "project"
    saved = {}
    # Identical collection times intentionally exercise explicit user choice;
    # neither dates nor observed behaviors may infer a pregnancy stage.
    for cow, category in (("00123", "pregnancy_early"), ("00124", "pregnancy_mid"), ("00125", "pregnancy_late")):
        source = make_record(tmp_path / "incoming" / f"546C50CA07D5-{cow}-w1" / "one.json")
        plan = org.plan_import(target, [{"path": str(source), "kind": "imu"}], "2026-09-01", category=category)
        assert plan["rows"][0]["status"] == "ready" and source.exists()
        assert org.execute(plan, tmp_path / ("job-" + category))["completed"]
        relative = Path(plan["rows"][0]["target"]).relative_to(target).as_posix()
        saved[relative] = category
    for relative, category in saved.items():
        context = read_context(target, relative)
        assert context["dataset_category"] == category
        assert context["record_date"] == "2026-09-01"
    with pytest.raises(ValueError):
        org.plan_import(target, [{"path": str(target), "kind": "imu"}], "2026-09-01", category="pregnancy")
