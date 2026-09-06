"""报告层单元测试：规则判定 / 结论 / 日志解析。不读本机 Cursor 安装。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import patch

import patch_report
import sand_patch as sp
from test_sand_patch import VANILLA, VANILLA_319


def _layout(target: Path) -> sp.CursorLayout:
    root = target.parent
    return sp.CursorLayout(
        install_root=root,
        app_root=root,
        product_json=root / "product.json",
        executable=root / "Cursor",
        target_paths=(target,),
        ext_host_path=None,
        version="3.18.9",
    )


class RuleStatusTest(unittest.TestCase):
    def test_empty_is_missing(self):
        target = Path("workbench.desktop.main.js")
        rules = patch_report.rule_status(_layout(target), {target: "nope"})
        by_key = {r["key"]: r for r in rules}
        self.assertEqual(by_key["managed_local_route"]["status"], "missing")
        self.assertEqual(by_key["local_actions"]["status"], "missing")
        self.assertIn("3.18.9", by_key["managed_local_route"]["fix"])

    def test_vanilla_anchors_pending(self):
        target = Path("chunk.js")
        rules = patch_report.rule_status(_layout(target), {target: VANILLA})
        by_key = {r["key"]: r for r in rules}
        self.assertEqual(by_key["managed_local_route"]["status"], "pending")
        self.assertEqual(by_key["local_actions"]["status"], "pending")
        self.assertEqual(by_key["subagent_local"]["status"], "pending")
        self.assertEqual(by_key["client_type"]["status"], "pending")

    def test_vanilla_319_anchors_pending(self):
        target = Path("chunk.js")
        rules = patch_report.rule_status(_layout(target), {target: VANILLA_319})
        by_key = {r["key"]: r for r in rules}
        for key in (
            "managed_local_route",
            "local_runtime_load",
            "move_exec",
            "local_actions",
            "subagent_local",
            "agent_host_identity",
            "agent_host_enablement",
        ):
            self.assertEqual(by_key[key]["status"], "pending", key)
            self.assertNotEqual(by_key[key]["status"], "missing", key)

    def test_patched_319_applied(self):
        patched, _stats = sp.apply_patch_to_content(VANILLA_319)
        target = Path("chunk.js")
        rules = patch_report.rule_status(_layout(target), {target: patched})
        by_key = {r["key"]: r for r in rules}
        self.assertEqual(by_key["managed_local_route"]["status"], "applied")
        self.assertEqual(by_key["local_runtime_load"]["status"], "applied")
        self.assertEqual(by_key["local_actions"]["status"], "applied")
        self.assertEqual(by_key["subagent_local"]["status"], "applied")
        self.assertEqual(by_key["move_exec"]["status"], "applied")
        self.assertEqual(by_key["agent_host_identity"]["status"], "applied")

    def test_318_missing_319_literals_is_not_failure(self):
        target = Path("chunk.js")
        rules = patch_report.rule_status(_layout(target), {target: VANILLA})
        by_key = {r["key"]: r for r in rules}
        missing = [r["key"] for r in rules if r["stream"] and r["status"] == "missing"]
        self.assertEqual(missing, [])
        self.assertEqual(by_key["managed_local_route"]["status"], "pending")

    def test_patched_applied(self):
        patched, _stats = sp.apply_patch_to_content(VANILLA)
        target = Path("chunk.js")
        rules = patch_report.rule_status(_layout(target), {target: patched})
        by_key = {r["key"]: r for r in rules}
        self.assertEqual(by_key["managed_local_route"]["status"], "applied")
        self.assertEqual(by_key["local_actions"]["status"], "applied")
        self.assertEqual(by_key["subagent_local"]["status"], "applied")
        self.assertEqual(by_key["move_exec"]["status"], "applied")
        self.assertEqual(by_key["agent_host_identity"]["status"], "applied")

    def test_partial_when_marker_and_anchor_remain(self):
        target = Path("chunk.js")
        content = VANILLA + "\n" + sp.SAND_MANAGED_LOCAL_ROUTE_MARKER
        rules = patch_report.rule_status(_layout(target), {target: content})
        by_key = {r["key"]: r for r in rules}
        self.assertEqual(by_key["managed_local_route"]["status"], "partial")

    def test_membership_filename_gate(self):
        desktop = Path("workbench.desktop.main.js")
        other = Path("other.js")
        marker = sp.SAND_MEMBERSHIP_MARKER + "(function(){})();"
        layout = sp.CursorLayout(
            install_root=Path("."),
            app_root=Path("."),
            product_json=Path("product.json"),
            executable=Path("Cursor"),
            target_paths=(desktop, other),
            ext_host_path=None,
            version="3.18.9",
        )
        rules = patch_report.rule_status(layout, {desktop: marker, other: marker})
        by_key = {r["key"]: r for r in rules}
        self.assertEqual(by_key["membership"]["status"], "applied")
        self.assertEqual(by_key["membership"]["files"], ["workbench.desktop.main.js"])


class SummarizeRulesTest(unittest.TestCase):
    def test_full_partial_none(self):
        required = {"optional": False, "title": "x"}
        self.assertEqual(
            patch_report.summarize_rules([{**required, "status": "applied"}])["verdict"],
            "full",
        )
        self.assertEqual(
            patch_report.summarize_rules(
                [
                    {**required, "status": "applied", "title": "a"},
                    {**required, "status": "pending", "title": "b"},
                ]
            )["verdict"],
            "partial",
        )
        self.assertEqual(
            patch_report.summarize_rules([{**required, "status": "missing"}])["verdict"],
            "none",
        )
        optional_only = patch_report.summarize_rules(
            [{"optional": True, "status": "missing", "title": "opt"}]
        )
        self.assertEqual(optional_only["required"], 0)
        self.assertEqual(optional_only["verdict"], "full")


class RuntimeReportTest(unittest.TestCase):
    def _report(self, body: str) -> dict:
        with NamedTemporaryFile("w", encoding="utf-8", suffix=".log", delete=False) as handle:
            handle.write(body)
            path = Path(handle.name)
        try:
            with patch.object(patch_report, "_agent_host_logs", return_value=[path]):
                return patch_report.runtime_report()
        finally:
            path.unlink(missing_ok=True)

    def test_working_managed_local(self):
        payload = json.dumps(
            {
                "runtime": "managed-local",
                "reason": "sand-client",
                "actionCase": "userMessageAction",
                "modelId": "x",
            }
        )
        out = self._report(
            "Activating agent host extension\n"
            "Loaded managed local-loop runtime\n"
            "move_exec ON\n"
            f"2026-09-06 12:00:00.000 [info] Selected Agent Host turn runtime {payload}\n"
        )
        self.assertEqual(out["verdict"], "working")
        self.assertTrue(out["ok"])
        self.assertEqual(out["turns"][0]["runtime"], "managed-local")

    def test_connect_fallback_hint(self):
        payload = json.dumps(
            {
                "runtime": "connect",
                "reason": "action-not-supported",
                "actionCase": "backgroundTaskCompletionAction",
            }
        )
        out = self._report(
            "Activating agent host extension\n"
            "Loaded managed local-loop runtime\n"
            "move_exec ON\n"
            f"2026-09-06 12:00:00.000 [info] Selected Agent Host turn runtime {payload}\n"
        )
        self.assertEqual(out["verdict"], "broken")
        self.assertIn("1.2.1", out["turns"][0]["hint"])

    def test_no_log(self):
        with patch.object(patch_report, "_agent_host_logs", return_value=[]):
            out = patch_report.runtime_report()
        self.assertEqual(out["verdict"], "no-log")
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
