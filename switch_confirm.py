"""切号二次确认文案。真正切号仍走 switch_account，这里只负责弹窗说什么。"""

from __future__ import annotations

TITLE = "切号确认"
OK = "确认切号"
CANCEL = "取消"


def confirm_lines(
    email: str | None,
    *,
    reset_machine_id: bool = False,
    web_token: bool = False,
) -> list[str]:
    who = str(email or "").strip() or "该账号"
    lines = [
        f"确定把本机 Cursor 切到 {who}？",
        "会先关掉当前 Cursor，写入登录态后再自动重启。",
    ]
    if web_token:
        lines.append("这是网站会话，切号时会先换成客户端登录票，大约多几秒。")
    if reset_machine_id:
        lines.append("已勾选「切号重置机器码」，本机机器码也会一起换掉。")
    return lines


def confirm_message(
    email: str | None,
    *,
    reset_machine_id: bool = False,
    web_token: bool = False,
) -> str:
    return "\n".join(confirm_lines(email, reset_machine_id=reset_machine_id, web_token=web_token))
