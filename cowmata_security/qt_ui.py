"""Shared role-specific login, administrator table and safe session shutdown."""
from pathlib import Path
from PySide6.QtCore import QThread, QTimer, Signal, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QFormLayout,
    QLineEdit, QLabel, QPushButton, QMessageBox, QTableWidget, QTableWidgetItem,
    QHBoxLayout, QSpinBox, QHeaderView, QAbstractItemView, QComboBox, QCheckBox)
from .client import Session, AccessDenied, read_config, install_session, current_session

class Task(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    def __init__(self, call, parent=None):
        super().__init__(parent); self.call = call
    def run(self):
        try: self.succeeded.emit(self.call())
        except Exception as exc: self.failed.emit(str(exc))
        finally: self.call = None

class LoginDialog(QDialog):
    def __init__(self, root, product, parent=None, username='', *, preferences=None):
        super().__init__(parent)
        from .login_preferences import LoginPreferences, DEFAULTS
        self.root = Path(root); self.product = product; self.locked_account = username
        self.session = None; self.task = None; self.ready = False; self.pending_login = None
        self.preferences = preferences if preferences is not None else LoginPreferences(product)
        self.saved = dict(DEFAULTS); preference_error = ''
        try: self.saved = self.preferences.load()
        except Exception: preference_error = '无法读取本机保存的登录信息，请手动输入。'
        self.setWindowTitle('COWMATA · 登录'); self.setMinimumWidth(460)
        self.setWindowModality(Qt.ApplicationModal)
        layout = QVBoxLayout(self); form = self.form = QFormLayout()
        self.mode = QComboBox(); self.mode.addItem('管理员', 'admin'); self.mode.addItem('操作员', 'operator')
        self.mode.setCurrentIndex(1 if self.saved['role'] == 'operator' else 0)
        form.addRow('登录身份', self.mode)
        self.account = QLineEdit(username or self.saved['account']); self.account.setReadOnly(bool(username))
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.Password)
        if self.saved['remember_password'] and (not username or username == self.saved['account']):self.password.setText(self.saved['password'])
        self.code = QLineEdit(); self.code.setEchoMode(QLineEdit.Password)
        self.code.setPlaceholderText('首次激活填写，已激活电脑可留空')
        form.addRow('账号', self.account); form.addRow('密码', self.password); form.addRow('授权码', self.code)
        layout.addLayout(form)
        choices = QHBoxLayout()
        self.remember = QCheckBox('保存密码'); self.remember.setChecked(self.saved['remember_password'])
        self.automatic = QCheckBox('自动登录'); self.automatic.setChecked(self.saved['auto_login'])
        self.remember.setToolTip('仅在当前 Windows 用户下加密保存；不勾选自动登录时仍需点击登录。')
        self.automatic.setToolTip('仅主动勾选后，下次打开才自动登录；同时保存密码。')
        self.forgot = QPushButton('忘记密码'); self.forgot.setFlat(True)
        choices.addWidget(self.remember); choices.addWidget(self.automatic); choices.addStretch(); choices.addWidget(self.forgot)
        layout.addLayout(choices)
        self.hint = QLabel(); self.hint.setWordWrap(True); layout.addWidget(self.hint)
        self.button = QPushButton('登录'); self.button.clicked.connect(self.submit)
        self.password.returnPressed.connect(self.submit); self.code.returnPressed.connect(self.submit)
        layout.addWidget(self.button)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.remember.toggled.connect(self.remember_changed); self.automatic.toggled.connect(self.automatic_changed)
        self.forgot.clicked.connect(self.password_help)
        self.update_mode()
        if preference_error:self.hint.setText(preference_error)
        QTimer.singleShot(0, self.auto_login)
    def update_mode(self):
        self.form.setRowVisible(self.code, self.mode.currentData() == 'operator')
        self.hint.setText(self.login_hint()); self.adjustSize()
    def mode_changed(self):
        self.password.clear(); self.code.clear(); self.automatic.setChecked(False); self.update_mode()
    def login_hint(self):
        return ('管理员只需账号和密码，可在其他电脑使用。自动登录须主动勾选。' if self.mode.currentData() == 'admin'
                else '首次使用需管理员下发的授权码；已激活电脑可留空。自动登录须主动勾选。')
    def remember_changed(self, checked):
        if not checked:
            self.automatic.setChecked(False)
            self.saved.update(password='', remember_password=False, auto_login=False)
            self.persist_disabled_options()
    def automatic_changed(self, checked):
        if checked:self.remember.setChecked(True)
        else:
            self.saved['auto_login'] = False; self.persist_disabled_options()
    def persist_disabled_options(self):
        try:self.preferences.save(self.saved)
        except Exception:self.hint.setText('登录设置保存失败；请检查本机用户目录权限。')
    def make_session(self):
        from .device_store import DeviceStore
        return Session(read_config(self.root), self.product, device_store=DeviceStore(self.product))
    def set_busy(self, busy):
        for field in (self.mode, self.account, self.password, self.code, self.remember, self.automatic, self.forgot, self.button):field.setEnabled(not busy)
    def auto_login(self):
        # A legacy device.bin alone is never consent for automatic sign-in.
        if self.locked_account or not self.automatic.isChecked() or not self.saved['auto_login']:return
        if self.account.text().strip() != self.saved['account'] or self.mode.currentData() != self.saved['role']:return
        if self.task and self.task.isRunning():return
        self.submit()
    def submit(self):
        if self.task and self.task.isRunning():return
        account = self.account.text().strip(); password = self.password.text(); code = self.code.text().strip()
        administrator = self.mode.currentData() == 'admin'
        if not account or not password:
            self.hint.setText('请填写账号和密码。'); return
        self.pending_login = {'account':account, 'password':password, 'role':self.mode.currentData(),
                              'remember_password':self.remember.isChecked(), 'auto_login':self.automatic.isChecked()}
        self.password.clear(); self.code.clear()
        try:self.session = self.make_session()
        except Exception as exc:
            self.pending_login = None; self.hint.setText(str(exc)); return
        self.set_busy(True); self.hint.setText('正在登录…')
        automatic = self.automatic.isChecked()
        call = (lambda:self.session.login_admin(account, password, remember_device=automatic)) if administrator else (lambda:self.session.login(account, code, password=password))
        self.task = Task(call, self); self.task.succeeded.connect(self.logged_in)
        self.task.failed.connect(self.login_failed); self.task.finished.connect(self.finished_request); self.task.start()
    def login_failed(self, message):
        self.pending_login = None; self.automatic.setChecked(False); self.hint.setText(message)
    def logged_in(self, result):
        if self.pending_login is not None:
            try:self.preferences.save(self.pending_login)
            except Exception:QMessageBox.warning(self, '登录设置', '登录成功，但本机无法保存登录设置；下次需要手动登录。')
        self.pending_login = None; install_session(self.session); self.ready = True
    def finished_request(self):
        self.pending_login = None; self.set_busy(False)
        if self.ready:self.accept()
    def password_help(self):
        dialog = QDialog(self); dialog.setWindowTitle('忘记密码'); dialog.setMinimumWidth(440)
        layout = QVBoxLayout(dialog)
        if self.mode.currentData() == 'admin':
            explanation = '管理员密码由你在管理电脑的账号表中维护，可在该表查询或修改。其他电脑请联系账号提供者。'
        else:explanation = '请联系给你发放账号的管理员查询或重设密码。此处不会绕过账号验证。'
        label = QLabel(explanation); label.setWordWrap(True); layout.addWidget(label)
        directory = {'pro':'Pro', 'ledger':'Uploader'}.get(self.product)
        registry = Path('E:/COWMATA-Security') / (directory or '') / 'authority.csv'
        if self.mode.currentData() == 'admin' and directory and registry.is_file():
            location = QLabel(str(registry)); location.setWordWrap(True); layout.addWidget(location)
            open_button = QPushButton('打开本机账号表')
            def open_registry():
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(registry))):label.setText('账号表无法打开，请在管理员电脑手动打开上述位置。')
            open_button.clicked.connect(open_registry); layout.addWidget(open_button)
        close_button = QPushButton('返回登录'); close_button.clicked.connect(dialog.accept); layout.addWidget(close_button)
        self.recovery_dialog = dialog; dialog.open()
    def reject(self):
        if self.task and self.task.isRunning():return
        self.pending_login = None; self.password.clear(); self.code.clear(); super().reject()

def authenticate(root, product, parent=None, username=''):
    dialog = LoginDialog(root, product, parent, username)
    if dialog.exec() != QDialog.Accepted: return None
    return dialog.session

class AdminDialog(QDialog):
    def __init__(self, parent=None, session=None):
        super().__init__(parent)
        self.session = session or current_session(); self.session.require('accounts')
        self.task = None; self.need_reload = False
        self.setWindowTitle('管理员 · 账号管理'); self.resize(860, 500)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['账号', '密码', '授权码'])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table)
        row = QHBoxLayout(); row.addWidget(QLabel('生成数量'))
        self.count = QSpinBox(); self.count.setRange(1, 100); self.count.setValue(10)
        row.addWidget(self.count)
        self.buttons = []
        for title, call in [('批量生成', self.batch_create), ('复制所选', self.copy_selected),
                            ('删除所选', self.remove_selected), ('打开本机 CSV', self.open_csv), ('刷新', self.reload)]:
            button = QPushButton(title); button.clicked.connect(call)
            row.addWidget(button); self.buttons.append(button)
        layout.addLayout(row)
        self.hint = QLabel('先批量生成，需要时复制所选账号发给使用者。删除账号即撤销授权。')
        self.hint.setWordWrap(True); layout.addWidget(self.hint)
        self.reload()
    def selected_rows(self):
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})
    def selection_changed(self):
        if not hasattr(self, 'buttons') or len(self.buttons) < 3: return
        busy = bool(self.task and self.task.isRunning())
        rows = self.selected_rows()
        self.buttons[1].setEnabled(not busy and bool(rows))
        own = self.session.identity.get('account')
        can_remove = len(rows) == 1 and self.table.item(rows[0], 0) is not None and self.table.item(rows[0], 0).text() != own
        self.buttons[2].setEnabled(not busy and can_remove)
    def invoke(self, action, **values):
        if self.task and self.task.isRunning(): return
        try: self.session.require('accounts')
        except AccessDenied as exc: self.hint.setText(str(exc)); return
        for button in self.buttons: button.setEnabled(False)
        self.count.setEnabled(False); self.hint.setText('正在处理…')
        self.task = Task(lambda: self.session.admin(action, **values), self)
        self.task.succeeded.connect(self.result); self.task.failed.connect(self.hint.setText)
        self.task.finished.connect(self.finished_request); self.task.start()
    def finished_request(self):
        for button in self.buttons: button.setEnabled(True)
        self.count.setEnabled(True); self.selection_changed()
        if self.need_reload:
            self.need_reload = False; QTimer.singleShot(0, self.reload)
    def reload(self): self.invoke('credentials')
    def batch_create(self): self.invoke('batch_create', count=self.count.value())
    def result(self, result):
        if 'credentials' not in result:
            self.need_reload = True; return
        rows = result['credentials']
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, key in enumerate(('account', 'password', 'code')):
                item = QTableWidgetItem(str(row[key]))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(i, j, item)
        self.hint.setText('共 '+str(len(rows))+' 个账号。管理员无需授权码；操作员首次激活后绑定设备。')
    def copy_selected(self):
        try: self.session.require('accounts')
        except AccessDenied as exc: self.hint.setText(str(exc)); return
        rows = self.selected_rows()
        if not rows: self.hint.setText('请先选择要复制的账号。'); return
        text = '账号\t密码\t授权码\n' + '\n'.join('\t'.join(self.table.item(row, column).text() for column in range(3)) for row in rows)
        QApplication.clipboard().setText(text)
        self.hint.setText('已复制所选 '+str(len(rows))+' 个账号。请仅交给对应使用者。')
    def remove_selected(self):
        rows = self.selected_rows()
        if len(rows) != 1: self.hint.setText('请一次选择一个要删除的账号。'); return
        account = self.table.item(rows[0], 0).text()
        if account == self.session.identity.get('account'):
            self.hint.setText('不能删除唯一管理员。'); return
        answer = QMessageBox.question(self, '删除账号', '删除 '+account+' 后，该账号将失去授权。是否删除？', QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes: self.invoke('remove_account', account=account)
    def open_csv(self):
        try: self.session.require('accounts')
        except AccessDenied as exc: self.hint.setText(str(exc)); return
        directory = {'pro': 'Pro', 'ledger': 'Uploader'}.get(self.session.product)
        path = Path('E:/COWMATA-Security') / (directory or '') / 'authority.csv'
        if not directory or not path.is_file():
            self.hint.setText('此电脑没有管理员维护表；请在管理员本机打开 CSV。'); return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.hint.setText('无法打开 CSV：'+str(path))
    def reject(self):
        if self.task and self.task.isRunning(): return
        self.table.clearContents(); self.table.setRowCount(0); super().reject()

def guard_action(parent,capability):
    try:current_session().require(capability);return True
    except AccessDenied as exc:
        QMessageBox.warning(parent,'权限限制',str(exc));return False

class SessionController:
    def __init__(self,window,root,product):
        self.window=window;self.root=root;self.product=product;self.worker=None;self.locked=False
        self.timer=QTimer(window);self.timer.setInterval(1000);self.timer.timeout.connect(self.tick);self.timer.start()
        menu=window.menuBar().addMenu('账号')
        session=current_session()
        if session.allows('accounts'):
            menu.addAction('账号管理…',lambda:AdminDialog(window).exec())
        menu.addAction('锁定当前账号',self.lock)
        menu.addAction('退出登录',self.logout)
        menu.addAction('清除此设备自动登录',self.forget)
        self.apply_role()
    def apply_role(self):
        session=current_session();role=session.identity.get('role')
        self.window.setWindowTitle(self.window.windowTitle().split(' · 账号:')[0]+' · 账号:'+session.identity.get('account','')+'（'+('管理员' if role=='admin' else '操作员')+'）')
        def visit(menu,inherited=None):
            for action in menu.actions():
                title=action.text().replace('&','')
                cap=inherited
                if any(x in title for x in ('行为识别','训练与识别','模型库','算法管理','逐项算法','自动生成候选')):cap='behavior'
                if any(x in title for x in ('健康与繁殖','繁殖与健康','产犊预测','发情预测','怀孕监测','疫病监测')):cap='health'
                if '数据集构建' in title:cap='dataset'
                if cap:
                    action.setEnabled(session.allows(cap))
                    if not session.allows(cap):action.setToolTip('此功能仅管理员可用')
                if action.menu():visit(action.menu(),cap)
        visit(self.window.menuBar())
    def tick(self):
        if self.locked:return
        session=current_session()
        if not session.allows('annotate'):
            self.shutdown();return
        if session.clock()-session.last_refresh>=60 and not (self.worker and self.worker.isRunning()):
            self.worker=Task(session.refresh,self.window);self.worker.failed.connect(lambda _:self.shutdown());self.worker.start()
    def lock(self):
        if self.locked:return
        self.locked=True
        # Keep the event loop alive while the business window is hidden: closing
        # the login dialog must not bypass asynchronous closeEvent saves.
        app=QApplication.instance();self.previous_quit=app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        session=current_session();name=session.identity.get('account','');session.clear()
        visible=[w for w in QApplication.topLevelWidgets() if w.isVisible()]
        for w in visible:w.hide()
        fresh=authenticate(self.root,self.product,None,name)
        if fresh:
            self.locked=False;app.setQuitOnLastWindowClosed(self.previous_quit)
            self.apply_role()
            for w in visible:w.show()
            return
        self.shutdown()
    def forget(self):
        session=current_session()
        if session.device_store:session.device_store.delete()
        from .login_preferences import LoginPreferences
        LoginPreferences(self.product).delete()
        self.logout()
    def logout(self):
        from .login_preferences import LoginPreferences
        LoginPreferences(self.product).disable_auto_login()
        # Local access is locked immediately. The network logout may finish
        # asynchronously while the ordinary window close saves pending work.
        session=current_session();token=session.token
        session.clear()
        self.logout_worker=Task(lambda:session.transport({'action':'logout','session':token,'product':session.product}),self.window)
        self.logout_worker.start();self.shutdown()
    def shutdown(self):
        if getattr(self,'shutting_down',False):return
        self.shutting_down=True;self.locked=True;self.timer.stop()
        QApplication.instance().setQuitOnLastWindowClosed(False)
        self.window._security_shutdown=True
        self.window._close_choice='save'
        self.pending_windows=[w for w in QApplication.topLevelWidgets() if w is not self.window]
        for w in self.pending_windows+[self.window]:w.hide()
        self.progress=QDialog();self.progress.setWindowTitle('COWMATA · 正在保存并安全退出')
        self.progress.setWindowFlags(self.progress.windowFlags() & ~Qt.WindowCloseButtonHint)
        self.progress.reject=lambda:None
        layout=QVBoxLayout(self.progress)
        self.close_hint=QLabel('正在停止后台任务并保存未完成工作。保存失败时将保持锁定，请勿强制结束进程。')
        self.close_hint.setWordWrap(True);layout.addWidget(self.close_hint);self.progress.show()
        self.close_timer=QTimer(self.progress);self.close_timer.setInterval(300)
        self.close_timer.timeout.connect(self.finish_shutdown);self.close_timer.start()
        self.main_closed=False;self.finish_shutdown()
    def finish_shutdown(self):
        if getattr(self,'closing_attempt',False):return
        self.closing_attempt=True
        try:
            if not self.main_closed:
                self.window._close_choice='save'
                self.main_closed=self.window.close()
                if not self.main_closed:return
            remaining=[]
            for window in self.pending_windows:
                try:
                    if not window.close():remaining.append(window)
                except RuntimeError:pass  # Its parent already disposed it.
            self.pending_windows=remaining
            if remaining:return
            for worker in (self.worker,getattr(self,'logout_worker',None)):
                if worker and worker.isRunning():return
            self.close_timer.stop();self.progress.hide()
            QApplication.quit()
        except Exception as exc:
            self.close_hint.setText('安全退出尚未完成，工作仍保留在内存：'+str(exc))
        finally:self.closing_attempt=False

def install_window(window,root,product):
    controller=SessionController(window,root,product)
    window._security_controller=controller
    return controller
