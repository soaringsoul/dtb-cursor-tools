"""cam_patch：用插件 inspect JSON 生成规则行；CLI 走 node sandCli.js。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cam_patch


def _empty_stream():
    return {
        "client": 0,
        "eligibility": 0,
        "managedLocal": 0,
        "runtimeLoad": 0,
        "moveExec": 0,
        "directStream": 0,
        "agentHost": 0,
        "identity": 0,
        "subagentRoute": 0,
        "subagentSession": 0,
        "taskTool": 0,
        "legacyTaskTool": 0,
        "actionRoute": 0,
        "resumeMode": 0,
        "completionWake": 0,
        "pushContextTimeout": 0,
        "rulesPreseed": 0,
        "legacy": 0,
    }


class CamPatchRulesTest(unittest.TestCase):
    def test_tested_versions(self):
        self.assertTrue(cam_patch.is_tested_version("3.19.13"))
        self.assertTrue(cam_patch.is_tested_version("3.18.25"))
        self.assertFalse(cam_patch.is_tested_version("3.20.0"))

    def test_unpatched_tested_version_is_pending(self):
        data = {
            "version": "3.19.13",
            "totals": {"sandAssignments": 0, "unpatchedAssignments": 4, "stream": _empty_stream()},
        }
        rows = {row["key"]: row for row in cam_patch.rule_rows(data)}
        self.assertEqual(rows["header"]["status"], "pending")
        self.assertEqual(rows["managedLocal"]["status"], "pending")
        self.assertNotEqual(rows["managedLocal"]["status"], "missing")

    def test_unknown_version_without_markers_is_missing(self):
        data = {
            "version": "3.20.0",
            "totals": {"sandAssignments": 0, "unpatchedAssignments": 0, "stream": _empty_stream()},
        }
        rows = {row["key"]: row for row in cam_patch.rule_rows(data)}
        self.assertEqual(rows["managedLocal"]["status"], "missing")

    def test_full_lifecycle_is_applied(self):
        stream = _empty_stream()
        stream.update(
            {
                "client": 3,
                "eligibility": 1,
                "managedLocal": 1,
                "runtimeLoad": 1,
                "moveExec": 1,
                "directStream": 1,
                "agentHost": 2,
                "identity": 1,
                "subagentRoute": 1,
                "subagentSession": 1,
                "taskTool": 1,
                "actionRoute": 1,
                "resumeMode": 1,
                "completionWake": 2,
            }
        )
        data = {
            "version": "3.19.13",
            "totals": {"sandAssignments": 6, "unpatchedAssignments": 0, "stream": stream},
        }
        rows = cam_patch.rule_rows(data)
        required = [r for r in rows if not r["optional"] and r["key"] != "legacyTaskTool"]
        self.assertTrue(all(r["status"] == "applied" for r in required), required)

    def test_run_cli_parses_json(self):
        payload = {"version": "3.19.13", "patched": False, "totals": {"stream": _empty_stream()}}

        class Fake:
            returncode = 0
            stdout = json.dumps(payload)
            stderr = ""

        with patch.object(cam_patch, "find_node", return_value="/usr/bin/node"), patch.object(
            cam_patch.subprocess, "run", return_value=Fake()
        ):
            out = cam_patch.inspect("/tmp/fake-app")
        self.assertEqual(out["version"], "3.19.13")

    def test_run_cli_surfaces_plugin_error(self):
        class Fake:
            returncode = 1
            stdout = ""
            stderr = "cursor-account-manager-sand: Sand Stream 补丁未完整命中，已中止写入\n    at apply"

        with patch.object(cam_patch, "find_node", return_value="/usr/bin/node"), patch.object(
            cam_patch.subprocess, "run", return_value=Fake()
        ):
            with self.assertRaises(cam_patch.CamPatchError) as ctx:
                cam_patch.apply("/tmp/fake-app")
        self.assertIn("未完整命中", str(ctx.exception))
        self.assertNotIn("at apply", str(ctx.exception))


class CamPatchPluginRestoreTest(unittest.TestCase):
    """对齐 cursor-account-manager extension.js 的 apply/restore 包装层。"""

    def test_plugin_default_state_root_honors_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"CURSOR_SAND_ROUTER_STATE": tmp}, clear=False):
                self.assertEqual(
                    cam_patch.plugin_default_state_root(), Path(tmp).resolve()
                )

    def test_apply_uses_plugin_state_root_not_sandclaimer(self):
        captured = {}

        class Fake:
            returncode = 0
            stdout = json.dumps({"changed": True, "files": []})
            stderr = ""

        def fake_run(args, **kwargs):
            captured["args"] = list(args)
            return Fake()

        with patch.object(cam_patch, "find_node", return_value="/usr/bin/node"), patch.object(
            cam_patch.subprocess, "run", side_effect=fake_run
        ):
            cam_patch.apply("/tmp/fake-app")
        joined = " ".join(str(a) for a in captured["args"])
        self.assertIn(str(cam_patch.plugin_default_state_root()), joined)
        self.assertNotIn("SandClaimer/sand-router", joined.replace("\\", "/"))

    def test_known_roots_include_plugin_vscode_and_legacy(self):
        roots = [str(p) for p in cam_patch.known_sand_state_roots()]
        self.assertIn(str(cam_patch.plugin_default_state_root()), roots)
        self.assertTrue(
            any(str(p).endswith("leila-local.cursor-sand-router") for p in roots)
        )
        self.assertIn(str(cam_patch.legacy_sandclaimer_state_root()), roots)

    def test_pick_restore_prefers_manifest_matching_app_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app = tmp_path / "Cursor.app" / "Contents" / "Resources" / "app"
            app.mkdir(parents=True)
            empty = tmp_path / "empty-root"
            empty.mkdir()
            winner = tmp_path / "winner-root"
            man_dir = winner / "backups" / "2026-09-08T00-00-00"
            man_dir.mkdir(parents=True)
            (man_dir / "manifest.json").write_text(
                json.dumps({"appRoot": str(app.resolve())}), encoding="utf-8"
            )
            with patch.object(
                cam_patch, "known_sand_state_roots", return_value=[empty, winner]
            ):
                picked = cam_patch.pick_restore_state_root(app)
            self.assertEqual(picked, winner)

    def test_restore_tries_next_state_root(self):
        calls = []

        def fake_cli(command, app_root, extra=None, state_dir=None):
            calls.append(Path(state_dir))
            if len(calls) == 1:
                raise cam_patch.CamPatchError("No backup manifest found")
            return {"restored": ["out/main.js"]}

        first, second = Path("/cam-first"), Path("/cam-second")
        with patch.object(cam_patch, "run_cli", side_effect=fake_cli), patch.object(
            cam_patch, "pick_restore_state_root", return_value=first
        ), patch.object(
            cam_patch, "known_sand_state_roots", return_value=[first, second]
        ):
            out = cam_patch.restore("/tmp/app")
        self.assertEqual(out["restored"], ["out/main.js"])
        self.assertEqual(calls, [first, second])

    def test_apply_retries_eperm_with_elevated(self):
        with patch.object(
            cam_patch,
            "run_cli",
            side_effect=cam_patch.CamPatchError("EPERM: operation not permitted"),
        ), patch.object(
            cam_patch, "run_elevated_cli", return_value={"changed": True, "files": []}
        ) as elev:
            out = cam_patch.apply("/tmp/fake-app")
        self.assertTrue(out["changed"])
        elev.assert_called_once()

    def test_dry_run_does_not_elevate_on_eperm(self):
        with patch.object(
            cam_patch,
            "run_cli",
            side_effect=cam_patch.CamPatchError("EPERM: operation not permitted"),
        ), patch.object(cam_patch, "run_elevated_cli") as elev:
            with self.assertRaises(cam_patch.CamPatchError):
                cam_patch.apply("/tmp/fake-app", dry_run=True)
            elev.assert_not_called()


if __name__ == "__main__":
    unittest.main()
