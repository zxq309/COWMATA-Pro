import concurrent.futures
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from cowmata_security.authority import Authority, Denied
from cowmata_security.client import AccessDenied, HttpsTransport, Session
from cowmata_security.protocol import dispatch
from cowmata_security.service import Application

class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.now=[10000.]
        self.path=Path(self.tmp.name)/'authority.csv'
        self.auth=Authority(self.path,clock=lambda:self.now[0])
        self.password='long-recovery-password-test-only'
        self.auth.bootstrap('admin',self.password)
        code=self.auth.recover_grant('admin',self.password,'pro')['code']
        self.admin=self.auth.redeem('admin',code,'pro')['session']
        self.auth.create_user(self.admin,'worker','operator')
    def tearDown(self):self.tmp.cleanup()
    def code(self, product='pro'):
        return self.auth.issue(self.admin,'worker',product)['code']
    def test_reuse_fails_even_after_restart(self):
        code=self.code();self.auth.redeem('worker',code,'pro')
        other=Authority(self.path,clock=lambda:self.now[0])
        with self.assertRaises(Denied):other.redeem('worker',code,'pro')
    def test_only_one_concurrent_redemption_succeeds(self):
        code=self.code()
        def attempt(_):
            try:self.auth.redeem('worker',code,'pro');return 1
            except Denied:return 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt,range(8))),1)
    def test_expiry_inclusive(self):
        code=self.code();self.now[0]+=600
        with self.assertRaises(Denied):self.auth.redeem('worker',code,'pro')
    def test_account_and_product_binding(self):
        code=self.code()
        for name,product in [('admin','pro'),('worker','ledger')]:
            with self.assertRaises(Denied):self.auth.redeem(name,code,product)
        self.assertEqual(self.auth.redeem('worker',code,'pro')['role'],'operator')
    def test_tampered_code_fails(self):
        code=self.code()
        with self.assertRaises(Denied):self.auth.redeem('worker',code+'x','pro')
    def test_server_roles_cannot_be_forged(self):
        token=self.auth.redeem('worker',self.code(),'pro')['session']
        for action in ['create_user','issue','set_active','users']:
            with self.assertRaises(Denied):dispatch(self.auth,{'action':action,'session':token,'account':'other','role':'admin','active':False,'product':'pro'})
        for cap in ['behavior','health','dataset','accounts']:
            with self.assertRaises(Denied):self.auth.authorize(token,'pro',cap)
        self.auth.authorize(token,'pro','annotate')
    def test_disable_revokes_codes_and_sessions(self):
        token=self.auth.redeem('worker',self.code(),'pro')['session'];code=self.code()
        self.auth.set_active(self.admin,'worker',False)
        self.auth.set_active(self.admin,'worker',True)
        with self.assertRaises(Denied):self.auth.refresh(token,'pro')
        with self.assertRaises(Denied):self.auth.redeem('worker',code,'pro')
    def test_logout_invalidates_session(self):
        token=self.auth.redeem('worker',self.code(),'pro')['session'];self.auth.logout(token)
        with self.assertRaises(Denied):self.auth.refresh(token,'pro')
    def test_no_raw_code_or_session_in_csv(self):
        code=self.code();token=self.auth.redeem('worker',code,'pro')['session']
        raw=self.path.read_bytes()
        for secret in (code,token,self.password,self.admin):self.assertNotIn(secret.encode(),raw)
    def test_recovery_requires_password_and_admin(self):
        with self.assertRaises(Denied):self.auth.recover_grant('admin','wrong','pro')
        with self.assertRaises(Denied):self.auth.recover_grant('worker',self.password,'pro')
        with self.assertRaises(Denied):self.auth.bootstrap('intruder',self.password)
    def test_no_self_disable(self):
        with self.assertRaises(Denied):self.auth.set_active(self.admin,'admin',False)
    def test_failed_attempts_rate_limited(self):
        for _ in range(10):
            with self.assertRaises(Denied):self.auth.redeem('worker','bad','pro')
        with self.assertRaises(Denied):self.auth.redeem('worker',self.code(),'pro')
    def test_session_expiry(self):
        token=self.auth.redeem('worker',self.code(),'pro')['session'];self.now[0]+=8*3600
        with self.assertRaises(Denied):self.auth.refresh(token,'pro')
    def test_wsgi_no_cache_and_no_secret_reflection(self):
        request=json.dumps({'action':'redeem','account':'worker','code':'a-secret','product':'pro'}).encode()
        capture=[]
        body=b''.join(Application(self.auth)({'PATH_INFO':'/v1/auth','REQUEST_METHOD':'POST','CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(request)),'wsgi.input':io.BytesIO(request)},lambda s,h:capture.append((s,dict(h)))))
        self.assertEqual(capture[0][0],'403 Forbidden')
        self.assertEqual(capture[0][1]['Cache-Control'],'no-store')
        self.assertNotIn(b'a-secret',body)
    def test_client_offline_limit_uses_monotonic_time(self):
        clock=[0.];offline=[False]
        def transport(req):
            if offline[0]:raise OSError('offline')
            try:return dispatch(self.auth,req)
            except Denied as e:raise AccessDenied(str(e))
        client=Session(transport,'pro',monotonic=lambda:clock[0])
        client.login('worker',self.code());offline[0]=True
        clock[0]=899;self.assertFalse(client.refresh());client.require('annotate')
        with self.assertRaises(AccessDenied):client.require('behavior')
        clock[0]=900
        with self.assertRaises(AccessDenied):client.refresh()
        self.assertFalse(client.token)
    def test_client_revoked_session_has_no_offline_grace(self):
        def transport(req):
            try:return dispatch(self.auth,req)
            except Denied as e:raise AccessDenied(str(e))
        client=Session(transport,'pro');client.login('worker',self.code())
        self.auth.set_active(self.admin,'worker',False)
        with self.assertRaises(AccessDenied):client.refresh()
        self.assertFalse(client.token)
    def test_even_owner_cannot_create_another_issuer(self):
        with self.assertRaises(Denied):self.auth.create_user(self.admin,'another_admin','admin')
    def test_malformed_csv_fails_closed(self):
        self.path.write_text('wrong,header\n',encoding='utf8')
        with self.assertRaises(ValueError):self.auth.refresh(self.admin,'pro')
    def test_no_database_is_created(self):
        self.assertEqual({p.suffix for p in Path(self.tmp.name).iterdir()},{'.csv','.lock'})
    def test_activation_persists_after_code_expiry(self):
        activated=self.auth.redeem('worker',self.code(),'pro')
        self.now[0]+=10*86400
        reply=self.auth.device_login(activated['device_secret'],'pro')
        self.assertEqual(reply['account'],'worker')
    def test_deleting_grant_row_revokes_device_and_open_session(self):
        from cowmata_security.csv_store import find
        activated=self.auth.redeem('worker',self.code(),'pro')
        with self.auth.store.transaction() as rows:
            device=next(r for r in rows if r['kind']=='device' and r['value']==activated['device_id'])
            rows[:]=[r for r in rows if not(r['kind']=='grant' and r['id']==device['actor'])]
        with self.assertRaises(Denied):self.auth.device_login(activated['device_secret'],'pro')
        with self.assertRaises(Denied):self.auth.refresh(activated['session'],'pro')
    def test_operator_cannot_list_or_revoke_devices(self):
        activated=self.auth.redeem('worker',self.code(),'pro')
        with self.assertRaises(Denied):self.auth.devices(activated['session'])
        with self.assertRaises(Denied):self.auth.revoke_device(activated['session'],activated['device_id'])
    def test_owner_revokes_device(self):
        activated=self.auth.redeem('worker',self.code(),'pro')
        self.auth.revoke_device(self.admin,activated['device_id'])
        with self.assertRaises(Denied):self.auth.device_login(activated['device_secret'],'pro')
    def test_client_silent_login_and_expired_session_renewal(self):
        class MemoryStore:
            value=None
            def load(self):return self.value
            def save(self,value):self.value=value
        saved=MemoryStore()
        def transport(req):
            try:return dispatch(self.auth,req)
            except Denied as e:raise AccessDenied(str(e))
        client=Session(transport,'pro',device_store=saved)
        client.login('worker',self.code());code_count=len([r for r in self.auth.store.path.read_text().splitlines() if r.startswith('grant,')])
        reopened=Session(transport,'pro',device_store=saved);self.assertTrue(reopened.resume())
        self.now[0]+=8*3600+1;self.assertTrue(reopened.refresh());self.assertTrue(reopened.allows('annotate'))
        self.assertNotIn('device_secret',reopened.identity)
        self.assertEqual(code_count,len([r for r in self.auth.store.path.read_text().splitlines() if r.startswith('grant,')]))
    def test_dpapi_round_trip_and_tamper_detection(self):
        import os
        if os.name!='nt':self.skipTest('Windows-only')
        from cowmata_security.device_store import DeviceStore
        store=DeviceStore('pro',Path(self.tmp.name)/'device.bin')
        value={'device_secret':'test-sensitive-device-secret'};store.save(value)
        self.assertNotIn(value['device_secret'].encode(),store.path.read_bytes());self.assertEqual(store.load(),value)
        raw=bytearray(store.path.read_bytes());raw[-1]^=1;store.path.write_bytes(raw)
        with self.assertRaises(AccessDenied):store.load()
    def test_legacy_owner_hash_import(self):
        import hashlib
        from cowmata_security.csv_store import find
        salt=b'synthetic-test-1';password='previous-owner-password'
        encoded='pbkdf2-sha256$600000$'+salt.hex()+'$'+hashlib.pbkdf2_hmac('sha256',password.encode(),salt,600000).hex()
        with self.auth.store.transaction() as rows:find(rows,'setting','recovery')['value']=encoded
        self.assertTrue(self.auth.recover_grant('admin',password,'pro')['code'].startswith('cw1_'))
    def test_independent_processes_redeem_once(self):
        import subprocess,sys
        code=self.code()
        probe='import sys,json;sys.path.insert(0,sys.argv[1]);from cowmata_security.authority import Authority,Denied;p,c=json.loads(sys.stdin.read());a=Authority(p,clock=lambda:10000.);\ntry:a.redeem("worker",c,"pro");print("success")\nexcept Denied:print("denied")'
        module_root=str(Path(__import__('cowmata_security').__file__).parent.parent)
        children=[subprocess.Popen([sys.executable,'-B','-c',probe,module_root],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(6)]
        results=[]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda child:child.communicate(json.dumps([str(self.path),code]).encode(),timeout=15),children))
        self.assertEqual(sum(b'success' in output for output,error in results),1,results)
        self.assertTrue(all(child.returncode==0 for child in children),results)
    def test_client_rejects_plain_http(self):
        for endpoint in ('http://example.com','https://user:pass@example.com','https://example.com/?secret=x'):
            with self.assertRaises(ValueError):HttpsTransport(endpoint)
    def test_client_no_role_escalation_from_caps(self):
        client=Session(lambda _:None,'pro');client.token='t'*43
        client._accept({'account':'worker','role':'operator','product':'pro','lease_seconds':900,'capabilities':['accounts','behavior','annotate']})
        self.assertFalse(client.allows('accounts'));self.assertTrue(client.allows('annotate'))

if __name__=='__main__':unittest.main()
