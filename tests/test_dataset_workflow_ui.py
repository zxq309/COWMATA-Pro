def test_legacy_preview_and_export_are_separate_actions(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication, QWidget

    from cowmata_tailring.workspace.dataset_workflow_ui import DatasetWorkflowWindow
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = DatasetWorkflowWindow(owner)
    jobs = []
    monkeypatch.setattr(dialog,'start_job',jobs.append)
    dialog.label_sources.setPlainText(str(tmp_path/'labels'))
    dialog.raw_sources.setPlainText(str(tmp_path/'raw'))
    dialog.target.setText(str(tmp_path/'resources'))
    dialog.category.setCurrentIndex(dialog.category.findData('pregnancy_late'))
    dialog.preview_migration()
    assert jobs[-1]['action'] == 'legacy_preview'
    assert jobs[-1]['category'] == 'pregnancy_late'
    assert jobs[-1]['identity_policy'] == 'review'
    assert jobs[-1]['target'].endswith('怀孕\\孕晚期') or jobs[-1]['target'].endswith('怀孕/孕晚期')
    dialog.dataset_sources.setPlainText(str(tmp_path/'project'))
    dialog.dataset_target.setText(str(tmp_path/'new-dataset'))
    dialog.export_data()
    assert jobs[-1]['action'] == 'dataset_export'
    assert '未知' in dialog.dataset_help.text()
    dialog.close()
    owner.close()
    app.processEvents()


def test_actual_background_preview_returns_to_ready(tmp_path, monkeypatch):
    import time

    from PySide6.QtWidgets import QApplication, QWidget
    from test_legacy_migration import sources

    from cowmata_tailring.workspace.dataset_workflow_ui import DatasetWorkflowWindow
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    raw,label = sources(tmp_path)
    owner = QWidget()
    dialog = DatasetWorkflowWindow(owner)
    dialog.label_sources.setPlainText(str(label))
    dialog.raw_sources.setPlainText(str(raw))
    dialog.target.setText(str(tmp_path/'output'))
    dialog.category.setCurrentIndex(dialog.category.findData('pregnancy_late'))
    dialog.preview_migration()
    until = time.monotonic()+12
    while dialog.running and time.monotonic()<until:
        app.processEvents()
        time.sleep(.01)
    try:
        assert not dialog.running
        assert dialog.plan_path and dialog.execute.isEnabled()
    finally:
        if dialog.process and dialog.process.state():
            dialog.process.kill()
            dialog.process.waitForFinished(3000)
        dialog.running = False
        dialog.close()
        owner.close()
