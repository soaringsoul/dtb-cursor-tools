"""cursor账号管理器：pywebview（Windows 用 Edge WebView2）+ 玻璃风 Web UI。

- UI 在 web/ 下（HTML/CSS/JS，iOS 玻璃浅蓝风）。
- Python 提供导入/领取能力，通过 window.pywebview.api 暴露给前端。
- 批量领取由前端逐个调用 claim_one 驱动，实时更新每行状态。
"""

import datetime
import errno
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time

import webview

import resolve
import sand_api
import sand_patch
import browser_login
import device_guard
import local_cursor
import grok_bot
import api_key_store
from accounts import AccountStore
from accounts import format_export_line
import quit_confirm
import login_detect
import ops_ui
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


def dock_icon_path() -> str:
    """开发时用源码目录里的 PNG。打包后的 .app 走 bundle 图标，这里找不到就跳过。"""
    for rel in ("assets/icon-1024.png", "icon.icns"):
        path = resource_path(rel)
        if os.path.isfile(path):
            return path
    return ""


def install_dock_icon() -> None:
    """用 ./launch.sh 启动时，Dock 默认是 Python 火箭。这里换成应用图标。"""
    if sys.platform != "darwin":
        return
    path = dock_icon_path()
    if not path:
        return
    try:
        from AppKit import NSApplication, NSImage
    except Exception:
        return
    image = NSImage.alloc().initWithContentsOfFile_(path)
    if image is None:
        return
    NSApplication.sharedApplication().setApplicationIconImage_(image)


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
        self._keys = api_key_store.ApiKeyStore()
        self._quit_confirmed = False
        self._ui_ready = False

    def mark_ui_ready(self) -> bool:
        self._ui_ready = True
        return True

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
        refresh_recorded = False
        if account_id and acct.get("refresh_token"):
            refresh_recorded = self._store.set_refresh_credentials(
                account_id, acct["refresh_token"], source="local_detect"
            )
        return {
            "ok": True,
            "id": account_id,
            "email": email,
            "membership": acct.get("membership"),
            "refreshRecorded": refresh_recorded,
            "accounts": self._store.list(),
        }

    def probe_refresh_one(self, account_id: str) -> dict:
        """探测并记录账号的 refresh_token（优先读本机 Cursor，其次已存/号池导入）。"""
        item = self._store.get(account_id)
        if not item:
            return {"ok": False, "error": "账号不存在"}
        source = None
        refresh = None
        client_id = item.get("clientId")
        acct = local_cursor.read_local_account()
        if acct and acct.get("refresh_token") and acct.get("token"):
            try:
                local_uid, _, _ = parse_token(acct["token"])
                if local_uid == account_id:
                    refresh = acct["refresh_token"]
                    source = "local"
            except Exception:
                pass
        if not refresh:
            refresh = item.get("refreshToken")
            if refresh:
                source = "stored"
        if not refresh:
            return {
                "ok": False,
                "error": "未能探测到 refresh_token（本机未登录该号，且列表里也没有已记录的 refresh）",
            }
        same_as_access = False
        try:
            _uid, access_jwt, _ = parse_token(item["token"])
            _uid2, refresh_jwt, _ = parse_token(refresh)
            same_as_access = access_jwt == refresh_jwt
        except Exception:
            pass
        self._store.set_refresh_credentials(account_id, refresh, client_id=client_id, source=source or "probe")
        return {
            "ok": True,
            "source": source,
            "sameAsAccess": same_as_access,
            "hasClientId": bool(client_id),
            "accounts": self._store.list(),
        }

    def refresh_login_one(self, account_id: str) -> dict:
        """用已记录的 refresh_token 换取新的 access_token，并写回账号表。"""
        item = self._store.get(account_id)
        if not item:
            return {"ok": False, "error": "账号不存在"}
        refresh = item.get("refreshToken")
        client_id = item.get("clientId")
        if not refresh:
            probe = self.probe_refresh_one(account_id)
            if not probe.get("ok"):
                return probe
            item = self._store.get(account_id) or item
            refresh = item.get("refreshToken")
            client_id = item.get("clientId")
        if not refresh:
            return {"ok": False, "error": "缺少 refresh_token，请先「探测 Refresh」或导入号池整行"}
        result = sand_api.refresh_login_tokens_with_fallback(refresh, item.get("token"), client_id)
        if not result.get("ok"):
            return result
        access = result["accessToken"]
        new_refresh = result.get("refreshToken") or access
        new_client = result.get("clientId") or client_id
        self._store.update_login_tokens(account_id, access, refresh_token=new_refresh, client_id=new_client)
        return {
            "ok": True,
            "tokenType": result.get("tokenType"),
            "exp": result.get("exp"),
            "usedAccessAsRefresh": bool(result.get("usedAccessAsRefresh")),
            "accessToken": access,
            "accounts": self._store.list(),
        }

    def refresh_login_kick_old(self, account_id: str) -> dict:
        """换新登录票；只有换票成功后才立刻踢掉本工具旧客户端，绝不踢 Cursor IDE。"""
        item = self._store.get(account_id)
        if not item:
            return {"ok": False, "error": "账号不存在"}
        old_claims: dict = {}
        if item.get("token"):
            try:
                _ouid, _ojwt, old_claims = parse_token(item["token"])
            except Exception:
                old_claims = {}
        result = self.refresh_login_one(account_id)
        if not result.get("ok"):
            return result
        dropped = self._drop_stale_tool_session_after_refresh(
            account_id, old_claims, result.get("accessToken") or ""
        )
        out = dict(result)
        out.pop("accessToken", None)
        out["droppedSessionId"] = dropped
        if not dropped:
            out["kickError"] = "换票已成功，但没对上要踢的旧客户端（可能官方没新开会话，或对上的是本机 Cursor）"
        return out

    def _drop_stale_tool_session_after_refresh(self, account_id: str, old_claims: dict, new_access: str) -> str:
        """换票会新开一台 Desktop App：踢掉本工具原来那条，绝不踢 Cursor IDE。"""
        try:
            _uid, _jwt, new_claims = parse_token(new_access)
        except Exception:
            return ""
        try:
            listed = self.list_sessions(account_id)
        except Exception:
            return ""
        sessions = list(listed.get("sessions") or [])
        ide = ""
        try:
            local = self.local_identity()
            if local.get("ok") and local.get("userId") == account_id:
                ide = self._resolve_pinned_local_session(account_id, sessions)
        except Exception:
            ide = ""
        drop = login_detect.stale_tool_session_id_after_refresh(old_claims, new_claims, sessions, ide)
        if not drop:
            return ""
        kind = "SESSION_TYPE_CLIENT"
        for row in sessions:
            if str(row.get("sessionId") or "").strip() == drop:
                kind = str(row.get("typeRaw") or row.get("type") or kind)
                break
        try:
            self.revoke_session(account_id, drop, kind)
        except Exception:
            return ""
        new_sid = login_detect.match_session_id_by_jwt_time(sessions, new_claims)
        self._replace_guard_keep_session(account_id, drop, new_sid)
        return drop

    def _replace_guard_keep_session(self, account_id: str, old_sid: str, new_sid: str) -> None:
        """保护若在跑：把旧本工具会话从保留名单换成新的，并换上新票。"""
        try:
            if not self._guard.is_running(account_id):
                return
        except Exception:
            return
        old = str(old_sid or "").strip()
        new = str(new_sid or "").strip()
        keep = [str(x or "").strip() for x in (self._guard.saved_keep_ids(account_id) or []) if str(x or "").strip()]
        if old:
            keep = [new if x == old else x for x in keep]
        if new and new not in keep:
            keep.append(new)
        keep = [x for x in keep if x]
        if not keep:
            return
        user_id, jwt, _claims, _item, error = self._auth_of(account_id)
        if error:
            return
        st = {}
        try:
            st = (self._guard.status() or {}).get(str(account_id) or "") or {}
        except Exception:
            st = {}
        seconds = st.get("intervalSeconds") or 30
        try:
            self._guard.start(account_id, user_id, jwt, keep, interval_seconds=seconds)
        except Exception:
            return

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

    def _local_hostname(self) -> str:
        try:
            return socket.gethostname() or ""
        except Exception:
            return ""

    def _remember_local_session(self, account_id: str, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        binds = _read_json("local_session_bind.json", {})
        if not isinstance(binds, dict):
            binds = {}
        mid = str((local_cursor.read_machine_ids() or {}).get("machineId") or "")
        binds[str(account_id)] = {
            "sessionId": sid,
            "machineId": mid,
            "hostname": self._local_hostname(),
            "labeledAt": int(time.time()),
        }
        _write_json("local_session_bind.json", binds)

    def _resolve_pinned_local_session(self, account_id: str, sessions) -> str:
        """用本机 Cursor JWT 的 time 对云端客户端 createdAt；对不上则回退本机记住的 sessionId。"""
        acct = local_cursor.read_local_account()
        claims_local: dict = {}
        if acct and acct.get("token"):
            try:
                _uid, _jwt, claims_local = parse_token(acct["token"])
            except Exception:
                claims_local = {}
        binds = _read_json("local_session_bind.json", {})
        if not isinstance(binds, dict):
            binds = {}
        mid = str((local_cursor.read_machine_ids() or {}).get("machineId") or "")
        saved = binds.get(str(account_id) or "")
        saved_sid = ""
        if isinstance(saved, dict) and (not mid or saved.get("machineId") == mid):
            saved_sid = str(saved.get("sessionId") or "")
        pinned = login_detect.resolve_local_session_id(sessions, claims_local, saved_sid)
        if pinned:
            self._remember_local_session(account_id, pinned)
        return pinned

    def list_accounts(self) -> list:
        return self._store.list()

    def list_account_tokens(self) -> dict:
        """显示 Token 开关打开后拉取：不含在 list_accounts 里，避免默认列表带出凭据。"""
        try:
            return {"ok": True, "tokens": self._store.token_views()}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "tokens": {}}

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

    def list_api_keys(self) -> list:
        return self._keys.list()

    def get_last_api_key_input(self) -> str:
        return self._keys.draft_text()

    def set_last_api_key_input(self, text: str) -> str:
        return self._keys.set_last_input(text or "")

    def import_api_keys(self, text: str) -> dict:
        return self._keys.import_keys(text or "")

    def remove_api_key(self, key_id: str) -> dict:
        ok = self._keys.remove(key_id)
        return {"ok": ok, "keys": self._keys.list(), "error": "" if ok else "密钥不存在"}

    def list_cloud_agents(self, key_id: str) -> dict:
        return self._keys.list_agents(key_id)

    def delete_all_cloud_agents(self, key_id: str) -> dict:
        return self._keys.delete_all_agents(key_id)

    def delete_cloud_agent(self, key_id: str, agent_id: str) -> dict:
        return self._keys.delete_agent(key_id, agent_id)

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
        pinned = ""
        if is_local:
            pinned = self._resolve_pinned_local_session(account_id, block.get("sessions") or [])
        tool_sid = ""
        if item and item.get("token"):
            try:
                _tuid, _tjwt, tool_claims = parse_token(item["token"])
                tool_sid = login_detect.match_session_id_by_jwt_time(
                    block.get("sessions") or [], tool_claims
                )
            except Exception:
                tool_sid = ""
        block["sessions"] = login_detect.sort_sessions_for_display(
            sand_api.annotate_local_sessions(
                block.get("sessions"),
                is_local,
                local_session_id=pinned or None,
                local_host=self._local_hostname() if pinned else None,
                tool_session_id=tool_sid or None,
            )
        )
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

    def revoke_sessions(self, account_id: str, items=None) -> dict:
        """批量踢下线。items 为 sessionId 字符串列表，或 {sessionId, type} 字典列表。一项失败不中断其余。"""
        user_id, jwt, _claims, _item, error = self._auth_of(account_id)
        if error:
            return {
                "ok": False,
                "error": error,
                "kicked": [],
                "failed": [],
                "kickedCount": 0,
                "failedCount": 0,
            }

        def _one(session_id, session_type):
            return browser_login.revoke_session_smart(user_id, jwt, session_id, session_type)

        return sand_api.revoke_many(items, _one)

    # ---- 本机设备保护：按设定间隔检测，自动下线未保留设备 ----

    def device_guard_start(self, account_id: str, keep_session_ids, interval_seconds=30) -> dict:
        """开启保护：keep_session_ids 是要保留的 sessionId 列表（不能为空）。interval_seconds 为检测间隔（5–3600 秒）。"""
        user_id, jwt, _claims, _item, error = self._auth_of(account_id)
        if error:
            return {"ok": False, "error": error}
        try:
            return self._guard.start(
                account_id, user_id, jwt, keep_session_ids, interval_seconds=interval_seconds
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def device_guard_start_auto(self, account_id: str, interval_seconds=30) -> dict:
        """批量保护：自动生成保留名单后开启。已在跑则跳过，不重开。"""
        if self._guard.is_running(account_id):
            st = (self._guard.status() or {}).get(str(account_id) or "") or {}
            return {"ok": True, "skipped": True, "reason": "already", "status": st}
        user_id, jwt, _claims, _item, error = self._auth_of(account_id)
        if error:
            return {"ok": False, "error": error}
        try:
            block = browser_login.fetch_sessions_smart(user_id, jwt)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if block.get("sessionError"):
            return {
                "ok": False,
                "error": block.get("sessionError") or "读取设备失败",
                "waf": bool(block.get("sessionWaf")),
            }
        local = self.local_identity()
        is_local = bool(local.get("ok") and local.get("userId") == account_id)
        keep = sand_api.pick_keep_session_ids(
            block.get("sessions") or [],
            self._guard.saved_keep_ids(account_id),
            is_local,
        )
        if not keep:
            return {"ok": False, "error": "当前没有登录设备，无法开启保护"}
        try:
            res = self._guard.start(
                account_id, user_id, jwt, keep, interval_seconds=interval_seconds
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if res.get("ok"):
            res["keepCount"] = len(keep)
            res["skipped"] = False
        return res

    def device_guard_pin_local(self, account_id: str, interval_seconds=30) -> dict:
        """一键本机保护：用本机 Cursor JWT 认 IDE 会话，必要时连同本工具会话一起保留。不刷票认设备。"""
        local = self.local_identity()
        if not (local.get("ok") and local.get("userId") == account_id):
            return {"ok": False, "error": "请先在本机 Cursor 登录这个号"}

        listed = self.list_sessions(account_id)
        sessions = list(listed.get("sessions") or [])
        session_count = len(sessions)
        status = {}
        try:
            status = (self._guard.status() or {}).get(str(account_id) or "") or {}
        except Exception:
            status = {}
        if not listed.get("ok"):
            return {
                "ok": False,
                "error": listed.get("error") or "读取设备失败",
                "keepIds": [],
                "beforeCount": session_count,
                "afterCount": session_count,
                "status": status,
            }

        ide_sid = self._resolve_pinned_local_session(account_id, sessions)
        tool_sid = ""
        item = self._store.get(account_id) or {}
        raw_token = item.get("token")
        if raw_token:
            try:
                _uid, _jwt, claims = parse_token(raw_token)
                tool_sid = login_detect.match_session_id_by_jwt_time(sessions, claims)
            except Exception:
                tool_sid = ""
        present = [str(row.get("sessionId") or "").strip() for row in sessions if isinstance(row, dict)]
        keep = login_detect.keep_session_ids_for_local_guard(ide_sid, tool_sid, present)
        if not keep:
            return {
                "ok": False,
                "error": "认不出本机 Cursor 那条客户端（签发时间没对上唯一一台），未开启保护",
                "keepIds": [],
                "beforeCount": session_count,
                "afterCount": session_count,
                "status": status,
            }

        self._remember_local_session(account_id, ide_sid)
        start = self.device_guard_start(account_id, keep, interval_seconds)
        if not start.get("ok"):
            return {
                "ok": False,
                "error": start.get("error") or "开启保护失败",
                "keepIds": keep,
                "beforeCount": session_count,
                "afterCount": session_count,
                "status": start.get("status") or status,
            }
        return {
            "ok": True,
            "keepIds": keep,
            "beforeCount": session_count,
            "afterCount": session_count,
            "status": start.get("status") or status,
            "error": "",
        }

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

    def _prepare_writable_token(
        self,
        account_id: str,
        refresh_first: bool = True,
        kick_old_tool: bool = False,
        write_target: str = "Cursor",
        exchange_web: bool = True,
    ) -> dict:
        """换票 + 探活 + 网站票兑换。成功后给出可写入客户端的 jwt，不关任何应用。"""
        item = self._store.get(account_id)
        if not item:
            return {"ok": False, "error": "账号不存在"}
        refreshed = False
        used_access = False
        dropped = ""
        kick_error = ""
        if refresh_first:
            if not item.get("refreshToken"):
                probe = self.probe_refresh_one(account_id)
                if not probe.get("ok"):
                    err = str(probe.get("error") or "未能探测到 refresh_token")
                    return {
                        "ok": False,
                        "error": err
                        + f" 未写入 {write_target}。可取消勾选「先刷新登录票」后仅切当前票。",
                    }
                item = self._store.get(account_id) or item
            old_claims: dict = {}
            if item.get("token"):
                try:
                    _ouid, _ojwt, old_claims = parse_token(item["token"])
                except Exception:
                    old_claims = {}
            result = self.refresh_login_one(account_id)
            if not result.get("ok"):
                return result
            refreshed = True
            used_access = bool(result.get("usedAccessAsRefresh"))
            new_access = result.get("accessToken") or ""
            if kick_old_tool:
                dropped = self._drop_stale_tool_session_after_refresh(
                    account_id, old_claims, new_access
                )
                if not dropped:
                    kick_error = (
                        "换票已成功，但没对上要踢的旧客户端（可能官方没新开会话，或对上的是本机 Cursor）"
                    )
            item = self._store.get(account_id) or item
        try:
            user_id, jwt, claims = parse_token(item["token"])
        except Exception as exc:
            return {"ok": False, "error": f"token 解析失败：{exc}"}
        label = item.get("label") or ""
        email = label if "@" in label else (claims.get("email") or user_id)
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
        refresh_jwt = item.get("refreshToken") or None
        exchanged = False
        if exchange_web and str(claims.get("type") or "").lower() == "web":
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
        elif not refresh_jwt:
            refresh_jwt = jwt
        return {
            "ok": True,
            "jwt": jwt,
            "refreshJwt": refresh_jwt,
            "email": email,
            "userId": user_id,
            "refreshed": refreshed,
            "usedAccessAsRefresh": used_access,
            "droppedSessionId": dropped,
            "kickError": kick_error,
            "exchanged": exchanged,
        }

    def _attach_session_block(self, out: dict, account_id: str, *, pin_local: bool) -> dict:
        try:
            listed = self.list_sessions(account_id)
        except Exception as exc:
            action = "已切号" if pin_local else "已登录 Bot"
            out["warning"] = f"{action}，但设备列表未刷新：{exc}"
            return out
        if isinstance(listed, dict):
            for key in (
                "sessions",
                "sessionCount",
                "sessionClientCount",
                "sessionWebCount",
                "sessionError",
            ):
                if key in listed:
                    out[key] = listed[key]
            if listed.get("sessionError"):
                out["warning"] = str(listed.get("sessionError"))
            if pin_local:
                try:
                    out["pinnedSessionId"] = self._resolve_pinned_local_session(
                        account_id, listed.get("sessions") or []
                    ) or ""
                except Exception:
                    out["pinnedSessionId"] = ""
        return out

    def login_bot(
        self,
        account_id: str,
        reset_machine_id: bool = False,
        refresh_first: bool = False,
        kick_old_tool: bool = False,
        login_url: str = "",
    ) -> dict:
        """写入 Grok Bot 自带 Cursor 账户列表并重启 Bot，不关闭、不改写 Cursor。"""
        del reset_machine_id  # 登录 Bot 不改 Cursor 机器码
        del login_url  # 不再走 loginDeepControl
        prep = self._prepare_writable_token(
            account_id,
            refresh_first,
            kick_old_tool,
            write_target="Grok Bot",
            exchange_web=True,
        )
        if not prep.get("ok"):
            return prep
        if grok_bot.find_app() is None:
            return {
                "ok": False,
                "error": grok_bot.missing_app_message(cursor_untouched=True),
            }
        try:
            grok_bot.close_grok_bot()
            grok_bot.write_local_account(
                prep["jwt"],
                prep.get("refreshJwt") or prep["jwt"],
                email=prep.get("email"),
                user_id=prep.get("userId"),
            )
            grok_bot.start_grok_bot()
        except grok_bot.GrokBotError as exc:
            return {"ok": False, "error": str(exc)}
        out = {
            "ok": True,
            "email": prep.get("email"),
            "resetMachineId": False,
            "exchanged": bool(prep.get("exchanged")),
            "refreshed": bool(prep.get("refreshed")),
            "usedAccessAsRefresh": bool(prep.get("usedAccessAsRefresh")),
            "droppedSessionId": prep.get("droppedSessionId") or "",
            "kickError": prep.get("kickError") or "",
            "pinnedSessionId": "",
            "warning": "",
            "client": "grok-bot",
            "nativeSwitch": True,
        }
        return self._attach_session_block(out, account_id, pin_local=False)

    def switch_account(
        self,
        account_id: str,
        reset_machine_id: bool = False,
        refresh_first: bool = True,
        kick_old_tool: bool = False,
        classic: bool = True,
    ) -> dict:
        """一键切号：默认先刷新登录票，再用新票写入本机 Cursor 并重启。"""
        prep = self._prepare_writable_token(
            account_id, refresh_first, kick_old_tool, write_target="Cursor"
        )
        if not prep.get("ok"):
            return prep
        jwt = prep["jwt"]
        email = prep.get("email") or ""
        refresh_jwt = prep.get("refreshJwt")
        user_id = prep.get("userId")
        try:
            layout = sand_patch.resolve_cursor_layout()
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": f"未找到本机 Cursor：{exc}"}
        try:
            sand_patch.close_cursor(layout)
            local_cursor.write_local_account(jwt, email, refresh_token=refresh_jwt, user_id=user_id)
            if reset_machine_id:
                local_cursor.reset_machine_ids()
            sand_patch.start_cursor(layout, classic=classic)
            out = {
                "ok": True,
                "email": email,
                "resetMachineId": bool(reset_machine_id),
                "exchanged": bool(prep.get("exchanged")),
                "refreshed": bool(prep.get("refreshed")),
                "usedAccessAsRefresh": bool(prep.get("usedAccessAsRefresh")),
                "droppedSessionId": prep.get("droppedSessionId") or "",
                "kickError": prep.get("kickError") or "",
                "pinnedSessionId": "",
                "warning": "",
            }
            return self._attach_session_block(out, account_id, pin_local=True)
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

    def app_info(self) -> dict:
        return ops_ui.app_info()

    def set_settings(self, data: dict) -> bool:
        _write_json("settings.json", data or {})
        return True

    def request_quit(self) -> dict:
        """关闭确认框点了「关闭应用」：置位并延迟 destroy。

        不能在这里同步 destroy()：JS 桥还要用 evaluate_js 把本次 RPC 回传给前端。
        也不能指望前端 window.close()：WKWebView 关不掉 NSWindow。
        """
        win = self._window
        quit_confirm.confirm_and_destroy(
            lambda: setattr(self, "_quit_confirmed", True),
            None if win is None else win.destroy,
        )
        return {"ok": True}


def main() -> None:
    resolve.install()
    install_dock_icon()
    install_quiet_local_http()
    api = Api()
    window = webview.create_window(
        "cursor账号管理器",
        resource_path(os.path.join("web", "index.html")),
        js_api=api,
        width=1440,
        height=840,
        min_size=(1180, 680),
        background_color="#EAF2FF",
    )
    api._window = window

    def on_closing():
        action = quit_confirm.closing_action(bool(api._quit_confirmed), bool(api._ui_ready))
        if quit_confirm.cancels_close(action):
            threading.Thread(target=_show_quit_modal, name="quit-confirm", daemon=True).start()
            return False
        return True

    def _show_quit_modal():
        try:
            window.evaluate_js("window.showQuitConfirm()")
        except Exception:
            api._quit_confirmed = True
            try:
                window.destroy()
            except Exception:
                pass

    window.events.closing += on_closing
    webview.start()
    # 窗口关闭后叫停所有保护线程并把「运行中」落成 False：下次打开只回填名单，不自动踢人。
    api._guard.stop_all(wait=True, timeout=3.0)


if __name__ == "__main__":
    main()
