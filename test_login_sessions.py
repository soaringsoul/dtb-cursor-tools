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
        self.assertFalse(block["sessionWaf"])
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


class AnnotateLocalSessionsTest(unittest.TestCase):
    def test_not_local_account_has_no_marks(self):
        rows = sand_api.normalize_sessions(SAMPLE)["sessions"]
        out = sand_api.annotate_local_sessions(rows, False)
        self.assertEqual([x["localMark"] for x in out], [None, None])

    def test_local_account_single_client_is_local(self):
        rows = sand_api.normalize_sessions(SAMPLE)["sessions"]
        out = sand_api.annotate_local_sessions(rows, True)
        marks = {x["type"]: x["localMark"] for x in out}
        self.assertEqual(marks["client"], "local")
        self.assertIsNone(marks["web"])

    def test_local_account_multiple_clients_are_maybe(self):
        payload = {
            "sessions": [
                SAMPLE["sessions"][0],
                {
                    "sessionId": "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222",
                    "type": "SESSION_TYPE_CLIENT",
                },
                SAMPLE["sessions"][1],
            ]
        }
        rows = sand_api.normalize_sessions(payload)["sessions"]
        out = sand_api.annotate_local_sessions(rows, True)
        clients = [x["localMark"] for x in out if x["type"] == "client"]
        webs = [x["localMark"] for x in out if x["type"] == "web"]
        self.assertEqual(clients, ["maybe-local", "maybe-local"])
        self.assertEqual(webs, [None])

    def test_does_not_mutate_input(self):
        rows = sand_api.normalize_sessions(SAMPLE)["sessions"]
        sand_api.annotate_local_sessions(rows, True)
        self.assertNotIn("localMark", rows[0])


class FetchSessionsTest(unittest.TestCase):
    def test_http_200(self):
        with patch.object(sand_api, "_get", return_value=(200, json.dumps(SAMPLE))):
            block = sand_api.fetch_sessions("user_01TESTSESSIONS000000000000", _jwt())
        self.assertEqual(block["sessionCount"], 2)
        self.assertEqual(block["sessionError"], "")
        self.assertFalse(block["sessionWaf"])

    def test_http_401_does_not_raise(self):
        with patch.object(sand_api, "_get", return_value=(401, '{"error":"no"}')):
            block = sand_api.fetch_sessions("user_01TESTSESSIONS000000000000", _jwt())
        self.assertEqual(block["sessions"], [])
        self.assertIn("401", block["sessionError"])

    def test_sends_origin_and_referer(self):
        calls = []

        def get(url, headers):
            calls.append((url, headers))
            return 200, json.dumps(SAMPLE)

        with patch.object(sand_api, "_get", side_effect=get):
            block = sand_api.fetch_sessions("user_01TESTSESSIONS000000000000", _jwt())
        self.assertEqual(block["sessionCount"], 2)
        self.assertEqual(len(calls), 1)
        url, headers = calls[0]
        self.assertEqual(url, sand_api.SESSIONS_URL)
        self.assertEqual(headers["origin"], sand_api.ORIGIN)
        self.assertEqual(headers["referer"], sand_api.DASHBOARD_REFERER)

    def test_vercel_checkpoint_is_not_plain_http_403(self):
        html = "<!DOCTYPE html><html><title>Vercel Security Checkpoint</title><p>We're verifying your browser</p></html>"
        with patch.object(sand_api, "_get", return_value=(403, html)):
            block = sand_api.fetch_sessions("user_01TESTSESSIONS000000000000", _jwt())
        self.assertEqual(block["sessions"], [])
        self.assertEqual(block["sessionError"], sand_api.WAF_SESSIONS_ERROR)
        self.assertTrue(block["sessionWaf"])
        self.assertNotIn("HTTP 403", block["sessionError"])

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
        self.assertFalse(out["sessionWaf"])

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


class RevokeTypeValueTest(unittest.TestCase):
    def test_maps_dashboard_enums(self):
        self.assertEqual(sand_api.revoke_type_value("SESSION_TYPE_WEB"), 1)
        self.assertEqual(sand_api.revoke_type_value("web"), 1)
        self.assertEqual(sand_api.revoke_type_value("SESSION_TYPE_CLIENT"), 2)
        self.assertEqual(sand_api.revoke_type_value("client"), 2)
        self.assertEqual(sand_api.revoke_type_value("SESSION_TYPE_MOBILE"), 10)
        self.assertEqual(sand_api.revoke_type_value("SESSION_TYPE_CHROME_EXTENSION"), 11)
        self.assertEqual(sand_api.revoke_type_value(2), 2)
        self.assertIsNone(sand_api.revoke_type_value(""))
        self.assertIsNone(sand_api.revoke_type_value("other"))


class RevokeSessionTest(unittest.TestCase):
    UID = "user_01TESTSESSIONS000000000000"
    SID = SAMPLE["sessions"][1]["sessionId"]

    def test_http_200_is_success_and_sends_origin_json_body(self):
        calls = []

        def post(url, headers, body="{}"):
            calls.append((url, headers, body))
            return 200, "{}"

        with patch.object(sand_api, "_post", side_effect=post):
            res = sand_api.revoke_session(self.UID, _jwt(), self.SID, "SESSION_TYPE_WEB")
        self.assertEqual(res["ok"], True)
        self.assertEqual(res["error"], "")
        self.assertEqual(res["status"], 200)
        self.assertEqual(len(calls), 1)
        url, headers, body = calls[0]
        self.assertEqual(url, sand_api.SESSIONS_REVOKE_URL)
        # 官网后台 Revoke 发的是 session_id + 数字 type，不是 sessionId。
        self.assertEqual(json.loads(body), {"session_id": self.SID, "type": 1})
        # 写操作要带会话 cookie + Origin 过 CSRF。
        self.assertEqual(headers["origin"], sand_api.ORIGIN)
        self.assertEqual(headers["referer"], sand_api.DASHBOARD_REFERER)
        self.assertIn("WorkosCursorSessionToken=" + self.UID + "%3A%3A", headers["cookie"])
        self.assertEqual(headers["content-type"], "application/json")

    def test_client_type_maps_to_2(self):
        calls = []

        def post(url, headers, body="{}"):
            calls.append(json.loads(body))
            return 200, "{}"

        with patch.object(sand_api, "_post", side_effect=post):
            sand_api.revoke_session(self.UID, _jwt(), SAMPLE["sessions"][0]["sessionId"], "client")
        self.assertEqual(calls[0]["type"], 2)
        self.assertEqual(calls[0]["session_id"], SAMPLE["sessions"][0]["sessionId"])
        self.assertNotIn("sessionId", calls[0])

    def test_looks_up_type_from_session_list_when_omitted(self):
        posts = []

        def post(url, headers, body="{}"):
            posts.append(json.loads(body))
            return 200, "{}"

        with patch.object(sand_api, "_post", side_effect=post), patch.object(
            sand_api, "fetch_sessions", return_value=sand_api.normalize_sessions(SAMPLE)
        ):
            res = sand_api.revoke_session(self.UID, _jwt(), self.SID)
        self.assertTrue(res["ok"])
        self.assertEqual(posts[0], {"session_id": self.SID, "type": 1})

    def test_http_200_html_is_not_success(self):
        html = "<!DOCTYPE html><title>Vercel Security Checkpoint</title>"
        with patch.object(sand_api, "_post", return_value=(200, html)):
            res = sand_api.revoke_session(self.UID, _jwt(), self.SID, "SESSION_TYPE_WEB")
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("waf") or "校验" in (res.get("error") or "") or "HTML" in (res.get("error") or ""))

    def test_http_401_returns_error_without_raising(self):
        with patch.object(sand_api, "_post", return_value=(401, '{"error":"no"}')):
            res = sand_api.revoke_session(self.UID, _jwt(), self.SID, "SESSION_TYPE_WEB")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 401)
        self.assertIn("401", res["error"])

    def test_vercel_checkpoint_is_not_auth_failure(self):
        html = "<!DOCTYPE html><title>Vercel Security Checkpoint</title>"
        with patch.object(sand_api, "_post", return_value=(403, html)):
            res = sand_api.revoke_session(self.UID, _jwt(), self.SID, "SESSION_TYPE_WEB")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 403)
        self.assertEqual(res["error"], sand_api.WAF_REVOKE_ERROR)
        self.assertTrue(res.get("waf"))

    def test_http_500_returns_error(self):
        with patch.object(sand_api, "_post", return_value=(500, "boom")):
            res = sand_api.revoke_session(self.UID, _jwt(), self.SID, "SESSION_TYPE_WEB")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 500)
        self.assertIn("500", res["error"])

    def test_network_zero_returns_error(self):
        with patch.object(sand_api, "_post", return_value=(0, "timeout")):
            res = sand_api.revoke_session(self.UID, _jwt(), self.SID, "SESSION_TYPE_WEB")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], 0)
        self.assertIn("无响应", res["error"])

    def test_empty_session_id_short_circuits(self):
        with patch.object(sand_api, "_post") as post:
            res = sand_api.revoke_session(self.UID, _jwt(), "   ", "SESSION_TYPE_WEB")
        self.assertFalse(res["ok"])
        post.assert_not_called()

    def test_session_id_is_stripped(self):
        with patch.object(sand_api, "_post", return_value=(200, "")) as post:
            res = sand_api.revoke_session(self.UID, _jwt(), "  " + self.SID + "\n", "SESSION_TYPE_WEB")
        self.assertTrue(res["ok"])
        body = post.call_args[0][2]
        self.assertEqual(json.loads(body), {"session_id": self.SID, "type": 1})


if __name__ == "__main__":
    unittest.main()
