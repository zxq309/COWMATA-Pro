import sys,csv,tempfile,unittest,concurrent.futures,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cowmata_security.simple_authority import SimpleAuthority,initialize_registry,new_credential,encode_registry,parse_registry
from cowmata_security.authority import Denied
from cowmata_security.protocol import dispatch
from cowmata_security.client import Session
class SimpleTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'authority.csv';self.now=[10000.]
  self.admin=new_credential('admin');self.operator=new_credential('operator01');initialize_registry(self.path,'pro',[self.admin,self.operator])
  self.a=SimpleAuthority(self.path,clock=lambda:self.now[0]);self.owner=self.login(self.admin)
 def login(self,item,authority=None):
  return (authority or self.a).redeem(item['account'],item['code'],'pro',password=item['password'])
 def write(self,items):self.path.write_bytes(encode_registry(items))
 def test_registry_only_three_columns_and_plain_credentials_visible(self):
  with self.path.open(encoding='utf-8-sig') as f:reader=csv.DictReader(f);self.assertEqual(reader.fieldnames,['账号','密码','授权码']);rows=list(reader)
  self.assertEqual(rows[0]['密码'],self.admin['password']);self.assertEqual(rows[1]['授权码'],self.operator['code'])
 def test_all_three_fields_required(self):
  for pw,code in [(None,self.operator['code']),('wrong',self.operator['code']),(self.operator['password'],'wrong')]:
   with self.assertRaises(Denied):self.a.redeem('operator01',code,'pro',password=pw)
 def test_batch_requires_owner(self):
  operator=self.login(self.operator)
  for action in ('credentials','batch_create'):
   with self.assertRaises(Denied):dispatch(self.a,{'action':action,'session':operator['session'],'product':'pro'})
 def test_batch_is_visible_in_same_csv(self):
  result=self.a.batch_create(self.owner['session'],10);self.assertEqual(len(result['credentials']),12)
  self.assertEqual(len(parse_registry(self.path.read_bytes())),12)
  for row in result['credentials'][2:]:self.login(row)
 def test_delete_row_revokes_existing_device_and_session(self):
  reply=self.login(self.operator);self.write([self.admin])
  for call in (lambda:self.a.refresh(reply['session'],'pro'),lambda:self.a.device_login(reply['device_secret'],'pro')):
   with self.assertRaises(Denied):call()
 def test_readding_deleted_row_does_not_restore_old_device(self):
  reply=self.login(self.operator);self.write([self.admin])
  with self.assertRaises(Denied):self.a.refresh(reply['session'],'pro')
  self.write([self.admin,self.operator])
  with self.assertRaises(Denied):self.a.device_login(reply['device_secret'],'pro')
 def test_password_change_revokes(self):
  reply=self.login(self.operator);changed={**self.operator,'password':'changed-password-123'};self.write([self.admin,changed])
  with self.assertRaises(Denied):self.a.refresh(reply['session'],'pro')
 def test_code_change_revokes_and_new_code_can_activate(self):
  reply=self.login(self.operator);changed=new_credential('operator01');self.write([self.admin,changed])
  with self.assertRaises(Denied):self.a.device_login(reply['device_secret'],'pro')
  self.assertEqual(self.login(changed)['account'],'operator01')
 def test_stockpiled_code_stays_available(self):
  self.now[0]+=365*86400;self.assertEqual(self.login(self.operator)['role'],'operator')
 def test_single_use_concurrent(self):
  def attempt(_):
   try:self.login(self.operator,SimpleAuthority(self.path,clock=lambda:self.now[0]));return True
   except Denied:return False
  with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:self.assertEqual(sum(ex.map(attempt,range(8))),1)
 def test_survives_service_restart(self):
  self.login(self.operator)
  with self.assertRaises(Denied):self.login(self.operator,SimpleAuthority(self.path))
 def test_device_login_does_not_consume_new_code_daily(self):
  reply=self.login(self.operator);self.now[0]+=60*86400
  self.assertEqual(self.a.device_login(reply['device_secret'],'pro')['account'],'operator01')
 def test_owner_cannot_be_deleted(self):
  with self.assertRaises(Denied):self.a.remove_account(self.owner['session'],'admin')
 def test_owner_deletes_operator(self):
  reply=self.login(self.operator);result=self.a.remove_account(self.owner['session'],'operator01');self.assertEqual(len(result['credentials']),1)
  with self.assertRaises(Denied):self.a.refresh(reply['session'],'pro')
 def test_duplicate_name_or_code_rejected(self):
  for value in ([self.admin,self.admin],[self.admin,{**self.operator,'code':self.admin['code']}]):
   with self.assertRaises(ValueError):encode_registry(value)
 def test_malformed_or_missing_csv_denies(self):
  self.path.write_text('wrong,header')
  with self.assertRaises(ValueError):self.a.refresh(self.owner['session'],'pro')
  self.path.unlink()
  with self.assertRaises(Denied):self.a.refresh(self.owner['session'],'pro')
 def test_operator_cannot_delete(self):
  reply=self.login(self.operator)
  with self.assertRaises(Denied):self.a.remove_account(reply['session'],'admin')
 def test_client_protocol_three_fields(self):
  session=Session(lambda req:dispatch(self.a,req),'pro');session.login(self.operator['account'],self.operator['code'],password=self.operator['password'])
  self.assertTrue(session.allows('annotate'));self.assertFalse(session.allows('accounts'))
 def test_internal_state_does_not_duplicate_plain_secrets(self):
  self.login(self.operator);raw=(self.path.parent/'.system/state.csv').read_text(encoding='utf-8-sig')
  for item in (self.admin,self.operator):
   self.assertNotIn(item['password'],raw);self.assertNotIn(item['code'],raw)
 def test_oversized_account_is_rejected_without_persisting_it(self):
  path=self.path.parent/'.system/state.csv';before=path.read_bytes()
  with self.assertRaises(Denied):self.a.redeem('a'*100000,self.operator['code'],'pro',password=self.operator['password'])
  self.assertEqual(path.read_bytes(),before)
 def test_foreign_product_denied(self):
  with self.assertRaises(Denied):self.a.redeem('operator01',self.operator['code'],'ledger',password=self.operator['password'])
if __name__=='__main__':unittest.main(verbosity=2)
