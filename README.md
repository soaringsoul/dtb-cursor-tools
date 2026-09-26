# cursor账号管理器

作者：**夜雨微寒**。当前版本见 `sand_patch.py` 里的 `TOOL_VERSION`（打包文件名也用这个号）。

本机桌面工具，用来管理自己的 Cursor 登录票：导入、验证额度与订阅、切到本机 Cursor、查看并踢掉云端登录设备、给指定账号做本机设备保护。界面是 pywebview 窗口，数据只留在运行它的这台电脑上。

本工具**免费**。作者不收费。若你是付费买到的安装包，请向卖家申请退款，并核对文件有没有被改过。

## 适合谁用

- 自己有多张 Cursor 登录票，想在一台电脑上看额度、到期日、是否失效。
- 需要把某一张票写进本机 Cursor（切号），或写入 Grok Bot 自带账户（登录 Bot）。
- 需要看某个号在云端还登录着哪些设备，并踢掉不该留的会话。

不适合、也不应当用于：

- 使用不是你自己的 token。
- 倒卖账号、安装包或「代领资格」。
- 把导出的账号文件、`SandClaimer` 数据目录或日志发给别人。

使用 Cursor 官方接口须遵守 Cursor 的服务条款。踢设备、换登录票、切号都会改你账号在官方侧的登录状态，误操作可能导致当前票失效。

## 功能

### 账号列表

- **导入**：粘贴文本，或选 `cursor_accounts_*.json`。支持裸 JWT（`eyJ...`）、`user_01XXXX::eyJ...`（WorkosCursorSessionToken），以及 `邮箱----token`。按 user id 去重，重复导入不覆盖首次导入时间。
- **导入后自动验证**（可关）：只读查询邮箱、套餐、订阅到期、Bot / Auto / 高级 用量和云端会话，不领取。
- **探测本机账号**：读取本机 Cursor 当前登录的号。该号在按导入时间排序时固定在最上面；按订阅到期时间排序时不置顶，以免打乱时间顺序。
- **筛选**：付费账户、Free、本机、保护中、失效、即将到期（7 天内）、Bot 用尽、需绑卡。可与「我的分类」同时生效。
- **我的分类**：自定义标签，一个账号可打多个。分类存在本机设置里，换 token 不会丢掉已打的标签。
- **排序**：默认按订阅到期时间升序（更早到期的在前）。点工具栏「时间」或表头「订阅到期」切换降序。点「账号 / 邮箱」按导入时间（新的在前），点额度列按 Bot 周用量。
- **套餐**：显示在邮箱右侧，不再单独占一列。
- **订阅到期**：优先显示待取消日，否则显示本计费周期结束。附带自动续费 / 到期不续、剩余时间、月付或年付，以及 token 本身的「登录至」。
- **额度**：Bot 周、Auto 月、高级 月、超额（按量，进度按 $20 画，接口不提供封顶）。付费套餐自带 Sand 时，验证后会显示「已开通bot」，这不是领取动作。
- **导出全部**：文本按套餐分大类，大类里再分未用 Bot / 已用 Bot / 不续费。账号行仍是 `邮箱----user_id::token`，可以再导入。
- **显示 Token**：展开 Worksession 与 refresh_token，可分别复制。默认关闭。

批量验证、领取、删除、保护都**只处理勾选的行**。未勾选时对应按钮不可用。

### 行内操作

| 操作 | 做什么 | 要注意 |
|---|---|---|
| 验证 | 只读刷新套餐、订阅、用量、会话 | 不会领取，也不会改本机 Cursor |
| 切号 | 关掉当前 Cursor，写入该号登录态后再打开 | 默认先向官方换一张新登录票。网站会话会先换成客户端票，稍慢 |
| 登录 Bot | 把该号写入 Grok Bot 自带账户并切换 | 不关 Cursor。若开了系统代理 / TUN，官方页可能提示「重新连接你的电脑」，先确认再继续 |
| 查看设备 | 拉官方 Active Sessions | 可勾选后踢下线。接口没有电脑名和 IP |
| 本机保护 | 打开保护页 | 须先在本机 Cursor 登录该号。停止保护只在保护页里 |
| 进控制台 | 用隔离浏览器打开 Cursor 控制台 | 每个账号一个独立浏览器 profile，不影响日常浏览器 |

窄屏会藏起操作列，改由账号格里的「操作 ▾」打开全部动作。

### 本机保护

1. 先在本机 Cursor 登录要保护的号。
2. 打开「本机保护」，勾选要**留下**的设备。保留名单不能为空。
3. 可先「立即删除未勾选」，或设检测间隔（默认 30 秒，范围 5–3600）后启动。
4. 之后新出现、不在保留名单里的会话会被踢。可同时保护多个号。
5. 离开保护页或关掉窗口**不会**自动停止已启动的保护。重启本工具后需要重新点启动（会回填上次的勾选和间隔）。
6. 若踢掉的是本工具自己正在用的那张票，保护最多约 10 分钟后失效并自动停止。

「检测登录时间」用设备创建时间与本机当前时间比较，落在设定间隔内的标成刚登录，并只勾选这些设备。

### 云端 Agent

与登录票列表分开。把 Cursor Dashboard 里创建的用户 API Key（`crsr_...`）粘贴进来，可列出该密钥下的 Cloud Agent 并删除。密钥只存本机。一键清理会永久删除，删前有数量确认。

### 领取 Sand（Grok Bot）资格

- **Pro / Pro+ / Ultra**：套餐自带资格。验证就能看到「已开通 · 套餐自带」。「领取」只是再确认一次。
- **免费号**：领取需要自己在浏览器里绑卡。工具会标「需绑卡」，「网页领取」用该号登录态打开领取页。
- **团队号**：走团队通道，并带上 `teamId`。

## 环境

- Python 3.11（开发与打包脚本按 3.11 写）。
- 桌面窗口依赖 [pywebview](https://pywebview.flowrl.com/)。
  - Windows：Edge WebView2（Win10/11 通常已有）。
  - macOS：系统 WebKit。
- 「进控制台 / 网页领取」需要本机有 Chrome、Chromium 或 Edge。
- 「切号」要能关掉并重启本机 Cursor，并写入它的登录库。macOS 上若写入失败，看系统是否拦截了该应用。

## 从源码运行

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

macOS 也可以：

```bash
python3 -m pip install -r requirements-mac.txt
python3 app.py
```

仓库里若有 `install-mac.command` / `start-mac.command`，双击即可。首次若被系统拦截，在 Finder 里对该文件选右键 → 打开。

只看界面、不连 Cursor、不用桌面 WebView：

```bash
python3 preview_server.py --port 43147
```

浏览器打开 `http://127.0.0.1:43147/`。这是演示数据，不能用来切号或踢设备。

### 测试

不联网：

在仓库根目录执行：

```bash
python3 -m unittest discover -s tests -t .
```

`preview_server.py` 只给界面预览，不要把它算进发布物。

## 数据放在哪

目录：

- Windows：`%LOCALAPPDATA%\SandClaimer\`
- macOS / Linux：`~/SandClaimer/`

常见文件：

| 文件 | 内容 |
|---|---|
| `accounts.json` | 账号与 token。Windows 上能用 DPAPI 时会加密；其它系统是本机明文 |
| `status.json` | 上次验证到的套餐、额度、订阅 |
| `settings.json` | 界面设置、自定义分类、筛选 |
| `device_guard.json` | 保护名单与统计，不含 token |

这些文件**不要**提交到 git，也不要打进安装包。`.gitignore` 未忽略用户主目录，但忽略了构建产物。克隆仓库后的工作区里不应出现上述 json。

分享工具时只发源码或你自己打出来的安装包。不要附带 `SandClaimer` 目录、导出的 txt，或任何带 token 的 JSON。

## 打包

版本号只改 `sand_patch.TOOL_VERSION`。Windows 与 macOS 脚本都会读它。

**Windows**（在 Windows 上）：

```bat
build_win.bat
```

得到 `nuitka-out\SandClaimer-<版本>.exe`。若本机有 Inno Setup 6，还会得到安装向导。`build.bat` 会转去调用 `build_win.bat`。

**macOS**（必须在 Mac 上，系统自带 bash 3.2 即可）：

```bash
./build_mac.sh
```

得到 `SandClaimer-<版本>.dmg`，以及 `nuitka-out/cursor账号管理器.app`。dmg 里可以把 app 拖进「应用程序」。未签名，首次需要右键 → 打开。

Nuitka 把 Python 编成原生可执行文件，启动比把 `.pyc` 打进去的打包方式快。这不是加密：机器码仍可被分析，不能当成保护 token 的手段。token 的保护方式是「不要离开本机」。

## 目录

```
app.py              窗口入口，以及网页调用的本机接口
web/                界面（index.html、style.css、app.js）
accounts.py         导入、去重、账号落盘
sand_api.py         只读额度/订阅/会话，以及领取、踢下线
browser_login.py    隔离浏览器，注入该号的登录 cookie
device_guard.py     本机保护的后台循环
local_cursor.py     读写本机 Cursor 登录库（切号）
login_bot.py        写入 Grok Bot 账户
ops_ui.py           列表筛选、排序、导出分组（与界面同一套规则）
resolve.py          用 DNS over HTTPS 解析 cursor.com，避免本机错误 DNS
sand_patch.py       版本号，以及定位 / 开关本机 Cursor
preview_server.py   浏览器里预览界面
build_mac.sh        macOS 打包
build_win.bat       Windows 打包
tests/              单元测试
```

## 隐私

- 登录票和 API Key 只在本进程和本机 ↔ Cursor 官方之间使用，不上传到作者或其它第三方。
- 验证、看额度、看设备是读官方接口。切号、登录 Bot、踢设备、领取会改官方侧状态。
- 隔离浏览器的 profile 在本机，按账号分开。
- 开源之后也一样：任何人拿到你的 token 文件，就能以该号调用官方接口。请把 `~/SandClaimer` 当成密码目录。

## 公开仓库之前

1. 选定许可证并提交 `LICENSE`。本仓库目前**没有**许可证文件；没有许可证时，别人默认不能随意复制或再分发。
2. 确认提交内容里没有 `accounts.json`、`status.json`、导出 txt、真实 token、API Key。
3. 不要提交 `nuitka-out/`、`.nuitka_venv/`、`*.dmg`（已在 `.gitignore`）。
4. 用一份干净克隆跑一遍 `python3 app.py` 和单元测试，确认不依赖你机器上的私有路径。

## 界面约定（给改 UI 的人）

视觉按 Ant Design v6 的密度来，主色用地图帮橙 `#FA8C16`，不用 Ant Design 默认蓝。

- 正文字号 14px，字重只用 400 和 600。
- 按钮、输入、页签高 32px，圆角 6px。标签圆角 4px。头像和状态点保持圆形。
- 账号行里的操作是描边按钮。实心主按钮只用于整页上的主动作（例如探测本机账号、弹窗确认）。
- 表格用紧凑内边距。订阅到期列要宽到能放下 `YYYY-MM-DD HH:MM` 一整行。
