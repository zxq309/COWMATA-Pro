"""One-time-code login and shared administrator console."""
from PySide6.QtCore import QThread, Signal
from cowmata_security.qt_ui import LoginDialog as SharedLogin, AdminDialog
from cowmata_security.client import current_session
from .ledger_sync import account_request
from . import ledger_session

class AccountWorker(QThread):
    success=Signal(dict);failed=Signal(str)
    def __init__(self,root,settings,action,values=None,parent=None):
        super().__init__(parent);self.root=root;self.settings=settings;self.action=action;self.values=dict(values or {})
    def run(self):
        try:self.success.emit(account_request(self.root,self.settings,self.action,**self.values))
        except Exception as error:self.failed.emit(str(error))
        finally:self.values.clear()

class LoginDialog(SharedLogin):
    def __init__(self,root,settings,parent=None,username=None):
        super().__init__(root,'ledger',parent,username or '');self.reply=None
    def logged_in(self,result):
        super().logged_in(result);self.reply=ledger_session.current()

class AccountsDialog(AdminDialog):
    def __init__(self,root,settings,parent=None):super().__init__(parent)

