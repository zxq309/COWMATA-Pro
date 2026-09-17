import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication, QPushButton

from cowmata_security import qt_ui as m

app = QApplication.instance() or QApplication([])


class FakePreferences:
    def __init__(self, value=None):
        from cowmata_security.login_preferences import DEFAULTS

        self.value = {**DEFAULTS, **(value or {})}

    def load(self):
        return dict(self.value)

    def save(self, value):
        self.value = dict(value)
        if not self.value["remember_password"]:
            self.value["password"] = ""
            self.value["auto_login"] = False


class FakeSession:
    product = "pro"
    identity = {"account": "admin", "role": "admin"}

    def __init__(self):
        self.calls = []
        self.device_store = types.SimpleNamespace(
            path=Path(tempfile.gettempdir()) / "cowmata-nonexistent-ui-test-device"
        )

    def require(self, cap):
        assert cap == "accounts"

    def login(self, account, code, *, password):
        self.calls.append(("operator", account, password, code))
        return self.identity

    def login_admin(self, account, password, *, remember_device=False):
        self.calls.append(("admin", account, password, remember_device))
        return self.identity

    def resume(self):
        self.calls.append("resume")
        return True

    def admin(self, action, **values):
        self.calls.append((action, values))
        return {
            "credentials": [
                {"account": "admin", "password": "test-only-password", "code": ""},
                {"account": "operator", "password": "test2", "code": "test3"},
            ]
        }


def wait(d):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        app.processEvents()
        if d.task and not d.task.isRunning():
            app.processEvents()
            return
        time.sleep(0.005)
    raise AssertionError("UI task did not finish")


class Tests(unittest.TestCase):
    def dialog(self):
        d = m.LoginDialog(".", "pro", preferences=FakePreferences())
        s = FakeSession()
        d.make_session = lambda: s
        return d, s

    def test_admin_only_two_visible_fields_and_one_button(self):
        d, s = self.dialog()
        d.show()
        app.processEvents()
        self.assertEqual(d.mode.currentData(), "admin")
        self.assertTrue(d.account.isVisible())
        self.assertTrue(d.password.isVisible())
        self.assertFalse(d.code.isVisible())
        self.assertFalse(d.form.labelForField(d.code).isVisible())
        self.assertEqual([b.text() for b in d.findChildren(QPushButton)], ["忘记密码", "登录"])
        self.assertFalse(d.remember.isChecked())
        self.assertFalse(d.automatic.isChecked())
        d.reject()

    def test_admin_submit_without_code_and_clear_password(self):
        d, s = self.dialog()
        d.account.setText("admin")
        d.password.setText("secret")
        d.submit()
        wait(d)
        self.assertIn(("admin", "admin", "secret", False), s.calls)
        self.assertTrue(d.ready)
        self.assertEqual(d.password.text(), "")
        self.assertEqual(d.code.text(), "")

    def test_operator_mode_requires_and_transmits_all_three_fields(self):
        d, s = self.dialog()
        d.mode.setCurrentIndex(1)
        d.show()
        app.processEvents()
        self.assertTrue(d.code.isVisible())
        d.account.setText("operator01")
        d.password.setText("pw")
        d.code.setText("code")
        d.submit()
        wait(d)
        self.assertIn(("operator", "operator01", "pw", "code"), s.calls)
        self.assertEqual(d.code.text(), "")

    def test_mode_switch_discards_hidden_code(self):
        d, s = self.dialog()
        d.mode.setCurrentIndex(1)
        d.code.setText("code")
        d.mode.setCurrentIndex(0)
        self.assertEqual(d.code.text(), "")
        d.reject()

    def test_missing_admin_password_does_not_start_request(self):
        d, s = self.dialog()
        d.account.setText("admin")
        d.submit()
        self.assertIsNone(d.task)
        d.reject()

    def test_busy_disables_mode_selection(self):
        d, s = self.dialog()
        d.set_busy(True)
        self.assertFalse(d.mode.isEnabled())
        d.set_busy(False)
        d.reject()

    def test_table_headers_and_batch_count(self):
        s = FakeSession()
        d = m.AdminDialog(session=s)
        wait(d)
        self.assertEqual(
            [d.table.horizontalHeaderItem(i).text() for i in range(3)], ["账号", "密码", "授权码"]
        )
        self.assertEqual(d.table.item(0, 2).text(), "")
        d.count.setValue(37)
        d.batch_create()
        wait(d)
        self.assertIn(("batch_create", {"count": 37}), s.calls)
        d.reject()

    def test_cannot_remove_self(self):
        s = FakeSession()
        d = m.AdminDialog(session=s)
        wait(d)
        d.table.selectRow(0)
        d.remove_selected()
        self.assertFalse(d.buttons[2].isEnabled())
        d.reject()

    def test_legacy_device_does_not_trigger_automatic_login(self):
        d, s = self.dialog()
        d.make_session = lambda: (_ for _ in ()).throw(AssertionError("Unexpected automatic login"))
        app.processEvents()
        self.assertIsNone(d.task)
        d.reject()

    def test_saved_password_prefills_but_does_not_submit(self):
        prefs = FakePreferences(
            {"account": "admin", "password": "saved", "remember_password": True}
        )
        d = m.LoginDialog(".", "pro", preferences=prefs)
        s = FakeSession()
        d.make_session = lambda: s
        app.processEvents()
        self.assertEqual(d.account.text(), "admin")
        self.assertEqual(d.password.text(), "saved")
        self.assertIsNone(d.task)
        d.reject()

    def test_explicit_auto_login_submits_saved_password(self):
        prefs = FakePreferences(
            {"account": "admin", "password": "saved", "remember_password": True, "auto_login": True}
        )
        d = m.LoginDialog(".", "pro", preferences=prefs)
        s = FakeSession()
        d.make_session = lambda: s
        wait(d)
        self.assertIn(("admin", "admin", "saved", True), s.calls)
        self.assertTrue(d.ready)

    def test_auto_login_implies_remember_password(self):
        d, s = self.dialog()
        d.automatic.setChecked(True)
        self.assertTrue(d.remember.isChecked())
        d.remember.setChecked(False)
        self.assertFalse(d.automatic.isChecked())
        d.reject()

    def test_unchecking_remember_deletes_saved_password(self):
        prefs = FakePreferences(
            {"account": "admin", "password": "saved", "remember_password": True, "auto_login": True}
        )
        d = m.LoginDialog(".", "pro", preferences=prefs)
        d.remember.setChecked(False)
        self.assertEqual(prefs.load()["password"], "")
        self.assertFalse(prefs.load()["auto_login"])
        d.reject()

    def test_lock_dialog_does_not_auto_login(self):
        prefs = FakePreferences(
            {"account": "admin", "password": "saved", "remember_password": True, "auto_login": True}
        )
        d = m.LoginDialog(".", "pro", username="admin", preferences=prefs)
        app.processEvents()
        self.assertIsNone(d.task)
        d.reject()

    def test_forgot_password_does_not_authenticate(self):
        d, s = self.dialog()
        d.password_help()
        app.processEvents()
        self.assertEqual(d.recovery_dialog.windowTitle(), "忘记密码")
        self.assertIsNone(d.task)
        self.assertFalse(d.ready)
        d.recovery_dialog.reject()
        d.reject()


if __name__ == "__main__":
    unittest.main()
