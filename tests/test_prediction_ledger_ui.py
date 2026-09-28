"""Prediction downloads require the primary ledger without relaxing authorization."""

import contextlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from cowmata_tailring.edge_download import site_records
from cowmata_tailring.edge_download.core import Cancelled, DownloadError
from cowmata_tailring.edge_download.site_records import SCHEMAS, refresh_records
from test_edge_download_384 import csv_content, values_for


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    values = values_for(tmp_path)
    folder = Path(values["ledger_directory"])
    folder.mkdir()
    previous = {sheet: csv_content(sheet) for sheet in SCHEMAS}
    for sheet, content in previous.items():
        (folder / SCHEMAS[sheet]["filename"]).write_bytes(content)
    return values, folder, previous


def current_bytes(folder):
    return {sheet: (folder / schema["filename"]).read_bytes()
            for sheet, schema in SCHEMAS.items()}


def fake_client(payloads, failures=None):
    class Client:
        def __init__(self, *args):
            pass

        def pull(self, sheet):
            if sheet in (failures or {}):
                raise failures[sheet]
            return payloads[sheet]

    return Client


@pytest.mark.parametrize("have_old_auxiliary", [True, False])
def test_primary_refresh_succeeds_with_unavailable_auxiliary(mirror, have_old_auxiliary):
    values, folder, previous = mirror
    if not have_old_auxiliary:
        for sheet in ("calving", "equipment"):
            (folder / SCHEMAS[sheet]["filename"]).unlink()
    new_sample = csv_content("samples", **{"佩戴结束": ""})
    messages = []
    result = refresh_records(
        values, threading.Event(), messages.append,
        client_factory=fake_client(
            {"samples": new_sample},
            {"calving": DownloadError("服务器暂无该表"), "equipment": OSError("网络中断")},
        ),
        primary_only=True,
    )
    assert result["changed"] == 1
    assert result["counts"] == {"samples": 1}
    assert set(result["warnings"]) == {"calving", "equipment"}
    assert (folder / SCHEMAS["samples"]["filename"]).read_bytes() == new_sample
    for sheet in ("calving", "equipment"):
        path = folder / SCHEMAS[sheet]["filename"]
        if have_old_auxiliary:
            assert path.read_bytes() == previous[sheet]
        else:
            assert not path.exists()
    state = json.loads((site_records.records_state_directory(folder) / "last-csv-sync.json").read_text("utf-8"))
    assert set(state["files"]) == {"samples"}
    assert state["warnings"] == result["warnings"]
    assert any("保留本地旧表" in message for message in messages)


def test_default_refresh_keeps_three_sheet_transaction(mirror):
    values, folder, previous = mirror
    with pytest.raises(DownloadError, match="辅助表不可用"):
        refresh_records(values, threading.Event(), client_factory=fake_client(
            {sheet: csv_content(sheet) for sheet in SCHEMAS},
            {"equipment": DownloadError("辅助表不可用")},
        ))
    assert current_bytes(folder) == previous


@pytest.mark.parametrize("failure", [DownloadError("主表不可用"), PermissionError("授权已过期")])
def test_primary_failure_never_uses_stale_primary_as_success(mirror, failure):
    values, folder, previous = mirror
    with pytest.raises(type(failure)):
        refresh_records(values, threading.Event(), primary_only=True,
                        client_factory=fake_client({}, {"samples": failure}))
    assert current_bytes(folder) == previous


@pytest.mark.parametrize("failure", [
    PermissionError("会话已过期"), DownloadError("台账连接未完成：授权失败"),
    DownloadError("Permission denied (publickey)"), Cancelled(),
])
def test_optional_mode_never_swallows_authorization_or_cancel(mirror, failure):
    values, folder, previous = mirror
    with pytest.raises(type(failure)):
        refresh_records(values, threading.Event(), primary_only=True,
                        client_factory=fake_client(
                            {sheet: csv_content(sheet) for sheet in SCHEMAS},
                            {"equipment": failure},
                        ))
    assert current_bytes(folder) == previous


def test_corrupt_auxiliary_preserves_old_bytes_and_reports_warning(mirror):
    values, folder, previous = mirror
    payloads = {sheet: csv_content(sheet) for sheet in SCHEMAS}
    payloads["calving"] = b"invalid header\n"
    result = refresh_records(values, threading.Event(), primary_only=True,
                             client_factory=fake_client(payloads))
    assert result["changed"] == 2
    assert set(result["warnings"]) == {"calving"}
    assert (folder / SCHEMAS["calving"]["filename"]).read_bytes() == previous["calving"]


def test_optional_mode_write_failure_rolls_back_successful_primary(mirror, monkeypatch):
    values, folder, previous = mirror
    original_write = site_records._write

    def failing_write(path, content):
        if path == folder / SCHEMAS["equipment"]["filename"]:
            raise OSError("disk full")
        original_write(path, content)

    monkeypatch.setattr(site_records, "_write", failing_write)
    with pytest.raises(OSError, match="disk full"):
        refresh_records(values, threading.Event(), primary_only=True,
                        client_factory=fake_client({sheet: csv_content(sheet) for sheet in SCHEMAS}))
    assert current_bytes(folder) == previous


def test_ui_refresh_requires_primary_and_continues_with_explicit_auxiliary_warning(tmp_path, monkeypatch):
    from cowmata_tailring.edge_download import pro_dialog

    values = dict(values_for(tmp_path), sync_ledger=True, server="http://example.invalid",
                  start_time="2026-09-26T00:00:00+08:00", end_time="2026-09-27T00:00:00+08:00")
    calls = []

    def refresh(current, cancel, log, *, primary_only):
        assert primary_only is True
        calls.append("refresh")
        return dict(changed=1, counts={"samples": 1}, warnings={"calving": "unavailable"})

    def run(*args, **kwargs):
        calls.append("download")
        return SimpleNamespace(saved=0, skipped=0, failed=0, pending=0, canceled=False)

    monkeypatch.setattr(pro_dialog, "raw_connection", lambda *args: contextlib.nullcontext())
    reports, receipts = [], []
    worker = pro_dialog.SyncWorker(values, refresher=refresh, runner=run)
    worker.completed.connect(reports.append)
    worker.ledger_refreshed.connect(receipts.append)
    worker.run()
    assert calls == ["refresh", "download"]
    assert not reports[0]["errors"]
    assert reports[0]["motion"] is not None
    assert receipts[0]["warnings"] == {"calving": "unavailable"}


def test_ui_primary_failure_never_starts_download(tmp_path):
    from cowmata_tailring.edge_download import pro_dialog

    def refresh(*args, primary_only):
        assert primary_only
        raise DownloadError("样本台账刷新失败")

    calls, reports = [], []
    worker = pro_dialog.SyncWorker(dict(values_for(tmp_path), sync_ledger=True), refresher=refresh,
                                   runner=lambda *args: calls.append(args))
    worker.completed.connect(reports.append)
    worker.run()
    assert not calls
    assert reports[0]["ledger"] is None
    assert any("样本台账刷新失败" in error for error in reports[0]["errors"])


def test_preview_passes_the_selected_data_root_to_prediction_plan(tmp_path, monkeypatch, qt_application):
    from cowmata_tailring.edge_download import pro_dialog

    calls = []

    class Plan:
        def __init__(self, folder, root=None):
            calls.append((folder, root))

        def preview(self):
            return []

    monkeypatch.setattr(pro_dialog, "PredictionPlan", Plan)
    worker = pro_dialog.PlanWorker(str(tmp_path / "ledger"), None, str(tmp_path / "raw"))
    worker.start()
    assert worker.wait()
    worker.timer.stop()
    result, error = worker.mail.get_nowait()
    assert not error
    assert result.local_status == {}
    assert calls == [(str(tmp_path / "ledger"), str(tmp_path / "raw"))]
    worker.deleteLater()


def test_receipt_distinguishes_auxiliary_warning_from_verified_data(qt_application):
    from PySide6.QtWidgets import QLabel
    from cowmata_tailring.edge_download.pro_dialog import ProDownloadDialog

    logged = []
    view = SimpleNamespace(csv_receipt=QLabel(), worker=None, refresh_info_line=lambda: None,
                           append=logged.append, refresh_plan=lambda: None)
    ProDownloadDialog.receive_ledger_receipt(view, dict(
        counts={"samples": 85}, warnings={"calving": "服务器不可用"}, changes={},
    ))
    text = view.csv_receipt.text()
    assert "样本试验台账.csv：已核验，共 85 条" in text
    assert "扬大产犊登记汇总.csv：未刷新，保留本地旧表" in text
    assert "扬大测试设备台账.csv：本轮未核验" in text
    assert "#fff0e5" in view.csv_receipt.styleSheet()
    view.csv_receipt.deleteLater()


def test_preview_retains_enrolled_wearing_after_end_is_filled(tmp_path, qt_application):
    from cowmata_tailring.edge_download.prediction import PredictionPlan
    from cowmata_tailring.edge_download.pro_dialog import PlanWorker

    folder, root = tmp_path / "ledger", tmp_path / "download"
    folder.mkdir()
    root.mkdir()
    path = folder / SCHEMAS["samples"]["filename"]
    content = csv_content("samples", **{"佩戴结束": ""})
    path.write_bytes(content)
    initial = PredictionPlan(folder, root=root)
    initial.persist()
    path.write_bytes(csv_content("samples", **{
        **site_records.read_csv(content, "samples")[0], "佩戴结束": "2026-08-18 18:00",
    }))

    assert PredictionPlan(folder).preview()[0]["eligibility"] == "excluded"
    worker = PlanWorker(str(folder), None, str(root))
    worker.start()
    assert worker.wait()
    worker.timer.stop()
    plan, error = worker.mail.get_nowait()
    assert not error
    row = plan.preview()[0]
    assert row["eligibility"] == "eligible"
    assert row["tracking_state"] == "closing"
    assert row["category"] == "待预测"
    worker.deleteLater()
