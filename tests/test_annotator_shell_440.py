"""4.4.0 shell: seven menus, every 4.3.9 command re-homed, uploader embedded without owning the app."""
import uuid

import pytest
from PySide6.QtWidgets import QApplication

from cowmata_tailring.workspace.modern_window import MainWindow

MENUS = ["文件", "编辑", "上传", "下载", "标注", "工具", "帮助"]


@pytest.fixture()
def window():
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    yield w
    w.close()
    app.processEvents()


def test_every_command_has_one_home(window):
    assert [a.text() for a in window.menuBar().actions()] == MENUS
    assert window._menu_unplaced == []
    inventory = window.menu_inventory()
    texts = [text for _, text in inventory]
    assert len(texts) == len(set(texts)), sorted(t for t in texts if texts.count(t) > 1)
    assert all(len(path) <= 2 for path, _ in inventory)
    upload = [text for path, text in inventory if path == ("上传",)]
    assert {"台账", "导入表格…", "同步修改", "刷新服务器", "新建记录…", "核对冲突…", "统计报告…", "导出 CSV…", "上传设置…"} <= set(upload)
    assert [text for path, text in inventory if path == ("上传", "表")] == ["样本试验台账", "产犊登记", "设备台账", "待产犊"]


def test_help_menu_holds_account(window):
    help_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "帮助")
    assert window.account_menu in [a.menu() for a in help_menu.actions()]


def test_visible_menu_text_is_short(window):
    for path, text in window.menu_inventory():
        assert len(text.rstrip("…")) <= 12, (path, text)


def sample(**values):
    from cowmata_tailring.ledger.ledger_core import FIELDS
    row = dict.fromkeys(FIELDS, "")
    row.update(记录ID=str(uuid.uuid4()), 版本="", 已删除="0", 牧场="扬大_高邮牧场", 牛号="24242R6", 设备号="0C3D5EA22F07",
               佩戴开始="2026-09-27 17:00", 监测目的="产犊监测", 记录日期="2026-09-27", 数据分类="calving",
               九轴="有效", 脉搏="有效", 温度="有效")
    row.update(values)
    return row


def test_embedded_uploader_shows_pending_sheet_and_never_quits(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    from cowmata_tailring.ledger import host, ledger_app, ledger_sync
    import cowmata_security.client as client
    import cowmata_security.qt_ui as qt_ui

    monkeypatch.setenv("COWMATA_LEDGER_HOME", str(tmp_path))
    monkeypatch.setattr(ledger_app, "EMBEDDED", False)
    monkeypatch.setattr(ledger_sync, "SECURITY_PRODUCT", "ledger")
    monkeypatch.setattr(ledger_sync, "transport_paths", ledger_sync.transport_paths)
    monkeypatch.setattr(qt_ui, "guard_action", lambda parent, cap: True)
    monkeypatch.setattr(client, "current_session",
                        lambda: type("S", (), {"identity": {"account": "zhangxiangqing", "role": "operator"}})())
    monkeypatch.setattr(ledger_app.MainWindow, "refresh_server", lambda self, *a, **k: None)
    quits = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(1)))

    class Main:
        _security_controller = None
    shell = host.LedgerHost.__new__(host.LedgerHost)
    host.QObject.__init__(shell)
    shell.main, shell.view = Main(), None
    view = shell.open()
    try:
        assert ledger_app.EMBEDDED and ledger_sync.SECURITY_PRODUCT == "pro"
        assert (tmp_path / "zhangxiangqing").is_dir()
        assert [view.sheet_tabs.tabText(i).split()[0] for i in range(view.sheet_tabs.count())] == list(host.SHEETS)
        store = view.stores["samples"]
        store.save(sample())
        store.save(sample(牛号="24243R6", 产犊开始="2026-09-28 10:00", 产犊结束="2026-09-28 10:40"))
        view.update_sheet_tabs()
        assert view.sheet_tabs.tabText(3).split() == ["待产犊", "1"]
        shell.show_sheet(3)
        assert [r["牛号"] for r in view.visible_rows()] == ["24242R6"]
        assert view.account_button.isHidden() and view.update_button.isHidden()
        assert shell.open() is view
    finally:
        assert shell.close()
        app.processEvents()
    assert quits == []

def test_closing_the_annotator_closes_the_ledger_window_first():
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    calls = []

    class Busy:
        def close(self):
            calls.append(1)
            return len(calls) > 1  # first call: sync still running
    w._ledger_host = Busy()
    retries = []
    w._retry_close = lambda ms: retries.append(ms)
    w.close()
    assert calls == [1] and retries == [300]
    w.close()
    app.processEvents()
    assert len(calls) == 2
