"""Completion of the one-click downloader: outcomes, notes, problems, retries.

Covers the five remaining behaviours requested on 2026-09-22: outcome texts
never block a reliable device download, notes persist on disk, the problem
list is non-modal/copyable and excludes merely-empty rows, empty rows never
block normal records of the same device, and a corrected ledger is completed
on the next click without re-downloading existing files.
"""

import base64
import csv
import json
import threading
import time
import uuid
from datetime import datetime, timedelta

import pytest

from cowmata_tailring.edge_download.core import CHINA, DownloadError, Job
from cowmata_tailring.edge_download.csv_download import run_csv_job
from cowmata_tailring.edge_download.csv_targets import FILES, CsvPlan
from cowmata_tailring.edge_download.download_notes import NotesStore, notes_path
from cowmata_tailring.edge_download.site_records import SCHEMAS, refresh_records, settings_defaults

START = datetime(2026, 8, 18, tzinfo=CHINA)
END = START + timedelta(days=1)
DEVICE = "546C50CA07FA"
OTHER_DEVICE = "546C50CA07FB"


def sample_row(**changes):
    row = dict(
        设备号=DEVICE,
        牛号="23077-E",
        佩戴开始="2026-08-18 00:00:00",
        佩戴结束="2026-08-19 00:00:00",
        监测目的="产犊监测",
        产犊开始="2026-08-18 08:00:00",
        产犊结束="2026-08-18 09:00:00",
        九轴="有效",
        脉搏="有效",
        温度="有效",
        数据分类="calving",
    )
    row.update(changes)
    return row


def equipment_row(**changes):
    row = dict(设备编码=DEVICE, 新佩戴牛号="23077-E", 日期="2026-08-18", 记录类型="佩戴")
    row.update(changes)
    return row


def write_sheet(folder, sheet, rows):
    fields = SCHEMAS[sheet]["fields"]
    with (folder / SCHEMAS[sheet]["filename"]).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            full = dict.fromkeys(fields, "")
            full.update({"记录ID": str(uuid.uuid4()), "已删除": "0"})
            full.update(row)
            writer.writerow(full)


def ledgers(folder, samples=None, equipment=None, calving=None):
    folder.mkdir(parents=True, exist_ok=True)
    write_sheet(folder, "samples", [sample_row()] if samples is None else samples)
    write_sheet(folder, "equipment", [equipment_row()] if equipment is None else equipment)
    write_sheet(folder, "calving", [dict(牛号="23077", 生产日期="2026-08-18", 牛场登记生产时间="09:00")]
                if calving is None else calving)
    return folder


def sensor_record(kind, device=DEVICE, uid=33179, ms=633, cow="23077-E"):
    data = dict(uid=uid, device=device, create_time=int(START.timestamp() * 1000) + 2502000 + ms)
    if kind == "motion":
        data.update(version=2, imu=base64.b64encode(bytes(22)).decode())
    elif kind == "temp":
        data.update(data=36.5)
    else:
        data.update(data="AQACAA==", ir_data="AwAEAA==", imu_data=None)
    if cow:
        data["cow_id"] = cow
    return data


def client_for(documents, calls):
    class Fake:
        def __init__(self, *args):
            pass

        def check(self):
            pass

        def listing(self, target, lo, hi, kinds):
            for kind, data in documents:
                if kind in kinds:
                    yield kind, data["uid"], data["device"], ""

        def record(self, kind, uid, device, cow):
            calls.append((kind, uid))
            return next(dict(d) for k, d in documents if k == kind and d["uid"] == uid)

    return Fake


def job_for(tmp_path, folder):
    return Job("http://example.test", tmp_path / "farm", "未分类", (),
               ("motion", "pulse", "temp"), START, END, folder)


def downloaded_files(farm):
    return [p for p in farm.rglob("*.json")
            if ".edge-download" not in p.parts and not p.name.startswith(".")]


# ---------------------------------------------------------------- task 4 ----

def test_incomplete_same_identity_row_never_blocks_the_complete_row(tmp_path):
    folder = ledgers(
        tmp_path / "ledger",
        samples=[
            sample_row(记录ID=str(uuid.uuid4())),
            sample_row(温度="", 监测目的="产后监测", 产犊开始="/", 产犊结束="/"),
        ],
    )
    plan = CsvPlan(folder)
    assert list(plan.bounds(START, END)) == [(DEVICE, START, END)]
    wear, reason = plan.resolve_download(DEVICE, START + timedelta(hours=2), "23077-E")
    assert wear is not None and wear.category == "产犊"
    pending = [r for r in plan.preview() if r["eligibility"] == "pending"]
    assert pending and "温度" in pending[0]["reason"]
    assert not any("待补全" in x["message"] for x in plan.issues)


def test_overlapping_foreign_identity_still_blocks_until_ledger_is_fixed(tmp_path):
    folder = ledgers(
        tmp_path / "ledger",
        samples=[
            sample_row(记录ID=str(uuid.uuid4())),
            sample_row(牛号="23078-F", 设备号=DEVICE, 温度="",
                       监测目的="产后监测", 产犊开始="/", 产犊结束="/"),
        ],
    )
    plan = CsvPlan(folder)
    assert list(plan.bounds(START, END)) == []
    wear, reason = plan.resolve_download(DEVICE, START + timedelta(hours=2), "23077-E")
    assert wear is None and ("重叠" in reason or "没有通过" in reason)


# ---------------------------------------------------------------- task 3 ----

def bad_code_ledgers(tmp_path):
    truncated, thirteen, near = "564C5CA00BB", "0C3D5EA22DE1F", "0C3D5EA22E1F"
    folder = ledgers(
        tmp_path / "ledger",
        samples=[
            sample_row(设备号=OTHER_DEVICE, 记录ID=str(uuid.uuid4())),
            sample_row(设备号=truncated, 牛号="23244A14", 佩戴开始="2026-08-18 06:00:00"),
            sample_row(设备号=thirteen, 牛号="23256W3", 佩戴开始="2026-08-18 05:00:00"),
            sample_row(温度="", 监测目的="产后监测", 产犊开始="/", 产犊结束="/"),
        ],
        equipment=[
            equipment_row(设备编码=OTHER_DEVICE, 新佩戴牛号="23077-E"),
            equipment_row(设备编码=truncated, 新佩戴牛号="23244A14", 日期="2026-08-18"),
            equipment_row(设备编码=near, 新佩戴牛号="23256W3", 日期="2026-08-18"),
        ],
    )
    return folder, truncated, thirteen, near


def test_bad_device_rows_form_copyable_problem_list_without_empty_rows(tmp_path, qt_application):
    from cowmata_tailring.edge_download.pro_dialog import LedgerReportWindow

    folder, truncated, thirteen, near = bad_code_ledgers(tmp_path)
    plan = CsvPlan(folder)
    assert plan.ready
    device_issues = [x for x in plan.issues if x.get("field") in ("设备号", "设备编码")]
    assert {x["device"] for x in device_issues} == {truncated, thirteen}
    assert all(x["record_id"] and x["cow"] for x in device_issues)
    assert not any("待补全" in x["message"] for x in plan.issues)  # empty rows stay out

    by_code = {x["device"]: x for x in device_issues}
    assert near in by_code[thirteen]["suggestion"] and "待确认候选" in by_code[thirteen]["suggestion"]
    cross = [x for x in device_issues if x["device"] == truncated]
    assert len(cross) == 2  # one sample row + one equipment row for the same bad code
    assert any("同一编码还出现在" in x["suggestion"] for x in cross)

    window = LedgerReportWindow()
    window.set_issues(plan.issues)
    assert not window.isModal()
    assert window.issue_table.rowCount() == len(plan.issues)
    window.tabs.setCurrentWidget(window.issue_table)
    window.copy_page()
    text = qt_application.clipboard().text()
    assert "表名" in text and truncated in text and thirteen in text and "建议" in text
    window.deleteLater()
    qt_application.processEvents()


def test_normal_records_download_while_bad_rows_are_only_reported(tmp_path):
    folder, truncated, thirteen, near = bad_code_ledgers(tmp_path)
    job = job_for(tmp_path, folder)
    documents = [("motion", sensor_record("motion", OTHER_DEVICE))]
    calls = []
    result = run_csv_job(job, threading.Event(), client_factory=client_for(documents, calls))
    assert result.saved == 1 and result.failed == 0
    files = downloaded_files(tmp_path / "farm")
    assert files and not any(truncated in str(p) or thirteen in str(p) for p in files)


# ---------------------------------------------------------------- task 1 ----

def outcome_ledgers(tmp_path):
    return ledgers(
        tmp_path / "ledger",
        samples=[sample_row(监测目的="死胎", 产犊开始="", 产犊结束="", 数据分类="review")],
        calving=[dict(牛号="23077", 生产日期="2026-08-18", 牛场登记生产时间="死胎")],
    )


def test_outcome_text_is_a_note_and_downloads_by_wearing_range(tmp_path):
    folder = outcome_ledgers(tmp_path)
    plan = CsvPlan(folder)
    assert plan.ready
    assert not any("死胎" in x["message"] for x in plan.issues)
    sample = next(r for r in plan.preview() if r["source"] == FILES[0])
    assert sample["eligibility"] == "eligible"
    assert sample["category"] == "待核对"  # never rewritten into a confirmed class
    assert "23077" not in plan.births  # no fabricated calving time
    birth_notes = [n for n in plan.notes if n["source"] == FILES[2]]
    assert birth_notes and birth_notes[0]["text"] == "死胎"
    assert birth_notes[0]["cow"] == "23077" and birth_notes[0]["device"] == DEVICE
    assert birth_notes[0]["download_start"].startswith("2026-08-18")
    assert any(n["source"] == FILES[0] and n["device"] == DEVICE for n in plan.notes)
    assert list(plan.bounds(START, END)) == [(DEVICE, START, END)]
    wear, _ = plan.resolve_download(DEVICE, START + timedelta(hours=1), "23077-E")
    assert wear is not None and wear.category == "待核对"

    job = job_for(tmp_path, folder)
    documents = [("temp", sensor_record("temp", DEVICE, uid=77001))]
    result = run_csv_job(job, threading.Event(), client_factory=client_for(documents, []))
    assert result.saved == 1 and result.failed == 0
    files = downloaded_files(tmp_path / "farm")
    assert files and any("待核对" in str(p) for p in files)


@pytest.mark.parametrize("purpose", ["流产", "难产", "早产"])
def test_known_outcome_words_behave_like_stillbirth(tmp_path, purpose):
    folder = ledgers(tmp_path / "ledger",
                     samples=[sample_row(监测目的=purpose, 产犊开始="", 产犊结束="")])
    plan = CsvPlan(folder)
    assert plan.resolve_download(DEVICE, START + timedelta(hours=1), "23077-E")[0] is not None


def test_outcome_row_without_confirmed_sensors_stays_pending(tmp_path):
    folder = ledgers(tmp_path / "ledger",
                     samples=[sample_row(监测目的="死胎", 产犊开始="", 产犊结束="", 温度="")])
    plan = CsvPlan(folder)
    assert list(plan.bounds(START, END)) == []
    assert plan.resolve_download(DEVICE, START + timedelta(hours=1), "23077-E")[0] is None


def test_full_datetime_in_birth_registry_is_a_time_not_a_note(tmp_path):
    folder = ledgers(
        tmp_path / "ledger",
        calving=[dict(牛号="23077", 生产日期="2026-08-18", 牛场登记生产时间="2026-08-18 09:30"),
                 dict(牛号="23078", 生产日期="2026-08-18", 牛场登记生产时间="09:25"),
                 dict(牛号="23079", 生产日期="2026-08-18", 牛场登记生产时间="死胎")],
    )
    plan = CsvPlan(folder)
    assert plan.births["23077"][0]["time"] == "2026-08-18T09:30:00+08:00"
    assert plan.births["23078"][0]["time"] == "2026-08-18T09:25:00+08:00"
    texts = [n["text"] for n in plan.notes if n["source"] == FILES[2]]
    assert texts == ["死胎"]


# ---------------------------------------------------------------- task 2 ----

def test_notes_archive_survives_restart_and_refresh(tmp_path):
    folder = outcome_ledgers(tmp_path)
    plan = CsvPlan(folder)
    path = notes_path(tmp_path / "farm")
    NotesStore(path).merge(plan.notes, plan.fingerprint).save()

    reopened = NotesStore(path)  # a later program start
    assert any(n["text"] == "死胎" and n["device"] == DEVICE and n["cow"] == "23077"
               and n["source"] == FILES[2] and n["download_start"] for n in reopened.records)
    assert all(k in reopened.records[0] for k in
               ("source", "row", "record_id", "cow", "device", "text",
                "download_start", "download_end"))

    # The field staff later replaces the outcome with a real time; the old
    # note must remain queryable as history, not vanish with the refresh.
    ledgers(folder, calving=[dict(牛号="23077", 生产日期="2026-08-18", 牛场登记生产时间="09:40")])
    refreshed = CsvPlan(folder)
    NotesStore(path).merge(refreshed.notes, refreshed.fingerprint).save()
    after = NotesStore(path)
    death = [n for n in after.records if n["text"] == "死胎"]
    assert death and death[0]["current"] is False


def test_dialog_persists_notes_and_a_new_dialog_instance_reads_them(tmp_path, qt_application, monkeypatch):
    from cowmata_tailring.edge_download import pro_dialog
    from cowmata_tailring.edge_download.pro_settings import ProSettings

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    root = tmp_path / "installation" / "Pro"
    root.mkdir(parents=True)
    folder = outcome_ledgers(tmp_path / "ledger")
    store = ProSettings(tmp_path / "config", app_root=root)
    store.value["ledger_directory"] = str(folder)

    def open_dialog():
        dialog = pro_dialog.ProDownloadDialog(store=store)
        deadline = time.monotonic() + 5
        while getattr(dialog, "plan_worker", None) is not None and time.monotonic() < deadline:
            qt_application.processEvents()
            time.sleep(0.01)
        qt_application.processEvents()
        return dialog

    first = open_dialog()
    try:
        assert any(n["text"] == "死胎" for n in first.csv_notes)
        assert notes_path(store.value["data_root"]).is_file()
    finally:
        first.deleteLater()
        qt_application.processEvents()
    # A fresh window behaves like a program restart and still finds the notes.
    second = open_dialog()
    try:
        assert any(n["text"] == "死胎" and n.get("current") for n in second.csv_notes)
    finally:
        second.deleteLater()
        qt_application.processEvents()


# ---------------------------------------------------------------- task 5 ----

def test_corrected_ledger_downloads_previously_skipped_data_without_duplicates(tmp_path):
    folder = ledgers(tmp_path / "ledger", samples=[sample_row(温度="")])
    job = job_for(tmp_path, folder)
    documents = [("motion", sensor_record("motion"))]

    def no_network(*args):
        pytest.fail("pending rows must not open raw connections")

    first = run_csv_job(job, threading.Event(), client_factory=no_network)
    assert first.saved == 0 and first.pending >= 1
    stale = CsvPlan(folder).fingerprint

    ledgers(folder, samples=[sample_row()])  # field fix synced through the uploader
    assert CsvPlan(folder).fingerprint != stale
    second = run_csv_job(job, threading.Event(), client_factory=client_for(documents, []))
    assert second.saved == 1 and second.failed == 0

    calls = []
    third = run_csv_job(job, threading.Event(), client_factory=client_for(documents, calls))
    assert third.saved == 0 and third.skipped >= 1 and calls == []
    assert len(downloaded_files(tmp_path / "farm")) == 1


def test_failed_cycle_is_not_recorded_as_downloaded_and_later_completes(tmp_path):
    folder = ledgers(tmp_path / "ledger")

    class Exploding:
        def __init__(self, *args):
            pass

        def check(self):
            pass

        def listing(self, target, lo, hi, kinds):
            raise DownloadError("network down")

    job = job_for(tmp_path, folder)
    result = run_csv_job(job, threading.Event(), client_factory=Exploding)
    assert result.failed >= 1 and result.saved == 0
    cycle = json.loads(
        (tmp_path / "farm" / ".edge-download" / "csv-cycle.json").read_text(encoding="utf-8"))
    assert cycle["status"] == "failed" and cycle["verified_ranges"] == []
    # After the outage the same click completes the missing data.
    documents = [("motion", sensor_record("motion"))]
    fixed = run_csv_job(job, threading.Event(), client_factory=client_for(documents, []))
    assert fixed.saved == 1


def test_refresh_failure_keeps_cached_csv_and_skips_this_cycle(tmp_path):
    folder = ledgers(tmp_path / "ledger")

    class OnceWorking:
        def __init__(self, values, cancel, log=lambda message: None):
            pass

        def pull(self, sheet):
            if sheet == "calving":
                raise DownloadError("连接中断")
            return (folder / SCHEMAS[sheet]["filename"]).read_bytes()

    values = {**settings_defaults(), "ledger_directory": str(folder)}
    before = {name: (folder / name).read_bytes() for name in FILES}
    with pytest.raises(DownloadError):
        refresh_records(values, threading.Event(), client_factory=OnceWorking)
    assert {name: (folder / name).read_bytes() for name in FILES} == before
    assert CsvPlan(folder).ready  # the previous valid ledger still authorizes plans

    from cowmata_tailring.edge_download.pro_dialog import SyncWorker

    calls = []

    def offline(*args):
        raise OSError("offline")

    worker = SyncWorker({**values, "sync_ledger": True},
                        runner=lambda *args: calls.append(args), refresher=offline)
    reports = []
    worker.completed.connect(reports.append)
    worker.run()
    assert not calls and reports[0]["errors"]
