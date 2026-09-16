"""refresh_token 探测、号池解析与 OAuth 刷新（mock HTTP，不联网）。"""

import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import accounts
import sand_api


def _jwt(sub="user_01TESTREFRESH00000000000", typ="session", exp=1893456000):
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub, "type": typ, "exp": exp}).encode())
        .decode()
        .rstrip("=")
    )
    return "eyJhbGciOiJub25lIn0." + payload + ".signature"


ACCESS = _jwt()
REFRESH = _jwt(sub="user_01TESTREFRESH00000000000", typ="refresh")
POOL_LINE = f"a@b.com----pwd----client-abc----{REFRESH}----cpwd----{ACCESS}"


class PoolLineParseTest(unittest.TestCase):
    def test_pool_line_from_text(self):
        rows = accounts.pool_lines_from_text(POOL_LINE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "a@b.com")
        self.assertEqual(rows[0]["clientId"], "client-abc")
        self.assertEqual(rows[0]["refreshToken"], REFRESH)
        self.assertEqual(rows[0]["accessToken"], ACCESS)


class AccountStoreRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = accounts._store_path
        accounts._store_path = lambda: os.path.join(self.tmp.name, "accounts.json")

    def tearDown(self):
        accounts._store_path = self._orig
        self.tmp.cleanup()

    def test_import_pool_line_records_refresh(self):
        store = accounts.AccountStore()
        touched = store.add_text(POOL_LINE)
        self.assertEqual(len(touched), 1)
        item = store.get(touched[0]["id"])
        self.assertEqual(item["refreshToken"], REFRESH)
        self.assertEqual(item["clientId"], "client-abc")
        listed = store.list()[0]
        self.assertTrue(listed["hasRefresh"])
        self.assertTrue(listed["hasClientId"])
        self.assertNotIn("token", listed)
        self.assertNotIn("refreshToken", listed)
        views = store.token_views()
        uid = listed["id"]
        self.assertIn("::", views[uid]["worksessionToken"])
        self.assertEqual(views[uid]["refreshToken"], REFRESH)
        self.assertTrue(views[uid]["worksessionToken"].endswith(ACCESS) or ACCESS in views[uid]["worksessionToken"])

    def test_update_login_tokens(self):
        store = accounts.AccountStore()
        store.add_text(ACCESS)
        uid = store.list()[0]["id"]
        new_access = _jwt(exp=1999999999)
        store.set_refresh_credentials(uid, REFRESH, client_id="cid")
        ok = store.update_login_tokens(uid, new_access, refresh_token=REFRESH, client_id="cid")
        self.assertTrue(ok)
        item = store.get(uid)
        self.assertEqual(item["token"], new_access)
        self.assertEqual(item["refreshSource"], "oauth_refresh")


class RefreshLoginTokensTest(unittest.TestCase):
    def test_refresh_success(self):
        new_access = _jwt(exp=2000000000)
        body = json.dumps(
            {"access_token": new_access, "refresh_token": REFRESH, "shouldLogout": False}
        )
        with patch.object(sand_api, "_post", return_value=(200, body)):
            out = sand_api.refresh_login_tokens(REFRESH, "test-client")
        self.assertTrue(out["ok"])
        self.assertEqual(out["accessToken"], new_access)
        self.assertEqual(out["tokenType"], "session")
        self.assertEqual(out["exp"], 2000000000)

    def test_refresh_should_logout(self):
        body = json.dumps({"shouldLogout": True})
        with patch.object(sand_api, "_post", return_value=(200, body)):
            out = sand_api.refresh_login_tokens(REFRESH)
        self.assertFalse(out["ok"])
        self.assertTrue(out.get("shouldLogout"))

    def test_refresh_http_error(self):
        body = json.dumps({"error": "invalid_grant"})
        with patch.object(sand_api, "_post", return_value=(400, body)):
            out = sand_api.refresh_login_tokens(REFRESH)
        self.assertFalse(out["ok"])
        self.assertIn("400", out["error"])


if __name__ == "__main__":
    unittest.main()
