# Sand 资格领取器（SandClaimer）

批量给 Cursor 账号领取 **Grok Bot（Sand）** 资格的桌面小工具。iOS 玻璃浅蓝风界面，自动识别两种 token 格式，支持导入 JSON、批量领取、批量添加账号。

## 功能

- **token 自动识别**：`access_token`（JWT，`eyJ...`）、`ws token`（`user_01XXXX::eyJ...`，即 WorkosCursorSessionToken）、以及号池常见的 `邮箱----token` 行（导入时直接认出邮箱）。
- **导入方式**：直接粘贴（每行一个，可混排）、粘贴 `cursor_accounts_*.json` 内容、或「导入文件」选一个/多个 JSON。按 user id 自动去重，并记录每个号的**导入时间**。
- **导入即自动验证**：导入后立刻对新号做「验证账号」（可在导入区关掉），列表里直接出现邮箱、套餐、订阅剩余时间与三池用量。
- **验证账号**：只检查不领取。判定 token 是否有效并刷新套餐、订阅、用量。验证时同时拉取云端登录会话（客户端 / 网页数量），账号列可点开看创建与过期时间；本机 Cursor 当前登录的号打「本机」标记。失效判定以数据接口的真实状态码为准：JWT 过期（离线）、`auth/me` 无会话（实测作废的票回 **204** 而非 401）、Sand / usage-summary 回 401/403 都会标「失效」；`auth/me` 说死时还会用 api2 二次确认，避免误杀。领取时遇到失效票也会直接标「失效」，不再往领取接口打。
- **批量领取**：给账号领 Sand（Grok Bot）资格，已开通短路、团队号自动带 `teamId`、个人号走试用、免费号标记「需绑卡」；领完只轻量刷 Bot 池，并汇总「新开通 / 已开通 / 需绑卡 / 失败」。
- **批量删除**：勾选后「删除选中」；「清空」删全部。
- **三池额度分开看**：Bot 周用量（每周重置，接口不回重置时间时按周期起点 + 7 天推算）、Auto 月用量、高级(API) 月用量；另标出按量付费（on-demand）已扣金额。不再显示 Cursor 的混合「总用量」（它是 Auto+API 两池加权，和任一池都对不上）。
- **导出 txt**：先按套餐分大类（Ultra → Pro+ → Pro → 团队 → Free），大类里再分「未用 Bot 额度 / 已用 Bot 额度 / 到期不续」，失效与未验证单独放最后；文件头给出各套餐数量，每段头部注明分类依据，并逐个列出该段每个号的到期 / 剩余 / Sand 状态 / 三池用量；段内按剩余时间从短到长。账号行保持 `邮箱----user_id::token`，可原样再导入。
- **Sand 资格口径**：验证时同时读 `get-sand-access-status`（权威）。实测 Pro / Pro+ / Ultra 套餐**自带** Sand 资格（接口返回 `proAndSuperGrokPlansGrantAccess: true`），所以付费号一导入验证就显示「已开通 · 套餐自带」，不是被领取了；「领取」只对免费号（需绑卡）和团队号有实际动作。
- **绕过本机 DNS 劫持**：内置 DoH 解析 `cursor.com` / `api2.cursor.sh` 真实 IP，即使本机跑着会劫持这些域名的网关（如 cgw）也能直连真实 Cursor。多家 DoH 并发竞速（Cloudflare 1.1.1.1、阿里 223.5.5.5 / 223.6.6.6、腾讯 1.12.12.12 / 120.53.53.53），谁先答谁赢；国内网络 1.1.1.1 常不可达，之前每个请求要白等 8 秒超时，现在 0.3 秒出结果，全部失败也只回落系统 DNS 一次、两分钟内不再重试。

## 运行（开发）

```bat
python -m pip install -r requirements.txt
python app.py
```

> Windows 需要 **Edge WebView2 运行时**（Win10/11 一般自带；缺失时到微软官网装「Evergreen WebView2 Runtime」）。

## 打包（Nuitka 编译 + 安装包）

双击或命令行运行：

```bat
build.bat
```

产物：

- `nuitka-out\SandClaimer-<版本>.exe` —— 单文件绿色版，双击即用（文件名带版本号，如 `SandClaimer-1.1.6.exe`）。
- `installer\SandClaimer-Setup-<版本>.exe` —— 中文安装向导，装到 Program Files 并建开始菜单/桌面快捷方式。

> 版本号统一取自 `sand_patch.py` 的 `TOOL_VERSION`，`build.bat` / `make_share.ps1` 会自动读取并写进产物文件名，无需多处手改。

`build.bat` 会依次：装依赖 → 修补 Nuitka 的 pywebview 插件 → 生成图标 → Nuitka 编译 → Inno Setup 打安装包。

### 为什么用 Nuitka（而非 PyInstaller）

- **启动更快**：Python 源码被编译成 C/机器码，不是解释执行的 `.pyc`。
- **天然混淆/加密**：产物是原生机器码，源码不可还原；onefile 运行时把负载解压到临时目录再执行（相当于加密封装），比 PyInstaller 的可直接解包 `.pyc` 强得多。
- `build.bat` 用 `--mingw64 --assume-yes-for-downloads`：首次编译 Nuitka 会自动下载并缓存 MinGW64，无需手动装 MSVC；之后走缓存会快很多。

> `patch_plugin.py`：Nuitka 4.1.3 的 pywebview 插件在 Windows 白名单里漏了 pywebview 6.2.x 新增的 `webview.platforms.win32`，会导致打包后 winforms 后端起不来。该脚本幂等地把它补进白名单，`build.bat` 已自动调用。
>
> `ChineseSimplified.isl`：安装向导的简体中文语言包（Inno Setup 默认不含）。

## 领取规则（与 Cursor 官方一致）

- **付费账号**（Pro+ / Ultra / Team）：直接开通，无需绑卡。
- **免费账号**：领取需先验证信用卡，工具会标记「需绑卡」（如返回验证链接会一并给出）。
- **团队账号**：走团队通道并自动带上 `teamId`（从 `get-me` 读取）。团队级开通是否覆盖全部成员座位，取决于 Cursor 侧策略。

## 用到的官方接口（均实测确认）

| 用途 | 方法 | 端点 | 鉴权 |
|---|---|---|---|
| Bot 周额度 | POST | `api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus` | Bearer accessToken |
| Auto / 高级 月额度、账单周期、按量付费 | GET | `cursor.com/api/usage-summary` | 会话 cookie |
| 订阅状态（续费 / 待取消 / 年付） | GET | `cursor.com/api/auth/stripe` | 会话 cookie |
| 探活（验证账号） | GET | `cursor.com/api/auth/me` | 会话 cookie |
| 登录会话 | GET | `cursor.com/api/auth/sessions` | 会话 cookie |
| 查资格 | POST | `cursor.com/api/dashboard/get-sand-access-status` | 会话 cookie |
| 取 teamId / 邮箱 | POST | `cursor.com/api/dashboard/get-me` | 会话 cookie |
| 个人领取 | POST | `cursor.com/api/dashboard/start-sand-trial` | cookie + Origin |
| 团队领取 | POST | `cursor.com/api/dashboard/request-sand-team-access`（body `{teamId}`） | cookie + Origin |

### 额度口径（为什么要分三池）

`usage-summary` 的 `individualUsage.plan` 里同时有 `autoPercentUsed`（Auto 池）、`apiPercentUsed`（高级模型 / API 池）和 `totalPercentUsed`。
实测 `totalPercentUsed` = (Auto 已用 + API 已用) / (Auto 上限 + API 上限)，是两池加权的混合值，与任何一池都不相等，
所以工具只把 Auto、高级、Bot（`GetSandUsageStatus.usagePercent`，按周重置）三个数分开展示，混合值仅放在鼠标悬停提示里。
`individualUsage.onDemand.used` > 0 表示该号已开按量付费并产生真实扣费，会单独标出。

## 本机 Cursor 补丁（Stream 回路）在 1.2.1 修的两个问题

补丁让 agent-host 走 `managed-local` 本地回路、以 `clientType:"sand"` 身份推理，从而计到 Bot 额度。但 3.18.x 的
`selectTurnRuntime` 在补丁点之前还有一道准入判定，只放 `userMessageAction + AGENT 模式 + 无子代理 run options`
进本地回路，其余一律 `{runtime:"connect"}` 回落云端 `AgentService.Run`——而这条路在 sand 身份下被服务端
`401 ERROR_NOT_LOGGED_IN` 拒绝。日志（`Cursor Agent Host.*.log`）里的实测表现：

- 每个后台命令跑完，Cursor 自动给模型发一轮 `backgroundTaskCompletionAction` → `action-not-supported` → connect → 401
  → 界面弹 **"An unexpected error occurred. Request ID: …"**（每次后台任务结束都弹一次）。
- 子代理由 `executeSubagentTurn` 以 `subagentTypeName / parentAgentToolCallId` 起 turn → `run-options-not-supported`
  → connect → 401 → 子代理起不来，自然也用不到 Bot 额度。

本地 runtime（675.js）其实已经注册了 `backgroundTaskCompletionAction / backgroundSubagentAction /
asyncAskQuestionCompletionAction …` 等 12 种动作的 handler，也有 `subagentTypeName` 与 ASK / PLAN 模式的处理代码，
所以 1.2.1 新增两条规则（`SAND_LOCAL_ACTIONS_V1`、`SAND_SUBAGENT_LOCAL_V1`）：准入白名单放宽到本地 runtime 真正支持的
动作，并把子代理三项 run options 用 `!1&&(…)` 短路。BYOK 私有模型、`customSystemPrompt / harness`、
`startPlanAction / injectContextAction` 仍按官方逻辑回落 connect。原代码全部保留为死代码，卸载按 marker 精确回退，
对 vanilla 合成夹具做过字节级往返测试（3.18.9 形态与 3.19.13 形态）。已测试注入版本：**3.18.9 / 3.18.25 / 3.19.13**；其它版本因锚点缺失拒绝写入。**已打旧版补丁的机器要重新点一次「打补丁」**（面板会提示「补丁需升级」）。

### 补丁面板怎么看「到底成没成功」（`patch_report.py`）

群友常见的「显示成功其实没成功 / 失败了不知道哪里失败」，面板现在分三层给证据：

- **逐条规则**（11 条）：每条单独判定 `已生效`（标记已写入）/ `未打`（找到锚点但没改）/ `锚点缺失`（这个 Cursor 构建里没有该代码 = 版本不符）/ `部分生效`（同一规则有的位置替换了有的没替换），并列出命中的文件和修法。头部给出「N/10 条必需规则生效」的结论；若正在运行的 Cursor 不在补丁目录（本机多个安装）会用红字标出。
- **打补丁的逐步报告**：定位 Cursor → 多安装提示 → 版本锚点 → 生成计划（改哪些文件、新增哪些规则）→ 关闭 Cursor（关不掉会明说）→ 写入并校验（marker / 扩展内嵌哈希 / product.json 完整性）→ 逐条规则确认 → 重启 Cursor（确认起来的进程就在补丁目录）→ 本机登录号的 Sand 资格。任何一步失败都写明原因与修法（没权限 → 以管理员运行；锚点缺失 → 装 3.18.9 / 3.18.25 / 3.19.13；文件被改 → 关自动更新重试 …），失败自动回滚。
- **验证生效**：读 Cursor 自己的 `Cursor Agent Host*.log`，检查本地回路 runtime 是否加载、move_exec 是否开、最近几轮到底走了 `managed-local`（本地 Bot 回路）还是 `connect`（回落云端，Bot 额度没用上）以及回落原因，并把 401 / 额度用尽等错误列出来。

## 安全

- token 只在本机内存与本机↔Cursor 官方之间使用，不上传任何第三方服务。
- 请勿把含 token 的 JSON 或本工具日志分享给他人。

## 项目结构

```
sand-claimer/
├─ app.py                # pywebview 入口 + JS 桥接 + 导出文本拼装
├─ sand_api.py           # Cursor Sand 查询/领取/验证（三池额度口径见上）
├─ accounts.py           # token/JSON 导入与账号表（记录导入时间、支持 邮箱----token）
├─ test_accounts_usage.py# 账号存储 / 额度解析 / 验证 / 导出文本 的单元测试（不联网）
├─ test_login_sessions.py# 云端登录会话归一化 / 拉取 / get_status 接入（不联网）
├─ sand_patch.py         # 本机 Cursor 客户端模式补丁 / 回退（Stream 回路 + 本地准入放宽）
├─ patch_report.py       # 逐条规则状态 / 逐步安装报告 / 读 Cursor 日志验证生效
├─ test_sand_patch.py    # 补丁规则单元测试（3.18 / 3.19 合成夹具字节级往返）
├─ test_patch_report.py  # 报告层单元测试（规则判定 / 步骤编排 / 日志解析）
├─ resolve.py            # DoH 绕过 DNS 劫持
├─ web/                  # 玻璃风 UI（index.html / style.css / app.js）
├─ docs/prd/             # 产品级迭代 PRD（号池线 / 补丁线排期）
├─ docs/superpowers/     # 单功能规格与实现计划
├─ CHANGELOG.md          # 版本记录（从 1.2.1 起）
├─ make_icon.py          # 生成多尺寸 icon.ico（自带沙漏图标，可用 assets/icon-1024.png 覆盖）
├─ patch_plugin.py       # 修补 Nuitka pywebview 插件（补 win32）
├─ installer.iss         # Inno Setup 安装包脚本
├─ ChineseSimplified.isl # 安装向导简体中文语言包
├─ icon.ico              # 应用图标（由 make_icon.py 生成）
├─ requirements.txt
└─ build.bat             # 一键：编译 + 打安装包
```

迭代排期、两条产品线和下一步做什么见 [迭代 PRD](docs/prd/2026-09-06-iteration-prd.md)。版本记录见 [CHANGELOG.md](CHANGELOG.md)。
