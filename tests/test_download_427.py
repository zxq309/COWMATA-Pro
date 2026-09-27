"""4.2.7 downloader: today's data waits for tomorrow, finished days are skipped,
dates and local state are visible, and single records can be re-downloaded."""

import json
import threading
from datetime import datetime, timedelta

from test_downloader_completion_422 import (
    DEVICE,
    START,
    client_for,
    downloaded_files,
    job_for,
    ledgers,
    sample_row,
    sensor_record,
)
from test_relative_download_412 import isolated_store  # noqa: F401

from cowmata_tailring.edge_download import download_status as status
from cowmata_tailring.edge_download.core import CHINA, Job
from cowmata_tailring.edge_download.csv_download import run_csv_job
from cowmata_tailring.edge_download.csv_targets import FILES, CsvPlan


def test_round_ends_at_midnight_and_today_is_left_for_tomorrow(tmp_path):
    folder = ledgers(tmp_path / "ledger")
    now = START + timedelta(hours=15)  # the wear day itself is still "today"
    job = Job("http://example.test", tmp_path / "farm", "未分类", (), ("motion", "pulse", "temp"),
              START, START + timedelta(days=1), folder)
    logs = []

    def no_network(*_):
        raise AssertionError("today's data must not be requested")

    result = run_csv_job(job, threading.Event(), logs.append, client_factory=no_network, now=now)
    assert result.saved == 0
    assert any("留到明天下载" in line for line in logs)


def test_finished_day_is_skipped_without_listing_and_progress_reaches_the_end(tmp_path):
    folder = ledgers(tmp_path / "ledger")
    job = job_for(tmp_path, folder)
    documents = [("motion", sensor_record("motion"))]
    first = run_csv_job(job, threading.Event(), client_factory=client_for(documents, []))
    assert first.saved == 1

    class NoListing:
        def __init__(self, *_):
            pass

        def check(self):
            pass

        def listing(self, *_):
            raise AssertionError("a finished, unchanged day must not be listed again")

    steps = []
    second = run_csv_job(job, threading.Event(), progress=lambda *a: steps.append(a), client_factory=NoListing)
    assert second.settled == 1 and second.failed == 0
    assert steps and steps[-1][0] == steps[-1][1], "progress must reach its end"


def test_forced_redownload_replaces_changed_local_file_and_keeps_backup(tmp_path):
    folder = ledgers(tmp_path / "ledger")
    job = job_for(tmp_path, folder)
    original = sensor_record("motion")
    run_csv_job(job, threading.Event(), client_factory=client_for([("motion", original)], []))
    file = downloaded_files(tmp_path / "farm")[0]
    changed = dict(original, imu=original["imu"][:-4] + "AQI=")
    file.write_text(json.dumps(changed), encoding="utf-8")
    record = next(r for r in CsvPlan(folder).preview() if r["source"] == FILES[0])
    calls = []
    result = run_csv_job(job, threading.Event(), client_factory=client_for([("motion", original)], calls),
                         only=[record], force=True)
    assert calls, "a forced download asks the server again"
    assert result.failed == 0 and result.saved == 1
    assert json.loads(file.read_text(encoding="utf-8")) == original
    backups = list((tmp_path / "farm" / ".edge-download" / "recovery").iterdir())
    assert any(json.loads(p.read_text(encoding="utf-8")) == changed for p in backups)


def test_only_selected_records_are_requested(tmp_path):
    other = "546C50CA07FB"
    folder = ledgers(tmp_path / "ledger", samples=[sample_row(), sample_row(设备号=other, 牛号="24001-A")],
                     equipment=[dict(设备编码=DEVICE, 新佩戴牛号="23077-E", 日期="2026-08-18", 记录类型="佩戴"),
                                dict(设备编码=other, 新佩戴牛号="24001-A", 日期="2026-08-18", 记录类型="佩戴")])
    job = job_for(tmp_path, folder)
    asked = []

    class Recorder:
        def __init__(self, *_):
            pass

        def check(self):
            pass

        def listing(self, target, lo, hi, kinds):
            asked.append(target.value if hasattr(target, "value") else str(target))
            return []

    record = next(r for r in CsvPlan(folder).preview() if r["source"] == FILES[0] and r["device"] == other)
    run_csv_job(job, threading.Event(), client_factory=Recorder, only=[record])
    assert asked and all(other in value for value in asked)
    assert not any(DEVICE in value for value in asked)


def test_local_status_classifies_records(tmp_path):
    now = datetime(2026, 9, 25, 13, tzinfo=CHINA)
    base = dict(cow="1", mark="", category="产犊", folder="DEV-1", source=FILES[0], reason="ok", warnings="")
    records = [
        dict(base, row=2, device="AA", eligibility="eligible", start="2026-09-20T09:00:00+08:00",
             end="2026-09-21T09:00:00+08:00"),
        dict(base, row=3, device="BB", eligibility="eligible", start="2026-09-19T09:00:00+08:00",
             end="2026-09-20T09:00:00+08:00"),
        dict(base, row=4, device="CC", eligibility="eligible", start="2026-09-25T08:00:00+08:00", end=""),
        dict(base, row=5, device="DD", eligibility="excluded", reason="九轴、温度无效，不下载此条记录的三类数据",
             start="2026-09-18T09:00:00+08:00", end="2026-09-19T09:00:00+08:00"),
        dict(base, row=6, device="EE", eligibility="eligible", start="2026-09-22T09:00:00+08:00",
             end="2026-09-23T09:00:00+08:00", folder="DEV-6"),
    ]
    motion = tmp_path / "产犊" / "Motion" / "2026-09-22" / "DEV-6"
    motion.mkdir(parents=True)
    (motion / "2026-09-22_10-00-00.json").write_text("{}", encoding="utf-8")
    done = {"AA": [(datetime(2026, 9, 20, 9, tzinfo=CHINA), datetime(2026, 9, 21, 9, tzinfo=CHINA))]}
    result = status.local_status(records, tmp_path, done=done, now=now)
    assert result[2]["state"] == "downloaded"
    assert result[3]["state"] == "missing"
    assert result[4]["state"] == "today"
    assert result[5]["state"] == "invalid"
    assert result[6] == dict(state="partial", files=1)


def test_dialog_lists_dates_newest_unfinished_first_and_colours_states(isolated_store, qt_application):  # noqa: F811
    from cowmata_tailring.edge_download.pro_dialog import ProDownloadDialog

    def row(number, day, device, eligibility="eligible", reason="ok"):
        return {"source": FILES[0], "eligibility": eligibility, "row": number, "reason": reason,
                "device": device, "cow": "C" + str(number), "mark": "", "category": "产犊", "warnings": "",
                "start": f"2026-09-{day:02d}T09:00:00+08:00", "end": f"2026-09-{day + 1:02d}T09:00:00+08:00"}

    class Plan:
        issues = []
        local_status = {2: dict(state="downloaded", files=5), 3: dict(state="missing", files=0),
                        4: dict(state="missing", files=0), 5: dict(state="invalid", files=0)}

        def preview(self):
            return [row(2, 20, "AAAA"), row(3, 18, "BBBB"), row(4, 21, "CCCC"),
                    row(5, 22, "DDDD", "excluded", "九轴无效，不下载此条记录的三类数据")]

    dialog = ProDownloadDialog(store=isolated_store)
    try:
        dialog.receive_plan(Plan(), "")
        days = [dialog.day_table.item(i, 0).text() for i in range(dialog.day_table.rowCount())]
        assert days == ["全部日期", "2026-09-21", "2026-09-18", "2026-09-20"]
        order = [dialog.plan_table.item(i, 2).text() for i in range(dialog.plan_table.rowCount())]
        assert order == ["CCCC", "BBBB", "AAAA", "DDDD"]
        labels = {dialog.plan_table.item(i, 2).text(): dialog.plan_table.item(i, 0) for i in range(4)}
        assert labels["AAAA"].text() == "已下载"
        assert labels["AAAA"].background().color().name() == status.STATES["downloaded"][1]
        assert labels["BBBB"].background().color().name() == status.STATES["missing"][1]
        assert labels["DDDD"].text() == "不下载 · 传感器无效"
        assert "已下载 1" in dialog.plan_label.text() and "传感器无效 1" in dialog.plan_label.text()
        dialog.day_table.selectRow(2)
        assert [dialog.plan_table.item(i, 2).text() for i in range(dialog.plan_table.rowCount())] == ["BBBB"]
        started = []
        dialog.start_task = lambda operation, **kwargs: started.append(kwargs)
        dialog.download_selected()
        assert [r["device"] for r in started[0]["only"]] == ["BBBB"] and not started[0].get("force")
        dialog.day_table.selectRow(0)
        dialog.search.setText("aaaa")
        assert [dialog.plan_table.item(i, 2).text() for i in range(dialog.plan_table.rowCount())] == ["AAAA"]
    finally:
        dialog.deleteLater()


def test_redownload_requires_selection_and_confirmation(isolated_store, qt_application, monkeypatch):  # noqa: F811
    from PySide6.QtWidgets import QMessageBox

    from cowmata_tailring.edge_download import pro_dialog

    class Plan:
        issues = []
        local_status = {2: dict(state="downloaded", files=5)}

        def preview(self):
            return [{"source": FILES[0], "eligibility": "eligible", "row": 2, "reason": "ok", "device": "AAAA",
                     "cow": "C", "mark": "", "category": "产犊", "warnings": "",
                     "start": "2026-09-20T09:00:00+08:00", "end": "2026-09-21T09:00:00+08:00"}]

    dialog = pro_dialog.ProDownloadDialog(store=isolated_store)
    try:
        dialog.receive_plan(Plan(), "")
        started = []
        dialog.start_task = lambda operation, **kwargs: started.append(kwargs)
        dialog.redownload_selected()
        assert not started and "请先在表格中选择" in dialog.status.text()
        dialog.plan_table.selectRow(0)
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
        dialog.redownload_selected()
        assert not started
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        dialog.redownload_selected()
        assert started[0]["force"] is True and started[0]["only"][0]["device"] == "AAAA"
    finally:
        dialog.deleteLater()


def test_sync_worker_passes_selection_to_the_runner(tmp_path):
    from cowmata_tailring.edge_download import pro_dialog

    seen = {}

    def runner(job, cancel, log, progress, **kwargs):
        seen.update(kwargs)
        from cowmata_tailring.edge_download.core import Result
        return Result()

    values = dict(sync_ledger=False, data_root=str(tmp_path / "farm"), server="http://example.test",
                  start_time="2026-08-01T00:00:00+08:00", end_time="", ledger_directory=str(tmp_path),
                  only_records=[dict(device="AAAA", start="2026-09-20T09:00:00+08:00", end="")],
                  force_download=True)
    worker = pro_dialog.SyncWorker(values, "all", runner=runner, connector=None)
    pro_dialog.raw_connection, original = (lambda *a: __import__("contextlib").nullcontext()), pro_dialog.raw_connection
    try:
        worker.run()
    finally:
        pro_dialog.raw_connection = original
    assert seen == dict(only=values["only_records"], force=True)