"""设备登录时间检测：用会话 createdAt 对比检测时刻，落在窗口内则视为刚登录。"""

from __future__ import annotations

import datetime
from typing import Any

DEFAULT_WINDOW_SECONDS = 120
MIN_WINDOW_SECONDS = 5
MAX_WINDOW_SECONDS = 3600
FUTURE_SKEW_MS = 5000


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
