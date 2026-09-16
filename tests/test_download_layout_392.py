from PySide6.QtWidgets import QTableWidgetItem
from test_download_repair_391 import make_dialog


def test_configuration_cancel_restores_rules_and_apply_never_starts(tmp_path, qt_application):
    dialog = make_dialog(tmp_path)
    try:
        original = dialog.directory.text()
        dialog.open_configuration()
        dialog.directory.setText(str(tmp_path / "changed"))
        dialog.config_dialog.reject()
        assert dialog.directory.text() == original
        dialog.open_configuration()
        changed = str(tmp_path / "new-data")
        dialog.directory.setText(changed)
        dialog.apply_configuration()
        assert dialog.store.value["data_root"] == changed
        assert not dialog.config_dialog.isVisible()
        assert not dialog.running and not dialog.timer.isActive()
    finally:
        dialog.deleteLater()


def test_long_plan_scrolls_without_compressing_headers(tmp_path, qt_application):
    dialog = make_dialog(tmp_path)
    try:
        dialog.show()
        table = dialog.plan_table
        table.setRowCount(200)
        for row in range(200):
            table.setItem(row, 0, QTableWidgetItem("546C50CA07FA"))
        dialog.resize(800, 500)
        qt_application.processEvents()
        table.fit_columns()
        assert table.verticalScrollBar().maximum() > 0
        metrics = table.horizontalHeader().fontMetrics()
        for col in range(7):
            assert table.columnWidth(col) >= metrics.horizontalAdvance(table.horizontalHeaderItem(col).text()) + 32
        table.scrollToBottom()
        assert table.verticalScrollBar().value() == table.verticalScrollBar().maximum()
        assert not dialog.log.isVisible() and not dialog.config_dialog.isVisible()
    finally:
        dialog.deleteLater()


def test_existing_csv_detected_only_for_missing_legacy_default(tmp_path, monkeypatch):
    import cowmata_tailring.edge_download.pro_settings as module
    from cowmata_tailring.edge_download.site_records import SCHEMAS

    legacy = tmp_path / "old-default"
    existing = tmp_path / "existing-records"
    existing.mkdir()
    for schema in SCHEMAS.values():
        (existing / schema["filename"]).write_text("test", encoding="utf-8")
    monkeypatch.setattr(module, "LOCAL_DIRECTORY", str(legacy))
    monkeypatch.setattr(module, "SERVER_DIRECTORY", str(existing))
    defaults = module.settings_defaults()
    monkeypatch.setattr(module, "settings_defaults", lambda: {**defaults, "ledger_directory": str(legacy)})
    store = module.ProSettings(tmp_path / "settings")
    assert store.value["ledger_directory"] == str(existing)
    custom = tmp_path / "custom-records"
    store.save(ledger_directory=str(custom))
    assert module.ProSettings(tmp_path / "settings").value["ledger_directory"] == str(custom)
    legacy.mkdir()
    assert module.ProSettings(tmp_path / "other-settings").value["ledger_directory"] == str(legacy)


def test_default_schedule_is_future_beijing_time_on_every_host(tmp_path, qt_application):
    from datetime import datetime

    from cowmata_tailring.edge_download.core import CHINA

    dialog = make_dialog(tmp_path)
    try:
        delay = (dialog._field_time(dialog.scheduled_at) - datetime.now(CHINA)).total_seconds()
        assert 3590 < delay <= 3600
        assert abs((dialog._field_time(dialog.end_at) - datetime.now(CHINA)).total_seconds()) < 10
    finally:
        dialog.deleteLater()
