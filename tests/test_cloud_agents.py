"""云端 Agent API 客户端与 API Key 存储：不联网。"""

import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import api_key_store
import cloud_agents


class ListAgentsTest(unittest.TestCase):
    def test_follows_next_cursor(self):
        pages = {
            None: {"items": [{"id": "bc-1", "status": "ACTIVE"}], "nextCursor": "c2"},
            "c2": {"items": [{"id": "bc-2", "status": "ARCHIVED"}], "nextCursor": None},
        }

        def fake(_key, _method, _path, query=None):
            return pages[(query or {}).get("cursor")]

        with patch.object(cloud_agents, "api_request", side_effect=fake):
            items = cloud_agents.list_all_agents("crsr_x")
        self.assertEqual([a["id"] for a in items], ["bc-1", "bc-2"])

    def test_include_archived_query_flag(self):
        seen = []

        def fake(_key, _method, _path, query=None):
            seen.append(dict(query or {}))
            return {"items": [], "nextCursor": None}

        with patch.object(cloud_agents, "api_request", side_effect=fake):
            cloud_agents.list_all_agents("crsr_x", include_archived=True)
            cloud_agents.list_all_agents("crsr_x", include_archived=False)
        self.assertEqual(seen[0].get("includeArchived"), "true")
        self.assertEqual(seen[1].get("includeArchived"), "false")


class WhoamiTest(unittest.TestCase):
    def test_returns_account_fields(self):
        payload = {"apiKeyName": "local_api", "userEmail": "you@example.com", "userId": 123}

        with patch.object(cloud_agents, "api_request", return_value=payload):
            info = cloud_agents.whoami("crsr_x")
        self.assertEqual(info["apiKeyName"], "local_api")
        self.assertEqual(info["userEmail"], "you@example.com")
        self.assertEqual(info["userId"], 123)


class DeleteAgentsTest(unittest.TestCase):
    def test_continues_after_one_failure(self):
        deleted = []

        def fake(_key, method, path, query=None):
            self.assertEqual(method, "DELETE")
            if path.endswith("bc-bad"):
                raise cloud_agents.ApiError(500, "boom")
            deleted.append(path)
            return {}

        agents = [{"id": "bc-ok"}, {"id": "bc-bad"}, {"id": "bc-ok2"}]
        with patch.object(cloud_agents, "api_request", side_effect=fake):
            result = cloud_agents.delete_agents("crsr_x", agents)
        self.assertEqual(result["ok"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("bc-bad", result["errors"][0])
        self.assertEqual(len(deleted), 2)


class ParseApiKeyTextTest(unittest.TestCase):
    def test_skips_blank_accepts_crsr(self):
        keys, failed = api_key_store.parse_api_key_text("\n  crsr_abc123xyz  \n\n")
        self.assertEqual(keys, ["crsr_abc123xyz"])
        self.assertEqual(failed, [])

    def test_accepts_bare_key_line(self):
        keys, failed = api_key_store.parse_api_key_text("key_abcdefghijklmnopqrstuv")
        self.assertEqual(keys, ["key_abcdefghijklmnopqrstuv"])
        self.assertEqual(failed, [])

    def test_rejects_jwt_and_keeps_line(self):
        jwt = "eyJhbGciOiJub25lIn0.e30.signature"
        keys, failed = api_key_store.parse_api_key_text(jwt)
        self.assertEqual(keys, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["line"], jwt)
        self.assertTrue(failed[0]["error"])


class ApiKeyStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "api_keys.json")
        self._orig = api_key_store._store_path
        api_key_store._store_path = lambda: self.path

    def tearDown(self):
        api_key_store._store_path = self._orig
        self.tmp.cleanup()

    def test_list_omits_raw_key(self):
        store = api_key_store.ApiKeyStore()
        rec = store.add("crsr_secret_value", {"apiKeyName": "n", "userEmail": "e@x.com", "userId": 9})
        rows = store.list()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("key", rows[0])
        self.assertEqual(rows[0]["userEmail"], "e@x.com")
        self.assertEqual(rows[0]["apiKeyName"], "n")
        self.assertEqual(rows[0]["id"], rec["id"])
        self.assertEqual(store.get(rec["id"])["key"], "crsr_secret_value")

    def test_dedup_same_key(self):
        store = api_key_store.ApiKeyStore()
        a = store.add("crsr_secret_value", {"userEmail": "a@x.com"})
        b = store.add("crsr_secret_value", {"userEmail": "b@x.com"})
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(store.list()), 1)
        self.assertEqual(store.get(a["id"])["userEmail"], "b@x.com")

    def test_id_is_sha256_prefix(self):
        digest = hashlib.sha256(b"crsr_secret_value").hexdigest()[:16]
        store = api_key_store.ApiKeyStore()
        rec = store.add("crsr_secret_value", {})
        self.assertEqual(rec["id"], digest)

    def test_import_whoami_401_records_failure(self):
        def boom(_key):
            raise cloud_agents.ApiError(401, "unauthorized")

        store = api_key_store.ApiKeyStore()
        result = store.import_keys("crsr_bad_key_xxxxx", whoami_fn=boom)
        self.assertEqual(result["added"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("401", result["failed"][0]["error"])
        self.assertEqual(store.list(), [])

    def test_last_input_roundtrip(self):
        store = api_key_store.ApiKeyStore()
        store.set_last_input("crsr_keep_me\ncrsr_second")
        again = api_key_store.ApiKeyStore()
        self.assertEqual(again.last_input(), "crsr_keep_me\ncrsr_second")
        self.assertEqual(again.list(), [])

    def test_import_remembers_raw_text(self):
        store = api_key_store.ApiKeyStore()
        store.import_keys("  crsr_keep_me  \n", whoami_fn=lambda _k: {"apiKeyName": "n"})
        self.assertEqual(store.last_input(), "  crsr_keep_me  \n")
        again = api_key_store.ApiKeyStore()
        self.assertEqual(again.last_input(), "  crsr_keep_me  \n")

    def test_legacy_plain_items_still_load(self):
        rec = {
            "id": api_key_store.key_id_for("crsr_old_key"),
            "key": "crsr_old_key",
            "apiKeyName": "n",
            "userEmail": "e@x.com",
            "userId": 1,
            "addedAt": 1,
        }
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"v": 1, "enc": "none", "items": [rec]}, handle)
        store = api_key_store.ApiKeyStore()
        self.assertEqual(len(store.list()), 1)
        self.assertEqual(store.last_input(), "")
        self.assertEqual(store.draft_text(), "crsr_old_key")
        self.assertEqual(store.get(rec["id"])["key"], "crsr_old_key")

    def test_draft_falls_back_to_latest_key(self):
        store = api_key_store.ApiKeyStore()
        store.add("crsr_old_key", {"apiKeyName": "a"})
        store.add("crsr_new_key", {"apiKeyName": "b"})
        self.assertEqual(store.last_input(), "")
        self.assertEqual(store.draft_text(), "crsr_new_key")
        store.set_last_input("crsr_typed")
        self.assertEqual(store.draft_text(), "crsr_typed")

    def test_list_agents_missing_key(self):
        store = api_key_store.ApiKeyStore()
        res = store.list_agents("missing")
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"])
        self.assertEqual(res["agents"], [])

    def test_list_and_delete_all_agents(self):
        store = api_key_store.ApiKeyStore()
        rec = store.add("crsr_bridge_secret", {"userEmail": "a@b.com"})
        agents = [
            {"id": "bc-1", "status": "ACTIVE", "name": "one", "createdAt": "2026-08-14T16:53:57Z"},
            {"id": "bc-2", "status": "ARCHIVED", "name": "two", "createdAt": "2026-08-15T07:32:54Z"},
        ]
        with patch.object(cloud_agents, "list_all_agents", return_value=agents):
            res = store.list_agents(rec["id"])
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["agents"]), 2)
        self.assertNotIn("key", res)
        with patch.object(cloud_agents, "list_all_agents", return_value=agents), patch.object(
            cloud_agents,
            "delete_agents",
            return_value={"ok": 2, "failed": 0, "errors": []},
        ) as deleted:
            out = store.delete_all_agents(rec["id"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["deleted"], 2)
        self.assertEqual(out["failed"], 0)
        deleted.assert_called_once()
        args, _kwargs = deleted.call_args
        self.assertEqual(args[0], "crsr_bridge_secret")
        self.assertEqual(len(args[1]), 2)

    def test_delete_one_agent(self):
        store = api_key_store.ApiKeyStore()
        rec = store.add("crsr_bridge_secret", {"userEmail": "a@b.com"})
        with patch.object(
            cloud_agents,
            "delete_agents",
            return_value={"ok": 1, "failed": 0, "errors": []},
        ) as deleted:
            out = store.delete_agent(rec["id"], "bc-1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["deleted"], 1)
        self.assertEqual(out["failed"], 0)
        args, _kwargs = deleted.call_args
        self.assertEqual(args[0], "crsr_bridge_secret")
        self.assertEqual(args[1], [{"id": "bc-1"}])

    def test_delete_one_agent_missing_key(self):
        store = api_key_store.ApiKeyStore()
        out = store.delete_agent("missing", "bc-1")
        self.assertFalse(out["ok"])
        self.assertEqual(out["deleted"], 0)

    def test_delete_one_agent_blank_id(self):
        store = api_key_store.ApiKeyStore()
        rec = store.add("crsr_bridge_secret", {})
        with patch.object(cloud_agents, "delete_agents") as deleted:
            out = store.delete_agent(rec["id"], "  ")
        self.assertFalse(out["ok"])
        deleted.assert_not_called()


class PreviewRpcTest(unittest.TestCase):
    def setUp(self):
        import preview_server

        self.ps = preview_server
        with preview_server._LOCK:
            preview_server._API_KEYS.clear()
            preview_server._AGENTS_BY_KEY.clear()
            preview_server._API_KEY_SECRETS.clear()
            preview_server._LAST_API_KEY_INPUT = ""

    def test_import_list_clean_without_leaking_key(self):
        res = self.ps._rpc("import_api_keys", ["crsr_preview_secret_xxx"])
        self.assertEqual(len(res["added"]), 1)
        self.assertNotIn("key", res["added"][0])
        kid = res["added"][0]["id"]
        keys = self.ps._rpc("list_api_keys", [])
        self.assertEqual(len(keys), 1)
        self.assertNotIn("key", keys[0])
        listed = self.ps._rpc("list_cloud_agents", [kid])
        self.assertTrue(listed["ok"])
        self.assertEqual(len(listed["agents"]), 2)
        deleted = self.ps._rpc("delete_all_cloud_agents", [kid])
        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["deleted"], 2)
        after = self.ps._rpc("list_cloud_agents", [kid])
        self.assertEqual(after["agents"], [])

    def test_delete_one_cloud_agent(self):
        res = self.ps._rpc("import_api_keys", ["crsr_preview_secret_xxx"])
        kid = res["added"][0]["id"]
        listed = self.ps._rpc("list_cloud_agents", [kid])
        aid = listed["agents"][0]["id"]
        deleted = self.ps._rpc("delete_cloud_agent", [kid, aid])
        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["deleted"], 1)
        after = self.ps._rpc("list_cloud_agents", [kid])
        self.assertEqual(len(after["agents"]), 1)
        self.assertNotEqual(after["agents"][0]["id"], aid)
        missing = self.ps._rpc("delete_cloud_agent", [kid, "bc-nope"])
        self.assertFalse(missing["ok"])

    def test_remembers_last_api_key_input(self):
        self.ps._rpc("import_api_keys", ["crsr_preview_secret_xxx"])
        self.assertEqual(self.ps._rpc("get_last_api_key_input", []), "crsr_preview_secret_xxx")
        self.ps._rpc("set_last_api_key_input", ["crsr_typed_again"])
        self.assertEqual(self.ps._rpc("get_last_api_key_input", []), "crsr_typed_again")
        keys = self.ps._rpc("list_api_keys", [])
        self.assertNotIn("key", keys[0])


if __name__ == "__main__":
    unittest.main()
