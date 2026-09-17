import base64
import csv
import json
import threading
from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import QDateTime

from cowmata_tailring.edge_download.core import CHINA, Client, DownloadError, Job
from cowmata_tailring.edge_download.csv_download import run_csv_job, save_record
from cowmata_tailring.edge_download.csv_targets import CsvPlan

START = datetime(2026, 8, 18, tzinfo=CHINA)
DEVICE = "546C50CA07FA"


def fixture_job(tmp_path, category="calving", purpose="产犊监测"):
    ledger = tmp_path / "ledger"
    ledger.mkdir(exist_ok=True)
    row = dict(
        设备号=DEVICE,
        牛号="23077-E",
        佩戴开始="2026-08-18 00:00:00",
        佩戴结束="2026-08-19 00:00:00",
        数据分类=category,
        监测目的=purpose,
        已删除="0",
    )
    with (ledger / "样本试验台账.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=row)
        w.writeheader()
        w.writerow(row)
    farm = tmp_path / "farm"
    farm.mkdir(exist_ok=True)
    return Job(
        "http://example.test",
        farm,
        "未分类",
        (),
        ("motion", "pulse"),
        START,
        START + timedelta(days=1),
        ledger,
    )


def record(kind, uid=33179, ms=633):
    data = dict(uid=uid, device=DEVICE, create_time=int(START.timestamp() * 1000) + 2502000 + ms)
    if kind == "motion":
        data.update(version=2, imu=base64.b64encode(bytes(22)).decode())
    else:
        data.update(data="AQACAA==", ir_data="AwAEAA==", imu_data=None)
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


@pytest.mark.parametrize("kind,modality", [("motion", "Motion"), ("pulse", "PPG")])
def test_existing_organized_file_skips_detail_before_download(tmp_path, kind, modality):
    job = fixture_job(tmp_path)
    doc = record(kind)
    old = (
        job.farm
        / "正常"
        / modality
        / "2026-08-18"
        / (DEVICE + "-23077-E")
        / "2026-08-18_00-41-42.json"
    )
    old.parent.mkdir(parents=True)
    original = json.dumps(doc, indent=2).encode()
    old.write_bytes(original)
    calls = []
    result = run_csv_job(job, threading.Event(), client_factory=client_for([(kind, doc)], calls))
    assert (result.saved, result.skipped, result.failed, calls) == (0, 1, 0, [])
    assert old.read_bytes() == original
    assert list(job.farm.glob("*/*/*/*/*.json")) == [old]


def test_motion_and_ppg_uids_are_separate_and_missing_only_is_fetched(tmp_path):
    job = fixture_job(tmp_path)
    docs = [("motion", record("motion")), ("pulse", record("pulse"))]
    old = job.farm / "产犊/Motion/2026-08-18" / (DEVICE + "-23077-E") / "2026-08-18_00-41-42.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps(docs[0][1]))
    calls = []
    result = run_csv_job(job, threading.Event(), client_factory=client_for(docs, calls))
    assert (result.saved, result.skipped, result.failed) == (1, 1, 0)
    assert calls == [("pulse", 33179)]
    new = job.farm / "产犊/PPG/2026-08-18" / (DEVICE + "-23077-E") / "2026-08-18_00-41-42.json"
    assert json.loads(new.read_bytes()) == docs[1][1]


def test_csv_revision_never_copies_existing_raw_file(tmp_path):
    job = fixture_job(tmp_path)
    doc = record("motion")
    calls = []
    fake = client_for([("motion", doc)], calls)
    assert run_csv_job(job, threading.Event(), client_factory=fake).saved == 1
    old = next(job.farm.glob("产犊/Motion/*/*/*.json"))
    original = old.read_bytes()
    fixture_job(tmp_path, "healthy", "正常监测")
    result = run_csv_job(job, threading.Event(), client_factory=fake)
    assert (result.saved, result.skipped, len(calls)) == (0, 1, 1)
    assert old.read_bytes() == original
    assert not list(job.farm.glob("正常/Motion/*/*/*.json"))


def test_new_name_is_seconds_only_and_same_second_collision_is_reported(tmp_path):
    job = fixture_job(tmp_path)
    plan = CsvPlan(job.ledger_directory)
    old, saved, _, _ = save_record(job, plan, "motion", record("motion"))
    assert saved and old.name == "2026-08-18_00-41-42.json"
    original = old.read_bytes()
    with pytest.raises(DownloadError, match="冲突"):
        save_record(job, plan, "motion", record("motion", uid=99, ms=900))
    assert old.read_bytes() == original
    assert len(list(old.parent.glob("*.json"))) == 1


@pytest.mark.parametrize(
    "purpose,want",
    [
        ("正常监测", "正常"),
        ("发情监测", "发情"),
        ("孕后期监测", "怀孕/孕晚期"),
        ("孕早期监测", "怀孕/孕早期"),
        ("孕中期监测", "怀孕/孕中期"),
        ("产犊监测", "产犊"),
        ("产后监测", "产犊"),
        ("难产", "产犊"),
        ("死胎", "产犊"),
        ("疫病监测", "疫病"),
    ],
)
@pytest.mark.parametrize("category", ["", "unclassified"])
def test_purpose_fallback_matches_uploader_131(tmp_path, purpose, want, category):
    job = fixture_job(tmp_path, category, purpose)
    assert CsvPlan(job.ledger_directory).wears[0].category == want


def test_review_category_is_not_lost(tmp_path):
    job = fixture_job(tmp_path, "review", "正常监测")
    assert CsvPlan(job.ledger_directory).wears[0].category == "待核对"


def test_raw_json_does_not_gain_downloader_fields():
    doc = record("motion")
    client = Client("http://example.test", threading.Event())
    client.envelope = lambda *args: dict(doc)
    assert client._record_once("motion", doc["uid"], DEVICE, "23077E") == doc


def make_dialog(tmp_path):
    from cowmata_tailring.edge_download.pro_dialog import ProDownloadDialog
    from cowmata_tailring.edge_download.pro_settings import ProSettings

    job = fixture_job(tmp_path)
    store = ProSettings(tmp_path / "settings")
    store.value.update(
        data_root=str(job.farm), ledger_directory=str(job.ledger_directory), auto_enabled=True
    )
    return ProDownloadDialog(store=store)


def test_open_never_arms_even_with_old_auto_setting(tmp_path, qt_application):
    dialog = make_dialog(tmp_path)
    try:
        assert not dialog.timer.isActive() and not dialog.running
        dialog.show()
        qt_application.processEvents()
        assert not dialog.timer.isActive()
        assert [dialog.mode.itemData(i) for i in range(dialog.mode.count())] == [
            "",
            "manual",
            "automatic",
            "scheduled",
        ]
        dialog.mode.setCurrentIndex(dialog.mode.findData("automatic"))
        assert not dialog.timer.isActive()
        assert dialog.save_settings() and not dialog.timer.isActive()
    finally:
        dialog.stop_task()
        dialog.deleteLater()


def test_scheduled_waits_for_explicit_start_and_pause_disarms(tmp_path, qt_application):
    dialog = make_dialog(tmp_path)
    try:
        dialog.mode.setCurrentIndex(dialog.mode.findData("scheduled"))
        dialog.scheduled_at.setDateTime(QDateTime.fromString((datetime.now(CHINA) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"), "yyyy-MM-dd HH:mm:ss"))
        assert not dialog.timer.isActive()
        dialog.start_selected()
        assert dialog.timer.isActive() and not dialog.running
        dialog.pause()
        assert not dialog.timer.isActive()
        dialog.show()
        assert not dialog.timer.isActive()
    finally:
        dialog.stop_task()
        dialog.deleteLater()


def test_uploader_131_pull_uses_memory_session(tmp_path, monkeypatch):
    import base64
    import hashlib

    from cowmata_tailring.edge_download.site_records import SCHEMAS, LedgerClient, settings_defaults

    values = settings_defaults()
    values["session_token"] = "test-memory-session"
    client = LedgerClient(values, threading.Event())
    seen = []
    content = (",".join(SCHEMAS["samples"]["fields"]) + "\r\n").encode("utf-8-sig")

    def request(payload):
        seen.append(payload)
        return json.dumps(
            dict(
                version=2,
                ok=True,
                target=payload["target"],
                sheet_id="samples",
                count=0,
                content=base64.b64encode(content).decode(),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        ).encode()

    monkeypatch.setattr(client, "request", request, raising=False)
    assert client.pull("samples") == content
    assert seen[0]["session_token"] == "test-memory-session"
    assert seen[0]["action"] == "pull" and seen[0]["changes"] == []


def test_manual_and_automatic_only_run_after_start(tmp_path, qt_application):
    import time
    from contextlib import nullcontext

    from cowmata_tailring.edge_download.core import Result
    from cowmata_tailring.edge_download.pro_dialog import SyncWorker

    for mode in ("manual", "automatic"):
        case = tmp_path / mode
        case.mkdir()
        dialog = make_dialog(case)
        dialog.sync_ledger.setChecked(False)

        def factory(values, operation, parent):
            worker = SyncWorker(values, operation, parent, runner=lambda *a: Result())
            return worker

        import cowmata_tailring.edge_download.pro_dialog as module

        original = module.raw_connection
        module.raw_connection = lambda *a: nullcontext()
        dialog.worker_factory = factory
        try:
            dialog.mode.setCurrentIndex(dialog.mode.findData(mode))
            assert not dialog.running and not dialog.timer.isActive()
            dialog.start_selected()
            assert dialog.running
            for _ in range(100):
                qt_application.processEvents()
                if not dialog.running:
                    break
                time.sleep(0.01)
            assert not dialog.running
            assert dialog.timer.isActive() == (mode == "automatic")
            dialog.pause()
            assert not dialog.timer.isActive()
        finally:
            module.raw_connection = original
            dialog.stop_task()
            if dialog.worker:
                dialog.worker.wait(2000)
            dialog.deleteLater()


def test_changed_file_is_repaired_and_missing_file_is_downloaded_again(tmp_path):
    job = fixture_job(tmp_path)
    docs = [("motion", record("motion")), ("pulse", record("pulse"))]
    calls = []
    fake = client_for(docs, calls)
    assert run_csv_job(job, threading.Event(), client_factory=fake).saved == 2
    motion = next(job.farm.glob("产犊/Motion/*/*/*.json"))
    pulse = next(job.farm.glob("产犊/PPG/*/*/*.json"))
    motion.write_bytes(b"{}")
    pulse.unlink()
    result = run_csv_job(job, threading.Event(), client_factory=fake)
    assert (result.saved, result.failed, len(calls)) == (2, 0, 4)
    assert json.loads(motion.read_bytes()) == docs[0][1]
    assert json.loads(pulse.read_bytes()) == docs[1][1]
    assert any(p.read_bytes() == b"{}" for p in (job.farm / ".edge-download/recovery").iterdir())


def test_legacy_without_uid_is_not_duplicated_after_detail_check(tmp_path):
    job = fixture_job(tmp_path)
    doc = record("pulse")
    old_doc = {k: v for k, v in doc.items() if k != "uid"}
    old = (
        job.farm / "怀孕/孕早期/PPG/2026-08-18" / (DEVICE + "-23077-E") / "2026-08-18_00-41-42.json"
    )
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps(old_doc))
    calls = []
    fake = client_for([("pulse", doc)], calls)
    result = run_csv_job(job, threading.Event(), client_factory=fake)
    assert (result.saved, result.skipped, len(calls)) == (0, 1, 1)
    assert run_csv_job(job, threading.Event(), client_factory=fake).skipped == 1
    assert len(calls) == 1


def test_scheduled_deadline_while_other_task_runs_is_not_lost(tmp_path, qt_application):
    dialog = make_dialog(tmp_path)
    try:
        dialog.mode.setCurrentIndex(dialog.mode.findData("scheduled"))
        dialog.scheduled_at.setDateTime(QDateTime.fromString((datetime.now(CHINA) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"), "yyyy-MM-dd HH:mm:ss"))
        dialog.start_selected()
        dialog.store.value["scheduled_time"] = (
            datetime.now(CHINA) - timedelta(seconds=1)
        ).isoformat()
        dialog.workers = lambda: [object()]
        dialog.timer_fired()
        assert dialog.armed and dialog.timer.isActive()
        dialog.workers = lambda: []
    finally:
        dialog.workers = lambda: []
        dialog.stop_task()
        dialog.deleteLater()


def test_settings_reject_persisting_login_secret(tmp_path):
    from cowmata_tailring.edge_download.pro_settings import ProSettings

    store = ProSettings(tmp_path / "settings")
    with pytest.raises(DownloadError):
        store.save(session_token="secret")
    assert not store.path.exists()


def test_bad_equipment_end_cannot_look_like_normal_wear(tmp_path):
    job = fixture_job(tmp_path)
    row = {
        "日期": "2026-08-18",
        "新佩戴牛号": "23077-E",
        "设备编码": DEVICE,
        "拆除时间(掉落）": "新硅胶垫",
        "记录类型": "佩戴",
        "已删除": "0",
    }
    with (job.ledger_directory / "扬大测试设备台账.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as f:
        w = csv.DictWriter(f, fieldnames=row)
        w.writeheader()
        w.writerow(row)
    plan = CsvPlan(job.ledger_directory)
    equipment = next(w for w in plan.wears if w.source == "扬大测试设备台账.csv")
    assert equipment.category == "待核对"
    assert plan.issues


def test_login_session_never_enters_settings_or_cycle_history(tmp_path, qt_application):
    import time

    from cowmata_tailring.edge_download.site_records import LedgerClient, settings_defaults

    client = LedgerClient(settings_defaults(), threading.Event())
    requests = []
    session = dict(
        token="only-in-memory",
        user={"role": "operator", "username": "tester"},
        expires_at=time.time() + 3600,
    )

    def request(payload):
        requests.append(payload)
        return json.dumps(dict(version=2, ok=True, **session)).encode()

    client.request = request
    assert client.login("tester", "test-password") == session
    assert requests[0]["action"] == "login"
    assert "changes" not in requests[0]
    dialog = make_dialog(tmp_path)
    try:
        report = dict(errors=[], motion=None, ledger=None, canceled=False, session=session)
        dialog.cycle_completed(report)
        assert dialog.session["token"] == "only-in-memory"
        text = dialog.store.path.read_text("utf-8")
        assert "only-in-memory" not in text and "test-password" not in text
    finally:
        dialog.stop_task()
        dialog.deleteLater()
