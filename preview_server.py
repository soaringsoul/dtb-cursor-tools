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

import sand_api

WEB = Path(__file__).resolve().parent / "web"

DEMO_ID = "user_01ANITAREID331200000000000"
DEMO_EMAIL = "anitareid3312@outlook.com"

_LOCK = threading.Lock()
_SESSIONS = [
    {
        "sessionId": "1c3c233a6194e5eb6463e48122d093c0537507a385c6475dc1485884c511f2a6",
        "type": "client",
        "typeRaw": "SESSION_TYPE_CLIENT",
        "createdAt": "2026-08-20T04:12:00.000Z",
        "expiresAt": "2026-10-19T04:12:00.000Z",
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
]
_GUARD = {
    "running": False,
    "keepIds": [],
    "startedAt": None,
    "lastTickAt": None,
    "tickCount": 0,
    "kickedCount": 0,
    "lastKicked": [],
    "lastError": "",
    "sessionCount": 3,
    "stopReason": "",
    "intervalMinutes": 1,
}
_STOP = threading.Event()


def _guard_loop():
    while not _STOP.wait(1.0):
        with _LOCK:
            if not _GUARD["running"]:
                continue
            keep = set(_GUARD["keepIds"])
            _GUARD["tickCount"] = int(_GUARD["tickCount"] or 0) + 1
            _GUARD["lastTickAt"] = time.time()
            victims = [s for s in list(_SESSIONS) if s["sessionId"] not in keep]
            for row in victims:
                _SESSIONS[:] = [s for s in _SESSIONS if s["sessionId"] != row["sessionId"]]
                _GUARD["kickedCount"] = int(_GUARD["kickedCount"] or 0) + 1
                kicked = {
                    "sessionId": row["sessionId"],
                    "type": row.get("type") or "other",
                    "createdAt": row.get("createdAt"),
                    "at": time.time(),
                }
                _GUARD["lastKicked"] = [kicked] + list(_GUARD["lastKicked"])[:7]


def _session_block():
    sessions = sand_api.annotate_local_sessions(list(_SESSIONS), True)
    return {
        "ok": True,
        "error": "",
        "email": DEMO_EMAIL,
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
        snap = dict(_GUARD)
        snap["keepIds"] = list(_GUARD["keepIds"])
        snap["lastKicked"] = list(_GUARD["lastKicked"])
        snap["sessionCount"] = len(_SESSIONS)
        return {DEMO_ID: snap}


def _rpc(method: str, args):
    if method == "list_accounts":
        return [
            {
                "id": DEMO_ID,
                "label": DEMO_EMAIL,
                "tokenType": "session",
                "addedAt": int(time.time() * 1000) - 86400000,
            }
        ]
    if method == "load_status":
        block = _session_block()
        return {
            DEMO_ID: {
                "kind": "alive",
                "alive": True,
                "membership": "business",
                "percent": 62.8,
                "autoPercent": 18.2,
                "apiPercent": 100,
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
        }
    if method == "get_settings":
        return {"hideHelp": True, "autoVerify": True}
    if method == "local_identity":
        return {"ok": True, "userId": DEMO_ID, "email": DEMO_EMAIL}
    if method == "list_sessions":
        return _session_block()
    if method == "revoke_session":
        sid = str((args or [None, ""])[1] if len(args or []) > 1 else "")
        with _LOCK:
            before = len(_SESSIONS)
            _SESSIONS[:] = [s for s in _SESSIONS if s["sessionId"] != sid]
            ok = len(_SESSIONS) < before
        return {"ok": ok, "error": "" if ok else "设备不在列表里", "status": 200 if ok else 404}
    if method == "open_dashboard":
        return {"ok": True, "browser": "preview", "url": "https://cursor.com/dashboard/spending", "reused": False}
    if method == "open_sessions_page":
        return {"ok": True, "browser": "preview", "url": "https://cursor.com/dashboard/settings#active-sessions", "reused": False}
    if method == "open_login":
        return {"ok": True, "browser": "preview"}
    if method == "device_guard_status":
        return _guard_status()
    if method == "device_guard_start":
        keep = list((args or [None, []])[1] if len(args or []) > 1 else [])
        keep = [str(x) for x in keep if str(x).strip()]
        if not keep:
            return {"ok": False, "error": "保留名单为空：至少勾选一台要保留的设备"}
        raw_iv = args[2] if len(args or []) > 2 else 1
        try:
            minutes = int(raw_iv)
        except (TypeError, ValueError):
            minutes = 1
        minutes = max(1, min(120, minutes))
        with _LOCK:
            _GUARD.update(
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
                    "intervalMinutes": minutes,
                }
            )
        return {"ok": True, "status": _guard_status()[DEMO_ID]}
    if method == "device_guard_stop":
        with _LOCK:
            _GUARD["running"] = False
            _GUARD["stopReason"] = "user"
        return {"ok": True, "running": False, "wasRunning": True, "status": _guard_status()[DEMO_ID]}
    if method == "patch_status":
        return {
            "ok": True,
            "installed": False,
            "version": "preview",
            "path": "",
            "streamCapable": False,
            "requiredVersion": "3.18.9 / 3.18.25 / 3.19.13",
            "testedVersion": true,
            "requiredVersions": ["3.18.9", "3.18.25", "3.19.13"],
            "downloadUrl": "",
        }
    if method in ("save_status", "set_settings", "clip_set"):
        return {"ok": True}
    if method == "clear_accounts":
        return []
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
    "list_accounts", "load_status", "get_settings", "set_settings", "save_status",
    "local_identity", "list_sessions", "revoke_session", "open_dashboard", "open_sessions_page", "open_login",
    "device_guard_status", "device_guard_start", "device_guard_stop", "device_guard_stop_all",
    "patch_status", "verify_runtime", "apply_patch", "restore_patch", "set_cursor_path",
    "detect_local_account", "import_files", "import_text", "clear_accounts",
    "remove_accounts", "claim_one", "verify_one", "status_one", "sand_status_one",
    "switch_account", "account_export_text", "export_accounts", "clip_set", "open_url"
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
