import sys,tempfile,unittest,concurrent.futures
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cowmata_security.simple_authority import SimpleAuthority,initialize_registry,new_credential,encode_registry,parse_registry
from cowmata_security.authority import Denied
from cowmata_security.protocol import dispatch
from cowmata_security.client import Session,AccessDenied
class AdminPasswordTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'authority.csv';self.now=[10000.]
  self.admin={**new_credential('owner_qa'),'code':''};self.operator=new_credential('operator01')
  initialize_registry(self.path,'pro',[self.admin,self.operator],owner='owner_qa')
  self.a=SimpleAuthority(self.path,clock=lambda:self.now[0])
 def login(self,item=None,**extra):
  item=item or self.admin
  return dispatch(self.a,{'action':'admin_login','account':item['account'],'password':item['password'],'product':'pro',**extra})
 def write(self,items):self.path.write_bytes(encode_registry(items))
 def test_admin_no_code_repeatable_and_all_permissions(self):
  one=self.login();two=self.login(device_label='Second PC')
  self.assertEqual(one['role'],'admin');self.assertEqual(two['role'],'admin');self.assertNotEqual(one['device_secret'],two['device_secret'])
  for cap in ('accounts','behavior','health','dataset'):self.a.authorize(one['session'],'pro',cap)
  self.assertEqual(self.a.device_login(two['device_secret'],'pro')['account'],'owner_qa')
 def test_password_only_admin_requires_fixed_owner(self):
  with self.assertRaises(Denied):self.login(self.operator,role='admin')
  op=self.a.redeem(self.operator['account'],self.operator['code'],'pro',password=self.operator['password'])
  self.assertEqual(op['role'],'operator')
  with self.assertRaises(Denied):self.a.credentials(op['session'])
 def test_operator_cannot_skip_authorization_code(self):
  with self.assertRaises(Denied):self.a.redeem(self.operator['account'],'','pro',password=self.operator['password'])
  self.write([self.admin,{**self.operator,'code':''}])
  with self.assertRaises(Denied):self.a.redeem(self.operator['account'],'','pro',password=self.operator['password'])
 def test_blank_operator_code_revokes_without_breaking_admin(self):
  op=self.a.redeem(self.operator['account'],self.operator['code'],'pro',password=self.operator['password'])
  self.write([self.admin,{**self.operator,'code':''}])
  self.assertEqual(self.login()['role'],'admin')
  with self.assertRaises(Denied):self.a.device_login(op['device_secret'],'pro')
 def test_password_rotation_revokes_all_old_admin_devices(self):
  old=self.login();self.admin={**self.admin,'password':'new-test-password'};self.write([self.admin,self.operator])
  with self.assertRaises(Denied):self.a.refresh(old['session'],'pro')
  with self.assertRaises(Denied):self.a.device_login(old['device_secret'],'pro')
  self.assertEqual(self.login()['role'],'admin')
 def test_deleted_and_readded_admin_allows_new_login_not_old_device(self):
  old=self.login();self.write([self.operator])
  with self.assertRaises(Denied):self.login()
  self.write([self.admin,self.operator]);self.assertEqual(self.login()['role'],'admin')
  with self.assertRaises(Denied):self.a.device_login(old['device_secret'],'pro')
 def test_admin_code_is_not_a_password_login_dependency(self):
  old=self.login();self.admin={**self.admin,'code':new_credential('owner_qa')['code']};self.write([self.admin,self.operator])
  self.assertEqual(self.a.refresh(old['session'],'pro')['role'],'admin')
 def test_wrong_password_rate_limited_and_persisted(self):
  for _ in range(10):
   with self.assertRaises(Denied):self.login(password='wrong')
  self.a=SimpleAuthority(self.path,clock=lambda:self.now[0])
  with self.assertRaisesRegex(Denied,'5'):self.login()
  self.now[0]+=301;self.assertEqual(self.login()['role'],'admin')
 def test_product_scope_and_missing_password_denied(self):
  for change in ({'product':'ledger'},{'password':None},{'password':''},{'account':'missing'}):
   with self.assertRaises(Denied):self.login(**change)
 def test_parallel_password_login_is_repeatable(self):
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
   results=list(ex.map(lambda _:self.login(),range(4)))
  self.assertEqual(len({r['device_secret'] for r in results}),4)
 def test_client_password_only_protocol_and_resume(self):
  class Store:
   data=None
   def save(self,value):self.data=value
   def load(self):return self.data
   def delete(self):self.data=None
  store=Store();requests=[]
  def transport(req):requests.append(req);return dispatch(self.a,req)
  session=Session(transport,'pro',device_store=store);session.login_admin(self.admin['account'],self.admin['password'],remember_device=True)
  self.assertEqual(requests[0]['action'],'admin_login');self.assertNotIn('code',requests[0]);self.assertTrue(session.allows('accounts'))
  self.assertNotIn('password',store.data);self.assertTrue(Session(transport,'pro',device_store=store).resume())
 def test_admin_client_rejects_operator_response(self):
  op=self.a.redeem(self.operator['account'],self.operator['code'],'pro',password=self.operator['password'])
  session=Session(lambda req:op,'pro')
  with self.assertRaises(AccessDenied):session.login_admin('owner_qa','test')
  self.assertFalse(session.token)
 def test_state_does_not_store_plain_password(self):
  self.login();self.assertNotIn(self.admin['password'],(self.path.parent/'.system/state.csv').read_text(encoding='utf-8-sig'))
if __name__=='__main__':unittest.main()
