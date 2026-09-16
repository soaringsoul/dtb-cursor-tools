"""Cursor Cloud Agents API v1 客户端。

文档：https://cursor.com/docs/cloud-agent/api/endpoints

- 列出：GET    /v1/agents
- 删除：DELETE /v1/agents/{id}   （不可逆）
- 身份：GET    /v1/me

认证：Basic（API Key 作用户名，密码为空）。
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_BASE = "https://api.cursor.com"
PAGE_SIZE = 100
REQUEST_TIMEOUT = 60


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


def api_request(
    api_key: str,
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
) -> Any:
    url = API_BASE + path
    if query:
        filtered = {k: v for k, v in query.items() if v is not None}
        if filtered:
            url += "?" + urllib.parse.urlencode(filtered)
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ApiError(exc.code, body) from exc


def whoami(api_key: str) -> dict[str, Any]:
    info = api_request(api_key, "GET", "/v1/me") or {}
    return {
        "apiKeyName": info.get("apiKeyName"),
        "userEmail": info.get("userEmail"),
        "userId": info.get("userId"),
    }


def list_all_agents(api_key: str, *, include_archived: bool = True) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        payload = api_request(
            api_key,
            "GET",
            "/v1/agents",
            {
                "limit": PAGE_SIZE,
                "cursor": cursor,
                "includeArchived": "true" if include_archived else "false",
            },
        ) or {}
        batch = payload.get("items") or []
        items.extend(batch)
        cursor = payload.get("nextCursor")
        if not cursor:
            break
    return items


def delete_agents(api_key: str, agents: list[dict[str, Any]]) -> dict[str, Any]:
    ok = 0
    failed = 0
    errors: list[str] = []
    for agent in agents or []:
        agent_id = str((agent or {}).get("id") or "").strip()
        if not agent_id:
            failed += 1
            errors.append("缺少 agent id")
            continue
        try:
            api_request(api_key, "DELETE", f"/v1/agents/{agent_id}")
        except ApiError as exc:
            failed += 1
            errors.append(f"{agent_id}: {exc}")
            continue
        ok += 1
    return {"ok": ok, "failed": failed, "errors": errors}
