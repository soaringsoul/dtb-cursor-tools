"""Browser preview of the SandClaimer UI with a mock pywebview bridge.

The real app is `python3 app.py` (pywebview). This server only exists so the
glass UI — including 进控制台 / 查看设备 / 本机保护 — can be clicked in a
normal browser without a desktop WebView.

  python3 preview_server.py --port 43147
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import accounts
import api_key_store
import login_detect
import sand_api

WEB = Path(__file__).resolve().parent / "web"

DEMO_ID = "user_01ANITAREID331200000000000"
DEMO_EMAIL = "anitareid3312@outlook.com"
DEMO_ID_2 = "user_01BATCHGUARD2200000000000"
DEMO_EMAIL_2 = "batchguard2200@outlook.com"

_LOCK = threading.RLock()
_PREVIEW_SETTINGS = {"hideHelp": True, "autoVerify": True}
_ACCOUNT_TAGS = {}
_API_KEYS = []
_API_KEY_SECRETS = {}
_LAST_API_KEY_INPUT = ""
_AGENTS_BY_KEY = {}

_SESSIONS_SEED = {
    DEMO_ID: [
        {
            "sessionId": "1c3c233a6194e5eb6463e48122d093c0537507a385c6475dc1485884c511f2a6",
            "type": "client",
            "typeRaw": "SESSION_TYPE_CLIENT",
            "createdAt": "2026-08-20T04:12:00.000Z",
            "expiresAt": "2026-10-19T04:12:00.000Z",
        },
        {
            "sessionId": "dddd4444dddd4444dddd4444dddd4444dddd4444dddd4444dddd4444dddd4444",
            "type": "client",
            "typeRaw": "SESSION_TYPE_CLIENT",
            "createdAt": "2026-09-01T08:00:00.000Z",
            "expiresAt": "2026-10-31T08:00:00.000Z",
        },
        {
            "sessionId": "9c5904911dd4a73f2883bd6a6f391e4d1d25422e653ede8c46613af700838eed",
            "type": "web",
            "typeRaw": "SESSION_TYPE_WEB",
            "createdAt": "2026-09-01T11:08:00.000Z",
            "expiresAt": "2026-09-08T11:08:00.000Z",
        },
        {
            "sessionId": "aa11bb22cc33dd44ee55ff6677889900aabbccddeeff00112233445566778899",
            "type": "other",
            "typeRaw": "SESSION_TYPE_MOBILE",
            "createdAt": "2026-09-04T18:40:00.000Z",
            "expiresAt": "2026-10-04T18:40:00.000Z",
        },
    ],
    DEMO_ID_2: [
        {
            "sessionId": "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222",
            "type": "client",
            "typeRaw": "SESSION_TYPE_CLIENT",
            "createdAt": "2026-08-22T04:12:00.000Z",
            "expiresAt": "2026-10-21T04:12:00.000Z",
        },
        {
            "sessionId": "cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333",
            "type": "web",
            "typeRaw": "SESSION_TYPE_WEB",
            "createdAt": "2026-09-02T11:08:00.000Z",
            "expiresAt": "2026-09-09T11:08:00.000Z",
        },
    ],
}


def _stamp_recent_logins(rows, aid):
    """预览：客户端保持各自创建时间；网页/其它设备标成刚登录。"""
    if aid != DEMO_ID:
        return
    now = time.time()
    offsets = [30, 50]
    i = 0
    for row in rows:
        if row.get("type") == "client" or row.get("typeRaw") == "SESSION_TYPE_CLIENT":
            continue
        if i >= len(offsets):
            break
        row["createdAt"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - offsets[i])
        )
        i += 1


def _copy_sessions(aid):
    rows = [dict(s) for s in _SESSIONS_SEED.get(aid, [])]
    _stamp_recent_logins(rows, aid)
    return rows


def _empty_guard(n=0):
    return {
        "running": False,
        "keepIds": [],
        "startedAt": None,
        "lastTickAt": None,
        "tickCount": 0,
        "kickedCount": 0,
        "lastKicked": [],
        "lastError": "",
        "sessionCount": n,
        "stopReason": "",
        "intervalSeconds": 30,
    }


_SESSIONS = {aid: _copy_sessions(aid) for aid in _SESSIONS_SEED}
_GUARDS = {aid: _empty_guard(len(rows)) for aid, rows in _SESSIONS.items()}
_STOP = threading.Event()


def _aid(args, idx=0):
    if args and len(args) > idx and args[idx]:
        return str(args[idx])
    return DEMO_ID


def _email_of(aid):
    if aid == DEMO_ID_2:
        return DEMO_EMAIL_2
    return DEMO_EMAIL


def _sessions_of(aid):
    return _SESSIONS.setdefault(aid, [])


def _guard_of(aid):
    return _GUARDS.setdefault(aid, _empty_guard())


def _guard_loop():
    while not _STOP.wait(1.0):
        with _LOCK:
            for aid, g in list(_GUARDS.items()):
                if not g["running"]:
                    continue
                keep = set(g["keepIds"])
                g["tickCount"] = int(g["tickCount"] or 0) + 1
                g["lastTickAt"] = time.time()
                rows = list(_SESSIONS.get(aid, []))
                victims = [s for s in rows if s["sessionId"] not in keep]
                remain = [s for s in rows if s["sessionId"] in keep]
                _SESSIONS[aid] = remain
                for row in victims:
                    g["kickedCount"] = int(g["kickedCount"] or 0) + 1
                    kicked = {
                        "sessionId": row["sessionId"],
                        "type": row.get("type") or "other",
                        "createdAt": row.get("createdAt"),
                        "at": time.time(),
                    }
                    g["lastKicked"] = [kicked] + list(g["lastKicked"] or [])[:7]


def _preview_local_session_id(rows):
    for row in rows or []:
        if row.get("type") == "client" or row.get("typeRaw") == "SESSION_TYPE_CLIENT":
            return str(row.get("sessionId") or "")
    return ""


def _preview_tool_session_id(rows):
    """预览共号：第二条客户端当作本工具自己的会话。"""
    seen = 0
    for row in rows or []:
        if row.get("type") == "client" or row.get("typeRaw") == "SESSION_TYPE_CLIENT":
            seen += 1
            if seen == 2:
                return str(row.get("sessionId") or "")
    return ""


def _session_block(aid=DEMO_ID):
    rows = [dict(s) for s in _sessions_of(aid)]
    _stamp_recent_logins(rows, aid)
    pinned = _preview_local_session_id(rows) if aid == DEMO_ID else ""
    tool = _preview_tool_session_id(rows) if aid == DEMO_ID else ""
    sessions = login_detect.sort_sessions_for_display(
        sand_api.annotate_local_sessions(
            rows,
            aid == DEMO_ID,
            local_session_id=pinned or None,
            local_host="preview-mac",
            tool_session_id=tool or None,
        )
    )
    return {
        "ok": True,
        "error": "",
        "email": _email_of(aid),
        "tokenType": "session",
        "sessions": sessions,
        "sessionCount": len(sessions),
        "sessionClientCount": sum(1 for s in sessions if s["type"] == "client"),
        "sessionWebCount": sum(1 for s in sessions if s["type"] == "web"),
        "sessionError": "",
        "sessionWaf": False,
    }


def _guard_status():
    with _LOCK:
        out = {}
        for aid, g in _GUARDS.items():
            snap = dict(g)
            snap["keepIds"] = list(g["keepIds"])
            snap["lastKicked"] = list(g["lastKicked"])
            snap["sessionCount"] = len(_SESSIONS.get(aid, []))
            out[aid] = snap
        return out


def _bump_local_client_created_at(aid):
    """换票后把该号第一条客户端会话 createdAt 改成现在（若已是现在则再加 1ms）。"""
    rows = _sessions_of(aid)
    now_ms = int(time.time() * 1000)
    for row in rows:
        if row.get("type") != "client" and row.get("typeRaw") != "SESSION_TYPE_CLIENT":
            continue
        prev = login_detect.created_at_ms(row.get("createdAt"))
        if prev is not None and now_ms <= prev:
            now_ms = prev + 1
        row["createdAt"] = login_detect.ms_to_iso(now_ms)
        return str(row.get("sessionId") or "")
    return ""


def _start_guard(aid, keep, seconds):
    g = _guard_of(aid)
    g.update(
        {
            "running": True,
            "keepIds": keep,
            "startedAt": time.time(),
            "lastTickAt": time.time(),
            "tickCount": 1,
            "kickedCount": 0,
            "lastKicked": [],
            "lastError": "",
            "stopReason": "",
            "intervalSeconds": seconds,
        }
    )
    return {"ok": True, "skipped": False, "keepCount": len(keep), "status": _guard_status()[aid]}


def _demo_agents():
    return [
        {
            "id": "bc-3bd13efc-296d-475a-b971-01711141ae57",
            "status": "ACTIVE",
            "createdAt": "2026-08-14T16:53:57.000Z",
            "name": "用户中心迭代",
        },
        {
            "id": "bc-61e83e7e-4936-a36b-275245826fb0",
            "status": "ARCHIVED",
            "createdAt": "2026-08-15T07:32:54.000Z",
            "name": "个人项目可见性",
        },
    ]


def _public_keys():
    with _LOCK:
        return [dict(x) for x in _API_KEYS]


def _draft_api_key_input():
    with _LOCK:
        if _LAST_API_KEY_INPUT:
            return _LAST_API_KEY_INPUT
        if not _API_KEYS:
            return ""
        last = _API_KEYS[-1]
        return _API_KEY_SECRETS.get(last["id"], "")


def _rpc(method: str, args):
    global _LAST_API_KEY_INPUT
    if method == "list_accounts":
        now = int(time.time() * 1000)
        return [
            {
                "id": DEMO_ID,
                "label": DEMO_EMAIL,
                "tokenType": "session",
                "addedAt": now - 86400000,
                "hasRefresh": True,
                "refreshRecordedAt": int(time.time()) - 3600,
                "hasClientId": True,
                "tagIds": list(_ACCOUNT_TAGS.get(DEMO_ID) or []),
            },
            {
                "id": DEMO_ID_2,
                "label": DEMO_EMAIL_2,
                "tokenType": "session",
                "addedAt": now - 43200000,
                "hasRefresh": True,
                "refreshRecordedAt": int(time.time()) - 1800,
                "hasClientId": True,
                "tagIds": list(_ACCOUNT_TAGS.get(DEMO_ID_2) or []),
            },
        ]
    if method == "load_status":
        out = {}
        for aid, membership, pct, just_reset in (
            (DEMO_ID, "business", (62.8, 18.2, 100), True),
            (DEMO_ID_2, "pro", (11.0, 4.0, 8.5), False),
        ):
            block = _session_block(aid)
            out[aid] = {
                "kind": "alive",
                "alive": True,
                "membership": membership,
                "percent": pct[0],
                "autoPercent": pct[1],
                "apiPercent": pct[2],
                "periodStart": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(time.time() - (3600 if just_reset else 4 * 86400)),
                ),
                "nextReset": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5 * 86400)),
                "cycleEnd": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 26 * 86400)),
                "sandGranted": True,
                "sandSource": "plan",
                "checkedAt": int(time.time() * 1000),
                "sessions": block["sessions"],
                "sessionCount": block["sessionCount"],
                "sessionClientCount": block["sessionClientCount"],
                "sessionWebCount": block["sessionWebCount"],
                "sessionError": "",
            }
        return out
    if method == "get_settings":
        with _LOCK:
            return dict(_PREVIEW_SETTINGS)
    if method == "set_settings":
        data = args[0] if args else {}
        if isinstance(data, dict):
            with _LOCK:
                _PREVIEW_SETTINGS.clear()
                _PREVIEW_SETTINGS.update(data)
        return {"ok": True}
    if method == "set_account_tags":
        aid = str(args[0] if args else "")
        raw = args[1] if len(args or []) > 1 else []
        with _LOCK:
            catalog = _PREVIEW_SETTINGS.get("tags")
            known = aid in (DEMO_ID, DEMO_ID_2)
        clean = accounts.filter_tag_ids(raw, catalog)
        if not known:
            return {"ok": False, "tagIds": [], "accounts": _rpc("list_accounts", [])}
        with _LOCK:
            _ACCOUNT_TAGS[aid] = clean
        return {"ok": True, "tagIds": clean, "accounts": _rpc("list_accounts", [])}
    if method == "app_info":
        import ops_ui
        return ops_ui.app_info()
    if method == "mark_ui_ready":
        return True
    if method == "request_quit":
        return {"ok": True}
    if method == "local_identity":
        return {"ok": True, "userId": DEMO_ID, "email": DEMO_EMAIL}
    if method == "list_sessions":
        return _session_block(_aid(args))
    if method == "revoke_session":
        aid = _aid(args)
        sid = str((args or [None, ""])[1] if len(args or []) > 1 else "")
        with _LOCK:
            rows = _sessions_of(aid)
            before = len(rows)
            _SESSIONS[aid] = [s for s in rows if s["sessionId"] != sid]
            ok = len(_SESSIONS[aid]) < before
        return {"ok": ok, "error": "" if ok else "设备不在列表里", "status": 200 if ok else 404}
    if method == "revoke_sessions":
        aid = _aid(args)
        items = args[1] if len(args or []) > 1 else []
        cleaned = sand_api.clean_revoke_items(items)
        kicked = []
        failed = []
        with _LOCK:
            rows = list(_sessions_of(aid))
            for t in cleaned:
                sid = t["session_id"]
                nxt = [s for s in rows if s["sessionId"] != sid]
                if len(nxt) < len(rows):
                    kicked.append({"sessionId": sid, "ok": True, "error": "", "status": 200})
                    rows = nxt
                else:
                    failed.append({"sessionId": sid, "ok": False, "error": "设备不在列表里", "status": 404})
            _SESSIONS[aid] = rows
        return {
            "ok": not failed,
            "error": failed[0]["error"] if failed else "",
            "kicked": kicked,
            "failed": failed,
            "kickedCount": len(kicked),
            "failedCount": len(failed),
            "waf": False,
        }
    if method == "open_dashboard":
        return {"ok": True, "browser": "preview", "url": "https://cursor.com/dashboard/spending", "reused": False}
    if method == "open_sessions_page":
        return {"ok": True, "browser": "preview", "url": "https://cursor.com/dashboard/settings#active-sessions", "reused": False}
    if method == "open_login":
        return {"ok": True, "browser": "preview"}
    if method == "switch_account" or method == "login_bot":
        aid = _aid(args)
        reset = bool(args[1]) if len(args or []) > 1 else False
        refresh_first = True if len(args or []) < 3 else bool(args[2])
        kick_old = bool(args[3]) if len(args or []) > 3 else False
        dropped = ""
        refreshed = False
        ident = _rpc("local_identity", [])
        with _LOCK:
            if refresh_first:
                if kick_old:
                    rows = list(_sessions_of(aid))
                    tool = _preview_tool_session_id(rows) if aid == DEMO_ID else ""
                    if tool and not (
                        ident.get("ok") and ident.get("userId") == aid and tool == _preview_local_session_id(rows)
                    ):
                        _SESSIONS[aid] = [s for s in rows if s.get("sessionId") != tool]
                        dropped = tool
                _bump_local_client_created_at(aid)
                refreshed = True
            block = _session_block(aid)
        return {
            **block,
            "ok": True,
            "email": _email_of(aid),
            "resetMachineId": reset,
            "exchanged": False,
            "warning": block.get("sessionError") or "",
            "refreshed": refreshed,
            "droppedSessionId": dropped,
        }
    if method == "device_guard_status":
        return _guard_status()
    if method == "device_guard_start":
        aid = _aid(args)
        keep = list((args or [None, []])[1] if len(args or []) > 1 else [])
        keep = [str(x) for x in keep if str(x).strip()]
        if not keep:
            return {"ok": False, "error": "保留名单为空：至少勾选一台要保留的设备"}
        raw_iv = args[2] if len(args or []) > 2 else 30
        try:
            seconds = int(raw_iv)
        except (TypeError, ValueError):
            seconds = 30
        seconds = max(5, min(3600, seconds))
        with _LOCK:
            return _start_guard(aid, keep, seconds)
    if method == "device_guard_start_auto":
        aid = _aid(args)
        raw_iv = args[1] if len(args or []) > 1 else 30
        try:
            seconds = int(raw_iv)
        except (TypeError, ValueError):
            seconds = 30
        seconds = max(5, min(3600, seconds))
        with _LOCK:
            g = _guard_of(aid)
            if g.get("running"):
                return {"ok": True, "skipped": True, "reason": "already", "status": dict(g)}
            keep = sand_api.pick_keep_session_ids(
                list(_sessions_of(aid)), g.get("keepIds") or [], aid == DEMO_ID
            )
            if not keep:
                return {"ok": False, "error": "当前没有登录设备，无法开启保护"}
            return _start_guard(aid, keep, seconds)
    if method == "device_guard_stop":
        aid = _aid(args)
        with _LOCK:
            g = _guard_of(aid)
            was = bool(g.get("running"))
            g["running"] = False
            g["stopReason"] = "user"
            return {"ok": True, "running": False, "wasRunning": was, "status": dict(g)}
    if method == "device_guard_stop_all":
        stopped = []
        with _LOCK:
            for aid, g in _GUARDS.items():
                if g.get("running"):
                    g["running"] = False
                    g["stopReason"] = "user"
                    stopped.append(aid)
        return {"ok": True, "stopped": stopped}
    if method in ("save_status", "clip_set"):
        return {"ok": True}
    if method == "clear_accounts":
        return []
    if method == "probe_refresh_one":
        return {
            "ok": True,
            "source": "local",
            "sameAsAccess": True,
            "hasClientId": True,
            "accounts": _rpc("list_accounts", []),
        }
    if method == "refresh_login_one":
        aid = _aid(args)
        with _LOCK:
            _bump_local_client_created_at(aid)
        return {
            "ok": True,
            "tokenType": "session",
            "exp": int(time.time()) + 86400 * 30,
            "accounts": _rpc("list_accounts", []),
        }
    if method == "refresh_login_kick_old":
        aid = _aid(args)
        ident = _rpc("local_identity", [])
        with _LOCK:
            rows = list(_sessions_of(aid))
            tool = _preview_tool_session_id(rows) if aid == DEMO_ID else ""
            dropped = ""
            if tool and not (ident.get("ok") and ident.get("userId") == aid and tool == _preview_local_session_id(rows)):
                _SESSIONS[aid] = [s for s in rows if s.get("sessionId") != tool]
                dropped = tool
            _bump_local_client_created_at(aid)
        return {
            "ok": True,
            "tokenType": "session",
            "exp": int(time.time()) + 86400 * 30,
            "droppedSessionId": dropped,
            "accounts": _rpc("list_accounts", []),
        }
    if method == "device_guard_pin_local":
        aid = _aid(args)
        ident = _rpc("local_identity", [])
        if not (ident.get("ok") and ident.get("userId") == aid):
            return {"ok": False, "error": "请先在本机 Cursor 登录这个号"}
        raw_iv = args[1] if len(args or []) > 1 else 30
        try:
            seconds = int(raw_iv)
        except (TypeError, ValueError):
            seconds = 30
        seconds = max(5, min(3600, seconds))
        with _LOCK:
            rows = [dict(s) for s in _sessions_of(aid)]
            count = len(rows)
            ide = _preview_local_session_id(rows) if aid == DEMO_ID else ""
            tool = _preview_tool_session_id(rows) if aid == DEMO_ID else ""
            present = [str(s.get("sessionId") or "") for s in rows]
            keep = login_detect.keep_session_ids_for_local_guard(ide, tool, present)
            if not keep:
                return {
                    "ok": False,
                    "error": "认不出本机 Cursor 那条客户端（签发时间没对上唯一一台），未开启保护",
                    "keepIds": [],
                    "beforeCount": count,
                    "afterCount": count,
                    "status": dict(_guard_of(aid)),
                }
            started = _start_guard(aid, keep, seconds)
            return {
                "ok": True,
                "keepIds": keep,
                "beforeCount": count,
                "afterCount": count,
                "status": started.get("status") or {},
                "error": "",
            }
    if method == "list_account_tokens":
        return {
            "ok": True,
            "tokens": {
                DEMO_ID: {
                    "worksessionToken": f"{DEMO_ID}::eyJpreview.worksession.token",
                    "refreshToken": "eyJpreview.refresh.token",
                },
                DEMO_ID_2: {
                    "worksessionToken": f"{DEMO_ID_2}::eyJpreview.worksession.token",
                    "refreshToken": "eyJpreview.refresh.token",
                },
            },
        }
    if method == "list_api_keys":
        return _public_keys()
    if method == "get_last_api_key_input":
        return _draft_api_key_input()
    if method == "set_last_api_key_input":
        text = str((args or [""])[0] or "")
        with _LOCK:
            _LAST_API_KEY_INPUT = text[: api_key_store.MAX_LAST_INPUT]
        return _LAST_API_KEY_INPUT
    if method == "import_api_keys":
        text = str((args or [""])[0] or "")
        keys, failed = api_key_store.parse_api_key_text(text)
        added = []
        with _LOCK:
            if text.strip():
                _LAST_API_KEY_INPUT = text[: api_key_store.MAX_LAST_INPUT]
            for key in keys:
                kid = api_key_store.key_id_for(key)
                rec = {
                    "id": kid,
                    "apiKeyName": "preview_api",
                    "userEmail": f"agent-{kid[:6]}@example.com",
                    "userId": 1000 + (int(kid[:6], 16) % 9000),
                    "addedAt": int(time.time()),
                }
                _API_KEYS[:] = [x for x in _API_KEYS if x["id"] != kid]
                _API_KEYS.append(rec)
                _API_KEY_SECRETS[kid] = key
                _AGENTS_BY_KEY.setdefault(kid, _demo_agents())
                added.append(dict(rec))
        return {"added": added, "failed": failed, "keys": _public_keys()}
    if method == "remove_api_key":
        kid = str((args or [""])[0] or "")
        with _LOCK:
            before = len(_API_KEYS)
            _API_KEYS[:] = [x for x in _API_KEYS if x["id"] != kid]
            _API_KEY_SECRETS.pop(kid, None)
            _AGENTS_BY_KEY.pop(kid, None)
            ok = len(_API_KEYS) < before
        return {"ok": ok, "keys": _public_keys(), "error": "" if ok else "密钥不存在"}
    if method == "list_cloud_agents":
        kid = str((args or [""])[0] or "")
        with _LOCK:
            known = any(x["id"] == kid for x in _API_KEYS)
            agents = [dict(a) for a in _AGENTS_BY_KEY.get(kid, [])]
        if not known:
            return {"ok": False, "error": "密钥不存在", "agents": []}
        return {"ok": True, "error": "", "agents": agents}
    if method == "delete_all_cloud_agents":
        kid = str((args or [""])[0] or "")
        with _LOCK:
            known = any(x["id"] == kid for x in _API_KEYS)
            agents = list(_AGENTS_BY_KEY.get(kid, []))
            if known:
                _AGENTS_BY_KEY[kid] = []
        if not known:
            return {"ok": False, "error": "密钥不存在", "deleted": 0, "failed": 0, "errors": []}
        n = len(agents)
        return {"ok": True, "deleted": n, "failed": 0, "errors": [], "error": ""}
    if method == "delete_cloud_agent":
        kid = str((args or ["", ""])[0] or "")
        aid = str((args[1] if len(args or []) > 1 else "") or "").strip()
        with _LOCK:
            known = any(x["id"] == kid for x in _API_KEYS)
            agents = list(_AGENTS_BY_KEY.get(kid, []))
            match = next((a for a in agents if a.get("id") == aid), None)
            if known and match:
                _AGENTS_BY_KEY[kid] = [a for a in agents if a.get("id") != aid]
        if not known:
            return {"ok": False, "error": "密钥不存在", "deleted": 0, "failed": 0, "errors": []}
        if not aid:
            return {"ok": False, "error": "缺少 agent id", "deleted": 0, "failed": 1, "errors": ["缺少 agent id"]}
        if not match:
            return {"ok": False, "error": "Agent 不存在", "deleted": 0, "failed": 1, "errors": [aid]}
        return {"ok": True, "deleted": 1, "failed": 0, "errors": [], "error": ""}
    return {"ok": False, "error": f"preview mock: {method}"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            html = (WEB / "index.html").read_text(encoding="utf-8")
            html = html.replace("<script src=\"app.js\"></script>", "<script src=\"/preview-mock.js\"></script>\n  <script src=\"app.js\"></script>", 1)
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/preview-mock.js":
            data = MOCK_JS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/rpc":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        result = _rpc(str(payload.get("method") or ""), payload.get("args") or [])
        data = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


MOCK_JS = r"""
(function () {
  function call(method, args) {
    return fetch("/rpc", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ method: method, args: args || [] }),
    }).then(function (r) { return r.json(); });
  }
  const names = [
    "list_accounts", "load_status", "get_settings", "set_settings", "save_status", "app_info",
    "local_identity", "list_sessions", "revoke_session", "revoke_sessions", "open_dashboard", "open_sessions_page", "open_login",
    "device_guard_status", "device_guard_start", "device_guard_start_auto", "device_guard_pin_local", "device_guard_stop", "device_guard_stop_all",
    "detect_local_account", "import_files", "import_text", "clear_accounts",
    "remove_accounts", "set_account_tags", "claim_one", "verify_one", "status_one", "sand_status_one",
    "switch_account", "login_bot", "probe_refresh_one", "refresh_login_one", "refresh_login_kick_old", "list_account_tokens",
    "account_export_text", "export_accounts", "clip_set",
    "list_api_keys", "get_last_api_key_input", "set_last_api_key_input", "import_api_keys", "remove_api_key",
    "list_cloud_agents", "delete_all_cloud_agents", "delete_cloud_agent",
    "mark_ui_ready", "request_quit"
  ];
  const api = {};
  names.forEach(function (name) {
    api[name] = function () { return call(name, Array.prototype.slice.call(arguments)); };
  });
  window.pywebview = { api: api };
  window.dispatchEvent(new Event("pywebviewready"));
})();
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=43147)
    args = parser.parse_args()
    threading.Thread(target=_guard_loop, name="preview-guard", daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"SandClaimer preview http://{args.host}:{args.port}/  (mock API, not the desktop app)", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
