"""设备登录时间检测：用会话 createdAt 对比检测时刻，落在窗口内则视为刚登录。"""

from __future__ import annotations

import datetime
import time
from typing import Any

DEFAULT_WINDOW_SECONDS = 120
MIN_WINDOW_SECONDS = 5
MAX_WINDOW_SECONDS = 3600
FUTURE_SKEW_MS = 5000
FRESH_TOOL_WINDOW_SECONDS = 1800


def clamp_window_seconds(raw: Any, default: int = DEFAULT_WINDOW_SECONDS) -> int:
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(MIN_WINDOW_SECONDS, min(MAX_WINDOW_SECONDS, n))


def created_at_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n < 1e12:
            n *= 1000
        return int(n)
    text = str(value).strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():
        n = float(text)
        if n < 1e12:
            n *= 1000
        return int(n)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    dt = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def is_recent_login(created_at: Any, now_ms: int, window_seconds: Any) -> bool:
    created = created_at_ms(created_at)
    try:
        now = int(now_ms)
    except (TypeError, ValueError):
        return False
    if created is None:
        return False
    window_ms = clamp_window_seconds(window_seconds) * 1000
    delta = now - created
    if delta < -FUTURE_SKEW_MS:
        return False
    if delta < 0:
        return True
    return delta <= window_ms


def recent_session_ids(sessions: Any, now_ms: int, window_seconds: Any) -> list[str]:
    ids: list[str] = []
    for row in sessions or []:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("sessionId") or "").strip()
        if not sid:
            continue
        if is_recent_login(row.get("createdAt"), now_ms, window_seconds):
            ids.append(sid)
    return ids


def checked_ids_after_detect(
    sessions: Any,
    now_ms: int,
    window_seconds: Any,
    previous_checked: Any = None,
) -> list[str]:
    """检测完成后替换勾选：丢掉原选中，只勾窗口内刚登录的设备。无刚登录则空列表。"""
    del previous_checked  # 故意忽略：一律以本次检测结果覆盖
    return recent_session_ids(sessions, now_ms, window_seconds)


def is_client_session(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    kind = str(row.get("type") or "").strip().lower()
    raw = str(row.get("typeRaw") or row.get("type") or "").strip().upper()
    return kind == "client" or raw == "SESSION_TYPE_CLIENT"


def _client_created_map(sessions: Any) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for row in sessions or []:
        if not is_client_session(row):
            continue
        sid = str(row.get("sessionId") or "").strip()
        if not sid:
            continue
        out[sid] = created_at_ms(row.get("createdAt"))
    return out


def pin_local_session_id(before: Any, after: Any) -> str:
    """换票前后客户端会话快照对比。恰好一台新增或 createdAt 变新则返回其 sessionId，否则空串。"""
    before_map = _client_created_map(before)
    after_map = _client_created_map(after)
    changed: list[str] = []
    for sid, after_ms in after_map.items():
        if sid not in before_map:
            changed.append(sid)
            continue
        before_ms = before_map[sid]
        if after_ms is None:
            continue
        if before_ms is None or after_ms > before_ms:
            changed.append(sid)
    if len(changed) == 1:
        return changed[0]
    return ""


def jwt_issued_ms(claims: Any) -> int | None:
    """Cursor JWT 用 `time`（unix 秒字符串），WorkOS 标准票用 `iat`。返回毫秒。"""
    if not isinstance(claims, dict):
        return created_at_ms(claims)
    raw = claims.get("time")
    if raw is None or raw == "":
        raw = claims.get("iat")
    ms = created_at_ms(raw)
    if ms is None:
        return None
    # created_at_ms 把 <1e12 当秒。time/iat 就是秒。
    return ms


def match_session_id_by_jwt_time(sessions: Any, jwt_time: Any, slack_seconds: int = 1) -> str:
    """本机 JWT 签发秒对准客户端 createdAt。恰好一条则返回 sessionId，否则空串。"""
    issued = jwt_issued_ms(jwt_time)
    if issued is None:
        issued = created_at_ms(jwt_time)
    if issued is None:
        return ""
    try:
        slack = max(0, int(slack_seconds))
    except (TypeError, ValueError):
        slack = 1
    issued_sec = issued // 1000
    hits: list[str] = []
    for row in sessions or []:
        if not is_client_session(row):
            continue
        sid = str(row.get("sessionId") or "").strip()
        if not sid:
            continue
        created = created_at_ms(row.get("createdAt"))
        if created is None:
            continue
        if abs(created // 1000 - issued_sec) <= slack:
            hits.append(sid)
    if len(hits) == 1:
        return hits[0]
    return ""


def resolve_local_session_id(sessions: Any, jwt_claims: Any, saved_session_id: Any = "") -> str:
    """优先 JWT time 对齐；对不上再用本机记住的 sessionId（仍在线才算）。"""
    hit = match_session_id_by_jwt_time(sessions, jwt_claims)
    if hit:
        return hit
    saved = str(saved_session_id or "").strip()
    if not saved:
        return ""
    for row in sessions or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("sessionId") or "").strip() == saved:
            return saved
    return ""


def keep_session_ids_for_local_guard(
    ide_session_id: Any,
    tool_session_id: Any = "",
    present_ids: Any = None,
) -> list[str]:
    """本机保护保留名单：必须含 Cursor IDE 那条；本工具若是另一条客户端也一并留。

    换票新会话不能单独当「本机」。IDE 不在当前列表里则返回空，调用方应拒绝启动。
    """
    present: set[str] | None
    if present_ids is None:
        present = None
    else:
        present = {str(x or "").strip() for x in present_ids if str(x or "").strip()}
    ide = str(ide_session_id or "").strip()
    if not ide:
        return []
    if present is not None and ide not in present:
        return []
    out = [ide]
    tool = str(tool_session_id or "").strip()
    if tool and tool != ide and (present is None or tool in present):
        out.append(tool)
    return out


def stale_tool_session_id_after_refresh(
    old_claims: Any,
    new_claims: Any,
    sessions: Any,
    ide_session_id: Any = "",
) -> str:
    """换票后若官方新开了一条客户端，返回应踢掉的旧本工具会话。不会返回 IDE。

    实测 2026-09-16：POST /oauth/token 用 session JWT 换票会新注册一台 Desktop App，
    旧会话仍在。「刷票并踢旧」在换票成功后踢掉这条旧会话，避免同一台电脑堆成多台设备。
    """
    new_sid = match_session_id_by_jwt_time(sessions, new_claims)
    old_sid = match_session_id_by_jwt_time(sessions, old_claims)
    if not new_sid or not old_sid or new_sid == old_sid:
        return ""
    ide = str(ide_session_id or "").strip()
    if old_sid == ide:
        return ""
    return old_sid


def newest_client_session_id(sessions: Any, exclude_ids: Any = None) -> str:
    exclude = {str(x or "").strip() for x in (exclude_ids or []) if str(x or "").strip()}
    best_sid = ""
    best_ms: int | None = None
    for row in sessions or []:
        if not is_client_session(row):
            continue
        sid = str(row.get("sessionId") or "").strip()
        if not sid or sid in exclude:
            continue
        ms = created_at_ms(row.get("createdAt"))
        if ms is None:
            continue
        if best_ms is None or ms > best_ms:
            best_ms = ms
            best_sid = sid
    return best_sid


def apply_fresh_marks(
    sessions: Any,
    now_ms: Any = None,
    window_seconds: Any = FRESH_TOOL_WINDOW_SECONDS,
) -> list:
    """给换票新开的客户端打 freshMark。不改入参。

    本工具那条若落在窗口内优先标「刚换票」；对不上时，最近一台新建客户端也标，避免沉在列表末尾找不到。
    """
    if now_ms is None:
        now = int(time.time() * 1000)
    else:
        try:
            now = int(now_ms)
        except (TypeError, ValueError):
            now = int(time.time() * 1000)
    out = [dict(row) for row in (sessions or []) if isinstance(row, dict)]
    any_fresh = False
    for item in out:
        if item.get("toolMark") == "tool" and is_recent_login(item.get("createdAt"), now, window_seconds):
            item["freshMark"] = "fresh"
            any_fresh = True
        else:
            item["freshMark"] = None
    if any_fresh:
        return out
    newest = newest_client_session_id(out)
    if not newest:
        return out
    for item in out:
        if str(item.get("sessionId") or "").strip() != newest:
            continue
        if is_recent_login(item.get("createdAt"), now, window_seconds):
            item["freshMark"] = "fresh"
        break
    return out


def sort_sessions_for_display(sessions: Any) -> list:
    """展示顺序：本机 → 本工具 → 刚换票 → 可能是本机 → 其余按创建时间新到旧。"""
    rows = [row for row in (sessions or []) if isinstance(row, dict)]

    def _priority(row: dict) -> tuple:
        if row.get("localMark") == "local":
            rank = 0
        elif row.get("toolMark") == "tool":
            rank = 1
        elif row.get("freshMark") == "fresh":
            rank = 2
        elif row.get("localMark") == "maybe-local":
            rank = 3
        else:
            rank = 4
        created = created_at_ms(row.get("createdAt")) or 0
        return (rank, -created)

    return sorted(rows, key=_priority)
