"""号池工作台：批量只处理勾选、顶栏 KPI、搜索筛选排序、首跑三任务。不联网。"""

import unittest
from pathlib import Path

from app import ops_ui
from app import sand_patch

ROOT = Path(__file__).resolve().parents[1]


NOW = 1_700_000_000_000  # ms


def row(aid, **kwargs):
    account = {"id": aid, "label": kwargs.pop("label", aid + "@ex.com"), "exp": kwargs.pop("exp", None)}
    state = kwargs.pop("state", {})
    guarding = kwargs.pop("guarding", False)
    return {"id": aid, "account": account, "state": state, "guarding": guarding, **kwargs}


class AppInfoTest(unittest.TestCase):
    def test_version_matches_sand_patch(self):
        info = ops_ui.app_info()
        self.assertEqual(info["version"], sand_patch.TOOL_VERSION)
        self.assertEqual(info["name"], "cursorAdmin")

    def test_help_jobs_are_three_operator_tasks(self):
        titles = [j["title"] for j in ops_ui.HELP_JOBS]
        self.assertEqual(titles, ["看额度", "切到本机", "守设备"])
        blob = " ".join(j["body"] for j in ops_ui.HELP_JOBS)
        self.assertIn("验证", blob)
        self.assertIn("切号", blob)
        self.assertIn("本机保护", blob)


class BatchIdsTest(unittest.TestCase):
    def test_empty_selection_is_empty(self):
        self.assertEqual(ops_ui.batch_ids(["a", "b"], []), [])

    def test_only_selected_in_account_order(self):
        self.assertEqual(ops_ui.batch_ids(["a", "b", "c"], ["c", "a"]), ["a", "c"])

    def test_ignores_selected_not_in_list(self):
        self.assertEqual(ops_ui.batch_ids(["a"], ["a", "ghost"]), ["a"])


class HeroKpisTest(unittest.TestCase):
    def test_counts_ops_kpis(self):
        rows = [
            row("a", state={"alive": True, "billingCycleEnd": "2024-11-18T00:00:00Z", "percent": 10}),
            row("b", state={"alive": False, "percent": 40}),
            row("c", state={"alive": True, "kind": "card", "percent": 100}, guarding=True),
            row("d", state={"alive": True, "hasAvailableUsage": False, "percent": 80}),
        ]
        k = ops_ui.hero_kpis(rows, now_ms=NOW)
        self.assertEqual(k["total"], 4)
        self.assertEqual(k["dead"], 1)
        self.assertEqual(k["guarding"], 1)
        self.assertEqual(k["botFull"], 2)
        self.assertEqual(k["expiring"], 0)

    def test_expiring_is_within_seven_days_not_already_due(self):
        soon = NOW + 3 * 86400000
        past = NOW - 86400000
        far = NOW + 30 * 86400000
        iso_soon = "2023-11-18T00:00:00Z"  # ignored; use ms via billing as ISO from now
        rows = [
            row("soon", state={"alive": True, "billingCycleEndMs": soon}),
            row("past", state={"alive": True, "billingCycleEndMs": past}),
            row("far", state={"alive": True, "billingCycleEndMs": far}),
        ]
        k = ops_ui.hero_kpis(rows, now_ms=NOW)
        self.assertEqual(k["expiring"], 1)


class MatchFilterSortTest(unittest.TestCase):
    def test_search_email_and_id(self):
        r = row("user_01ABC", label="alice@ex.com")
        self.assertTrue(ops_ui.account_matches(r, query="alice", filt="all", local_user_id=None))
        self.assertTrue(ops_ui.account_matches(r, query="01abc", filt="all", local_user_id=None))
        self.assertFalse(ops_ui.account_matches(r, query="bob", filt="all", local_user_id=None))

    def test_filter_local_guarding_dead(self):
        local = row("me", state={"alive": True})
        dead = row("x", state={"alive": False})
        g = row("g", state={"alive": True}, guarding=True)
        self.assertTrue(ops_ui.account_matches(local, query="", filt="local", local_user_id="me"))
        self.assertFalse(ops_ui.account_matches(dead, query="", filt="local", local_user_id="me"))
        self.assertTrue(ops_ui.account_matches(dead, query="", filt="dead", local_user_id="me"))
        self.assertTrue(ops_ui.account_matches(g, query="", filt="guarding", local_user_id="me"))

    def test_filter_paid_and_free_by_membership(self):
        pro = row("p", state={"membership": "pro"})
        plus = row("pp", state={"membership": "pro_plus"})
        free = row("f", state={"membership": "free"})
        trial = row("t", state={"membership": "free_trial"})
        unknown = row("u", state={})
        self.assertTrue(ops_ui.account_matches(pro, filt="paid"))
        self.assertTrue(ops_ui.account_matches(plus, filt="paid"))
        self.assertFalse(ops_ui.account_matches(free, filt="paid"))
        self.assertFalse(ops_ui.account_matches(unknown, filt="paid"))
        self.assertTrue(ops_ui.account_matches(free, filt="free"))
        self.assertTrue(ops_ui.account_matches(trial, filt="free"))
        self.assertFalse(ops_ui.account_matches(pro, filt="free"))
        self.assertFalse(ops_ui.account_matches(unknown, filt="free"))

    def test_default_sort_is_expiry_ascending(self):
        later = row("later", state={"billingCycleEndMs": NOW + 9 * 86400000})
        sooner = row("sooner", state={"billingCycleEndMs": NOW + 2 * 86400000})
        ordered = ops_ui.sort_rows([later, sooner], now_ms=NOW)
        self.assertEqual([r["id"] for r in ordered], ["sooner", "later"])

    def test_time_sort_descending_puts_latest_expiry_first(self):
        later = row("later", state={"billingCycleEndMs": NOW + 9 * 86400000})
        sooner = row("sooner", state={"billingCycleEndMs": NOW + 2 * 86400000})
        missing = row("missing")
        ordered = ops_ui.sort_rows(
            [sooner, missing, later], sort_by="remain", descending=True, now_ms=NOW
        )
        self.assertEqual([r["id"] for r in ordered], ["later", "sooner", "missing"])

    def test_added_sort_is_newest_first(self):
        old = row("old")
        old["account"]["addedAt"] = 100
        mid = row("mid")
        mid["account"]["addedAt"] = 200
        new = row("new")
        new["account"]["addedAt"] = 300
        ordered = ops_ui.sort_rows([old, mid, new], sort_by="added", now_ms=NOW)
        self.assertEqual([r["id"] for r in ordered], ["new", "mid", "old"])

    def test_logged_in_account_stays_above_newer_adds(self):
        old = row("old")
        old["account"]["addedAt"] = 100
        current = row("me")
        current["account"]["addedAt"] = 150
        new = row("new")
        new["account"]["addedAt"] = 300
        ordered = ops_ui.sort_rows(
            [old, current, new], sort_by="added", now_ms=NOW, local_user_id="me"
        )
        self.assertEqual([r["id"] for r in ordered], ["me", "new", "old"])

    def test_time_sort_still_pins_local(self):
        soon = row("soon", state={"billingCycleEndMs": NOW + 86400000})
        local = row("me", state={"billingCycleEndMs": NOW + 9 * 86400000})
        ordered = ops_ui.sort_rows(
            [local, soon], sort_by="remain", now_ms=NOW, local_user_id="me"
        )
        self.assertEqual([r["id"] for r in ordered], ["me", "soon"])
        desc = ops_ui.sort_rows(
            [soon, local], sort_by="remain", descending=True, now_ms=NOW, local_user_id="me"
        )
        self.assertEqual([r["id"] for r in desc], ["me", "soon"])

    def test_sort_remain_puts_soonest_first(self):
        a = row("a", state={"billingCycleEndMs": NOW + 9 * 86400000})
        b = row("b", state={"billingCycleEndMs": NOW + 2 * 86400000})
        c = row("c", state={"alive": False, "billingCycleEndMs": NOW + 1 * 86400000})
        ordered = ops_ui.sort_rows([c, a, b], sort_by="remain", now_ms=NOW)
        self.assertEqual([r["id"] for r in ordered], ["b", "a", "c"])

    def test_sort_bot_desc_then_added(self):
        a = row("a", state={"percent": 10})
        b = row("b", state={"percent": 90})
        ordered = ops_ui.sort_rows([a, b], sort_by="bot", now_ms=NOW)
        self.assertEqual([r["id"] for r in ordered], ["b", "a"])


class ClaimVisibleTest(unittest.TestCase):
    def test_hide_claim_when_already_ok(self):
        self.assertFalse(ops_ui.claim_visible({"kind": "ok"}))
        self.assertTrue(ops_ui.claim_visible({"kind": "card"}))
        self.assertTrue(ops_ui.claim_visible({}))
        self.assertTrue(ops_ui.claim_visible(None))


class HtmlContractTest(unittest.TestCase):
    def test_ops_launch_controls_exist(self):
        html = (ROOT / "web/index.html").read_text(encoding="utf-8")
        for needle in (
            'id="appVersion"',
            'id="qqGroup"',
            "1056952049",
            "https://qm.qq.com/q/POZe1e3WYG",
            'id="btnHideNotice"',
            'id="btnEmptyDetect"',
            'id="statExpiring"',
            'id="statGuard"',
            'id="statBotFull"',
            'id="acctSearch"',
            'id="acctFilter"',
            'value="paid"',
            'value="free"',
            'id="helpJobs"',
            'id="helpDetail"',
            'id="helpClose"',
            'class="help-scroll"',
            'class="help-foot"',
            'id="guardPinLocal"',
            'id="tabGuard"',
            'id="paneGuard"',
            'id="guardEmpty"',
            'id="guardAccountSelect"',
            'id="guardProbe"',
            'id="guardRefreshLogin"',
            'id="guardRefreshKick"',
            'id="guardDiff"',
            'id="toolbarMore"',
            'id="btnClear"',
            'id="btnSortTime"',
        ):
            self.assertIn(needle, html)
        self.assertNotIn("<th>套餐</th>", html)
        import_block = html.split('id="importCard"', 1)[1].split('id="accountsCard"', 1)[0]
        self.assertNotIn('id="btnClear"', import_block)
        self.assertIn("本机号池工作台", html)
        js = (ROOT / "web/app.js").read_text(encoding="utf-8")
        self.assertIn('listSort = { key: "remain", dir: 1 }', js)
        self.assertIn("function toggleTimeSort()", js)
        self.assertIn('class="mail-line"', js)
        self.assertIn('colspan="5"', js)
        self.assertNotIn("${planCell(st)}", js)
        self.assertIn('listFilter === "paid"', js)
        self.assertIn('listFilter === "free"', js)
        self.assertIn("localUserId && x.a.id === localUserId", js)
        sort_fn = js.split("function orderedAccounts(", 1)[1].split("function visibleAccounts(", 1)[0]
        self.assertNotIn('listSort.key !== "remain"', sort_fn)
        self.assertIn("本机登录中", js)
        self.assertIn('class="pill local-now"', js)
        self.assertIn("is-local", js)
        css = (ROOT / "web/style.css").read_text(encoding="utf-8")
        self.assertIn("tbody tr.is-local td", css)
        self.assertIn(".pill.local-now", css)

    def test_ticket_sheet_is_grouped_not_flat_grid(self):
        html = (ROOT / "web/index.html").read_text(encoding="utf-8")
        self.assertIn('id="menuMeta"', html)
        self.assertIn('class="modal glass sheet"', html)
        self.assertNotIn('class="menu-grid" id="menuBody"', html)
        self.assertIn('id="menuBody"', html)
        css = (ROOT / "web/style.css").read_text(encoding="utf-8")
        self.assertIn(".menu-sheet", css)
        self.assertIn(".menu-group", css)

    def test_antd_primary_uses_brand_orange(self):
        css = (ROOT / "web/style.css").read_text(encoding="utf-8")
        self.assertIn("--brand: #fa8c16", css)
        primary = css.split(".btn.primary {", 1)[1].split("}", 1)[0]
        self.assertIn("var(--brand)", primary)
        self.assertNotIn("brand-action", primary)
        self.assertIn(".tab-bar", css)
        self.assertIn("var(--layout-bg)", css.split(".tab-bar {", 1)[1].split("}", 1)[0])

    def test_tags_use_antd_four_px_radius_not_pill(self):
        css = (ROOT / "web/style.css").read_text(encoding="utf-8")
        self.assertIn("--radius-tag: 4px", css)
        pill = css.split("\n.pill {", 1)[1].split("}", 1)[0]
        self.assertIn("var(--radius-tag)", pill)
        self.assertNotIn("999px", pill)
        # 头像与状态圆点仍是全圆
        self.assertIn("border-radius: 50%", css.split(".avatar {", 1)[1].split("}", 1)[0])
        self.assertIn("border-radius: 50%", css.split(".pill.guard .dot {", 1)[1].split("}", 1)[0])

    def test_only_one_solid_primary_per_account_row(self):
        js = (ROOT / "web/app.js").read_text(encoding="utf-8")
        main_actions = js.split("function rowMainActions(", 1)[1].split("\n}", 1)[0]
        self.assertNotIn('cls: "primary"', main_actions)

    def test_inline_controls_match_antd_small_control_size(self):
        css = (ROOT / "web/style.css").read_text(encoding="utf-8")
        self.assertNotIn("font-weight: 650", css)
        ico = css.split("\n.ico {", 1)[1].split("}", 1)[0]
        self.assertIn("height: var(--control-h-sm)", ico)
        self.assertIn("border-radius: var(--radius-sm)", ico)
        self.assertNotIn("30px", css.split(".ico-row .btn.tiny {", 1)[1].split("}", 1)[0])

    def test_active_tab_uses_brand_text(self):
        css = (ROOT / "web/style.css").read_text(encoding="utf-8")
        tab = css.split(".tab-btn {", 1)[1].split("}", 1)[0]
        self.assertIn("height: var(--control-h)", tab)
        self.assertIn("border-radius: var(--radius-sm)", tab)
        active = css.split(".tab-btn.active {", 1)[1].split("}", 1)[0]
        self.assertIn("var(--brand", active)


class TicketMenuGroupsTest(unittest.TestCase):
    def test_groups_are_view_and_danger_without_swap(self):
        g = ops_ui.ticket_menu_groups({"id": "u", "hasRefresh": True})
        self.assertEqual([x["id"] for x in g["groups"]], ["view", "danger"])
        acts = [i["act"] for group in g["groups"] for i in group["items"]]
        self.assertNotIn("refreshLogin", acts)
        self.assertNotIn("refreshLoginKickOld", acts)
        self.assertNotIn("probeRefresh", acts)
        self.assertNotIn("dashboard", acts)

    def test_remove_is_alone_in_danger(self):
        g = ops_ui.ticket_menu_groups({})
        danger = g["groups"][-1]
        self.assertEqual([i["act"] for i in danger["items"]], ["remove"])
        self.assertEqual(danger.get("kind"), "danger")
        self.assertTrue(danger["items"][0].get("span"))

    def test_claim_hidden_when_already_ok(self):
        view = [i["act"] for i in ops_ui.ticket_menu_groups({}, {"kind": "ok"})["groups"][0]["items"]]
        self.assertNotIn("claim", view)
        view2 = [i["act"] for i in ops_ui.ticket_menu_groups({}, {"kind": "card"})["groups"][0]["items"]]
        self.assertIn("claim", view2)

    def test_token_actions_live_in_row_main_not_icons(self):
        acts = [i["act"] for group in ops_ui.ticket_menu_groups({})["groups"] for i in group["items"]]
        self.assertNotIn("showToken", acts)
        self.assertNotIn("copy", acts)
        off = {i["act"]: i for i in ops_ui.row_main_actions(token_on=False)}
        on = {i["act"]: i for i in ops_ui.row_main_actions(token_on=True)}
        self.assertEqual(off["showToken"]["label"], "显示 Token")
        self.assertEqual(on["showToken"]["label"], "隐藏 Token")
        self.assertEqual(off["showToken"]["cls"], "token")
        self.assertEqual(on["showToken"]["cls"], "token on")
        self.assertEqual(off["copy"]["label"], "复制 Token")
        self.assertEqual(off["copy"]["cls"], "copy")
        self.assertFalse(off["showToken"].get("keepOpen"))

    def test_pills_show_refresh_state(self):
        has_rt = ops_ui.ticket_menu_groups({"hasRefresh": True})["pills"]
        no_rt = ops_ui.ticket_menu_groups({"hasRefresh": False})["pills"]
        self.assertTrue(any("已有 Refresh" in p for p in has_rt))
        self.assertTrue(any("未探测 Refresh" in p for p in no_rt))


class OpsColumnLayoutContractTest(unittest.TestCase):
    def test_ops_column_narrower_with_token_outline_buttons(self):
        css = (ROOT / "web/style.css").read_text(encoding="utf-8")
        col = css.split("th.col-act, td.col-act {", 1)[1].split("}", 1)[0]
        self.assertIn("196px", col)
        self.assertNotIn("248px", col)
        wrap = css.split(".act-wrap .btn {", 1)[1].split("}", 1)[0]
        self.assertIn("padding: 0 4px", wrap)
        self.assertIn(".act-wrap .btn.token {", css)
        self.assertIn(".act-wrap .btn.copy {", css)
        js = (ROOT / "web/app.js").read_text(encoding="utf-8")
        main = js.split("function rowMainActions(", 1)[1].split("\n}", 1)[0]
        self.assertIn('act: "showToken"', main)
        self.assertIn('act: "copy"', main)
        self.assertIn('cls: "copy"', main)
        icons = js.split("function ticketMenuGroups(", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("showToken", icons)
        self.assertNotIn('"copy"', icons)
        self.assertNotIn("'copy'", icons)

class TagFilterUiContractTest(unittest.TestCase):
    def test_my_categories_controls_exist(self):
        html = (ROOT / "web/index.html").read_text(encoding="utf-8")
        for needle in (
            'id="tagFilter"',
            'id="btnManageTags"',
            'id="tagMask"',
            'id="tagPickMask"',
            'value="paid"',
        ):
            self.assertIn(needle, html)
        js = (ROOT / "web/app.js").read_text(encoding="utf-8")
        self.assertIn('listFilter === "paid"', js)
        boot = js[js.index("async function boot") :]
        self.assertIn('setListFilter(settings.listFilter || "paid", false)', boot)
        self.assertIn("setTagFilter(settings.tagFilter, false)", boot)
        filt = js[js.index("function setListFilter") : js.index("function setListFilter") + 400]
        self.assertIn("saveSettings({ listFilter", filt)
        tag = js[js.index("function setTagFilter") : js.index("function setTagFilter") + 500]
        self.assertIn("saveSettings({ tagFilter", tag)
        self.assertIn('tagFilter === "none"', js)
        self.assertIn('act === "editTags"', js)


if __name__ == "__main__":
    unittest.main()
