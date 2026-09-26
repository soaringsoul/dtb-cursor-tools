"""账号自定义分类：标签落在账号上，换 token 不丢，只接受目录里的 id。不联网。"""

import os
import tempfile
import unittest

from app import accounts
from tests.test_refresh_token import ACCESS, _jwt


class AccountTagStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = accounts._store_path
        accounts._store_path = lambda: os.path.join(self.tmp.name, "accounts.json")

    def tearDown(self):
        accounts._store_path = self._orig
        self.tmp.cleanup()

    def test_reimport_keeps_tag_ids(self):
        store = accounts.AccountStore()
        store.add_text(ACCESS)
        uid = store.list()[0]["id"]
        self.assertTrue(store.set_tag_ids(uid, ["t1", "t2", "t1", ""]))
        store.add_text(_jwt(exp=1999999999))
        self.assertEqual(store.get(uid)["tagIds"], ["t1", "t2"])
        self.assertEqual(store.list()[0]["tagIds"], ["t1", "t2"])
        reloaded = accounts.AccountStore()
        self.assertEqual(reloaded.get(uid)["tagIds"], ["t1", "t2"])

    def test_filter_tag_ids_keeps_catalog_only(self):
        catalog = [{"id": "t1", "name": "工作"}, {"id": "t2", "name": "备用"}]
        self.assertEqual(accounts.filter_tag_ids(["t2", "gone", "t2", ""], catalog), ["t2"])
        self.assertEqual(accounts.filter_tag_ids(None, catalog), [])
