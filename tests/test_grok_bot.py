"""Grok Bot 独立客户端：写自己的 sand-secrets，不碰 Cursor。"""

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

AID = "user_01GROKBOT00000000000000"


def _jwt(sub=AID, typ="session", exp=1893456000, email="a@b.com"):
    payload = {"sub": sub, "type": typ, "exp": exp, "email": email}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + encoded + ".x"


ACCESS = _jwt(sub="auth0|" + AID)
REFRESH = _jwt(sub="auth0|" + AID, typ="refresh")


class AccountScopeTest(unittest.TestCase):
    def test_hashes_jwt_sub_as_sha256_hex(self):
        import grok_bot

        sub = "auth0|" + AID
        self.assertEqual(
            grok_bot.account_scope(ACCESS),
            hashlib.sha256(sub.encode("utf-8")).hexdigest(),
        )

    def test_falls_back_to_raw_token_when_sub_missing(self):
        import grok_bot

        raw = "not-a-jwt"
        self.assertEqual(
            grok_bot.account_scope(raw),
            hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )


class PlaintextWrapTest(unittest.TestCase):
    def test_prefix_and_roundtrip(self):
        import grok_bot

        wrapped = grok_bot.wrap_plaintext("hello-token")
        self.assertTrue(wrapped.startswith("plaintext:v1:"))
        rest = wrapped[len("plaintext:v1:") :]
        self.assertEqual(base64.b64decode(rest).decode("utf-8"), "hello-token")
        self.assertEqual(grok_bot.unwrap_plaintext(wrapped), "hello-token")


class UserDataDirTest(unittest.TestCase):
    def test_darwin_is_application_support_grok_bot(self):
        import grok_bot

        with patch.object(grok_bot.sys, "platform", "darwin"):
            path = grok_bot.user_data_dir()
        self.assertEqual(path.name, "Grok Bot")
        self.assertIn("Application Support", str(path))
        self.assertNotIn("/Cursor/", str(path).replace("\\", "/") + "/")

    def test_win32_is_appdata_grok_bot(self):
        import grok_bot

        with (
            patch.object(grok_bot.sys, "platform", "win32"),
            patch.dict(grok_bot.os.environ, {"APPDATA": r"C:\Users\me\AppData\Roaming"}, clear=False),
        ):
            path = grok_bot.user_data_dir()
        self.assertEqual(path.name, "Grok Bot")
        self.assertIn("AppData", str(path))


class OscryptTest(unittest.TestCase):
    def test_v10_roundtrip_matches_electron_safe_storage(self):
        import grok_bot

        blob = grok_bot.oscrypt_encrypt("hello-token", "unit-test-key")
        raw = base64.b64decode(blob)
        self.assertTrue(raw.startswith(b"v10"), raw[:8])
        self.assertFalse(blob.startswith("plaintext:v1:"))
        self.assertEqual(grok_bot.oscrypt_decrypt(blob, "unit-test-key"), "hello-token")


class WriteSecretsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _write(self, **kwargs):
        import grok_bot

        with (
            patch.object(grok_bot, "user_data_dir", return_value=self.root),
            patch.object(
                grok_bot, "_safe_storage_password", return_value="unit-test-key"
            ),
        ):
            return grok_bot.write_local_account(
                kwargs.get("access", ACCESS),
                kwargs.get("refresh", REFRESH),
                email=kwargs.get("email", "a@b.com"),
            )

    def _accounts(self):
        data = json.loads((self.root / "sand-secrets.json").read_text(encoding="utf-8"))
        return json.loads(data["cursor-accounts"])

    def test_write_sets_active_encrypted_tokens_not_plaintext(self):
        import grok_bot

        self._write()
        rec = self._accounts()
        scope = grok_bot.account_scope(ACCESS)
        self.assertEqual(rec["active"], scope)
        slot = rec["accounts"][scope]
        for key in ("cursor-access-token", "cursor-refresh-token", "cursor-account-profile"):
            stored = slot[key]
            self.assertFalse(stored.startswith("plaintext:v1:"), stored[:20])
            self.assertTrue(base64.b64decode(stored).startswith(b"v10"))
        self.assertEqual(
            grok_bot.oscrypt_decrypt(slot["cursor-access-token"], "unit-test-key"),
            ACCESS,
        )
        self.assertEqual(
            grok_bot.oscrypt_decrypt(slot["cursor-refresh-token"], "unit-test-key"),
            REFRESH,
        )
        profile = json.loads(
            grok_bot.oscrypt_decrypt(slot["cursor-account-profile"], "unit-test-key")
        )
        self.assertEqual(profile.get("email"), "a@b.com")

    def test_write_keeps_other_accounts_and_machine_id(self):
        import grok_bot

        old_scope = "a" * 64
        seed = {
            "cursor-machine-id": "keep-me",
            "local-exec-file-key": "keep-key",
            "cursor-accounts": json.dumps(
                {
                    "active": old_scope,
                    "accounts": {
                        old_scope: {
                            "cursor-access-token": grok_bot.wrap_plaintext("old"),
                            "cursor-refresh-token": grok_bot.wrap_plaintext("old-rt"),
                        }
                    },
                }
            ),
        }
        (self.root / "sand-secrets.json").write_text(json.dumps(seed), encoding="utf-8")
        self._write()
        data = json.loads((self.root / "sand-secrets.json").read_text(encoding="utf-8"))
        self.assertEqual(data["cursor-machine-id"], "keep-me")
        rec = json.loads(data["cursor-accounts"])
        self.assertIn(old_scope, rec["accounts"])
        self.assertEqual(rec["active"], grok_bot.account_scope(ACCESS))

    def test_write_account_slot_blob(self):
        import grok_bot

        pers = self.root / "sand-client-persistence"
        pers.mkdir()
        self._write()
        blob = pers / (grok_bot.account_slot_blob_name() + ".blob")
        self.assertTrue(blob.is_file())
        payload = json.loads(blob.read_text(encoding="utf-8"))
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["value"], "auth0|" + AID)

    def test_does_not_touch_cursor_vscdb(self):
        import grok_bot

        cursor_root = self.root / "Cursor" / "User" / "globalStorage"
        cursor_root.mkdir(parents=True)
        vscdb = cursor_root / "state.vscdb"
        vscdb.write_text("cursor-db", encoding="utf-8")
        with (
            patch.object(grok_bot, "user_data_dir", return_value=self.root / "Grok Bot"),
            patch.object(grok_bot, "_safe_storage_password", return_value="unit-test-key"),
            patch("local_cursor.write_local_account") as cursor_write,
        ):
            (self.root / "Grok Bot").mkdir()
            grok_bot.write_local_account(ACCESS, REFRESH, email="a@b.com")
        cursor_write.assert_not_called()
        self.assertEqual(vscdb.read_text(encoding="utf-8"), "cursor-db")

    def test_encrypt_unavailable_uses_toplevel_plaintext_and_drops_nested_slot(self):
        import grok_bot

        scope = grok_bot.account_scope(ACCESS)
        seed = {
            "cursor-machine-id": "keep-me",
            "cursor-accounts": json.dumps(
                {
                    "active": scope,
                    "accounts": {
                        scope: {
                            "cursor-access-token": grok_bot.wrap_plaintext("stale"),
                            "cursor-refresh-token": grok_bot.wrap_plaintext("stale-rt"),
                        }
                    },
                }
            ),
        }
        (self.root / "sand-secrets.json").write_text(json.dumps(seed), encoding="utf-8")
        with (
            patch.object(grok_bot, "user_data_dir", return_value=self.root),
            patch.object(grok_bot, "_safe_storage_password", return_value=None),
        ):
            grok_bot.write_local_account(ACCESS, REFRESH, email="a@b.com")
        data = json.loads((self.root / "sand-secrets.json").read_text(encoding="utf-8"))
        self.assertEqual(data["cursor-machine-id"], "keep-me")
        self.assertEqual(grok_bot.unwrap_plaintext(data["cursor-access-token"]), ACCESS)
        self.assertEqual(grok_bot.unwrap_plaintext(data["cursor-refresh-token"]), REFRESH)
        rec = json.loads(data["cursor-accounts"])
        self.assertNotIn(scope, rec["accounts"])

    def test_wrong_key_falls_back_and_keeps_existing_accounts(self):
        import grok_bot

        old_scope = "b" * 64
        good = grok_bot.oscrypt_encrypt("old-token", "real-key")
        seed = {
            "cursor-accounts": json.dumps(
                {
                    "active": old_scope,
                    "accounts": {
                        old_scope: {
                            "cursor-access-token": good,
                            "cursor-refresh-token": good,
                        }
                    },
                }
            ),
        }
        (self.root / "sand-secrets.json").write_text(json.dumps(seed), encoding="utf-8")
        with (
            patch.object(grok_bot, "user_data_dir", return_value=self.root),
            patch.object(grok_bot, "_safe_storage_password", return_value="unit-test-key"),
        ):
            grok_bot.write_local_account(ACCESS, REFRESH, email="a@b.com")
        data = json.loads((self.root / "sand-secrets.json").read_text(encoding="utf-8"))
        rec = json.loads(data["cursor-accounts"])
        self.assertIn(old_scope, rec["accounts"])
        self.assertEqual(rec["accounts"][old_scope]["cursor-access-token"], good)
        scope = grok_bot.account_scope(ACCESS)
        self.assertNotIn(scope, rec["accounts"])
        self.assertEqual(grok_bot.unwrap_plaintext(data["cursor-access-token"]), ACCESS)


class FindAndStartTest(unittest.TestCase):
    def test_find_app_darwin_applications(self):
        import grok_bot

        fake = Path("/Applications/Grok Bot.app")
        with (
            patch.object(grok_bot.sys, "platform", "darwin"),
            patch.object(Path, "is_dir", lambda self: str(self) == str(fake)),
        ):
            found = grok_bot.find_app()
        self.assertEqual(found, fake)

    def test_darwin_start_opens_grok_bot_not_cursor(self):
        import grok_bot

        fake = Path("/Applications/Grok Bot.app")
        with (
            patch.object(grok_bot.sys, "platform", "darwin"),
            patch.object(grok_bot, "find_app", return_value=fake),
            patch.object(grok_bot.shutil, "which", return_value="/usr/bin/open"),
            patch.object(grok_bot.subprocess, "run") as run,
        ):
            self.assertTrue(grok_bot.start_grok_bot())
        cmd = list(run.call_args[0][0])
        self.assertEqual(cmd[:3], ["/usr/bin/open", "-a", str(fake)])
        self.assertNotIn("Cursor.app", " ".join(cmd))
        self.assertNotIn("--classic", cmd)

    def test_windows_registry_text_yields_exe_path(self):
        import grok_bot

        text = (
            "    InstallLocation    REG_SZ    C:\\Users\\me\\AppData\\Local\\Programs\\Grok Bot\n"
            "    DisplayIcon    REG_SZ    C:\\Users\\me\\AppData\\Local\\Programs\\Grok Bot\\Grok Bot.exe,0\n"
        )
        paths = grok_bot.exe_paths_from_registry_text(text)
        self.assertIn(
            r"C:\Users\me\AppData\Local\Programs\Grok Bot\Grok Bot.exe",
            paths,
        )

    def test_find_app_windows_uses_install_search(self):
        import grok_bot

        exe = Path(r"D:\Apps\Grok Bot\Grok Bot.exe")
        with (
            patch.object(grok_bot.sys, "platform", "win32"),
            patch.object(grok_bot, "_search_windows_exe", return_value=exe),
        ):
            self.assertEqual(grok_bot.find_app(), exe)

    def test_missing_message_on_windows_names_exe(self):
        import grok_bot

        with patch.object(grok_bot.sys, "platform", "win32"):
            text = grok_bot.missing_app_message()
        self.assertIn("Grok Bot.exe", text)
        self.assertNotIn("/Applications", text)

    def test_start_without_app_raises(self):
        import grok_bot

        with patch.object(grok_bot, "find_app", return_value=None):
            with self.assertRaises(grok_bot.GrokBotError):
                grok_bot.start_grok_bot()


class DetectLoginUrlTest(unittest.TestCase):
    def test_parses_osascript_stdout(self):
        import grok_bot

        url = (
            "https://cursor.com/loginDeepControl?challenge=abc&uuid=def"
            "&mode=login&redirectTarget=sand"
        )
        with (
            patch.object(grok_bot.sys, "platform", "darwin"),
            patch.object(
                grok_bot.subprocess,
                "run",
                return_value=MagicMock(returncode=0, stdout=url + "\n", stderr=""),
            ),
        ):
            self.assertEqual(grok_bot.detect_login_deep_url(), url)

    def test_ignores_unrelated_browser_url(self):
        import grok_bot

        with (
            patch.object(grok_bot.sys, "platform", "darwin"),
            patch.object(
                grok_bot.subprocess,
                "run",
                return_value=MagicMock(
                    returncode=0, stdout="https://cursor.com/dashboard\n", stderr=""
                ),
            ),
        ):
            self.assertIsNone(grok_bot.detect_login_deep_url())


class CloseGrokBotTest(unittest.TestCase):
    def test_darwin_quit_uses_bundle_id_not_cursor(self):
        import grok_bot

        with (
            patch.object(grok_bot.sys, "platform", "darwin"),
            patch.object(grok_bot.subprocess, "run") as run,
            patch.object(grok_bot, "_wait_for_exit", return_value=True),
            patch.object(grok_bot, "_running_pids", return_value=[4242, 4242]),
        ):
            grok_bot.close_grok_bot()
        cmds = [" ".join(str(x) for x in c.args[0]) for c in run.call_args_list if c.args]
        blob = "\n".join(cmds)
        self.assertIn("com.anysphere.sand", blob)
        self.assertNotIn("Cursor", blob)
        self.assertNotIn("com.todesktop", blob)


if __name__ == "__main__":
    unittest.main()
