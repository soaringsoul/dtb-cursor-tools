"""验证生效：读 agent-host 日志时，预期的云端回落不应被说成旧版补丁。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import patch_report


def _turn(ts, runtime, reason, action="userMessageAction", model="grok-4.6"):
    payload = (
        f'{{"runtime":"{runtime}","reason":"{reason}",'
        f'"conversationId":"c","generationUUID":"g",'
        f'"actionCase":"{action}","modelId":"{model}"}}'
    )
    return f"{ts} [info] Selected Agent Host turn runtime {payload}"


class RuntimeReportTest(unittest.TestCase):
    def _report(self, body: str):
        handle = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8")
        handle.write(body)
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))
        with patch.object(patch_report, "_agent_host_logs", return_value=[Path(handle.name)]):
            return patch_report.runtime_report(max_turns=6)

    def test_simulated_message_among_local_turns_is_expected_not_old_patch(self):
        text = "\n".join(
            [
                "2026-09-07 13:56:52.535 [info] Activating agent host extension",
                "2026-09-07 13:56:52.535 [info] Loaded managed local-loop runtime",
                "2026-09-07 13:56:52.756 [info] move_exec ON: using @anysphere/agent-host-exec",
                _turn("2026-09-07 13:57:28.412", "managed-local", "sand-client"),
                _turn("2026-09-07 14:00:06.694", "connect", "simulated-message-not-supported", model="claude-fable-5-1"),
                _turn("2026-09-07 14:00:10.679", "managed-local", "sand-client", model="claude-fable-5-1"),
            ]
        )
        rep = self._report(text)
        self.assertEqual(rep["verdict"], "working")
        self.assertNotIn("重新打", rep["headline"])
        sim = next(t for t in rep["turns"] if t["reason"] == "simulated-message-not-supported")
        self.assertIn("属正常", sim["hint"])
        self.assertNotIn("旧版", sim["hint"])
        self.assertNotIn("1.2.1", sim["hint"])

    def test_invalid_arguments_tool_noise_is_not_listed(self):
        text = "\n".join(
            [
                "2026-09-07 13:56:52.535 [info] Activating agent host extension",
                "2026-09-07 13:56:52.535 [info] Loaded managed local-loop runtime",
                "2026-09-07 13:56:52.756 [info] move_exec ON: using @anysphere/agent-host-exec",
                _turn("2026-09-07 13:57:28.412", "managed-local", "sand-client"),
                "2026-09-07 14:00:39.232 [error] Error: Invalid arguments:",
                "path: Required",
                '2026-09-07 14:00:12.210 [error] ActionRequiredError: You have an unpaid invoice Visit cursor.com/dashboard',
            ]
        )
        rep = self._report(text)
        blob = "\n".join(rep["errors"])
        self.assertNotIn("Invalid arguments", blob)
        self.assertIn("unpaid invoice", blob)
