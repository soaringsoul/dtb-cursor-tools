"""检测登录时间：设备 createdAt 与检测时刻的间隔是否落在窗口内。不联网。"""

import unittest

import login_detect


NOW = 1_778_227_200_000  # 2026-05-08T00:00:00Z 任意固定点


def iso_ago(seconds):
    return login_detect.ms_to_iso(NOW - seconds * 1000)


class ClampWindowTest(unittest.TestCase):
    def test_default_120(self):
        self.assertEqual(login_detect.clamp_window_seconds(None), 120)
        self.assertEqual(login_detect.clamp_window_seconds(""), 120)
        self.assertEqual(login_detect.clamp_window_seconds("abc"), 120)

    def test_bounds(self):
        self.assertEqual(login_detect.clamp_window_seconds(1), 5)
        self.assertEqual(login_detect.clamp_window_seconds(99999), 3600)
        self.assertEqual(login_detect.clamp_window_seconds(120), 120)
        self.assertEqual(login_detect.clamp_window_seconds("90"), 90)


class IsRecentLoginTest(unittest.TestCase):
    def test_within_window(self):
        self.assertTrue(login_detect.is_recent_login(iso_ago(30), NOW, 120))
        self.assertTrue(login_detect.is_recent_login(iso_ago(120), NOW, 120))

    def test_outside_window(self):
        self.assertFalse(login_detect.is_recent_login(iso_ago(121), NOW, 120))
        self.assertFalse(login_detect.is_recent_login(iso_ago(3600), NOW, 120))

    def test_missing_created(self):
        self.assertFalse(login_detect.is_recent_login(None, NOW, 120))
        self.assertFalse(login_detect.is_recent_login("", NOW, 120))

    def test_small_future_skew_counts(self):
        self.assertTrue(login_detect.is_recent_login(login_detect.ms_to_iso(NOW + 2000), NOW, 120))

    def test_far_future_does_not_count(self):
        self.assertFalse(login_detect.is_recent_login(login_detect.ms_to_iso(NOW + 60_000), NOW, 120))

    def test_epoch_ms_input(self):
        self.assertTrue(login_detect.is_recent_login(NOW - 15_000, NOW, 120))


class RecentSessionIdsTest(unittest.TestCase):
    def test_picks_only_recent(self):
        rows = [
            {"sessionId": "old", "createdAt": iso_ago(400)},
            {"sessionId": "new", "createdAt": iso_ago(20)},
            {"sessionId": "web", "createdAt": iso_ago(80)},
            {"sessionId": "blank", "createdAt": iso_ago(10)},
        ]
        rows[3]["sessionId"] = "  "
        ids = login_detect.recent_session_ids(rows, NOW, 120)
        self.assertEqual(ids, ["new", "web"])


class CheckedAfterDetectTest(unittest.TestCase):
    def test_clears_previous_and_selects_recent(self):
        rows = [
            {"sessionId": "keep-old", "createdAt": iso_ago(400)},
            {"sessionId": "fresh-a", "createdAt": iso_ago(20)},
            {"sessionId": "fresh-b", "createdAt": iso_ago(80)},
        ]
        ids = login_detect.checked_ids_after_detect(
            rows, NOW, 120, previous_checked=["keep-old", "fresh-a"]
        )
        self.assertEqual(ids, ["fresh-a", "fresh-b"])
        self.assertNotIn("keep-old", ids)

    def test_none_recent_clears_all(self):
        rows = [{"sessionId": "old", "createdAt": iso_ago(400)}]
        ids = login_detect.checked_ids_after_detect(rows, NOW, 120, previous_checked=["old"])
        self.assertEqual(ids, [])


def _sess(sid, created, kind="client"):
    row = {"sessionId": sid, "createdAt": created, "type": kind}
    if kind == "client":
        row["typeRaw"] = "SESSION_TYPE_CLIENT"
    elif kind == "web":
        row["typeRaw"] = "SESSION_TYPE_WEB"
    return row


class PinLocalSessionIdTest(unittest.TestCase):
    def test_same_client_created_at_newer(self):
        before = [_sess("c1", iso_ago(400)), _sess("c2", iso_ago(300))]
        after = [_sess("c1", iso_ago(10)), _sess("c2", iso_ago(300))]
        self.assertEqual(login_detect.pin_local_session_id(before, after), "c1")

    def test_new_client_session(self):
        before = [_sess("c1", iso_ago(400))]
        after = [_sess("c1", iso_ago(400)), _sess("c-new", iso_ago(5))]
        self.assertEqual(login_detect.pin_local_session_id(before, after), "c-new")

    def test_unchanged_returns_empty(self):
        rows = [_sess("c1", iso_ago(400)), _sess("w1", iso_ago(10), "web")]
        self.assertEqual(login_detect.pin_local_session_id(rows, rows), "")

    def test_two_clients_changed_returns_empty(self):
        before = [_sess("c1", iso_ago(400)), _sess("c2", iso_ago(300))]
        after = [_sess("c1", iso_ago(10)), _sess("c2", iso_ago(8))]
        self.assertEqual(login_detect.pin_local_session_id(before, after), "")

    def test_web_only_change_ignored(self):
        before = [_sess("c1", iso_ago(400)), _sess("w1", iso_ago(400), "web")]
        after = [_sess("c1", iso_ago(400)), _sess("w1", iso_ago(5), "web")]
        self.assertEqual(login_detect.pin_local_session_id(before, after), "")


class MatchSessionByJwtTimeTest(unittest.TestCase):
    JWT_TIME = login_detect.created_at_ms("2026-09-16T11:25:18.000Z") // 1000

    def test_exact_created_at_match(self):
        rows = [_sess("c1", "2026-09-16T11:25:18.000Z"), _sess("c2", "2026-08-01T00:00:00.000Z")]
        self.assertEqual(
            login_detect.match_session_id_by_jwt_time(rows, self.JWT_TIME),
            "c1",
        )

    def test_two_clients_same_second_empty(self):
        rows = [
            _sess("c1", "2026-09-16T11:25:18.000Z"),
            _sess("c2", "2026-09-16T11:25:18.400Z"),
        ]
        self.assertEqual(login_detect.match_session_id_by_jwt_time(rows, self.JWT_TIME), "")

    def test_web_same_time_ignored(self):
        rows = [
            _sess("c1", "2026-08-01T00:00:00.000Z"),
            _sess("w1", "2026-09-16T11:25:18.000Z", "web"),
        ]
        self.assertEqual(login_detect.match_session_id_by_jwt_time(rows, self.JWT_TIME), "")

    def test_no_match_empty(self):
        rows = [_sess("c1", "2026-08-01T00:00:00.000Z")]
        self.assertEqual(login_detect.match_session_id_by_jwt_time(rows, self.JWT_TIME), "")

    def test_jwt_issued_ms_from_time_and_iat(self):
        self.assertEqual(
            login_detect.jwt_issued_ms({"time": str(self.JWT_TIME)}),
            self.JWT_TIME * 1000,
        )
        self.assertEqual(login_detect.jwt_issued_ms({"iat": self.JWT_TIME}), self.JWT_TIME * 1000)
        self.assertIsNone(login_detect.jwt_issued_ms({"sub": "user_x"}))

    def test_resolve_prefers_jwt_then_saved(self):
        rows = [_sess("c1", "2026-09-16T11:25:18.000Z"), _sess("c2", "2026-08-01T00:00:00.000Z")]
        self.assertEqual(
            login_detect.resolve_local_session_id(rows, {"time": str(self.JWT_TIME)}, "c2"),
            "c1",
        )
        old = [_sess("c2", "2026-08-01T00:00:00.000Z")]
        self.assertEqual(login_detect.resolve_local_session_id(old, {"time": str(self.JWT_TIME)}, "c2"), "c2")
        self.assertEqual(login_detect.resolve_local_session_id(old, {"time": str(self.JWT_TIME)}, "gone"), "")


class KeepSessionIdsForLocalGuardTest(unittest.TestCase):
    def test_keeps_ide_only_when_tool_same_or_empty(self):
        self.assertEqual(login_detect.keep_session_ids_for_local_guard("ide", "", ["ide", "other"]), ["ide"])
        self.assertEqual(login_detect.keep_session_ids_for_local_guard("ide", "ide", ["ide"]), ["ide"])

    def test_keeps_ide_and_distinct_tool(self):
        self.assertEqual(
            login_detect.keep_session_ids_for_local_guard("ide", "tool", ["ide", "tool", "other"]),
            ["ide", "tool"],
        )

    def test_empty_if_ide_missing_from_present(self):
        self.assertEqual(login_detect.keep_session_ids_for_local_guard("ide", "tool", ["tool"]), [])
        self.assertEqual(login_detect.keep_session_ids_for_local_guard("", "tool", ["tool"]), [])


class StaleToolSessionAfterRefreshTest(unittest.TestCase):
    OLD_TIME = login_detect.created_at_ms("2026-09-16T11:25:18.000Z") // 1000
    NEW_TIME = login_detect.created_at_ms("2026-09-16T14:42:11.000Z") // 1000

    def test_drops_old_tool_session_when_refresh_spawned_new_client(self):
        rows = [
            _sess("ide", "2026-09-16T10:00:00.000Z"),
            _sess("old-tool", "2026-09-16T11:25:18.000Z"),
            _sess("new-tool", "2026-09-16T14:42:11.000Z"),
        ]
        drop = login_detect.stale_tool_session_id_after_refresh(
            {"time": str(self.OLD_TIME)},
            {"time": str(self.NEW_TIME)},
            rows,
            "ide",
        )
        self.assertEqual(drop, "old-tool")

    def test_does_not_drop_ide_even_if_old_token_matched_it(self):
        rows = [
            _sess("ide", "2026-09-16T11:25:18.000Z"),
            _sess("new-tool", "2026-09-16T14:42:11.000Z"),
        ]
        drop = login_detect.stale_tool_session_id_after_refresh(
            {"time": str(self.OLD_TIME)},
            {"time": str(self.NEW_TIME)},
            rows,
            "ide",
        )
        self.assertEqual(drop, "")

    def test_empty_when_same_session_renewed(self):
        rows = [_sess("tool", "2026-09-16T11:25:18.000Z")]
        drop = login_detect.stale_tool_session_id_after_refresh(
            {"time": str(self.OLD_TIME)},
            {"time": str(self.OLD_TIME)},
            rows,
            "",
        )
        self.assertEqual(drop, "")


class SortSessionsForDisplayTest(unittest.TestCase):
    def test_local_then_tool_then_newest(self):
        rows = [
            _sess("old", "2026-08-01T00:00:00.000Z"),
            _sess("new-tool", "2026-09-16T14:42:11.000Z"),
            _sess("ide", "2026-09-16T10:00:00.000Z"),
            _sess("mid", "2026-09-01T00:00:00.000Z"),
        ]
        rows[2]["localMark"] = "local"
        rows[1]["toolMark"] = "tool"
        out = login_detect.sort_sessions_for_display(rows)
        self.assertEqual([x["sessionId"] for x in out], ["ide", "new-tool", "mid", "old"])

    def test_newest_first_when_unmarked(self):
        rows = [
            _sess("a", "2026-08-01T00:00:00.000Z"),
            _sess("c", "2026-09-16T14:42:11.000Z"),
            _sess("b", "2026-09-01T00:00:00.000Z"),
        ]
        out = login_detect.sort_sessions_for_display(rows)
        self.assertEqual([x["sessionId"] for x in out], ["c", "b", "a"])

    def test_fresh_unmarked_after_tool_before_old(self):
        rows = [
            _sess("old", "2026-08-01T00:00:00.000Z"),
            _sess("fresh", "2026-09-16T14:42:11.000Z"),
            _sess("tool", "2026-09-16T11:00:00.000Z"),
        ]
        rows[1]["freshMark"] = "fresh"
        rows[2]["toolMark"] = "tool"
        out = login_detect.sort_sessions_for_display(rows)
        self.assertEqual([x["sessionId"] for x in out], ["tool", "fresh", "old"])


if __name__ == "__main__":
    unittest.main()
