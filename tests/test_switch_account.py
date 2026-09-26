"""切号先刷票再写入：刷新失败不关 Cursor。不联网。"""

import base64
import json
import unittest
from unittest.mock import MagicMock, patch

AID = "user_01SWITCHREFRESH0000000000"


def _jwt(sub=AID, typ="session", exp=1893456000):
    payload = {"sub": sub, "type": typ, "exp": exp, "email": "a@b.com"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + encoded + ".x"


OLD = _jwt(exp=1893456000)
NEW = _jwt(exp=1999999999)


class SwitchAccountRefreshFirstTest(unittest.TestCase):
    def setUp(self):
        import app.desktop as app

        self.app = app
        self.api = app.Api.__new__(app.Api)
        self.item = {"token": OLD, "refreshToken": "rt", "label": "a@b.com"}
        self.api._store = MagicMock()
        self.api._store.get.side_effect = lambda _id: dict(self.item)
        self.api.refresh_login_one = MagicMock()
        self.api.probe_refresh_one = MagicMock()
        self.api._drop_stale_tool_session_after_refresh = MagicMock(return_value="")
        self.api.list_sessions = MagicMock(
            return_value={
                "ok": True,
                "sessions": [{"sessionId": "sid-new", "type": "client", "localMark": "local"}],
                "sessionCount": 1,
                "sessionClientCount": 1,
                "sessionWebCount": 0,
                "sessionError": "",
            }
        )
        self.api._resolve_pinned_local_session = MagicMock(return_value="sid-new")
        self.layout = object()

    def _patch_write(self):
        return (
            patch.object(self.app.sand_api, "token_exp", return_value=1999999999),
            patch.object(self.app.sand_api, "probe_token_alive", return_value="alive"),
            patch.object(self.app.sand_patch, "resolve_cursor_layout", return_value=self.layout),
            patch.object(self.app.sand_patch, "close_cursor"),
            patch.object(self.app.local_cursor, "write_local_account"),
            patch.object(self.app.sand_patch, "start_cursor"),
        )

    def test_refresh_first_writes_new_token_then_closes_cursor(self):
        def do_refresh(_aid):
            self.item["token"] = NEW
            return {"ok": True, "tokenType": "session", "accessToken": NEW, "usedAccessAsRefresh": False}

        self.api.refresh_login_one.side_effect = do_refresh
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_write()
        with p_exp, p_alive, p_lay, p_close as close, p_write as write, p_start:
            res = self.api.switch_account(AID, False, True, False)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["refreshed"])
        self.api.refresh_login_one.assert_called_once_with(AID)
        close.assert_called_once()
        write.assert_called_once()
        self.assertEqual(write.call_args[0][0], NEW)
        self.assertEqual(res.get("pinnedSessionId"), "sid-new")
        self.assertEqual(res.get("sessionCount"), 1)
        self.assertNotIn("accessToken", res)
        self.api._drop_stale_tool_session_after_refresh.assert_not_called()

    def test_refresh_failure_does_not_close_cursor(self):
        self.api.refresh_login_one.return_value = {"ok": False, "error": "换票失败"}
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_write()
        with p_exp, p_alive, p_lay, p_close as close, p_write, p_start:
            res = self.api.switch_account(AID, False, True, False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "换票失败")
        close.assert_not_called()
        self.api.refresh_login_one.assert_called_once()

    def test_kick_old_after_successful_refresh(self):
        def do_refresh(_aid):
            self.item["token"] = NEW
            return {"ok": True, "accessToken": NEW}

        self.api.refresh_login_one.side_effect = do_refresh
        self.api._drop_stale_tool_session_after_refresh.return_value = "old-tool-sid"
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_write()
        with p_exp, p_alive, p_lay, p_close, p_write, p_start:
            res = self.api.switch_account(AID, False, True, True)
        self.assertTrue(res["ok"], res)
        self.api._drop_stale_tool_session_after_refresh.assert_called_once()
        self.assertEqual(res.get("droppedSessionId"), "old-tool-sid")

    def test_refresh_first_false_skips_oauth(self):
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_write()
        with p_exp, p_alive, p_lay, p_close, p_write as write, p_start:
            res = self.api.switch_account(AID, False, False, True)
        self.assertTrue(res["ok"], res)
        self.assertFalse(res.get("refreshed"))
        self.api.refresh_login_one.assert_not_called()
        self.api._drop_stale_tool_session_after_refresh.assert_not_called()
        self.assertEqual(write.call_args[0][0], OLD)

    def test_list_sessions_error_still_ok(self):
        def do_refresh(_aid):
            self.item["token"] = NEW
            return {"ok": True, "accessToken": NEW}

        self.api.refresh_login_one.side_effect = do_refresh
        self.api.list_sessions.side_effect = RuntimeError("waf")
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_write()
        with p_exp, p_alive, p_lay, p_close, p_write, p_start:
            res = self.api.switch_account(AID, False, True, False)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res.get("warning"))
        self.assertEqual(res.get("pinnedSessionId") or "", "")

    def test_probe_failure_appends_hint_and_skips_refresh(self):
        self.item.pop("refreshToken")
        self.api.probe_refresh_one.return_value = {"ok": False, "error": "未能探测到 refresh_token"}
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_write()
        with p_exp, p_alive, p_lay, p_close as close, p_write, p_start:
            res = self.api.switch_account(AID, False, True, False)
        self.assertFalse(res["ok"])
        self.assertIn("未能探测到 refresh_token", res["error"])
        self.assertIn("未写入 Cursor", res["error"])
        self.api.refresh_login_one.assert_not_called()
        close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
