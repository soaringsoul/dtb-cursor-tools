"""切号二次确认：点「切号」先出文案，确认后才真正切。不联网。"""

import unittest

import switch_confirm


class SwitchConfirmCopyTest(unittest.TestCase):
    def test_title_and_buttons(self):
        self.assertEqual(switch_confirm.TITLE, "切号确认")
        self.assertEqual(switch_confirm.OK, "确认切号")
        self.assertEqual(switch_confirm.CANCEL, "取消")

    def test_names_the_account_and_mentions_restart(self):
        lines = switch_confirm.confirm_lines("alice@example.com")
        self.assertTrue(any("alice@example.com" in line for line in lines))
        self.assertTrue(any("重启" in line for line in lines))

    def test_blank_email_falls_back(self):
        lines = switch_confirm.confirm_lines("  ")
        self.assertTrue(any("该账号" in line for line in lines))

    def test_reset_machine_id_extra_line(self):
        plain = switch_confirm.confirm_lines("a@b.com", reset_machine_id=False)
        reset = switch_confirm.confirm_lines("a@b.com", reset_machine_id=True)
        self.assertFalse(any("机器码" in line for line in plain))
        self.assertTrue(any("机器码" in line for line in reset))

    def test_web_token_extra_line(self):
        plain = switch_confirm.confirm_lines("a@b.com", web_token=False)
        web = switch_confirm.confirm_lines("a@b.com", web_token=True)
        self.assertFalse(any("网站会话" in line for line in plain))
        self.assertTrue(any("网站会话" in line for line in web))


if __name__ == "__main__":
    unittest.main()
