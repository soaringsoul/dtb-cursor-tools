# Sand 资格领取器 · macOS 使用说明

Windows 用户：双击 `启动.bat` 即可，无需看本文件。

## 运行（源码模式，推荐）

1. 装 Python 3（若未装）：https://www.python.org/downloads/
2. 双击 `install-mac.command`（首次装依赖，需联网）。
   - 若提示“无法打开、来自身份不明的开发者”：右键该文件 → 打开 → 打开。
3. 以后双击 `start-mac.command` 启动。

命令行等价：

```bash
python3 -m pip install -r requirements-mac.txt
python3 app.py
```

## 打补丁 / 切号需要的权限

- 「切号」「打补丁」要写入 Cursor 安装目录与登录库。首次可能弹出权限请求，允许即可。
- 若失败，用终端 `sudo python3 app.py` 再试。

## 一键打包成 .app / .dmg（在 Mac 上做）

Windows 上无法编译出 Mac 程序，必须在 Mac 上打。**双击 `build-mac-app.command`** 即可一键完成：
建虚拟环境 → 装依赖 → 生成图标 → PyInstaller 打 `.app` → 写版本号 → ad-hoc 签名 → 生成 `.dmg`。

产物是同目录下的 **`SandClaimer-<版本>.dmg`**（版本号自动取自 `sand_patch.py` 的 `TOOL_VERSION`）。
把这个 `.dmg` 发群，Mac 用户下载 → 打开 → 拖进「应用程序」→ 双击运行（首次右键图标 → 打开绕过 Gatekeeper）。

> 首次双击 `.command` 若提示“来自身份不明的开发者”：右键该文件 → 打开；或先在终端执行一次 `chmod +x *.command`。

## 隐私

- 账号 token 只存在本机（macOS 下 `~/Library/Application Support/SandClaimer/`），不上传第三方。
- 分享本工具时，请勿附带上述目录或任何导出的账号文件。
