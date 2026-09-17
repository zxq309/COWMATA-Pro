"""Local owner setup; recovery password never leaves the administrator computer."""
import ctypes
import json
import os
import shutil
import time
from pathlib import Path
from PySide6.QtWidgets import QDialog,QVBoxLayout,QFormLayout,QLineEdit,QLabel,QPushButton,QComboBox
from .authority import Authority
from .client import Session
from .protocol import dispatch

SERVICE=Path('E:/Services/CowmataLedgerUploadV2')
CSV_PATHS={'pro':Path('E:/COWMATA-Security/Pro/authority.csv'),'ledger':Path('E:/COWMATA-Security/Uploader/authority.csv')}

def available():
    return os.name=='nt' and bool(ctypes.windll.shell32.IsUserAnAdmin()) and (SERVICE/'receiver-config.json').is_file()

def atomic_json(path,value):
    import tempfile
    fd,name=tempfile.mkstemp(prefix='.config-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

def activate_service(root):
    if not available():raise PermissionError('只能在你的管理员电脑上启用授权中心')
    source=Path(root)/'server-upgrade/receiver.py'
    if not source.is_file():raise ValueError('缺少已验证的服务端升级文件')
    config=json.loads((SERVICE/'receiver-config.json').read_text(encoding='utf-8-sig'))
    backup=SERVICE/('security-code-backup-'+str(time.time_ns()));backup.mkdir()
    for name in ('receiver.py','receiver-config.json'):shutil.copy2(SERVICE/name,backup/name)
    shutil.copytree(Path(__file__).resolve().parent,SERVICE/'cowmata_security',dirs_exist_ok=True)
    target=SERVICE/'receiver.py';temporary=SERVICE/'receiver.security.new';shutil.copy2(source,temporary);os.replace(temporary,target)
    config['security_csv']={k:str(v) for k,v in CSV_PATHS.items()};config['security_required']=True
    atomic_json(SERVICE/'receiver-config.json',config)

class LocalCenter(QDialog):
    def __init__(self,root,parent=None):
        super().__init__(parent);self.root=root;self.setWindowTitle('唯一管理员 · 本机 CSV 授权中心');self.resize(650,430)
        layout=QVBoxLayout(self);notice=QLabel('授权记录只保存在本机指定 CSV。首次启用将切换服务器到一次性激活模式，旧密码登录会停止。恢复口令请离线保管。');notice.setWordWrap(True);layout.addWidget(notice)
        form=QFormLayout();self.account=QLineEdit('admin');self.password=QLineEdit();self.password.setEchoMode(QLineEdit.Password)
        self.confirm=QLineEdit();self.confirm.setEchoMode(QLineEdit.Password);self.product=QComboBox();self.product.addItem('COWMATA Pro','pro');self.product.addItem('上传器 1.3.4','ledger')
        for name,widget in [('唯一管理员账号',self.account),('管理员恢复口令（至少16位）',self.password),('首次设置时再输入一次',self.confirm),('管理哪个应用',self.product)]:form.addRow(name,widget)
        layout.addLayout(form)
        paths=QLabel('Pro: '+str(CSV_PATHS['pro'])+'\nUploader: '+str(CSV_PATHS['ledger']));paths.setWordWrap(True);layout.addWidget(paths)
        self.hint=QLabel();self.hint.setWordWrap(True);layout.addWidget(self.hint)
        for title,callback in [('首次初始化两个 CSV 并启用服务',self.initialize),('打开账号与设备授权管理',self.manage),('为本人生成一次性激活码',self.issue_owner)]:
            button=QPushButton(title);button.clicked.connect(callback);layout.addWidget(button)
    def owner_session(self):
        if not available():raise PermissionError('需要本机 Windows 管理员身份')
        product=self.product.currentData();authority=Authority(CSV_PATHS[product],product=product)
        code=authority.recover_grant(self.account.text().strip(),self.password.text(),product)['code']
        self.password.clear();self.confirm.clear()
        session=Session(lambda req:dispatch(authority,req),product)
        session.login(self.account.text().strip(),code)
        return session
    def initialize(self):
        try:
            if not available():raise PermissionError('需要本机 Windows 管理员身份')
            if any(p.exists() for p in CSV_PATHS.values()):raise ValueError('CSV 已存在，禁止覆盖。请使用下方管理按钮。')
            password=self.password.text()
            if password!=self.confirm.text():raise ValueError('两次恢复口令不一致')
            if len(password)<16:raise ValueError('恢复口令至少16位')
            import subprocess
            for product,path in CSV_PATHS.items():
                path.parent.mkdir(parents=True,exist_ok=True)
                # Restricted SSH user needs write access to consume grants. Others receive no access.
                result=subprocess.run(['icacls',str(path.parent),'/inheritance:r','/grant:r','*S-1-5-32-544:(OI)(CI)F','*S-1-5-18:(OI)(CI)F','cowmata_upload:(OI)(CI)M'],capture_output=True,creationflags=0x08000000)
                if result.returncode:raise PermissionError('无法设置授权目录权限，未初始化：'+str(path.parent))
                Authority(path,product=product).bootstrap(self.account.text().strip(),password)
            activate_service(self.root);self.hint.setText('本机服务已启用。请先为本人生成激活码，然后分配操作员账号。')
        except Exception as exc:self.hint.setText(str(exc))
        finally:self.password.clear();self.confirm.clear()
    def manage(self):
        try:
            from .qt_ui import AdminDialog
            session=self.owner_session();dialog=AdminDialog(self,session=session);dialog.exec();session.logout()
        except Exception as exc:self.hint.setText(str(exc))
    def issue_owner(self):
        try:
            session=self.owner_session();result=session.admin('issue',account=session.identity['account'],product=session.product)
            dialog=QDialog(self);dialog.setWindowTitle('管理员本人激活码 · 仅显示一次');layout=QVBoxLayout(dialog)
            field=QLineEdit(result['code']);field.setReadOnly(True);layout.addWidget(field)
            layout.addWidget(QLabel('10分钟内在登录窗口使用；只可激活一个设备。'))
            button=QPushButton('关闭');button.clicked.connect(dialog.accept);layout.addWidget(button)
            dialog.exec();field.clear();result.clear();session.logout()
        except Exception as exc:self.hint.setText(str(exc))
