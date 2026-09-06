"""登录会话识别：不联网。"""

import base64
import json
import unittest
from unittest.mock import patch

import sand_api


def _jwt(sub="user_01TESTSESSIONS000000000000"):
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub, "type": "session"}).encode())
        .decode()
        .rstrip("=")
    )
    return "eyJhbGciOiJub25lIn0." + payload + ".x"


SAMPLE = {
    "sessions": [
        {
            "sessionId": "1c3c233a6194e5eb6463e48122d093c0537507a385c6475dc1485884c511f2a6",
            "type": "SESSION_TYPE_CLIENT",
            "createdAt": "2026-09-04T23:37:40.000Z",
            "expiresAt": "2026-11-03T23:37:40.000Z",
        },
        {
            "sessionId": "9c5904911dd4a73f2883bd6a6f391e4d1d25422e653ede8c46613af700838eed",
            "type": "SESSION_TYPE_WEB",
            "createdAt": "2026-09-05T00:19:51.000Z",
            "expiresAt": "2026-11-04T00:19:51.000Z",
        },
    ]
}


class NormalizeSessionsTest(unittest.TestCase):
    def test_client_and_web_counts(self):
        block = sand_api.normalize_sessions(SAMPLE)
        self.assertEqual(block["sessionCount"], 2)
        self.assertEqual(block["sessionClientCount"], 1)
        self.assertEqual(block["sessionWebCount"], 1)
        self.assertEqual(block["sessionError"], "")
        types = [x["type"] for x in block["sessions"]]
        self.assertEqual(types, ["client", "web"])
        self.assertEqual(block["sessions"][0]["typeRaw"], "SESSION_TYPE_CLIENT")
        self.assertEqual(block["sessions"][0]["sessionId"], SAMPLE["sessions"][0]["sessionId"])

    def test_unknown_type_counts_as_other(self):
        block = sand_api.normalize_sessions(
            {"sessions": [{"sessionId": "ab", "type": "SESSION_TYPE_MOBILE"}]}
        )
        self.assertEqual(block["sessionCount"], 1)
        self.assertEqual(block["sessionClientCount"], 0)
        self.assertEqual(block["sessionWebCount"], 0)
        self.assertEqual(block["sessions"][0]["type"], "other")

    def test_skips_bad_items(self):
        block = sand_api.normalize_sessions(
            {"sessions": [None, 3, {"sessionId": "ok", "type": "SESSION_TYPE_WEB"}]}
        )
        self.assertEqual(block["sessionCount"], 1)
        self.assertEqual(block["sessionWebCount"], 1)

    def test_bad_payload(self):
        block = sand_api.normalize_sessions("nope")
        self.assertEqual(block["sessions"], [])
        self.assertEqual(block["sessionCount"], 0)
        self.assertTrue(block["sessionError"])


class FetchSessionsTest(unittest.TestCase):
    def test_http_200(self):
        with patch.object(sand_api, "_get", return_value=(200, json.dumps(SAMPLE))):
            block = sand_api.fetch_sessions("user_01TESTSESSIONS000000000000", _jwt())
        self.assertEqual(block["sessionCount"], 2)
        self.assertEqual(block["sessionError"], "")

    def test_http_401_does_not_raise(self):
        with patch.object(sand_api, "_get", return_value=(401, '{"error":"no"}')):
            block = sand_api.fetch_sessions("user_01TESTSESSIONS000000000000", _jwt())
        self.assertEqual(block["sessions"], [])
        self.assertIn("401", block["sessionError"])

    def test_invalid_json(self):
        with patch.object(sand_api, "_get", return_value=(200, "not-json")):
            block = sand_api.fetch_sessions("user_01TESTSESSIONS000000000000", _jwt())
        self.assertEqual(block["sessions"], [])
        self.assertTrue(block["sessionError"])

    def test_network_zero(self):
        with patch.object(sand_api, "_get", return_value=(0, "timeout")):
            block = sand_api.fetch_sessions("user_01TESTSESSIONS000000000000", _jwt())
        self.assertEqual(block["sessionCount"], 0)
        self.assertIn("无响应", block["sessionError"])


class GetStatusSessionsTest(unittest.TestCase):
    def _dispatch(self, url, headers, body="{}"):
        if url == sand_api.SAND_USAGE_URL:
            return 200, json.dumps({"hasNonZeroIncludedLimit": True, "usagePercent": 1})
        if url == sand_api.GET_ME_URL:
            return 200, json.dumps({"email": "a@b.c"})
        if url == sand_api.ACCESS_STATUS_URL:
            return 200, json.dumps({"state": "SAND_ACCESS_STATE_GRANTED"})
        if url == sand_api.TEAM_SPEND_URL:
            return 200, json.dumps({"teamMemberSpend": []})
        return 200, "{}"

    def _get(self, url, headers):
        if url == sand_api.SESSIONS_URL:
            return 200, json.dumps(SAMPLE)
        if url == sand_api.USAGE_SUMMARY_URL:
            return 200, json.dumps(
                {
                    "membershipType": "pro",
                    "individualUsage": {"plan": {"autoPercentUsed": 1, "apiPercentUsed": 2}},
                }
            )
        if url == sand_api.STRIPE_URL:
            return 200, json.dumps({"membershipType": "pro", "subscriptionStatus": "active"})
        return 404, ""

    def test_get_status_includes_sessions_alive_from_usage(self):
        with patch.object(sand_api, "_post", side_effect=self._dispatch), patch.object(
            sand_api, "_get", side_effect=self._get
        ):
            out = sand_api.get_status(_jwt())
        self.assertEqual(out["sessionCount"], 2)
        self.assertEqual(out["sessionClientCount"], 1)
        self.assertTrue(out["alive"])
        self.assertEqual(out["sessionError"], "")

    def test_sessions_401_does_not_kill_account(self):
        def get(url, headers):
            if url == sand_api.SESSIONS_URL:
                return 401, "{}"
            return self._get(url, headers)

        with patch.object(sand_api, "_post", side_effect=self._dispatch), patch.object(
            sand_api, "_get", side_effect=get
        ):
            out = sand_api.get_status(_jwt())
        self.assertTrue(out["alive"])
        self.assertEqual(out["sessionCount"], 0)
        self.assertIn("401", out["sessionError"])


if __name__ == "__main__":
    unittest.main()
