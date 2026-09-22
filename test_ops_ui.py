"""号池工作台：批量只处理勾选、顶栏 KPI、搜索筛选排序、首跑三任务。不联网。"""

import unittest
from pathlib import Path

import ops_ui
import sand_patch


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
        self.assertEqual(info["name"], "cursor账号管理器")

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

    def test_default_sort_is_newest_added_first(self):
        old = row("old")
        old["account"]["addedAt"] = 100
        mid = row("mid")
        mid["account"]["addedAt"] = 200
        new = row("new")
        new["account"]["addedAt"] = 300
        ordered = ops_ui.sort_rows([old, mid, new], now_ms=NOW)
        self.assertEqual([r["id"] for r in ordered], ["new", "mid", "old"])

    def test_logged_in_account_stays_above_newer_adds(self):
        old = row("old")
        old["account"]["addedAt"] = 100
        current = row("me")
        current["account"]["addedAt"] = 150
        new = row("new")
        new["account"]["addedAt"] = 300
        ordered = ops_ui.sort_rows([old, current, new], now_ms=NOW, local_user_id="me")
        self.assertEqual([r["id"] for r in ordered], ["me", "new", "old"])

    def test_logged_in_stays_on_top_when_sorting_by_remain(self):
        soon = row("soon", state={"billingCycleEndMs": NOW + 86400000})
        local = row("me", state={"billingCycleEndMs": NOW + 9 * 86400000})
        ordered = ops_ui.sort_rows(
            [soon, local], sort_by="remain", now_ms=NOW, local_user_id="me"
        )
        self.assertEqual([r["id"] for r in ordered], ["me", "soon"])

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
        html = Path("web/index.html").read_text(encoding="utf-8")
        for needle in (
            'id="appVersion"',
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
        ):
            self.assertIn(needle, html)
        import_block = html.split('id="importCard"', 1)[1].split('id="accountsCard"', 1)[0]
        self.assertNotIn('id="btnClear"', import_block)
        self.assertIn("本机号池工作台", html)
        js = Path("web/app.js").read_text(encoding="utf-8")
        self.assertIn('listSort = { key: "added", dir: 1 }', js)
        self.assertIn('listFilter === "paid"', js)
        self.assertIn('listFilter === "free"', js)
        self.assertIn("localUserId && x.a.id === localUserId", js)

    def test_ticket_sheet_is_grouped_not_flat_grid(self):
        html = Path("web/index.html").read_text(encoding="utf-8")
        self.assertIn('id="menuMeta"', html)
        self.assertIn('class="modal glass sheet"', html)
        self.assertNotIn('class="menu-grid" id="menuBody"', html)
        self.assertIn('id="menuBody"', html)
        css = Path("web/style.css").read_text(encoding="utf-8")
        self.assertIn(".menu-sheet", css)
        self.assertIn(".menu-group", css)

    def test_antd_primary_uses_brand_orange(self):
        css = Path("web/style.css").read_text(encoding="utf-8")
        self.assertIn("--brand: #fa8c16", css)
        primary = css.split(".btn.primary {", 1)[1].split("}", 1)[0]
        self.assertIn("var(--brand)", primary)
        self.assertNotIn("brand-action", primary)
        self.assertIn(".tab-bar", css)
        self.assertIn("var(--layout-bg)", css.split(".tab-bar {", 1)[1].split("}", 1)[0])


class TicketMenuGroupsTest(unittest.TestCase):
    def test_groups_are_view_and_danger_without_swap(self):
        g = ops_ui.ticket_menu_groups({"id": "u", "hasRefresh": True})
        self.assertEqual([x["id"] for x in g["groups"]], ["view", "danger"])
        acts = [i["act"] for group in g["groups"] for i in group["items"]]
        self.assertNotIn("refreshLogin", acts)
        self.assertNotIn("refreshLoginKickOld", acts)
        self.assertNotIn("probeRefresh", acts)

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

    def test_token_label_toggles_and_stays_open(self):
        off = {i["act"]: i for i in ops_ui.ticket_menu_groups({}, token_on=False)["groups"][0]["items"]}
        on = {i["act"]: i for i in ops_ui.ticket_menu_groups({}, token_on=True)["groups"][0]["items"]}
        self.assertEqual(off["showToken"]["label"], "显示 Token")
        self.assertEqual(on["showToken"]["label"], "隐藏 Token")
        self.assertTrue(off["showToken"].get("keepOpen"))

    def test_pills_show_refresh_state(self):
        has_rt = ops_ui.ticket_menu_groups({"hasRefresh": True})["pills"]
        no_rt = ops_ui.ticket_menu_groups({"hasRefresh": False})["pills"]
        self.assertTrue(any("已有 Refresh" in p for p in has_rt))
        self.assertTrue(any("未探测 Refresh" in p for p in no_rt))


if __name__ == "__main__":
    unittest.main()
