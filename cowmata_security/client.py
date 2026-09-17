"""Memory-only sessions; fail closed after a monotonic offline lease."""
from __future__ import annotations
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

class AccessDenied(PermissionError):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AccessDenied('Authorization redirects are not allowed')

class HttpsTransport:
    def __init__(self, endpoint):
        url = urllib.parse.urlsplit(endpoint)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('A fixed HTTPS authorization endpoint is required')
        self.endpoint = endpoint.rstrip('/')+'/v1/auth'
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    def __call__(self, request):
        req = urllib.request.Request(self.endpoint, data=json.dumps(request).encode(),
            headers={'Content-Type':'application/json'}, method='POST')
        try:
            with self.opener.open(req, timeout=8) as response:
                data = response.read(4*1024*1024+1)
                if len(data)>4*1024*1024:
                    raise AccessDenied('Authorization response too large')
                result=json.loads(data)
        except urllib.error.HTTPError as exc:
            if exc.code in (400,401,403,409,429):
                raise AccessDenied('授权被拒绝：账号、登录码或会话无效') from None
            raise OSError('Authorization service unavailable') from None
        except urllib.error.URLError as exc:
            # Certificate/hostname failures are fatal; never grant offline grace for them.
            if isinstance(exc.reason, ssl.SSLError):
                raise AccessDenied('服务器证书验证失败') from None
            raise OSError('Authorization service unavailable') from None
        if not result.get('ok'):
            raise AccessDenied(result.get('error','授权失败'))
        return result['result']

class Session:
    def __init__(self, transport, product, *, monotonic=time.monotonic, device_store=None):
        if product not in ('pro','ledger'):
            raise ValueError('Unknown product')
        self.transport=transport
        self.product=product
        self.clock=monotonic
        self.token=''
        self.identity={}
        self.deadline=0
        self.last_refresh=0
        self.device_store=device_store
    def login(self, account, code, password=None):
        self.clear()
        import socket
        if not code and self.device_store:
            saved=self.device_store.load()
            if not saved:raise AccessDenied('此电脑尚未激活，请填写管理员下发的授权码')
            result=self.transport({'action':'password_device_login','account':account,'password':password,'device_secret':saved['device_secret'],'product':self.product})
        else:
            result=self.transport({'action':'redeem','account':account,'code':code,'product':self.product,'device_label':socket.gethostname(), **({'password':password} if password is not None else {})})
        return self._complete_login(result)
    def login_admin(self, account, password, *, remember_device=False):
        self.clear()
        import socket
        result=self.transport({'action':'admin_login','account':account,'password':password,'product':self.product,'device_label':socket.gethostname()})
        if result.get('role')!='admin' or result.get('account')!=account:
            raise AccessDenied('管理员身份验证失败')
        return self._complete_login(result, remember_admin=remember_device)
    def _complete_login(self, result, *, remember_admin=False):
        token=result.get('session')
        if not isinstance(token,str) or len(token)<32:
            raise AccessDenied('Invalid session response')
        self._accept(result)
        self.token=token
        if self.device_store:
            if result['role']=='admin' and not remember_admin:
                self.device_store.delete()
            elif result.get('device_secret'):
                self.device_store.save({'device_secret':result['device_secret'],'device_id':result['device_id'],'product':self.product})
        return dict(self.identity)
    def _accept(self, result):
        role=result.get('role')
        lease=result.get('lease_seconds')
        if result.get('product')!=self.product or role not in ('admin','operator') or type(lease) not in (float,int) or not 0<lease<=900 or not isinstance(result.get('account'),str):
            self.clear()
            raise AccessDenied('Invalid authorization response')
        # Local policy is an upper bound even if a malformed response adds privileges.
        from .authority import CAPABILITIES
        caps=set(result.get('capabilities',[])) & set(CAPABILITIES[role])
        self.identity={**result,'capabilities':sorted(caps)}
        for secret in ('session','device_secret','device_id'):self.identity.pop(secret,None)
        self.last_refresh=self.clock()
        self.deadline=self.last_refresh+lease
    def resume(self):
        if not self.device_store:return False
        saved=self.device_store.load()
        if not saved:return False
        result=self.transport({'action':'device_login','device_secret':saved['device_secret'],'product':self.product})
        token=result.get('session')
        if not isinstance(token,str) or len(token)!=43:raise AccessDenied('Invalid session response')
        self._accept(result);self.token=token
        return True
    def refresh(self):
        expected_account=self.identity.get('account')
        if not self.token:
            raise AccessDenied('请先登录')
        try:
            result=self.transport({'action':'refresh','session':self.token,'product':self.product})
            self._accept(result)
        except AccessDenied:
            self.clear()
            if self.device_store and self.resume():
                if self.identity.get('account') != expected_account:
                    self.clear()
                    raise AccessDenied('设备已切换账号，请重新打开当前任务')
                return True
            raise
        except OSError:
            if self.clock()>=self.deadline:
                self.clear()
                raise AccessDenied('离线授权已到期，请联网重新登录') from None
            return False
        return True
    def allows(self, capability):
        return bool(self.token and self.clock()<self.deadline and capability in self.identity.get('capabilities',[]))
    def require(self, capability):
        if not self.allows(capability):
            raise AccessDenied('当前账号无权使用此功能，或登录已过期')
    def admin(self, action, **values):
        self.require('accounts')
        # No administrative action is queued for offline replay.
        return self.transport({'action':action,'session':self.token,'product':self.product,**values})
    def clear(self):
        self.token=''
        self.identity={}
        self.deadline=0
    def logout(self):
        token=self.token
        self.clear()
        if token:
            try:self.transport({'action':'logout','session':token,'product':self.product})
            except OSError:pass

_current=None

def install_session(session):
    global _current
    _current=session

def current_session():
    if _current is None:
        raise AccessDenied('请先登录')
    return _current

def require(capability):
    current_session().require(capability)

def read_config(root):
    config_path=Path(root)/'cowmata-security.json'
    if not config_path.is_file():
        raise AccessDenied('尚未配置授权服务。请由管理员部署服务并配置 cowmata-security.json。')
    config=json.loads(config_path.read_text(encoding='utf-8-sig'))
    if config.get('transport')=='ssh':
        from .ssh_transport import SshTransport
        return SshTransport(config, root)
    if config.get('transport')!='https':
        raise AccessDenied('Unsupported transport configuration')
    return HttpsTransport(config['endpoint'])
