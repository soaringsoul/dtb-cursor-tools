"""补丁规则单元测试：合成夹具（不是 Cursor 发行包）上的打补丁 / 卸载字节往返。"""

from __future__ import annotations

import unittest

import sand_patch as sp

# 仅含锚点字面量的合成片段，避免把官方 dist 整包提交进仓库。
VANILLA = "\n".join(
    [
        'g.header.set("x-cursor-client-type","ide")',
        '{"x-cursor-client-type":"ide"}',
        'isGlass?"glass":"ide"',
        "function r4g(e){const{adminSettingsService:t}={};return t}",
        "foo({hasResolvedTeamMembership:a,teamId:b}){return x===M.FREE&&y&&z===void 0}",
        '_membershipType=()=>this.storageService.get("k")',
        "hasValidPaymentMethod=async()=>{return await q()}",
        'try{return(yield a.checkFeatureGate(b))?{runtime:"managed-local",reason:"eligible"}:{runtime:"connect",reason:"gate-off"}}catch(err){}',
        "let t=!1;try{t=await n.cursor.checkFeatureGate(g)}catch(x){void agent_host_local_loop}if(!t){load()}",
        'clientIdentity:{clientType:"ide"}',
        "createAgentHost),w=await Promise.resolve(c.cursor.checkFeatureGate(g)).catch(()=>!1)",
        'return"userMessageAction"!==e.actionCase?"action-not-supported":e.requestedMode!=="agent"',
        "x||void 0!==r.runOptions.subagentTypeName||void 0!==r.runOptions.parentAgentToolCallId||!0===r.runOptions.directMetaParentChildSubagent||y",
        "this._agentHostEnabled=foo,this.other=1",
    ]
)

# 3.19.13 形态：参考树字面量 + 与 3.18 相同的 identity / enablement。
VANILLA_319 = "\n".join(
    [
        'g.header.set("x-cursor-client-type","ide")',
        sp.MANAGED_LOCAL_ROUTE_319_ORIGINAL,
        sp.LOCAL_RUNTIME_LOAD_319_ORIGINAL,
        'clientIdentity:{clientType:"ide"}',
        sp.MOVE_EXEC_319_ORIGINAL,
        sp.LOCAL_ACTIONS_319_ORIGINAL,
        sp.SUBAGENT_319_ORIGINAL,
        "this._agentHostEnabled=foo,this.other=1",
    ]
)


class ApplyRemoveRoundtripTest(unittest.TestCase):
    def test_vanilla_roundtrip_bytes(self):
        patched, stats = sp.apply_patch_to_content(VANILLA)
        self.assertGreater(stats.total, 0)
        self.assertIn(sp.SAND_LOCAL_ACTIONS_MARKER, patched)
        self.assertIn(sp.SAND_SUBAGENT_LOCAL_MARKER, patched)
        self.assertIn(sp.SAND_MANAGED_LOCAL_ROUTE_MARKER, patched)
        restored, removed = sp.remove_patch_from_content(patched)
        self.assertGreater(removed.total, 0)
        self.assertEqual(restored, VANILLA)
        self.assertNotIn("SAND_", restored)

    def test_apply_hits_stream_anchors(self):
        patched, stats = sp.apply_patch_to_content(VANILLA)
        self.assertGreater(stats.managed_local_route, 0)
        self.assertGreater(stats.local_runtime_load, 0)
        self.assertGreater(stats.agent_host_identity, 0)
        self.assertGreater(stats.move_exec, 0)
        self.assertGreater(stats.local_actions, 0)
        self.assertGreater(stats.subagent_local, 0)
        self.assertGreater(stats.agent_host_enablement, 0)
        self.assertIn(sp.AGENT_HOST_IDENTITY_PATCHED, patched)

    def test_second_apply_does_not_reinsert_local_actions(self):
        once, _stats = sp.apply_patch_to_content(VANILLA)
        twice, stats = sp.apply_patch_to_content(once)
        self.assertEqual(once.count(sp.SAND_LOCAL_ACTIONS_MARKER), 1)
        self.assertEqual(twice.count(sp.SAND_LOCAL_ACTIONS_MARKER), 1)
        self.assertEqual(stats.local_actions, 0)

    def test_318_does_not_take_319_track(self):
        patched, _stats = sp.apply_patch_to_content(VANILLA)
        self.assertNotIn("/*Ms*/", patched)
        self.assertNotIn(sp.MANAGED_LOCAL_ROUTE_319_ORIGINAL, patched)
        self.assertNotIn(sp.LOCAL_RUNTIME_LOAD_319_PATCHED, patched)
        self.assertNotIn(sp.MOVE_EXEC_319_PATCHED, patched)


class ApplyRemove319Test(unittest.TestCase):
    def test_vanilla_319_roundtrip_bytes(self):
        patched, stats = sp.apply_patch_to_content(VANILLA_319)
        self.assertGreater(stats.total, 0)
        restored, removed = sp.remove_patch_from_content(patched)
        self.assertGreater(removed.total, 0)
        self.assertEqual(restored, VANILLA_319)
        self.assertNotIn("SAND_", restored)

    def test_apply_hits_seven_stream_stats(self):
        patched, stats = sp.apply_patch_to_content(VANILLA_319)
        self.assertGreater(stats.managed_local_route, 0)
        self.assertGreater(stats.local_runtime_load, 0)
        self.assertGreater(stats.agent_host_identity, 0)
        self.assertGreater(stats.move_exec, 0)
        self.assertGreater(stats.local_actions, 0)
        self.assertGreater(stats.subagent_local, 0)
        self.assertGreater(stats.agent_host_enablement, 0)
        self.assertIn(sp.SAND_MANAGED_LOCAL_ROUTE_MARKER, patched)
        self.assertIn(sp.SAND_LOCAL_RUNTIME_LOAD_MARKER, patched)
        self.assertIn(sp.SAND_MOVE_EXEC_MARKER, patched)
        self.assertIn(sp.SAND_LOCAL_ACTIONS_MARKER, patched)
        self.assertIn(sp.SAND_SUBAGENT_LOCAL_MARKER, patched)
        self.assertNotIn("try{return{runtime:\"managed-local\"", patched)

    def test_second_apply_319_idempotent(self):
        once, _stats = sp.apply_patch_to_content(VANILLA_319)
        twice, stats = sp.apply_patch_to_content(once)
        self.assertEqual(once, twice)
        self.assertEqual(stats.managed_local_route, 0)
        self.assertEqual(stats.local_runtime_load, 0)
        self.assertEqual(stats.move_exec, 0)
        self.assertEqual(stats.local_actions, 0)
        self.assertEqual(stats.subagent_local, 0)

    def test_uninstall_cam_319_literals(self):
        cam = VANILLA_319.replace(
            sp.MANAGED_LOCAL_ROUTE_319_ORIGINAL,
            sp.CAM_MANAGED_LOCAL_ROUTE_319_PATCHED,
        ).replace(
            sp.LOCAL_RUNTIME_LOAD_319_ORIGINAL,
            sp.LOCAL_RUNTIME_LOAD_319_PATCHED,
        ).replace(
            sp.MOVE_EXEC_319_ORIGINAL,
            sp.CAM_MOVE_EXEC_319_PATCHED,
        ).replace(
            sp.LOCAL_ACTIONS_319_ORIGINAL,
            sp.CAM_LOCAL_ACTIONS_319_PATCHED,
        ).replace(
            sp.SUBAGENT_319_ORIGINAL,
            sp.CAM_SUBAGENT_319_PATCHED,
        )
        restored, stats = sp.remove_patch_from_content(cam)
        self.assertGreater(stats.total, 0)
        self.assertEqual(restored, VANILLA_319)
        self.assertNotIn("SAND_", restored)

    def test_migrate_cam_319_then_roundtrip(self):
        cam = VANILLA_319.replace(
            sp.MOVE_EXEC_319_ORIGINAL,
            sp.CAM_MOVE_EXEC_319_PATCHED,
        ).replace(
            sp.LOCAL_ACTIONS_319_ORIGINAL,
            sp.CAM_LOCAL_ACTIONS_319_PATCHED,
        ).replace(
            sp.SUBAGENT_319_ORIGINAL,
            sp.CAM_SUBAGENT_319_PATCHED,
        )
        patched, stats = sp.apply_patch_to_content(cam)
        self.assertGreater(stats.move_exec, 0)
        self.assertGreater(stats.local_actions, 0)
        self.assertGreater(stats.subagent_local, 0)
        self.assertIn(sp.SAND_MOVE_EXEC_MARKER, patched)
        self.assertNotIn(sp.CAM_MOVE_EXEC_MARKER, patched)
        restored, _removed = sp.remove_patch_from_content(patched)
        self.assertEqual(restored, VANILLA_319)


class CursorVersionTest(unittest.TestCase):
    def test_tested_versions(self):
        self.assertTrue(sp.is_tested_cursor_version("3.18.9"))
        self.assertTrue(sp.is_tested_cursor_version("3.18.25"))
        self.assertTrue(sp.is_tested_cursor_version("3.19.13"))
        self.assertTrue(sp.is_tested_cursor_version("  3.19.13\n"))
        self.assertFalse(sp.is_tested_cursor_version("3.19.7"))
        self.assertFalse(sp.is_tested_cursor_version("3.20.0"))

    def test_download_urls_newest_default(self):
        urls = sp.cursor_download_urls("9.9.9")
        self.assertEqual(urls["version"], "3.19.13")
        self.assertIn("dd066f332fcea7382764400fde902f61920648d5", urls["windows"])
        self.assertIn("CursorUserSetup-x64-3.19.13.exe", urls["windows"])
        official_318 = sp.cursor_download_urls("3.18.9")
        self.assertEqual(official_318["version"], "3.18.9")
        self.assertIn("2ba48ff3f7514cc4643c52ca9f7b3173d9b66137", official_318["windows"])

    def test_content_has_319_anchors(self):
        self.assertTrue(sp._content_has_stream_anchors(VANILLA_319))
        self.assertTrue(sp._content_has_stream_anchors(VANILLA))
        self.assertFalse(sp._content_has_stream_anchors("console.log('hello')"))


class AnchorDetectTest(unittest.TestCase):
    def test_managed_local_regex(self):
        self.assertIsNotNone(sp.MANAGED_LOCAL_ROUTE_RE.search(VANILLA))

    def test_no_anchor_is_noop(self):
        raw = "console.log('hello')"
        patched, stats = sp.apply_patch_to_content(raw)
        self.assertEqual(patched, raw)
        self.assertEqual(stats.total, 0)
        restored, removed = sp.remove_patch_from_content(patched)
        self.assertEqual(restored, raw)
        self.assertEqual(removed.total, 0)


if __name__ == "__main__":
    unittest.main()
