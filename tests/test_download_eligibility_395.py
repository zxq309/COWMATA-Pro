import csv
import threading
from datetime import datetime, timedelta

import pytest

from cowmata_tailring.edge_download.core import CHINA, Job
from cowmata_tailring.edge_download.csv_download import run_csv_job
from cowmata_tailring.edge_download.csv_targets import FILES, CsvPlan

START = datetime(2026, 8, 18, tzinfo=CHINA)
DEVICE = "546C50CA07FA"


def ledgers(folder, **changes):
    folder.mkdir(exist_ok=True)
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
        已删除="0",
    )
    row.update(changes)
    device = dict(
        设备编码=DEVICE,
        新佩戴牛号="23077-E",
        日期="2026-08-18",
        **{"拆除时间(掉落）": "2026-08-18"},
        记录类型="佩戴",
    )
    for name, data in zip(
        FILES, [row, device, dict(牛号="23077", 生产日期="2026-08-18", 牛场登记生产时间="09:00")]
    ):
        with (folder / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=data)
            writer.writeheader()
            writer.writerow(data)
    return folder


def ranges(folder):
    return list(CsvPlan(folder).bounds(START, START + timedelta(days=1)))


@pytest.mark.parametrize("field", ["产犊开始", "产犊结束", "九轴", "脉搏", "温度"])
def test_missing_field_cannot_be_authorized_by_equipment_table(tmp_path, field):
    folder = ledgers(tmp_path / "ledger", **{field: ""})
    assert ranges(folder) == []


@pytest.mark.parametrize("field", ["九轴", "温度"])
def test_invalid_motion_or_temp_never_queried(tmp_path, field):
    folder = ledgers(tmp_path / "ledger", **{field: "无效"})
    assert ranges(folder) == []
    job = Job(
        "http://example.test",
        tmp_path / "farm",
        "未分类",
        (),
        ("motion",),
        START,
        START + timedelta(days=1),
        folder,
    )

    def no_network(*args):
        pytest.fail("Excluded rows must not instantiate network clients")

    result = run_csv_job(job, threading.Event(), client_factory=no_network)
    assert result.saved == 0


def test_invalid_ppg_is_still_downloadable_if_filled(tmp_path):
    assert ranges(ledgers(tmp_path / "ledger", 脉搏="无效"))


@pytest.mark.parametrize("purpose", ["孕后期监测", "孕晚期监测", "发情监测", "产后监测"])
def test_noncalving_slash_is_filled(tmp_path, purpose):
    assert ranges(ledgers(tmp_path / "ledger", 监测目的=purpose, 产犊开始="/", 产犊结束="/"))


@pytest.mark.parametrize(
    "start,end",
    [
        ("/", "/"),
        ("2026-08-18", "2026-08-19"),
        ("2026-08-18 09:00", "2026-08-18 08:00"),
        ("########", "2026-08-18 09:00"),
    ],
)
def test_calving_requires_valid_ordered_times(tmp_path, start, end):
    assert not ranges(ledgers(tmp_path / "ledger", 产犊开始=start, 产犊结束=end))


def test_rechecks_filled_fields_next_cycle(tmp_path):
    folder = ledgers(tmp_path / "ledger", 温度="")
    first = CsvPlan(folder)
    assert not list(first.bounds(START, START + timedelta(days=1)))
    ledgers(folder)
    assert ranges(folder)
    assert first.fingerprint != CsvPlan(folder).fingerprint


@pytest.mark.parametrize("missing", FILES)
def test_all_three_csvs_required(tmp_path, missing):
    folder = ledgers(tmp_path / "ledger")
    (folder / missing).unlink()
    assert ranges(folder) == []


def test_report_explains_why_each_sample_is_pending(tmp_path):
    plan = CsvPlan(ledgers(tmp_path / "ledger", 温度=""))
    sample = next(r for r in plan.preview() if r["source"] == FILES[0])
    assert sample["eligibility"] == "pending"
    assert "温度" in sample["reason"]


def test_refresh_failure_never_runs_downloader(tmp_path):
    from cowmata_tailring.edge_download.pro_dialog import SyncWorker

    calls, reports = [], []

    def failed_refresh(*args):
        raise OSError("CSV server unavailable")

    worker = SyncWorker(
        dict(sync_ledger=True), runner=lambda *args: calls.append(args), refresher=failed_refresh
    )
    worker.completed.connect(reports.append)
    worker.run()
    assert not calls
    assert reports[0]["errors"]


def test_plan_read_does_not_block_window_or_timer(tmp_path, qt_application, monkeypatch):
    import time

    from cowmata_tailring.edge_download import pro_dialog
    from cowmata_tailring.edge_download.pro_settings import ProSettings

    original = pro_dialog.CsvPlan

    def slow_plan(folder):
        time.sleep(0.3)
        return original(folder)

    monkeypatch.setattr(pro_dialog, "CsvPlan", slow_plan)
    store = ProSettings(tmp_path / "settings")
    store.value["ledger_directory"] = str(ledgers(tmp_path / "ledger"))
    start = time.monotonic()
    dialog = pro_dialog.ProDownloadDialog(store=store)
    elapsed = time.monotonic() - start
    try:
        assert elapsed < 0.25
        deadline = time.monotonic() + 3
        while getattr(dialog, "plan_worker", None) and time.monotonic() < deadline:
            qt_application.processEvents()
            time.sleep(0.01)
        qt_application.processEvents()
        assert "可下载" in dialog.plan_label.text()
        assert all(c.isChecked() and not c.isEnabled() for c in dialog.kind_checks.values())
    finally:
        for worker in dialog.workers():
            worker.cancel.set()
            worker.wait(3000)
        qt_application.processEvents()
        dialog.deleteLater()


@pytest.mark.parametrize("name", FILES)
def test_malformed_reference_or_sample_file_never_authorizes_download(tmp_path, name):
    folder = ledgers(tmp_path / "ledger")
    (folder / name).write_text("wrong,headers\n1,2\n", encoding="utf-8-sig")
    plan = CsvPlan(folder)
    assert not plan.ready
    assert not list(plan.bounds(START, START + timedelta(days=1)))
