def test_dataset_tabs_send_independent_requests(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication, QWidget

    from cowmata_tailring.workspace.dataset_build_ui import DatasetBuildWindow

    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = DatasetBuildWindow(owner)
    jobs = []
    monkeypatch.setattr(dialog, "start_job", jobs.append)
    dialog.sources.setPlainText(str(tmp_path / "project"))
    dialog.submit("dataset_audit")
    assert jobs[-1]["action"] == "dataset_audit"
    dialog.target.setText(str(tmp_path / "dataset"))
    dialog.behavior_checks["MOUNTING"].setChecked(False)
    dialog.submit("behavior_build")
    assert jobs[-1]["action"] == "behavior_build" and "MOUNTING" not in jobs[-1]["behaviors"]
    dialog.submit("decision_build")
    assert jobs[-1]["action"] == "decision_build"
    assert dialog.tabs.count() == 4
    assert not hasattr(dialog, "label_sources")
    dialog.close()
    owner.close()
    app.processEvents()


def test_background_audit_returns_to_ready(tmp_path, monkeypatch):
    import time

    from PySide6.QtWidgets import QApplication, QWidget
    from test_legacy_migration import sources

    from cowmata_tailring.workspace.dataset_build_ui import DatasetBuildWindow
    from cowmata_tailring.workspace.legacy_migration import execute_migration, plan_migration

    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("COWMATA_ACCESS_DIR", str(tmp_path / "access"))
    raw, label = sources(tmp_path)
    project = tmp_path / "project"
    execute_migration(plan_migration([label], [raw], project, category="pregnancy_late"))
    owner = QWidget()
    dialog = DatasetBuildWindow(owner)
    dialog.sources.setPlainText(str(project))
    dialog.submit("dataset_audit")
    stop = time.monotonic() + 20
    while dialog.running and time.monotonic() < stop:
        app.processEvents()
        time.sleep(0.01)
    try:
        assert not dialog.running
        assert "处理完成" in dialog.status.text()
        assert "事件：1" in dialog.details.toPlainText()
    finally:
        if dialog.process and dialog.process.state():
            dialog.process.kill()
            dialog.process.waitForFinished(3000)
        dialog.running = False
        dialog.close()
        owner.close()
        app.processEvents()
