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


def test_default_cache_is_installation_relative_and_custom_directory_is_preserved(tmp_path, monkeypatch):
    from cowmata_tailring.edge_download.pro_settings import ProSettings
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    root = tmp_path / "app"
    store = ProSettings(tmp_path / "settings", app_root=root)
    assert store.display_path(store.value["ledger_directory"]) == "../COWMATA Pro 数据/现场台账"
    custom = tmp_path / "custom-records"
    store.save(ledger_directory=str(custom))
    assert ProSettings(tmp_path / "settings", app_root=root).value["ledger_directory"] == str(custom)


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
