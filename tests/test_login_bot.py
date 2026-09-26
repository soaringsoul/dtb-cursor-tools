"""登录 Bot：写入 Grok Bot 自带账户列表并重启客户端，不关 Cursor，不走网页授权。"""

import base64
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

AID = "user_01LOGINBOT00000000000000"
SAMPLE_URL = (
    "https://cursor.com/loginDeepControl?challenge=FUDMUY_jFaak3csMHHxhoA4DymZmERvLtWVxkohwrhY"
    "&uuid=e5e26ccb-98e7-4aaa-94d7-98eb7ed02ee0&mode=login&redirectTarget=sand"
    "&supportsSelectedTeamLogin=true"
)


def _jwt(sub=AID, typ="session", exp=1893456000):
    payload = {"sub": sub, "type": typ, "exp": exp, "email": "a@b.com"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + encoded + ".x"


OLD = _jwt(exp=1893456000)


def _classic_from_start(mock_start):
    args, kwargs = mock_start.call_args
    if "classic" in kwargs:
        return kwargs["classic"]
    if len(args) > 1:
        return args[1]
    return True


class LoginBotApiTest(unittest.TestCase):
    def setUp(self):
        import app

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

    def _patch_cursor_write(self):
        return (
            patch.object(self.app.sand_api, "token_exp", return_value=1999999999),
            patch.object(self.app.sand_api, "probe_token_alive", return_value="alive"),
            patch.object(self.app.sand_patch, "resolve_cursor_layout", return_value=self.layout),
            patch.object(self.app.sand_patch, "close_cursor"),
            patch.object(self.app.local_cursor, "write_local_account"),
            patch.object(self.app.sand_patch, "start_cursor"),
        )

    def _patch_bot(self):
        return (
            patch.object(self.app.sand_api, "token_exp", return_value=1999999999),
            patch.object(self.app.sand_api, "probe_token_alive", return_value="alive"),
            patch.object(self.app.grok_bot, "find_app", return_value=Path("/Applications/Grok Bot.app")),
            patch.object(self.app.grok_bot, "close_grok_bot"),
            patch.object(self.app.grok_bot, "write_local_account"),
            patch.object(self.app.grok_bot, "start_grok_bot", return_value=True),
            patch.object(self.app.grok_bot, "wait_for_login_deep_url", return_value=""),
            patch.object(
                self.app.sand_api,
                "confirm_login_deep_control",
                return_value={"ok": True, "status": 200},
            ),
        )

    def test_login_bot_writes_native_account_and_restarts_grok_bot(self):
        p_exp, p_alive, p_find, p_close_bot, p_write_bot, p_start_bot, p_wait, p_confirm = self._patch_bot()
        with (
            p_exp,
            p_alive,
            p_find,
            p_close_bot as close_bot,
            p_write_bot as write_bot,
            p_start_bot as start_bot,
            p_wait,
            p_confirm as confirm,
            patch.object(self.app.sand_patch, "close_cursor") as close_cursor,
            patch.object(self.app.local_cursor, "write_local_account") as write_cursor,
            patch.object(self.app.sand_patch, "start_cursor") as start_cursor,
        ):
            order: list[str] = []
            close_bot.side_effect = lambda *a, **k: order.append("close")
            write_bot.side_effect = lambda *a, **k: order.append("write") or {"ok": True}
            start_bot.side_effect = lambda *a, **k: order.append("start") or True
            res = self.api.login_bot(AID, False, False, False)
        self.assertTrue(res["ok"], res)
        close_cursor.assert_not_called()
        write_cursor.assert_not_called()
        start_cursor.assert_not_called()
        confirm.assert_not_called()
        self.assertEqual(order, ["close", "write", "start"])
        self.assertEqual(write_bot.call_args[0][0], OLD)

    def test_login_bot_ignores_pasted_login_url(self):
        p_exp, p_alive, p_find, p_close_bot, p_write_bot, p_start_bot, p_wait, p_confirm = self._patch_bot()
        with (
            p_exp,
            p_alive,
            p_find,
            p_close_bot as close_bot,
            p_write_bot as write_bot,
            p_start_bot as start_bot,
            p_confirm as confirm,
        ):
            res = self.api.login_bot(AID, False, False, False, SAMPLE_URL)
        self.assertTrue(res["ok"], res)
        confirm.assert_not_called()
        write_bot.assert_called_once()
        close_bot.assert_called_once()
        start_bot.assert_called_once()

    def test_switch_account_still_starts_classic(self):
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_cursor_write()
        with p_exp, p_alive, p_lay, p_close, p_write, p_start as start:
            res = self.api.switch_account(AID, False, False, False)
        self.assertTrue(res["ok"], res)
        start.assert_called_once()
        self.assertIs(_classic_from_start(start), True)

    def test_login_bot_reuses_write_gates_expired_skips_confirm(self):
        p_exp, p_alive, p_find, p_close_bot, p_write_bot, p_start_bot, p_wait, p_confirm = self._patch_bot()
        with (
            patch.object(self.app.sand_api, "token_exp", return_value=1),
            p_alive,
            p_find,
            p_confirm as confirm,
            patch.object(self.app.sand_patch, "close_cursor") as close_cursor,
        ):
            res = self.api.login_bot(AID, False, False, False)
        self.assertFalse(res["ok"])
        self.assertIn("过期", res["error"])
        close_cursor.assert_not_called()
        confirm.assert_not_called()

    def test_login_bot_refresh_failure_does_not_close_cursor_or_confirm(self):
        self.api.refresh_login_one.return_value = {"ok": False, "error": "换票失败"}
        p_exp, p_alive, p_find, p_close_bot, p_write_bot, p_start_bot, p_wait, p_confirm = self._patch_bot()
        with (
            p_exp,
            p_alive,
            p_confirm as confirm,
            patch.object(self.app.sand_patch, "close_cursor") as close_cursor,
        ):
            res = self.api.login_bot(AID, False, True, False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "换票失败")
        close_cursor.assert_not_called()
        confirm.assert_not_called()

    def test_login_bot_does_not_open_isolated_browser(self):
        p_exp, p_alive, p_find, p_close_bot, p_write_bot, p_start_bot, p_wait, p_confirm = self._patch_bot()
        with (
            p_exp,
            p_alive,
            p_find,
            p_close_bot,
            p_write_bot,
            p_start_bot,
            p_wait,
            p_confirm,
            patch.object(self.app.browser_login, "open_with_token") as open_tok,
        ):
            res = self.api.login_bot(AID, False, False, False)
        self.assertTrue(res["ok"], res)
        open_tok.assert_not_called()

    def test_login_bot_missing_app_without_url_does_not_close_cursor(self):
        p_exp, p_alive, p_find, p_close_bot, p_write_bot, p_start_bot, p_wait, p_confirm = self._patch_bot()
        with (
            p_exp,
            p_alive,
            patch.object(self.app.grok_bot, "find_app", return_value=None),
            p_close_bot as close_bot,
            p_write_bot,
            p_start_bot as start_bot,
            p_confirm as confirm,
            patch.object(self.app.sand_patch, "close_cursor") as close_cursor,
        ):
            res = self.api.login_bot(AID, False, False, False)
        self.assertFalse(res["ok"])
        self.assertIn("Grok Bot", res["error"])
        close_cursor.assert_not_called()
        close_bot.assert_not_called()
        start_bot.assert_not_called()
        confirm.assert_not_called()

    def test_login_bot_does_not_reset_cursor_machine_id(self):
        p_exp, p_alive, p_find, p_close_bot, p_write_bot, p_start_bot, p_wait, p_confirm = self._patch_bot()
        with (
            p_exp,
            p_alive,
            p_find,
            p_close_bot,
            p_write_bot,
            p_start_bot,
            p_wait,
            p_confirm,
            patch.object(self.app.local_cursor, "reset_machine_ids") as reset_mid,
        ):
            res = self.api.login_bot(AID, True, False, False)
        self.assertTrue(res["ok"], res)
        reset_mid.assert_not_called()
        self.assertFalse(res.get("resetMachineId"))

    def test_login_bot_exchanges_web_token_like_switch(self):
        web = _jwt(typ="web")
        session = _jwt(typ="session")
        self.item["token"] = web
        p_exp, p_alive, p_find, p_close_bot, p_write_bot, p_start_bot, p_wait, p_confirm = self._patch_bot()
        with (
            p_exp,
            p_alive,
            p_find,
            p_close_bot,
            p_write_bot as write_bot,
            p_start_bot,
            p_wait,
            p_confirm as confirm,
            patch.object(
                self.app.sand_api, "exchange_web_to_session", return_value=(session, "rt2")
            ) as exch,
        ):
            res = self.api.login_bot(AID, False, False, False)
        self.assertTrue(res["ok"], res)
        exch.assert_called_once_with(web)
        confirm.assert_not_called()
        self.assertEqual(write_bot.call_args[0][0], session)


class LoginBotConfirmCopyTest(unittest.TestCase):
    def test_title_and_buttons(self):
        import login_bot_confirm

        self.assertEqual(login_bot_confirm.TITLE, "登录 Bot 确认")
        self.assertEqual(login_bot_confirm.OK, "确认登录 Bot")
        self.assertEqual(login_bot_confirm.CANCEL, "取消")

    def test_mentions_grok_bot_link_and_not_sign_in(self):
        import login_bot_confirm

        blob = "\n".join(login_bot_confirm.confirm_lines("alice@example.com"))
        self.assertIn("alice@example.com", blob)
        self.assertIn("Grok Bot", blob)
        self.assertTrue("不会关" in blob or "不关" in blob)
        self.assertTrue("账户" in blob or "切换" in blob)
        self.assertNotIn("loginDeepControl", blob)
        self.assertNotIn("Sign in", blob)
        self.assertNotIn("关掉当前 Cursor", blob)
        self.assertIn("当前这张票", blob)
        self.assertNotIn("会先换新登录票", blob)
        self.assertIn("正在重新连接你的电脑", blob)
        self.assertIn("关掉代理", blob)

    def test_does_not_mention_isolated_authenticator(self):
        import login_bot_confirm

        blob = "\n".join(login_bot_confirm.confirm_lines("a@b.com"))
        self.assertNotIn("authenticator", blob.lower())
        self.assertNotIn("隔离", blob)


class LoginBotUiContractTest(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.js = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.html = (root / "web" / "index.html").read_text(encoding="utf-8")

    def _fn(self, name, next_name):
        start = self.js.index(f"function {name}")
        end = self.js.index(f"function {next_name}")
        return self.js[start:end]

    def test_verify_and_switch_lead_row_actions(self):
        chunk = self._fn("rowMainActions", "ticketMenuGroups")
        verify_at = chunk.index('label: "验证"')
        switch_at = chunk.index('label: "切号"')
        login_at = chunk.index('label: "登录 Bot"')
        self.assertLess(verify_at, switch_at)
        self.assertLess(switch_at, login_at)
        icons = self._fn("ticketMenuHtml", "actionButtonsHtml")
        self.assertNotIn('label: "验证"', icons)
        self.assertNotIn('label: "切号"', icons)

    def test_ticket_menu_does_not_include_login_bot(self):
        chunk = self._fn("ticketMenuGroups", "ticketMenuHtml")
        self.assertNotIn("登录 Bot", chunk)
        self.assertNotIn("loginBot", chunk)
        self.assertNotIn("进控制台", chunk)
        self.assertNotIn("dashboard", chunk)

    def test_ticket_actions_are_inline_icons(self):
        chunk = self._fn("ticketMenuHtml", "actionButtonsHtml")
        self.assertIn('class="ico-row"', chunk)
        self.assertIn("TICKET_ICONS", chunk)
        self.assertNotIn("登录信息", chunk)
        self.assertNotIn('data-act="ticketMenu"', chunk)

    def test_dashboard_sits_right_of_guard(self):
        chunk = self._fn("rowMainActions", "ticketMenuGroups")
        self.assertLess(chunk.index('label: "本机保护"'), chunk.index('label: "进控制台"'))
        self.assertIn('act: "dashboard"', chunk)

    def test_js_calls_login_bot_api_not_open_login(self):
        self.assertIn("api().login_bot", self.js)
        self.assertIn('act === "loginBot"', self.js)
        start = self.js.index("async function loginBot(")
        end = self.js.index("async function openLogin(")
        chunk = self.js[start:end]
        self.assertNotIn("open_login", chunk)
        self.assertNotIn("openLogin(", chunk)
        self.assertNotIn("authenticator", chunk.lower())
        self.assertNotIn("needLoginUrl", chunk)

    def test_confirm_copy_in_js_keeps_cursor_open(self):
        self.assertIn("loginBotConfirmCopy", self.js)
        start = self.js.index("function loginBotConfirmCopy")
        nxt = self.js.find("\nfunction ", start + 1)
        chunk = self.js[start:nxt]
        self.assertIn("Grok Bot", chunk)
        self.assertTrue("不会关" in chunk or "不关" in chunk)
        self.assertNotIn("关掉当前 Cursor", chunk)
        self.assertNotIn("loginDeepControl", chunk)
        self.assertNotIn("Sign in", chunk)

    def test_button_title_says_grok_bot_client(self):
        chunk = self._fn("rowMainActions", "ticketMenuGroups")
        self.assertIn("Grok Bot", chunk)
        self.assertNotIn("不进经典编辑器", chunk)
        self.assertNotIn("不要在网页点 Sign in", chunk)

    def test_html_hides_login_bot_url_input(self):
        self.assertIn("loginBotUrlWrap", self.html)
        start = self.js.index("function paintSwitchConfirmChrome")
        nxt = self.js.find("\nfunction ", start + 1)
        chunk = self.js[start:nxt]
        self.assertIn('pendingSwitchKind === "loginBot"', chunk)
        self.assertGreater(chunk.count("urlWrap.hidden = true"), 0)
        self.assertIn("switchConfirmOpts", chunk)
        self.assertIn("opts.hidden = loginBot", chunk)

    def test_html_switch_opts_wrapped_for_login_bot_hide(self):
        self.assertIn('id="switchConfirmOpts"', self.html)
        self.assertIn("switchRefreshFirstChk", self.html)

    def test_switch_confirm_leaves_refresh_unchecked(self):
        start = self.html.index('id="switchRefreshFirstChk"')
        tag = self.html[start:start + 80]
        self.assertNotIn("checked", tag)
        chunk = self._fn("openSwitchConfirm", "openLoginBotConfirm")
        self.assertIn("refreshChk.checked = false", chunk)
        self.assertNotIn("refreshChk.checked = true", chunk)


class PreviewLoginBotRpcTest(unittest.TestCase):
    def test_preview_exposes_login_bot(self):
        import preview_server as ps

        self.assertIn("login_bot", ps.MOCK_JS)
        res = ps._rpc("login_bot", [ps.DEMO_ID, False, False, False])
        self.assertTrue(res.get("ok"), res)


if __name__ == "__main__":
    unittest.main()
