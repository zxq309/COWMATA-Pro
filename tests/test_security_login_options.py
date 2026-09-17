import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cowmata_security.authority import Denied
from cowmata_security.client import AccessDenied, Session
from cowmata_security.device_store import DeviceStore
from cowmata_security.protocol import dispatch
from cowmata_security.simple_authority import (
    SimpleAuthority,
    encode_registry,
    initialize_registry,
    new_credential,
)


class LoginOptionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = [10000.0]
        self.admin = {**new_credential("admin"), "code": ""}
        self.op = new_credential("operator01")
        initialize_registry(self.root / "authority.csv", "pro", [self.admin, self.op])
        self.a = SimpleAuthority(self.root / "authority.csv", clock=lambda: self.now[0])
        self.store = DeviceStore("pro", self.root / "device.bin")

    def transport(self, req):
        try:
            return dispatch(self.a, req)
        except Denied as e:
            raise AccessDenied(str(e)) from e

    def session(self):
        return Session(self.transport, "pro", device_store=self.store)

    def test_admin_default_does_not_save_device(self):
        s = self.session()
        s.login_admin("admin", self.admin["password"])
        self.assertFalse(self.store.path.exists())

    def test_admin_explicit_auto_login_can_persist_device(self):
        s = self.session()
        s.login_admin("admin", self.admin["password"], remember_device=True)
        self.assertTrue(self.store.path.exists())
        self.assertTrue(self.session().resume())

    def test_manual_admin_login_removes_old_automatic_device(self):
        s = self.session()
        s.login_admin("admin", self.admin["password"], remember_device=True)
        s.login_admin("admin", self.admin["password"])
        self.assertFalse(self.store.path.exists())

    def test_operator_manual_reopen_uses_password_and_existing_activation(self):
        s = self.session()
        s.login("operator01", self.op["code"], password=self.op["password"])
        reopened = self.session()
        reopened.login("operator01", "", password=self.op["password"])
        self.assertEqual(reopened.identity["role"], "operator")
        with self.assertRaises(AccessDenied):
            self.session().login("operator01", "", password="wrong")

    def test_operator_needs_initial_code_on_new_pc(self):
        with self.assertRaises(AccessDenied):
            self.session().login("operator01", "", password=self.op["password"])

    def test_saved_device_cannot_be_used_as_other_account(self):
        s = self.session()
        s.login("operator01", self.op["code"], password=self.op["password"])
        with self.assertRaises(AccessDenied):
            self.session().login("admin", "", password=self.admin["password"])

    def test_manual_operator_still_respects_revocation(self):
        s = self.session()
        s.login("operator01", self.op["code"], password=self.op["password"])
        (self.root / "authority.csv").write_bytes(encode_registry([self.admin]))
        with self.assertRaises(AccessDenied):
            self.session().login("operator01", "", password=self.op["password"])

    def test_admin_24_hour_active_session_without_saved_device(self):
        s = self.session()
        s.login_admin("admin", self.admin["password"])
        token = s.token
        for _ in range(24):
            self.now[0] += 3600
            self.assertTrue(s.refresh())
            self.assertTrue(s.allows("dataset"))
        self.assertEqual(s.token, token)
        self.assertFalse(self.store.path.exists())

    def test_expired_admin_session_not_resurrected_without_auto_login(self):
        s = self.session()
        s.login_admin("admin", self.admin["password"])
        self.now[0] += 8 * 3600
        with self.assertRaises(AccessDenied):
            s.refresh()

    def test_saved_password_is_dpapi_encrypted_and_cleared(self):
        from cowmata_security.login_preferences import LoginPreferences

        prefs = LoginPreferences("pro", self.root / "preferences.bin")
        self.assertFalse(prefs.load()["auto_login"])
        prefs.save(
            {
                "account": "admin",
                "role": "admin",
                "password": "qa-saved-password",
                "remember_password": True,
                "auto_login": False,
            }
        )
        self.assertNotIn(b"qa-saved-password", prefs.path.read_bytes())
        self.assertEqual(prefs.load()["password"], "qa-saved-password")
        prefs.save({**prefs.load(), "remember_password": False, "auto_login": False})
        self.assertEqual(prefs.load()["password"], "")

    def test_auto_login_cannot_be_saved_without_password_consent(self):
        from cowmata_security.login_preferences import LoginPreferences

        prefs = LoginPreferences("pro", self.root / "preferences.bin")
        prefs.save(
            {
                "account": "admin",
                "role": "admin",
                "password": "not-saved",
                "remember_password": False,
                "auto_login": True,
            }
        )
        self.assertFalse(prefs.load()["auto_login"])
        self.assertEqual(prefs.load()["password"], "")


if __name__ == "__main__":
    unittest.main()
