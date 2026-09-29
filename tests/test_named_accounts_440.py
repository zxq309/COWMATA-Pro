"""4.4.0: named accounts (8-char password + one-time key) and Annotator uploads."""
import json
import tempfile
import unittest
from pathlib import Path

from cowmata_security.authority import Denied
from cowmata_security.receiver_adapter import handle_security, ledger_principal
from cowmata_security.simple_authority import (
    PASSWORD_ALPHABET, SimpleAuthority, initialize_registry, new_credential, new_password, parse_registry,
)


class NamedAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.admin = new_credential('admin')
        self.operator = new_credential('operator01')
        self.paths = {'pro': root / 'Pro' / 'authority.csv', 'ledger': root / 'Uploader' / 'authority.csv'}
        for product, path in self.paths.items():
            initialize_registry(path, product, [self.admin, self.operator])
        self.config = {'security_csv': {k: str(v) for k, v in self.paths.items()}, 'security_required': True}

    def call(self, product, request):
        reply = handle_security({'request': {'product': product, **request}}, self.config, 'test')
        self.assertTrue(reply['ok'], reply.get('error'))
        return reply['result']

    def admin_session(self, product='pro'):
        return self.call(product, {'action': 'admin_login', 'account': 'admin', 'password': self.admin['password']})['session']

    def test_password_is_eight_unambiguous_characters(self):
        for _ in range(200):
            value = new_password()
            self.assertEqual(len(value), 8)
            self.assertTrue(set(value) <= set(PASSWORD_ALPHABET))
            self.assertTrue(any(c.isupper() for c in value) and any(c.islower() for c in value) and any(c.isdigit() for c in value))

    def test_create_named_account_mirrors_to_uploader_and_signs_in_to_both(self):
        created = self.call('pro', {'action': 'create_account', 'session': self.admin_session(), 'account': 'zhangxiangqing'})
        item = created['created']
        self.assertEqual(item['account'], 'zhangxiangqing')
        self.assertEqual(len(item['password']), 8)
        self.assertEqual(created['mirror'], {'ledger': 'added'})
        for product, path in self.paths.items():
            rows = {r['account']: r for r in parse_registry(path.read_bytes())}
            self.assertEqual(rows['zhangxiangqing'], item)
            reply = SimpleAuthority(path, product=product).redeem('zhangxiangqing', item['code'], product, password=item['password'])
            self.assertEqual(reply['role'], 'operator')

    def test_one_time_key_is_single_use(self):
        item = self.call('pro', {'action': 'create_account', 'session': self.admin_session(), 'account': 'luoyufan'})['created']
        authority = SimpleAuthority(self.paths['pro'], product='pro')
        authority.redeem('luoyufan', item['code'], 'pro', password=item['password'])
        with self.assertRaises(Denied):
            authority.redeem('luoyufan', item['code'], 'pro', password=item['password'])

    def test_duplicate_invalid_and_operator_requests_are_rejected(self):
        session = self.admin_session()
        self.call('pro', {'action': 'create_account', 'session': session, 'account': 'zhaoyachen'})
        for account in ('zhaoyachen', 'Zhao', 'x', '1abc', ''):
            reply = handle_security({'request': {'product': 'pro', 'action': 'create_account', 'session': session, 'account': account}}, self.config, 'test')
            self.assertFalse(reply['ok'], account)
        operator = self.call('pro', {'action': 'redeem', 'account': 'operator01', 'code': self.operator['code'],
                                     'password': self.operator['password']})['session']
        reply = handle_security({'request': {'product': 'pro', 'action': 'create_account', 'session': operator, 'account': 'jiaotengyu'}}, self.config, 'test')
        self.assertFalse(reply['ok'])

    def test_remove_mirrors_but_never_removes_owner(self):
        session = self.admin_session()
        self.call('pro', {'action': 'create_account', 'session': session, 'account': 'liuyanping'})
        removed = self.call('pro', {'action': 'remove_account', 'session': session, 'account': 'liuyanping'})
        self.assertEqual(removed['mirror'], {'ledger': 'removed'})
        for path in self.paths.values():
            self.assertNotIn('liuyanping', {r['account'] for r in parse_registry(path.read_bytes())})
            self.assertIn('admin', {r['account'] for r in parse_registry(path.read_bytes())})

    def test_same_name_with_other_credentials_is_never_touched(self):
        # <=4.3.9 batch_create filled each registry separately: operator05 may be two different people.
        other = new_credential('operator05')
        from cowmata_security.simple_authority import encode_registry
        self.paths['ledger'].write_bytes(encode_registry(parse_registry(self.paths['ledger'].read_bytes()) + [other]))
        pro_rows = parse_registry(self.paths['pro'].read_bytes()) + [new_credential('operator05')]
        self.paths['pro'].write_bytes(encode_registry(pro_rows))
        session = self.admin_session()
        removed = self.call('pro', {'action': 'remove_account', 'session': session, 'account': 'operator05'})
        self.assertEqual(removed['mirror'], {'ledger': 'conflict'})
        self.assertIn(other, parse_registry(self.paths['ledger'].read_bytes()))
        # Same the other way: creating a name that exists with other credentials is reported, not merged.
        created = self.call('ledger', {'action': 'create_account', 'session': self.admin_session('ledger'), 'account': 'zhangjianqi'})
        self.assertEqual(created['mirror'], {'pro': 'added'})
        self.call('pro', {'action': 'remove_account', 'session': session, 'account': 'zhangjianqi'})
        rows = parse_registry(self.paths['pro'].read_bytes())
        self.assertNotIn('zhangjianqi', {r['account'] for r in rows})

    def test_pro_session_may_upload_with_upload_capability(self):
        operator = self.call('pro', {'action': 'redeem', 'account': 'operator01', 'code': self.operator['code'],
                                     'password': self.operator['password']})['session']
        for action in ('pull', 'inventory', 'sync'):
            principal = ledger_principal({'action': action, 'session_token': operator, 'security_product': 'pro'}, self.config)
            self.assertEqual(principal['username'], 'operator01')
        with self.assertRaises(Denied):
            ledger_principal({'action': 'sync', 'session_token': 'forged' * 8, 'security_product': 'pro'}, self.config)
        json.dumps(principal)


if __name__ == '__main__':
    unittest.main()