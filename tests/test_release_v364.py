import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_classifier_hotfix import video_row
from test_fixes_v351 import organizer  # noqa: F401


def test_app_starts_without_entering_network_update_gate(tmp_path):
    script = """import sys,types
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from cowmata_tailring.app import main,update_ui
from PySide6.QtWidgets import QWidget,QApplication
from PySide6.QtCore import QTimer
def gate(): raise AssertionError('Network gate was called before workspace')
update_ui.verify_startup_update=gate
update_ui.UpdateController=lambda window:None
class Window(QWidget):
 def __init__(self):
  super().__init__();print('WORKSPACE_READY',flush=True);QTimer.singleShot(20,QApplication.instance().quit)
m=types.ModuleType('cowmata_tailring.workspace.modern_window');m.MainWindow=Window
sys.modules[m.__name__]=m
raise SystemExit(main.main(['--mode','workspace']))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(Path.cwd())], capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"WORKSPACE_READY" in result.stdout


def test_every_view_starts_copy_while_health_checks_are_still_running(tmp_path, monkeypatch):
    from cowmata_tailring.workspace import fast_transfer, intake_health
    from cowmata_tailring.workspace import video_intake as v

    monkeypatch.setattr(v, "inspect", video_row)
    specs = []
    entered = [threading.Event() for _ in range(3)]
    for i in range(3):
        path = tmp_path / f"cam{i}" / "a.mp4"
        path.parent.mkdir()
        path.write_bytes(bytes([i + 1]) * 512)
        specs.append(dict(kind="video", path=str(path.parent), camera=f"视角0{i + 1}"))
    copy = fast_transfer.copy_verified

    def copying(src, *args, **kwargs):
        entered[int(Path(src).parent.name[-1])].set()
        return copy(src, *args, **kwargs)

    def checking(path, cache, cancelled=lambda: False):
        assert all(e.wait(3) for e in entered), (
            "A view waits for full decoding before its copy can start"
        )
        return dict(stamp=v.core.file_stamp(path), delete_reason="")

    monkeypatch.setattr(fast_transfer, "copy_verified", copying)
    monkeypatch.setattr(intake_health, "assess_video", checking)
    result = v.organize(
        tmp_path / "farm",
        specs,
        category="calving",
        farm=str(tmp_path / "farm"),
        job=tmp_path / "job",
        cache=tmp_path / "cache",
        delete_unusable=True,
    )
    assert result["archived_files"] == 3
    for r in result["rows"]:
        assert Path(r["target"]).read_bytes() == Path(r["source"]).read_bytes()
        assert 0 <= r["transfer_seconds"] <= r["file_seconds"] + 0.01


def test_new_root_clears_old_table_details_progress_and_live_view(organizer, tmp_path):  # noqa: F811
    from cowmata_tailring.workspace.classification_report import LiveReport
    from cowmata_tailring.workspace.classification_viewer import ClassificationReportWindow

    job = tmp_path / "old-job"
    with LiveReport(job) as report:
        report.row(
            dict(
                source=str(tmp_path / "old.mp4"),
                status="done",
                target="old-target",
                message="old-details",
            )
        )
    organizer.job = job
    organizer.apply_report_snapshot()
    organizer.table.selectRow(0)
    organizer.record_details.setPlainText("old-details")
    viewer = ClassificationReportWindow(organizer, lambda: organizer.job)
    viewer.show()
    viewer.refresh()
    folder = tmp_path / "new"
    folder.mkdir()
    (folder / "a.mp4").write_bytes(b"new")
    organizer.set_video_root(folder)
    viewer.refresh()
    assert organizer.job is None and organizer.model.rowCount() == 0
    assert not organizer.record_details.toPlainText() and organizer.bar.value() == 0
    assert viewer.model.rowCount() == 0 and not viewer.details.text()
    assert (job / "归类记录.csv").exists()
    assert not organizer.record_details.isVisible()
    button = getattr(organizer, "records_button", None)
    assert button is not None and button.isVisible()
    viewer.close()


def test_download_progress_can_continue_past_five_minutes(monkeypatch):
    from cowmata_tailring.edge_download import core

    class Reply:
        url = "http://localhost/data"
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read1(self, n):
            self.step += 1
            return b"chunk" if self.step <= 4 else b""

        step = 0

    monkeypatch.setattr(core, "urlopen", lambda *a, **k: Reply())
    import time

    tick = iter(range(0, 10000, 120))
    monkeypatch.setattr(time, "monotonic", lambda: next(tick))
    assert (
        core.Client("http://localhost", threading.Event()).get("http://localhost/data")
        == b"chunk" * 4
    )


def test_device_server_accepts_uid_only_details_and_missing_cow(tmp_path, monkeypatch):
    import base64

    from cowmata_tailring.edge_download import core

    client = core.Client("https://data.cowmata.com", threading.Event())

    def envelope(path, params):
        assert params == {"uid": 123}, "Device-server details must use UID-only queries"
        return dict(
            device="ABC",
            version=0,
            imu=base64.b64encode(bytes(18)).decode(),
            create_time=1786896000000,
        )

    monkeypatch.setattr(client, "envelope", envelope)
    record = client.record("motion", 123, "ABC", "")
    assert record.get("cow_id", "") == ""


def test_temperature_raw_payload_is_preserved():
    from cowmata_tailring.edge_download.core import validate_payload

    data = dict(create_time=1786896000000, device="ABC", data=38.2)
    validate_payload(data, "temp")
    assert data["data"] == 38.2


def test_record_time_updates_while_verification_is_running(tmp_path, monkeypatch):
    import time

    from cowmata_tailring.workspace import intake_health
    from cowmata_tailring.workspace import video_intake as v
    from cowmata_tailring.workspace.classification_report import read_snapshot

    monkeypatch.setattr(v, "inspect", video_row)
    path = tmp_path / "cam/a.mp4"
    path.parent.mkdir()
    path.write_bytes(b"video" * 100)
    job = tmp_path / "job"

    def checking(path, cache, cancelled=lambda: False):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rows = read_snapshot(job)["rows"]
            if rows and rows[0].get("file_seconds", 0) >= 0.5:
                return dict(stamp=v.core.file_stamp(path), delete_reason="")
            time.sleep(0.05)
        pytest.fail("The live CSV keeps a frozen duration during verification")

    monkeypatch.setattr(intake_health, "assess_video", checking)
    result = v.organize(
        tmp_path / "farm",
        [dict(kind="video", path=str(path.parent), camera="视角01")],
        category="calving",
        farm=str(tmp_path / "farm"),
        job=job,
        cache=tmp_path / "cache",
        delete_unusable=True,
    )
    assert result["archived_files"] == 1
    row = result["rows"][0]
    assert row["health_seconds"] >= 0.5 and row["transfer_seconds"] < row["file_seconds"]


def test_public_data_tls_bundle_keeps_strict_validation():
    import ssl

    from cowmata_tailring.edge_download.transport import tls_context

    context = tls_context("https://data.cowmata.com/data.bin")
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


def test_invalid_download_payload_retries_in_current_run(monkeypatch):
    import base64

    from cowmata_tailring.edge_download.core import Client

    client = Client("https://data.cowmata.com", threading.Event())
    attempts = []

    def envelope(*args):
        attempts.append(1)
        return dict(
            device="ABC",
            version=0,
            imu="bad" if len(attempts) < 4 else base64.b64encode(bytes(18)).decode(),
            create_time=1786896000000,
        )

    monkeypatch.setattr(client, "envelope", envelope)
    assert client.record("motion", 1, "ABC", "")["imu"] == base64.b64encode(bytes(18)).decode()
    assert len(attempts) == 4
