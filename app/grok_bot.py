"""独立 Grok Bot 客户端的登录态读写与启停。

Grok Bot.app（bundle id com.anysphere.sand）是和 Cursor IDE 分开的 Electron 应用，
登录数据在「Application Support/Grok Bot/sand-secrets.json」，不是 Cursor 的 state.vscdb。
本模块绝不关闭、写入或启动 Cursor。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


APP_NAME = "Grok Bot"
BUNDLE_ID = "com.anysphere.sand"
EXE_NAME = "Grok Bot.exe"
SECRETS_FILE = "sand-secrets.json"
ACCOUNTS_KEY = "cursor-accounts"
ACCESS_KEY = "cursor-access-token"
REFRESH_KEY = "cursor-refresh-token"
PROFILE_KEY = "cursor-account-profile"
PLAINTEXT_PREFIX = "plaintext:v1:"
ACCOUNT_SLOT_SLICE = "sand.client.slice.client-meta.account-slot"
PERSISTENCE_DIR = "sand-client-persistence"
SAFE_STORAGE_SERVICE = "Grok Bot Safe Storage"
SAFE_STORAGE_ACCOUNT = "Grok Bot Key"
_OSCRYPT_SALT = b"saltysalt"
_OSCRYPT_IV = b" " * 16
_OSCRYPT_PREFIX = b"v10"
_OSCRYPT_ITERATIONS = 1003
_OSCRYPT_KEY_LEN = 16

_SAFE_STORAGE_PASSWORD_CACHE: Optional[str] = None
_SAFE_STORAGE_PASSWORD_TRIED = False


class GrokBotError(Exception):
    """找不到客户端、写盘失败等。"""


def user_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32" or os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming"
        )
        return Path(os.path.join(base, APP_NAME))
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / APP_NAME


def secrets_path() -> Path:
    return user_data_dir() / SECRETS_FILE


def wrap_plaintext(value: str) -> str:
    raw = (value or "").encode("utf-8")
    return PLAINTEXT_PREFIX + base64.b64encode(raw).decode("ascii")


def unwrap_plaintext(stored: str) -> str:
    text = stored or ""
    if text.startswith(PLAINTEXT_PREFIX):
        return base64.b64decode(text[len(PLAINTEXT_PREFIX) :]).decode("utf-8")
    return text


def _aes128_cbc(encrypt: bool, key: bytes, iv: bytes, data: bytes) -> bytes:
    if sys.platform == "darwin":
        try:
            return _ccrypt_aes128(encrypt, key, iv, data)
        except Exception:
            pass
    return _openssl_aes128(encrypt, key, iv, data)


def _ccrypt_aes128(encrypt: bool, key: bytes, iv: bytes, data: bytes) -> bytes:
    import ctypes

    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    if not getattr(lib.CCCrypt, "_grok_bot_typed", False):
        lib.CCCrypt.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        lib.CCCrypt.restype = ctypes.c_int32
        lib.CCCrypt._grok_bot_typed = True
    key_buf = ctypes.create_string_buffer(key, len(key))
    iv_buf = ctypes.create_string_buffer(iv, len(iv))
    in_buf = ctypes.create_string_buffer(data, len(data))
    moved = ctypes.c_size_t(0)
    out_len = len(data) + 32
    out = ctypes.create_string_buffer(out_len)
    rc = lib.CCCrypt(
        0 if encrypt else 1,
        0,
        0x0001,
        ctypes.addressof(key_buf),
        len(key),
        ctypes.addressof(iv_buf),
        ctypes.addressof(in_buf),
        len(data),
        ctypes.addressof(out),
        out_len,
        ctypes.byref(moved),
    )
    if rc != 0:
        raise GrokBotError(f"CCCrypt 失败：{rc}")
    return out.raw[: moved.value]


def _openssl_aes128(encrypt: bool, key: bytes, iv: bytes, data: bytes) -> bytes:
    cmd = [
        "openssl",
        "enc",
        "-aes-128-cbc",
        "-K",
        key.hex(),
        "-iv",
        iv.hex(),
        "-nosalt",
    ]
    if not encrypt:
        cmd.append("-d")
    proc = subprocess.run(
        cmd,
        input=data,
        capture_output=True,
        timeout=8,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[:200]
        raise GrokBotError(f"openssl AES 失败：{err or proc.returncode}")
    return proc.stdout


def oscrypt_encrypt(plaintext: str, password: str) -> str:
    """Electron `safeStorage.encryptString(s).toString("base64")`：Chromium OSCrypt v10。"""
    key = hashlib.pbkdf2_hmac(
        "sha1",
        (password or "").encode("utf-8"),
        _OSCRYPT_SALT,
        _OSCRYPT_ITERATIONS,
        dklen=_OSCRYPT_KEY_LEN,
    )
    ct = _aes128_cbc(True, key, _OSCRYPT_IV, (plaintext or "").encode("utf-8"))
    if not ct.startswith(_OSCRYPT_PREFIX):
        ct = _OSCRYPT_PREFIX + ct
    return base64.b64encode(ct).decode("ascii")


def oscrypt_decrypt(stored: str, password: str) -> str:
    blob = base64.b64decode(stored or "")
    if blob.startswith(_OSCRYPT_PREFIX):
        blob = blob[len(_OSCRYPT_PREFIX) :]
    key = hashlib.pbkdf2_hmac(
        "sha1",
        (password or "").encode("utf-8"),
        _OSCRYPT_SALT,
        _OSCRYPT_ITERATIONS,
        dklen=_OSCRYPT_KEY_LEN,
    )
    pt = _aes128_cbc(False, key, _OSCRYPT_IV, blob)
    return pt.decode("utf-8")


def _safe_storage_password() -> Optional[str]:
    """读 Grok Bot Keychain 项。失败（超时/拒绝）返回 None，由写入走 Hy 明文迁移。"""
    global _SAFE_STORAGE_PASSWORD_CACHE, _SAFE_STORAGE_PASSWORD_TRIED
    if _SAFE_STORAGE_PASSWORD_TRIED:
        return _SAFE_STORAGE_PASSWORD_CACHE
    _SAFE_STORAGE_PASSWORD_TRIED = True
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-w",
                "-s",
                SAFE_STORAGE_SERVICE,
                "-a",
                SAFE_STORAGE_ACCOUNT,
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    pw = (proc.stdout or "").rstrip("\n")
    if pw:
        _SAFE_STORAGE_PASSWORD_CACHE = pw
        return pw
    return None


def encrypt_secret(value: str) -> str:
    pw = _safe_storage_password()
    if not pw:
        raise GrokBotError("Grok Bot Safe Storage 不可用")
    return oscrypt_encrypt(value, pw)


def _jwt_payload(token: str) -> dict:
    try:
        part = (token or "").split(".")[1]
        pad = "=" * ((4 - len(part) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(part + pad))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def principal_from_token(access_token: str) -> str:
    sub = str(_jwt_payload(access_token).get("sub") or "").strip()
    return sub if sub else (access_token or "")


def account_scope(access_token: str) -> str:
    return hashlib.sha256(principal_from_token(access_token).encode("utf-8")).hexdigest()


def account_slot_blob_name() -> str:
    return (
        base64.b32encode(ACCOUNT_SLOT_SLICE.encode("ascii"))
        .decode("ascii")
        .rstrip("=")
        .lower()
    )


def _load_json_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_accounts(data: dict) -> dict:
    raw = data.get(ACCOUNTS_KEY)
    rec: Any = None
    if isinstance(raw, str):
        try:
            rec = json.loads(raw)
        except Exception:
            rec = None
    elif isinstance(raw, dict):
        rec = raw
    if not isinstance(rec, dict):
        rec = {"active": None, "accounts": {}}
    accounts = rec.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    rec["accounts"] = accounts
    return rec


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sand-secrets.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _slot_principal(access_token: str, user_id: Optional[str] = None) -> str:
    principal = principal_from_token(access_token)
    if principal.startswith("auth0|") or principal.startswith("user_"):
        return principal if principal.startswith("auth0|") else ("auth0|" + principal)
    uid = str(user_id or "").strip()
    if uid:
        return uid if uid.startswith("auth0|") else ("auth0|" + uid)
    return principal


def _write_account_slot(access_token: str, user_id: Optional[str] = None) -> None:
    pers = user_data_dir() / PERSISTENCE_DIR
    pers.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "value": _slot_principal(access_token, user_id),
    }
    blob = pers / (account_slot_blob_name() + ".blob")
    blob.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_local_account(
    access_token: str,
    refresh_token: str,
    email: str | None = None,
    user_id: str | None = None,
) -> dict:
    """写入 Grok Bot 自带的 Cursor 账户列表（与侧栏「一键切换」同一套 cursor-accounts）。

    原生 switchAccount 只认 Electron safeStorage（v10）密文；plaintext:v1 写进
    账户槽会被当成不可读。拿得到钥匙串时写入 v10；拿不到则只写顶层
    plaintext:v1，并删掉本账户的嵌套槽，让 Grok Bot 启动时自己加密迁移。
    """
    token = (access_token or "").strip()
    refresh = (refresh_token or token).strip()
    if not token:
        raise GrokBotError("缺少 access_token，无法写入 Grok Bot")
    path = secrets_path()
    data = _load_json_object(path)
    rec = _parse_accounts(data)
    scope = account_scope(token)
    profile = json.dumps({"email": email}, ensure_ascii=False) if email else ""
    pw = _safe_storage_password()
    encrypted = bool(pw) and _oscrypt_matches_existing(pw or "")
    if encrypted:
        access_stored = oscrypt_encrypt(token, pw or "")
        refresh_stored = oscrypt_encrypt(refresh, pw or "")
        profile_stored = oscrypt_encrypt(profile, pw or "") if profile else ""
    else:
        access_stored = wrap_plaintext(token)
        refresh_stored = wrap_plaintext(refresh)
        profile_stored = wrap_plaintext(profile) if profile else ""

    if encrypted:
        slot = dict(rec["accounts"].get(scope) or {})
        slot[ACCESS_KEY] = access_stored
        slot[REFRESH_KEY] = refresh_stored
        if profile_stored:
            slot[PROFILE_KEY] = profile_stored
        rec["accounts"][scope] = slot
        rec["active"] = scope
        data[ACCESS_KEY] = access_stored
        data[REFRESH_KEY] = refresh_stored
    else:
        rec["accounts"].pop(scope, None)
        if rec.get("active") == scope:
            rec["active"] = None
        data[ACCESS_KEY] = access_stored
        data[REFRESH_KEY] = refresh_stored
    data[ACCOUNTS_KEY] = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    _write_account_slot(token, user_id)
    return {
        "ok": True,
        "accountId": scope,
        "path": str(path),
        "encrypted": encrypted,
    }


def _oscrypt_matches_existing(password: str) -> bool:
    """已有 v10 密文时，必须能解开才允许按同一把钥匙写入，避免写坏侧栏账户。"""
    data = _load_json_object(secrets_path())
    rec = _parse_accounts(data)
    samples: List[str] = []
    top = data.get(ACCESS_KEY)
    if isinstance(top, str) and top and not top.startswith(PLAINTEXT_PREFIX):
        samples.append(top)
    for slot in rec["accounts"].values():
        if not isinstance(slot, dict):
            continue
        for key in (ACCESS_KEY, REFRESH_KEY, PROFILE_KEY):
            val = slot.get(key)
            if isinstance(val, str) and val and not val.startswith(PLAINTEXT_PREFIX):
                samples.append(val)
    if not samples:
        return True
    for blob in samples:
        try:
            oscrypt_decrypt(blob, password)
            return True
        except Exception:
            continue
    return False


def _is_windows() -> bool:
    return sys.platform == "win32" or os.name == "nt"


def missing_app_message(*, cursor_untouched: bool = False) -> str:
    if _is_windows():
        text = "未找到本机 Grok Bot.exe。已在常见安装目录、卸载注册表和开始菜单里查找。"
    else:
        text = "未找到本机 Grok Bot 客户端（例如 /Applications/Grok Bot.app）。"
    if cursor_untouched:
        text += "Cursor 未关闭。"
    return text


def _windows_exe_in(directory: str) -> str:
    return directory.rstrip("\\/") + "\\" + EXE_NAME


def exe_paths_from_registry_text(text: str) -> List[str]:
    """从 reg query 文本里取出 InstallLocation / DisplayIcon 指向的 Grok Bot.exe。"""
    import re

    found: List[str] = []
    seen = set()
    for line in (text or "").splitlines():
        match = re.match(r"\s*(\S+)\s+REG_\w+\s+(.*)$", line)
        if not match:
            continue
        name, raw = match.group(1), match.group(2).strip().strip('"')
        raw = re.sub(r",\s*-?\d+$", "", raw).strip().strip('"')
        if not raw:
            continue
        if name in ("DisplayIcon", "UninstallString", "QuietUninstallString"):
            exe = raw
            lower = exe.lower()
            if ".exe" in lower:
                exe = exe[: lower.rfind(".exe") + 4]
            if not exe.lower().endswith(".exe"):
                continue
        elif name in ("InstallLocation", "InstallDir"):
            exe = _windows_exe_in(raw)
        elif name in ("(Default)",):
            if not raw.lower().endswith(".exe"):
                continue
            exe = raw
        else:
            continue
        key = exe.lower()
        if key not in seen:
            seen.add(key)
            found.append(exe)
    return found


def _windows_search_roots() -> List[Path]:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
    program_files_x86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    program_w6432 = os.environ.get("ProgramW6432") or program_files
    return [
        Path(local) / "Programs",
        Path(local),
        Path(program_files),
        Path(program_files_x86),
        Path(program_w6432),
    ]


def _walk_for_exe(root: Path, max_depth: int = 3) -> Optional[Path]:
    if not root.is_dir():
        return None
    root_depth = len(root.parts)
    try:
        for current, dirs, files in os.walk(root):
            depth = len(Path(current).parts) - root_depth
            if depth > max_depth:
                dirs.clear()
                continue
            if EXE_NAME in files:
                return Path(current) / EXE_NAME
    except OSError:
        return None
    return None


def _registry_exe_candidates() -> List[str]:
    if not _is_windows():
        return []
    queries = [
        ["reg", "query", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall", "/s", "/f", APP_NAME],
        ["reg", "query", r"HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall", "/s", "/f", APP_NAME],
        ["reg", "query", r"HKLM\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "/s", "/f", APP_NAME],
        ["reg", "query", rf"HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths\{EXE_NAME}"],
        ["reg", "query", rf"HKLM\Software\Microsoft\Windows\CurrentVersion\App Paths\{EXE_NAME}"],
    ]
    found: List[str] = []
    for cmd in queries:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        found.extend(exe_paths_from_registry_text(proc.stdout or ""))
    return found


def _start_menu_exe_candidates() -> List[str]:
    if not _is_windows():
        return []
    appdata = os.environ.get("APPDATA") or ""
    program_data = os.environ.get("ProgramData") or r"C:\ProgramData"
    roots = [
        str(Path(appdata) / "Microsoft" / "Windows" / "Start Menu") if appdata else "",
        str(Path(program_data) / "Microsoft" / "Windows" / "Start Menu"),
    ]
    script = (
        "$sh = New-Object -ComObject WScript.Shell; "
        "$roots = @(" + ",".join("'" + r.replace("'", "''") + "'" for r in roots if r) + "); "
        "Get-ChildItem -Path $roots -Recurse -Filter '*Grok Bot*.lnk' -ErrorAction SilentlyContinue | "
        "ForEach-Object { $sh.CreateShortcut($_.FullName).TargetPath }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    out: List[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip().strip('"')
        if line.lower().endswith(".exe"):
            out.append(line)
    return out


def _running_exe_candidates() -> List[str]:
    if not _is_windows():
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='Grok Bot.exe'\" | "
        "Select-Object -ExpandProperty ExecutablePath"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip().lower().endswith(".exe")]


def _first_existing_exe(candidates: List[str]) -> Optional[Path]:
    for raw in candidates:
        path = Path(raw)
        if path.is_file() and path.name.lower() == EXE_NAME.lower():
            return path
        if path.is_dir():
            nested = path / EXE_NAME
            if nested.is_file():
                return nested
    return None


def _search_windows_exe() -> Optional[Path]:
    """在 Windows 上查找已安装的 Grok Bot.exe：正在运行的进程、注册表、开始菜单、常见安装目录。"""
    hit = _first_existing_exe(_running_exe_candidates())
    if hit:
        return hit
    hit = _first_existing_exe(_registry_exe_candidates())
    if hit:
        return hit
    hit = _first_existing_exe(_start_menu_exe_candidates())
    if hit:
        return hit
    for root in _windows_search_roots():
        found = _walk_for_exe(root, max_depth=3)
        if found:
            return found
    return None


def find_app() -> Optional[Path]:
    if sys.platform == "darwin":
        for candidate in (
            Path("/Applications") / f"{APP_NAME}.app",
            Path.home() / "Applications" / f"{APP_NAME}.app",
        ):
            if candidate.is_dir():
                return candidate
        return None
    if _is_windows():
        return _search_windows_exe()
    return None


def _running_pids() -> List[int]:
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["pgrep", "-f", "/Grok Bot.app/"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        pids: List[int] = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids
    return []


def _wait_for_exit(timeout: float = 12) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _running_pids():
            return True
        time.sleep(0.2)
    return not _running_pids()


def close_grok_bot() -> int:
    """只退出 Grok Bot，绝不碰 Cursor。"""
    if sys.platform == "darwin":
        pids = _running_pids()
        if not pids:
            return 0
        osa = shutil.which("osascript") or "/usr/bin/osascript"
        subprocess.run(
            [osa, "-e", f'tell application id "{BUNDLE_ID}" to quit'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        if _wait_for_exit(8):
            return len(set(pids))
        for pid in _running_pids():
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        if not _wait_for_exit(3):
            for pid in _running_pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            _wait_for_exit(2)
        return len(set(pids))
    if sys.platform == "win32" or os.name == "nt":
        subprocess.run(
            ["taskkill", "/IM", f"{APP_NAME}.exe", "/T"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        return 0
    return 0


def start_grok_bot() -> bool:
    app = find_app()
    if app is None:
        raise GrokBotError(missing_app_message())
    try:
        if sys.platform == "darwin":
            cmd = [shutil.which("open") or "/usr/bin/open", "-a", str(app)]
            subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
            return True
        if sys.platform == "win32" or os.name == "nt":
            flags = 0x00000200  # CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(
                [str(app)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=flags,
            )
            return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GrokBotError(f"无法打开 Grok Bot：{exc}") from exc
    raise GrokBotError("当前仅支持 Windows 和 macOS")


_CHROME_LOGIN_SCRIPT = """
if application "Google Chrome" is running then
  tell application "Google Chrome"
    repeat with w in windows
      repeat with t in tabs of w
        set u to URL of t as text
        if u contains "loginDeepControl" then return u
      end repeat
    end repeat
  end tell
end if
"""

_SAFARI_LOGIN_SCRIPT = """
if application "Safari" is running then
  tell application "Safari"
    repeat with w in windows
      repeat with t in tabs of w
        set u to URL of t as text
        if u contains "loginDeepControl" then return u
      end repeat
    end repeat
  end tell
end if
"""

_EDGE_LOGIN_SCRIPT = """
if application "Microsoft Edge" is running then
  tell application "Microsoft Edge"
    repeat with w in windows
      repeat with t in tabs of w
        set u to URL of t as text
        if u contains "loginDeepControl" then return u
      end repeat
    end repeat
  end tell
end if
"""


def _osascript(script: str) -> str:
    osa = shutil.which("osascript") or "/usr/bin/osascript"
    try:
        proc = subprocess.run(
            [osa, "-e", script],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip()


def detect_login_deep_url() -> Optional[str]:
    """从本机已打开的浏览器标签里找 Grok Bot 刚打开的 loginDeepControl。"""
    if sys.platform != "darwin":
        return None
    for script in (_CHROME_LOGIN_SCRIPT, _SAFARI_LOGIN_SCRIPT, _EDGE_LOGIN_SCRIPT):
        text = _osascript(script)
        for line in text.splitlines():
            line = line.strip().strip('"')
            if "loginDeepControl" in line:
                return line
    return None


def wait_for_login_deep_url(timeout: float = 12, interval: float = 0.4) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = detect_login_deep_url()
        if found:
            return found
        time.sleep(interval)
    return detect_login_deep_url() or ""

