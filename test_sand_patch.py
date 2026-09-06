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
