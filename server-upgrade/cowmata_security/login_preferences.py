"""Explicit login preferences protected by Windows DPAPI, separate from activation."""
from pathlib import Path
from .device_store import DeviceStore

DEFAULTS={'account':'','role':'admin','password':'','remember_password':False,'auto_login':False}
class LoginPreferences:
    def __init__(self,product,path=None):
        target=Path(path) if path else DeviceStore(product).path.with_name('login-preferences.bin')
        self.storage=DeviceStore(product,target);self.path=target
    def load(self):
        value=self.storage.load()
        if value is None:return dict(DEFAULTS)
        return self.clean(value)
    def clean(self,value):
        if not isinstance(value,dict):raise ValueError('登录设置无效')
        account=value.get('account','');password=value.get('password','');role=value.get('role','admin')
        if not isinstance(account,str) or len(account)>32 or not isinstance(password,str) or len(password)>1024 or role not in ('admin','operator'):raise ValueError('登录设置无效')
        remember=value.get('remember_password') is True
        automatic=remember and value.get('auto_login') is True
        return {'account':account,'role':role,'password':password if remember else '', 'remember_password':remember,'auto_login':automatic}
    def save(self,value):self.storage.save(self.clean(value))
    def delete(self):self.storage.delete()
    def disable_auto_login(self):
        value=self.load();value['auto_login']=False;self.save(value)
