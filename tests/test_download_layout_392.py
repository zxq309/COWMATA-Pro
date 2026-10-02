from pathlib import Path

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
        for col in range(table.columnCount()):
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
    # 4.4.6: the 台账 of the tree's farm (no tree yet: one starts beside the app in test mode).
    assert Path(store.value["ledger_directory"]) == tmp_path / "1_下载器" / "扬大_高邮牧场" / "台账"
    custom = tmp_path / "custom-records"
    store.save(ledger_directory=str(custom))
    assert ProSettings(tmp_path / "settings", app_root=root).value["ledger_directory"] == str(custom)


def test_one_button_and_short_interval(tmp_path, qt_application):
    dialog = make_dialog(tmp_path)
    try:
        assert dialog.interval.value() >= 1 and dialog.interval.suffix() == " 分钟"
        assert [dialog.plan_table.horizontalHeaderItem(i).text() for i in range(dialog.plan_table.columnCount())] == [
            "状态", "牛号", "设备号", "分类", "佩戴", "文件"]
        assert not hasattr(dialog, "end_at") and not hasattr(dialog, "day_table")
    finally:
        dialog.deleteLater()


