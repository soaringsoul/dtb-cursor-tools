"""本机设备保护：不联网，fetch_sessions / revoke_session 全部用假实现。"""

import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import device_guard
import sand_api

UID = "user_01GUARDTEST0000000000000000"
JWT = "eyJhbGciOiJub25lIn0.e30.x"

KEEP = "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"
OTHER_1 = "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"
OTHER_2 = "cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333"
NEWCOMER = "dddd4444dddd4444dddd4444dddd4444dddd4444dddd4444dddd4444dddd4444"


def _session(sid, kind="SESSION_TYPE_CLIENT"):
    return {"sessionId": sid, "type": kind, "createdAt": "2026-09-04T23:37:40.000Z", "expiresAt": "2026-11-03T23:37:40.000Z"}


def _block(*sids):
    return sand_api.normalize_sessions({"sessions": [_session(s) for s in sids]})


def _wait_until(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class _Cloud:
    """假的云端：sessions 是当前登录设备；revoke 成功后把设备从列表移除（模拟服务端行为）。"""

    def __init__(self, sids, revoke_ok=True, remove_on_revoke=True):
        self.lock = threading.Lock()
        self.sids = list(sids)
        self.revoked = []
        self.revoke_types = []
        self.fetch_calls = 0
        self.revoke_ok = revoke_ok
        self.remove_on_revoke = remove_on_revoke
        self.fetch_error = ""

    def fetch(self, user_id, jwt):
        with self.lock:
            self.fetch_calls += 1
            if self.fetch_error:
                return sand_api.empty_session_block(self.fetch_error)
            return _block(*self.sids)

    def revoke(self, user_id, jwt, sid, session_type=None):
        with self.lock:
            self.revoked.append(sid)
            self.revoke_types.append(session_type)
            if not self.revoke_ok:
                return {"ok": False, "error": "HTTP 500: boom", "status": 500}
            if self.remove_on_revoke and sid in self.sids:
                self.sids.remove(sid)
            return {"ok": True, "error": "", "status": 200}

    def add(self, sid):
        with self.lock:
            self.sids.append(sid)


class DeviceGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "device_guard.json")
        self.managers = []

    def tearDown(self):
        for mgr in self.managers:
            mgr.stop_all(wait=True, timeout=3.0)
        self.tmp.cleanup()

    def _read_state(self):
        with open(self.state, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _manager(self, cloud, tick=0.02, state=None):
        mgr = device_guard.DeviceGuardManager(
            state_path=state or self.state,
            fetch_sessions=cloud.fetch,
            revoke_session=cloud.revoke,
            tick_seconds=tick,
        )
        self.managers.append(mgr)
        return mgr

    # ---- 核心：踢未保留、留已保留 ----

    def test_tick_revokes_others_but_never_kept(self):
        cloud = _Cloud([KEEP, OTHER_1, OTHER_2])
        mgr = self._manager(cloud)
        res = mgr.start(UID, UID, JWT, [KEEP])
        self.assertTrue(res["ok"], res)
        self.assertTrue(_wait_until(lambda: set(cloud.revoked) >= {OTHER_1, OTHER_2}))
        # 再跑几轮，确认保留的那台永远不会被踢。
        time.sleep(0.1)
        self.assertNotIn(KEEP, cloud.revoked)
        self.assertEqual(cloud.sids, [KEEP])
        st = mgr.status()[UID]
        self.assertTrue(st["running"])
        self.assertEqual(st["kickedCount"], 2)
        self.assertEqual(st["keepIds"], [KEEP])
        self.assertEqual(st["sessionCount"], 1)
        self.assertEqual(st["lastError"], "")
        self.assertIsNotNone(st["lastTickAt"])
        kicked_ids = {x["sessionId"] for x in st["lastKicked"]}
        self.assertEqual(kicked_ids, {OTHER_1, OTHER_2})
        self.assertEqual(st["lastKicked"][0]["type"], "client")
        self.assertTrue(cloud.revoke_types)
        self.assertTrue(all(t == "SESSION_TYPE_CLIENT" for t in cloud.revoke_types))

    def test_new_device_appearing_later_is_kicked(self):
        cloud = _Cloud([KEEP])
        mgr = self._manager(cloud)
        mgr.start(UID, UID, JWT, [KEEP])
        self.assertTrue(_wait_until(lambda: cloud.fetch_calls >= 2))
        self.assertEqual(cloud.revoked, [])
        cloud.add(NEWCOMER)
        self.assertTrue(_wait_until(lambda: NEWCOMER in cloud.revoked))
        self.assertNotIn(KEEP, cloud.revoked)
        self.assertEqual(mgr.status()[UID]["kickedCount"], 1)

    def test_multiple_keep_ids(self):
        cloud = _Cloud([KEEP, OTHER_1, OTHER_2])
        mgr = self._manager(cloud)
        mgr.start(UID, UID, JWT, [KEEP, OTHER_1])
        self.assertTrue(_wait_until(lambda: OTHER_2 in cloud.revoked))
        time.sleep(0.08)
        self.assertEqual(set(cloud.revoked), {OTHER_2})

    # ---- 安全闸 ----

    def test_empty_keep_list_refused(self):
        cloud = _Cloud([KEEP, OTHER_1])
        mgr = self._manager(cloud)
        for bad in ([], None, ["", "  "]):
            res = mgr.start(UID, UID, JWT, bad)
            self.assertFalse(res["ok"])
            self.assertIn("保留名单为空", res["error"])
        time.sleep(0.06)
        self.assertEqual(cloud.fetch_calls, 0)
        self.assertEqual(cloud.revoked, [])
        self.assertNotIn(UID, mgr.status())
        self.assertFalse(mgr.is_running(UID))
        self.assertFalse(os.path.exists(self.state))

    def test_missing_auth_refused(self):
        cloud = _Cloud([KEEP])
        mgr = self._manager(cloud)
        self.assertFalse(mgr.start(UID, "", JWT, [KEEP])["ok"])
        self.assertFalse(mgr.start(UID, UID, "", [KEEP])["ok"])
        self.assertFalse(mgr.start("", UID, JWT, [KEEP])["ok"])
        self.assertEqual(cloud.fetch_calls, 0)

    def test_fetch_error_does_not_revoke_and_records_last_error(self):
        cloud = _Cloud([KEEP, OTHER_1])
        cloud.fetch_error = "会话接口无响应"
        mgr = self._manager(cloud)
        mgr.start(UID, UID, JWT, [KEEP])
        self.assertTrue(_wait_until(lambda: cloud.fetch_calls >= 3))
        self.assertEqual(cloud.revoked, [])
        st = mgr.status()[UID]
        self.assertTrue(st["running"])
        self.assertIn("无响应", st["lastError"])
        # 网络恢复后自动继续踢，lastError 清空。
        with cloud.lock:
            cloud.fetch_error = ""
        self.assertTrue(_wait_until(lambda: OTHER_1 in cloud.revoked))
        self.assertTrue(_wait_until(lambda: mgr.status()[UID]["lastError"] == ""))

    def test_fetch_raising_is_caught(self):
        calls = {"n": 0}

        def boom(user_id, jwt):
            calls["n"] += 1
            raise RuntimeError("kaboom")

        mgr = device_guard.DeviceGuardManager(
            state_path=self.state, fetch_sessions=boom, revoke_session=lambda *a: {"ok": True}, tick_seconds=0.02
        )
        self.managers.append(mgr)
        mgr.start(UID, UID, JWT, [KEEP])
        self.assertTrue(_wait_until(lambda: calls["n"] >= 3))
        st = mgr.status()[UID]
        self.assertTrue(st["running"])
        self.assertIn("kaboom", st["lastError"])

    def test_revoke_failure_recorded_and_retried_later(self):
        cloud = _Cloud([KEEP, OTHER_1], revoke_ok=False)
        mgr = self._manager(cloud)
        with patch.object(device_guard, "REVOKE_RETRY", 0.05):
            mgr.start(UID, UID, JWT, [KEEP])
            self.assertTrue(_wait_until(lambda: len(cloud.revoked) >= 1))
            st = mgr.status()[UID]
            self.assertEqual(st["kickedCount"], 0)
            self.assertIn("HTTP 500", st["lastError"])
            self.assertTrue(_wait_until(lambda: len(cloud.revoked) >= 2))
        self.assertEqual(set(cloud.revoked), {OTHER_1})

    def test_revoke_not_counted_until_session_disappears(self):
        # 接口对错误 body 也会 200 {}。只有列表里真的少了这台，才算踢掉；还在就重试。
        cloud = _Cloud([KEEP, OTHER_1], remove_on_revoke=False)
        mgr = self._manager(cloud)
        with patch.object(device_guard, "REVOKE_RETRY", 0.05):
            mgr.start(UID, UID, JWT, [KEEP])
            self.assertTrue(_wait_until(lambda: len(cloud.revoked) >= 1))
            self.assertEqual(mgr.status()[UID]["kickedCount"], 0)
            self.assertTrue(_wait_until(lambda: len(cloud.revoked) >= 2))
            self.assertEqual(mgr.status()[UID]["kickedCount"], 0)
            cloud.remove_on_revoke = True
            self.assertTrue(_wait_until(lambda: mgr.status()[UID]["kickedCount"] == 1))
        self.assertNotIn(OTHER_1, cloud.sids)

    def test_auto_stop_after_persistent_auth_failure(self):
        cloud = _Cloud([KEEP, OTHER_1])
        cloud.fetch_error = "会话接口 HTTP 401"
        mgr = self._manager(cloud)
        with patch.object(device_guard, "AUTH_FAIL_LIMIT", 3):
            mgr.start(UID, UID, JWT, [KEEP])
            self.assertTrue(_wait_until(lambda: not mgr.is_running(UID)))
        st = mgr.status()[UID]
        self.assertFalse(st["running"])
        self.assertEqual(st["stopReason"], "auth")
        self.assertIn("自动停止", st["lastError"])
        self.assertEqual(cloud.revoked, [])

    def test_waf_block_does_not_count_as_dead_token(self):
        cloud = _Cloud([KEEP, OTHER_1])
        cloud.fetch_error = "会话接口被网站人机校验拦截，请稍后再刷新（不要连续狂点）"
        mgr = self._manager(cloud)
        with patch.object(device_guard, "AUTH_FAIL_LIMIT", 3):
            mgr.start(UID, UID, JWT, [KEEP])
            self.assertTrue(_wait_until(lambda: cloud.fetch_calls >= 4))
        self.assertTrue(mgr.is_running(UID))
        self.assertNotEqual(mgr.status()[UID].get("stopReason"), "auth")
        self.assertEqual(cloud.revoked, [])
        mgr.stop(UID, wait=True)

    # ---- 停止 ----

    def test_stop_actually_stops(self):
        cloud = _Cloud([KEEP, OTHER_1])
        mgr = self._manager(cloud)
        mgr.start(UID, UID, JWT, [KEEP])
        self.assertTrue(_wait_until(lambda: cloud.fetch_calls >= 2))
        res = mgr.stop(UID, wait=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["wasRunning"])
        self.assertFalse(mgr.is_running(UID))
        self.assertFalse(any(t.name == f"device-guard-{UID}" and t.is_alive() for t in threading.enumerate()))
        fetched = cloud.fetch_calls
        cloud.add(NEWCOMER)
        time.sleep(0.12)  # 好几个 tick 周期
        self.assertEqual(cloud.fetch_calls, fetched)
        self.assertNotIn(NEWCOMER, cloud.revoked)
        st = mgr.status()[UID]
        self.assertFalse(st["running"])
        self.assertFalse(st["stopping"])
        self.assertEqual(st["stopReason"], "user")
        self.assertEqual(st["keepIds"], [KEEP])
        # 停止是幂等的。
        again = mgr.stop(UID, wait=True)
        self.assertTrue(again["ok"])
        self.assertFalse(again["wasRunning"])

    def test_stop_all(self):
        cloud_a = _Cloud([KEEP, OTHER_1])
        cloud_b = _Cloud([KEEP, OTHER_2])
        mgr = device_guard.DeviceGuardManager(state_path=self.state, tick_seconds=0.02)
        self.managers.append(mgr)
        # 两个账号共用一个 manager；用账号 id 分流到各自的假云端。
        clouds = {"user_a": cloud_a, "user_b": cloud_b}
        mgr._fetch = lambda uid, jwt: clouds[uid].fetch(uid, jwt)
        mgr._revoke = lambda uid, jwt, sid, *rest: clouds[uid].revoke(uid, jwt, sid, rest[0] if rest else None)
        mgr.start("user_a", "user_a", JWT, [KEEP])
        mgr.start("user_b", "user_b", JWT, [KEEP])
        self.assertTrue(_wait_until(lambda: OTHER_1 in cloud_a.revoked and OTHER_2 in cloud_b.revoked))
        res = mgr.stop_all(wait=True)
        self.assertEqual(sorted(res["stopped"]), ["user_a", "user_b"])
        self.assertFalse(mgr.is_running("user_a"))
        self.assertFalse(mgr.is_running("user_b"))
        st = mgr.status()
        self.assertFalse(st["user_a"]["running"])
        self.assertFalse(st["user_b"]["running"])

    def test_restart_replaces_keep_list_without_overlapping_loops(self):
        cloud = _Cloud([KEEP, OTHER_1, OTHER_2], remove_on_revoke=False)
        mgr = self._manager(cloud)
        mgr.start(UID, UID, JWT, [KEEP, OTHER_1, OTHER_2])
        self.assertTrue(_wait_until(lambda: cloud.fetch_calls >= 2))
        self.assertEqual(cloud.revoked, [])
        res = mgr.start(UID, UID, JWT, [KEEP])
        self.assertTrue(res["ok"])
        self.assertTrue(_wait_until(lambda: set(cloud.revoked) >= {OTHER_1, OTHER_2}))
        # 同一账号始终只有一个循环线程在跑。
        def loops():
            return [t for t in threading.enumerate() if t.name == f"device-guard-{UID}" and t.is_alive()]

        self.assertTrue(_wait_until(lambda: len(loops()) == 1))
        st = mgr.status()[UID]
        self.assertTrue(st["running"])
        self.assertEqual(st["keepIds"], [KEEP])

    def test_forget_stops_and_clears_memory(self):
        cloud = _Cloud([KEEP, OTHER_1])
        mgr = self._manager(cloud)
        mgr.start(UID, UID, JWT, [KEEP])
        self.assertTrue(_wait_until(lambda: OTHER_1 in cloud.revoked))
        mgr.forget(UID)
        self.assertFalse(mgr.is_running(UID))
        # 线程还在收尾时就已经不再上报；线程退出后也不留「最后一帧」。
        self.assertNotIn(UID, mgr.status())
        self.assertTrue(_wait_until(lambda: not any(t.name == f"device-guard-{UID}" and t.is_alive() for t in threading.enumerate())))
        self.assertNotIn(UID, mgr.status())
        self.assertEqual(mgr.saved_keep_ids(UID), [])
        self.assertEqual(self._read_state(), {})

    # ---- 持久化：只回填名单，不自动开 ----

    def test_state_persisted_and_restored_without_autostart(self):
        cloud = _Cloud([KEEP, OTHER_1])
        mgr = self._manager(cloud)
        mgr.start(UID, UID, JWT, [KEEP])
        self.assertTrue(_wait_until(lambda: OTHER_1 in cloud.revoked))
        self.assertTrue(_wait_until(lambda: self._read_state()[UID]["kickedCount"] == 1))
        data = self._read_state()
        self.assertEqual(data[UID]["keepIds"], [KEEP])
        self.assertTrue(data[UID]["running"])
        # 模拟程序没正常退出直接重开：名单回填、标记「上次在跑」，但绝不自动启动。
        cloud2 = _Cloud([KEEP, OTHER_1])
        mgr2 = self._manager(cloud2)
        st = mgr2.status()[UID]
        self.assertFalse(st["running"])
        self.assertTrue(st["wasRunning"])
        self.assertTrue(st["saved"])
        self.assertEqual(st["keepIds"], [KEEP])
        self.assertEqual(st["kickedCount"], 1)
        self.assertEqual(mgr2.saved_keep_ids(UID), [KEEP])
        time.sleep(0.08)
        self.assertEqual(cloud2.fetch_calls, 0)
        self.assertEqual(cloud2.revoked, [])
        # 正常停止后落盘 running=False。
        mgr.stop(UID, wait=True)
        self.assertFalse(self._read_state()[UID]["running"])

    def test_corrupt_state_file_is_ignored(self):
        with open(self.state, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        mgr = self._manager(_Cloud([KEEP]))
        self.assertEqual(mgr.status(), {})
        with open(self.state, "w", encoding="utf-8") as handle:
            json.dump({UID: {"keepIds": []}, "x": 5}, handle)
        mgr = self._manager(_Cloud([KEEP]))
        self.assertEqual(mgr.status(), {})


class CleanIdsTest(unittest.TestCase):
    def test_clean_ids(self):
        self.assertEqual(device_guard.clean_ids([" a ", "b", "a", "", None, 3]), ["a", "b", "3"])
        self.assertEqual(device_guard.clean_ids(None), [])


class IntervalMinutesTest(unittest.TestCase):
    def test_clean_interval_minutes_clamps(self):
        self.assertEqual(device_guard.clean_interval_minutes(1), 1)
        self.assertEqual(device_guard.clean_interval_minutes("5"), 5)
        self.assertEqual(device_guard.clean_interval_minutes(0), 1)
        self.assertEqual(device_guard.clean_interval_minutes(-3), 1)
        self.assertEqual(device_guard.clean_interval_minutes(999), device_guard.MAX_INTERVAL_MINUTES)
        self.assertEqual(device_guard.clean_interval_minutes(None), device_guard.DEFAULT_INTERVAL_MINUTES)
        self.assertEqual(device_guard.clean_interval_minutes("nope"), device_guard.DEFAULT_INTERVAL_MINUTES)

    def test_start_without_interval_keeps_manager_tick(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cloud = _Cloud([KEEP])
        mgr = device_guard.DeviceGuardManager(
            state_path=os.path.join(tmp.name, "g.json"),
            fetch_sessions=cloud.fetch,
            revoke_session=cloud.revoke,
            tick_seconds=0.02,
        )
        self.addCleanup(lambda: mgr.stop_all(wait=True, timeout=3.0))
        mgr.start(UID, UID, JWT, [KEEP])
        guard = mgr._guards[UID]
        self.assertEqual(guard.tick_seconds, 0.02)
        self.assertIsNone(guard.interval_minutes)
        self.assertIsNone(mgr.status()[UID].get("intervalMinutes"))

    def test_start_interval_minutes_sets_per_guard_wait_and_persists(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = os.path.join(tmp.name, "g.json")
        cloud = _Cloud([KEEP])
        mgr = device_guard.DeviceGuardManager(
            state_path=state,
            fetch_sessions=cloud.fetch,
            revoke_session=cloud.revoke,
            tick_seconds=0.02,
        )
        self.addCleanup(lambda: mgr.stop_all(wait=True, timeout=3.0))
        res = mgr.start(UID, UID, JWT, [KEEP], interval_minutes=3)
        self.assertTrue(res["ok"], res)
        guard = mgr._guards[UID]
        self.assertEqual(guard.interval_minutes, 3)
        self.assertEqual(guard.tick_seconds, 3 * device_guard.SECONDS_PER_MINUTE)
        self.assertEqual(mgr.status()[UID]["intervalMinutes"], 3)
        with open(state, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved[UID]["intervalMinutes"], 3)
        mgr.stop(UID, wait=True)
        st = mgr.status()[UID]
        self.assertFalse(st["running"])
        self.assertEqual(st["intervalMinutes"], 3)
        mgr2 = device_guard.DeviceGuardManager(
            state_path=state,
            fetch_sessions=cloud.fetch,
            revoke_session=cloud.revoke,
            tick_seconds=0.02,
        )
        self.addCleanup(lambda: mgr2.stop_all(wait=True, timeout=3.0))
        self.assertEqual(mgr2.status()[UID]["intervalMinutes"], 3)


if __name__ == "__main__":
    unittest.main()
