# cursorAdmin

一个运行在本机的 Cursor 多账号管理工具，用来集中查看账号状态、额度与订阅信息，并完成本机切号、登录设备管理、设备保护、Cloud Agent 管理等操作。

作者：**夜雨微寒**

当前版本：**v1.2.1**

> 本项目是第三方工具，不是 Cursor 官方产品，与 Cursor 官方不存在隶属关系。请仅使用你自己拥有或明确获得授权的账号和凭据，并遵守 Cursor 的服务条款。

## 项目解决什么问题

当 Cursor 账号变多以后，真正麻烦的往往不是“登录”，而是信息开始变得分散：

- 哪个账号还有效；
- 当前是什么套餐；
- 订阅什么时候到期；
- Bot / Auto / 高级额度用了多少；
- 哪些设备还在线；
- 本机现在登录的是哪个账号；
- 怎么快速切换到另一个账号。

这个项目把这些常用操作集中到一个本地桌面界面里。

账号数据默认只保存在运行工具的这台电脑上。

## 核心功能

### 多账号统一管理

支持导入：

- 裸 JWT；
- `user_...::token`（WorkosCursorSessionToken）；
- `邮箱----token`；
- `cursor_accounts_*.json`。

导入后按 user id 去重，并保留首次导入时间。

账号列表可以查看：

- 邮箱与套餐；
- 订阅到期时间；
- token 登录有效期；
- Bot 周用量；
- Auto 月用量；
- 高级模型月用量；
- 超额用量；
- 账号是否失效；
- 是否需要绑卡；
- 是否为本机当前登录账号。

支持付费账户、Free、本机、保护中、失效、即将到期、Bot 用尽、需绑卡等筛选，也可以给账号添加自定义分类。

### 账号验证

“验证”是只读操作，会从 Cursor 官方接口刷新：

- 邮箱；
- 套餐；
- 订阅；
- 使用额度；
- Active Sessions；
- token 有效状态。

不会自动切号，也不会因为验证就领取任何资格。

批量验证只处理已勾选的账号。

### 本机切号

可以把指定账号写入本机 Cursor 登录状态。

切号时会关闭当前 Cursor，完成登录态切换后重新启动。

默认会优先向官方换取新的客户端登录票；如果当前是网站会话，转换为客户端票时可能稍慢。

> 切号会真实修改本机 Cursor 登录状态。正在执行重要任务时，请先确认当前工作已经保存。

### 登录设备管理

可以读取 Cursor 官方 Active Sessions，并将不需要的会话踢下线。

Cursor 当前接口不会提供完整电脑名和 IP，因此本工具只展示官方接口能够返回的信息。

### 本机设备保护

如果某个账号只希望保留指定设备，可以启用设备保护。

使用方式：

1. 先在本机 Cursor 登录需要保护的账号；
2. 打开“本机保护”；
3. 勾选需要保留的设备；
4. 设置检测间隔并启动。

之后新出现、且不在保留名单里的会话会被自动踢下线。

默认检测间隔为 30 秒，可设置范围为 5–3600 秒。

保护配置会保存在本机；应用重启后需要重新启动保护任务。

### 登录 Grok Bot

可以把指定账号写入 Grok Bot 自带账户并切换。

这个操作不会关闭 Cursor。

如果系统开启了代理 / TUN，官方页面可能提示“重新连接你的电脑”，请先确认当前网络环境再继续。

### Cloud Agent 管理

可以单独保存 Cursor Dashboard 创建的用户 API Key（`crsr_...`），用于：

- 查询该 Key 下的 Cloud Agent；
- 删除指定 Agent；
- 一键清理 Agent。

API Key 只存本机，与普通账号 token 列表分开管理。

### Bot / Sand 状态

工具会展示账号当前 Bot / Sand 相关状态，并根据 Cursor 官方返回结果判断是否已开通、是否需要绑卡等。

对于会改变官方侧状态的动作，界面会明确区分只读验证与实际操作。

## 适合谁

适合：

- 自己长期使用多个 Cursor 账号；
- 想统一查看套餐、额度和订阅时间；
- 经常需要在本机切换账号；
- 想检查自己的账号还有哪些设备在线；
- 想给自己的账号做简单的设备会话保护；
- 需要管理自己 API Key 下的 Cloud Agent。

不应当用于：

- 使用来源不明的 token；
- 使用不属于自己的账号；
- 未经授权控制他人账号或设备会话；
- 倒卖账号、token、安装包或所谓“资格”；
- 分发包含账号凭据的数据文件。

本工具免费提供。

如果你是付费买到的安装包，请核对来源，并优先从本仓库获取源码自行构建。

## 快速开始

项目主要按 **Windows 和 macOS** 场景维护。

### Windows

建议使用 Python 3.11。

```bash
python -m pip install -r requirements.txt
python -m app
```

也可以直接双击：

```text
script/启动.bat
```

### macOS

```bash
python3 -m pip install -r requirements-mac.txt
python3 -m app
```

首次也可以双击：

```text
script/install-mac.command
```

以后运行：

```text
script/start-mac.command
```

如果 macOS 首次拦截 `.command` 或未签名应用，请在 Finder 中右键文件或应用，选择“打开”。

更详细的 macOS 说明见 [README-mac.md](README-mac.md)。

## 界面预览

如果只想查看界面，不连接 Cursor，也不调用桌面 WebView，可以启动预览服务：

```bash
python3 -m app.preview_server --port 43147
```

浏览器打开：

```text
http://127.0.0.1:43147/
```

预览模式使用演示数据，不能切号、踢设备或执行其它真实账号操作。

## 数据与隐私

默认数据目录：

- Windows：`%LOCALAPPDATA%\SandClaimer\`
- macOS / Linux：`~/SandClaimer/`

常见文件：

| 文件 | 内容 |
|---|---|
| `accounts.json` | 账号与 token；Windows 可用 DPAPI 时会加密，其它系统为本机明文 |
| `status.json` | 最近一次验证到的套餐、额度、订阅等状态 |
| `settings.json` | 界面设置、自定义分类和筛选 |
| `device_guard.json` | 设备保护名单与统计，不含 token |

隐私原则：

- 登录票与 API Key 只在本机保存和使用；
- 工具不会把账号凭据上传到作者服务器；
- 正常网络请求发生在本机与 Cursor 官方服务之间；
- 隔离浏览器 profile 按账号分开保存在本机。

请把 `SandClaimer` 数据目录视为密码目录。

不要分享：

- `accounts.json`；
- 导出的账号 txt / json；
- 包含真实 token 的日志；
- API Key；
- 整个 `SandClaimer` 数据目录。

## 操作风险说明

| 操作 | 作用 | 是否改变状态 |
|---|---|---|
| 验证 | 刷新套餐、订阅、额度、会话 | 否 |
| 切号 | 写入本机 Cursor 登录态并重新启动 Cursor | 是 |
| 登录 Bot | 将账号写入 Grok Bot 自带账户 | 是 |
| 查看设备 | 查询 Active Sessions | 否 |
| 踢设备 | 删除指定 Active Session | 是 |
| 本机保护 | 定时清理未保留的设备会话 | 是 |
| 进控制台 | 用隔离浏览器打开 Cursor 控制台 | 可能产生新的官方会话 |

执行会改变账号状态的操作前，请确认账号归属和影响范围。

## 测试

在仓库根目录执行：

```bash
python3 -m unittest discover -s tests -t .
```

测试默认不依赖真实账号联网。

## 打包

版本号统一定义在：

```text
app/sand_patch.py -> TOOL_VERSION
```

当前为 **1.2.1**。

### Windows

```bat
build_win.bat
```

主要产物：

```text
nuitka-out\cursorAdmin-<版本>.exe
```

如果本机安装了 Inno Setup 6，还会生成安装向导。

### macOS

必须在 macOS 上构建：

```bash
./build_mac.sh
```

主要产物：

```text
cursorAdmin-<版本>.dmg
nuitka-out/cursorAdmin.app
```

当前 macOS 构建未签名，首次运行可能需要右键“打开”。

> Nuitka 会把 Python 编译为原生可执行文件，但这不等于安全加密。真正保护 token 的方式仍然是：不要让凭据离开自己的设备。

## 项目结构

```text
app/
  desktop.py        桌面窗口入口与本机接口
  accounts.py       账号导入、去重与落盘
  sand_api.py       套餐、额度、订阅、会话等接口
  browser_login.py  隔离浏览器登录
  device_guard.py   设备保护
  local_cursor.py   本机 Cursor 登录态读写
  grok_bot.py       Grok Bot 账户写入
  ops_ui.py         筛选、排序、导出规则
  resolve.py        DNS over HTTPS 解析
  sand_patch.py     版本号及 Cursor 本机补丁逻辑
  preview_server.py 浏览器界面预览

web/                Web UI
script/             启动与辅助脚本
tests/              单元测试
build_win.bat        Windows 打包
build_mac.sh         macOS 打包
```

## 安全与开源说明

账号管理工具最重要的不是功能多少，而是凭据有没有被安全处理。

建议：

1. 优先检查源码；
2. 对网络请求和本地落盘逻辑做一次审查；
3. 尽量自己从源码运行或自行构建；
4. 下载 Release 后核对发布文件校验值。

仓库中不应出现任何真实账号凭据。

提交代码前请再次确认没有包含：

- `accounts.json`；
- `status.json`；
- 导出的账号文件；
- 真实 token；
- API Key；
- `SandClaimer` 用户数据目录；
- Nuitka / DMG 等本机构建产物。

当前仓库尚未提供 `LICENSE` 文件。在明确许可证之前，请不要默认项目代码可以被任意复制、再分发或商用。

## 参与项目

如果遇到：

- Cursor 接口变化；
- Windows / macOS 兼容问题；
- 账号状态判断错误；
- UI 显示问题；
- 潜在安全问题；

欢迎提交 Issue 或 Pull Request。

提交问题时，请先删除截图、日志中的邮箱、token、API Key 等敏感信息。

## 交流

QQ 交流群：<https://qm.qq.com/q/POZe1e3WYG>（1056952049）

---

项目地址：<https://github.com/soaringsoul/dtb-cursor-tools>
