"""本机 API Key 存储。

路径：LOCALAPPDATA\\SandClaimer\\api_keys.json（macOS 为 ~/SandClaimer/api_keys.json）。
信封与 accounts.json 相同（Windows DPAPI；不可用时明文 items）。
list() 不回传完整密钥。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from typing import Any, Callable

import cloud_agents
from accounts import _dpapi
from cloud_agents import ApiError

_JWT_PREFIX = "eyJ"


def _store_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "SandClaimer", "api_keys.json")


def key_id_for(api_key: str) -> str:
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]


def parse_api_key_text(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """按行解析 API Key。空行跳过；JWT / 非法行进 failed。"""
    keys: list[str] = []
    failed: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(_JWT_PREFIX) or " " in line or "\t" in line:
            failed.append({"line": line, "error": "不是 API Key（需要 crsr_… 或整行密钥）"})
            continue
        if not (line.startswith("crsr_") or len(line) >= 8):
            failed.append({"line": line, "error": "不是 API Key（需要 crsr_… 或整行密钥）"})
            continue
        if line in seen:
            continue
        seen.add(line)
        keys.append(line)
    return keys, failed


def _public(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "apiKeyName": item.get("apiKeyName") or "",
        "userEmail": item.get("userEmail") or "",
        "userId": item.get("userId"),
        "addedAt": item.get("addedAt"),
    }


MAX_LAST_INPUT = 64 * 1024


def _split_payload(payload: Any) -> tuple[list[Any], str]:
    """兼容旧文件：加密体以前是 items 数组，现在是 {items, last_input}。"""
    if isinstance(payload, list):
        return payload, ""
    if not isinstance(payload, dict):
        return [], ""
    items = payload.get("items", [])
    last = payload.get("last_input")
    return (items if isinstance(items, list) else []), (last if isinstance(last, str) else "")


def _pack_delete(result: dict[str, Any]) -> dict[str, Any]:
    errors = result.get("errors") or []
    return {
        "ok": int(result.get("failed") or 0) == 0,
        "deleted": int(result.get("ok") or 0),
        "failed": int(result.get("failed") or 0),
        "errors": errors,
        "error": "; ".join(errors) if errors else "",
    }


class ApiKeyStore:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._last_input = ""
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        path = _store_path()
        try:
            with open(path, "rb") as handle:
                envelope = json.loads(handle.read().decode("utf-8"))
        except Exception:
            return
        items: list[Any] = []
        last_input = ""
        if isinstance(envelope, dict) and envelope.get("enc") == "dpapi":
            blob = base64.b64decode(envelope.get("data", ""))
            dec = _dpapi(blob, protect=False)
            if dec:
                try:
                    items, last_input = _split_payload(json.loads(dec.decode("utf-8")))
                except Exception:
                    items, last_input = [], ""
        elif isinstance(envelope, dict):
            items, last_input = _split_payload(envelope)
        if last_input:
            self._last_input = last_input[:MAX_LAST_INPUT]
        if not isinstance(items, list):
            return
        for it in items:
            kid = it.get("id")
            key = it.get("key")
            if not kid or not key:
                continue
            self._items[kid] = {
                "id": kid,
                "key": key,
                "apiKeyName": it.get("apiKeyName") or "",
                "userEmail": it.get("userEmail") or "",
                "userId": it.get("userId"),
                "addedAt": it.get("addedAt") if isinstance(it.get("addedAt"), (int, float)) else None,
            }

    def _payload(self) -> dict[str, Any]:
        return {"items": list(self._items.values()), "last_input": self._last_input}

    def last_input(self) -> str:
        with self._lock:
            return self._last_input

    def draft_text(self) -> str:
        """输入框应回填的内容：上次粘贴优先，没有则用最近添加的那把密钥。"""
        with self._lock:
            if self._last_input:
                return self._last_input
            latest = None
            latest_at = None
            for rec in self._items.values():
                at = rec.get("addedAt")
                ts = at if isinstance(at, (int, float)) else 0
                if latest is None or ts >= (latest_at or 0):
                    latest = rec
                    latest_at = ts
            return str((latest or {}).get("key") or "")

    def set_last_input(self, text: str) -> str:
        value = str(text or "")
        if len(value) > MAX_LAST_INPUT:
            value = value[:MAX_LAST_INPUT]
        with self._lock:
            if self._last_input == value:
                return value
            self._last_input = value
            self._save()
            return value

    def _save(self) -> None:
        path = _store_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            body = self._payload()
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            enc = _dpapi(payload, protect=True)
            if enc is not None:
                envelope = {"v": 1, "enc": "dpapi", "data": base64.b64encode(enc).decode("ascii")}
            else:
                envelope = {"v": 1, "enc": "none", **body}
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            pass

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_public(v) for v in self._items.values()]

    def get(self, key_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(str(key_id or ""))
            return dict(item) if item else None

    def add(self, api_key: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        key = (api_key or "").strip()
        meta = meta or {}
        kid = key_id_for(key)
        with self._lock:
            existing = self._items.get(kid) or {}
            rec = {
                "id": kid,
                "key": key,
                "apiKeyName": meta.get("apiKeyName") if meta.get("apiKeyName") is not None else existing.get("apiKeyName") or "",
                "userEmail": meta.get("userEmail") if meta.get("userEmail") is not None else existing.get("userEmail") or "",
                "userId": meta.get("userId") if meta.get("userId") is not None else existing.get("userId"),
                "addedAt": existing.get("addedAt") or int(time.time()),
            }
            self._items[kid] = rec
            self._save()
            return dict(rec)

    def remove(self, key_id: str) -> bool:
        kid = str(key_id or "")
        with self._lock:
            if kid not in self._items:
                return False
            del self._items[kid]
            self._save()
            return True

    def import_keys(
        self,
        text: str,
        whoami_fn: Callable[[str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from cloud_agents import whoami as default_whoami

        fn = whoami_fn or default_whoami
        if (text or "").strip():
            self.set_last_input(text)
        keys, failed = parse_api_key_text(text)
        added: list[dict[str, Any]] = []
        for key in keys:
            try:
                info = fn(key) or {}
            except ApiError as exc:
                failed.append({"line": key, "error": str(exc)})
                continue
            except Exception as exc:
                failed.append({"line": key, "error": str(exc)})
                continue
            rec = self.add(key, info)
            added.append(_public(rec))
        return {"added": added, "failed": failed, "keys": self.list()}

    def list_agents(self, key_id: str) -> dict[str, Any]:
        item = self.get(key_id)
        if not item:
            return {"ok": False, "error": "密钥不存在", "agents": []}
        try:
            agents = cloud_agents.list_all_agents(item["key"], include_archived=True)
        except ApiError as exc:
            return {"ok": False, "error": str(exc), "agents": []}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "agents": []}
        return {"ok": True, "error": "", "agents": agents}

    def delete_all_agents(self, key_id: str) -> dict[str, Any]:
        listed = self.list_agents(key_id)
        if not listed.get("ok"):
            return {
                "ok": False,
                "error": listed.get("error") or "列出失败",
                "deleted": 0,
                "failed": 0,
                "errors": [],
            }
        agents = listed.get("agents") or []
        if not agents:
            return {"ok": True, "deleted": 0, "failed": 0, "errors": [], "error": ""}
        item = self.get(key_id)
        if not item:
            return {"ok": False, "error": "密钥不存在", "deleted": 0, "failed": 0, "errors": []}
        result = cloud_agents.delete_agents(item["key"], agents)
        return _pack_delete(result)

    def delete_agent(self, key_id: str, agent_id: str) -> dict[str, Any]:
        item = self.get(key_id)
        if not item:
            return {"ok": False, "error": "密钥不存在", "deleted": 0, "failed": 0, "errors": []}
        aid = str(agent_id or "").strip()
        if not aid:
            return {"ok": False, "error": "缺少 agent id", "deleted": 0, "failed": 1, "errors": ["缺少 agent id"]}
        return _pack_delete(cloud_agents.delete_agents(item["key"], [{"id": aid}]))
