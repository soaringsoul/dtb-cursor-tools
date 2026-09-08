"""Sand 资格领取器：pywebview（Windows 用 Edge WebView2）+ 玻璃风 Web UI。

- UI 在 web/ 下（HTML/CSS/JS，iOS 玻璃浅蓝风）。
- Python 提供导入/领取能力，通过 window.pywebview.api 暴露给前端。
- 批量领取由前端逐个调用 claim_one 驱动，实时更新每行状态。
"""

import datetime
import errno
import json
import os
import socketserver
import subprocess
import sys
import time

import webview

import resolve
import sand_api
import cam_patch
import sand_patch
import patch_report
import browser_login
import device_guard
import local_cursor
from accounts import AccountStore
from accounts import format_export_line
from sand_api import claim as claim_token
from sand_api import get_sand_status
from sand_api import get_status
from sand_api import parse_token
from sand_api import verify as verify_token


_STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "SandClaimer")
_QUIET_HTTP_INSTALLED = False


def is_client_disconnect(exc=None) -> bool:
    """WebView / 浏览器掐掉本地 HTTP 连接时的常见错误，不是程序崩溃。"""
    err = sys.exc_info()[1] if exc is None else exc
    if isinstance(err, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return True
    return isinstance(err, OSError) and getattr(err, "errno", None) in (
        errno.ECONNRESET,
        errno.EPIPE,
        errno.ECONNABORTED,
    )


def install_quiet_local_http() -> None:
    """压掉 pywebview 内置 wsgiref 在客户端断连时刷到终端的 traceback。"""
    global _QUIET_HTTP_INSTALLED
    if _QUIET_HTTP_INSTALLED:
        return
    orig = socketserver.BaseServer.handle_error

    def handle_error(self, request, client_address):
        if is_client_disconnect():
            return
        orig(self, request, client_address)

    socketserver.BaseServer.handle_error = handle_error
    _QUIET_HTTP_INSTALLED = True

# 补丁锚定的 Cursor 版本（与 cursor-account-manager sandPatcher.TESTED_CURSOR_VERSIONS 一致）。
REQUIRED_CURSOR_VERSION = cam_patch.required_version_label()
_CURSOR_SHA = "2ba48ff3f7514cc4643c52ca9f7b3173d9b66137"
_CURSOR_DL_BASE = f"https://downloads.cursor.com/production/{_CURSOR_SHA}"
CURSOR_DOWNLOADS = {
    "windows": _CURSOR_DL_BASE + "/win32/x64/user-setup/CursorUserSetup-x64-3.18.9.exe",
    "windows_system": _CURSOR_DL_BASE + "/win32/x64/system-setup/CursorSetup-x64-3.18.9.exe",
    "mac": _CURSOR_DL_BASE + "/darwin/universal/Cursor-darwin-universal.dmg",
}


def _os_key() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def _read_json(name: str, default):
    try:
        with open(os.path.join(_STATE_DIR, name), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _write_json(name: str, data) -> None:
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        path = os.path.join(_STATE_DIR, name)
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(path + ".tmp", path)
    except Exception:
        pass


def resource_path(rel: str) -> str:
    """兼容 PyInstaller onefile：优先用解包目录 _MEIPASS。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def build_export_text(store, payload):
    """把前端给的分段 payload 拼成导出 txt。返回 (text, count)；count=0 表示没有可导出的账号。

    输出结构（每段）：
        # ===== 标题 ｜ 共 N 个 =====
        # 说明：分类依据
        #   [1] 邮箱  订阅到期 … · Bot 0% · Auto 62% · 高级 100%
        #   [2] …
        邮箱----user_id::jwt          <- 与上面 [n] 一一对应，行本身不带注释
    """
    sections = []
    header_lines: list[str] = []
    if isinstance(payload, dict):
        sections = list(payload.get("sections") or [])
        header_lines = [str(x) for x in (payload.get("header") or []) if str(x).strip()]
    elif isinstance(payload, list):
        sections = [{"title": "", "ids": payload}]

    seen: set[str] = set()
    blocks: list[str] = []
    count = 0
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        title = str(sec.get("title") or "").strip()
        note = str(sec.get("note") or "").strip()
        annotations = sec.get("annotations") if isinstance(sec.get("annotations"), dict) else {}
        ids = [str(x) for x in (sec.get("ids") or [])]
        lines: list[str] = []
        index_lines: list[str] = []
        for account_id in ids:
            if account_id in seen:
                continue
            item = store.get(account_id)
            if not item or not item.get("token"):
                continue
            try:
                line = format_export_line(item)
            except Exception:
                continue
            seen.add(account_id)
            lines.append(line)
            label = item.get("label") or ""
            shown = label if "@" in label else account_id
            extra = str(annotations.get(account_id) or "").strip()
            index_lines.append("#   [%d] %s%s" % (len(lines), shown, ("  " + extra) if extra else ""))
        if not lines:
            continue
        if title:
            blocks.append("# ===== %s ｜ 共 %d 个 =====" % (title, len(lines)))
            if note:
                blocks.append("# 说明：%s" % note)
            blocks.extend(index_lines)
        blocks.extend(lines)
        blocks.append("")
        count += len(lines)

    if count == 0:
        return "", 0
    head = ["# %s" % x for x in header_lines]
    if head:
        head.append("")
    return "\n".join(head + blocks).rstrip() + "\n", count


class Api:
    # 属性必须以下划线开头：pywebview 生成 JS 桥接时会递归遍历 js_api 的公开属性
    # （webview/util.py get_functions），遍历到 Window 对象会与建窗线程互等而死锁。
    def __init__(self) -> None:
        self._store = AccountStore()
        self._window: webview.Window | None = None
        # 本机设备保护：每账号一个守护线程，名单记忆落在 _STATE_DIR/device_guard.json（重启不自动开）。
        # 已打开隔离浏览器时，检测/踢下线复用那扇窗口，不会每轮再拉起浏览器。
        self._guard = device_guard.DeviceGuardManager(
            os.path.join(_STATE_DIR, "device_guard.json"),
            fetch_sessions=browser_login.fetch_sessions_smart,
            revoke_session=browser_login.revoke_session_smart,
        )

    def _auth_of(self, account_id: str):
        """取账号的 (user_id, jwt, claims, item, error)；账号不存在或 token 解析失败时 error 非空。"""
        item = self._store.get(account_id)
        if not item or not item.get("token"):
            return None, None, {}, None, "账号不存在"
        try:
            user_id, jwt, claims = parse_token(item["token"])
        except Exception as exc:
            return None, None, {}, item, f"token 解析失败：{exc}"
        return user_id, jwt, claims or {}, item, ""

    def import_files(self) -> dict:
        """弹原生文件选择框，导入 JSON/文本账号文件。"""
        paths = None
        try:
            if self._window is not None:
                paths = self._window.create_file_dialog(
                    webview.OPEN_DIALOG,
                    allow_multiple=True,
                    file_types=("JSON 文件 (*.json)", "文本文件 (*.txt)", "所有文件 (*.*)"),
                )
        except Exception:
            paths = None
        if not paths:
            return {"added": 0, "ids": [], "accounts": self._store.list()}
        added = self._store.add_json_files(list(paths))
        # ids 交给前端：导入后自动验证这些号（邮箱 / 用量 / 订阅剩余时间）。
        return {"added": len(added), "ids": [x["id"] for x in added], "accounts": self._store.list()}

    def import_text(self, text: str) -> dict:
        added = self._store.add_text(text or "")
        return {"added": len(added), "ids": [x["id"] for x in added], "accounts": self._store.list()}

    def detect_local_account(self) -> dict:
        """以本机账号探测：读本机 Cursor 登录 token，自动加入列表（回写真实邮箱）。"""
        acct = local_cursor.read_local_account()
        if not acct or not acct.get("token"):
            return {"ok": False, "error": "未检测到本机 Cursor 登录（请先在本机 Cursor 登录账号）"}
        touched = self._store.add_text(acct["token"])
        account_id = touched[0]["id"] if touched else None
        email = acct.get("email")
        if account_id and email and "@" in email:
            self._store.set_label(account_id, email)
        return {
            "ok": True,
            "id": account_id,
            "email": email,
            "membership": acct.get("membership"),
            "accounts": self._store.list(),
        }

    def local_identity(self) -> dict:
        """本机 Cursor 当前登录的 user id / 邮箱。未登录返回 ok=False。不写入账号表。"""
        acct = local_cursor.read_local_account()
        if not acct or not acct.get("token"):
            return {"ok": False, "userId": None, "email": None}
        try:
            user_id, _jwt, claims = parse_token(acct["token"])
        except Exception:
            return {"ok": False, "userId": None, "email": None}
        email = acct.get("email") or claims.get("email")
        return {"ok": True, "userId": user_id, "email": email}

    def list_accounts(self) -> list:
        return self._store.list()

    def remove_account(self, account_id: str) -> list:
        self._store.remove(account_id)
        self._guard.forget(account_id)
        return self._store.list()

    def remove_accounts(self, account_ids) -> dict:
        """批量删除勾选的账号，一次落盘。"""
        ids = [str(x) for x in (account_ids or [])]
        removed = self._store.remove_many(ids)
        for account_id in ids:
            self._guard.forget(account_id)
        return {"removed": removed, "accounts": self._store.list()}

    def set_label(self, account_id: str, label: str) -> bool:
        """把查到的真实邮箱回写到账号，刷新/领取后行内显示邮箱。"""
        self._store.set_label(account_id, label)
        return True

    def clear_accounts(self) -> list:
        for account_id in [x.get("id") for x in self._store.list()]:
            if account_id:
                self._guard.forget(account_id)
        self._store.clear()
        return self._store.list()

    def export_accounts(self, payload) -> dict:
        """导出 txt：按分类分段写入，账号行仍是 邮箱----user_id::jwt（保持可原样粘回导入），一个号只出现一次。

        每段头部用 # 注释注明：分类名 / 数量 / 分类依据，并逐个列出该段每个号的到期、剩余与三池用量
        （注释只放头部，账号行本身不加尾注，避免别的工具按 ---- 切分时把注释当成 token）。
        payload = {"sections": [{"title", "note", "ids": [...], "annotations": {id: "剩 3天 · Bot 0% …"}}],
                   "header": ["整体说明行", ...]}
        段内顺序由前端决定（已按剩余时间从短到长排好），这里原样保留。
        """
        text, count = build_export_text(self._store, payload)
        if count == 0:
            return {"ok": False, "error": "没有可导出的账号"}

        path = None
        try:
            if self._window is not None:
                fname = "sand_export_%s.txt" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                result = self._window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename=fname,
                    file_types=("文本文件 (*.txt)", "所有文件 (*.*)"),
                )
                if isinstance(result, (list, tuple)):
                    path = result[0] if result else None
                else:
                    path = result
        except Exception as exc:
            return {"ok": False, "error": f"打开保存框失败：{exc}"}
        if not path:
            return {"ok": False, "error": "已取消", "count": 0}

        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except Exception as exc:
            return {"ok": False, "error": f"写入文件失败：{exc}"}
        return {"ok": True, "count": count, "path": str(path), "text": text}

    def account_export_text(self, account_id: str) -> dict:
        """单条复制：邮箱----user_id::jwt，与导入行格式一致。"""
        item = self._store.get(account_id)
        if not item or not item.get("token"):
            return {"ok": False, "error": "账号不存在"}
        try:
            text = format_export_line(item)
        except Exception as exc:
            return {"ok": False, "error": f"导出失败：{exc}"}
        label = item.get("label") or ""
        email = label if "@" in label else (item.get("id") or "")
        return {"ok": True, "text": text, "email": email}

    def clip_set(self, text: str) -> dict:
        """浏览器剪贴板不可用时的兜底：用 Windows clip.exe 写系统剪贴板（token 为 ASCII，无编码问题）。"""
        try:
            cmd = "clip" if os.name == "nt" else "pbcopy"
            kwargs = {"input": (text or "").encode("utf-8"), "timeout": 5}
            if os.name == "nt":
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW，避免闪黑框
            proc = subprocess.run(cmd, **kwargs)
            return {"ok": proc.returncode == 0}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def claim_one(self, account_id: str) -> dict:
        item = self._store.get(account_id)
        if not item:
            return {"outcome": "failed", "detail": "账号不存在"}
        try:
            return claim_token(item["token"])
        except Exception as exc:
            return {"outcome": "failed", "detail": str(exc)}

    def status_one(self, account_id: str) -> dict:
        item = self._store.get(account_id)
        if not item:
            return {"error": "账号不存在"}
        try:
            return get_status(item["token"])
        except Exception as exc:
            return {"error": str(exc)}

    def sand_status_one(self, account_id: str) -> dict:
        """领取之后只刷 Bot 周用量这一池（1 个接口），不重复拉套餐/订阅。"""
        item = self._store.get(account_id)
        if not item:
            return {"error": "账号不存在"}
        try:
            return get_sand_status(item["token"])
        except Exception as exc:
            return {"error": str(exc)}

    def verify_one(self, account_id: str) -> dict:
        """验证账号：token 是否有效（过期/401/403）+ 邮箱 / 套餐 / 订阅剩余 / 三池用量 / 登录会话。"""
        item = self._store.get(account_id)
        if not item:
            return {"error": "账号不存在"}
        try:
            return verify_token(item["token"])
        except Exception as exc:
            return {"error": str(exc)}

    def open_login(self, account_id: str) -> dict:
        """用该账号 token 打开一个已登录浏览器并跳到 Sand 领取页，供手动完成（免费号绑卡等）。"""
        item = self._store.get(account_id)
        if not item:
            return {"ok": False, "error": "账号不存在"}
        try:
            user_id, jwt, _claims = parse_token(item["token"])
        except Exception as exc:
            return {"ok": False, "error": f"token 解析失败：{exc}"}
        try:
            opened = browser_login.open_with_token(user_id, jwt)
            return {"ok": True, "browser": opened.get("name"), "reused": bool(opened.get("reused"))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _open_account_browser(self, account_id: str, url: str) -> dict:
        """隔离浏览器 + 注入该号会话 cookie，打开指定 cursor.com 页面，浏览器留给用户。"""
        user_id, jwt, _claims, _item, error = self._auth_of(account_id)
        if error:
            return {"ok": False, "error": error}
        try:
            opened = browser_login.open_with_token(user_id, jwt, url=url)
            return {
                "ok": True,
                "browser": opened.get("name"),
                "url": url,
                "reused": bool(opened.get("reused")),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_dashboard(self, account_id: str) -> dict:
        """进控制台：用该账号登录态开一个隔离浏览器到 Cursor 控制台（dashboard/spending），浏览器留给用户。

        与 cursor-account-manager 插件的 openAccountDashboard 一致：独立 profile + CDP 注入
        WorkosCursorSessionToken，不碰用户日常浏览器的登录态。
        """
        return self._open_account_browser(account_id, browser_login.CURSOR_DASHBOARD_SPENDING)

    def open_sessions_page(self, account_id: str) -> dict:
        """打开官方 Active Sessions 页：同样注入 token，供人手过校验或在网页里查看/踢设备。

        每个账号只开一扇窗口：已打开则前置并跳到会话页，不新开。之后检测/踢下线复用这扇窗口。
        """
        return self._open_account_browser(account_id, browser_login.CURSOR_DASHBOARD_SESSIONS)

    # ---- 登录设备：实时查看 / 踢下线 ----

    def list_sessions(self, account_id: str) -> dict:
        """查看设备：实时拉取该账号云端登录会话（GET /api/auth/sessions），不依赖上次验证的缓存。"""
        user_id, jwt, claims, item, error = self._auth_of(account_id)
        if error:
            return {"ok": False, "error": error, **sand_api.empty_session_block(error)}
        try:
            block = browser_login.fetch_sessions_smart(user_id, jwt)
        except Exception as exc:
            block = sand_api.empty_session_block(str(exc))
        local = self.local_identity()
        is_local = bool(local.get("ok") and local.get("userId") == account_id)
        block["sessions"] = sand_api.annotate_local_sessions(block.get("sessions"), is_local)
        label = (item or {}).get("label") or ""
        email = label if "@" in label else (claims.get("email") or user_id)
        return {
            "ok": not block.get("sessionError"),
            "error": block.get("sessionError") or "",
            "email": email,
            # 本工具用的这张票是网页(web)还是客户端(session)会话：踢掉同类会话前给用户提个醒。
            "tokenType": claims.get("type"),
            **block,
            "browserOpen": browser_login.has_live_browser(user_id),
        }

    def revoke_session(self, account_id: str, session_id: str, session_type: str = "") -> dict:
        """踢下线：与官网 Revoke 相同的 POST body。列表里还在就不能算踢掉。"""
        user_id, jwt, _claims, _item, error = self._auth_of(account_id)
        if error:
            return {"ok": False, "error": error, "status": 0}
        try:
            return browser_login.revoke_session_smart(user_id, jwt, session_id, session_type)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "status": 0}

    # ---- 本机设备保护：按设定间隔检测，自动下线未保留设备 ----

    def device_guard_start(self, account_id: str, keep_session_ids, interval_minutes=1) -> dict:
        """开启保护：keep_session_ids 是要保留的 sessionId 列表（不能为空）。interval_minutes 为检测间隔（1–120 分钟）。"""
        user_id, jwt, _claims, _item, error = self._auth_of(account_id)
        if error:
            return {"ok": False, "error": error}
        try:
            return self._guard.start(
                account_id, user_id, jwt, keep_session_ids, interval_minutes=interval_minutes
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def device_guard_stop(self, account_id: str) -> dict:
        try:
            return self._guard.stop(account_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def device_guard_stop_all(self) -> dict:
        try:
            return self._guard.stop_all()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def device_guard_status(self) -> dict:
        """account_id -> {running, keepIds, lastTickAt, lastError, kickedCount, lastKicked, sessionCount, …}。"""
        try:
            return self._guard.status()
        except Exception:
            return {}

    def switch_account(self, account_id: str, reset_machine_id: bool = False) -> dict:
        """一键切号：关闭本机 Cursor → 写入所选账号登录态（可选重置机器码）→ 重开 Cursor。"""
        item = self._store.get(account_id)
        if not item:
            return {"ok": False, "error": "账号不存在"}
        try:
            user_id, jwt, claims = parse_token(item["token"])
        except Exception as exc:
            return {"ok": False, "error": f"token 解析失败：{exc}"}
        label = item.get("label") or ""
        email = label if "@" in label else (claims.get("email") or user_id)
        # 闸①（借鉴 kc-cursor cursor-manager）：切进一个死号 = 设置里有号、一发就重登（等于没切）。
        # 先本地看 exp（离线、即时），再联网探活；只在明确失效（401/403）时拦，网络问题不拦。
        exp = sand_api.token_exp(item["token"])
        if exp is not None and exp <= int(time.time()):
            return {
                "ok": False,
                "error": "该账号登录票已过期：切了也登不上（设置里会有号、一发消息就要重登，等于没切）。"
                "请重新领取/导入该号的新 token 再切。",
            }
        if sand_api.probe_token_alive(item["token"]) == "dead":
            return {
                "ok": False,
                "error": "该账号已失效或被限（服务端不认这张登录票：401/403 或无会话）：切了也登不上（等于没切）。"
                "请换一个有效号，或重新导入该号的新 token。",
            }
        # 网站会话票（type=web）直接写进客户端对话层不认（能显示账号、一发消息就要重登）。
        # 先按官方深度登录换成客户端 session 票；换不到再回退原样写（至少不比以前差）。
        refresh_jwt = None
        exchanged = False
        if str(claims.get("type") or "").lower() == "web":
            try:
                access, refresh = sand_api.exchange_web_to_session(item["token"])
            except Exception:
                access, refresh = None, None
            if access:
                jwt = access
                refresh_jwt = refresh or access
                exchanged = True
            else:
                return {
                    "ok": False,
                    "error": "这是网站会话（type=web），换取客户端登录票失败："
                    "多为该 token 已过期/被限流或网络问题。可重试，或用「网页领取」在浏览器里用。",
                }
        try:
            layout = sand_patch.resolve_cursor_layout()
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": f"未找到本机 Cursor：{exc}"}
        try:
            sand_patch.close_cursor(layout)
            local_cursor.write_local_account(jwt, email, refresh_token=refresh_jwt, user_id=user_id)
            if reset_machine_id:
                local_cursor.reset_machine_ids()
            sand_patch.start_cursor(layout)
            return {
                "ok": True,
                "email": email,
                "resetMachineId": bool(reset_machine_id),
                "exchanged": exchanged,
            }
        except PermissionError as exc:
            return {"ok": False, "error": f"没有写入权限，请用管理员身份运行本工具：{exc}"}
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---- 状态记忆 / 设置持久化 ----

    def load_status(self) -> dict:
        """读取上次每个账号的 Sand 状态（套餐/额度/是否开通），重开时还原到列表。"""
        data = _read_json("status.json", {})
        return data if isinstance(data, dict) else {}

    def save_status(self, data: dict) -> bool:
        _write_json("status.json", data or {})
        return True

    def get_settings(self) -> dict:
        data = _read_json("settings.json", {})
        return data if isinstance(data, dict) else {}

    def set_settings(self, data: dict) -> bool:
        _write_json("settings.json", data or {})
        return True

    # ---- 本机 Cursor Sand 补丁（cursor-account-manager 的 sandPatcher / sandStream）----

    def set_cursor_path(self, path: str) -> dict:
        """设置自定义 Cursor 路径（传空或 auto 恢复自动检测），随后返回最新补丁状态。"""
        value = (path or "").strip() or "auto"
        try:
            sand_patch.save_cursor_path(value)
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return self.patch_status()

    def patch_status(self) -> dict:
        try:
            layout = sand_patch.resolve_cursor_layout()
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            st = sand_patch.inspect_status(layout)
            os_key = _os_key()
            result = {
                "ok": True,
                "version": layout.version,
                "path": str(layout.install_root),
                "installed": bool(st.installed),
                "streamMode": bool(st.stream_mode_installed),
                "streamCapable": bool(st.stream_capable),
                "client": st.client_markers + st.legacy_client_markers,
                "eligibility": st.eligibility_markers + st.legacy_eligibility_markers,
                "requiredVersion": REQUIRED_CURSOR_VERSION,
                "testedVersion": bool(st.tested_version),
                "requiredVersions": list(cam_patch.TESTED_CURSOR_VERSIONS),
                "os": os_key,
                "downloadUrl": CURSOR_DOWNLOADS.get(os_key, CURSOR_DOWNLOADS["windows"]),
                "downloadUrlSystem": CURSOR_DOWNLOADS["windows_system"] if os_key == "windows" else "",
            }
            # 逐条规则 + 运行中的 Cursor 是否就是这一份（群友「显示成功其实没成功」的两大来源）。
            try:
                result.update(patch_report.status_report(layout))
            except Exception as exc:
                result["rulesError"] = str(exc)
            return result
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": str(exc), "version": layout.version, "path": str(layout.install_root)}

    def open_url(self, url: str) -> dict:
        """在系统默认浏览器打开链接（用于「下载对应版本 Cursor」按钮）。"""
        try:
            import webbrowser
            if not (url or "").startswith(("http://", "https://")):
                return {"ok": False, "error": "非法链接"}
            webbrowser.open(url)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def apply_patch(self) -> dict:
        """打补丁并返回逐步 / 逐规则报告（见 patch_report.install_with_report）。"""
        try:
            layout = sand_patch.resolve_cursor_layout()
        except sand_patch.SandToolError as exc:
            return {
                "ok": False,
                "verdict": "failed",
                "headline": f"定位 Cursor 失败：{exc}",
                "steps": [{"key": "locate", "title": "定位 Cursor", "status": "fail", "detail": str(exc),
                           "fix": "在补丁面板「设置路径」里填 Cursor.exe / 安装目录的路径"}],
                "rulesBefore": [], "rulesAfter": [],
            }
        try:
            report = patch_report.install_with_report(layout)
        except PermissionError as exc:
            report = {"ok": False, "verdict": "failed", "headline": f"没有写入权限：{exc}",
                      "steps": [{"key": "write", "title": "写入补丁文件", "status": "fail", "detail": str(exc),
                                 "fix": "右键「以管理员身份运行」本工具后重试"}], "rulesBefore": [], "rulesAfter": []}
        except Exception as exc:
            report = {"ok": False, "verdict": "failed", "headline": f"{type(exc).__name__}: {exc}",
                      "steps": [{"key": "unexpected", "title": "未预期错误", "status": "fail", "detail": str(exc),
                                 "fix": "把这段文字发给群主"}], "rulesBefore": [], "rulesAfter": []}
        report["error"] = "" if report.get("ok") else report.get("headline", "")
        return report

    def verify_runtime(self) -> dict:
        """读 Cursor 的 agent-host 日志，判断补丁是否真的在跑（本地回路 / 每轮走的路 / 401）。"""
        try:
            return patch_report.runtime_report()
        except Exception as exc:
            return {"ok": False, "verdict": "no-log", "headline": f"读取日志失败：{exc}", "turns": [], "errors": [], "checks": []}

    def restore_patch(self) -> dict:
        try:
            layout = sand_patch.resolve_cursor_layout()
            sand_patch.uninstall(layout)
            return {"ok": True}
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": str(exc)}
        except PermissionError as exc:
            return {"ok": False, "error": f"没有写入权限，请用管理员身份运行本工具：{exc}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def main() -> None:
    resolve.install()
    install_quiet_local_http()
    api = Api()
    window = webview.create_window(
        "Sand 资格领取器",
        resource_path(os.path.join("web", "index.html")),
        js_api=api,
        width=1220,
        height=800,
        min_size=(960, 640),
        background_color="#EAF2FF",
    )
    api._window = window
    webview.start()
    # 窗口关闭后叫停所有保护线程并把「运行中」落成 False：下次打开只回填名单，不自动踢人。
    api._guard.stop_all(wait=True, timeout=3.0)


if __name__ == "__main__":
    main()
