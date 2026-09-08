"""用账号 token 打开一个已登录的浏览器（Chrome/Edge），落到 Sand 领取页或 Cursor 控制台，供手动操作。

原理：WorkosCursorSessionToken 是 HttpOnly cookie，命令行/URL 都带不进普通浏览器；
只能用 CDP（DevTools 协议）：启动带调试端口 + 独立 profile 的浏览器 → Network.setCookie
注入会话 cookie → Page.navigate 到目标页 → 浏览器留给用户手动操作。

每个账号只开一扇隔离窗口。窗口还在时，「进控制台 / 过校验」复用它，不再新开；
设备列表 / 踢下线 / 本机保护也走这扇窗口的页面 fetch，不会每轮检测再拉起浏览器。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import websocket  # websocket-client

import sand_api

CURSOR_ONBOARDING = "https://cursor.com/bot/onboarding?product=grok-bot"
CURSOR_DASHBOARD = "https://cursor.com/dashboard"
# 「进控制台」落点与 cursor-account-manager 插件一致：直接到用量 / 账单页。
CURSOR_DASHBOARD_SPENDING = "https://cursor.com/dashboard/spending"
# 官方 Active Sessions：dashboard → My Settings。人机校验拦截接口时打开这里给用户手动拖动 / 踢设备。
CURSOR_DASHBOARD_SESSIONS = "https://cursor.com/dashboard/settings#active-sessions"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _find_browser_linux():
    """Linux：先按命令名走 PATH，再兜底常见的 /usr/bin、/opt、snap 安装路径。"""
    commands = [
        ("google-chrome", "chrome"),
        ("google-chrome-stable", "chrome"),
        ("chromium", "chrome"),
        ("chromium-browser", "chrome"),
        ("microsoft-edge", "edge"),
        ("microsoft-edge-stable", "edge"),
    ]
    for cmd, name in commands:
        found = shutil.which(cmd)
        if found:
            return found, name
    paths = [
        ("/usr/bin/google-chrome", "chrome"),
        ("/usr/bin/google-chrome-stable", "chrome"),
        ("/opt/google/chrome/chrome", "chrome"),
        ("/usr/bin/chromium", "chrome"),
        ("/usr/bin/chromium-browser", "chrome"),
        ("/snap/bin/chromium", "chrome"),
        ("/usr/lib/chromium/chromium", "chrome"),
        ("/usr/lib/chromium-browser/chromium-browser", "chrome"),
        ("/usr/bin/microsoft-edge", "edge"),
        ("/usr/bin/microsoft-edge-stable", "edge"),
        ("/opt/microsoft/msedge/msedge", "edge"),
    ]
    for path, name in paths:
        if os.path.isfile(path):
            return path, name
    return None, None


def _find_browser():
    """返回 (exe 路径, 'chrome'|'edge')。优先 Chrome，回退 Edge。跨 Windows / macOS / Linux。"""
    if sys.platform == "darwin":
        macs = [
            ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "chrome"),
            (os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), "chrome"),
            ("/Applications/Chromium.app/Contents/MacOS/Chromium", "chrome"),
            ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "edge"),
        ]
        for path, name in macs:
            if path and os.path.isfile(path):
                return path, name
        return None, None
    if sys.platform.startswith("linux"):
        return _find_browser_linux()
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    chrome = [
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pfx, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe") if local else "",
    ]
    edge = [
        os.path.join(pfx, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for path in chrome:
        if path and os.path.isfile(path):
            return path, "chrome"
    for path in edge:
        if path and os.path.isfile(path):
            return path, "edge"
    return None, None


def _profile_dir(user_id: str) -> str:
    """按 user_id 隔离 profile：不同账号各自会话，不污染用户日常浏览器。"""
    safe = re.sub(r"[^A-Za-z0-9_]", "_", user_id) or "default"
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = os.path.join(base, "SandClaimer", "browser-profiles", safe)
    os.makedirs(path, exist_ok=True)
    return path


def _http_json(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_page_target(port: int, timeout: float = 15.0):
    """等浏览器 CDP 就绪并返回一个 page target 的 webSocketDebuggerUrl。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            targets = _http_json(port, "/json")
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.25)
    return None


class _CDP:
    def __init__(self, ws_url: str, timeout: float = 20.0):
        self._ws = websocket.create_connection(ws_url, timeout=timeout)
        self._id = 0
        self._timeout = timeout

    def call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        self._id += 1
        mid = self._id
        payload = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self._ws.send(json.dumps(payload))
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == mid:
                return msg
        raise TimeoutError(f"CDP 无响应：{method}")

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


_LIVE_LOCK = threading.Lock()
_LIVE: dict = {}
_CDP_LOCK = threading.Lock()


class _BrowserGone(RuntimeError):
    pass


class _NoCursorPage(RuntimeError):
    pass


def _meta_path(profile: str) -> str:
    return os.path.join(profile, "sandclaimer-debug.json")


def _remember(user_id: str, port: int, name: str) -> None:
    info = {"port": int(port), "name": name or "chrome"}
    with _LIVE_LOCK:
        _LIVE[user_id] = info
    try:
        with open(_meta_path(_profile_dir(user_id)), "w", encoding="utf-8") as handle:
            json.dump(info, handle)
    except Exception:
        pass


def _forget(user_id: str) -> None:
    with _LIVE_LOCK:
        _LIVE.pop(user_id, None)


def _debug_alive(port: int) -> bool:
    try:
        _http_json(int(port), "/json/version")
        return True
    except Exception:
        return False


def _browser_ws(port: int) -> str:
    try:
        version = _http_json(int(port), "/json/version")
    except Exception as exc:
        raise _BrowserGone(f"隔离浏览器调试端口不可用：{exc}") from exc
    ws = (version or {}).get("webSocketDebuggerUrl") if isinstance(version, dict) else ""
    if not ws:
        raise _BrowserGone("隔离浏览器没有 browser target")
    return str(ws)


def _read_devtools_port(profile: str) -> int | None:
    path = os.path.join(profile, "DevToolsActivePort")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            line = (handle.readline() or "").strip()
        port = int(line)
        return port if port > 0 else None
    except Exception:
        return None


def _profile_chrome_pid(profile: str) -> int | None:
    """Chrome 把 SingletonLock 链到 `hostname-pid`。进程还在说明这个 profile 已被占用。"""
    lock = os.path.join(profile, "SingletonLock")
    try:
        target = os.readlink(lock)
    except OSError:
        return None
    pid_s = str(target).rsplit("-", 1)[-1].strip()
    try:
        pid = int(pid_s)
    except ValueError:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _pid_command(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            timeout=3,
        ).strip()
    except Exception:
        return ""


def _port_from_command(command: str) -> int | None:
    match = re.search(r"--remote-debugging-port=(\d+)", command or "")
    if not match:
        return None
    port = int(match.group(1))
    return port if port > 0 else None


def _discover_profile_session(user_id: str) -> dict | None:
    """应用重启后内存/json 里的调试口会丢；从仍占用 profile 的 Chrome 进程找回。"""
    profile = _profile_dir(user_id)
    pid = _profile_chrome_pid(profile)
    port = _port_from_command(_pid_command(pid)) if pid else None
    if not port:
        port = _read_devtools_port(profile)
    if port and _debug_alive(port):
        return {"port": int(port), "name": "chrome"}
    return None


def _read_saved_meta(user_id: str) -> dict | None:
    try:
        with open(_meta_path(_profile_dir(user_id)), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        data = None
    if isinstance(data, dict) and data.get("port"):
        return {"port": int(data["port"]), "name": data.get("name") or "chrome"}
    port = _read_devtools_port(_profile_dir(user_id))
    if port:
        return {"port": port, "name": "chrome"}
    return None


def session_for(user_id: str) -> dict | None:
    """该账号隔离浏览器是否还活着。活着则返回 {port, name}。"""
    user_id = str(user_id or "").strip()
    if not user_id:
        return None
    with _LIVE_LOCK:
        info = dict(_LIVE.get(user_id) or {})
    if info.get("port") and _debug_alive(info["port"]):
        return info
    saved = _read_saved_meta(user_id)
    if saved and _debug_alive(saved["port"]):
        _remember(user_id, saved["port"], saved.get("name") or "chrome")
        return {"port": saved["port"], "name": saved.get("name") or "chrome"}
    discovered = _discover_profile_session(user_id)
    if discovered:
        _remember(user_id, discovered["port"], discovered.get("name") or "chrome")
        return discovered
    if info:
        _forget(user_id)
    return None


def has_live_browser(user_id: str) -> bool:
    return session_for(user_id) is not None


def _page_targets(port: int) -> list:
    targets = _http_json(int(port), "/json")
    if not isinstance(targets, list):
        return []
    return [
        t
        for t in targets
        if isinstance(t, dict) and t.get("type") == "page" and t.get("webSocketDebuggerUrl")
    ]


def _is_blank_url(url: str | None) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    base = raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return base in ("about:blank", "chrome://newtab", "chrome://new-tab-page")


def _cursor_pages(pages: list) -> list:
    return [t for t in pages if str(t.get("url") or "").startswith("https://cursor.com")]


def _blank_pages(pages: list) -> list:
    return [t for t in pages if _is_blank_url(t.get("url"))]


def _open_plan(pages: list) -> tuple:
    """进控制台该动哪一页。返回 ('navigate', page) 或 ('create', None)。

    崩溃恢复会把数据工作台等旧标签加回来，命令行再塞一个 about:blank 并聚焦它。
    不能拿 /json 里第一项乱跳，否则控制台开在后台、眼前只剩空白页。
    Chrome 152 的 /json 还可能是空的，这时只能新建标签。
    """
    if not pages:
        return "create", None
    cursor = _cursor_pages(pages)
    if cursor:
        return "navigate", cursor[0]
    blanks = _blank_pages(pages)
    if blanks:
        return "navigate", blanks[-1]
    return "create", None


def _pick_page_ws(port: int, require_cursor: bool = False):
    pages = _page_targets(port)
    if not pages:
        raise _BrowserGone("隔离浏览器没有可连接的页面")
    cursor = _cursor_pages(pages)
    if require_cursor and not cursor:
        raise _NoCursorPage("官方页已关掉")
    chosen = (cursor or pages)[0]
    return chosen["webSocketDebuggerUrl"], str(chosen.get("url") or "")


def _cdp_result(msg: dict, action: str) -> dict:
    if msg.get("error"):
        err = msg["error"]
        text = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(f"{action}失败：{text or '未知错误'}")
    return msg.get("result") or {}


def _connect_page(ws_url: str) -> _CDP:
    try:
        return _CDP(ws_url)
    except Exception as exc:
        raise _BrowserGone(f"隔离浏览器无法连接：{exc}") from exc


def _cookie_param(user_id: str, jwt: str) -> dict:
    return {
        "name": "WorkosCursorSessionToken",
        "value": f"{user_id}%3A%3A{jwt}",
        "domain": ".cursor.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    }


def _page_infos(cdp: _CDP) -> list:
    try:
        cdp.call("Target.setDiscoverTargets", {"discover": True})
    except Exception:
        pass
    infos = _cdp_result(cdp.call("Target.getTargets"), "读取标签").get("targetInfos") or []
    pages = []
    for item in infos:
        if not isinstance(item, dict) or item.get("type") != "page" or not item.get("targetId"):
            continue
        pages.append({"url": item.get("url") or "", "id": item.get("targetId")})
    return pages


def _inject_cookie(cdp: _CDP, user_id: str, jwt: str, session_id: str | None = None) -> None:
    _cdp_result(cdp.call("Network.enable", session_id=session_id), "启用网络")
    res = _cdp_result(
        cdp.call("Network.setCookie", _cookie_param(user_id, jwt), session_id=session_id),
        "注入 cookie",
    )
    if res.get("success") is False:
        raise RuntimeError("注入 cookie 失败")


def _set_cookies(cdp: _CDP, user_id: str, jwt: str) -> None:
    try:
        _cdp_result(cdp.call("Storage.setCookies", {"cookies": [_cookie_param(user_id, jwt)]}), "注入 cookie")
        return
    except Exception:
        pass
    pages = _page_infos(cdp)
    if not pages:
        return
    attached = _cdp_result(
        cdp.call("Target.attachToTarget", {"targetId": pages[0]["id"], "flatten": True}),
        "连接标签",
    )
    _inject_cookie(cdp, user_id, jwt, attached.get("sessionId"))


def _activate(cdp: _CDP, target_id: str, session_id: str | None = None) -> None:
    try:
        cdp.call("Target.activateTarget", {"targetId": target_id})
    except Exception:
        pass
    if session_id:
        try:
            cdp.call("Page.bringToFront", session_id=session_id)
        except Exception:
            pass


def _create_tab(cdp: _CDP, url: str) -> str | None:
    created = _cdp_result(cdp.call("Target.createTarget", {"url": url}), "打开页面")
    tid = created.get("targetId")
    if tid:
        _activate(cdp, tid)
    return tid


def _navigate_attached(cdp: _CDP, target_id: str, user_id: str, jwt: str, url: str | None) -> None:
    attached = _cdp_result(
        cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True}),
        "连接标签",
    )
    sid = attached.get("sessionId")
    try:
        _inject_cookie(cdp, user_id, jwt, sid)
    except Exception:
        pass
    if url:
        _cdp_result(cdp.call("Page.enable", session_id=sid), "启用页面")
        _cdp_result(cdp.call("Page.navigate", {"url": url}, session_id=sid), "打开页面")
    _activate(cdp, target_id, sid)


def _wait_url_prefix(port: int, prefix: str, timeout: float = 8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cdp = _connect_page(_browser_ws(port))
            try:
                for page in _page_infos(cdp):
                    if str(page.get("url") or "").startswith(prefix):
                        return page
            finally:
                cdp.close()
        except Exception:
            pass
        time.sleep(0.25)
    return None


def _close_extra_blank_tabs(cdp: _CDP) -> None:
    pages = _page_infos(cdp)
    if not _cursor_pages(pages):
        return
    for page in _blank_pages(pages):
        try:
            cdp.call("Target.closeTarget", {"targetId": page["id"]})
        except Exception:
            pass


def _mark_profile_clean_exit(profile: str) -> None:
    prefs = os.path.join(profile, "Default", "Preferences")
    try:
        with open(prefs, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return
        profile_prefs = data.get("profile")
        if not isinstance(profile_prefs, dict) or profile_prefs.get("exit_type") != "Crashed":
            return
        profile_prefs["exit_type"] = "Normal"
        with open(prefs, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
    except Exception:
        pass


def _eval_value(msg: dict):
    if msg.get("error"):
        err = msg["error"]
        text = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(text or "未知错误")
    payload = msg.get("result") or {}
    if payload.get("exceptionDetails"):
        detail = payload["exceptionDetails"]
        exc = detail.get("exception") if isinstance(detail, dict) else {}
        text = ""
        if isinstance(exc, dict):
            text = exc.get("description") or exc.get("value") or ""
        raise RuntimeError(text or (detail.get("text") if isinstance(detail, dict) else "") or "页面脚本失败")
    return (payload.get("result") or {}).get("value")


def _prepare_page(port: int, user_id: str, jwt: str, url: str | None = None, bring_to_front: bool = False) -> None:
    cdp = _connect_page(_browser_ws(port))
    try:
        _set_cookies(cdp, user_id, jwt)
        pages = _page_infos(cdp)
        action, page = _open_plan(pages)
        if action == "navigate" and page:
            _navigate_attached(cdp, page["id"], user_id, jwt, url)
        elif url:
            _create_tab(cdp, url)
        elif pages and bring_to_front:
            _activate(cdp, pages[0]["id"])
        else:
            raise _BrowserGone("隔离浏览器没有可连接的页面")
        if url:
            opened = None
            deadline = time.time() + 8.0
            while time.time() < deadline:
                for item in _page_infos(cdp):
                    if str(item.get("url") or "").startswith("https://cursor.com"):
                        opened = item
                        break
                if opened:
                    break
                time.sleep(0.25)
            if not opened:
                _create_tab(cdp, url)
            _close_extra_blank_tabs(cdp)
    finally:
        cdp.close()
    if url and not _wait_url_prefix(port, "https://cursor.com", timeout=3.0):
        raise RuntimeError("浏览器已打开，但没有跳到 Cursor 页面")


def _browser_fetch(port: int, user_id: str, jwt: str, method: str, url: str, body: str | None = None) -> tuple:
    """在已打开的 cursor.com 页里 fetch。返回 (status, text)。"""
    method = "POST" if str(method or "").upper() == "POST" else "GET"
    payload = json.dumps(body if body is not None else "")
    script = (
        """(async () => {
  const r = await fetch(%s, {
    method: %s,
    credentials: "include",
    headers: {
      accept: "application/json",
      origin: "https://cursor.com",
      referer: "https://cursor.com/dashboard"%s
    }%s
  });
  return {status: r.status, text: (await r.text()).slice(0, 200000)};
})()"""
        % (
            json.dumps(url),
            json.dumps(method),
            ', "content-type": "application/json"' if method == "POST" else "",
            (", body: " + payload) if method == "POST" else "",
        )
    )
    cdp = _connect_page(_browser_ws(port))
    try:
        pages = _page_infos(cdp)
        cursor = _cursor_pages(pages)
        if not cursor:
            raise _NoCursorPage("官方页已关掉")
        attached = _cdp_result(
            cdp.call("Target.attachToTarget", {"targetId": cursor[0]["id"], "flatten": True}),
            "连接标签",
        )
        sid = attached.get("sessionId")
        _inject_cookie(cdp, user_id, jwt, sid)
        _cdp_result(cdp.call("Runtime.enable", session_id=sid), "启用脚本")
        msg = cdp.call(
            "Runtime.evaluate",
            {"expression": script, "awaitPromise": True, "returnByValue": True},
            session_id=sid,
        )
        value = _eval_value(msg)
    finally:
        cdp.close()
    if not isinstance(value, dict):
        raise RuntimeError("浏览器返回无法解析")
    try:
        status = int(value.get("status") or 0)
    except Exception:
        status = 0
    return status, str(value.get("text") or "")


def _with_cdp(user_id: str, fn):
    info = session_for(user_id)
    if not info:
        return None
    with _CDP_LOCK:
        try:
            return fn(info)
        except _BrowserGone:
            _forget(user_id)
            return None


def fetch_sessions_if_open(user_id: str, jwt: str):
    """窗口还在则从该浏览器读设备列表；没开过窗口返回 None（调用方走普通接口）。"""

    def run(info):
        try:
            status, text = _browser_fetch(
                info["port"], user_id, jwt, "GET", sand_api.SESSIONS_URL
            )
        except _NoCursorPage:
            return sand_api.empty_session_block(
                "隔离浏览器还在，但官方页已关掉。请再点一次「浏览器打开」，之后不要关 cursor.com 标签。"
            )
        if status == 0:
            return sand_api.empty_session_block("从已打开的浏览器读取无响应")
        if sand_api.is_waf_block(status, text):
            block = sand_api.empty_session_block(sand_api.WAF_SESSIONS_ERROR, waf=True)
        elif status != 200:
            block = sand_api.empty_session_block(f"会话接口 HTTP {status}")
        else:
            try:
                block = sand_api.normalize_sessions(json.loads(text))
            except Exception:
                block = sand_api.empty_session_block("会话数据无法解析")
        block["sessionVia"] = "browser"
        return block

    return _with_cdp(user_id, run)


def revoke_session_if_open(user_id: str, jwt: str, session_id: str, session_type=None):
    """窗口还在则在该浏览器里提交踢下线；没开过窗口返回 None。"""
    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": False, "error": "sessionId 为空", "status": 0}
    if not session_for(user_id):
        return None
    type_value = sand_api.revoke_type_value(session_type)
    if type_value is None:
        return {"ok": False, "error": "无法确定设备类型，未提交踢下线", "status": 0}

    def run(info):
        try:
            status, text = _browser_fetch(
                info["port"],
                user_id,
                jwt,
                "POST",
                sand_api.SESSIONS_REVOKE_URL,
                json.dumps({"session_id": sid, "type": type_value}),
            )
        except _NoCursorPage:
            return {
                "ok": False,
                "error": "隔离浏览器还在，但官方页已关掉。请再点一次「浏览器打开」。",
                "status": 0,
            }
        result = sand_api._revoke_http_result(status, text)
        if result.get("ok"):
            result = dict(result)
            result["sessionVia"] = "browser"
        return result

    return _with_cdp(user_id, run)


def fetch_sessions_smart(user_id: str, jwt: str) -> dict:
    """优先读已打开的隔离浏览器；没有窗口才走普通 HTTP。检测不会因此新开窗口。"""
    block = fetch_sessions_if_open(user_id, jwt)
    if block is not None:
        return block
    return sand_api.fetch_sessions(user_id, jwt)


def revoke_session_smart(user_id: str, jwt: str, session_id: str, session_type=None) -> dict:
    sid = str(session_id or "").strip()
    type_value = sand_api.revoke_type_value(session_type)
    if type_value is None:
        block = fetch_sessions_smart(user_id, jwt)
        for row in block.get("sessions") or []:
            if str(row.get("sessionId") or "") == sid:
                type_value = sand_api.revoke_type_value(row.get("typeRaw") or row.get("type"))
                break
    if type_value is None:
        return {"ok": False, "error": "无法确定设备类型，未提交踢下线", "status": 0}
    res = revoke_session_if_open(user_id, jwt, sid, type_value)
    if res is not None:
        return res
    return sand_api.revoke_session(user_id, jwt, sid, type_value)


def open_with_token(user_id: str, jwt: str, url: str = CURSOR_ONBOARDING) -> dict:
    """打开或复用该账号的隔离浏览器。返回 {name, reused}。抛异常表示失败。"""
    existing = session_for(user_id)
    if existing:
        try:
            with _CDP_LOCK:
                _prepare_page(existing["port"], user_id, jwt, url=url, bring_to_front=True)
            return {"name": existing.get("name") or "chrome", "reused": True}
        except _BrowserGone:
            _forget(user_id)

    exe, name = _find_browser()
    if not exe:
        raise RuntimeError("未找到 Chrome / Chromium / Edge 浏览器")

    profile = _profile_dir(user_id)
    if _profile_chrome_pid(profile):
        discovered = _discover_profile_session(user_id)
        if discovered:
            with _CDP_LOCK:
                _prepare_page(discovered["port"], user_id, jwt, url=url, bring_to_front=True)
            _remember(user_id, discovered["port"], discovered.get("name") or name)
            return {"name": discovered.get("name") or name, "reused": True}
        raise RuntimeError("该账号的隔离 Chrome 已在运行，但连不上调试端口。请先完全退出那扇 Chrome，再点「进控制台」。")

    port = _free_port()
    _mark_profile_clean_exit(profile)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-bubble",
        "--new-window",
        "about:blank",
    ]
    popen_kwargs = dict(
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True  # 父进程退出不带走浏览器
    subprocess.Popen(args, **popen_kwargs)

    ws_url = _wait_page_target(port)
    if not ws_url:
        discovered = _discover_profile_session(user_id)
        if discovered:
            with _CDP_LOCK:
                _prepare_page(discovered["port"], user_id, jwt, url=url, bring_to_front=True)
            _remember(user_id, discovered["port"], discovered.get("name") or name)
            return {"name": discovered.get("name") or name, "reused": True}
        raise RuntimeError("浏览器调试端口未就绪（可能被安全软件拦截）")

    with _CDP_LOCK:
        _prepare_page(port, user_id, jwt, url=url, bring_to_front=False)
    _remember(user_id, port, name)
    return {"name": name, "reused": False}
