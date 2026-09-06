"""账号存储 / 额度解析 / 验证 / 导出文本：不联网。"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import accounts
import sand_api
from app import build_export_text


def _jwt(sub="user_01TESTACCT0000000000000000", typ="session", exp=2_000_000_000, extra=None):
    payload = {"sub": sub, "type": typ, "exp": exp}
    if extra:
        payload.update(extra)
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + body + ".signature"


def _ws(sub="user_01TESTACCT0000000000000000", **kwargs):
    return f"{sub}::{_jwt(sub, **kwargs)}"


class ParseTokenTest(unittest.TestCase):
    def test_ws_token(self):
        token = _ws()
        uid, jwt, claims = sand_api.parse_token(token)
        self.assertEqual(uid, "user_01TESTACCT0000000000000000")
        self.assertTrue(jwt.startswith("eyJ"))
        self.assertEqual(claims.get("type"), "session")

    def test_percent_encoded_sep(self):
        sub = "user_01TESTACCT0000000000000000"
        token = f"{sub}%3A%3A{_jwt(sub)}"
        uid, jwt, _claims = sand_api.parse_token(token)
        self.assertEqual(uid, sub)
        self.assertTrue(jwt.startswith("eyJ"))

    def test_bare_jwt_sub(self):
        uid, jwt, _claims = sand_api.parse_token(_jwt())
        self.assertEqual(uid, "user_01TESTACCT0000000000000000")
        self.assertTrue(jwt.startswith("eyJ"))

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            sand_api.parse_token("   ")


class TokenExtractTest(unittest.TestCase):
    def test_labeled_line_maps_email(self):
        token = _ws()
        text = f"a@b.com----{token}"
        labels = accounts.labels_from_text(text)
        self.assertEqual(labels.get(token), "a@b.com")

    def test_json_prefers_access_over_refresh(self):
        access = _ws("user_01TESTPRIO000000000000000")
        refresh = _ws("user_01TESTPRIO000000000000000", extra={"jti": "refresh"})
        raw = json.dumps({"refresh_token": refresh, "access_token": access})
        pairs = accounts.tokens_from_json_text(raw)
        prios = {tok: prio for prio, tok in pairs}
        self.assertEqual(prios[access], 5)
        self.assertEqual(prios[refresh], 1)


class AccountStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "accounts.json")
        self.patcher = patch.object(accounts, "_store_path", return_value=self.path)
        self.patcher.start()
        self.store = accounts.AccountStore()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_dedup_same_user(self):
        a = _ws("user_01TESTDEDUP00000000000000")
        b = _ws("user_01TESTDEDUP00000000000000", extra={"n": 2})
        self.store.add_text(f"one@x.com----{a}\n{b}")
        rows = self.store.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "user_01TESTDEDUP00000000000000")
        self.assertEqual(rows[0]["label"], "one@x.com")

    def test_refresh_does_not_override_access(self):
        uid = "user_01TESTPRIO000000000000000"
        access = _ws(uid)
        refresh = _ws(uid, extra={"jti": "r"})
        self.store.add_text(json.dumps({"access_token": access}))
        self.store.add_text(json.dumps({"refresh_token": refresh}))
        item = self.store.get(uid)
        self.assertEqual(item["token"], access)

    def test_added_at_stable_on_reimport(self):
        token = _ws("user_01TESTADD0000000000000000")
        self.store.add_text(token)
        first = self.store.get("user_01TESTADD0000000000000000")["addedAt"]
        time.sleep(0.01)
        self.store.add_text(token)
        second = self.store.get("user_01TESTADD0000000000000000")["addedAt"]
        self.assertEqual(first, second)

    def test_export_line_roundtrip_label(self):
        token = _ws("user_01TESTEXP0000000000000000")
        self.store.add_text(f"exp@x.com----{token}")
        line = accounts.format_export_line(self.store.get("user_01TESTEXP0000000000000000"))
        self.assertTrue(line.startswith("exp@x.com----user_01TESTEXP0000000000000000::"))
        again = self.store.add_text(line)
        self.assertEqual(again[0]["id"], "user_01TESTEXP0000000000000000")


class UsageFieldsTest(unittest.TestCase):
    def test_three_pools_split(self):
        body = {
            "membershipType": "pro",
            "individualUsage": {
                "plan": {
                    "autoPercentUsed": 62.13,
                    "apiPercentUsed": 100,
                    "totalPercentUsed": 66.19,
                    "used": 2000,
                    "limit": 2000,
                    "breakdown": {"total": 2500},
                },
                "onDemand": {"enabled": True, "used": 123},
            },
        }
        fields = sand_api._general_usage_fields(body)
        self.assertEqual(fields["autoPercent"], 62.13)
        self.assertEqual(fields["apiPercent"], 100.0)
        self.assertEqual(fields["totalPercent"], 66.19)
        self.assertEqual(fields["onDemandUsedCents"], 123.0)
        self.assertTrue(fields["onDemandEnabled"])
        self.assertNotEqual(fields["totalPercent"], fields["autoPercent"])
        self.assertNotEqual(fields["totalPercent"], fields["apiPercent"])

    def test_sand_unlock_and_estimated_reset(self):
        body = {
            "hasNonZeroIncludedLimit": True,
            "usagePercent": 1.5,
            "currentPeriodStart": "2020-01-01T00:00:00.000Z",
        }
        fields = sand_api._sand_usage_fields(body)
        self.assertTrue(fields["unlocked"])
        self.assertEqual(fields["percent"], 1.5)
        self.assertTrue(fields["nextResetEstimated"])
        self.assertTrue(fields["nextReset"])


class AliveFromCodesTest(unittest.TestCase):
    def test_any_200_alive(self):
        self.assertTrue(sand_api.alive_from_codes(401, 200))

    def test_401_403_dead(self):
        self.assertIs(sand_api.alive_from_codes(401, 403), False)

    def test_all_zero_unknown(self):
        self.assertIsNone(sand_api.alive_from_codes(0, 0))


class VerifyTest(unittest.TestCase):
    def test_expired_jwt_offline(self):
        token = _jwt(exp=1)
        with patch.object(sand_api, "_get") as get, patch.object(sand_api, "_post") as post:
            out = sand_api.verify(token)
        self.assertIs(out["alive"], False)
        self.assertIn("过期", out["aliveReason"])
        get.assert_not_called()
        post.assert_not_called()

    def test_probe_dead_and_api2_dead(self):
        token = _ws()

        def get(url, headers):
            if url == sand_api.AUTH_ME_URL:
                return 204, ""
            return 0, ""

        with patch.object(sand_api, "_get", side_effect=get), patch.object(
            sand_api, "_post", return_value=(401, "no")
        ):
            out = sand_api.verify(token)
        self.assertIs(out["alive"], False)


class ExportTextTest(unittest.TestCase):
    def test_annotations_stay_in_comments(self):
        uid = "user_01TESTOUT0000000000000000"
        token = _ws(uid)
        item = {"id": uid, "label": "out@x.com", "token": token}

        class Store:
            def get(self, account_id):
                return item if account_id == uid else None

        text, count = build_export_text(
            Store(),
            {
                "header": ["Ultra 1"],
                "sections": [
                    {
                        "title": "Ultra · 未用 Bot",
                        "note": "按套餐",
                        "ids": [uid, uid],
                        "annotations": {uid: "剩 3天 · Bot 0%"},
                    }
                ],
            },
        )
        self.assertEqual(count, 1)
        account_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(len(account_lines), 1)
        self.assertTrue(account_lines[0].startswith("out@x.com----" + uid + "::"))
        self.assertNotIn("剩 3天", account_lines[0])
        self.assertIn("剩 3天 · Bot 0%", text)
        self.assertIn("# ===== Ultra · 未用 Bot", text)
        self.assertTrue(text.startswith("# Ultra 1"))


if __name__ == "__main__":
    unittest.main()
