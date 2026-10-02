"""上传: the field-ledger uploader running inside the Annotator.

The uploader's own window and logic are reused unchanged; only the shell differs:
one login (the Annotator's session signs uploads), the Annotator's bundled SSH
client and pinned host key, no tray icon or self-updater, and closing the window
never quits the application.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer

APP_ROOT = Path(__file__).resolve().parents[2]
SHEETS = ("样本试验台账", "产犊登记", "设备台账", "待产犊")


def configure():
    """Idempotent: route the vendored uploader through the Annotator's transport and session."""
    from cowmata_tailring.edge_download.site_records import default_key

    from . import ledger_app, ledger_sync

    ledger_app.EMBEDDED = True
    ledger_sync.SECURITY_PRODUCT = "pro"

    def transport_paths(_root):
        return (APP_ROOT / "vendor/ledger-ssh/usr/bin/ssh.exe", Path(default_key()),
                APP_ROOT / "cowmata_tailring/edge_download/ledger_known_hosts")

    ledger_sync.transport_paths = transport_paths


def data_root(account):
    """Per-account local working copy of the uploader (上传 · 台账), separate from the standalone uploader's folder.

    4.4.6: kept in the site tree, ``<目录树>\\1_下载器\\<牧场>\\台账\\上传\\<账号>``, next to the 台账 it uploads, so every
    business file lives in the one tree. A working copy an earlier version kept in the Windows profile is moved
    there once, unsynced edits included; without a tree the profile folder is used as before.
    """
    base = os.environ.get("COWMATA_LEDGER_HOME")
    if base:
        return Path(base) / account
    legacy = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "COWMATA Annotator" / "ledger" / account
    from cowmata_tailring.edge_download.paths import UPLOAD_FOLDER, site_ledger

    ledger = site_ledger()
    if ledger is None:
        return legacy
    target = ledger / UPLOAD_FOLDER / account
    if legacy.is_dir() and not target.exists():
        from cowmata_tailring.edge_download.site_adopt import move_tree

        try:
            move_tree(legacy, target)
        except OSError:
            return legacy  # never lose unsynced edits: keep working where they are and retry next time
    return target


def _window_class():
    from . import ledger_session
    from .ledger_app import MainWindow
    from cowmata_tailring.workspace.theme import SEGMENTED

    class LedgerWindow(MainWindow):
        def __init__(self, host, store, account):
            super().__init__(store, network=True, account=account)
            self.host = host
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self.setWindowTitle("上传 · 台账")
            self.resize(1360, 820)
            # Account, updates and branding belong to the Annotator shell.
            for widget in (self.account_button, self.update_button, self.update_status):
                widget.hide()
            identity = self.brand_icon.parentWidget()
            if identity is not None and identity is not self:
                identity.hide()
            for widget, text in ((self.import_button, "导入"), (self.new_button, "新建"), (self.report_button, "统计"),
                                 (self.refresh_button, "刷新"), (self.sync_button, "同步")):
                widget.setText(text)
                widget.setFixedHeight(32)
            self.import_button.setMinimumWidth(96)
            self.sync_button.setMinimumWidth(96)
            self.search.setFixedHeight(32)
            self.search.setPlaceholderText("搜索")
            self.sheet_tabs.setStyleSheet(SEGMENTED)
            self.operation_label.setText("就绪")
            # The uploader's 28 px glyph buttons (☰ ⚙ …) need zero padding under the Annotator theme.
            from PySide6.QtWidgets import QToolButton
            for button in self.findChildren(QToolButton):
                if button.maximumWidth() <= 32:
                    button.setStyleSheet("QToolButton {padding:0; border-radius:8px; font-size:15px;}")

        def lock_access(self, message):
            if self._auth_locked:
                return
            self.cancel_local_work()
            self.review_panel.hide()
            self._auth_locked = True
            self._refresh_batch = None
            self.timer.stop()
            self.auth_timer.stop()
            QTimer.singleShot(0, self._resume_after_login)

        def _resume_after_login(self):
            if self._local_busy:
                QTimer.singleShot(100, self._resume_after_login)
                return
            controller = getattr(self.host.main, "_security_controller", None)
            if controller is not None and not controller.locked and not ledger_session.active():
                controller.lock()  # Modal re-login of the Annotator; quits the app if declined.
            if ledger_session.active():
                self._auth_locked = False
                self.next_retry = 0
                self.timer.start(1000)
                self.auth_timer.start(1000)
                QTimer.singleShot(250, lambda: self.refresh_server(automatic=True))
            elif controller is not None and not getattr(controller, "shutting_down", False):
                QTimer.singleShot(500, self._resume_after_login)

        def relogin(self, message):
            self._resume_after_login()

    return LedgerWindow


class LedgerHost(QObject):
    """Owns at most one ledger window; menu actions open it and forward to its commands."""

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.view = None

    def open(self):
        from cowmata_security.client import AccessDenied, current_session
        from cowmata_security.qt_ui import guard_action

        if self.view is not None:
            self.view.show()
            self.view.raise_()
            self.view.activateWindow()
            return self.view
        if not guard_action(self.main, "upload"):
            return None
        try:
            identity = current_session().identity
        except AccessDenied:
            return None
        configure()
        from .ledger_core import DEFAULTS, Store

        account = {"username": identity["account"], "display_name": identity["account"], "role": identity["role"]}
        root = data_root(account["username"])
        root.mkdir(parents=True, exist_ok=True)
        store = Store(root)
        store.configure({key: DEFAULTS[key] for key in ("host", "port", "user", "server_file")})
        self.view = _window_class()(self, store, account)
        self.view.destroyed.connect(self._released)
        self.view.show()
        return self.view

    def _released(self, *_):
        self.view = None

    def run(self, command, *args):
        view = self.open()
        if view is not None and not view._local_busy:
            getattr(view, command)(*args)

    def show_sheet(self, index):
        view = self.open()
        if view is not None:
            view.sheet_tabs.setCurrentIndex(index)

    def close(self):
        """True when no window remains; a running sync keeps it open and returns False."""
        return self.view is None or self.view.close()
