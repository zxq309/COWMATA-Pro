import json
import time
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QWidget
from test_paired_dataset_v370 import fixture_farm

from cowmata_tailring.workspace import dataset_build_ui as ui


def configure(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(ui, "QSettings", lambda: settings)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    owner = QWidget()
    return app, owner, ui.DatasetBuildWindow(owner)


def test_five_dataset_tasks_send_independent_requests(monkeypatch, tmp_path):
    app, owner, dialog = configure(tmp_path, monkeypatch)
    jobs = []
    monkeypatch.setattr(
        dialog,
        "start_job",
        lambda: jobs.append(json.loads((dialog.job / "request.json").read_text(encoding="utf-8"))),
    )
    dialog.sources.setPlainText(str(tmp_path / "farm"))
    dialog.target.setText(str(tmp_path / "datasets"))
    for task in ["behavior", "calving", "estrus", "pregnancy", "disease"]:
        dialog.set_task(task)
        dialog.submit()
        assert jobs[-1]["task"] == task and jobs[-1]["action"] == "paired_build"
        assert jobs[-1]['layout'] == 'current'
    assert dialog.task.count() == 5 and not hasattr(dialog, "tabs")
    dialog.close()
    owner.close()
    app.processEvents()


def test_background_pairing_publishes_live_counts(tmp_path, monkeypatch):
    app, owner, dialog = configure(tmp_path, monkeypatch)
    fixture_farm(tmp_path / "farm")
    dialog.sources.setPlainText(str(tmp_path / "farm"))
    dialog.target.setText(str(tmp_path / "datasets"))
    dialog.submit()
    stop = time.monotonic() + 30
    while dialog.running and time.monotonic() < stop:
        app.processEvents()
        time.sleep(0.01)
    try:
        assert not dialog.running, dialog.status.text()
        assert dialog.model.rowCount() == 2, dialog.status.text()
        assert all(row["status"] == "done" for row in dialog.model.rows)
        assert (Path(dialog.output) / "构建记录.csv").is_file()
        assert dialog.bar.value() == 2
        previous = dialog.output
        dialog.set_task("calving")
        assert not dialog.output and dialog.model.rowCount() == 0
        assert Path(previous).is_dir()
    finally:
        if dialog.process and dialog.process.state():
            dialog.process.kill()
            dialog.process.waitForFinished(3000)
        dialog.running = False
        dialog.close()
        owner.close()
        app.processEvents()
