"""一键本机保护：校本机登录、JWT 认 IDE、必要时留本工具会话、再开保护。不联网。"""

import unittest
from unittest.mock import MagicMock, patch

import app
import login_detect


AID = "user_01PINLOCAL00000000000000000"
OTHER = "user_01OTHER0000000000000000000"
SID_A = "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"
SID_B = "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"
SID_C = "cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333"
OLD = "2026-08-20T04:12:00.000Z"
NEW = "2026-09-16T12:00:00.000Z"


def _client(sid, created):
    return {
        "sessionId": sid,
        "type": "client",
        "typeRaw": "SESSION_TYPE_CLIENT",
        "createdAt": created,
    }


def _web(sid, created):
    return {
        "sessionId": sid,
        "type": "web",
        "typeRaw": "SESSION_TYPE_WEB",
        "createdAt": created,
    }


def _block(rows, ok=True, error=""):
    return {
        "ok": ok,
        "error": error,
        "sessions": list(rows),
        "sessionCount": len(rows),
        "sessionError": error,
    }


class DeviceGuardPinLocalTest(unittest.TestCase):
    def setUp(self):
        self.api = app.Api.__new__(app.Api)
        self.api._store = MagicMock()
        self.api._store.get.return_value = {"refreshToken": "rt", "token": "tok"}
        self.api._guard = MagicMock()
        self.api._window = None
        self.api.local_identity = MagicMock(return_value={"ok": True, "userId": AID, "email": "a@b.c"})
        self.api.probe_refresh_one = MagicMock(return_value={"ok": True})
        self.api.refresh_login_one = MagicMock(return_value={"ok": True, "tokenType": "session"})
        self.api.device_guard_start = MagicMock(
            return_value={"ok": True, "status": {"running": True, "keepIds": [SID_A]}}
        )
        self.api._remember_local_session = MagicMock()
        self.api._resolve_pinned_local_session = MagicMock(return_value=SID_B)
        self.sessions = [_client(SID_A, OLD), _client(SID_B, OLD), _web("w1", OLD)]
        self.api.list_sessions = MagicMock(return_value=_block(self.sessions))

    def test_not_local_account_aborts(self):
        self.api.local_identity.return_value = {"ok": True, "userId": OTHER, "email": "x@y.z"}
        res = self.api.device_guard_pin_local(AID)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "请先在本机 Cursor 登录这个号")
        self.api.list_sessions.assert_not_called()
        self.api.refresh_login_one.assert_not_called()
        self.api.device_guard_start.assert_not_called()

    def test_no_ide_match_aborts_without_refresh(self):
        self.api._resolve_pinned_local_session.return_value = ""
        res = self.api.device_guard_pin_local(AID)
        self.assertFalse(res["ok"])
        self.assertIn("认不出本机", res["error"])
        self.api.refresh_login_one.assert_not_called()
        self.api.device_guard_start.assert_not_called()
        self.api._remember_local_session.assert_not_called()

    def test_keeps_ide_session_not_refresh_delta(self):
        with patch.object(app, "parse_token", return_value=(AID, "tok", {"time": "1"})), patch.object(
            login_detect, "match_session_id_by_jwt_time", return_value=SID_C
        ):
            rows = [_client(SID_A, OLD), _client(SID_B, OLD), _client(SID_C, NEW), _web("w1", OLD)]
            self.api.list_sessions.return_value = _block(rows)
            res = self.api.device_guard_pin_local(AID, 45)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["keepIds"], [SID_B, SID_C])
        self.api.device_guard_start.assert_called_once_with(AID, [SID_B, SID_C], 45)
        self.api._remember_local_session.assert_called_once_with(AID, SID_B)
        self.api.refresh_login_one.assert_not_called()

    def test_keeps_ide_only_when_tool_not_distinct(self):
        with patch.object(app, "parse_token", return_value=(AID, "tok", {"time": "1"})), patch.object(
            login_detect, "match_session_id_by_jwt_time", return_value=SID_B
        ):
            res = self.api.device_guard_pin_local(AID, 30)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["keepIds"], [SID_B])
        self.api.device_guard_start.assert_called_once_with(AID, [SID_B], 30)


class PinLocalHelperSanityTest(unittest.TestCase):
    def test_helper_still_used_for_web_only(self):
        before = [_client(SID_A, OLD), _web("w1", OLD)]
        after = [_client(SID_A, OLD), _web("w1", NEW)]
        self.assertEqual(login_detect.pin_local_session_id(before, after), "")


class PreviewPinLocalTest(unittest.TestCase):
    def setUp(self):
        import preview_server as ps

        self.ps = ps
        with ps._LOCK:
            for aid in list(ps._SESSIONS_SEED):
                ps._SESSIONS[aid] = ps._copy_sessions(aid)
                ps._GUARDS[aid] = ps._empty_guard(len(ps._SESSIONS[aid]))

    def test_refresh_bumps_local_client_created_at(self):
        ps = self.ps
        before = [dict(s) for s in ps._SESSIONS[ps.DEMO_ID]]
        ps._rpc("refresh_login_one", [ps.DEMO_ID])
        after = [dict(s) for s in ps._SESSIONS[ps.DEMO_ID]]
        self.assertEqual(
            login_detect.pin_local_session_id(before, after),
            before[0]["sessionId"],
        )

    def test_kick_old_drops_preview_tool_session(self):
        ps = self.ps
        tool = ps._SESSIONS_SEED[ps.DEMO_ID][1]["sessionId"]
        res = ps._rpc("refresh_login_kick_old", [ps.DEMO_ID])
        self.assertTrue(res and res.get("ok"), res)
        self.assertEqual(res.get("droppedSessionId"), tool)
        ids = [s["sessionId"] for s in ps._SESSIONS[ps.DEMO_ID]]
        self.assertNotIn(tool, ids)

    def test_pin_local_starts_guard_for_demo(self):
        ps = self.ps
        want = [
            ps._SESSIONS_SEED[ps.DEMO_ID][0]["sessionId"],
            ps._SESSIONS_SEED[ps.DEMO_ID][1]["sessionId"],
        ]
        res = ps._rpc("device_guard_pin_local", [ps.DEMO_ID, 30])
        self.assertTrue(res and res.get("ok"), res)
        self.assertEqual(res["keepIds"], want)
        self.assertTrue(res["status"]["running"])
        self.assertEqual(
            ps._SESSIONS[ps.DEMO_ID][0]["createdAt"],
            ps._SESSIONS_SEED[ps.DEMO_ID][0]["createdAt"],
        )

    def test_pin_local_rejects_other_account(self):
        ps = self.ps
        res = ps._rpc("device_guard_pin_local", [ps.DEMO_ID_2, 30])
        self.assertFalse(res.get("ok"))
        self.assertEqual(res["error"], "请先在本机 Cursor 登录这个号")

    def test_demo_marks_only_first_client_when_shared(self):
        ps = self.ps
        res = ps._rpc("list_sessions", [ps.DEMO_ID])
        clients = [s for s in res["sessions"] if s["type"] == "client"]
        self.assertEqual(len(clients), 2)
        self.assertEqual(clients[0]["localMark"], "local")
        self.assertEqual(clients[0]["localHost"], "preview-mac")
        self.assertIsNone(clients[0]["toolMark"])
        self.assertIsNone(clients[1]["localMark"])
        self.assertEqual(clients[1]["toolMark"], "tool")

    def test_switch_account_refresh_first_bumps_and_returns_sessions(self):
        ps = self.ps
        before = ps._SESSIONS[ps.DEMO_ID][0]["createdAt"]
        res = ps._rpc("switch_account", [ps.DEMO_ID, False, True, False])
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res.get("refreshed"))
        self.assertGreater(ps._SESSIONS[ps.DEMO_ID][0]["createdAt"], before)
        self.assertIn("sessions", res)
        self.assertTrue(
            any(
                s.get("localMark") == "local" or s.get("freshMark") == "fresh" or s.get("toolMark") == "tool"
                for s in res["sessions"]
            )
        )

    def test_switch_account_kick_old_drops_preview_tool(self):
        ps = self.ps
        tool = ps._SESSIONS_SEED[ps.DEMO_ID][1]["sessionId"]
        res = ps._rpc("switch_account", [ps.DEMO_ID, False, True, True])
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("droppedSessionId"), tool)
        ids = [s["sessionId"] for s in ps._SESSIONS[ps.DEMO_ID]]
        self.assertNotIn(tool, ids)

    def test_switch_account_refresh_first_false_does_not_bump(self):
        ps = self.ps
        before = ps._SESSIONS[ps.DEMO_ID][0]["createdAt"]
        res = ps._rpc("switch_account", [ps.DEMO_ID, False, False, True])
        self.assertTrue(res.get("ok"), res)
        self.assertFalse(res.get("refreshed"))
        self.assertEqual(ps._SESSIONS[ps.DEMO_ID][0]["createdAt"], before)
        self.assertFalse(res.get("droppedSessionId"))


class ResolvePinnedLocalSessionTest(unittest.TestCase):
    def setUp(self):
        self.api = app.Api.__new__(app.Api)
        self.api._local_hostname = MagicMock(return_value="office-mac")

    def test_jwt_time_match_is_remembered(self):
        jwt_time = login_detect.created_at_ms("2026-09-16T11:25:18.000Z") // 1000
        rows = [_client(SID_A, "2026-09-16T11:25:18.000Z"), _client(SID_B, OLD)]
        binds = {}
        with patch.object(app, "_read_json", return_value=binds), patch.object(
            app, "_write_json"
        ) as write, patch.object(
            app.local_cursor, "read_local_account", return_value={"token": "tok"}
        ), patch.object(
            app, "parse_token", return_value=(AID, "tok", {"time": str(jwt_time)})
        ), patch.object(
            app.local_cursor, "read_machine_ids", return_value={"machineId": "mid-aaa"}
        ):
            pinned = self.api._resolve_pinned_local_session(AID, rows)
        self.assertEqual(pinned, SID_A)
        write.assert_called()
        saved = write.call_args[0][1][AID]
        self.assertEqual(saved["sessionId"], SID_A)
        self.assertEqual(saved["machineId"], "mid-aaa")
        self.assertEqual(saved["hostname"], "office-mac")

    def test_machine_id_mismatch_ignores_saved_bind(self):
        rows = [_client(SID_A, OLD), _client(SID_B, OLD)]
        binds = {AID: {"sessionId": SID_A, "machineId": "old-mid"}}
        with patch.object(app, "_read_json", return_value=binds), patch.object(
            app, "_write_json"
        ) as write, patch.object(
            app.local_cursor, "read_local_account", return_value={"token": "tok"}
        ), patch.object(
            app, "parse_token", return_value=(AID, "tok", {"time": "1"})
        ), patch.object(
            app.local_cursor, "read_machine_ids", return_value={"machineId": "new-mid"}
        ):
            pinned = self.api._resolve_pinned_local_session(AID, rows)
        self.assertEqual(pinned, "")
        write.assert_not_called()

    def test_saved_bind_used_when_jwt_time_misses(self):
        rows = [_client(SID_A, OLD), _client(SID_B, OLD)]
        binds = {AID: {"sessionId": SID_B, "machineId": "mid-aaa"}}
        with patch.object(app, "_read_json", return_value=binds), patch.object(
            app, "_write_json"
        ) as write, patch.object(
            app.local_cursor, "read_local_account", return_value={"token": "tok"}
        ), patch.object(
            app, "parse_token", return_value=(AID, "tok", {"time": "1"})
        ), patch.object(
            app.local_cursor, "read_machine_ids", return_value={"machineId": "mid-aaa"}
        ):
            pinned = self.api._resolve_pinned_local_session(AID, rows)
        self.assertEqual(pinned, SID_B)
        write.assert_called()


if __name__ == "__main__":
    unittest.main()
