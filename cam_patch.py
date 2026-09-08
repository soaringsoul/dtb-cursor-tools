"""本机 Cursor 补丁：直接调用 cursor-account-manager 的 sandCli / sandPatcher。

桌面工具只负责定位安装、关/开 Cursor、出报告；文件改写、校验、备份与
3.18.9 / 3.18.25 / 3.19.13 规则全部以插件源码为准，避免两套补丁漂移。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union


TESTED_CURSOR_VERSIONS = ("3.18.9", "3.18.25", "3.19.13")

# 与 sandStream.js EXPECTED_LIFECYCLE 对齐。
EXPECTED_LIFECYCLE = {
    "managedLocal": 1,
    "runtimeLoad": 1,
    "moveExec": 1,
    "directStream": 1,
    "agentHost": 2,
    "identity": 1,
    "subagentRoute": 1,
    "subagentSession": 1,
    "taskTool": 1,
    "actionRoute": 1,
    "resumeMode": 1,
    "completionWake": 2,
}

RULE_DEFS: Sequence[Dict[str, Any]] = (
    {
        "key": "header",
        "title": "请求头改为 sand",
        "why": "x-cursor-client-type 走 sand，服务端按 Grok Bot 计到 Bot 额度",
        "stream": False,
        "optional": False,
    },
    {
        "key": "client",
        "title": "客户端身份标记",
        "why": "写入 SAND_CLIENT 标记，后续注入以此识别已改身份",
        "stream": False,
        "optional": False,
        "stat": "client",
        "want": 1,
    },
    {
        "key": "eligibility",
        "title": "资格判定短路",
        "why": "跳过客户端里「是否允许 Sand」的管理员设置判定",
        "stream": True,
        "optional": False,
        "stat": "eligibility",
        "want": 1,
    },
    {
        "key": "managedLocal",
        "title": "agent-host 强制走本地回路",
        "why": "对话在本机 agent-host 以 sand 身份推理，这是计到 Bot 额度的核心",
        "stream": True,
        "optional": False,
        "stat": "managedLocal",
        "want": 1,
    },
    {
        "key": "runtimeLoad",
        "title": "强制加载本地 runtime",
        "why": "无视 agent_host_local_loop 灰度，始终加载 managed-local runtime",
        "stream": True,
        "optional": False,
        "stat": "runtimeLoad",
        "want": 1,
    },
    {
        "key": "directStream",
        "title": "直连推理 Stream",
        "why": "本地回路走 InferenceService Stream，而不是云端 Agent Run",
        "stream": True,
        "optional": False,
        "stat": "directStream",
        "want": 1,
    },
    {
        "key": "agentHost",
        "title": "强制开启 agent host",
        "why": "workbench 侧无视 cursorAgentHostEnabled 开关",
        "stream": True,
        "optional": False,
        "stat": "agentHost",
        "want": 2,
    },
    {
        "key": "identity",
        "title": "agent-host 身份 ide → sand",
        "why": "本地回路发出的推理请求以 sand 客户端身份出现",
        "stream": True,
        "optional": False,
        "stat": "identity",
        "want": 1,
    },
    {
        "key": "moveExec",
        "title": "move_exec：host 自带工具执行器",
        "why": "读写文件 / 终端由 agent-host 同包提供，不再等 cursor-agent-exec 注册",
        "stream": True,
        "optional": False,
        "stat": "moveExec",
        "want": 1,
    },
    {
        "key": "subagentRoute",
        "title": "子代理走本地回路",
        "why": "子代理 turn 不再因 runOptions 回落云端",
        "stream": True,
        "optional": False,
        "stat": "subagentRoute",
        "want": 1,
    },
    {
        "key": "subagentSession",
        "title": "子代理会话本地化",
        "why": "子代理 session 也走 managed-local",
        "stream": True,
        "optional": False,
        "stat": "subagentSession",
        "want": 1,
    },
    {
        "key": "taskTool",
        "title": "Task 工具走本地（V2）",
        "why": "Task / 子代理工具调用留在 Bot 回路",
        "stream": True,
        "optional": False,
        "stat": "taskTool",
        "want": 1,
    },
    {
        "key": "actionRoute",
        "title": "后台动作走本地",
        "why": "后台任务完成通知不再回落云端被 401",
        "stream": True,
        "optional": False,
        "stat": "actionRoute",
        "want": 1,
    },
    {
        "key": "resumeMode",
        "title": "子代理 resume 走 Agent 模式",
        "why": "resume 时保持本地 Agent 回路",
        "stream": True,
        "optional": False,
        "stat": "resumeMode",
        "want": 1,
    },
    {
        "key": "completionWake",
        "title": "后台完成唤醒走本地",
        "why": "后台命令跑完后的唤醒不再弹 unexpected error",
        "stream": True,
        "optional": False,
        "stat": "completionWake",
        "want": 2,
    },
    {
        "key": "pushContextTimeout",
        "title": "push_req_context 超时加长",
        "why": "避免上下文推送 10s 超时",
        "stream": True,
        "optional": True,
        "stat": "pushContextTimeout",
        "want": 1,
    },
    {
        "key": "rulesPreseed",
        "title": "规则预填充",
        "why": "本地回路预置规则提示",
        "stream": True,
        "optional": True,
        "stat": "rulesPreseed",
        "want": 1,
    },
)


class CamPatchError(RuntimeError):
    pass


def is_tested_version(version: str) -> bool:
    text = str(version or "").strip()
    return any(text == item or text.startswith(item + ".") for item in TESTED_CURSOR_VERSIONS)


def required_version_label() -> str:
    return " / ".join(TESTED_CURSOR_VERSIONS)


def _bundle_root() -> Path:
    here = Path(__file__).resolve().parent
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else here


def cam_src_dir() -> Path:
    root = _bundle_root()
    path = root / "cursor-account-manager-main" / "src"
    if not (path / "sandCli.js").is_file():
        raise CamPatchError(f"找不到插件补丁脚本：{path / 'sandCli.js'}")
    return path


def find_node() -> str:
    found = shutil.which("node") or shutil.which("nodejs")
    if found:
        return found
    raise CamPatchError(
        "未找到 Node.js。本机 Cursor 补丁已改为使用 cursor-account-manager 同一套规则，请先安装 Node.js 后再试。"
    )


def plugin_default_state_root() -> Path:
    """与 sandPatcher.defaultStateRoot() 一致。"""
    env = os.environ.get("CURSOR_SAND_ROUTER_STATE")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor Sand Router"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home()) / "Cursor Sand Router"
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "cursor-sand-router"


def legacy_sandclaimer_state_root() -> Path:
    """旧版 SandClaimer 自己写过备份的目录，回退时仍要搜到。"""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return base / "SandClaimer" / "sand-router"


def cursor_global_storage_dir() -> Path:
    """与 extension.js sandGlobalStorageDir() 一致。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home()) / "Cursor" / "User" / "globalStorage"
    return Path.home() / ".config" / "Cursor" / "User" / "globalStorage"


def state_root() -> Path:
    """打补丁时写入备份的目录：插件 CLI 的 defaultStateRoot。"""
    return plugin_default_state_root()


def known_sand_state_roots() -> List[Path]:
    """与 extension.js knownSandStateRoots() 对齐，并额外包含旧 SandClaimer 目录。"""
    seen: List[str] = []
    out: List[Path] = []

    def add(value: Union[str, Path, None]) -> None:
        if value is None:
            return
        text = str(value).strip()
        if not text or text in seen:
            return
        seen.append(text)
        out.append(Path(text))

    add(plugin_default_state_root())
    gs = cursor_global_storage_dir()
    add(gs / "leila-local.cursor-sand-router")
    add(legacy_sandclaimer_state_root())
    try:
        if gs.is_dir():
            for name in gs.iterdir():
                if re.search(r"sand-router|sandrouter", name.name, re.I):
                    add(name)
    except OSError:
        pass
    return out


def manifest_paths(state_dir: Union[str, Path]) -> List[Path]:
    backups = Path(state_dir).resolve() / "backups"
    if not backups.is_dir():
        return []
    files: List[Path] = []
    try:
        for entry in backups.iterdir():
            if not entry.is_dir():
                continue
            man = entry / "manifest.json"
            if man.is_file():
                files.append(man)
    except OSError:
        return []
    files.sort(key=lambda item: item.as_posix(), reverse=True)
    return files


def find_latest_manifest(app_root: Union[str, Path], state_dir: Union[str, Path]) -> Optional[Path]:
    root = str(Path(app_root).resolve())
    for candidate in manifest_paths(state_dir):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        try:
            stored = str(Path(str(data.get("appRoot") or "")).resolve())
        except Exception:
            continue
        if stored == root:
            return candidate
    return None


def pick_restore_state_root(app_root: Union[str, Path]) -> Path:
    """与 extension.js pickRestoreStateRoot() 一致。"""
    roots = known_sand_state_roots()
    if app_root:
        for root in roots:
            try:
                if find_latest_manifest(app_root, root):
                    return root
            except Exception:
                continue
    for root in roots:
        try:
            if (Path(root) / "backups").is_dir():
                return root
        except Exception:
            continue
    return cursor_global_storage_dir() / "leila-local.cursor-sand-router"


def _clean_error(stderr: str, stdout: str) -> str:
    text = (stderr or stdout or "").strip()
    text = re.sub(r"^cursor-account-manager-sand:\s*", "", text)
    first = text.splitlines()[0].strip() if text else ""
    return first or "插件补丁脚本失败"


def _is_eperm(exc: BaseException) -> bool:
    text = str(exc or "")
    return getattr(exc, "code", None) == "EPERM" or "EPERM" in text


def _cli_argv(
    command: str,
    app_root: Union[str, Path],
    extra: Optional[Sequence[str]] = None,
    state_dir: Optional[Union[str, Path]] = None,
) -> List[str]:
    args = [
        command,
        "--app-root",
        str(Path(app_root).resolve()),
        "--state-root",
        str(Path(state_dir or state_root()).resolve()),
        "--json",
    ]
    if extra:
        args.extend(extra)
    return args


def _parse_cli_json(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        raise CamPatchError("插件补丁脚本没有返回 JSON")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CamPatchError(f"插件补丁脚本输出无法解析：{text[:240]}") from exc
    if not isinstance(data, dict):
        raise CamPatchError("插件补丁脚本返回了非对象 JSON")
    return data


def run_cli(
    command: str,
    app_root: Union[str, Path],
    extra: Optional[Sequence[str]] = None,
    state_dir: Optional[Union[str, Path]] = None,
) -> dict:
    node = find_node()
    cli = cam_src_dir() / "sandCli.js"
    args = [node, str(cli), *_cli_argv(command, app_root, extra, state_dir)]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(cam_src_dir()),
        )
    except subprocess.TimeoutExpired as exc:
        raise CamPatchError("插件补丁脚本超时（180 秒）") from exc
    if proc.returncode != 0:
        raise CamPatchError(_clean_error(proc.stderr, proc.stdout))
    return _parse_cli_json(proc.stdout)


def _parse_elevated_output(tmp_out: Path) -> dict:
    raw = ""
    try:
        if tmp_out.is_file():
            raw = tmp_out.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        raw = ""
    try:
        tmp_out.unlink(missing_ok=True)
    except OSError:
        pass
    if not raw:
        return {"changed": True}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    if "error" in raw.lower():
        raise CamPatchError(_clean_error(raw, ""))
    return {"changed": True}


def _run_elevated_darwin(node: str, cli: Path, argv: Sequence[str]) -> dict:
    tmp_out = Path(tempfile.gettempdir()) / f"cam_sand_{os.getpid()}.json"
    quoted = " ".join(shlex.quote(part) for part in [node, str(cli), *argv])
    shell_cmd = f"{quoted} > {shlex.quote(str(tmp_out))} 2>&1"
    escaped = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    osa = f'do shell script "{escaped}" with administrator privileges'
    try:
        proc = subprocess.run(
            ["osascript", "-e", osa],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise CamPatchError("提权执行超时") from exc
    if proc.returncode != 0:
        try:
            tmp_out.unlink(missing_ok=True)
        except OSError:
            pass
        detail = (proc.stderr or proc.stdout or "").strip() or "osascript failed"
        raise CamPatchError(f"提权执行失败（用户可能取消了密码输入）: {detail}")
    return _parse_elevated_output(tmp_out)


def _run_elevated_win32(node: str, cli: Path, argv: Sequence[str]) -> dict:
    tmp_out = Path(tempfile.gettempdir()) / f"cam_sand_{os.getpid()}.json"
    tmp_cmd = Path(tempfile.gettempdir()) / f"cam_sand_{os.getpid()}.cmd"
    quoted = " ".join(f'"{part}"' for part in [node, str(cli), *argv])
    tmp_cmd.write_text(f"@echo off\r\n{quoted} > \"{tmp_out}\" 2>&1\r\n", encoding="utf-8")
    ps_path = str(tmp_cmd).replace("'", "''")
    ps = f"Start-Process -FilePath '{ps_path}' -Verb RunAs -Wait -WindowStyle Hidden"
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise CamPatchError("提权执行超时") from exc
    finally:
        try:
            tmp_cmd.unlink(missing_ok=True)
        except OSError:
            pass
    if proc.returncode != 0:
        try:
            tmp_out.unlink(missing_ok=True)
        except OSError:
            pass
        detail = (proc.stderr or proc.stdout or "").strip() or "UAC failed"
        raise CamPatchError(f"提权执行失败（用户可能取消了 UAC）: {detail}")
    return _parse_elevated_output(tmp_out)


def _run_elevated_linux(node: str, cli: Path, argv: Sequence[str]) -> dict:
    tmp_out = Path(tempfile.gettempdir()) / f"cam_sand_{os.getpid()}.json"
    quoted = " ".join(shlex.quote(part) for part in [node, str(cli), *argv])
    shell_cmd = f"{quoted} > {shlex.quote(str(tmp_out))} 2>&1"
    try:
        proc = subprocess.run(
            ["pkexec", "bash", "-c", shell_cmd],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise CamPatchError("提权执行超时") from exc
    except FileNotFoundError as exc:
        raise CamPatchError("提权执行失败: 未找到 pkexec") from exc
    if proc.returncode != 0:
        try:
            tmp_out.unlink(missing_ok=True)
        except OSError:
            pass
        detail = (proc.stderr or proc.stdout or "").strip() or "pkexec failed"
        raise CamPatchError(f"提权执行失败: {detail}")
    return _parse_elevated_output(tmp_out)


def run_elevated_cli(
    command: str,
    app_root: Union[str, Path],
    extra: Optional[Sequence[str]] = None,
    state_dir: Optional[Union[str, Path]] = None,
) -> dict:
    """与 extension.js runElevated(sandCli.js, …) 一致：EPERM 时提权再跑同一套 CLI。"""
    node = find_node()
    cli = cam_src_dir() / "sandCli.js"
    argv = _cli_argv(command, app_root, extra, state_dir)
    if sys.platform == "darwin":
        return _run_elevated_darwin(node, cli, argv)
    if sys.platform == "win32":
        return _run_elevated_win32(node, cli, argv)
    return _run_elevated_linux(node, cli, argv)


def inspect(app_root: Union[str, Path]) -> dict:
    return run_cli("status", app_root)


def apply(app_root: Union[str, Path], dry_run: bool = False) -> dict:
    extra = ["--dry-run"] if dry_run else None
    try:
        return run_cli("apply", app_root, extra)
    except CamPatchError as exc:
        if dry_run or not _is_eperm(exc):
            raise
        return run_elevated_cli("apply", app_root, extra)


def restore(app_root: Union[str, Path], force: bool = True) -> dict:
    extra = ["--force"] if force else None
    preferred = pick_restore_state_root(app_root)
    roots = [preferred] + [root for root in known_sand_state_roots() if root != preferred]
    last_err: Optional[BaseException] = None
    for root in roots:
        try:
            try:
                return run_cli("restore", app_root, extra, state_dir=root)
            except CamPatchError as exc:
                if _is_eperm(exc):
                    return run_elevated_cli("restore", app_root, extra, state_dir=root)
                raise
        except CamPatchError as exc:
            last_err = exc
    raise last_err or CamPatchError("没有找到可回滚的备份")


def _count(stream: dict, key: str) -> int:
    try:
        return int(stream.get(key) or 0)
    except Exception:
        return 0


def rule_rows(inspect_data: dict) -> List[dict]:
    totals = inspect_data.get("totals") if isinstance(inspect_data, dict) else {}
    if not isinstance(totals, dict):
        totals = {}
    stream = totals.get("stream") if isinstance(totals.get("stream"), dict) else {}
    sand = int(totals.get("sandAssignments") or 0)
    unpatched = int(totals.get("unpatchedAssignments") or 0)
    out: List[dict] = []
    for spec in RULE_DEFS:
        if spec["key"] == "header":
            if sand > 0 and unpatched == 0:
                status, markers = "applied", sand
            elif unpatched > 0 and sand > 0:
                status, markers = "partial", sand
            elif unpatched > 0:
                status, markers = "pending", 0
            else:
                status, markers = "missing", 0
        else:
            markers = _count(stream, spec["stat"])
            want = int(spec.get("want") or 1)
            if markers >= want:
                status = "applied"
            elif markers > 0:
                status = "partial"
            elif is_tested_version(str(inspect_data.get("version") or "")):
                status = "pending"
            else:
                status = "missing"
        labels = {"applied": "已生效", "partial": "部分生效", "pending": "未打", "missing": "锚点缺失"}
        fix = ""
        if status == "pending":
            fix = "重新点一次「打补丁」即可补上（会自动重启 Cursor）。"
        elif status == "partial":
            fix = "有锚点没被替换到，重新点一次「打补丁」。"
        elif status == "missing" and not spec["optional"]:
            fix = (
                f"当前 Cursor 可能不是 {required_version_label()}："
                "请安装已测试版本并关闭自动更新后重试。"
            )
        out.append(
            {
                "key": spec["key"],
                "title": spec["title"],
                "why": spec["why"],
                "stream": spec["stream"],
                "optional": spec["optional"],
                "status": status,
                "statusLabel": labels[status],
                "markers": markers,
                "files": [],
                "fix": fix,
            }
        )
    leftover = _count(stream, "legacyTaskTool")
    if leftover:
        out.append(
            {
                "key": "legacyTaskTool",
                "title": "旧 Task 补丁残留",
                "why": "V1 Task 标记还在，需要重新打补丁升级到 V2",
                "stream": True,
                "optional": False,
                "status": "partial",
                "statusLabel": "部分生效",
                "markers": leftover,
                "files": [],
                "fix": "重新点一次「打补丁」升级（会自动重启 Cursor）。",
            }
        )
    return out
