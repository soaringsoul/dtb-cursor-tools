"""Cursor Sand（Grok Bot）资格查询与领取。

所有端点均为真机实测确认：
  - 额度：POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus（Bearer accessToken）
  - 资格：POST https://cursor.com/api/dashboard/get-sand-access-status（会话 cookie）
  - teamId：POST https://cursor.com/api/dashboard/get-me（会话 cookie）
  - 领取：个人 POST /api/dashboard/start-sand-trial；团队 POST /api/dashboard/request-sand-team-access（body 带 teamId）
  - 登录会话：GET https://cursor.com/api/auth/sessions（会话 cookie）
鉴权：api2 用 Bearer 明文 accessToken；cursor.com 用会话 cookie（userId::jwt），写操作再加 Origin 过 CSRF。

额度口径（实测 usage-summary 原始响应，Pro 个人号）：
  individualUsage.plan = {
      autoPercentUsed: 62.13,   # Auto 池（账单月）
      apiPercentUsed: 100,      # 高级模型 / API 池（账单月，满了封顶 100）
      totalPercentUsed: 66.19,  # 两池加权混合：(auto已用+api已用)/(auto上限+api上限)，与任一池都不相等
      used/limit: 2000/2000,    # 「已含 $20」这一桶（分），总消费≥$20 就恒为满
      breakdown: {included, bonus, total},  # total = 账单月总消费（分），bonus = total - included
  }
  individualUsage.onDemand = {enabled, used}  # 按量付费（真实扣费，分）
三个池子必须分开展示：Bot 周用量（GetSandUsageStatus.usagePercent）、Auto、高级(API)；
totalPercentUsed 是混合值，只能当参考，不能当「用量」显示。
"""

import base64
import datetime
import hashlib
import json
import re
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests

SAND_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus"
ACCESS_STATUS_URL = "https://cursor.com/api/dashboard/get-sand-access-status"
START_TRIAL_URL = "https://cursor.com/api/dashboard/start-sand-trial"
TEAM_ACCESS_URL = "https://cursor.com/api/dashboard/request-sand-team-access"
TEAM_ONBOARD_URL = "https://cursor.com/api/dashboard/update-team-sand-onboarding-completed"
GET_ME_URL = "https://cursor.com/api/dashboard/get-me"
USAGE_SUMMARY_URL = "https://cursor.com/api/usage-summary"
TEAM_SPEND_URL = "https://cursor.com/api/dashboard/get-team-spend"
STRIPE_URL = "https://cursor.com/api/auth/stripe"
AUTH_ME_URL = "https://cursor.com/api/auth/me"
SESSIONS_URL = "https://cursor.com/api/auth/sessions"
ORIGIN = "https://cursor.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TIMEOUT = 20
# Sand（Grok Bot）额度按周重置；接口不回 nextResetTimestampUtc 时用 currentPeriodStart + 7 天推算。
SAND_PERIOD_DAYS = 7


def _b64url_json(segment: str) -> dict:
    segment = segment.replace("-", "+").replace("_", "/")
    segment += "=" * (-len(segment) % 4)
    return json.loads(base64.b64decode(segment).decode("utf-8", "replace"))


def parse_token(raw: str):
    """把用户粘贴的 token 解析成 (user_id, access_token_jwt, claims)。

    支持两种格式：
      1) 纯 access_token（JWT，形如 eyJ...）；user id 从 JWT 的 sub 里取。
      2) ws token：user_01XXX::eyJ...（WorkosCursorSessionToken，:: 可为 %3A%3A）。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("空 token")
    text = re.sub(r"^WorkosCursorSessionToken=", "", text, flags=re.I).strip()
    sep = "::" if "::" in text else ("%3A%3A" if "%3A%3A" in text else None)
    pasted_uid = None
    jwt = text
    if sep:
        left, _, right = text.partition(sep)
        pasted_uid = left.strip()
        jwt = right.strip()
    claims: dict = {}
    try:
        claims = _b64url_json(jwt.split(".")[1])
    except Exception:
        claims = {}
    sub = str(claims.get("sub", ""))
    from_sub = sub.split("|")[-1] if sub else ""
    user_id = from_sub if from_sub.startswith("user_") else (pasted_uid or "")
    if not user_id.startswith("user_"):
        raise ValueError("无法解析 user id（既不是 ws token，JWT 里也没有 sub）")
    return user_id, jwt, claims


LOGIN_DEEP_URL = "https://cursor.com/api/auth/loginDeepCallbackControl"
AUTH_POLL_URL = "https://api2.cursor.sh/auth/poll"
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def token_type(token: str) -> str | None:
    """返回 JWT payload 里的 type（web / session / …）；解析失败返回 None。"""
    try:
        _uid, jwt, claims = parse_token(token)
        return claims.get("type")
    except Exception:
        return None


def token_exp(token: str) -> int | None:
    """返回 JWT 的过期时间（exp，epoch 秒）；无 exp 或解析失败返回 None。"""
    try:
        _uid, _jwt, claims = parse_token(token)
        exp = claims.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def probe_token_alive(token: str) -> str:
    """切号前的「探活闸」：用会话 cookie 打 cursor.com/api/auth/me，判断这个号还能不能用。

    借鉴 kc-cursor 的 verify_account 闸①——切进一个死号会「设置里有号、一发消息就重登」
    （表现就是「切了等于没切」），所以切之前先探一下。
    返回 'alive'（200，能用）/ 'dead'（已失效或被限）/ 'unknown'（网络等无法判定）。

    实测：票作废后 auth/me 不是 401，而是 **204 无内容**（同一号 api2 / usage-summary 全部 401），
    所以 204 也按 dead 处理；verify() 里还会再用 api2 二次确认，避免偶发误杀。
    """
    try:
        user_id, jwt, _claims = parse_token(token)
    except Exception:
        return "unknown"
    headers = {"cookie": _cookie(user_id, jwt), "accept": "application/json", "user-agent": UA}
    status, _text = _get(AUTH_ME_URL, headers)
    if status == 200:
        return "alive"
    if status in (204, 401, 403):
        return "dead"
    return "unknown"


def exchange_web_to_session(token: str, timeout: int = 30):
    """把 type=web 的会话票换成 Cursor 客户端认的 type=session 的 (accessToken, refreshToken)。

    背景：网页登录得到的是 type=web 的 WorkosCursorSessionToken，能过 cursor.com 接口，但写进
    客户端 cursorAuth/accessToken 后，设置里能显示账号、一发消息就要求重新登录。这里按官方深度
    登录（PKCE）走一遍：用仍有效的 web Cookie 自动确认授权，从 api2.cursor.sh/auth/poll 换回真正
    的 session accessToken + refreshToken。

    仅对 web 票有意义；session 票或换取失败返回 (None, None)，由调用方回退原样写。
    """
    try:
        user_id, jwt, claims = parse_token(token)
    except Exception:
        return None, None
    if str(claims.get("type") or "").lower() != "web":
        return None, None

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    handshake = str(uuid.uuid4())
    cookie_val = f"{user_id}::{jwt}"

    session = requests.Session()
    session.trust_env = False
    session.headers.update({"user-agent": CHROME_UA, "accept": "application/json, text/plain, */*"})
    try:
        resp = session.post(
            LOGIN_DEEP_URL,
            json={"uuid": handshake, "challenge": challenge},
            headers={
                "cookie": f"WorkosCursorSessionToken={cookie_val}",
                "content-type": "application/json",
                "origin": ORIGIN,
            },
            timeout=timeout,
        )
        if not (200 <= resp.status_code < 300):
            return None, None
    except Exception:
        return None, None

    # 轮询换 token（最多 20 次 × 1s）；poll 走 api2，不带 Cookie。
    for _ in range(20):
        time.sleep(1.0)
        try:
            poll = session.get(
                f"{AUTH_POLL_URL}?uuid={handshake}&verifier={verifier}",
                headers={"accept": "*/*"},
                timeout=timeout,
            )
        except Exception:
            continue
        if poll.status_code == 200 and (poll.text or "").strip():
            try:
                data = poll.json()
            except Exception:
                continue
            access = (data.get("accessToken") or "").strip()
            if access:
                refresh = (data.get("refreshToken") or "").strip() or access
                return access, refresh
    return None, None


def _cookie(user_id: str, jwt: str) -> str:
    return f"WorkosCursorSessionToken={user_id}%3A%3A{jwt}"


def _bearer_headers(jwt: str) -> dict:
    return {
        "authorization": f"Bearer {jwt}",
        "content-type": "application/json",
        "connect-protocol-version": "1",
        "user-agent": UA,
    }


def _cookie_headers(user_id: str, jwt: str, origin: bool = False) -> dict:
    headers = {
        "cookie": _cookie(user_id, jwt),
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": UA,
    }
    if origin:
        headers["origin"] = ORIGIN
    return headers


def _post(url: str, headers: dict, body: str = "{}"):
    try:
        resp = requests.post(url, headers=headers, data=body.encode("utf-8"), timeout=TIMEOUT)
        return resp.status_code, resp.text
    except Exception as exc:
        return 0, str(exc)


def _get(url: str, headers: dict):
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        return resp.status_code, resp.text
    except Exception as exc:
        return 0, str(exc)


def _num(value):
    """接口里的数字字段可能是 int/float/数字字符串；统一成 float，非数字返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _usage_plan_block(body: dict) -> dict:
    """取 usage-summary 里真正带用量的 plan 块：个人号在 individualUsage，团队号可能在 teamUsage。"""
    for key in ("individualUsage", "teamUsage"):
        block = body.get(key)
        if isinstance(block, dict):
            plan = block.get("plan")
            if isinstance(plan, dict) and plan:
                return block
    return {}


def fetch_general_usage_with_code(user_id: str, jwt: str):
    """同 fetch_general_usage，但连 HTTP 状态码一起返回 (status, dict|None)，供判定账号是否失效。"""
    headers = {"cookie": _cookie(user_id, jwt), "accept": "application/json", "user-agent": UA}
    status, text = _get(USAGE_SUMMARY_URL, headers)
    if status != 200:
        return status, None
    try:
        body = json.loads(text)
    except Exception:
        return status, None
    if not isinstance(body, dict):
        return status, None
    return status, _general_usage_fields(body)


def fetch_general_usage(user_id: str, jwt: str):
    """查账号月度额度（GET usage-summary，会话 cookie，GET 无需 Origin）。

    把 Auto 池 / 高级模型(API) 池 / 按量付费拆开返回；totalPercentUsed 只作参考值一起带回，
    UI 不拿它当「用量」显示（它是两池加权混合，和任一池都对不上）。
    """
    return fetch_general_usage_with_code(user_id, jwt)[1]


def _general_usage_fields(body: dict) -> dict:
    block = _usage_plan_block(body)
    plan = block.get("plan") or {}
    on_demand = block.get("onDemand") or {}
    breakdown = plan.get("breakdown") or {}
    return {
        "membership": body.get("membershipType"),
        "unlimited": body.get("isUnlimited"),
        # 三个池子分开：Auto / 高级(API)；Bot 周用量另走 GetSandUsageStatus。
        "autoPercent": _num(plan.get("autoPercentUsed")),
        "apiPercent": _num(plan.get("apiPercentUsed")),
        "totalPercent": _num(plan.get("totalPercentUsed")),
        # 「已含额度」桶（分）：Pro 为 2000/2000；总消费≥$20 时恒满，仅供参考。
        "includedUsedCents": _num(plan.get("used")),
        "includedLimitCents": _num(plan.get("limit")),
        # 账单月总消费（分，含 Bot 与所有模型）——只进 tooltip，不作主显示。
        "cycleTotalCents": _num(breakdown.get("total")),
        # 按量付费：enabled 且 used>0 说明这个号已经在产生真实扣费。
        "onDemandEnabled": bool(on_demand.get("enabled")) if on_demand else None,
        "onDemandUsedCents": _num(on_demand.get("used")),
        # 计费周期：billingCycleEnd 就是本期订阅结束/续费日（真正的“订阅到期”，非 token 有效期）。
        "billingCycleStart": body.get("billingCycleStart"),
        "billingCycleEnd": body.get("billingCycleEnd"),
    }


def fetch_subscription(user_id: str, jwt: str):
    """查订阅状态（GET auth/stripe，会话 cookie）。返回是否在续费、是否待取消、月付/年付。"""
    headers = {"cookie": _cookie(user_id, jwt), "accept": "application/json", "user-agent": UA, "origin": ORIGIN}
    status, text = _get(STRIPE_URL, headers)
    if status != 200:
        return None
    try:
        body = json.loads(text)
    except Exception:
        return None
    return {
        "membership": body.get("membershipType"),
        "subscriptionStatus": body.get("subscriptionStatus"),
        "pendingCancellationDate": body.get("pendingCancellationDate"),
        "isYearlyPlan": body.get("isYearlyPlan"),
    }


def _iso_to_ms(iso):
    """ISO8601（如 2026-08-26T17:22:03.913Z）转毫秒时间戳；失败返回 None。"""
    if not iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _estimate_next_reset(period_start_iso):
    """接口不回 nextResetTimestampUtc 时，按周期起点 + 7 天推算下次重置（ISO，UTC）。"""
    start_ms = _iso_to_ms(period_start_iso)
    if start_ms is None:
        return None
    reset_ms = start_ms + SAND_PERIOD_DAYS * 86400 * 1000
    # 周期起点可能是很久以前的锚点：往后滚到第一个未来的重置点。
    now_ms = int(time.time() * 1000)
    period_ms = SAND_PERIOD_DAYS * 86400 * 1000
    while reset_ms <= now_ms:
        reset_ms += period_ms
    dt = datetime.datetime.fromtimestamp(reset_ms / 1000.0, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def fetch_usage_with_code(jwt: str):
    """同 fetch_usage，但连 HTTP 状态码一起返回 (status, dict|None)。

    api2 对作废的票回 401 + ERROR_NOT_LOGGED_IN，这是判「账号失效」最直接的证据。
    """
    status, text = _post(SAND_USAGE_URL, _bearer_headers(jwt))
    if status != 200:
        return status, None
    try:
        body = json.loads(text)
    except Exception:
        return status, None
    if not isinstance(body, dict):
        return status, None
    return status, _sand_usage_fields(body)


def fetch_usage(jwt: str):
    """查 Sand（Grok Bot）周额度。unlocked=已开通（有非零额度）。

    实测响应：currentPeriodStart / usagePercent / hasAvailableUsage / hasNonZeroIncludedLimit /
    grokPlanLabel；nextResetTimestampUtc 通常缺失，此时按 7 天周期推算并标记 nextResetEstimated。
    """
    return fetch_usage_with_code(jwt)[1]


def _sand_usage_fields(body: dict) -> dict:
    unlocked = (body.get("includedLimitZero") is not True) and (
        body.get("hasNonZeroIncludedLimit") is True
    )
    next_reset = body.get("nextResetTimestampUtc")
    estimated = False
    if not next_reset:
        next_reset = _estimate_next_reset(body.get("currentPeriodStart"))
        estimated = next_reset is not None
    return {
        "unlocked": unlocked,
        "percent": _num(body.get("usagePercent")),
        "hasAvailableUsage": body.get("hasAvailableUsage"),
        "nextReset": next_reset,
        "nextResetEstimated": estimated,
        "periodStart": body.get("currentPeriodStart"),
        "plan": body.get("grokPlanLabel"),
    }


def empty_session_block(error: str = "") -> dict:
    return {
        "sessions": [],
        "sessionCount": 0,
        "sessionClientCount": 0,
        "sessionWebCount": 0,
        "sessionError": error or "",
    }


def _map_session_type(raw) -> str:
    text = str(raw or "").upper()
    if text == "SESSION_TYPE_CLIENT":
        return "client"
    if text == "SESSION_TYPE_WEB":
        return "web"
    return "other"


def normalize_sessions(payload) -> dict:
    """把 /api/auth/sessions 的 JSON 收成 UI 字段。非法 payload 返回空列表 + sessionError。"""
    if not isinstance(payload, dict):
        return empty_session_block("会话数据无法解析")
    raw_list = payload.get("sessions")
    if not isinstance(raw_list, list):
        return empty_session_block("会话数据无法解析")
    items = []
    client_n = 0
    web_n = 0
    for row in raw_list:
        if not isinstance(row, dict):
            continue
        sid = row.get("sessionId")
        if not isinstance(sid, str) or not sid.strip():
            continue
        type_raw = row.get("type") if isinstance(row.get("type"), str) else ""
        mapped = _map_session_type(type_raw)
        if mapped == "client":
            client_n += 1
        elif mapped == "web":
            web_n += 1
        created = row.get("createdAt") if isinstance(row.get("createdAt"), str) else None
        expires = row.get("expiresAt") if isinstance(row.get("expiresAt"), str) else None
        items.append(
            {
                "sessionId": sid.strip(),
                "type": mapped,
                "typeRaw": type_raw,
                "createdAt": created,
                "expiresAt": expires,
            }
        )
    return {
        "sessions": items,
        "sessionCount": len(items),
        "sessionClientCount": client_n,
        "sessionWebCount": web_n,
        "sessionError": "",
    }


def fetch_sessions(user_id: str, jwt: str) -> dict:
    """只读拉取云端登录会话。任何失败都返回 empty_session_block，不抛。"""
    headers = {"cookie": _cookie(user_id, jwt), "accept": "application/json", "user-agent": UA}
    status, text = _get(SESSIONS_URL, headers)
    if status == 0:
        return empty_session_block("会话接口无响应")
    if status != 200:
        return empty_session_block(f"会话接口 HTTP {status}")
    try:
        payload = json.loads(text)
    except Exception:
        return empty_session_block("会话数据无法解析")
    return normalize_sessions(payload)


def fetch_access(user_id: str, jwt: str):
    # cursor.com 的 dashboard POST 端点即使是读也要 Origin 过 CSRF，否则 403。
    status, text = _post(ACCESS_STATUS_URL, _cookie_headers(user_id, jwt, origin=True))
    if status != 200:
        return None
    try:
        body = json.loads(text)
    except Exception:
        return None
    return {
        "granted": body.get("state") == "SAND_ACCESS_STATE_GRANTED",
        "state": body.get("state"),
        "blockReason": body.get("blockReason"),
        # 实测：Pro / Pro+ / Ultra（及 SuperGrok）套餐自带 Sand 资格，无需领取；免费号才需要走试用 + 绑卡。
        "planGrantsAccess": body.get("proAndSuperGrokPlansGrantAccess"),
        "isPaidTrialPlan": body.get("isPaidTrialPlan"),
    }


def fetch_team_id(user_id: str, jwt: str):
    """返回 (team_id, email)。非团队账号 team_id 为 None。"""
    status, text = _post(GET_ME_URL, _cookie_headers(user_id, jwt, origin=True))
    if status != 200:
        return None, None
    try:
        body = json.loads(text)
    except Exception:
        return None, None
    team_id = body.get("teamId")
    email = body.get("email")
    return (team_id if isinstance(team_id, int) and team_id > 0 else None), email


def _tier_label(billing_tier):
    """把 TEAM_MEMBER_BILLING_TIER_TIER_2000 归一成短档位标签「T2000」。

    注意：这个数字是 Cursor 的档位/信用点口径，**不是美元金额**（usage-summary 里
    同值出现在 plan.limit，与 bonus/total 同单位的信用点）。真正的美元只有 includedSpendCents。
    """
    raw = str(billing_tier or "")
    match = re.search(r"(\d+)\s*$", raw)
    if match:
        return "T" + match.group(1)
    short = raw.replace("TEAM_MEMBER_BILLING_TIER_", "").replace("TIER_", "").strip()
    return short or None


def fetch_team_spend(user_id: str, jwt: str, team_id: int):
    """查团队每个成员的绝对额度：档位($)/已用($)/用量%。返回成员列表；失败返回 None。

    合并后 Grok Bot 用量走 cursor.com 团队账单，body 必须带 teamId，否则 401「Team ID is required」。
    """
    body = json.dumps({"teamId": team_id})
    status, text = _post(TEAM_SPEND_URL, _cookie_headers(user_id, jwt, origin=True), body)
    if status != 200:
        return None
    try:
        rows = json.loads(text).get("teamMemberSpend")
    except Exception:
        return None
    return rows if isinstance(rows, list) else None


def _spend_row_for(rows, email, user_id):
    """在 team-spend 列表里按邮箱（优先）或数字 userId 匹配当前账号那一行。"""
    if not rows:
        return None
    want = (email or "").strip().lower()
    for row in rows:
        if want and str(row.get("email", "")).strip().lower() == want:
            return row
    return None


def _spend_fields(row) -> dict:
    """把一行 team-spend 归一成给 UI 用的绝对额度字段。"""
    if not row:
        return {}
    cents = _num(row.get("includedSpendCents"))
    return {
        "billingTier": row.get("billingTier"),
        "tierLabel": _tier_label(row.get("billingTier")),
        "spendUsd": (cents / 100.0) if cents is not None else None,
        "teamPercent": _num(row.get("totalPercentUsed")),
        "autoPercent": _num(row.get("autoPercentUsed")),
        "apiPercent": _num(row.get("apiPercentUsed")),
        "role": row.get("role"),
    }


def start_trial(user_id: str, jwt: str):
    status, text = _post(START_TRIAL_URL, _cookie_headers(user_id, jwt, origin=True))
    if status != 200:
        return "failed", f"HTTP {status}: {text[:160]}"
    low = text.lower()
    if "cardverificationrequired" in low or "card_verification" in low:
        match = re.search(r'"(https://[^"]*(?:checkout|stripe)[^"]*)"', text)
        return "card_required", (match.group(1) if match else "")
    return "activated", ""


def request_team(user_id: str, jwt: str, team_id: int):
    body = json.dumps({"teamId": team_id})
    status, text = _post(TEAM_ACCESS_URL, _cookie_headers(user_id, jwt, origin=True), body)
    if status != 200:
        return "failed", f"HTTP {status}: {text[:160]}"
    # 标记团队 onboarding 完成是幂等辅助调用，失败不影响领取结果。
    _post(TEAM_ONBOARD_URL, _cookie_headers(user_id, jwt, origin=True), body)
    return "team_ok", ""


def _sand_fields(usage) -> dict:
    """GetSandUsageStatus 结果归一成 UI 字段（Bot 周用量池）。"""
    usage = usage or {}
    return {
        "unlocked": usage.get("unlocked"),
        "percent": usage.get("percent"),
        "hasAvailableUsage": usage.get("hasAvailableUsage"),
        "nextReset": usage.get("nextReset"),
        "nextResetEstimated": usage.get("nextResetEstimated"),
        "periodStart": usage.get("periodStart"),
        "plan": usage.get("plan"),
    }


def get_sand_status(token: str) -> dict:
    """只查 Sand（Bot 周用量）这一池，供领取后轻量刷新，不打其它 5 个接口。"""
    _user_id, jwt, _claims = parse_token(token)
    code, usage = fetch_usage_with_code(jwt)
    if usage is None:
        err = {"error": f"Sand 额度接口无响应或 token 无效（HTTP {code}）"}
        if code in (401, 403):
            err.update({"alive": False, "aliveReason": DEAD_REASON})
        return err
    out = _sand_fields(usage)
    out["alive"] = True
    return out


def _safe(fn, *args):
    """线程池里跑单个接口：任何异常都当「没查到」（None），不让一个接口拖垮整行。"""
    try:
        return fn(*args)
    except Exception:
        return None


def alive_from_codes(*codes) -> bool | None:
    """由数据接口的 HTTP 状态码判定账号是否可用。

    任一接口 200 → True（票被服务端接受）；否则任一 401/403 → False（票作废 / 被封）；
    全是网络失败（0）或其它码 → None（无法判定）。
    """
    if any(c == 200 for c in codes):
        return True
    if any(c in (401, 403) for c in codes):
        return False
    return None


DEAD_REASON = "服务端拒绝（401/403）：账号已失效、被封或票已作废"


def get_status(token: str, sand=None) -> dict:
    """查询单个账号的完整状态（只读），供 UI 展示：Bot 周用量 / Auto / 高级(API) 三池分开返回。

    四个接口互不依赖，并发打（单个账号从 4 次串行往返缩到 1 次）；team-spend 依赖 teamId，随后再打。
    另并发拉 GET /api/auth/sessions；失败不影响 alive。
    sand 可传入已经拿到的 (status, usage)，避免 verify() 二次确认后重复打一次 Sand 接口。
    结果里带 alive：由 Sand / usage-summary 的真实状态码判定（见 alive_from_codes）。
    """
    user_id, jwt, claims = parse_token(token)
    with ThreadPoolExecutor(max_workers=6) as pool:
        f_usage = None if sand is not None else pool.submit(_safe, fetch_usage_with_code, jwt)
        f_team = pool.submit(_safe, fetch_team_id, user_id, jwt)
        f_general = pool.submit(_safe, fetch_general_usage_with_code, user_id, jwt)
        f_sub = pool.submit(_safe, fetch_subscription, user_id, jwt)
        f_access = pool.submit(_safe, fetch_access, user_id, jwt)
        f_sess = pool.submit(_safe, fetch_sessions, user_id, jwt)
        sand_code, usage = sand if sand is not None else (f_usage.result() or (0, None))
        team_id, email = f_team.result() or (None, None)
        general_code, general = f_general.result() or (0, None)
        general = general or {}
        sub = f_sub.result() or {}
        access = f_access.result() or {}
        sess = f_sess.result() or empty_session_block("会话接口无响应")
    alive = alive_from_codes(sand_code, general_code)
    resolved_email = email or claims.get("email") or user_id
    # 团队账号：从 team-spend 拿绝对额度（档位$/已用$/用量%）；个人号无 teamId 跳过。
    spend = {}
    if team_id is not None:
        rows = fetch_team_spend(user_id, jwt, team_id)
        spend = _spend_fields(_spend_row_for(rows, resolved_email, user_id))

    def pick(*values):
        for value in values:
            if value is not None:
                return value
        return None

    result = {
        "email": resolved_email,
        "teamId": team_id,
        "membership": pick(general.get("membership"), sub.get("membership")),
        "unlimited": general.get("unlimited"),
        # —— 池 2/3：Auto 与 高级(API)，账单月口径；个人号来自 usage-summary，团队号回退 team-spend。
        "autoPercent": pick(general.get("autoPercent"), spend.get("autoPercent")),
        "apiPercent": pick(general.get("apiPercent"), spend.get("apiPercent")),
        # 混合值，仅供 tooltip 参考。
        "totalPercent": pick(general.get("totalPercent"), spend.get("teamPercent")),
        "includedUsedCents": general.get("includedUsedCents"),
        "includedLimitCents": general.get("includedLimitCents"),
        "cycleTotalCents": general.get("cycleTotalCents"),
        "onDemandEnabled": general.get("onDemandEnabled"),
        "onDemandUsedCents": general.get("onDemandUsedCents"),
        # 真实订阅：本期结束/续费日 + 续费状态；tokenExp 是 token 60 天有效期（区别于订阅）。
        "billingCycleStart": general.get("billingCycleStart"),
        "billingCycleEnd": general.get("billingCycleEnd"),
        "subscriptionStatus": sub.get("subscriptionStatus"),
        "pendingCancellationDate": sub.get("pendingCancellationDate"),
        "isYearlyPlan": sub.get("isYearlyPlan"),
        "tokenExp": claims.get("exp"),
        "billingTier": spend.get("billingTier"),
        "tierLabel": spend.get("tierLabel"),
        "spendUsd": spend.get("spendUsd"),
        "teamPercent": spend.get("teamPercent"),
        # 有效性：来自真实数据接口的状态码，而不是猜。
        "alive": alive,
        "aliveReason": DEAD_REASON if alive is False else "",
        # Sand 资格（权威口径 get-sand-access-status）：granted=已授予；planGrantsAccess=付费套餐自带、无需领取。
        "accessGranted": access.get("granted"),
        "accessState": access.get("state"),
        "blockReason": access.get("blockReason"),
        "planGrantsAccess": access.get("planGrantsAccess"),
        "sessions": sess.get("sessions") or [],
        "sessionCount": int(sess.get("sessionCount") or 0),
        "sessionClientCount": int(sess.get("sessionClientCount") or 0),
        "sessionWebCount": int(sess.get("sessionWebCount") or 0),
        "sessionError": sess.get("sessionError") or "",
    }
    # —— 池 1：Bot 周用量（Sand）。
    result.update(_sand_fields(usage))
    return result


def verify(token: str) -> dict:
    """验证账号：先看 token 是否过期（离线），再探活（auth/me）+ 拉完整状态，综合判定有效性。

    返回统一带 alive / aliveReason / checkedAt：
      alive=True   账号可用，其余字段同 get_status
      alive=False  已失效（token 过期 或 服务端拒绝），不再浪费其余接口
      alive=None   网络等原因无法判定，仍尽力带回拉到的数据

    判定顺序：
      1. JWT exp 已过 → 直接失效（离线、零请求）。
      2. auth/me 探活说 dead（401/403/204）→ 再用 api2 的 Sand 接口二次确认：api2 也不给 200 才判死，
         api2 200 则视为 cursor.com 偶发抽风，按活号继续拉状态（复用这次 Sand 结果，不重复请求）。
      3. 其余情况以数据接口的真实状态码为准（alive_from_codes）；探活 200 可兜底判活。
    """
    checked_at = int(time.time())
    try:
        user_id, jwt, claims = parse_token(token)
    except Exception as exc:
        return {"alive": False, "aliveReason": f"token 无法解析：{exc}", "checkedAt": checked_at}
    exp = claims.get("exp")
    fallback_email = claims.get("email") or user_id
    if isinstance(exp, (int, float)) and exp <= checked_at:
        return {
            "alive": False,
            "aliveReason": "登录票已过期（JWT exp 已过）",
            "checkedAt": checked_at,
            "email": fallback_email,
            "tokenExp": exp,
        }
    probe = _safe(probe_token_alive, token) or "unknown"
    sand = None
    if probe == "dead":
        sand = _safe(fetch_usage_with_code, jwt) or (0, None)
        if sand[0] != 200:
            return {
                "alive": False,
                "aliveReason": DEAD_REASON,
                "checkedAt": checked_at,
                "email": fallback_email,
                "tokenExp": exp,
            }
    status = get_status(token, sand=sand)
    alive = status.get("alive")
    if alive is None and probe == "alive":
        alive = True
    status["alive"] = alive
    if alive is True:
        status["aliveReason"] = ""
    elif alive is False:
        status["aliveReason"] = DEAD_REASON
    else:
        status["aliveReason"] = "接口无响应（网络问题？），以下为尽力拉取的数据"
    status["checkedAt"] = checked_at
    return status


def claim(token: str) -> dict:
    """领取 Sand：已开通短路；能读到 teamId 走团队通道（带 teamId）；否则个人试用；免费号返回需绑卡。

    票已作废（Sand 接口 401/403）直接返回 outcome=dead，不再往 cursor.com 打领取请求。
    """
    user_id, jwt, claims = parse_token(token)
    sand_code, usage = fetch_usage_with_code(jwt)
    if sand_code in (401, 403):
        return {
            "outcome": "dead",
            "email": claims.get("email") or user_id,
            "detail": f"登录票已失效（HTTP {sand_code}），无法领取；请重新导入该号的新 token",
        }
    # 提前取 teamId + 真实 email（get-me），让每个返回分支都能带上邮箱。
    team_id, me_email = fetch_team_id(user_id, jwt)
    email = me_email or claims.get("email") or user_id
    if usage and usage.get("unlocked"):
        return {"outcome": "already", "email": email, "teamId": team_id, "percent": usage.get("percent"), "detail": "已开通"}
    access = fetch_access(user_id, jwt)
    if access and access.get("granted"):
        return {"outcome": "already", "email": email, "teamId": team_id, "detail": "已授予资格"}
    if team_id is not None:
        outcome, detail = request_team(user_id, jwt, team_id)
        return {
            "outcome": "team_ok" if outcome == "team_ok" else "failed",
            "email": email,
            "teamId": team_id,
            "detail": detail or "团队已请求/开通",
        }
    outcome, detail = start_trial(user_id, jwt)
    if outcome == "activated":
        return {"outcome": "activated", "email": email, "detail": "个人已开通"}
    if outcome == "card_required":
        return {"outcome": "card_required", "email": email, "detail": "免费账号需先验证信用卡", "url": detail}
    return {"outcome": "failed", "email": email, "detail": detail}
