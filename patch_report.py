"""补丁报告：把「检测 / 打补丁 / 验证生效」拆成逐条规则、逐个步骤的可读结果。

群友反馈最多的是「显示成功，其实没成功」和「失败了不知道哪里失败」。原因归为四类，这里逐一给出证据：
  1. 规则层面：某条锚点在这台 Cursor 里没命中（版本 / 构建不符）→ 逐条列出 已生效 / 未打 / 锚点缺失 / 部分。
  2. 写入层面：没权限、Cursor 关不掉、文件被外部改动、写后校验失败 → 每一步单独报 ok / warn / fail + 修法。
  3. 进程层面：补丁写进了 A 目录，平时双击运行的却是 B 目录的 Cursor（本机多个安装）；或 Cursor 根本没重启。
  4. 账号层面：补丁没问题，但本机登录的号没有 Sand 资格 / 票已失效，用起来照样报错。
另外提供「验证生效」：读 Cursor 自己的 agent-host 日志，看本地回路是否真的加载、最近几轮到底走了
managed-local 还是 connect、为什么回落、有没有 401。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import cam_patch
import sand_patch as sp

# ---------------------------------------------------------------------------
# 规则清单
# ---------------------------------------------------------------------------

VERSION_FIX = (
    "当前 Cursor 可能不是 3.18.9 / 3.18.25 / 3.19.13："
    "请安装已测试版本并关闭自动更新后重试。"
)
REPATCH_FIX = "重新点一次「打补丁」即可补上（会自动重启 Cursor）。"

STATUS_LABEL = {
    "applied": "已生效",
    "partial": "部分生效",
    "pending": "未打",
    "missing": "锚点缺失",
}


def _rel(layout: sp.CursorLayout, path: Path) -> str:
    try:
        return path.resolve().relative_to(layout.app_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def rule_status(layout: sp.CursorLayout, contents: Optional[Dict[Path, str]] = None, inspect_data: Optional[dict] = None) -> List[dict]:
    """逐条规则来自插件 inspect 的 stream / header 计数。"""
    data = inspect_data
    if data is None:
        data = cam_patch.inspect(layout.app_root)
    return cam_patch.rule_rows(data)


def summarize_rules(rules: Sequence[dict]) -> dict:
    """给一组规则状态下结论：full / partial / none，以及缺了哪些。"""
    required = [r for r in rules if not r["optional"]]
    applied = [r for r in required if r["status"] == "applied"]
    missing = [r for r in required if r["status"] == "missing"]
    pending = [r for r in required if r["status"] in ("pending", "partial")]
    if len(applied) == len(required):
        verdict = "full"
    elif applied:
        verdict = "partial"
    else:
        verdict = "none"
    return {
        "verdict": verdict,
        "applied": len(applied),
        "required": len(required),
        "missing": [r["title"] for r in missing],
        "pending": [r["title"] for r in pending],
    }


# ---------------------------------------------------------------------------
# 进程 / 安装位置
# ---------------------------------------------------------------------------


def running_cursor_paths() -> List[str]:
    """当前正在运行的 Cursor 可执行文件路径（去重）。"""
    if sys.platform == "win32":
        paths = sp._windows_running_candidates()
    elif sys.platform == "darwin":
        paths = [str(p) for _pid, p in sp._mac_process_paths()]
    else:
        paths = []
    seen: Dict[str, str] = {}
    for p in paths:
        if p and "cursor" in p.lower():
            seen.setdefault(os.path.normcase(os.path.normpath(p)), p)
    return list(seen.values())


def _same_install(layout: sp.CursorLayout, exe_path: str) -> bool:
    try:
        return sp._is_within(Path(exe_path).resolve(), layout.install_root.resolve())
    except Exception:
        return False


def other_installs(layout: sp.CursorLayout) -> List[str]:
    """本机其他 Cursor 安装（注册表 / 运行中进程 / 默认目录），排除当前选中的。"""
    found: Dict[str, str] = {}
    for _source, values in sp._default_candidate_groups():
        for other in sp._valid_layouts(tuple(values)):
            if sp._path_key(other.app_root) != sp._path_key(layout.app_root):
                found.setdefault(sp._path_key(other.install_root), str(other.install_root))
    return list(found.values())


def wait_for_cursor(layout: sp.CursorLayout, timeout: float = 10.0) -> Tuple[List[str], List[str]]:
    """等 Cursor 起来；返回 (从补丁目录启动的进程路径, 从别的目录启动的进程路径)。"""
    deadline = time.monotonic() + timeout
    ours: List[str] = []
    others: List[str] = []
    while time.monotonic() < deadline:
        paths = running_cursor_paths()
        ours = [p for p in paths if _same_install(layout, p)]
        others = [p for p in paths if not _same_install(layout, p)]
        if ours:
            break
        time.sleep(0.5)
    return ours, others


# ---------------------------------------------------------------------------
# 安装报告
# ---------------------------------------------------------------------------


@dataclass
class Report:
    version: str = ""
    path: str = ""
    steps: List[dict] = field(default_factory=list)
    rulesBefore: List[dict] = field(default_factory=list)
    rulesAfter: List[dict] = field(default_factory=list)
    backupDir: str = ""
    verdict: str = "failed"  # full / partial / failed
    headline: str = ""

    def step(self, key: str, title: str, status: str, detail: str = "", fix: str = "") -> dict:
        item = {"key": key, "title": title, "status": status, "detail": detail, "fix": fix}
        self.steps.append(item)
        return item

    def to_dict(self) -> dict:
        return {
            "ok": self.verdict != "failed",
            "verdict": self.verdict,
            "headline": self.headline,
            "version": self.version,
            "path": self.path,
            "steps": self.steps,
            "rulesBefore": self.rulesBefore,
            "rulesAfter": self.rulesAfter,
            "backupDir": self.backupDir,
        }


def _finish(report: Report) -> dict:
    fails = [s for s in report.steps if s["status"] == "fail"]
    warns = [s for s in report.steps if s["status"] == "warn"]
    rules = summarize_rules(report.rulesAfter or report.rulesBefore)
    if fails:
        report.verdict = "failed"
        report.headline = "补丁失败：" + fails[0]["title"] + "——" + fails[0]["detail"]
    elif rules["verdict"] != "full":
        report.verdict = "partial"
        report.headline = (
            f"补丁只生效了 {rules['applied']}/{rules['required']} 条"
            + ("，缺锚点：" + "、".join(rules["missing"]) if rules["missing"] else "")
            + ("，未打：" + "、".join(rules["pending"]) if rules["pending"] else "")
        )
    elif warns:
        report.verdict = "partial"
        report.headline = "补丁已写入并校验通过，但有需要你确认的地方：" + warns[0]["title"]
    else:
        report.verdict = "full"
        report.headline = "补丁全部生效，Cursor 已从补丁目录重启"
    return report.to_dict()


def account_check() -> dict:
    """本机登录号有没有 Sand 资格（补丁再对，号不行也用不了）。只读、失败不阻塞。"""
    try:
        import local_cursor
        import sand_api
    except Exception as exc:  # pragma: no cover
        return {"status": "skip", "detail": f"无法加载账号检查模块：{exc}"}
    try:
        acct = local_cursor.read_local_account()
    except Exception as exc:
        return {"status": "skip", "detail": f"读取本机 Cursor 登录态失败：{exc}"}
    if not acct or not acct.get("token"):
        return {"status": "warn", "detail": "本机 Cursor 未登录：打完补丁也要先登录一个有 Sand 资格的号", "fix": "在 Cursor 里登录，或用本工具「切号」切一个已开通 Sand 的号"}
    email = acct.get("email") or ""
    try:
        code, usage = sand_api.fetch_usage_with_code(sand_api.parse_token(acct["token"])[1])
    except Exception as exc:
        return {"status": "skip", "detail": f"Sand 额度接口无法访问（网络？）：{exc}", "email": email}
    if code in (401, 403):
        return {"status": "warn", "email": email, "detail": f"本机登录号 {email} 的登录票已失效（HTTP {code}）：Cursor 里会提示重新登录，补丁无关", "fix": "在 Cursor 里退出重登，或用本工具「切号」换一个有效号"}
    if code != 200 or not usage:
        return {"status": "skip", "email": email, "detail": f"Sand 额度接口返回 HTTP {code}，暂时无法判断资格"}
    if not usage.get("unlocked"):
        return {"status": "warn", "email": email, "detail": f"本机登录号 {email} 没有 Sand 资格：补丁打了也用不了 Bot", "fix": "免费号需绑卡领取；或切到 Pro / Pro+ / Ultra 号（套餐自带 Sand）"}
    pct = usage.get("percent")
    avail = usage.get("hasAvailableUsage")
    detail = f"本机登录号 {email} 已开通 Sand，Bot 本周用量 {pct if pct is not None else '?'}%"
    if avail is False:
        return {"status": "warn", "email": email, "detail": detail + "，但本周额度已用尽", "fix": "等下次重置（列表里有倒计时）或切另一个号"}
    return {"status": "ok", "email": email, "detail": detail}


def install_with_report(layout: sp.CursorLayout) -> dict:
    """带逐步报告的安装。文件改写交给 cursor-account-manager 的 applyPatch。"""
    report = Report(version=layout.version, path=str(layout.install_root))
    report.step("locate", "定位 Cursor", "ok", f"Cursor {layout.version} · {layout.install_root}")

    others = other_installs(layout)
    if others:
        report.step(
            "multi",
            "本机存在多个 Cursor 安装",
            "warn",
            "补丁只会写进上面这一个；另外还有：" + "；".join(others),
            "确认你平时双击打开的就是被打补丁的那个（看 Cursor「关于」里的安装路径），否则在补丁面板用「设置路径」指到正确目录再打",
        )

    try:
        inspect_data = cam_patch.inspect(layout.app_root)
    except cam_patch.CamPatchError as exc:
        report.step("inspect", "读取当前补丁状态", "fail", str(exc), "请先安装 Node.js，并确保 cursor-account-manager-main/src 还在本工具目录里")
        return _finish(report)

    report.rulesBefore = rule_status(layout, inspect_data=inspect_data)
    before_summary = summarize_rules(report.rulesBefore)
    tested = cam_patch.is_tested_version(layout.version)
    if tested:
        report.step(
            "anchors",
            "版本检查",
            "ok",
            f"Cursor {layout.version} 在已测试列表（{cam_patch.required_version_label()}）",
        )
    else:
        report.step(
            "anchors",
            "版本检查",
            "warn",
            f"Cursor {layout.version} 不在已测试列表（{cam_patch.required_version_label()}）。插件仍会尝试，命中不全则拒绝写入。",
            VERSION_FIX,
        )

    try:
        dry = cam_patch.apply(layout.app_root, dry_run=True)
    except cam_patch.CamPatchError as exc:
        report.step("plan", "生成补丁计划", "fail", str(exc), VERSION_FIX)
        return _finish(report)
    except PermissionError as exc:
        report.step("plan", "生成补丁计划", "fail", f"没有读取权限：{exc}", "右键「以管理员身份运行」本工具后重试")
        return _finish(report)

    if dry.get("reason") == "already-patched" or not dry.get("changed"):
        if before_summary["verdict"] == "full":
            report.step("plan", "生成补丁计划", "ok", "所有规则已是最新，无需改文件")
            report.rulesAfter = report.rulesBefore
            _restart_and_check(layout, report)
            return _finish(report)
        report.step(
            "plan",
            "生成补丁计划",
            "fail",
            dry.get("reason") or "插件没有找到可改写的锚点",
            VERSION_FIX,
        )
        return _finish(report)

    files = dry.get("files") or []
    names = []
    for item in files:
        if isinstance(item, dict):
            names.append(str(item.get("rel") or ""))
        else:
            names.append(str(item))
    names = [n for n in names if n]
    report.step(
        "plan",
        "生成补丁计划",
        "ok",
        f"将按 cursor-account-manager 规则修改 {len(names)} 个文件"
        + ("：" + "、".join(names[:12]) + ("…" if len(names) > 12 else "") if names else ""),
    )

    was_running = running_cursor_paths()
    sp.close_cursor(layout)
    still = [p for p in running_cursor_paths() if _same_install(layout, p)]
    if still:
        report.step(
            "close",
            "关闭 Cursor",
            "fail",
            "Cursor 仍在运行，无法安全写入：" + "；".join(still),
            "手动完全退出 Cursor（包括右下角托盘图标）再重试；如果 Cursor 是以管理员身份运行的，本工具也要用管理员身份打开",
        )
        return _finish(report)
    report.step("close", "关闭 Cursor", "ok", "已关闭正在运行的 Cursor" if was_running else "Cursor 本来就没在运行")

    try:
        applied = cam_patch.apply(layout.app_root)
    except PermissionError as exc:
        report.step("write", "写入补丁文件", "fail", f"没有写入权限：{exc}", "右键「以管理员身份运行」本工具后重试（Program Files 目录需要管理员）")
        return _finish(report)
    except cam_patch.CamPatchError as exc:
        report.step("write", "写入补丁文件并校验", "fail", str(exc), VERSION_FIX)
        return _finish(report)
    except Exception as exc:  # pragma: no cover
        report.step("write", "写入补丁文件", "fail", f"{type(exc).__name__}: {exc}", "把这段文字发给群主")
        return _finish(report)

    backup = applied.get("backupDir") or ""
    report.backupDir = str(backup)
    written = applied.get("files") or names
    count = len(written) if isinstance(written, list) else len(names)
    report.step("write", "写入补丁文件", "ok", f"已写入 {count} 个文件" + (f"，备份在 {backup}" if backup else ""))
    after = applied.get("after") if isinstance(applied.get("after"), dict) else None
    if after and after.get("streamLifecycle"):
        report.step("verify", "写入后校验", "ok", "插件校验通过：Stream 回路与子代理生命周期均已命中")
    elif after and after.get("streamMode"):
        report.step("verify", "写入后校验", "warn", "核心 Stream 回路已命中，但子代理生命周期未齐", REPATCH_FIX)
    else:
        report.step("verify", "写入后校验", "warn", "插件已写入，但 Stream 生命周期尚未判定为完整", VERSION_FIX)
    sp._mac_seal(layout)

    report.rulesAfter = rule_status(layout, inspect_data=after)
    after_summary = summarize_rules(report.rulesAfter)
    report.step(
        "rules",
        "逐条规则确认",
        "ok" if after_summary["verdict"] == "full" else "warn",
        f"{after_summary['applied']}/{after_summary['required']} 条必需规则已生效"
        + ("；缺锚点：" + "、".join(after_summary["missing"]) if after_summary["missing"] else "")
        + ("；未打：" + "、".join(after_summary["pending"]) if after_summary["pending"] else ""),
        "" if after_summary["verdict"] == "full" else VERSION_FIX,
    )

    _restart_and_check(layout, report)
    return _finish(report)


def _restart_and_check(layout: sp.CursorLayout, report: Report) -> None:
    sp.close_cursor(layout)
    started = sp.start_cursor(layout)
    if not started:
        report.step("restart", "重启 Cursor", "warn", "没能自动拉起 Cursor", "手动双击打开被打补丁的那个 Cursor")
    else:
        ours, others = wait_for_cursor(layout, timeout=10.0)
        if ours:
            report.step("restart", "重启 Cursor", "ok", "Cursor 已从补丁目录启动：" + ours[0])
        elif others:
            report.step(
                "restart",
                "重启 Cursor",
                "warn",
                "起来的 Cursor 不在补丁目录：" + "；".join(others),
                "你运行的不是被打补丁的那个 Cursor。用补丁面板「设置路径」指到这个路径重新打，或改用被打补丁目录里的 Cursor",
            )
        else:
            report.step("restart", "重启 Cursor", "warn", "10 秒内没检测到 Cursor 进程", "手动打开 Cursor；若打开的是另一份安装，请用「设置路径」指定")
    acct = account_check()
    report.step(
        "account",
        "本机登录账号的 Sand 资格",
        acct.get("status", "skip"),
        acct.get("detail", ""),
        acct.get("fix", ""),
    )


def status_report(layout: sp.CursorLayout) -> dict:
    """「检测」用：逐条规则 + 结论 + 运行中的 Cursor 是否就是这一份。"""
    rules = rule_status(layout)
    summary = summarize_rules(rules)
    running = running_cursor_paths()
    ours = [p for p in running if _same_install(layout, p)]
    others = [p for p in running if not _same_install(layout, p)]
    return {
        "rules": rules,
        "summary": summary,
        "runningFromPatched": bool(ours),
        "runningElsewhere": others,
        "otherInstalls": other_installs(layout),
    }


# ---------------------------------------------------------------------------
# 验证生效：读 Cursor 自己的 agent-host 日志
# ---------------------------------------------------------------------------

_SELECTED_RE = re.compile(r"^(\S+ \S+) \[info\] Selected Agent Host turn runtime (\{.*\})\s*$")
_REASON_HINTS = {
    "action-not-supported": "旧版补丁（<1.2.1）：后台任务完成等动作回落云端被 401。重新打补丁。",
    "run-options-not-supported": "旧版补丁（<1.2.1）：子代理回落云端。重新打补丁；若已是 1.2.1，说明该 turn 带 customSystemPrompt / harness，官方本地回路不支持。",
    "managed-local-unavailable": "本地 runtime 没加载成：看上面「Loaded managed local-loop runtime」是否出现；没出现多为 675.js 未打上或加载失败。",
    "managed-local-http2-unavailable": "本地推理 HTTP/2 通道不可用：网络 / 代理拦了 api2.cursor.sh 的 HTTP/2。",
    "gate-off": "本地路由补丁没生效（managed-local 路由锚点未替换）。重新打补丁。",
    "gate-check-failed": "本地路由判定抛错。重新打补丁并看日志。",
    "privacy-mode-unavailable": "读不到隐私模式设置，Cursor 尚未完成登录 / 初始化。稍后再试。",
    "gate-reader-unavailable": "runtimeCapabilities 缺 checkFeatureGate，构建不符。",
    "mode-not-supported": "Ask / Plan 模式官方本地回路不支持。切到 Agent 才会走本地 Bot。不必因此再打补丁。",
    "model-not-supported": "该轮没有 modelId（模型未选）。",
    "private-model-not-supported": "使用了自带 API Key 的私有模型，官方本地回路不支持，走云端属正常。",
    "simulated-message-not-supported": "Cursor 内部模拟消息（续写 / 系统注入），官方本地回路不支持，走云端属正常。不必再打补丁。",
}

# 1.2.1 补丁仍会故意回落云端：不算「补丁没打上」。
_EXPECTED_CONNECT_REASONS = {
    "simulated-message-not-supported",
    "private-model-not-supported",
    "mode-not-supported",
}


def _cursor_log_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor" / "logs"
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Cursor" / "logs"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Cursor" / "logs"


def _agent_host_logs(limit_sessions: int = 2) -> List[Path]:
    root = _cursor_log_root()
    if not root.is_dir():
        return []
    sessions = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    logs: List[Path] = []
    for session in sessions[:limit_sessions]:
        for path in session.rglob("*.log"):
            if path.parent.name == "anysphere.cursor-agent-host" and path.name.startswith("Cursor Agent Host") and "Network" not in path.name:
                logs.append(path)
    return sorted(logs, key=lambda p: p.stat().st_mtime, reverse=True)


def runtime_report(max_turns: int = 6) -> dict:
    """看 Cursor 实际跑起来后：本地回路有没有加载、最近几轮走了哪条路、有没有 401。"""
    logs = _agent_host_logs()
    if not logs:
        return {
            "ok": False,
            "verdict": "no-log",
            "headline": "没找到 Cursor 的 agent-host 日志",
            "detail": f"日志目录：{_cursor_log_root()}。请先打开 Cursor 并在 Agent 里发一条消息，再点验证。",
            "turns": [],
            "errors": [],
        }
    log = logs[0]
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"ok": False, "verdict": "no-log", "headline": f"读日志失败：{exc}", "turns": [], "errors": []}
    lines = text.splitlines()
    activated = any("Activating agent host extension" in ln for ln in lines)
    loaded = any("Loaded managed local-loop runtime" in ln for ln in lines)
    backend_nal = any("using backend NAL" in ln for ln in lines)
    move_exec_on = any("move_exec ON" in ln for ln in lines)
    disabled = any("Agent host is disabled" in ln for ln in lines)
    turns: List[dict] = []
    for ln in lines:
        m = _SELECTED_RE.match(ln)
        if not m:
            continue
        try:
            data = json.loads(m.group(2))
        except Exception:
            continue
        reason = str(data.get("reason") or "")
        turns.append(
            {
                "time": m.group(1),
                "runtime": data.get("runtime"),
                "reason": reason,
                "action": data.get("actionCase"),
                "model": data.get("modelId"),
                "hint": "" if data.get("runtime") == "managed-local" else _REASON_HINTS.get(reason, ""),
            }
        )
    errors: List[str] = []
    for ln in lines:
        if "[error]" in ln or "unauthenticated" in ln or "usage limit" in ln.lower():
            head = ln[:220]
            # 堆栈行、Git API 噪音、Agent 工具参数写错（Invalid arguments）都不算补丁故障。
            if "at c:" in head or head.strip().startswith("at ") or "Failed to find a Git API" in head:
                continue
            low = head.lower()
            if "invalid arguments" in low or "withstreamingeditparsedargs" in low or "nal.tool_call.failure" in low:
                continue
            errors.append(head)
    recent_turns = turns[-max_turns:]
    recent_errors = errors[-5:]
    unauth = any("unauthenticated" in e or "Authentication error" in e for e in recent_errors)
    limit_hit = any("usage limit" in e.lower() for e in recent_errors)

    checks = [
        {"title": "agent-host 扩展已激活", "ok": activated, "detail": "" if activated else "日志里没有激活记录：Cursor 可能还没打开 Agent 窗口"},
        {"title": "本地回路 runtime 已加载", "ok": loaded and not backend_nal, "detail": "" if loaded and not backend_nal else ("回落到后端 NAL：local_runtime_load 补丁没生效或 675.js 加载失败" if backend_nal else "未加载")},
        {"title": "move_exec ON（host 自带工具执行器）", "ok": move_exec_on, "detail": "" if move_exec_on else "move_exec 未开：工具会等 cursor-agent-exec 注册，可能 30 秒超时"},
    ]
    if disabled:
        checks.append({"title": "agent host 被开关关闭", "ok": False, "detail": "cursorAgentHostEnabled gate off：agent_host_enablement 补丁没生效"})
    local_turns = [t for t in recent_turns if t["runtime"] == "managed-local"]
    connect_turns = [t for t in recent_turns if t["runtime"] != "managed-local"]
    expected_connect = [t for t in connect_turns if t["reason"] in _EXPECTED_CONNECT_REASONS]
    unexpected_connect = [t for t in connect_turns if t["reason"] not in _EXPECTED_CONNECT_REASONS]

    if not activated:
        verdict, headline = "unknown", "Cursor 还没启动 agent-host，无法判断。打开 Cursor 的 Agent 面板发一条消息后再验证"
    elif not (loaded and not backend_nal and move_exec_on):
        verdict, headline = "broken", "补丁没有真正生效：本地回路没加载 / move_exec 未开，见下方检查项"
    elif not recent_turns:
        verdict, headline = "ready", "本地回路已加载、move_exec 已开；还没有对话记录——发一条消息再验证可看到走的是哪条路"
    elif unexpected_connect and not local_turns:
        verdict, headline = "broken", "最近几轮全部回落云端（connect），Bot 额度没用上，原因见每轮的提示"
    elif unexpected_connect:
        verdict, headline = "partial", f"最近 {len(recent_turns)} 轮里 {len(unexpected_connect)} 轮回落云端，原因见提示"
    elif expected_connect and local_turns:
        verdict, headline = (
            "working",
            f"生效：最近 {len(recent_turns)} 轮里 {len(local_turns)} 轮走本地 Bot 回路；"
            f"{len(expected_connect)} 轮是官方本地回路不支持的动作，走云端属正常",
        )
    elif expected_connect:
        verdict, headline = "ready", "本地回路已加载；最近几轮都是官方不支持走本地的动作（见说明），发一条普通 Agent 消息再验证"
    else:
        verdict, headline = "working", f"生效：最近 {len(recent_turns)} 轮全部走本地 Bot 回路（managed-local / sand-client）"
    if unauth:
        headline += "；且出现登录票 401——本机登录号失效或无 Sand 资格，请在列表里验证该号"
    if limit_hit:
        headline += "；出现 Bot 额度用尽提示"
    return {
        "ok": verdict in ("working", "ready"),
        "verdict": verdict,
        "headline": headline,
        "log": str(log),
        "checks": checks,
        "turns": recent_turns,
        "errors": recent_errors,
    }
