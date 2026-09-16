"""Behavioral coverage for automatic raw downloads and the Ledger 1.1.0 integration."""

import base64
import csv
import hashlib
import io
import json
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from cowmata_tailring.edge_download.automatic import run_automatic_job
from cowmata_tailring.edge_download.core import CHINA, Cancelled, DownloadError, Job
from cowmata_tailring.edge_download.ledger import LedgerPlan, parse_csv_ledger
from cowmata_tailring.edge_download.site_records import (
    SCHEMAS,
    decode_reply,
    read_csv,
    records_state_directory,
    refresh_records,
    settings_defaults,
)

START = datetime(2026, 8, 17, tzinfo=CHINA)
DEVICE = "546C50CA07FA"


def csv_content(sheet, **changes):
    row = dict.fromkeys(SCHEMAS[sheet]["fields"], "")
    row.update({"记录ID": str(uuid.uuid4()), "版本": str(uuid.uuid4()), "已删除": "0"})
    if sheet == "samples":
        row.update(
            {
                "设备号": DEVICE,
                "牛号": "21231A2",
                "佩戴开始": "2026-08-16 08:00",
                "佩戴结束": "2026-08-18 18:00",
                "数据分类": "pregnancy_late",
            }
        )
    row.update(changes)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, SCHEMAS[sheet]["fields"])
    writer.writeheader()
    writer.writerow(row)
    return stream.getvalue().encode("utf-8-sig")


def values_for(tmp_path):
    return {
        **settings_defaults(),
        "ledger_directory": str(tmp_path / "现场记录"),
        "data_root": str(tmp_path / "数据"),
    }


def make_reply(content, sheet, target):
    return dict(
        version=2,
        ok=True,
        sheet_id=sheet,
        target=target,
        count=len(read_csv(content, sheet)),
        content=base64.b64encode(content).decode(),
        sha256=hashlib.sha256(content).hexdigest(),
    )


@pytest.mark.parametrize("sheet", list(SCHEMAS))
def test_actual_uploader_schema_and_reply(sheet):
    data = csv_content(sheet)
    target = "F:\\牛舍_现场记录\\" + SCHEMAS[sheet]["filename"]
    assert decode_reply(json.dumps(make_reply(data, sheet, target)), sheet, target) == data


@pytest.mark.parametrize(
    "change",
    [
        dict(version=1),
        dict(ok=False),
        dict(target="elsewhere"),
        dict(sheet_id="wrong"),
        dict(sha256="0" * 64),
        dict(content="???"),
        dict(count=100),
    ],
)
def test_bad_reply_never_accepted(change):
    data = csv_content("samples")
    reply = make_reply(data, "samples", "target")
    reply.update(change)
    with pytest.raises(DownloadError):
        decode_reply(json.dumps(reply), "samples", "target")


def test_refresh_reuses_bytes_and_backs_up_all_changed_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    values = values_for(tmp_path)
    payload = {s: csv_content(s) for s in SCHEMAS}

    class Client:
        def __init__(self, *args):
            pass

        def pull(self, sheet):
            return payload[sheet]

    first = refresh_records(values, threading.Event(), client_factory=Client)
    assert first["changed"] == 3
    folder = Path(values["ledger_directory"])
    stamps = {s: (folder / SCHEMAS[s]["filename"]).stat().st_mtime_ns for s in SCHEMAS}
    assert refresh_records(values, threading.Event(), client_factory=Client)["changed"] == 0
    assert stamps == {s: (folder / SCHEMAS[s]["filename"]).stat().st_mtime_ns for s in SCHEMAS}
    previous = dict(payload)
    payload["samples"] = csv_content("samples", **{"数据分类": "healthy"})
    assert refresh_records(values, threading.Event(), client_factory=Client)["changed"] == 1
    backup = (
        records_state_directory(folder)
        / "csv-backups"
        / hashlib.sha256(previous["samples"]).hexdigest()
        / SCHEMAS["samples"]["filename"]
    )
    assert backup.read_bytes() == previous["samples"]
    assert {p.name for p in folder.iterdir()} == {v["filename"] for v in SCHEMAS.values()}
    for sheet in SCHEMAS:
        assert (folder / SCHEMAS[sheet]["filename"]).read_bytes() == payload[sheet]


@pytest.mark.parametrize("failure", ["network", "corrupt", "cancel"])
def test_failed_last_csv_keeps_entire_previous_set(tmp_path, failure):
    values = values_for(tmp_path)
    folder = Path(values["ledger_directory"])
    folder.mkdir()
    old = {s: csv_content(s) for s in SCHEMAS}
    cancel = threading.Event()
    for s in SCHEMAS:
        (folder / SCHEMAS[s]["filename"]).write_bytes(old[s])

    class Client:
        def __init__(self, *args):
            pass

        def pull(self, sheet):
            if sheet == "equipment":
                if failure == "network":
                    raise DownloadError("network")
                if failure == "corrupt":
                    return b"bad"
                cancel.set()
                raise Cancelled()
            return csv_content(sheet)

    with pytest.raises((DownloadError, Cancelled)):
        refresh_records(values, cancel, client_factory=Client)
    for s in SCHEMAS:
        assert (folder / SCHEMAS[s]["filename"]).read_bytes() == old[s]


def test_user_edit_during_pull_is_kept(tmp_path):
    values = values_for(tmp_path)
    folder = Path(values["ledger_directory"])
    folder.mkdir()
    old = csv_content("samples")
    edited = csv_content("samples")
    target = folder / SCHEMAS["samples"]["filename"]
    target.write_bytes(old)

    class Client:
        def __init__(self, *args):
            pass

        def pull(self, sheet):
            if sheet == "equipment":
                target.write_bytes(edited)
            return csv_content(sheet)

    with pytest.raises(DownloadError, match="有修改"):
        refresh_records(values, threading.Event(), client_factory=Client)
    assert target.read_bytes() == edited


def test_replace_failure_rolls_back_already_written_csv(tmp_path, monkeypatch):
    from cowmata_tailring.edge_download import site_records

    values = values_for(tmp_path)
    folder = Path(values["ledger_directory"])
    folder.mkdir()
    old = {s: csv_content(s) for s in SCHEMAS}
    for s in SCHEMAS:
        (folder / SCHEMAS[s]["filename"]).write_bytes(old[s])

    class Client:
        def __init__(self, *args):
            pass

        def pull(self, sheet):
            return csv_content(sheet)

    original = site_records._write

    def fail(path, raw):
        if path == folder / SCHEMAS["calving"]["filename"]:
            raise PermissionError("open in Excel")
        return original(path, raw)

    monkeypatch.setattr(site_records, "_write", fail)
    with pytest.raises(PermissionError):
        refresh_records(values, threading.Event(), client_factory=Client)
    for s in SCHEMAS:
        assert (folder / SCHEMAS[s]["filename"]).read_bytes() == old[s]


def test_csv_plan_uses_exact_device_historical_cow_and_time(tmp_path):
    file = tmp_path / "样本试验台账.csv"
    file.write_bytes(csv_content("samples"))
    plan = LedgerPlan(parse_csv_ledger(file))
    record = dict(device=DEVICE, cow_id="21231A2", create_time=int(START.timestamp() * 1000))
    assert plan.classify(record)["category"] == "怀孕/孕晚期"
    assert plan.classify({**record, "cow_id": "other"})["category"] == "待核对"
    assert plan.classify({**record, "device": "000000000000"})["category"] == "未分类"
    file.write_bytes(csv_content("samples", **{"已删除": "1"}))
    assert LedgerPlan(parse_csv_ledger(file)).classify(record)["category"] == "未分类"


def test_overlapping_wearing_records_stay_pending(tmp_path):
    file = tmp_path / "样本试验台账.csv"
    file.write_bytes(csv_content("samples"))
    value = parse_csv_ledger(file)
    value["entries"].append({**value["entries"][0], "cow": "other"})
    assert (
        LedgerPlan(value).classify(
            dict(device=DEVICE, cow_id="21231A2", create_time=int(START.timestamp() * 1000))
        )["category"]
        == "待核对"
    )


@pytest.fixture
def automatic_server():
    stamp = int(START.timestamp() * 1000)
    state = dict(
        records=[
            dict(
                uid=stamp,
                device=DEVICE,
                cow_id="21231A2",
                create_time=stamp,
                revision="1",
                downloadable=True,
                archive_state="ready",
            )
        ],
        details=0,
        pages=[],
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            if parsed.path == "/health":
                data = {"ok": True}
            elif parsed.path == "/device/data/records":
                cursor = int(query.get("cursor", 0))
                state["pages"].append(cursor)
                row = state["records"][cursor : cursor + 1]
                end = cursor + len(row)
                data = dict(
                    records=row,
                    nextCursor=end,
                    snapshotUid=len(state["records"]),
                    scanned=len(row),
                    hasMore=end < len(state["records"]),
                )
            else:
                state["details"] += 1
                data = dict(
                    device=DEVICE,
                    cow_id="21231A2",
                    create_time=int(query["uid"]),
                    version=2,
                    imu=base64.b64encode(bytes(44)).decode(),
                )
            raw = json.dumps(dict(code=0, data=data)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:" + str(httpd.server_port), state
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def test_paginated_sync_late_upload_pending_and_repair(tmp_path, automatic_server):
    url, state = automatic_server
    job = Job(url, tmp_path, "未分类", (), ("motion",), START, START + timedelta(days=1))
    result = run_automatic_job(job, threading.Event())
    assert (result.saved, result.failed) == (1, 0)
    assert run_automatic_job(job, threading.Event()).skipped == 1 and state["details"] == 1
    second = {
        **state["records"][0],
        "uid": int((START + timedelta(hours=1)).timestamp() * 1000),
        "create_time": int((START + timedelta(hours=1)).timestamp() * 1000),
        "downloadable": False,
        "archive_state": "pending",
    }
    state["records"].append(second)
    result = run_automatic_job(job, threading.Event())
    assert result.pending == 1 and result.skipped == 1
    second.update(downloadable=True, archive_state="ready")
    result = run_automatic_job(job, threading.Event())
    assert result.saved == 1 and state["pages"][-2:] == [0, 1]
    file = next((tmp_path / "未分类/Motion").rglob("*.json"))
    file.write_bytes(b"corrupt")
    result = run_automatic_job(job, threading.Event())
    assert result.saved == 1 and json.loads(file.read_bytes())["imu"]
    assert any(p.read_bytes() == b"corrupt" for p in (tmp_path/".edge-download/recovery").iterdir())


def test_csv_revision_reclassifies_without_rewriting_raw_bytes(tmp_path, automatic_server):
    url, state = automatic_server
    led = tmp_path / "现场记录"
    led.mkdir()
    csvfile = led / "样本试验台账.csv"
    csvfile.write_bytes(csv_content("samples"))
    job = Job(url, tmp_path, "未分类", (), ("motion",), START, START + timedelta(days=1), led)
    assert run_automatic_job(job, threading.Event()).saved == 1
    first = next((tmp_path / "怀孕/孕晚期/Motion").rglob("*.json"))
    raw = first.read_bytes()
    csvfile.write_bytes(csv_content("samples", **{"数据分类": "healthy"}))
    result = run_automatic_job(job, threading.Event())
    assert result.failed == 0 and result.skipped == 1
    assert not first.exists()
    assert next((tmp_path / "正常/Motion").rglob("*.json")).read_bytes() == raw
    assert state["details"] == 1


def spin(app, predicate):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Qt operation timed out")


def test_worker_keeps_csv_success_when_motion_connection_fails(tmp_path, qt_application):
    from cowmata_tailring.edge_download.pro_dialog import SyncWorker

    values = values_for(tmp_path)
    values.update(server="http://127.0.0.1:18031", start_time=START.isoformat())
    reports = []

    def connector(*args):
        raise DownloadError("missing motion authorization")

    worker = SyncWorker(
        values, connector=connector, refresher=lambda *args: dict(changed=3, counts={})
    )
    worker.completed.connect(reports.append)
    worker.start()
    spin(qt_application, lambda: bool(reports))
    assert worker.wait(2000)
    assert reports[0]["ledger"]["changed"] == 3 and reports[0]["motion"] is None
    assert "missing motion authorization" in reports[0]["errors"][0]
    worker.deleteLater()


def test_pro_ui_saves_locations_and_pauses_then_can_restart(tmp_path, qt_application):
    from cowmata_tailring.edge_download.pro_dialog import ProDownloadDialog
    from cowmata_tailring.edge_download.pro_settings import ProSettings

    store = ProSettings(tmp_path / "config")
    store.value.update(values_for(tmp_path))
    store.value.update(auto_enabled=False, server="http://127.0.0.1:18031")
    dialog = ProDownloadDialog(store=store, launch_automatically=False)
    assert dialog.ledger_directory.text() == str(tmp_path / "现场记录")
    assert dialog.raw_mode.currentData() == "http"
    dialog.mode.setCurrentIndex(dialog.mode.findData("automatic"))
    assert not dialog.timer.isActive()
    dialog.pause()
    assert not dialog.scheduling_stopped and not dialog.timer.isActive()
    assert dialog.save_settings()
    restored = ProSettings(tmp_path / "config")
    assert restored.value["ledger_directory"] == str(tmp_path / "现场记录")
    dialog.close()
    dialog.deleteLater()


def test_host_close_waits_for_new_worker_and_stops_timer(tmp_path, qt_application):
    from PySide6.QtWidgets import QMainWindow

    from cowmata_tailring.edge_download.integration import install_menu
    from cowmata_tailring.edge_download.pro_dialog import ProDownloadDialog, SyncWorker
    from cowmata_tailring.edge_download.pro_settings import ProSettings

    store = ProSettings(tmp_path / "config")
    store.value.update(values_for(tmp_path))
    store.value.update(auto_enabled=False)

    def refresh(values, cancel, log):
        cancel.wait(5)
        raise Cancelled()

    def factory(values, operation, parent):
        return SyncWorker(
            values, operation, parent, refresher=refresh
        )
    host = QMainWindow()
    controller = install_menu(host, host.menuBar().addMenu("数据准备"))
    dialog = ProDownloadDialog(
        host, store=store, worker_factory=factory, launch_automatically=False
    )
    controller.dialog = dialog
    host.show()
    dialog.start_task("ledger")
    assert dialog.running
    host.close()
    spin(qt_application, lambda: not host.isVisible() and not dialog.running)
    assert not dialog.timer.isActive()
    host.deleteLater()


@pytest.mark.parametrize("rejected", [False, True])
def test_connection_only_sends_pull_and_uses_uploader_ssh_flags(tmp_path, monkeypatch, rejected):
    from cowmata_tailring.edge_download import site_records

    app = tmp_path / "app"
    exe = app / "vendor/ledger-ssh/usr/bin/ssh.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    key = tmp_path / "key"
    key.write_bytes(b"synthetic")
    values = values_for(tmp_path)
    values["ledger_key"] = str(key)
    captured = {}

    class Bridge:
        port = 12345

        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Input(io.BytesIO):
        def close(self):
            captured["request"] = json.loads(self.getvalue())
            super().close()

    class Process:
        returncode = 1 if rejected else 0

        def __init__(self, args, **kwargs):
            captured["args"] = args
            self.stdin = Input()
            target = values["ledger_server_directory"] + "\\" + SCHEMAS["samples"]["filename"]
            reply = (
                dict(version=2, ok=False, error="目标路径与服务器固定路径不一致")
                if rejected
                else make_reply(csv_content("samples"), "samples", target)
            )
            kwargs["stdout"].write(json.dumps(reply).encode())
            kwargs["stdout"].flush()

        def poll(self):
            return self.returncode

        def wait(self, **kwargs):
            return self.returncode

    monkeypatch.setattr(site_records.subprocess, "Popen", Process)
    client = site_records.LedgerClient(
        values, threading.Event(), app_root=app, bridge_factory=Bridge
    )
    if rejected:
        with pytest.raises(DownloadError, match="目标路径与服务器固定路径不一致"):
            client.pull("samples")
    else:
        client.pull("samples")
    assert captured["request"]["action"] == "pull" and captured["request"]["changes"] == []
    assert (
        "StrictHostKeyChecking=yes" in captured["args"] and "ProxyCommand=none" in captured["args"]
    )
    assert captured["args"][-1] == "cowmata-ledger-upload-v2"


@pytest.mark.parametrize("fail_body", [False, True])
def test_raw_direct_connection_uses_physical_bridge_and_closes_ssh(
    tmp_path, monkeypatch, fail_body
):
    from cowmata_tailring.edge_download import raw_connection as module

    values = values_for(tmp_path)
    key = tmp_path / "raw-key"
    key.write_bytes(b"synthetic")
    values.update(
        server="http://127.0.0.1:18031",
        raw_connection="direct_ssh",
        raw_key=str(key),
        raw_user="administrator",
        raw_remote_port=8031,
    )
    actions = []

    class Bridge:
        port = 33333

        def __init__(self, host, port):
            actions.append(("bridge", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            actions.append("bridge_closed")

    class Process:
        stopped = False

        def __init__(self, args, **kwargs):
            actions.append(args)

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True
            actions.append("ssh_closed")

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(module, "DirectBridge", Bridge)
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    monkeypatch.setattr(module, "health", lambda *a, **kw: True)

    def run():
        with module.raw_connection(values, threading.Event(), lambda msg: None):
            if fail_body:
                raise DownloadError("body failed")
            actions.append("download")

    if fail_body:
        with pytest.raises(DownloadError, match="body failed"):
            run()
    else:
        run()
    assert "ssh_closed" in actions and "bridge_closed" in actions
    args = actions[1]
    assert "127.0.0.1:18031:127.0.0.1:8031" in args
    assert "StrictHostKeyChecking=yes" in args and "ProxyJump=none" in args


def test_pre_cancel_does_not_open_raw_direct_connection(tmp_path, monkeypatch):
    from cowmata_tailring.edge_download import raw_connection as module

    values = values_for(tmp_path)
    key = tmp_path / "raw-key"
    key.write_bytes(b"synthetic")
    values.update(
        server="http://127.0.0.1:18031",
        raw_connection="direct_ssh",
        raw_key=str(key),
        raw_user="administrator",
    )
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(module, "DirectBridge", lambda *a: pytest.fail("unexpected connection"))
    with pytest.raises(Cancelled):
        with module.raw_connection(values, cancel, lambda msg: None):
            pass
