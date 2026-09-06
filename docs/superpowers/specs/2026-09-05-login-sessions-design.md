# SandClaimer 登录会话识别 · 设计规格

**日期**：2026-09-05  
**状态**：待用户审阅书面规格  
**范围**：`dtb_cusor-bot-sand` 第一期（方案 1，只读）  
**代号**：login-sessions

---

## 0. 决策摘要（已确认）

| 决策点 | 结论 |
|--------|------|
| 产品目标 | 自动识别「当前账号在 Cursor 云端有哪些登录会话」，并标出本机 Cursor 正在用列表里的哪一个号 |
| 触发方式 | 挂进现有「验证账号」（含导入后自动验证）。不另做批量「扫设备」按钮 |
| 鉴权 | 只用该号已有的 `WorkosCursorSessionToken`（与 `auth/me`、`usage-summary` 相同）。**不用** `crsr_` API key |
| 接口 | `GET https://cursor.com/api/auth/sessions`（本机企业号实测 200） |
| 展示 | 账号列 meta 增加 pill：`N 会话 · 客户端x / 网页y`；点 pill 打开玻璃弹层看明细 |
| 本机标记 | 账号列加「本机」pill：本机 `state.vscdb` 里的 user id 与该行 id 相同 |
| 踢会话 | **不做** |
| 电脑名 / IP / OS | **不展示**（接口不返回这些字段，禁止编造） |
| 机器码跨号关联（原需求 C） | **不做**（第二期） |
| 领取后轻量刷 Bot | **不**额外打 sessions |

---

## 1. 问题与目标

### 1.1 问题

号池里同一个 Cursor 账号可能同时登在客户端和网页。现有验证只看票是否有效、套餐和三池用量，看不出这个号此刻有几条云端会话。本机「探测本机账号」能把本机号加进列表，但列表里没有「哪一行就是本机正在用的号」的持续标记。

### 1.2 成功标准

1. 点「验证账号」（或导入后自动验证）后，有效账号行出现会话 pill：数量 + 客户端/网页拆分。
2. 点 pill 弹出明细：每条会话的类型、创建时间、过期时间；`sessionId` 只显示前 8 位，完整值放在 `title` 里。
3. 本机 Cursor 已登录且该号在列表中时，对应行有「本机」标记；切号或再探测后标记跟着变。
4. sessions 接口失败或超时 **不改变** 该号的有效/失效判定，也不冲掉套餐/用量。
5. 失效号（JWT 过期或探活已判死、提前返回）不打 sessions。
6. 不出现踢会话、重置机器码、调用 `crsr_` key 的任何新路径。

### 1.3 非目标（本规格明确不做）

- 踢掉 / 登出任意会话。
- 展示电脑名、操作系统、IP、地理位置、UA。
- 用 `crsr_` Cloud Agents / Admin API 拉设备。
- 对比 `telemetry.machineId` 做跨号关联（原选项 C）。
- 在「领取」或 `sand_status_one` 路径上拉 sessions。
- 改补丁 / 切号 / 导出 txt 的格式（导出行仍是 `邮箱----user_id::token`）。

---

## 2. 实测接口契约

本机 Cursor 登录号（会话票，`JWT.type=session`）实测：

```http
GET https://cursor.com/api/auth/sessions
Cookie: WorkosCursorSessionToken={userId}%3A%3A{jwt}
Accept: application/json
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)
```

成功响应（200，`application/json`）：

```json
{
  "sessions": [
    {
      "sessionId": "64-char-hex",
      "type": "SESSION_TYPE_CLIENT",
      "createdAt": "2026-09-04T23:37:40.000Z",
      "expiresAt": "2026-11-03T23:37:40.000Z"
    },
    {
      "sessionId": "64-char-hex",
      "type": "SESSION_TYPE_WEB",
      "createdAt": "2026-09-05T00:19:51.000Z",
      "expiresAt": "2026-11-04T00:19:51.000Z"
    }
  ]
}
```

已确认：

- 查询参数 `?details=1` / `?includeDevice=true` / `?full=1` **不会**增加字段。
- `https://api.cursor.com/v1/sessions` 对 `crsr_` key 返回 404。`crsr_` 只能打 `/v1/me`，与本功能无关。
- 当前会话 JWT **没有** `sid` / `sessionId` claim，无法把「当前这张票」精确对上列表里的某一条。因此弹层 **不** 标「当前这条」，只标账号级「本机」（见 §4）。

类型枚举按实测两值处理；未知 `type` 原样保留并在 UI 显示为「其他」。

---

## 3. 架构

```
验证账号
  └─ sand_api.verify()
       ├─ 早退（解析失败 / JWT 过期 / 探活死号）→ 不打 sessions
       └─ get_status()
            ThreadPoolExecutor（现有 5 路 + sessions 共 6 路）
              ├─ Sand 用量
              ├─ get-me
              ├─ usage-summary
              ├─ stripe 订阅
              ├─ sand-access
              └─ GET /api/auth/sessions   ← 新增
```

本机标记不走云端：

```
启动 / 探测本机 / 切号完成
  └─ local_cursor.read_local_account()
       └─ parse_token → localUserId
            前端：accounts[].id === localUserId → 显示「本机」
```

---

## 4. 数据形状

`get_status` / `verify` 成功路径在现有字段之外增加：

```python
{
  "sessions": [
    {
      "sessionId": str,          # 完整 64 hex，供 title；UI 默认只显示前 8 位
      "type": "client" | "web" | "other",
      "typeRaw": str,            # 接口原值，如 SESSION_TYPE_CLIENT
      "createdAt": str | None,   # ISO-8601，解析失败则为 None
      "expiresAt": str | None,
    }
  ],
  "sessionCount": int,
  "sessionClientCount": int,
  "sessionWebCount": int,
  "sessionError": str,           # 拉取失败时非空；成功为空字符串
}
```

映射：

- `SESSION_TYPE_CLIENT` → `client`
- `SESSION_TYPE_WEB` → `web`
- 其它非空字符串 → `other`（计入 `sessionCount`，不计入 client/web）
- `sessions` 不是 list、或缺字段：该条丢弃，不让整次验证失败

失败（HTTP ≠ 200、JSON 解析失败、网络 0）：

- `sessions`: `[]`
- `sessionCount` / `sessionClientCount` / `sessionWebCount`: `0`
- `sessionError`: 短中文，例如 `会话接口 HTTP 401` / `会话接口无响应`

`alive` **只**继续由现有 `alive_from_codes(sand_code, general_code)` 决定，sessions 的状态码 **不**参与。

早退的死号响应 **不**带 `sessions` 键；前端沿用旧值或显示「—」。

本机身份由独立桥接方法提供，不塞进每个账号的 verify 结果：

```python
# Api.local_identity() →
{"ok": True, "userId": "user_...", "email": "..."}  # 本机已登录
{"ok": False, "userId": None, "email": None}        # 未登录 / 读不到
```

`userId` 与账号表主键相同（`parse_token` 的 `user_` id）。

---

## 5. UI

### 5.1 账号列

现有 meta 行（有效性 pill、网站会话、导入/验证时间）追加：

1. **本机** pill（`pill info mini`）：`localUserId === account.id`。title：`本机 Cursor 当前登录的是这个号`。
2. **会话** pill：
   - 有数据：`2 会话 · 客户端1 / 网页1`（网页为 0 时仍写出，避免歧义）。
   - `sessionError` 非空：`会话 —`，title 为错误文案。
   - 尚未验证、无 `sessions` 键：不显示会话 pill。
   - 失效号：不显示会话 pill（与早退一致）。

会话 pill 可点（`data-act="sessions"`），打开弹层。本机 pill 不可点。

### 5.2 弹层

复用现有 `.modal-mask` / `.modal.glass` 模式，新增 `#sessionMask`，一次只展示一个账号：

- 标题：`登录会话 · {邮箱或 id}`
- 列表：类型（客户端 / 网页 / 其他）、创建、过期、id 前 8 位
- 脚注一行：`Cursor 接口不提供电脑名或 IP。本机标记来自本机登录库，无法对应到上面某一条会话。`
- 关闭按钮「知道了」

时间格式与现有 `fmtTs` 一致。非法日期显示 `—`。

### 5.3 本机身份刷新时机

调用 `local_identity()`：

- `boot()` 拉完账号列表之后
- `detectLocal()` 成功之后
- `switch_account` 成功返回之后

失败则清掉 `localUserId`，所有「本机」pill 消失。

### 5.4 帮助文案

使用说明「常用操作」增加一条，说明验证会同时拉云端会话数量，点 pill 看明细；不踢设备；没有电脑名。

---

## 6. 错误处理

| 情况 | 行为 |
|------|------|
| sessions HTTP 200 且 JSON 合法 | 写入列表与计数，`sessionError=""` |
| sessions HTTP 401/403 | 计数 0，`sessionError` 说明被拒绝；**不**因此把号标失效（票可能只是 web/client 通道差异；alive 仍看用量接口） |
| 网络失败 / 超时 | 计数 0，`sessionError=会话接口无响应` |
| 响应不是对象或 `sessions` 不是数组 | 同上，`sessionError=会话数据无法解析` |
| verify 早退死号 | 不请求 sessions |
| 本机未登录 | 无「本机」pill，会话功能不受影响 |

并发：`get_status` 的 `max_workers` 从 5 改为 6。sessions 失败不得抛出到 `verify()` 顶层。

---

## 7. 测试

仓库 README 写有 `test_accounts_usage.py`，当前目录下 **没有** 该文件。本功能新增 `test_login_sessions.py`（不联网），至少覆盖：

1. 把实测形态的 JSON 解析成 `client`/`web` 计数。
2. 未知 `type` → `other`，计入 `sessionCount`。
3. 缺字段的元素被跳过。
4. 非 200 / 非法 JSON → 空列表 + `sessionError`，且 **不**把 `alive` 设为 False（用 fake `_get`）。
5. `get_status` 在 mock 下会带上 `sessions` 键（sessions 与其它接口一并 mock）。

不强制做 UI 截图；实现后用现有桌面窗口点一次验证，人工看 pill 与弹层。

---

## 8. 改动文件

| 文件 | 职责 |
|------|------|
| `sand_api.py` | `SESSIONS_URL`、`fetch_sessions`、`_normalize_sessions`；`get_status` 并发拉取并写入 §4 字段 |
| `app.py` | `local_identity()` 桥接 |
| `local_cursor.py` | 无需新写盘；`read_local_account` 已够。如需避免重复 parse，可加只读 `local_user_id()` 小函数 |
| `web/app.js` | `applyStatus` 合并 sessions；渲染 pill；弹层；刷新 `localUserId` |
| `web/index.html` | `#sessionMask` 弹层 + 帮助文案 |
| `web/style.css` | 弹层列表的最小样式，沿用玻璃风，不加新主题 |
| `test_login_sessions.py` | §7 |
| `README.md` | 「用到的官方接口」表增加一行 sessions；功能列表补一句 |

不改：`sand_patch.py`、切号写库、导出 txt 分段逻辑、`crsr_` 任何调用。

---

## 9. 第二期（本规格不实施）

- 踢会话（需单独确认 revoke 接口，避免误踢本机）。
- 机器码跨号关联。
- 若 Cursor 日后在 sessions 响应里增加 hostname / IP，再扩展字段，不提前占位。

---

## 10. 规格自检

- 无「待定 / TODO」占位。
- 接口、鉴权、UI、错误、测试、文件清单与 §0 决策一致。
- 范围可被一份实现计划覆盖。
- 「本机」指定为账号级匹配，不假装能对应到某一条 `sessionId`。
