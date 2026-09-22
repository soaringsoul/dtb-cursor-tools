"""登录 Bot 二次确认文案。真正写入走独立 Grok Bot 客户端，不关 Cursor。"""

from __future__ import annotations

TITLE = "登录 Bot 确认"
OK = "确认登录 Bot"
CANCEL = "取消"


def confirm_lines(
    email: str | None,
    *,
    reset_machine_id: bool = False,
    web_token: bool = False,
    refresh_first: bool = False,
    kick_old_tool: bool = False,
) -> list[str]:
    who = str(email or "").strip() or "该账号"
    lines = [
        f"确定用 {who} 登录独立的 Grok Bot 客户端？",
        "不会关闭 Cursor。会把该号写入 Grok Bot 自带的「Cursor 账户」列表并切过去，然后重启 Grok Bot。",
    ]
    if refresh_first:
        lines.append("会先换新登录票，再用新票写入 Grok Bot。")
    else:
        lines.append("不会换新登录票，用的是列表里当前这张票。")
    if web_token:
        lines.append("这是网站会话票，会先换成客户端票再写入 Grok Bot。")
    if reset_machine_id:
        lines.append("「切号重置机器码」对登录 Bot 无效：不会改 Cursor 的机器码。")
    del kick_old_tool  # 复选框 label 承担说明，确认正文不重复
    return lines


def confirm_message(
    email: str | None,
    *,
    reset_machine_id: bool = False,
    web_token: bool = False,
    refresh_first: bool = False,
    kick_old_tool: bool = False,
) -> str:
    return "\n".join(
        confirm_lines(
            email,
            reset_machine_id=reset_machine_id,
            web_token=web_token,
            refresh_first=refresh_first,
            kick_old_tool=kick_old_tool,
        )
    )
