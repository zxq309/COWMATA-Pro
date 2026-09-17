"""Three-column owner registry, with automatic single-use/session bookkeeping."""
from __future__ import annotations
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
from pathlib import Path
from contextlib import contextmanager
from .authority import Authority, Denied, digest, SESSION_TTL
from .csv_store import CsvStore, record, find

COLUMNS=('账号','密码','授权码')

def fingerprint(item):
    return hashlib.sha256(json.dumps(item,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def admin_fingerprint(item):
    # The administrator's unused code never controls password sessions.
    return fingerprint({'account':item['account'],'password':item['password']})

def parse_registry(raw):
    if len(raw)>4*1024*1024:raise ValueError('账号表过大')
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'),newline=''))
    if reader.fieldnames!=list(COLUMNS):raise ValueError('账号表必须只有三列：账号、密码、授权码')
    result=[];names=set();codes=set()
    for row in reader:
        if None in row or any(v is None or '\0' in v for v in row.values()):raise ValueError('账号表有缺少或多余的单元格')
        item={'account':row['账号'].strip(),'password':row['密码'],'code':row['授权码'].strip()}
        if not re.fullmatch(r'[a-z][a-z0-9_]{2,31}',item['account']):raise ValueError('账号须为3至32位小写字母、数字或下划线，并以字母开头')
        if not 1<=len(item['password'])<=1024:raise ValueError('密码不能为空')
        if item['code'] and not re.fullmatch(r'[A-Za-z0-9_-]{16,256}',item['code']):raise ValueError('授权码格式无效，请使用生成的授权码')
        if item['account'] in names or (item['code'] and item['code'] in codes):raise ValueError('账号或授权码重复，请保留唯一的一行')
        names.add(item['account']);codes.add(item['code']);result.append(item)
    if len(result)>1000:raise ValueError('账号数量最多1000个')
    return result

def encode_registry(items):
    stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=COLUMNS)
    writer.writeheader()
    for item in items:writer.writerow(dict(zip(COLUMNS,(item['account'],item['password'],item['code']))))
    raw=stream.getvalue().encode('utf-8-sig');parse_registry(raw);return raw

def atomic_registry(path,raw):
    import tempfile
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.accounts-',suffix='.tmp')
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

class Rows(list):
    pass

class ManagedStore:
    def __init__(self,path,product):
        self.path=Path(path).resolve();self.product=product
        self.internal=CsvStore(self.path.parent/'.system'/'state.csv')
    def sync(self,rows,items):
        owner=find(rows,'setting','owner');scope=find(rows,'setting','product')
        if not owner or not scope or scope['value']!=self.product:raise Denied('账号服务未正确初始化')
        active={r['account']:r for r in items}
        for user in rows:
            if user['kind']=='user':
                user['active']='1' if user['account'] in active else '0'
                user['role']='admin' if user['account']==owner['value'] else 'operator'
        valid={digest(r['code']):fingerprint(r) for r in items if r['code']}
        admin_item=active.get(owner['value'])
        for grant in rows:
            if grant['kind']!='grant':continue
            if grant['action']=='admin-password':
                matches=bool(admin_item and grant['account']==owner['value'] and grant['product']==self.product and grant['value']==admin_fingerprint(admin_item))
            else:matches=grant['id'] in valid and grant['value']==valid[grant['id']]
            if not matches:grant['revoked']='1'
        for item in items:
            account=item['account'];code_id=digest(item['code'])
            if not find(rows,'user',account):rows.append(record('user',account,account=account,role='admin' if account==owner['value'] else 'operator',active=1))
            if item['code'] and not find(rows,'grant',code_id):rows.append(record('grant',code_id,account=account,product=self.product,expires='inf',revoked=0,value=fingerprint(item)))
    @contextmanager
    def transaction(self,*,create=False):
        if create:raise Denied('三列账号表须由管理员迁移，禁止自动重建')
        failure=None
        with self.internal.transaction() as saved:
            if not self.path.is_file():raise Denied('账号表不存在，授权已停止')
            original=self.path.read_bytes();items=parse_registry(original)
            self.sync(saved,items)
            rows=Rows(dict(r) for r in saved);rows.credentials=[dict(r) for r in items]
            try:
                yield rows
                if not self.path.is_file() or self.path.read_bytes()!=original:raise Denied('账号表正在编辑，请稍后重试')
                if rows.credentials!=items:
                    raw=encode_registry(rows.credentials)
                    self.sync(rows,rows.credentials)
                    atomic_registry(self.path,raw)
                saved[:]=rows
            except Exception as exc:
                # Persist only registry-driven revocations, not a failed action.
                failure=exc
        if failure is not None:raise failure

def initialize_registry(path,product,items,owner='admin'):
    path=Path(path).resolve();state=path.parent/'.system'/'state.csv'
    if path.exists() or state.exists():raise ValueError('账号表或运行状态已存在，禁止覆盖')
    if not any(r['account']==owner for r in items):raise ValueError('必须保留唯一管理员')
    raw=encode_registry(items);path.parent.mkdir(parents=True,exist_ok=True)
    with CsvStore(state).transaction(create=True) as rows:
        rows.extend([record('setting','owner',value=owner),record('setting','product',value=product)])
    atomic_registry(path,raw)
    if os.name=='nt':
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(state.parent),2)
    with ManagedStore(path,product).transaction():pass

def new_credential(account):
    return {'account':account,'password':'Cw!'+secrets.token_urlsafe(12),'code':'CW-'+secrets.token_urlsafe(24)}

class SimpleAuthority:
    requires_password=True
    def __init__(self,path,*,product='pro',clock=None):
        from time import time
        self.engine=Authority(path,product=product,clock=clock or time)
        self.engine.store=ManagedStore(path,product)
        self.store=self.engine.store;self.product=product
    def redeem(self,name,code,product,peer='local',device_label='Desktop',password=None):
        a=self.engine;a._check_product(product)
        if not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9_]{2,31}',name) or not isinstance(password,str) or len(password)>1024 or not isinstance(code,str) or len(code)>256:raise Denied('请填写账号、密码、授权码；旧版客户端请升级')
        result=None
        with self.store.transaction() as rows:
            a._prune(rows)
            for scope in ('account:'+name,'peer:'+str(peer)):a._check_rate(rows,scope)
            item=next((r for r in rows.credentials if r['account']==name),None)
            grant=find(rows,'grant',digest(code));user=find(rows,'user',name)
            valid=bool(code and item and hmac.compare_digest(item['password'].encode(),password.encode()) and hmac.compare_digest(item['code'],code))
            if not valid or not grant or not user or user['active']!='1' or grant['account']!=name or grant['product']!=product or grant['consumed'] or grant['revoked']=='1':
                for scope in ('account:'+name,'peer:'+str(peer)):a._failure(rows,scope)
                a._audit(rows,name,'denied-login',product)
            else:
                now=a.clock();grant['consumed']=str(now)
                result=self._new_session(rows,name,product,grant,device_label)
        if result is None:raise Denied('账号、密码或授权码无效，或此码已激活；已激活电脑请使用自动登录')
        return result
    def _new_session(self,rows,name,product,grant,device_label):
        a=self.engine;now=a.clock()
        token=secrets.token_urlsafe(32);device_secret=secrets.token_urlsafe(32);device_id=secrets.token_hex(16)
        label=str(device_label)[:80].replace('\r',' ').replace('\n',' ')
        rows.append(record('device',digest(device_secret),account=name,product=product,value=device_id,subject=label,revoked=0,at=now,actor=grant['id']))
        rows.append(record('session',digest(token),account=name,product=product,expires=now+SESSION_TTL,revoked=0,subject=device_id))
        a._audit(rows,name,'login',product)
        return {'session':token,'device_secret':device_secret,'device_id':device_id,**a._view(a._identity(rows,token,product=product))}
    def admin_login(self,name,password,product,peer='local',device_label='Desktop'):
        a=self.engine;a._check_product(product)
        if not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9_]{2,31}',name) or not isinstance(password,str) or not 1<=len(password)<=1024:raise Denied('请填写管理员账号和密码')
        result=None
        with self.store.transaction() as rows:
            a._prune(rows)
            for scope in ('account:'+name,'peer:'+str(peer)):a._check_rate(rows,scope)
            owner=find(rows,'setting','owner');user=find(rows,'user',name)
            item=next((r for r in rows.credentials if r['account']==name),None)
            valid=bool(item and owner and name==owner['value'] and user and user['active']=='1' and hmac.compare_digest(item['password'].encode(),password.encode()))
            if not valid:
                for scope in ('account:'+name,'peer:'+str(peer)):a._failure(rows,scope)
                a._audit(rows,name,'denied-admin-login',product)
            else:
                # Each password login creates a fresh device grant. Revoked devices
                # stay revoked even if an old password or CSV row is restored.
                grant=record('grant',secrets.token_hex(32),account=name,product=product,expires='inf',revoked=0,value=admin_fingerprint(item),action='admin-password')
                rows.append(grant)
                result=self._new_session(rows,name,product,grant,device_label)
        if result is None:raise Denied('管理员账号或密码不正确')
        return result
    def password_device_login(self,name,password,device_secret,product,peer='local'):
        a=self.engine;a._check_product(product)
        if not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9_]{2,31}',name) or not isinstance(password,str) or not 1<=len(password)<=1024 or not isinstance(device_secret,str) or len(device_secret)!=43:raise Denied('请填写账号和密码，首次使用需要授权码')
        result=None
        with self.store.transaction() as rows:
            a._prune(rows)
            for scope in ('account:'+name,'peer:'+str(peer)):a._check_rate(rows,scope)
            item=next((r for r in rows.credentials if r['account']==name),None)
            device=find(rows,'device',digest(device_secret));user=find(rows,'user',name)
            grant=find(rows,'grant',device['actor']) if device else None
            valid=bool(item and user and user['role']=='operator' and user['active']=='1' and device and device['account']==name and device['product']==product and device['revoked']!='1' and grant and grant['revoked']!='1' and hmac.compare_digest(item['password'].encode(),password.encode()))
            if not valid:
                for scope in ('account:'+name,'peer:'+str(peer)):a._failure(rows,scope)
                a._audit(rows,name,'denied-password-device-login',product)
            else:
                token=secrets.token_urlsafe(32)
                rows.append(record('session',digest(token),account=name,product=product,expires=a.clock()+SESSION_TTL,revoked=0,subject=device['value']))
                a._audit(rows,name,'password-device-login',product)
                result={'session':token,**a._view(a._identity(rows,token,product=product))}
        if result is None:raise Denied('账号、密码或本机授权无效；首次使用请填写授权码')
        return result
    def credentials(self,token):
        with self.store.transaction() as rows:
            self.engine._identity(rows,token,admin=True)
            return {'credentials':[dict(r) for r in rows.credentials]}
    def batch_create(self,token,count=10):
        if type(count) is not int or not 1<=count<=100:raise ValueError('每次生成1至100个账号')
        with self.store.transaction() as rows:
            actor=self.engine._identity(rows,token,admin=True)
            if len(rows.credentials)+count>1000:raise ValueError('账号总数最多1000个')
            used={r['account'] for r in rows.credentials};index=1
            for _ in range(count):
                while 'operator'+str(index).zfill(2) in used:index+=1
                name='operator'+str(index).zfill(2);used.add(name);rows.credentials.append(new_credential(name))
            self.engine._audit(rows,actor['name'],'batch-create',str(count))
            return {'credentials':[dict(r) for r in rows.credentials]}
    def remove_account(self,token,account):
        with self.store.transaction() as rows:
            actor=self.engine._identity(rows,token,admin=True);owner=find(rows,'setting','owner')['value']
            if account==owner:raise Denied('不能删除唯一管理员')
            if not any(r['account']==account for r in rows.credentials):raise ValueError('账号不存在')
            rows.credentials[:]=[r for r in rows.credentials if r['account']!=account]
            self.engine._audit(rows,actor['name'],'remove-account',account)
            return {'credentials':[dict(r) for r in rows.credentials]}
    def refresh(self,token,product):
        with self.store.transaction() as rows:
            identity=self.engine._identity(rows,token,product=product)
            if identity['role']=='admin':
                # Keep an already running job alive without storing admin credentials.
                # _identity rejects expired/revoked tokens before any renewal occurs.
                expiry=self.engine.clock()+SESSION_TTL
                find(rows,'session',digest(token))['expires']=str(expiry)
                identity['expires']=str(expiry)
            return self.engine._view(identity)
    def device_login(self,*args):return self.engine.device_login(*args)
    def authorize(self,*args):return self.engine.authorize(*args)
    def logout(self,*args):return self.engine.logout(*args)
    def users(self,*args):return self.engine.users(*args)
    def devices(self,*args):return self.engine.devices(*args)
    def revoke_device(self,*args):raise Denied('请在三列表格中删除账号行撤权')
    def issue(self,*args):raise Denied('请使用三列表格批量生成账号')
    def create_user(self,*args):raise Denied('请使用三列表格批量生成账号')
    def set_active(self,*args):raise Denied('请在三列表格中删除账号行撤权')
