import concurrent.futures
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cowmata_security.client import AccessDenied, Session
from cowmata_security.receiver_adapter import handle_security
from cowmata_security.simple_authority import (
    SimpleAuthority,
    encode_registry,
    initialize_registry,
    parse_registry,
)


class RegistryEncodingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "authority.csv"
        self.items = [{"account": "admin", "password": "test-中文-password", "code": ""}]
        initialize_registry(self.path, "pro", self.items)
        self.config = {"security_csv": {"pro": str(self.path)}}

    def request(self, password="test-中文-password", **extra):
        return handle_security(
            {
                "request": {
                    "action": "admin_login",
                    "product": "pro",
                    "account": "admin",
                    "password": password,
                    **extra,
                }
            },
            self.config,
            "qa",
        )

    def test_excel_gbk_end_to_end_login(self):
        self.path.write_bytes(encode_registry(self.items).decode("utf-8-sig").encode("gbk"))
        reply = self.request()
        self.assertTrue(reply["ok"], reply.get("error"))
        self.assertEqual(reply["result"]["role"], "admin")

    def test_utf8_bom_and_no_bom_preserve_values(self):
        for encoding in ("utf-8", "utf-8-sig", "gb18030"):
            self.assertEqual(
                parse_registry(encode_registry(self.items).decode("utf-8-sig").encode(encoding)),
                self.items,
            )

    def test_gbk_still_requires_exact_three_columns(self):
        for header in ("账号,密码,授权码,角色", "账号,密码", "账号,密码,密码"):
            self.path.write_bytes((header + "\nadmin,test-中文-password,\n").encode("gbk"))
            self.assertFalse(self.request()["ok"])

    def test_four_admin_computers_remain_active_after_each_login(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda i: self.request(device_label="QA-PC-" + str(i)), range(4)))
        a = SimpleAuthority(self.path, product="pro")
        self.assertEqual(len({r["result"]["session"] for r in results}), 4)
        for r in results:
            self.assertEqual(a.refresh(r["result"]["session"], "pro")["role"], "admin")

    def test_client_copied_registry_cannot_authorize_server_login(self):
        forged = Path(self.tmp.name) / "copied" / "authority.csv"
        forged.parent.mkdir()
        forged.write_bytes(
            encode_registry([{"account": "admin", "password": "forged", "code": ""}])
        )

        def transport(req):
            r = handle_security({"request": req}, self.config, "qa")
            if not r["ok"]:
                raise AccessDenied(r["error"])
            return r["result"]

        session = Session(transport, "pro")
        with self.assertRaises(AccessDenied):
            session.login_admin("admin", "forged")
        self.assertFalse(session.token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
