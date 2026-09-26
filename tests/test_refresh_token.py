"""refresh_token 探测、号池解析与 OAuth 刷新（mock HTTP，不联网）。"""

import base64
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app import accounts
from app import sand_api


def _jwt(sub="user_01TESTREFRESH00000000000", typ="session", exp=1893456000, time=None):
    payload = {"sub": sub, "type": typ, "exp": exp}
    if time is not None:
        payload["time"] = str(time)
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode())
        .decode()
        .rstrip("=")
    )
    return "eyJhbGciOiJub25lIn0." + encoded + ".signature"


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
        self.assertEqual(out["refreshToken"], REFRESH)
        self.assertEqual(out["tokenType"], "session")
        self.assertEqual(out["exp"], 2000000000)

    def test_missing_refresh_in_response_uses_new_access(self):
        new_access = _jwt(exp=2000000000)
        body = json.dumps(
            {"access_token": new_access, "id_token": "x", "shouldLogout": False}
        )
        with patch.object(sand_api, "_post", return_value=(200, body)):
            out = sand_api.refresh_login_tokens(REFRESH, "test-client")
        self.assertTrue(out["ok"])
        self.assertEqual(out["accessToken"], new_access)
        self.assertEqual(out["refreshToken"], new_access)

    def test_refresh_should_logout(self):
        body = json.dumps({"access_token": "", "id_token": "", "shouldLogout": True})
        with patch.object(sand_api, "_post", return_value=(200, body)):
            out = sand_api.refresh_login_tokens(REFRESH)
        self.assertFalse(out["ok"])
        self.assertTrue(out.get("shouldLogout"))

    def test_fallback_uses_access_when_refresh_should_logout(self):
        new_access = _jwt(exp=2000000001)
        dead = json.dumps({"access_token": "", "id_token": "", "shouldLogout": True})
        ok = json.dumps({"access_token": new_access, "id_token": "x", "shouldLogout": False})
        with patch.object(sand_api, "_post", side_effect=[(200, dead), (200, ok)]) as post:
            out = sand_api.refresh_login_tokens_with_fallback(REFRESH, ACCESS, "test-client")
        self.assertTrue(out["ok"])
        self.assertTrue(out.get("usedAccessAsRefresh"))
        self.assertEqual(out["accessToken"], new_access)
        self.assertEqual(out["refreshToken"], new_access)
        self.assertEqual(post.call_count, 2)

    def test_fallback_skips_retry_when_refresh_is_already_access(self):
        body = json.dumps({"access_token": "", "id_token": "", "shouldLogout": True})
        with patch.object(sand_api, "_post", return_value=(200, body)) as post:
            out = sand_api.refresh_login_tokens_with_fallback(ACCESS, ACCESS)
        self.assertFalse(out["ok"])
        self.assertEqual(post.call_count, 1)

    def test_refresh_http_error(self):
        body = json.dumps({"error": "invalid_grant"})
        with patch.object(sand_api, "_post", return_value=(400, body)):
            out = sand_api.refresh_login_tokens(REFRESH)
        self.assertFalse(out["ok"])
        self.assertIn("400", out["error"])


class RefreshLoginDropsOldToolSessionTest(unittest.TestCase):
    AID = "user_01TESTREFRESH00000000000"
    OLD_SID = "aa" * 32
    NEW_SID = "bb" * 32
    IDE_SID = "cc" * 32
    OLD_ISO = "2026-09-16T11:25:18.000Z"
    NEW_ISO = "2026-09-16T14:42:11.000Z"

    def setUp(self):
        import app.desktop as app
        from app import login_detect

        self.app = app
        self.old_time = login_detect.created_at_ms(self.OLD_ISO) // 1000
        self.new_time = login_detect.created_at_ms(self.NEW_ISO) // 1000
        self.old_tok = _jwt(time=self.old_time)
        self.new_tok = _jwt(exp=2000000002, time=self.new_time)
        self.api = app.Api.__new__(app.Api)
        self.api._store = MagicMock()
        self.api._store.get.return_value = {
            "token": self.old_tok,
            "refreshToken": REFRESH,
            "clientId": "cid",
        }
        self.api._store.list.return_value = []
        self.api._store.update_login_tokens = MagicMock(return_value=True)
        self.api.local_identity = MagicMock(return_value={"ok": False})
        self.api._resolve_pinned_local_session = MagicMock(return_value="")
        self.api.list_sessions = MagicMock(
            return_value={
                "ok": True,
                "sessions": [
                    {
                        "sessionId": self.OLD_SID,
                        "type": "client",
                        "typeRaw": "SESSION_TYPE_CLIENT",
                        "createdAt": self.OLD_ISO,
                    },
                    {
                        "sessionId": self.NEW_SID,
                        "type": "client",
                        "typeRaw": "SESSION_TYPE_CLIENT",
                        "createdAt": self.NEW_ISO,
                    },
                ],
            }
        )
        self.api.revoke_session = MagicMock(return_value={"ok": True})
        self.api._guard = MagicMock()
        self.api._guard.is_running.return_value = False
        self._oauth_ok = {
            "ok": True,
            "accessToken": self.new_tok,
            "refreshToken": self.new_tok,
            "tokenType": "session",
            "clientId": "cid",
        }

    def test_plain_refresh_does_not_kick(self):
        with patch.object(
            sand_api, "refresh_login_tokens_with_fallback", return_value=self._oauth_ok
        ) as refresh:
            res = self.api.refresh_login_one(self.AID)
        self.assertTrue(res["ok"], res)
        refresh.assert_called_once()
        self.api._store.update_login_tokens.assert_called_once()
        self.api.revoke_session.assert_not_called()
        self.assertFalse(res.get("droppedSessionId"))

    def test_kick_old_refreshes_then_drops_tool_session(self):
        with patch.object(
            sand_api, "refresh_login_tokens_with_fallback", return_value=self._oauth_ok
        ) as refresh:
            res = self.api.refresh_login_kick_old(self.AID)
        self.assertTrue(res["ok"], res)
        refresh.assert_called_once()
        self.api._store.update_login_tokens.assert_called_once()
        self.api.revoke_session.assert_called_once_with(
            self.AID, self.OLD_SID, "SESSION_TYPE_CLIENT"
        )
        self.assertEqual(res.get("droppedSessionId"), self.OLD_SID)

    def test_kick_old_skips_revoke_when_refresh_fails(self):
        with patch.object(
            sand_api,
            "refresh_login_tokens_with_fallback",
            return_value={"ok": False, "error": "换票失败"},
        ):
            res = self.api.refresh_login_kick_old(self.AID)
        self.assertFalse(res["ok"], res)
        self.api._store.update_login_tokens.assert_not_called()
        self.api.revoke_session.assert_not_called()
        self.assertFalse(res.get("droppedSessionId"))

    def test_kick_old_does_not_drop_ide_session(self):
        self.api.local_identity.return_value = {"ok": True, "userId": self.AID}
        self.api._resolve_pinned_local_session.return_value = self.OLD_SID
        with patch.object(
            sand_api, "refresh_login_tokens_with_fallback", return_value=self._oauth_ok
        ):
            res = self.api.refresh_login_kick_old(self.AID)
        self.assertTrue(res["ok"], res)
        self.api.revoke_session.assert_not_called()
        self.assertEqual(res.get("droppedSessionId") or "", "")


if __name__ == "__main__":
    unittest.main()
