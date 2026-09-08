"""隔离浏览器只开一次：复用已打开的窗口，检测不会再 Popen。"""

import json
import unittest
from unittest.mock import patch

import browser_login
import sand_api
from test_login_sessions import SAMPLE
from test_login_sessions import _jwt


class BrowserReuseTest(unittest.TestCase):
    def setUp(self):
        browser_login._LIVE.clear()

    def tearDown(self):
        browser_login._LIVE.clear()

    def test_no_window_returns_none(self):
        self.assertIsNone(browser_login.session_for("user_01NONE"))
        self.assertFalse(browser_login.has_live_browser("user_01NONE"))
        self.assertIsNone(browser_login.fetch_sessions_if_open("user_01NONE", _jwt()))
        self.assertIsNone(browser_login.revoke_session_if_open("user_01NONE", _jwt(), "abc"))

    def test_browser_revoke_posts_session_id_and_numeric_type(self):
        uid = "user_01REVOKE00000000000000000"
        sid = SAMPLE["sessions"][0]["sessionId"]
        browser_login._LIVE[uid] = {"port": 9333, "name": "chrome"}
        with patch.object(browser_login, "_debug_alive", return_value=True), patch.object(
            browser_login, "_browser_fetch", return_value=(200, "{}")
        ) as fetch:
            res = browser_login.revoke_session_if_open(uid, _jwt(), sid, "SESSION_TYPE_CLIENT")
        self.assertTrue(res["ok"])
        self.assertEqual(res.get("sessionVia"), "browser")
        self.assertEqual(fetch.call_args[0][3], "POST")
        self.assertEqual(json.loads(fetch.call_args[0][5]), {"session_id": sid, "type": 2})

    def test_open_reuses_live_port_without_launch(self):
        uid = "user_01REUSE000000000000000000"
        browser_login._LIVE[uid] = {"port": 9333, "name": "chrome"}

        with patch.object(browser_login, "_debug_alive", return_value=True), patch.object(
            browser_login, "_prepare_page"
        ) as prepare, patch.object(browser_login.subprocess, "Popen") as popen:
            out = browser_login.open_with_token(uid, _jwt(), url=browser_login.CURSOR_DASHBOARD_SESSIONS)

        self.assertTrue(out["reused"])
        self.assertEqual(out["name"], "chrome")
        prepare.assert_called_once()
        popen.assert_not_called()

    def test_open_launches_when_no_window(self):
        uid = "user_01LAUNCH0000000000000000"
        with patch.object(browser_login, "session_for", return_value=None), patch.object(
            browser_login, "_find_browser", return_value=("/bin/chrome", "chrome")
        ), patch.object(browser_login, "_free_port", return_value=9444), patch.object(
            browser_login, "_profile_dir", return_value="/tmp/sandclaimer-test-profile"
        ), patch.object(browser_login.subprocess, "Popen") as popen, patch.object(
            browser_login, "_wait_page_target", return_value="ws://127.0.0.1:9444/devtools/page/1"
        ), patch.object(browser_login, "_prepare_page") as prepare, patch.object(
            browser_login, "_remember"
        ) as remember, patch.object(
            browser_login, "_profile_chrome_pid", return_value=None
        ):
            out = browser_login.open_with_token(uid, _jwt())

        self.assertFalse(out["reused"])
        self.assertEqual(out["name"], "chrome")
        popen.assert_called_once()
        args = popen.call_args[0][0]
        self.assertIn("--new-window", args)
        self.assertIn("about:blank", args)
        prepare.assert_called_once()
        remember.assert_called_once()

    def test_open_plan_navigates_newest_blank_not_workbench(self):
        pages = [
            {"url": "chrome://newtab/", "webSocketDebuggerUrl": "ws://a"},
            {"url": "https://map.dtbgis.com/studio", "webSocketDebuggerUrl": "ws://b"},
            {"url": "about:blank", "webSocketDebuggerUrl": "ws://c"},
        ]
        action, page = browser_login._open_plan(pages)
        self.assertEqual(action, "navigate")
        self.assertEqual(page["url"], "about:blank")

    def test_open_plan_prefers_cursor_tab(self):
        pages = [
            {"url": "about:blank", "webSocketDebuggerUrl": "ws://a"},
            {"url": "https://cursor.com/dashboard", "webSocketDebuggerUrl": "ws://b"},
        ]
        action, page = browser_login._open_plan(pages)
        self.assertEqual(action, "navigate")
        self.assertEqual(page["url"], "https://cursor.com/dashboard")

    def test_open_plan_creates_when_only_unrelated_tabs(self):
        pages = [{"url": "https://map.dtbgis.com/studio", "webSocketDebuggerUrl": "ws://a"}]
        action, page = browser_login._open_plan(pages)
        self.assertEqual(action, "create")
        self.assertIsNone(page)

    def test_prepare_page_navigates_focused_blank(self):
        class FakeCDP:
            def __init__(self):
                self.calls = []

            def call(self, method, params=None, session_id=None):
                self.calls.append((method, params or {}, session_id))
                if method == "Target.getTargets":
                    return {
                        "result": {
                            "targetInfos": [
                                {"type": "page", "url": "https://map.dtbgis.com/studio", "targetId": "workbench"},
                                {"type": "page", "url": "about:blank", "targetId": "blank"},
                            ]
                        }
                    }
                if method == "Target.attachToTarget":
                    return {"result": {"sessionId": "sid-blank"}}
                if method == "Storage.setCookies":
                    return {"result": {}}
                if method == "Network.enable":
                    return {"result": {}}
                if method == "Network.setCookie":
                    return {"result": {"success": True}}
                if method == "Page.enable":
                    return {"result": {}}
                if method == "Page.navigate":
                    return {"result": {"frameId": "f"}}
                if method == "Target.setDiscoverTargets":
                    return {"result": {}}
                if method == "Target.activateTarget":
                    return {"result": {}}
                if method == "Page.bringToFront":
                    return {"result": {}}
                if method == "Target.closeTarget":
                    return {"result": {"success": True}}
                return {"result": {}}

            def close(self):
                pass

        fake = FakeCDP()
        cursor_page = {"url": "https://cursor.com/dashboard/spending", "id": "blank"}

        def infos(cdp):
            if any(method == "Page.navigate" for method, _params, _sid in fake.calls):
                return [cursor_page]
            return [
                {"url": "https://map.dtbgis.com/studio", "id": "workbench"},
                {"url": "about:blank", "id": "blank"},
            ]

        with patch.object(browser_login, "_browser_ws", return_value="ws://127.0.0.1:9333/devtools/browser/x"), patch.object(
            browser_login, "_connect_page", return_value=fake
        ), patch.object(browser_login, "_page_infos", side_effect=infos), patch.object(
            browser_login, "_wait_url_prefix", return_value=cursor_page
        ):
            browser_login._prepare_page(9333, "user_01X", "jwt", url=browser_login.CURSOR_DASHBOARD_SPENDING)

        attach = [c for c in fake.calls if c[0] == "Target.attachToTarget"]
        nav = [c for c in fake.calls if c[0] == "Page.navigate"]
        create = [c for c in fake.calls if c[0] == "Target.createTarget"]
        self.assertEqual(attach[0][1]["targetId"], "blank")
        self.assertEqual(nav[0][1]["url"], browser_login.CURSOR_DASHBOARD_SPENDING)
        self.assertFalse(create)

    def test_prepare_page_creates_tab_when_json_empty(self):
        class FakeCDP:
            def __init__(self):
                self.calls = []
                self.created = False

            def call(self, method, params=None, session_id=None):
                self.calls.append((method, params or {}, session_id))
                if method == "Target.createTarget":
                    self.created = True
                    return {"result": {"targetId": "new"}}
                return {"result": {}}

            def close(self):
                pass

        fake = FakeCDP()

        def infos(_cdp):
            if fake.created:
                return [{"url": "https://cursor.com/dashboard/spending", "id": "new"}]
            return []

        with patch.object(browser_login, "_browser_ws", return_value="ws://x"), patch.object(
            browser_login, "_connect_page", return_value=fake
        ), patch.object(browser_login, "_set_cookies"), patch.object(
            browser_login, "_page_infos", side_effect=infos
        ), patch.object(
            browser_login,
            "_wait_url_prefix",
            return_value={"url": "https://cursor.com/dashboard/spending", "id": "new"},
        ):
            browser_login._prepare_page(9333, "user_01X", "jwt", url=browser_login.CURSOR_DASHBOARD_SPENDING)
        self.assertTrue(fake.created)

    def test_session_for_discovers_running_chrome_after_restart(self):
        uid = "user_01DISCOVER00000000000000"
        with patch.object(browser_login, "_debug_alive", return_value=True), patch.object(
            browser_login, "_read_saved_meta", return_value=None
        ), patch.object(
            browser_login,
            "_discover_profile_session",
            return_value={"port": 59848, "name": "chrome"},
        ) as discover, patch.object(browser_login, "_remember") as remember:
            info = browser_login.session_for(uid)
        self.assertEqual(info["port"], 59848)
        discover.assert_called_once()
        remember.assert_called_once()

    def test_open_reuses_profile_chrome_without_live_cache(self):
        uid = "user_01PROFILE000000000000000"
        with patch.object(browser_login, "session_for", return_value=None), patch.object(
            browser_login, "_find_browser", return_value=("/bin/chrome", "chrome")
        ), patch.object(
            browser_login, "_profile_dir", return_value="/tmp/sandclaimer-test-profile"
        ), patch.object(browser_login, "_profile_chrome_pid", return_value=59750), patch.object(
            browser_login,
            "_discover_profile_session",
            return_value={"port": 59848, "name": "chrome"},
        ), patch.object(browser_login, "_prepare_page") as prepare, patch.object(
            browser_login, "_remember"
        ), patch.object(browser_login.subprocess, "Popen") as popen:
            out = browser_login.open_with_token(uid, _jwt(), url=browser_login.CURSOR_DASHBOARD_SPENDING)
        self.assertTrue(out["reused"])
        popen.assert_not_called()
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args[0][0], 59848)

    def test_port_from_command_reads_explicit_debug_port(self):
        cmd = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            "--remote-debugging-port=59848 --user-data-dir=/tmp/x about:blank"
        )
        self.assertEqual(browser_login._port_from_command(cmd), 59848)
        self.assertIsNone(browser_login._port_from_command("--remote-debugging-port=0"))
        self.assertIsNone(browser_login._port_from_command("chrome"))

    def test_smart_fetch_uses_browser_and_skips_http(self):
        uid = "user_01SMART00000000000000000"
        via = sand_api.normalize_sessions(SAMPLE)
        via["sessionVia"] = "browser"
        with patch.object(browser_login, "fetch_sessions_if_open", return_value=via), patch.object(
            sand_api, "fetch_sessions"
        ) as http:
            block = browser_login.fetch_sessions_smart(uid, _jwt())
        self.assertEqual(block["sessionVia"], "browser")
        self.assertEqual(block["sessionCount"], 2)
        http.assert_not_called()

    def test_smart_fetch_falls_back_to_http_without_window(self):
        uid = "user_01HTTP000000000000000000"
        http_block = sand_api.normalize_sessions(SAMPLE)
        with patch.object(browser_login, "fetch_sessions_if_open", return_value=None), patch.object(
            sand_api, "fetch_sessions", return_value=http_block
        ) as http:
            block = browser_login.fetch_sessions_smart(uid, _jwt())
        self.assertEqual(block["sessionCount"], 2)
        self.assertNotEqual(block.get("sessionVia"), "browser")
        http.assert_called_once()

    def test_browser_fetch_normalizes_json(self):
        uid = "user_01CDP00000000000000000000"
        browser_login._LIVE[uid] = {"port": 9333, "name": "chrome"}
        with patch.object(browser_login, "_debug_alive", return_value=True), patch.object(
            browser_login, "_browser_fetch", return_value=(200, json.dumps(SAMPLE))
        ):
            block = browser_login.fetch_sessions_if_open(uid, _jwt())
        self.assertEqual(block["sessionCount"], 2)
        self.assertEqual(block["sessionVia"], "browser")
        self.assertFalse(block["sessionWaf"])

    def test_browser_waf_does_not_look_like_dead_token(self):
        uid = "user_01WAF00000000000000000000"
        browser_login._LIVE[uid] = {"port": 9333, "name": "chrome"}
        html = "<!DOCTYPE html><title>Vercel Security Checkpoint</title>"
        with patch.object(browser_login, "_debug_alive", return_value=True), patch.object(
            browser_login, "_browser_fetch", return_value=(403, html)
        ):
            block = browser_login.fetch_sessions_if_open(uid, _jwt())
        self.assertTrue(block["sessionWaf"])
        self.assertEqual(block["sessionVia"], "browser")
        self.assertNotIn("HTTP 403", block["sessionError"])


if __name__ == "__main__":
    unittest.main()
