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


if __name__ == "__main__":
    unittest.main()
