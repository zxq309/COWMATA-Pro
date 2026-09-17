"""Single-owner authority backed exclusively by locked local CSV files."""
from __future__ import annotations
import base64
import hashlib
import hmac
import re
import secrets
import time
from .csv_store import CsvStore, record, find

PRODUCTS=frozenset({'pro','ledger'})
CAPABILITIES={'operator':['annotate','prepare','upload'], 'admin':['annotate','prepare','upload','behavior','health','dataset','accounts']}
GRANT_TTL=600
LEASE_TTL=900
SESSION_TTL=8*3600
class Denied(PermissionError):pass

def digest(value):return hashlib.sha256(value.encode('utf-8')).hexdigest()
def valid_name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9_]{2,31}',value):
        raise ValueError('账号须为3至32位小写字母、数字或下划线，并以字母开头')
    return value

class Authority:
    def __init__(self,path,*,product='pro',clock=time.time):
        if product not in PRODUCTS:raise ValueError('Unknown product')
        self.store=CsvStore(path);self.path=str(self.store.path);self.product=product;self.clock=clock
    def _check_product(self,product):
        if product!=self.product:raise Denied('登录码只可用于指定应用')
    def _audit(self,rows,actor,action,subject):
        rows.append(record('audit',secrets.token_hex(16),at=self.clock(),actor=actor,action=action,subject=subject))
        audits=[r for r in rows if r['kind']=='audit']
        if len(audits)>5000:
            expired={r['id'] for r in audits[:-5000]};rows[:]=[r for r in rows if r['kind']!='audit' or r['id'] not in expired]
    def _prune(self,rows):
        now=self.clock()
        linked={r['actor'] for r in rows if r['kind']=='device' and r['revoked']!='1'}
        rows[:]=[r for r in rows if not ((r['kind'] in ('grant','session') and r['id'] not in linked and float(r['expires'])<now-86400) or (r['kind']=='failure' and float(r['until'])<now))]
    def bootstrap(self,name,recovery_password):
        name=valid_name(name)
        if not isinstance(recovery_password,str) or not 16<=len(recovery_password)<=1024:raise ValueError('恢复口令至少16个字符')
        salt=secrets.token_bytes(16)
        hashed=hashlib.scrypt(recovery_password.encode(),salt=salt,n=32768,r=8,p=1,maxmem=128*1024*1024)
        with self.store.transaction(create=True) as rows:
            if rows:raise Denied('授权中心已经初始化')
            rows.extend([record('setting','recovery',value=base64.b64encode(salt+hashed).decode()),record('setting','owner',value=name),record('setting','product',value=self.product),record('user',name,account=name,role='admin',active=1)])
            self._audit(rows,name,'bootstrap',name)
    def _check_rate(self,rows,subject):
        r=find(rows,'failure',subject)
        if r and int(r['count'])>=10 and float(r['until'])>self.clock():raise Denied('尝试过多，请5分钟后重试')
    def _failure(self,rows,subject):
        r=find(rows,'failure',subject)
        if not r:r=record('failure',subject,count=0,until=0);rows.append(r)
        r['count']=str(int(r['count'])+1 if float(r['until'])>self.clock() else 1);r['until']=str(self.clock()+300)
    def recover_grant(self,name,password,product):
        name=valid_name(name);self._check_product(product)
        if not isinstance(password,str) or len(password)>1024:raise Denied('恢复凭据无效')
        result=None
        with self.store.transaction() as rows:
            self._check_rate(rows,'recovery')
            stored=find(rows,'setting','recovery');owner=find(rows,'setting','owner')
            encoded=stored['value'] if stored else ''
            if encoded.startswith('pbkdf2-sha256$'):
                algorithm,iterations,salt,expected=encoded.split('$')
                if int(iterations)!=600000:raise Denied('Unsupported legacy password parameters')
                actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex()
                matches=hmac.compare_digest(actual,expected)
            else:
                raw=base64.b64decode(encoded) if stored else bytes(80)
                actual=hashlib.scrypt(password.encode(),salt=raw[:16],n=32768,r=8,p=1,maxmem=128*1024*1024)
                matches=hmac.compare_digest(actual,raw[16:])
            user=find(rows,'user',name)
            if not owner or owner['value']!=name or not user or user['active']!='1' or not matches:
                self._failure(rows,'recovery')
            else:result=self._issue(rows,name,product,600,'local-owner')
        if result is None:raise Denied('管理员恢复口令无效')
        return result
    def _identity(self,rows,token,*,admin=False,product=None):
        if not isinstance(token,str) or len(token)!=43:raise Denied('Session invalid or expired')
        s=find(rows,'session',digest(token));u=find(rows,'user',s['account']) if s else None
        device=next((r for r in rows if r['kind']=='device' and s and r['value']==s['subject']),None)
        grant=find(rows,'grant',device['actor']) if device else None
        owner=find(rows,'setting','owner');scope=find(rows,'setting','product')
        if not scope or scope['value']!=self.product:raise Denied('授权文件应用标识不匹配')
        if not s or not u or not device or not grant or grant['revoked']=='1' or device['revoked']=='1' or s['revoked']=='1' or u['active']!='1' or float(s['expires'])<=self.clock() or (product and s['product']!=product):raise Denied('Session invalid or expired')
        if admin and (u['role']!='admin' or not owner or u['account']!=owner['value']):raise Denied('只有唯一管理员能管理账号和生成登录码')
        return {**s,'name':u['account'],'role':u['role']}
    def _issue(self,rows,name,product,ttl,actor):
        self._check_product(product)
        if type(ttl) is not int or not 60<=ttl<=600:raise ValueError('有效期必须为60至600秒')
        user=find(rows,'user',name)
        if not user or user['active']!='1':raise Denied('账号不可用')
        code='cw1_'+secrets.token_urlsafe(32);expiry=self.clock()+ttl
        rows.append(record('grant',digest(code),account=name,product=product,expires=expiry,revoked=0))
        self._audit(rows,actor,'issue-grant',name)
        return {'code':code,'account':name,'product':product,'expires_at':expiry}
    def issue(self,token,name,product,ttl=600):
        name=valid_name(name)
        with self.store.transaction() as rows:
            self._prune(rows);actor=self._identity(rows,token,admin=True)
            return self._issue(rows,name,product,ttl,actor['name'])
    def create_user(self,token,name,role='operator'):
        name=valid_name(name)
        with self.store.transaction() as rows:
            actor=self._identity(rows,token,admin=True)
            if role!='operator':raise Denied('只能创建操作员；本系统仅有一个发码管理员')
            if find(rows,'user',name):raise ValueError('账号已存在')
            if sum(r['kind']=='user' for r in rows)>=1000:raise ValueError('账号数量已达上限')
            rows.append(record('user',name,account=name,role=role,active=1));self._audit(rows,actor['name'],'create-account',name)
        return {'account':name,'role':role}
    def set_active(self,token,name,active):
        name=valid_name(name)
        if type(active) is not bool:raise ValueError('active must be a boolean')
        with self.store.transaction() as rows:
            actor=self._identity(rows,token,admin=True);user=find(rows,'user',name)
            if actor['name']==name and not active:raise Denied('不能停用唯一管理员')
            if not user:raise ValueError('账号不存在')
            user['active']='1' if active else '0'
            if not active:
                for row in rows:
                    if row['kind'] in ('grant','session','device') and row['account']==name:row['revoked']='1'
            self._audit(rows,actor['name'],'enable-account' if active else 'disable-account',name)
        return {'account':name,'active':active}
    def _view(self,row):
        now=self.clock()
        return {'account':row['name'],'role':row['role'],'product':row['product'],'capabilities':CAPABILITIES[row['role']], 'server_time':now,'expires_at':float(row['expires']),'lease_seconds':max(0,min(900,float(row['expires'])-now))}
    def redeem(self,name,code,product,peer='local',device_label='Desktop'):
        name=valid_name(name);self._check_product(product)
        if not isinstance(code,str) or len(code)>256:raise Denied('Invalid login grant')
        result=None
        with self.store.transaction() as rows:
            self._prune(rows)
            for scope in ('account:'+name,'peer:'+str(peer)):self._check_rate(rows,scope)
            grant=find(rows,'grant',digest(code));user=find(rows,'user',name)
            if not grant or not user or user['active']!='1' or grant['account']!=name or grant['product']!=product or grant['consumed'] or grant['revoked']=='1' or float(grant['expires'])<=self.clock():
                for scope in ('account:'+name,'peer:'+str(peer)):self._failure(rows,scope)
                self._audit(rows,name,'denied-login',product)
            else:
                now=self.clock();grant['consumed']=str(now)
                token=secrets.token_urlsafe(32)
                device_secret=secrets.token_urlsafe(32);device_id=secrets.token_hex(16)
                label=str(device_label)[:80].replace('\r',' ').replace('\n',' ')
                rows.append(record('device',digest(device_secret),account=name,product=product,value=device_id,subject=label,revoked=0,at=now,actor=grant['id']))
                rows.append(record('session',digest(token),account=name,product=product,expires=now+SESSION_TTL,revoked=0,subject=device_id))
                self._audit(rows,name,'login',product)
                result={'session':token,'device_secret':device_secret,'device_id':device_id,**self._view(self._identity(rows,token,product=product))}
        if result is None:raise Denied('登录码无效、已过期或已被使用')
        return result
    def refresh(self,token,product):
        with self.store.transaction() as rows:return self._view(self._identity(rows,token,product=product))
    def authorize(self,token,product,capability):
        view=self.refresh(token,product)
        if capability not in view['capabilities']:raise Denied('Permission denied')
        return view
    def users(self,token):
        with self.store.transaction() as rows:
            self._identity(rows,token,admin=True)
            return [{'name':r['account'],'role':r['role'],'active':r['active']=='1'} for r in rows if r['kind']=='user']
    def logout(self,token):
        with self.store.transaction() as rows:
            row=find(rows,'session',digest(token))
            if row:row['revoked']='1'
        return {'ok':True}

    def device_login(self,device_secret,product):
        self._check_product(product)
        if not isinstance(device_secret,str) or len(device_secret)!=43:raise Denied('设备授权无效')
        with self.store.transaction() as rows:
            self._prune(rows)
            device=find(rows,'device',digest(device_secret))
            user=find(rows,'user',device['account']) if device else None
            grant=find(rows,'grant',device['actor']) if device else None
            if not grant or grant['revoked']=='1' or not device or not user or device['revoked']=='1' or user['active']!='1' or device['product']!=product:raise Denied('设备授权已撤销，请联系管理员')
            token=secrets.token_urlsafe(32);now=self.clock()
            rows.append(record('session',digest(token),account=device['account'],product=product,expires=now+SESSION_TTL,revoked=0,subject=device['value']))
            self._audit(rows,device['account'],'device-login',device['value'])
            return {'session':token,**self._view(self._identity(rows,token,product=product))}
    def devices(self,token):
        with self.store.transaction() as rows:
            self._identity(rows,token,admin=True)
            return [{'device_id':r['value'],'account':r['account'],'label':r['subject'],'revoked':r['revoked']=='1','created_at':r['at'],'authorization_id':r['actor']} for r in rows if r['kind']=='device']
    def revoke_device(self,token,device_id):
        with self.store.transaction() as rows:
            actor=self._identity(rows,token,admin=True)
            target=next((r for r in rows if r['kind']=='device' and r['value']==device_id),None)
            if not target:raise ValueError('设备不存在')
            target['revoked']='1'
            rows[:]=[r for r in rows if not (r['kind']=='grant' and r['id']==target['actor'])]
            self._audit(rows,actor['name'],'revoke-device',device_id)
        return {'ok':True}
