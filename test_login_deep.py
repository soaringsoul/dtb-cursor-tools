"""Grok Bot 官方 PKCE：解析 loginDeepControl，代调 loginDeepCallbackControl，不 poll。"""

import base64
import json
import unittest
from unittest.mock import patch

import sand_api

SAMPLE = (
    "https://cursor.com/loginDeepControl?challenge=FUDMUY_jFaak3csMHHxhoA4DymZmERvLtWVxkohwrhY"
    "&uuid=e5e26ccb-98e7-4aaa-94d7-98eb7ed02ee0&mode=login&redirectTarget=sand"
    "&supportsSelectedTeamLogin=true"
)


def _jwt(sub="user_01DEEPLOGIN00000000000", typ="session", exp=1893456000):
    payload = {"sub": sub, "type": typ, "exp": exp, "email": "a@b.com"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + encoded + ".x"


class ParseLoginDeepUrlTest(unittest.TestCase):
    def test_extracts_uuid_challenge_and_sand_target(self):
        parsed = sand_api.parse_login_deep_url(SAMPLE)
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(parsed["uuid"], "e5e26ccb-98e7-4aaa-94d7-98eb7ed02ee0")
        self.assertEqual(parsed["challenge"], "FUDMUY_jFaak3csMHHxhoA4DymZmERvLtWVxkohwrhY")
        self.assertEqual(parsed["mode"], "login")
        self.assertEqual(parsed["redirectTarget"], "sand")
        self.assertTrue(parsed["supportsSelectedTeamLogin"])

    def test_accepts_www_and_cn_hosts(self):
        url = SAMPLE.replace("https://cursor.com/", "https://www.cursor.com/cn/")
        parsed = sand_api.parse_login_deep_url(url)
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(parsed["uuid"], "e5e26ccb-98e7-4aaa-94d7-98eb7ed02ee0")

    def test_rejects_missing_challenge(self):
        parsed = sand_api.parse_login_deep_url(
            "https://cursor.com/loginDeepControl?uuid=e5e26ccb-98e7-4aaa-94d7-98eb7ed02ee0"
        )
        self.assertFalse(parsed["ok"])

    def test_rejects_unrelated_url(self):
        parsed = sand_api.parse_login_deep_url("https://cursor.com/dashboard")
        self.assertFalse(parsed["ok"])


class ConfirmLoginDeepControlTest(unittest.TestCase):
    def _session(self, status=200, text="ok"):
        posts = []
        gets = []

        class Resp:
            def __init__(self):
                self.status_code = status
                self.text = text

        class Sess:
            def __init__(self):
                self.headers = {}
                self.trust_env = True

            def post(self, url, **kwargs):
                posts.append((url, kwargs))
                return Resp()

            def get(self, url, **kwargs):
                gets.append((url, kwargs))
                return Resp()

        return Sess, posts, gets

    def test_posts_callback_with_uuid_and_does_not_poll(self):
        token = _jwt()
        Sess, posts, gets = self._session()
        with patch.object(sand_api.requests, "Session", Sess):
            res = sand_api.confirm_login_deep_control(token, SAMPLE)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][0], sand_api.LOGIN_DEEP_URL)
        body = posts[0][1]["json"]
        self.assertEqual(body["uuid"], "e5e26ccb-98e7-4aaa-94d7-98eb7ed02ee0")
        self.assertEqual(body["challenge"], "FUDMUY_jFaak3csMHHxhoA4DymZmERvLtWVxkohwrhY")
        self.assertEqual(body["redirectTarget"], "sand")
        self.assertEqual(body["mode"], "login")
        cookie = posts[0][1]["headers"]["cookie"]
        self.assertIn("WorkosCursorSessionToken=", cookie)
        self.assertIn("user_01DEEPLOGIN00000000000", cookie)
        self.assertEqual(gets, [])

    def test_session_token_is_still_posted(self):
        token = _jwt(typ="session")
        Sess, posts, _gets = self._session()
        with patch.object(sand_api.requests, "Session", Sess):
            res = sand_api.confirm_login_deep_control(token, SAMPLE)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(posts), 1)

    def test_http_error_is_not_ok(self):
        token = _jwt()
        Sess, _posts, _gets = self._session(status=400, text="nope")
        with patch.object(sand_api.requests, "Session", Sess):
            res = sand_api.confirm_login_deep_control(token, SAMPLE)
        self.assertFalse(res["ok"])
        self.assertIn("400", res["error"])


if __name__ == "__main__":
    unittest.main()
