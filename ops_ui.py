"""号池工作台：列表过滤 / 排序 / KPI / 批量勾选。与 web/app.js 保持一致。"""

from __future__ import annotations

from datetime import datetime, timezone

import sand_patch

APP_NAME = "cursor账号管理器"
EXPIRING_MS = 7 * 24 * 3600 * 1000

HELP_JOBS = [
    {
        "title": "看额度",
        "body": "导入或探测本机账号后点「验证」，看 Bot / Auto / 高级 三池和到期日。验证只读，不会领取。",
    },
    {
        "title": "切到本机",
        "body": "点该号「切号」。会先关掉当前 Cursor，写入登录态后再打开。默认先换新登录票。",
    },
    {
        "title": "守设备",
        "body": "先在本机 Cursor 登录该号，再打开「本机保护」页勾选要留的设备。一键本机保护在该页里，避免误踢 IDE。",
    },
]


def app_info() -> dict:
    return {"name": APP_NAME, "version": sand_patch.TOOL_VERSION, "helpJobs": HELP_JOBS}


def batch_ids(account_ids, selected_ids):
    selected = set(selected_ids or [])
    return [i for i in (account_ids or []) if i in selected]


def to_ms(v):
    if v is None or v == "":
        return float("nan")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        n = float(v)
        if n < 1e12:
            n *= 1000
        return n
    s = str(v).strip()
    if s.replace(".", "", 1).isdigit():
        n = float(s)
        if n < 1e12:
            n *= 1000
        return n
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000
    except ValueError:
        return float("nan")


def _is_nan(n):
    return n != n


def remain_ms(account, state, now_ms):
    st = state or {}
    if st.get("billingCycleEndMs") is not None:
        end = float(st["billingCycleEndMs"])
        return end - now_ms
    raw = st.get("pendingCancellationDate") or st.get("billingCycleEnd")
    sub = to_ms(raw) if raw else float("nan")
    if not _is_nan(sub):
        return sub - now_ms
    exp = to_ms((account or {}).get("exp"))
    if _is_nan(exp):
        return float("nan")
    return exp - now_ms


def is_dead(state):
    return bool(state) and state.get("alive") is False


def is_bot_full(state):
    if not state:
        return False
    if state.get("hasAvailableUsage") is False:
        return True
    p = state.get("percent")
    try:
        return p is not None and float(p) >= 100
    except (TypeError, ValueError):
        return False


def is_expiring(account, state, now_ms):
    if is_dead(state):
        return False
    r = remain_ms(account, state, now_ms)
    return (not _is_nan(r)) and 0 < r <= EXPIRING_MS


def is_card(state):
    return bool(state) and state.get("kind") == "card"


def membership_key(state):
    raw = str((state or {}).get("membership") or "").lower().replace("+", "plus")
    return "".join(ch for ch in raw if ch.isalnum())


_PAID_MEMBERSHIPS = {"pro", "proplus", "ultra", "enterprise", "team", "business"}


def is_paid_plan(state):
    return membership_key(state) in _PAID_MEMBERSHIPS


def is_free_plan(state):
    key = membership_key(state)
    return key == "free" or key == "freetrial"


def hero_kpis(rows, now_ms):
    rows = rows or []
    dead = 0
    guarding = 0
    bot_full = 0
    expiring = 0
    for r in rows:
        st = r.get("state") or {}
        acc = r.get("account") or {"id": r.get("id")}
        if is_dead(st):
            dead += 1
        if r.get("guarding"):
            guarding += 1
        if is_bot_full(st):
            bot_full += 1
        if is_expiring(acc, st, now_ms):
            expiring += 1
    return {
        "total": len(rows),
        "dead": dead,
        "guarding": guarding,
        "botFull": bot_full,
        "expiring": expiring,
    }


def account_matches(row, query="", filt="all", local_user_id=None, now_ms=0):
    acc = (row or {}).get("account") or {}
    st = (row or {}).get("state") or {}
    aid = str((row or {}).get("id") or acc.get("id") or "")
    label = str(acc.get("label") or "")
    q = (query or "").strip().lower()
    if q:
        blob = f"{label} {aid}".lower()
        if q not in blob:
            return False
    f = filt or "all"
    if f == "all":
        return True
    if f == "local":
        return bool(local_user_id) and aid == local_user_id
    if f == "guarding":
        return bool(row.get("guarding"))
    if f == "dead":
        return is_dead(st)
    if f == "card":
        return is_card(st)
    if f == "botFull":
        return is_bot_full(st)
    if f == "expiring":
        return is_expiring(acc, st, now_ms)
    if f == "paid":
        return is_paid_plan(st)
    if f == "free":
        return is_free_plan(st)
    return True


def claim_visible(state):
    return not (state and state.get("kind") == "ok")


def ticket_menu_groups(account=None, state=None, token_on=False, busy=False):
    """账号行图标：显示 Token / 复制 / 网页领取 / 领取 / 移除。与 web/app.js 保持一致。"""
    account = account or {}
    has_refresh = bool(account.get("hasRefresh"))
    dis = bool(busy)
    pills = ["已有 Refresh"] if has_refresh else ["未探测 Refresh"]
    if token_on:
        pills.append("Token 已展开")

    view = [
        {
            "act": "showToken",
            "label": "隐藏 Token" if token_on else "显示 Token",
            "title": "显示或隐藏 Worksession / Refresh token",
            "keepOpen": True,
        },
        {
            "act": "copy",
            "label": "复制",
            "title": "复制：邮箱----user_id::token",
            "disabled": dis,
        },
        {
            "act": "browser",
            "label": "网页领取",
            "title": "用该账号登录态打开隔离浏览器到 Sand 领取页",
            "disabled": dis,
        },
    ]
    if claim_visible(state):
        view.append(
            {
                "act": "claim",
                "label": "领取",
                "title": "领取 Sand 资格",
                "disabled": dis,
                "span": True,
            }
        )

    return {
        "pills": pills,
        "groups": [
            {"id": "view", "title": "查看", "items": view},
            {
                "id": "danger",
                "title": "",
                "kind": "danger",
                "items": [
                    {
                        "act": "remove",
                        "label": "移除这个账号",
                        "cls": "danger",
                        "disabled": dis,
                        "span": True,
                    }
                ],
            },
        ],
    }


def sort_rows(rows, sort_by="added", now_ms=0, local_user_id=None):
    rows = list(rows or [])

    def remain_tuple(r):
        st = r.get("state") or {}
        acc = r.get("account") or {}
        dead = 1 if is_dead(st) else 0
        rem = remain_ms(acc, st, now_ms)
        missing = 1 if _is_nan(rem) else 0
        return (dead, missing, 0 if missing else rem)

    def bot_tuple(r):
        st = r.get("state") or {}
        p = st.get("percent")
        try:
            val = float(p)
            missing = 0
        except (TypeError, ValueError):
            val = 0.0
            missing = 1
        return (missing, -val)

    def added_tuple(r):
        acc = r.get("account") or {}
        added = acc.get("addedAt") or 0
        try:
            return (0, -float(added))
        except (TypeError, ValueError):
            return (1, 0)

    def pin_rank(r):
        aid = str(r.get("id") or (r.get("account") or {}).get("id") or "")
        return 0 if local_user_id and aid == str(local_user_id) else 1

    keyfn = remain_tuple
    if sort_by == "bot":
        keyfn = bot_tuple
    elif sort_by == "added":
        keyfn = added_tuple
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda it: (pin_rank(it[1]), keyfn(it[1]), it[0]))
    return [r for _, r in indexed]
