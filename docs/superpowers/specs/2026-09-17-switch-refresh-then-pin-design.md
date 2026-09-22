# 切号先刷票再写入 · 设计规格

**日期**：2026-09-17  
**状态**：已实现  
**范围**：`dtb-cursor-tools`（SandClaimer 桌面端）  
**代号**：switch-refresh-then-pin  
**已选方案**：B（改切号本身：先刷新登录票，再用新票写入 Cursor，只重启一次）

---

## 0. 决策摘要（已确认）

| 决策点 | 结论 |
|--------|------|
| 产品目标 | 添加 token → 验证额度 → 切号 这条主路径里，**既换到新登录票，又让「本机」落在最新那台设备上** |
| 做法 | 切号默认 **先探测并刷新登录票**，成功后再把 **新票** 写入本机 Cursor 并重启。Cursor 与本工具共用新票后，本机 JWT 对准的就是刷票新开的那条客户端 |
| 不采用 | A：切号后再刷票、再二次写入（Cursor 关开两次）；C：只把「本机」标签挪到最新客户端、Cursor 仍用旧票（一键保护会踢 IDE） |
| 单独「刷登录票」 | **保持只换票、不切号、不关 Cursor** |
| 单独「刷票并踢旧」 | **保持现状**：换票成功后只踢本工具旧客户端，不踢 IDE，不切号 |
| 导入 + 自动验证 | **不改** |
| 一键本机保护 | **不改**：仍用本机 Cursor JWT 认 IDE，不靠刷票认本机 |
| 刷新失败 | **中止切号**：不关 Cursor、不写盘。用户可取消勾选「先刷新」后仅切当前票 |
| 踢旧 | 确认框可选；默认勾选；语义与现有「刷票并踢旧」相同（只踢本工具旧客户端） |
| 切号后自动踢光其它电脑 | **不做** |

---

## 1. 问题与目标

### 1.1 问题

当前主路径是：

1. 添加 token（导入后可自动验证额度 / 状态）
2. 点「切号」：把 **列表里现有的票** 写入 Cursor 并重启 → Cursor 登录会话 **A**
3. 再点「探测票 / 刷登录票」：OAuth 换新票，官方新开 Desktop App 会话 **B**

结果：本机 Cursor 仍拿着切号时写入的旧票（A），本工具拿着新票（B）。设备列表里「本机」钉在较旧的 A，「本工具」在最新的 B，看起来像标签没跟上。把「本机」标签硬挪到 B 会让「只留本机 / 一键保护」踢掉真 IDE。

### 1.2 成功标准

1. 点「切号」→ 确认框 **默认勾选**「先刷新登录票再写入本机 Cursor」。
2. 确认后：探测 refresh（若还没有）→ 刷新登录票 **必须成功** → 可选踢本工具旧客户端 → **用新票** 关 Cursor / 写盘 / 开 Cursor。全程只重启一次。
3. 切号成功后立刻拉一次设备：本机 Cursor 已写入的 JWT 与本工具当前票相同，`list_sessions` 把「本机」标在新票对应的那条客户端上（通常与「本工具」同一条，且是刚换出来的那台）。
4. 刷新失败：返回错误，**不**调用关 Cursor / 写登录态 / 开 Cursor。
5. 取消勾选「先刷新」：行为与今天的 `switch_account` 相同（写当前票，不 OAuth）。
6. 账号行上的「刷登录票」「刷票并踢旧」不经过切号，行为不变。

### 1.3 非目标

- 导入后自动切号。
- 切号成功后自动开「一键本机保护」或自动踢掉该号其它电脑上的客户端。
- 把「本机」标签显示到 Cursor 实际未使用的会话上。
- 改探测 / 验证 / 领取 / 本机保护抽屉的主流程。
- 为等 Cursor 窗口完全起来而做轮询等待（写盘后立刻读 `state.vscdb` 即可认本机）。

---

## 2. 关键机制（为什么本机能落到最新设备）

官方 `POST /oauth/token`（`grant_type=refresh_token`）会新登记一台 `SESSION_TYPE_CLIENT`。本工具换票后若 **不** 把新 JWT 写入 Cursor，IDE 会话不会变。

方案 B 的顺序：

```text
探测 refresh → OAuth 换新票（会话 B 出现，账号表已是新 JWT）
  → 可选：踢掉换票前本工具那条旧客户端
  → 把账号表里的新 JWT 写入 state.vscdb 并重启 Cursor
  → list_sessions：本机 JWT == 新票 → 「本机」对准 B
```

Cursor IDE 使用已写入的 session JWT 登录，不会再走一遍本工具那次 OAuth，因此一般 **不会** 再新开一台。若启动后 Cursor 静默续期，按既有结论不会堆 Desktop App，「本机」仍跟 `read_local_account()` 的 JWT。

---

## 3. API 与数据流

### 3.1 扩展 `Api.switch_account`

文件：`dtb-cursor-tools/app.py`

```python
def switch_account(
    self,
    account_id: str,
    reset_machine_id: bool = False,
    refresh_first: bool = True,
    kick_old_tool: bool = False,
) -> dict:
```

兼容：旧前端只传 `(id, resetMid)` 时，`refresh_first` 默认为 **True**（新产品行为）。预览服与测试必须显式传参。

**`refresh_first=True` 时，在现有探活 / 关 Cursor 之前：**

1. 若账号没有 `refreshToken`：调用现有 `probe_refresh_one`。失败则 `{"ok": False, "error": ...}`，其中 error 沿用探测失败文案，并追加「未写入 Cursor。可取消勾选「先刷新登录票」后仅切当前票。」
2. 解析当前 `item["token"]` 为 `old_claims`（失败则空 dict）。
3. 调用现有 `refresh_login_one`。失败则原样返回（`ok: False`），**不得**进入关 Cursor。
4. 若 `kick_old_tool`：用刷新成功返回的 `accessToken`（或刷新后 store 里的新 token）调用现有 `_drop_stale_tool_session_after_refresh`。踢失败只记 `droppedSessionId=""` / `kickError`，**不**阻断切号。
5. 再走今天的切号主体，闸门一律针对 **store 里当前票**（刷新成功后即新票）：过期闸、`probe_token_alive`、必要时 `exchange_web_to_session`、关 Cursor、`write_local_account`、可选重置机器码、开 Cursor。

**`refresh_first=False`：** 完全跳过 1–4，与今天相同。

成功返回在现有字段上增加：

| 字段 | 含义 |
|------|------|
| `refreshed` | 是否实际执行了 OAuth 换票 |
| `usedAccessAsRefresh` | 换票时是否用 access 顶 refresh（现有 refresh 返回值） |
| `droppedSessionId` | 踢掉的旧本工具 sessionId，未踢则为 `""` |
| `kickError` | 勾了踢旧但没踢到时的说明；未勾则为 `""` |
| `pinnedSessionId` | 切号写盘后 `_resolve_pinned_local_session` 的结果；对不上则为 `""` |

`pinnedSessionId`：成功写盘后调用现有 `list_sessions`（内部已 annotate + 排序 + remember bind）。WAF / 网络失败不让切号变 `ok: False`，只让 `pinnedSessionId=""`，可把 `listError` 放进 `warning`。

成功路径 **不要** 把 `accessToken` 回给前端。

### 3.2 前端 `switch_account` 调用

文件：`dtb-cursor-tools/web/app.js`

```javascript
await api().switch_account(id, resetMid, refreshFirst, kickOld);
```

成功 toast 须区分：已刷新 / 仅切号 / 已踢旧 / 认到本机会话 / 有 `warning`。

成功后顺序：`refreshLocalIdentity()` → `applySessionBlock(id, 若返回带 sessions 则用返回值，否则再 list_sessions)` → `render()`。若切号 RPC 已带 `sessions`，前端不必再打一次；规格要求 **后端** 在成功写盘后自己 `list_sessions` 并把 `sessions` 等字段合并进返回值（与 `list_sessions` 的 block 相同的键：`sessions`、`sessionCount`、`sessionClientCount`、`sessionWebCount`、`sessionError`）。这样前端只消费一次返回。

### 3.3 预览桥

文件：`dtb-cursor-tools/preview_server.py`

`switch_account` 读取 `args[2]`=`refresh_first`（缺省 True）、`args[3]`=`kick_old_tool`。若 refresh_first：走与 `refresh_login_one` / `refresh_login_kick_old` 相同的 mock（bump 客户端 createdAt；kick 时丢掉预览「本工具」那条）。然后返回 `refreshed` / `droppedSessionId` / 带 annotate 的 `sessions`。

---

## 4. 切号确认框

### 4.1 文案模块

文件：`dtb-cursor-tools/switch_confirm.py`  
测试：`dtb-cursor-tools/test_switch_confirm.py`

`confirm_lines` 增加关键字参数：

```python
def confirm_lines(
    email: str | None,
    *,
    reset_machine_id: bool = False,
    web_token: bool = False,
    refresh_first: bool = True,
    kick_old_tool: bool = False,
) -> list[str]:
```

在现有「关 Cursor / 写入 / 重启」两句之后：

- `refresh_first=True`：追加「会先换新登录票，再用新票写入 Cursor，本机标签会落在最新那台设备上。」
- `refresh_first=False`：追加「不会换新登录票，写入的是列表里当前这张票。」
- `web_token`、`reset_machine_id` 行保持现有逻辑。
- `kick_old_tool` 不单独占一行说明（由复选框 label 承担），避免和「不会踢 IDE」重复两遍。

### 4.2 弹窗 DOM

文件：`dtb-cursor-tools/web/index.html`

在 `#switchConfirmBody` 文案下方增加两个复选框（切号确认框内，不复用顶栏「切号重置机器码」）：

| id | 默认 | label |
|----|------|--------|
| `switchRefreshFirstChk` | 勾选 | 先刷新登录票再写入本机 Cursor |
| `switchKickOldChk` | 勾选 | 踢掉刷新前本工具的旧客户端（不会踢 Cursor IDE） |

`switchKickOldChk` 在「先刷新」未勾选时 `disabled`，提交时视为 `false`。

`web/app.js` 的 `switchConfirmCopy` 与 `openSwitchConfirm` 必须与 `switch_confirm.confirm_lines` 使用同一套条件（可用 bridge 暴露 `switch_confirm_lines`，或前端继续手写但测试锁 Python 文案、前端字符串与规格例句一致）。**选定：前端继续手写平行文案**，Python 模块继续给单测锁句子；两边句子必须与 §4.1 相同，禁止各写各的意思。

确认按钮仍为「确认切号」。

### 4.3 帮助

`web/index.html` 使用说明「探测 Refresh / 刷新登录票」与切号相关句改为：切号默认先换新票再写入；取消勾选则只切当前票。不要承诺会踢掉其它电脑上的设备。

---

## 5. 错误处理

| 情况 | 行为 |
|------|------|
| 账号不存在 / token 解析失败 | 与今天切号相同，不刷新 |
| 先刷新但探测不到 refresh | 不切号；error 含探测失败原因 +「可取消勾选先刷新后仅切当前票」 |
| OAuth 换票失败 | 不切号；返回 `refresh_login_one` 的 error |
| 换票成功但踢旧对不上 | 继续切号；`droppedSessionId=""`，`kickError` 沿用现有说明 |
| 换票成功但探活判死 / 已过期 | 不关 Cursor（今天切号闸仍在刷新之后执行）。此时账号表 **已经** 被 `refresh_login_one` 写成新票。接受这个副作用：票已新，只是没写入 Cursor。toast / error 用今天的过期 / 失效文案 |
| 关 Cursor / 写盘 / 开 Cursor 失败 | 与今天相同；票若已刷新则保持新票 |
| 写盘成功但 `list_sessions` 失败 | 切号 `ok: True`，`warning` 说明设备列表未刷新，`pinnedSessionId=""` |
| 网站会话 + 先刷新 | 先走 OAuth；若已变成 session 票，后续 `exchange_web_to_session` 自然跳过。若仍是 web，沿用今天切号里的 web 兑换 |

不加「刷新失败自动改成只切旧票」的隐式回退。

---

## 6. 测试（不联网）

新增或扩展：

1. **`test_switch_confirm.py`**  
   - `refresh_first=True` 的文案含「新登录票」与「最新」。  
   - `refresh_first=False` 的文案含「当前这张票」，不含「最新那台」。

2. **新 `test_switch_account.py`**（mock `refresh_login_one` / `probe_refresh_one` / `sand_patch.close_cursor` / `write_local_account` / `start_cursor` / `list_sessions`）  
   - `refresh_first=True`：先 `refresh_login_one`，成功后才 `close_cursor`；写入的 JWT 是刷新后 store 里的新票。  
   - `refresh_login_one` 失败：`close_cursor` 不被调用。  
   - `kick_old_tool=True` 且刷新成功：调用 `_drop_stale_tool_session_after_refresh`。  
   - `kick_old_tool=False`：不调用踢旧。  
   - `refresh_first=False`：不调用 `refresh_login_one`。  
   - 刷新成功但 `list_sessions` 抛错：仍 `ok: True`，带 `warning`。

3. **`test_pin_local.py` 预览**  
   - preview `switch_account` 在 refresh_first 默认下会 bump 客户端时间 / 可踢预览本工具会话（与现有 refresh mock 一致），且返回的 sessions 里本机或本工具能对上被 bump 的那条。

不改 `device_guard_pin_local` 的既有断言（仍然不得为认本机而刷票）。

---

## 7. 文件清单

| 文件 | 职责 |
|------|------|
| `app.py` | 扩展 `switch_account` 参数与刷新-再写入顺序；成功后合并 `list_sessions` |
| `switch_confirm.py` | 确认框说明句 |
| `web/index.html` | 确认框复选框 + 帮助一句 |
| `web/app.js` | 确认框状态、调用四参、toast、回写设备块 |
| `preview_server.py` | mock 四参切号 |
| `test_switch_confirm.py` | 文案 |
| `test_switch_account.py` | 切号顺序与失败不写盘 |
| `test_pin_local.py` | 预览桥 |

不改：`login_detect.py` 的认 IDE 规则、`device_guard_pin_local`、单独刷票 RPC。

---

## 8. 第二期（本规格不实施）

- 切号成功后自动启动本机保护。
- 切号成功后踢掉该号在其它机器上的全部客户端。
- 刷新失败时自动降级为只切旧票。
