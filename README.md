# cursor账号管理器（作者：夜雨微寒）

批量给 Cursor 账号领取 **Grok Bot（Sand）** 资格的桌面小工具。iOS 玻璃浅蓝风界面，自动识别两种 token 格式，支持导入 JSON、批量领取、批量添加账号。

## 功能

- **token 自动识别**：`access_token`（JWT，`eyJ...`）、`ws token`（`user_01XXXX::eyJ...`，即 WorkosCursorSessionToken）、以及号池常见的 `邮箱----token` 行（导入时直接认出邮箱）。
- **导入方式**：直接粘贴（每行一个，可混排）、粘贴 `cursor_accounts_*.json` 内容、或「导入文件」选一个/多个 JSON。按 user id 自动去重，并记录每个号的**导入时间**。
- **导入即自动验证**：导入后立刻对新号做「验证账号」（可在导入区关掉），列表里直接出现邮箱、套餐、订阅剩余时间与三池用量。
- **验证账号**：只检查不领取。判定 token 是否有效并刷新套餐、订阅、用量。验证时同时拉取云端登录会话（客户端 / 网页数量），账号列可点开看创建与过期时间；本机 Cursor 当前登录的号打「本机」标记。失效判定以数据接口的真实状态码为准：JWT 过期（离线）、`auth/me` 无会话（实测作废的票回 **204** 而非 401）、Sand / usage-summary 回 401/403 都会标「失效」；`auth/me` 说死时还会用 api2 二次确认，避免误杀。领取时遇到失效票也会直接标「失效」，不再往领取接口打。
- **进控制台**：用该账号的登录态打开一个**隔离浏览器**（独立 profile，不碰你日常浏览器），直接落到 Cursor 控制台 `https://cursor.com/dashboard/spending`，浏览器留给你操作。
- **查看设备**：用该号已有登录票调官方接口拉云端设备（对应 dashboard Settings → Active Sessions），不必每次浏览器登录；每台可**踢下线**（最多约 10 分钟生效）。若被 cursor.com 人机校验拦截，可点「去浏览器过校验」用同一张票打开官方会话页手动拖动或踢设备。
- **本机设备保护**：开启前勾选要保留的设备并设置**检测间隔（秒，默认 30）**，之后按该间隔检测账号的登录设备并**自动下线未保留设备**（开启后新登录进来的设备同样被踢）。跑在 Python 守护线程里，界面空闲也照常工作；可同时给多个账号开。
- **批量领取**：给账号领 Sand（Grok Bot）资格，已开通短路、团队号自动带 `teamId`、个人号走试用、免费号标记「需绑卡」；领完只轻量刷 Bot 池，并汇总「新开通 / 已开通 / 需绑卡 / 失败」。
- **批量删除**：勾选后「删除选中」；「清空」删全部。
- **三池额度分开看**：Bot 周用量（每周重置，接口不回重置时间时按周期起点 + 7 天推算）、Auto 月用量、高级(API) 月用量；超额（按量已扣）用同一套进度条，默认上限 $20。不再显示 Cursor 的混合「总用量」（它是 Auto+API 两池加权，和任一池都对不上）。
- **导出 txt**：先按套餐分大类（Ultra → Pro+ → Pro → 团队 → Free），大类里再分「未用 Bot 额度 / 已用 Bot 额度 / 到期不续」，失效与未验证单独放最后；文件头给出各套餐数量，每段头部注明分类依据，并逐个列出该段每个号的到期 / 剩余 / Sand 状态 / 三池用量；段内按剩余时间从短到长。账号行保持 `邮箱----user_id::token`，可原样再导入。
- **Sand 资格口径**：验证时同时读 `get-sand-access-status`（权威）。实测 Pro / Pro+ / Ultra 套餐**自带** Sand 资格（接口返回 `proAndSuperGrokPlansGrantAccess: true`），所以付费号一导入验证就显示「已开通 · 套餐自带」，不是被领取了；「领取」只对免费号（需绑卡）和团队号有实际动作。
- **绕过本机 DNS 劫持**：内置 DoH 解析 `cursor.com` / `api2.cursor.sh` 真实 IP，即使本机跑着会劫持这些域名的网关（如 cgw）也能直连真实 Cursor。多家 DoH 并发竞速（Cloudflare 1.1.1.1、阿里 223.5.5.5 / 223.6.6.6、腾讯 1.12.12.12 / 120.53.53.53），谁先答谁赢；国内网络 1.1.1.1 常不可达，之前每个请求要白等 8 秒超时，现在 0.3 秒出结果，全部失败也只回落系统 DNS 一次、两分钟内不再重试。

## 运行（开发）

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

纯浏览器预览界面（演示数据，不连 Cursor、也不打开桌面 WebView）：

```bash
python3 preview_server.py --port 43147
```

> Windows 需要 **Edge WebView2 运行时**（Win10/11 一般自带；缺失时到微软官网装「Evergreen WebView2 Runtime」）。
> 「网页领取 / 进控制台」需要本机装有 Chrome / Chromium / Edge（Windows、macOS、Linux 都会自动找，Linux 另查 PATH、`/usr/bin`、`/opt`、snap）。

单元测试（不联网）：

```bash
python3 -m unittest test_login_sessions.py test_device_guard.py test_browser_reuse.py -v
```

## 进控制台 / 查看设备 / 本机设备保护

这三项已整合进本桌面工具，账号列表每一行都有对应按钮（窄屏时收进账号格里的「操作 ▾」菜单）。

### 进控制台

`Api.open_dashboard(account_id)` → `browser_login.open_with_token(user_id, jwt, url="https://cursor.com/dashboard/spending")`：
启动带调试端口 + 按账号隔离 profile 的 Chrome / Edge，通过 CDP 注入 `WorkosCursorSessionToken`（HttpOnly，
命令行 / URL 都带不进普通浏览器）。注入时带上 `https://cursor.com` 的 url 和 `sourceScheme=Secure`，避免新版 Chrome 把 Secure cookie 丢掉后被 302 到 `authenticator.cursor.sh`。若仍停在登录页会再注入并重跳控制台。已有 cursor.com 标签就复用并前置。浏览器留给用户。与插件 `openAccountDashboard` 行为一致。

### 查看设备（踢下线）

- 列表：`GET https://cursor.com/api/auth/sessions`（会话 cookie + `Origin`/`Referer`），`Api.list_sessions` **实时**拉取，不依赖上次验证的缓存。缺来源头时接口会回 403，和登录态作废不是一回事。
- 踢下线：`POST https://cursor.com/api/auth/sessions/revoke`，body `{"sessionId": "..."}`，会话 cookie + `Origin` 过 CSRF；
  HTTP 200 即视为已提交（`sand_api.revoke_session`，任何失败都返回 `{ok, error, status}` 不抛）。服务端最多约 10 分钟才真正生效。
- 弹窗「登录设备」：读取中 / 失败 / 空 三态；每台显示类型（客户端 / 网页 / 其他）、短 sessionId、创建 / 过期时间，
  「踢下线」按钮点了先在行内确认。与本工具所用登录票同类型的会话会标出来——踢掉它该号在本工具里就失效了。
- 人机校验（Vercel Security Checkpoint）不是 token 失效：额度 / 验证仍走 token。失败时可点「浏览器打开」→
  `Api.open_sessions_page` 注入 cookie 打开 `https://cursor.com/dashboard/settings#active-sessions`。
  每个账号只开一扇窗口；窗口还在时检测 / 踢下线走该页的 `fetch`，不会每轮再拉起浏览器。关窗后才回落普通 HTTP。
应用重启后会从仍占用该 profile 的 Chrome 进程找回调试口（不依赖内存里的端口）；找不到调试口时不会再启动第二次，避免 Chrome 单例只打开 `about:blank`。

### 本机设备保护（`device_guard.py`）

- **开启前**弹窗列出当前登录设备并让你**勾选要保留的**（本机账号默认勾上全部客户端会话；重开本工具会回填上次的勾选）。
  保留名单为空一律拒绝启动，否则会把包括本机在内的所有设备全踢掉。
- **开启后**按设定的**检测间隔（秒，默认 30，可填 5–3600）**一轮：拉一次设备列表，把不在保留名单里的 sessionId 逐个 `revoke`。开启后新登录进来的设备不在名单里，同样会被踢。改间隔要先停止再重新启动。
- 每个账号一个守护线程（`Api.device_guard_start / stop / stop_all / status`），同一账号绝不重叠两个循环；每轮异常只记 `lastError`，线程不会死；
  拉列表失败的那一轮不踢任何人；同一会话踢过后 30 秒内不重复发 revoke（服务端生效有延迟，列表里可能还挂着）；
  连续 30 轮 401/403（本工具用的票自己失效，约 15 分钟）自动停止。
- 行内显示紫色「保护中 · 已踢 N」标签（点开看启动时间 / 最近检测 / 轮次 / 已踢列表），按钮变成「停止保护」；账号列表标题旁有全局提示。
- 名单与统计落在 `SandClaimer/device_guard.json`（不存 token）。**重启本工具不会自动恢复踢人**，需要重新点「本机保护」启动。

## 打包（Nuitka 编译 + 安装包）

**Windows：**

```bat
build_win.bat
```

产物：

- `nuitka-out\SandClaimer-<版本>.exe` —— 单文件绿色版，双击即用（文件名带版本号，如 `SandClaimer-1.1.6.exe`）。
- `installer\SandClaimer-Setup-<版本>.exe` —— 中文安装向导，装到 Program Files 并建开始菜单/桌面快捷方式（需本机已装 Inno Setup 6）。

`build.bat` 会转调 `build_win.bat`。

**macOS：**

```bash
./build_mac.sh
```

产物 `SandClaimer-<版本>.dmg`（内含 `cursor账号管理器.app`）。

> 版本号统一取自 `sand_patch.py` 的 `TOOL_VERSION`，`build_win.bat` / `build_mac.sh` / `make_share.ps1` 会自动读取并写进产物文件名，无需多处手改。

`build_win.bat` 会依次：装依赖 → 修补 Nuitka 的 pywebview 插件 → 生成图标 → Nuitka 编译 → Inno Setup 打安装包。

### 为什么用 Nuitka（而非 PyInstaller）

- **启动更快**：Python 源码被编译成 C/机器码，不是解释执行的 `.pyc`。
- **天然混淆/加密**：产物是原生机器码，源码不可还原；onefile 运行时把负载解压到临时目录再执行（相当于加密封装），比 PyInstaller 的可直接解包 `.pyc` 强得多。
- `build_win.bat` 用 `--mingw64 --assume-yes-for-downloads`：首次编译 Nuitka 会自动下载并缓存 MinGW64，无需手动装 MSVC；之后走缓存会快很多。

> `patch_plugin.py`：Nuitka 4.1.3 的 pywebview 插件在 Windows 白名单里漏了 pywebview 6.2.x 新增的 `webview.platforms.win32`，会导致打包后 winforms 后端起不来。该脚本幂等地把它补进白名单，`build_win.bat` 已自动调用。
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
| 登录会话 / 设备列表 | GET | `cursor.com/api/auth/sessions` | 会话 cookie |
| 踢下线（查看设备 / 本机设备保护） | POST | `cursor.com/api/auth/sessions/revoke`（body `{sessionId}`） | cookie + Origin |
| 查资格 | POST | `cursor.com/api/dashboard/get-sand-access-status` | 会话 cookie |
| 取 teamId / 邮箱 | POST | `cursor.com/api/dashboard/get-me` | 会话 cookie |
| 个人领取 | POST | `cursor.com/api/dashboard/start-sand-trial` | cookie + Origin |
| 团队领取 | POST | `cursor.com/api/dashboard/request-sand-team-access`（body `{teamId}`） | cookie + Origin |

### 额度口径（为什么要分三池）

`usage-summary` 的 `individualUsage.plan` 里同时有 `autoPercentUsed`（Auto 池）、`apiPercentUsed`（高级模型 / API 池）和 `totalPercentUsed`。
实测 `totalPercentUsed` = (Auto 已用 + API 已用) / (Auto 上限 + API 上限)，是两池加权的混合值，与任何一池都不相等，
所以工具只把 Auto、高级、Bot（`GetSandUsageStatus.usagePercent`，按周重置）三个数分开展示，混合值仅放在鼠标悬停提示里。
`individualUsage.onDemand.used` > 0 表示该号已开按量付费并产生真实扣费；列表里用第四条进度条显示，默认超额上限 $20（接口不给封顶）。

## 安全

- token 只在本机内存与本机↔Cursor 官方之间使用，不上传任何第三方服务。
- 请勿把含 token 的 JSON 或本工具日志分享给他人。

## 项目结构

```
sand-claimer/
├─ app.py                # pywebview 入口 + JS 桥接 + 导出文本拼装
├─ sand_api.py           # Cursor Sand 查询/领取/验证 + 登录会话列表/踢下线（三池额度口径见上）
├─ browser_login.py      # CDP 注入登录态开隔离浏览器（网页领取 / 进控制台），Win / macOS / Linux 找浏览器
├─ device_guard.py       # 本机设备保护：按秒间隔（默认 30）检测登录设备、自动下线未保留设备（守护线程 + 名单记忆）
├─ accounts.py           # token/JSON 导入与账号表（记录导入时间、支持 邮箱----token）
├─ test_accounts_usage.py# 账号存储 / 额度解析 / 验证 / 导出文本 的单元测试（不联网）
├─ test_login_sessions.py# 云端登录会话归一化 / 拉取 / 踢下线 / get_status 接入（不联网）
├─ test_device_guard.py  # 本机设备保护：踢未保留 / 留已保留 / 空名单拒绝 / 停止 / 持久化（不联网）
├─ sand_patch.py         # 定位本机 Cursor / 关开进程（切号用）；TOOL_VERSION 供打包读取
├─ resolve.py            # DoH 绕过 DNS 劫持
├─ web/                  # 玻璃风 UI（index.html / style.css / app.js）
├─ make_icon.py          # 生成多尺寸 icon.ico（自带沙漏图标，可用 assets/icon-1024.png 覆盖）
├─ patch_plugin.py       # 修补 Nuitka pywebview 插件（补 win32）
├─ installer.iss         # Inno Setup 安装包脚本
├─ ChineseSimplified.isl # 安装向导简体中文语言包
├─ icon.ico              # 应用图标（由 make_icon.py 生成）
├─ requirements.txt
├─ build_win.bat         # Windows 一键：Nuitka 编译 + 打安装包
├─ build_mac.sh          # macOS 一键：Nuitka 编译 .app + .dmg
└─ build.bat             # 转调 build_win.bat
```
