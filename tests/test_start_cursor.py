"""start_cursor 经典 IDE vs Agents/Grok Bot 启动参数。不真正拉起 Cursor。"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sand_patch


class CursorStartArgsTest(unittest.TestCase):
    def test_classic_true_passes_classic_flag(self):
        self.assertEqual(sand_patch.cursor_start_args(classic=True), ("--classic",))

    def test_classic_false_omits_classic(self):
        args = sand_patch.cursor_start_args(classic=False)
        self.assertEqual(args, ())
        self.assertNotIn("--classic", args)

    def test_classic_false_does_not_invent_a_third_flag(self):
        args = sand_patch.cursor_start_args(classic=False)
        self.assertNotIn("--glass", args)
        self.assertNotIn("--no-classic", args)

    def test_default_is_classic_for_switch_account(self):
        self.assertEqual(sand_patch.cursor_start_args(), ("--classic",))


class StartCursorCommandTest(unittest.TestCase):
    def _darwin(self, classic):
        layout = MagicMock()
        with (
            patch.object(sand_patch.sys, "platform", "darwin"),
            patch.object(
                sand_patch, "_find_app_bundle", return_value=Path("/Applications/Cursor.app")
            ),
            patch.object(sand_patch.shutil, "which", return_value="/usr/bin/open"),
            patch.object(sand_patch.subprocess, "run") as run,
        ):
            ok = sand_patch.start_cursor(layout, classic=classic)
        self.assertTrue(ok)
        return list(run.call_args[0][0])

    def test_darwin_classic_true_uses_classic_args(self):
        cmd = self._darwin(True)
        self.assertEqual(cmd[:3], ["/usr/bin/open", "-a", "/Applications/Cursor.app"])
        self.assertIn("--args", cmd)
        self.assertIn("--classic", cmd)

    def test_darwin_classic_false_omits_classic(self):
        cmd = self._darwin(False)
        self.assertEqual(cmd[:3], ["/usr/bin/open", "-a", "/Applications/Cursor.app"])
        self.assertNotIn("--classic", cmd)
        self.assertNotIn("--glass", cmd)

    def test_win32_classic_false_starts_exe_without_classic(self):
        layout = MagicMock()
        layout.executable = Path("C:/Cursor/Cursor.exe")
        with (
            patch.object(sand_patch.sys, "platform", "win32"),
            patch.object(sand_patch.subprocess, "Popen") as popen,
        ):
            ok = sand_patch.start_cursor(layout, classic=False)
        self.assertTrue(ok)
        cmd = list(popen.call_args[0][0])
        self.assertEqual(cmd[0], "C:/Cursor/Cursor.exe")
        self.assertNotIn("--classic", cmd)

    def test_default_start_cursor_still_classic(self):
        layout = MagicMock()
        with (
            patch.object(sand_patch.sys, "platform", "darwin"),
            patch.object(
                sand_patch, "_find_app_bundle", return_value=Path("/Applications/Cursor.app")
            ),
            patch.object(sand_patch.shutil, "which", return_value="/usr/bin/open"),
            patch.object(sand_patch.subprocess, "run") as run,
        ):
            sand_patch.start_cursor(layout)
        cmd = list(run.call_args[0][0])
        self.assertIn("--classic", cmd)


if __name__ == "__main__":
    unittest.main()
