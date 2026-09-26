"""关闭确认决策：不依赖 pywebview。

Cocoa 上 windowShouldClose 跑在 GUI 主线程；evaluate_js / create_confirmation_dialog
都会 AppHelper.callAfter 后再用信号量等结果。closing 回调里同步调用就会自己等自己。
"""

import unittest

from app import quit_confirm


class ClosingActionTest(unittest.TestCase):
    def test_already_confirmed_allows_close(self):
        self.assertEqual(quit_confirm.closing_action(True, True), "allow")
        self.assertEqual(quit_confirm.closing_action(True, False), "allow")

    def test_ui_ready_defers_js_modal(self):
        self.assertEqual(quit_confirm.closing_action(False, True), "defer_js")

    def test_ui_not_ready_allows_close(self):
        # 系统确认框同样会在 Cocoa 主线程死锁；界面未就绪时宁可直接关。
        self.assertEqual(quit_confirm.closing_action(False, False), "allow")

    def test_all_actions_are_safe_on_gui_thread(self):
        for confirmed in (True, False):
            for ui_ready in (True, False):
                action = quit_confirm.closing_action(confirmed, ui_ready)
                self.assertTrue(
                    quit_confirm.is_gui_thread_safe(action),
                    msg=f"{action} would block Cocoa windowShouldClose",
                )

    def test_defer_js_cancels_this_close(self):
        self.assertTrue(quit_confirm.cancels_close("defer_js"))
        self.assertFalse(quit_confirm.cancels_close("allow"))

    def test_copy(self):
        self.assertIn("确定关闭", quit_confirm.QUIT_MESSAGE)
        self.assertEqual(quit_confirm.QUIT_TITLE, "退出确认")


class ScheduleDestroyTest(unittest.TestCase):
    def test_waits_then_destroys(self):
        # 不能在 JS 桥 RPC 里同步 destroy：桥还在 evaluate_js 回传结果。
        # 确认后必须先返回 RPC，再另开线程延迟关窗。
        calls = []
        sleeps = []

        def sleep(sec):
            sleeps.append(sec)

        def spawn(fn):
            fn()

        quit_confirm.schedule_destroy(lambda: calls.append("destroyed"), sleep=sleep, spawn=spawn)
        self.assertEqual(sleeps, [quit_confirm.DESTROY_DELAY_SEC])
        self.assertEqual(calls, ["destroyed"])
        self.assertGreater(quit_confirm.DESTROY_DELAY_SEC, 0)


class ConfirmAndDestroyTest(unittest.TestCase):
    def test_sets_flag_then_schedules_destroy(self):
        order = []

        def set_flag():
            order.append("flag")

        def destroy():
            order.append("destroy")

        def schedule(fn):
            order.append("schedule")
            fn()

        quit_confirm.confirm_and_destroy(set_flag, destroy, schedule=schedule)
        self.assertEqual(order, ["flag", "schedule", "destroy"])

    def test_missing_destroy_still_sets_flag(self):
        flags = []
        quit_confirm.confirm_and_destroy(lambda: flags.append(True), None)
        self.assertEqual(flags, [True])


if __name__ == "__main__":
    unittest.main()
