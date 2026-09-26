"""关闭窗口前的确认文案与决策。

closing 回调跑在 GUI 线程（macOS 上是 Cocoa windowShouldClose）。
pywebview 的 evaluate_js / create_confirmation_dialog 都会把工作丢回主线程再用
信号量等待，所以这两个都不能在 closing 里同步调用——会自己等自己，表现为
「Python 未响应」、必须强制退出。
"""

import threading
import time

QUIT_TITLE = "退出确认"
QUIT_MESSAGE = "确定关闭 cursorAdmin？"
QUIT_OK = "关闭应用"
QUIT_CANCEL = "取消"

GUI_SAFE_ACTIONS = frozenset({"allow", "defer_js"})


def closing_action(confirmed: bool, ui_ready: bool) -> str:
    """窗口即将关闭时怎么处理。

    allow: 已经确认，或界面还没就绪（避免系统对话框同样卡死），放行
    defer_js: 取消本次关闭，另开线程再弹页内确认框
    """
    if confirmed or not ui_ready:
        return "allow"
    return "defer_js"


def is_gui_thread_safe(action: str) -> bool:
    return action in GUI_SAFE_ACTIONS


def cancels_close(action: str) -> bool:
    return action == "defer_js"


DESTROY_DELAY_SEC = 0.3


def schedule_destroy(destroy_fn, *, sleep=None, spawn=None) -> None:
    """JS 桥 RPC 返回之后再关窗。

    不能在 request_quit 里同步 destroy：js_bridge 还要用 evaluate_js 把 {ok:true}
    回给前端。也不能指望页面 window.close()——WKWebView 里它关不掉 NSWindow。
    """
    if sleep is None:
        sleep = time.sleep

    def run():
        sleep(DESTROY_DELAY_SEC)
        destroy_fn()

    if spawn is None:
        threading.Thread(target=run, name="quit-destroy", daemon=True).start()
    else:
        spawn(run)


def confirm_and_destroy(set_confirmed, destroy_fn, *, schedule=None) -> None:
    """页内点了「关闭应用」：先置位让 closing 放行，再延迟 destroy。

    必须先置位再 schedule：destroy→window.close 会再进 on_closing，
    此时 confirmed 必须已是 True，否则又弹一次确认框。
    """
    set_confirmed()
    if destroy_fn is None:
        return
    (schedule or schedule_destroy)(destroy_fn)
