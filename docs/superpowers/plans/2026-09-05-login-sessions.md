# 登录会话识别 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。

**目标：** 验证账号时并发拉取 Cursor 云端登录会话，在账号列显示数量与客户端/网页拆分，点开弹层看明细，并标出本机当前登录号；不踢会话。  
**架构：** `sand_api.fetch_sessions` 打 `GET https://cursor.com/api/auth/sessions`（会话 cookie，与现有 verify 相同），结果并入 `get_status`；本机标记由 `Api.local_identity()` 读 `state.vscdb` 后与账号 id 比对。  
**技术栈：** 现有 Python 3 + requests + pywebview 前端（`web/app.js`）。测试用 unittest，不联网。  
**规格：** `dtb_cusor-bot-sand/docs/superpowers/specs/2026-09-05-login-sessions-design.md`

**Commit 约定：** 仅当用户明确要求 commit 时执行各任务的 git commit 步骤；未要求则跳过 commit，继续下一任务。

**落地：** 功能已随 1.2.1 合入；1.2.2 用 `test_login_sessions.py` 做不联网回归。第二期（踢会话等）见迭代 PRD 1.5.0，本计划无对应任务。

---

## 文件

| 文件 | 职责 |
|------|------|
| `dtb_cusor-bot-sand/test_login_sessions.py` | 新建。不联网单测：归一化、fetch_sessions、get_status 带上 sessions 且 alive 不跟 sessions 走 |
| `dtb_cusor-bot-sand/sand_api.py` | `SESSIONS_URL`、`empty_session_block`、`normalize_sessions`、`fetch_sessions`；`get_status` 第 6 路并发 |
| `dtb_cusor-bot-sand/app.py` | `Api.local_identity()` |
| `dtb_cusor-bot-sand/web/index.html` | 会话弹层 + 帮助一条 |
| `dtb_cusor-bot-sand/web/style.css` | 可点 pill 按钮、会话列表 |
| `dtb_cusor-bot-sand/web/app.js` | 合并 sessions、本机 pill、弹层 |
| `dtb_cusor-bot-sand/README.md` | 接口表 + 功能一句 |

不改：`sand_patch.py`、切号写库、导出 txt、任何 `crsr_` 调用。

---

## 任务 1：失败测试 — 会话归一化与 fetch_sessions

**文件：**
- 创建：`dtb_cusor-bot-sand/test_login_sessions.py`

**步骤 1：写测试文件**

```python
"""登录会话识别：不联网。"""

import base64
import json
import unittest
from unittest.mock import patch

import sand_api


def _jwt(sub="user_01TESTSESSIONS000000000000"):
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub, "type": "session"}).encode())
        .decode()
        .rstrip("=")
    )
    return "eyJhbGciOiJub25lIn0." + payload + ".x"


SAMPLE = {
    "sessions": [
        {
            "sessionId": "1c3c233a6194e5eb6463e48122d093c0537507a385c6475dc1485884c511f2a6",
            "type": "SESSION_TYPE_CLIENT",
            "createdAt": "2026-09-04T23:37:40.000Z",
            "expiresAt": "2026-11-03T23:37:40.000Z",
        },
        {
            "sessionId": "9c5904911dd4a73f2883bd6a6f391e4d1d25422e653ede8c46613af700838eed",
            "type": "SESSION_TYPE_WEB",
            "createdAt": "2026-09-05T00:19:51.000Z",
            "expiresAt": "2026-11-04T00:19:51.000Z",
        },
    ]
}


class NormalizeSessionsTest(unittest.TestCase):
    def test_client_and_web_counts(self):
        block = sand_api.normalize_sessions(SAMPLE)
        self.assertEqual(block["sessionCount"], 2)
        self.assertEqual(block["sessionClientCount"], 1)
        self.assertEqual(block["sessionWebCount"], 1)
        self.assertEqual(block["sessionError"], "")
        types = [x["type"] for x in block["sessions"]]
        self.assertEqual(types, ["client", "web"])
        self.assertEqual(block["sessions"][0]["typeRaw"], "SESSION_TYPE_CLIENT")
        self.assertEqual(block["sessions"][0]["sessionId"], SAMPLE["sessions"][0]["sessionId"])

    def test_unknown_type_counts_as_other(self):
        block = sand_api.normalize_sessions(
            {"sessions": [{"sessionId": "ab", "type": "SESSION_TYPE_MOBILE"}]}
        )
        self.assertEqual(block["sessionCount"], 1)
        self.assertEqual(block["sessionClientCount"], 0)
        self.assertEqual(block["sessionWebCount"], 0)
        self.assertEqual(block["sessions"][0]["type"], "other")

    def test_skips_bad_items(self):
        block = sand_api.normalize_sessions(
            {"sessions": [None, 3, {"sessionId": "ok", "type": "SESSION_TYPE_WEB"}]}
        )
        self.assertEqual(block["sessionCount"], 1)
        self.assertEqual(block["sessionWebCount"], 1)

    def test_bad_payload(self):
        block = sand_api.normalize_sessions("nope")
        self.assertEqual(block["sessions"], [])
        self.assertEqual(block["sessionCount"], 0)
        self.assertTrue(block["sessionError"])


class FetchSessionsTest(unittest.TestCase):
    def test_http_200(self):
        with patch.object(sand_api, "_get", return_value=(200, json.dumps(SAMPLE))):
            block = sand_api.fetch_sessions("user_01TESTSESSIONS000000000000", _jwt())
        self.assertEqual(block["sessionCount"], 2)
        self.assertEqual(block["sessionError"], "")

    def test_http_401_does_not_raise(self):
        with patch.object(sand_api, "_get", return_value=(401, '{"error":"no"}')):
            block = sand_api.fetch_sessions("user_01TESTSESSIONS000000000000", _jwt())
        self.assertEqual(block["sessions"], [])
        self.assertIn("401", block["sessionError"])

    def test_invalid_json(self):
        with patch.object(sand_api, "_get", return_value=(200, "not-json")):
            block = sand_api.fetch_sessions("user_01TESTSESSIONS000000000000", _jwt())
        self.assertEqual(block["sessions"], [])
        self.assertTrue(block["sessionError"])

    def test_network_zero(self):
        with patch.object(sand_api, "_get", return_value=(0, "timeout")):
            block = sand_api.fetch_sessions("user_01TESTSESSIONS000000000000", _jwt())
        self.assertEqual(block["sessionCount"], 0)
        self.assertIn("无响应", block["sessionError"])


class GetStatusSessionsTest(unittest.TestCase):
    def _dispatch(self, url, headers, body="{}"):
        if url == sand_api.SAND_USAGE_URL:
            return 200, json.dumps({"hasNonZeroIncludedLimit": True, "usagePercent": 1})
        if url == sand_api.GET_ME_URL:
            return 200, json.dumps({"email": "a@b.c"})
        if url == sand_api.ACCESS_STATUS_URL:
            return 200, json.dumps({"state": "SAND_ACCESS_STATE_GRANTED"})
        if url == sand_api.TEAM_SPEND_URL:
            return 200, json.dumps({"teamMemberSpend": []})
        return 200, "{}"

    def _get(self, url, headers):
        if url == sand_api.SESSIONS_URL:
            return 200, json.dumps(SAMPLE)
        if url == sand_api.USAGE_SUMMARY_URL:
            return 200, json.dumps(
                {
                    "membershipType": "pro",
                    "individualUsage": {"plan": {"autoPercentUsed": 1, "apiPercentUsed": 2}},
                }
            )
        if url == sand_api.STRIPE_URL:
            return 200, json.dumps({"membershipType": "pro", "subscriptionStatus": "active"})
        return 404, ""

    def test_get_status_includes_sessions_alive_from_usage(self):
        with patch.object(sand_api, "_post", side_effect=self._dispatch), patch.object(
            sand_api, "_get", side_effect=self._get
        ):
            out = sand_api.get_status(_jwt())
        self.assertEqual(out["sessionCount"], 2)
        self.assertEqual(out["sessionClientCount"], 1)
        self.assertTrue(out["alive"])
        self.assertEqual(out["sessionError"], "")

    def test_sessions_401_does_not_kill_account(self):
        def get(url, headers):
            if url == sand_api.SESSIONS_URL:
                return 401, "{}"
            return self._get(url, headers)

        with patch.object(sand_api, "_post", side_effect=self._dispatch), patch.object(
            sand_api, "_get", side_effect=get
        ):
            out = sand_api.get_status(_jwt())
        self.assertTrue(out["alive"])
        self.assertEqual(out["sessionCount"], 0)
        self.assertIn("401", out["sessionError"])


if __name__ == "__main__":
    unittest.main()
```

**步骤 2：跑测试，确认失败**

```bash
cd /Users/soaringsoul/MyLocalGithubWorkstation/ditubang_web_servers/dtb_cusor-bot-sand
python3 -m unittest test_login_sessions.py -q
```

预期：`ImportError` 或 `AttributeError`（还没有 `normalize_sessions` / `fetch_sessions` / `SESSIONS_URL`）。

**步骤 3：** 不要在这一步改 `sand_api.py`。

---

## 任务 2：实现 sand_api 会话拉取并让测试通过

**文件：**
- 修改：`dtb_cusor-bot-sand/sand_api.py`

**步骤 1：在常量区 `AUTH_ME_URL` 下一行加**

```python
SESSIONS_URL = "https://cursor.com/api/auth/sessions"
```

文件头注释的端点列表加一行：

```
  - 登录会话：GET https://cursor.com/api/auth/sessions（会话 cookie）
```

**步骤 2：在 `fetch_access` 之前插入以下函数（不要插到 `get_status` 后面，测试与 `get_status` 都要能 import 到）**

```python
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
```

**步骤 3：改 `get_status`**

把 `ThreadPoolExecutor(max_workers=5)` 改成 `max_workers=6`，并增加 sessions 这一路：

在 `f_access = pool.submit(...)` 后加：

```python
        f_sess = pool.submit(_safe, fetch_sessions, user_id, jwt)
```

在 `access = f_access.result() or {}` 后加：

```python
        sess = f_sess.result() or empty_session_block("会话接口无响应")
```

在 `result = { ... }` 字典里、`planGrantsAccess` 之后加入：

```python
        "sessions": sess.get("sessions") or [],
        "sessionCount": int(sess.get("sessionCount") or 0),
        "sessionClientCount": int(sess.get("sessionClientCount") or 0),
        "sessionWebCount": int(sess.get("sessionWebCount") or 0),
        "sessionError": sess.get("sessionError") or "",
```

不要把 sessions 的 HTTP 码传给 `alive_from_codes`。

**步骤 4：跑测试**

```bash
cd /Users/soaringsoul/MyLocalGithubWorkstation/ditubang_web_servers/dtb_cusor-bot-sand
python3 -m unittest test_login_sessions.py -q
```

预期：`OK`（全部通过）。若 `GetStatusSessionsTest` 因其它 mock 路径失败，只修 mock 或 `get_status` 接线，不要放宽断言。

**步骤 5：** 用户要求 commit 时再提交。

---

## 任务 3：本机身份桥接

**文件：**
- 修改：`dtb_cusor-bot-sand/app.py`

**步骤 1：在 `Api.detect_local_account` 之后增加**

```python
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
```

**步骤 2：冒烟（本机已登录时）**

```bash
cd /Users/soaringsoul/MyLocalGithubWorkstation/ditubang_web_servers/dtb_cusor-bot-sand
python3 -c "import app; print(app.Api().local_identity())"
```

预期：`ok True` 且 `userId` 以 `user_` 开头；未登录则为 `ok False`。不要打印 token。

---

## 任务 4：会话弹层 HTML / CSS / 帮助文案

**文件：**
- 修改：`dtb_cusor-bot-sand/web/index.html`
- 修改：`dtb_cusor-bot-sand/web/style.css`

**步骤 1：在 `index.html` 的 `</ul>`（常用操作列表）里、导出全部那条之后插入**

```html
          <li><b>登录会话</b>：「验证账号」会同时拉取该号在 Cursor 云端的登录会话（客户端 / 网页）。账号列的会话标签可点开看创建与过期时间。接口不提供电脑名或 IP；「本机」标记来自本机 Cursor 登录库。不会踢掉任何会话。</li>
```

**步骤 2：在 `#helpMask` 整块之后、`#toast` 之前插入**

```html
  <div class="modal-mask" id="sessionMask" hidden>
    <div class="modal glass">
      <h2 id="sessionTitle">登录会话</h2>
      <div class="modal-body" id="sessionBody"></div>
      <div class="modal-actions"><button class="btn primary" id="sessionOk">知道了</button></div>
    </div>
  </div>
```

**步骤 3：在 `style.css` 的 `.pill.mini` 规则后追加**

```css
button.pill {
  font: inherit;
  cursor: pointer;
  appearance: none;
  -webkit-appearance: none;
}
button.pill:disabled { cursor: default; }
.session-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.session-list li {
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid var(--field-edge);
  background: var(--field);
}
.session-list .sid { font-family: "SF Mono", Consolas, ui-monospace, monospace; font-size: 11px; }
```

---

## 任务 5：前端合并 sessions、本机标记、弹层

**文件：**
- 修改：`dtb_cusor-bot-sand/web/app.js`

**步骤 1：在文件顶部 `const selected = new Set();` 附近增加**

```javascript
let localUserId = null; // 本机 Cursor 当前登录的 user_ id；未登录为 null
```

**步骤 2：增加刷新与展示辅助函数（放在 `validityPill` 附近）**

```javascript
async function refreshLocalIdentity() {
  try {
    const res = await api().local_identity();
    localUserId = res && res.ok && res.userId ? res.userId : null;
  } catch (e) {
    localUserId = null;
  }
}

function sessionTypeLabel(t) {
  if (t === "client") return "客户端";
  if (t === "web") return "网页";
  return "其他";
}

function sessionPills(a, st) {
  const bits = [];
  if (localUserId && a.id === localUserId) {
    bits.push(`<span class="pill info mini" title="本机 Cursor 当前登录的是这个号">本机</span>`);
  }
  if (st && st.alive === false) return bits.join("");
  if (!st || !Object.prototype.hasOwnProperty.call(st, "sessionCount")) return bits.join("");
  if (st.sessionError) {
    bits.push(
      `<button type="button" class="pill idle mini" data-act="sessions" data-id="${esc(a.id)}" title="${esc(st.sessionError)}">会话 —</button>`
    );
    return bits.join("");
  }
  const n = st.sessionCount || 0;
  const c = st.sessionClientCount || 0;
  const w = st.sessionWebCount || 0;
  bits.push(
    `<button type="button" class="pill info mini" data-act="sessions" data-id="${esc(a.id)}" title="查看云端登录会话">` +
      `${n} 会话 · 客户端${c} / 网页${w}</button>`
  );
  return bits.join("");
}

function openSessions(id) {
  const a = accounts.find((x) => x.id === id);
  const st = rowState[id] || {};
  const mail = a && a.label && a.label.includes("@") ? a.label : (a && (a.label || a.id)) || id;
  $("sessionTitle").textContent = "登录会话 · " + mail;
  const rows = Array.isArray(st.sessions) ? st.sessions : [];
  let html = "";
  if (st.sessionError) html += `<p>${esc(st.sessionError)}</p>`;
  if (!rows.length && !st.sessionError) html += `<p>没有返回任何会话。</p>`;
  if (rows.length) {
    html += `<ul class="session-list">`;
    for (const s of rows) {
      const sid = s.sessionId || "";
      const short = sid.slice(0, 8);
      html += `<li><div><b>${esc(sessionTypeLabel(s.type))}</b> · <span class="sid" title="${esc(sid)}">${esc(short)}</span></div>` +
        `<div class="hint">创建 ${esc(s.createdAt ? fmtTs(toMs(s.createdAt)) : "—")}　过期 ${esc(s.expiresAt ? fmtTs(toMs(s.expiresAt)) : "—")}</div></li>`;
    }
    html += `</ul>`;
  }
  html += `<p class="hint">Cursor 接口不提供电脑名或 IP。本机标记来自本机登录库，无法对应到上面某一条会话。</p>`;
  $("sessionBody").innerHTML = html;
  $("sessionMask").hidden = false;
}

function hideSessions() {
  $("sessionMask").hidden = true;
}
```

`toMs` 已存在；若 `createdAt` 是 ISO 字符串，现有 `toMs` 必须能吃。打开 `toMs`：若它只处理秒级数字，则在 `openSessions` 里对 ISO 用 `Date.parse`：

若 `toMs` 已是：

```javascript
function toMs(v) {
  const n = Number(v);
  if (!isFinite(n) || n <= 0) return NaN;
  return n < 1e12 ? n * 1000 : n;
}
```

则改 `openSessions` 里时间为：

```javascript
function sessionTimeLabel(iso) {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  return isNaN(ms) ? "—" : fmtTs(ms);
}
```

创建/过期都走 `sessionTimeLabel`，不要用 `toMs` 处理 ISO。

**步骤 3：改 `render()` 的 meta 拼接**

把

```javascript
      const meta =
        `<div class="meta">${validityPill(st)}${tokTag}` +
        `<span title="导入时间">导入 ${esc(addedAt || "—")}</span>` +
        (checkedAt ? `<span title="上次验证时间">验证 ${esc(checkedAt)}</span>` : "") +
        `</div>`;
```

换成

```javascript
      const meta =
        `<div class="meta">${validityPill(st)}${tokTag}${sessionPills(a, st)}` +
        `<span title="导入时间">导入 ${esc(addedAt || "—")}</span>` +
        (checkedAt ? `<span title="上次验证时间">验证 ${esc(checkedAt)}</span>` : "") +
        `</div>`;
```

**步骤 4：改 `applyStatus`，在 `teamPercent` 之后写入**

```javascript
    sessions: Array.isArray(res.sessions) ? res.sessions : [],
    sessionCount: res.sessionCount,
    sessionClientCount: res.sessionClientCount,
    sessionWebCount: res.sessionWebCount,
    sessionError: res.sessionError || "",
```

`applyVerify` 在 `alive === false` 时保持现有早退（不展示会话 pill，因为 `sessionPills` 在 `alive === false` 时只可能输出本机标记）。不要在死号路径上把旧 `sessions` 清掉以外的字段搞乱；`sessionPills` 已经按 `alive === false` 隐藏会话按钮。

**步骤 5：改 `onTableClick`**

现有开头是 `if (!btn || busy) return;`。改成：会话弹层在 busy 时也允许打开（只读缓存）：

```javascript
function onTableClick(e) {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.getAttribute("data-id");
  const act = btn.getAttribute("data-act");
  if (act === "sessions") {
    openSessions(id);
    return;
  }
  if (busy) return;
  // ... 其余分支不变
}
```

**步骤 6：`detectLocal` 成功后、`switchAccount` 成功后、`boot()` 拉完列表后调用 `await refreshLocalIdentity()`。**

`detectLocal` 在 `accounts = res.accounts || [];` 之后：

```javascript
    await refreshLocalIdentity();
    render();
```

`switchAccount` 在 `res && res.ok` 分支里 toast 之后：

```javascript
      await refreshLocalIdentity();
      render();
```

`boot()` 在第一次 `render()` 之前或之后：

```javascript
  await refreshLocalIdentity();
  render();
```

（若已 `render()` 过一次，再 `refreshLocalIdentity` + `render` 一次即可。）

**步骤 7：`boot()` 事件绑定增加**

```javascript
  $("sessionOk").addEventListener("click", hideSessions);
```

---

## 任务 6：README

**文件：**
- 修改：`dtb_cusor-bot-sand/README.md`

**步骤 1：功能列表「验证账号」那条末尾补一句**

`验证时同时拉取云端登录会话（客户端 / 网页数量），账号列可点开看创建与过期时间；本机 Cursor 当前登录的号打「本机」标记。`

**步骤 2：「用到的官方接口」表增加一行**

| 登录会话 | GET | `cursor.com/api/auth/sessions` | 会话 cookie |

**步骤 3：项目结构里若列出测试文件，加上 `test_login_sessions.py`。**

---

## 任务 7：回归验证

**步骤 1：**

```bash
cd /Users/soaringsoul/MyLocalGithubWorkstation/ditubang_web_servers/dtb_cusor-bot-sand
python3 -m unittest test_login_sessions.py -q
```

预期：`OK`。

**步骤 2：** 人工（实现者启动 `python3 app.py`）：对列表里一个有效号点「验证」，账号列出现 `N 会话 · 客户端x / 网页y`；点开弹层能看到类型与时间；本机登录号有「本机」。失效号无会话 pill。不要点任何踢会话（功能不存在）。

---

## 规格覆盖对照

| 规格章节 | 任务 | 落地 |
|----------|------|------|
| §0 / §1 只读验证触发、不用 API key、不踢 | 任务 2、5；无 revoke 代码 | 是 |
| §2 接口契约 | 任务 2 `SESSIONS_URL` + GET cookie | 是 |
| §3 架构并发 + 本机标记 | 任务 2 `max_workers=6`；任务 3–5 | 是 |
| §4 数据形状 | 任务 1–2 | 是 |
| §5 UI pill / 弹层 / 刷新时机 / 帮助 | 任务 4–5 | 是 |
| §6 错误处理、alive 不跟 sessions | 任务 1 `test_sessions_401_does_not_kill_account`、任务 2 | 是 |
| §7 测试文件 | 任务 1 | 是（`test_login_sessions.py`） |
| §8 README | 任务 6 | 是 |
| §9 第二期 | 无任务（正确） | 故意未做 |
