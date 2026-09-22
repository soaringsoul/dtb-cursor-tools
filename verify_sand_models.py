#!/usr/bin/env python3
"""Sand 诊断脚本（不依赖 sand_patch 安装/卸载）。

可在**未打补丁**时检查：
  - 本机 Cursor 账号 / Sand 资格 / Bot 额度（api2 DashboardService）
  - cursor_sand_direct 补丁是否已安装（只读 status）
  - Agent Host 路由日志（managed-local vs connect）
  - Cursor 结构化日志 agent.turn.outcome（IDE 内实机结果）

可选：裸 HTTP/2 探测 AgentService/Run（Cursor IDE 新主链路；须回 requestContextResult）。

用法：
  python3 verify_sand_models.py                    # 全量诊断 + fable 门槛
  python3 verify_sand_models.py --account-only     # 只查账号/额度
  python3 verify_sand_models.py --patch-only       # 只查补丁状态
  python3 verify_sand_models.py --routes-only      # 只查 Agent Host 路由日志
  python3 verify_sand_models.py --from-logs        # 只查 agent.turn.outcome
  python3 verify_sand_models.py --stream-probe     # 打 AgentService/Run（HTTP/2 双工）
  python3 verify_sand_models.py --no-gate            # 跳过门槛判定，exit 0

退出码（默认带门槛判定）：
  0  gate 模型在日志里 outcome=success 且 stream_mode 已安装
  1  gate 有记录但失败
  2  stream_mode 未安装
  3  日志中尚无 gate 模型记录
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import struct
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests

ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = ROOT / "script"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPT_DIR))

import local_cursor  # noqa: E402
import resolve  # noqa: E402
from sand_api import fetch_access, fetch_usage, parse_token, _cookie  # noqa: E402

STREAM_URL = "https://api2.cursor.sh/aiserver.v1.InferenceService/Stream"
RUN_INFERENCE_URL = "https://api2.cursor.sh/aiserver.v1.InferenceService/RunInference"
RUN_INFERENCE_PROBE = ROOT / "run_inference_probe.js"
AGENT_RUN_PROBE = ROOT / "agent_run_probe.js"
TOOL_VERSION = "2.2.0"
GATE_MODEL = "claude-fable-5-1"
GATE_MODEL_ALIASES = (
    "claude-fable-5-1",
    "claude-fable-5-1-thinking-high",
    "fable-5.1",
    "fable5.1",
    "fable-5",
)
DEFAULT_MODELS = (
    "claude-fable-5-1-thinking-high",
    "composer-2.5",
    "gpt-5.5-high-fast",
)
PROMPT = "Reply with exactly: OK"
LOG_GLOB = os.path.expanduser("~/Library/Application Support/Cursor/logs/**/*.log")
HOST_ROUTE_RE = re.compile(
    r"Selected Agent Host turn runtime (\{.*\})"
)


@dataclass(frozen=True)
class PatchSnapshot:
    cursor_version: str
    install_root: str
    installed: bool
    stream_mode_installed: bool
    tool_exec_bridge_installed: bool
    direct_stream_markers: int
    error: str = ""


@dataclass
class ProbeResult:
    path: str
    client_type: str
    model_id: str
    max_mode: bool
    ok: bool
    ttfb_ms: Optional[int]
    text_preview: str
    error_code: str
    detail: str
    interpretation: str = ""


@dataclass
class LogOutcome:
    model: str
    outcome: str
    error_code: str
    error_text: str
    pre_network_ms: Optional[float]
    log_file: str
    ts: str = ""


@dataclass
class HostRoute:
    runtime: str
    reason: str
    model_id: str
    log_file: str
    ts: str = ""


@dataclass
class GateVerdict:
    passed: bool
    exit_code: int
    gate_model: str
    stream_mode: bool
    latest: Optional[LogOutcome]
    blockers: List[str]


def _product_meta() -> Tuple[str, str]:
    try:
        import cursor_sand_direct as sand_direct

        layout = sand_direct.resolve_cursor_layout(interactive=False)
        product = json.loads(layout.product_json.read_bytes().decode("utf-8-sig"))
        return (
            str(product.get("version") or layout.version),
            str(product.get("commit") or ""),
        )
    except Exception:
        return "3.21.12", ""


def load_patch_snapshot() -> PatchSnapshot:
    try:
        import cursor_sand_direct as sand_direct

        layout = sand_direct.resolve_cursor_layout(interactive=False)
        status = sand_direct.inspect_status(layout)
        return PatchSnapshot(
            cursor_version=layout.version,
            install_root=str(layout.install_root),
            installed=status.installed,
            stream_mode_installed=status.stream_mode_installed,
            tool_exec_bridge_installed=status.tool_exec_bridge_installed,
            direct_stream_markers=status.direct_stream_markers,
        )
    except Exception as exc:
        version, _ = _product_meta()
        return PatchSnapshot(
            cursor_version=version,
            install_root="",
            installed=False,
            stream_mode_installed=False,
            tool_exec_bridge_installed=False,
            direct_stream_markers=0,
            error=str(exc),
        )


def print_patch_status(snapshot: PatchSnapshot) -> None:
    print("=== 补丁状态（cursor_sand_direct · 只读）===")
    if snapshot.error:
        print(f"⚠ 读取失败: {snapshot.error}")
    print(f"Cursor: {snapshot.cursor_version} @ {snapshot.install_root or '?'}")
    print(f"installed: {snapshot.installed}")
    print(f"stream_mode: {snapshot.stream_mode_installed}")
    print(f"tool_exec_bridge: {snapshot.tool_exec_bridge_installed}")
    print(f"direct_stream_markers: {snapshot.direct_stream_markers}")
    if not snapshot.stream_mode_installed:
        print(
            "⚠ 未安装 Sand Stream 直连补丁时，Agent 易走 connect / managed-local-unavailable，"
            "Grok 等 Sand 模型可能失败或落到 IDE 计费通道。"
        )


def _connect_frame(body: bytes) -> bytes:
    return b"\x00" + struct.pack(">I", len(body)) + body


def _encode_stream_body(
    model_id: str,
    max_mode: bool,
    prompt: str = PROMPT,
    conversation_id: Optional[str] = None,
) -> bytes:
    cid = conversation_id or str(uuid.uuid4())
    node = r"""
process.env.SAND_RPC_TEST = '1';
require('./sand_rpc.js');
const t = global.__sandTest;
const model = process.argv[1];
const maxm = process.argv[2] === 'true';
const prompt = process.argv[3];
const cid = process.argv[4];
const msg = {
  runRequest: {
    conversationId: cid,
    requestedModel: {
      modelId: model,
      maxMode: maxm,
      parameters: [{ id: 'effort', value: 'high' }],
    },
    action: {
      case: 'userMessageAction',
      value: {
        userMessageAction: {
          userMessage: { text: prompt },
        },
      },
    },
  },
};
process.stdout.write(Buffer.from(t.enc(msg)).toString('base64'));
"""
    b64 = subprocess.check_output(
        [
            "node",
            "-e",
            node,
            model_id,
            "true" if max_mode else "false",
            prompt,
            cid,
        ],
        cwd=str(ROOT),
        text=True,
    ).strip()
    return base64.b64decode(b64)


def _parse_connect_trailer(raw: bytes) -> Tuple[str, str, str]:
    texts: List[str] = []
    connect_code = ""
    server_err = ""
    i = 0
    while i + 5 <= len(raw):
        flag = raw[i]
        ln = struct.unpack(">I", raw[i + 1 : i + 5])[0]
        i += 5
        chunk = raw[i : i + ln]
        i += ln
        if flag & 0x02:
            try:
                trailer = json.loads(chunk.decode("utf-8", "replace"))
                err = trailer.get("error") or {}
                connect_code = str(err.get("code") or "")
                details = err.get("details") or []
                if details and isinstance(details[0], dict):
                    dbg = details[0].get("debug") or {}
                    server_err = str(
                        dbg.get("error") or dbg.get("details") or err.get("message") or ""
                    )
            except Exception:
                server_err = chunk.decode("utf-8", "replace")[:300]
            continue
        if not chunk:
            continue
        try:
            node = r"""
process.env.SAND_RPC_TEST='1';require('./sand_rpc.js');
const t=global.__sandTest;const b=Buffer.from(process.argv[1],'base64');
process.stdout.write(JSON.stringify(t.dec(b)));
"""
            out = subprocess.check_output(
                ["node", "-e", node, base64.b64encode(chunk).decode()],
                cwd=str(ROOT),
                text=True,
            )
            obj = json.loads(out)
            if obj.get("text"):
                texts.append(obj["text"])
            if obj.get("err"):
                server_err = obj["err"]
        except Exception:
            ascii_part = re.sub(
                r"[^\x20-\x7E\n\r\t]+",
                " ",
                chunk.decode("latin-1", "replace"),
            )
            if ascii_part.strip():
                texts.append(ascii_part.strip()[:120])
    return "".join(texts)[:200], connect_code, str(server_err)[:400]


def _headers(jwt: str, client_type: str, version: str, commit: str) -> dict:
    h = {
        "authorization": f"Bearer {jwt}",
        "content-type": "application/connect+proto",
        "connect-protocol-version": "1",
        "x-cursor-client-type": client_type,
        "x-cursor-client-version": version,
        "x-cursor-streaming": "true",
        "user-agent": f"sand-verify/{TOOL_VERSION}",
    }
    if commit:
        h["x-cursor-client-commit"] = commit
    if client_type == "sand":
        h["x-sand-box-namespace"] = "prod"
    return h


def interpret_inference_probe(usage_ok: bool, result: ProbeResult) -> str:
    if result.ok:
        return "裸 AgentService/Run 有文本返回，账号与 HTTP/2 双工握手可用。"
    code = result.error_code or result.detail
    detail = result.detail or code or ""
    if "requestContext" in detail or code == "timeout":
        return (
            "双工流已建立但未完成：服务端会先发 execServerMessage.requestContextArgs，"
            "客户端必须回 execClientMessage.requestContextResult，否则只会心跳不出字。"
        )
    if "Sand traffic is not supported" in detail:
        return (
            "RunInference 拒绝 Sand 流量。高级模型应走 AgentService/Run + accessToken + ide，"
            "不要走 grokBotToken / InferenceService 旧口。"
        )
    if usage_ok and code in {"ERROR_NOT_LOGGED_IN", "unauthenticated"}:
        return (
            "GetSandUsageStatus 已通过，但该入口回 NOT_LOGGED_IN："
            "常见于外部脚本缺少 Cursor 扩展内握手/签名。"
        )
    if code == "ERROR_OUTDATED_CLIENT":
        return "客户端版本过旧或 proto 不匹配。"
    if code == "internal" and "parse binary" in detail:
        return "proto 解析失败；请确认 sand_rpc.js 与 Cursor 版本匹配。"
    if code.startswith("http_"):
        return f"HTTP 层异常：{code}"
    return detail or code or "未知错误"


def interpret_stream_probe(usage_ok: bool, result: ProbeResult) -> str:
    return interpret_inference_probe(usage_ok, result)


def probe_agent_run(
    jwt: str,
    model_id: str,
    client_type: str,
    version: str,
    commit: str,
    max_mode: bool = False,
    usage_ok: bool = False,
    timeout: float = 45.0,
) -> ProbeResult:
    if not AGENT_RUN_PROBE.is_file():
        result = ProbeResult(
            path="AgentService/Run",
            client_type=client_type,
            model_id=model_id,
            max_mode=max_mode,
            ok=False,
            ttfb_ms=None,
            text_preview="",
            error_code="missing_probe_script",
            detail=str(AGENT_RUN_PROBE),
        )
        result.interpretation = interpret_inference_probe(usage_ok, result)
        return result
    env = os.environ.copy()
    try:
        uid, _, _ = parse_token(jwt)
        if uid:
            env["SAND_PROBE_COOKIE"] = _cookie(uid, jwt)
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            [
                "node",
                str(AGENT_RUN_PROBE),
                jwt,
                model_id,
                client_type,
                version,
                commit,
                "true" if max_mode else "false",
                PROMPT,
                str(int(timeout * 1000)),
            ],
            cwd=str(ROOT),
            text=True,
            timeout=timeout + 5,
            env=env,
        )
        payload = json.loads(out)
    except subprocess.TimeoutExpired:
        result = ProbeResult(
            path="AgentService/Run",
            client_type=client_type,
            model_id=model_id,
            max_mode=max_mode,
            ok=False,
            ttfb_ms=None,
            text_preview="",
            error_code="timeout",
            detail=f"probe exceeded {timeout:.0f}s",
        )
        result.interpretation = interpret_inference_probe(usage_ok, result)
        return result
    except Exception as exc:
        result = ProbeResult(
            path="AgentService/Run",
            client_type=client_type,
            model_id=model_id,
            max_mode=max_mode,
            ok=False,
            ttfb_ms=None,
            text_preview="",
            error_code="probe_error",
            detail=str(exc),
        )
        result.interpretation = interpret_inference_probe(usage_ok, result)
        return result

    err_code = str(payload.get("error_code") or payload.get("detail") or "")
    result = ProbeResult(
        path=str(payload.get("path") or "AgentService/Run"),
        client_type=client_type,
        model_id=model_id,
        max_mode=max_mode,
        ok=bool(payload.get("ok")),
        ttfb_ms=payload.get("ttfb_ms"),
        text_preview=str(payload.get("text_preview") or ""),
        error_code=err_code,
        detail=str(payload.get("detail") or err_code),
    )
    result.interpretation = interpret_inference_probe(usage_ok, result)
    return result


def probe_run_inference(
    jwt: str,
    model_id: str,
    client_type: str,
    version: str,
    commit: str,
    max_mode: bool = False,
    usage_ok: bool = False,
    timeout: float = 45.0,
) -> ProbeResult:
    if not RUN_INFERENCE_PROBE.is_file():
        result = ProbeResult(
            path="InferenceService/RunInference",
            client_type=client_type,
            model_id=model_id,
            max_mode=max_mode,
            ok=False,
            ttfb_ms=None,
            text_preview="",
            error_code="missing_probe_script",
            detail=str(RUN_INFERENCE_PROBE),
        )
        result.interpretation = interpret_inference_probe(usage_ok, result)
        return result
    try:
        out = subprocess.check_output(
            [
                "node",
                str(RUN_INFERENCE_PROBE),
                jwt,
                model_id,
                client_type,
                version,
                commit,
                "true" if max_mode else "false",
                PROMPT,
                str(int(timeout * 1000)),
            ],
            cwd=str(ROOT),
            text=True,
            timeout=timeout + 5,
        )
        payload = json.loads(out)
    except subprocess.TimeoutExpired:
        result = ProbeResult(
            path="InferenceService/RunInference",
            client_type=client_type,
            model_id=model_id,
            max_mode=max_mode,
            ok=False,
            ttfb_ms=None,
            text_preview="",
            error_code="timeout",
            detail=f"probe exceeded {timeout:.0f}s",
        )
        result.interpretation = interpret_inference_probe(usage_ok, result)
        return result
    except Exception as exc:
        result = ProbeResult(
            path="InferenceService/RunInference",
            client_type=client_type,
            model_id=model_id,
            max_mode=max_mode,
            ok=False,
            ttfb_ms=None,
            text_preview="",
            error_code="probe_error",
            detail=str(exc),
        )
        result.interpretation = interpret_inference_probe(usage_ok, result)
        return result

    err_code = str(payload.get("error_code") or payload.get("detail") or "")
    result = ProbeResult(
        path=str(payload.get("path") or "InferenceService/RunInference"),
        client_type=client_type,
        model_id=model_id,
        max_mode=max_mode,
        ok=bool(payload.get("ok")),
        ttfb_ms=payload.get("ttfb_ms"),
        text_preview=str(payload.get("text_preview") or ""),
        error_code=err_code,
        detail=str(payload.get("detail") or err_code),
    )
    result.interpretation = interpret_inference_probe(usage_ok, result)
    return result


def probe_stream(
    jwt: str,
    model_id: str,
    client_type: str,
    version: str,
    commit: str,
    max_mode: bool = False,
    usage_ok: bool = False,
    timeout: float = 45.0,
) -> ProbeResult:
    body = _connect_frame(_encode_stream_body(model_id, max_mode))
    started = time.monotonic()
    try:
        resp = requests.post(
            STREAM_URL,
            headers=_headers(jwt, client_type, version, commit),
            data=body,
            timeout=timeout,
            stream=True,
        )
    except Exception as exc:
        result = ProbeResult(
            path="InferenceService/Stream",
            client_type=client_type,
            model_id=model_id,
            max_mode=max_mode,
            ok=False,
            ttfb_ms=None,
            text_preview="",
            error_code="network_error",
            detail=str(exc),
        )
        result.interpretation = interpret_stream_probe(usage_ok, result)
        return result

    raw = b""
    ttfb_ms: Optional[int] = None
    try:
        for part in resp.iter_content(chunk_size=4096):
            if not part:
                continue
            if ttfb_ms is None:
                ttfb_ms = int((time.monotonic() - started) * 1000)
            raw += part
            if len(raw) > 256_000:
                break
    except Exception as exc:
        result = ProbeResult(
            path="InferenceService/Stream",
            client_type=client_type,
            model_id=model_id,
            max_mode=max_mode,
            ok=False,
            ttfb_ms=ttfb_ms,
            text_preview="",
            error_code="read_error",
            detail=str(exc),
        )
        result.interpretation = interpret_stream_probe(usage_ok, result)
        return result

    text, connect_code, server_err = _parse_connect_trailer(raw)
    ok = bool(text.strip()) and not server_err
    err_code = server_err or connect_code or f"http_{resp.status_code}"
    result = ProbeResult(
        path="InferenceService/Stream",
        client_type=client_type,
        model_id=model_id,
        max_mode=max_mode,
        ok=ok,
        ttfb_ms=ttfb_ms,
        text_preview=text,
        error_code=err_code,
        detail=server_err or connect_code or "",
    )
    result.interpretation = interpret_stream_probe(usage_ok, result)
    return result


def _line_timestamp(line: str) -> str:
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)", line.strip())
    return m.group(1) if m else ""


def _normalize_gate_model(name: str) -> str:
    low = (name or "").lower().replace("_", "-")
    if "fable" in low and ("5-1" in low or "5.1" in low):
        return GATE_MODEL
    return name


def load_log_outcomes() -> List[LogOutcome]:
    rows: List[LogOutcome] = []
    for path in glob.glob(LOG_GLOB, recursive=True):
        if "Structured Logs" not in path:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "agent.turn.outcome" not in line:
                        continue
                    m = re.search(r"\{.*\}$", line.strip())
                    if not m:
                        continue
                    obj = json.loads(m.group())
                    md = obj.get("metadata") or {}
                    pn = md.get("pre_network_ms")
                    rows.append(
                        LogOutcome(
                            model=str(md.get("model_intent") or "?"),
                            outcome=str(md.get("outcome") or "?"),
                            error_code=str(md.get("error_code") or ""),
                            error_text=str(md.get("error_text") or "")[:200],
                            pre_network_ms=float(pn) if pn is not None else None,
                            log_file=path,
                            ts=_line_timestamp(line),
                        )
                    )
        except OSError:
            continue
    rows.sort(key=lambda r: (r.ts, r.log_file))
    return rows


def load_agent_host_routes(limit: int = 20) -> List[HostRoute]:
    rows: List[HostRoute] = []
    for path in glob.glob(LOG_GLOB, recursive=True):
        if "cursor-agent-host" not in path or "Agent Host" not in path:
            continue
        if "Network" in path:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "Selected Agent Host turn runtime" not in line:
                        continue
                    m = HOST_ROUTE_RE.search(line)
                    if not m:
                        continue
                    obj = json.loads(m.group(1))
                    rows.append(
                        HostRoute(
                            runtime=str(obj.get("runtime") or "?"),
                            reason=str(obj.get("reason") or ""),
                            model_id=str(obj.get("modelId") or "?"),
                            log_file=path,
                            ts=_line_timestamp(line),
                        )
                    )
        except (OSError, json.JSONDecodeError):
            continue
    rows.sort(key=lambda r: (r.ts, r.log_file))
    return rows[-limit:]


def print_agent_host_routes(rows: List[HostRoute]) -> None:
    print("\n=== Agent Host 路由（最近）===")
    if not rows:
        print("（无记录）请在 Cursor 里发一条 Agent 消息后重跑")
        return
    for row in rows:
        print(
            f"  {row.ts} runtime={row.runtime:14} reason={row.reason:28} model={row.model_id}"
        )
    summary = Counter((r.runtime, r.reason) for r in rows)
    print("汇总:", dict(summary))


def gate_outcomes(rows: List[LogOutcome], gate_model: str) -> List[LogOutcome]:
    gate = _normalize_gate_model(gate_model)
    return [r for r in rows if _normalize_gate_model(r.model) == gate]


def evaluate_gate(
    snapshot: PatchSnapshot,
    rows: List[LogOutcome],
    gate_model: str,
) -> GateVerdict:
    gate = _normalize_gate_model(gate_model)
    blockers: List[str] = []
    if not snapshot.stream_mode_installed:
        blockers.append(
            "stream_mode 未安装（请用 script/cursor_sand_direct.py install 后重启 Cursor）"
        )
    hits = gate_outcomes(rows, gate)
    if not hits:
        blockers.append(
            f"日志中尚无 {gate} 的 agent.turn.outcome（请在 Cursor 选该模型发一条消息）"
        )
        return GateVerdict(
            False,
            3 if snapshot.stream_mode_installed else 2,
            gate,
            snapshot.stream_mode_installed,
            None,
            blockers,
        )

    latest = hits[-1]
    if latest.outcome == "success":
        if not snapshot.stream_mode_installed:
            blockers.append("fable 曾成功但当前 stream_mode 未就绪，建议重新安装补丁")
            return GateVerdict(False, 2, gate, False, latest, blockers)
        return GateVerdict(True, 0, gate, True, latest, [])

    if latest.error_code == "upgrade":
        blockers.append(
            "服务端 upgrade（常见：未付账单）。需 stream_mode 走 Sand 路由，或结清 dashboard 账单。"
        )
    elif latest.pre_network_ms and latest.pre_network_ms > 10_000:
        blockers.append(
            f"pre_network_ms≈{latest.pre_network_ms:.0f}ms，易出现 Taking longer…；"
            "确认补丁已装且 Agent Host 为 managed-local。"
        )
    else:
        blockers.append(
            f"最近 outcome={latest.outcome} err={latest.error_code or '-'}: "
            f"{latest.error_text[:120]}"
        )
    exit_code = 2 if not snapshot.stream_mode_installed else 1
    return GateVerdict(False, exit_code, gate, snapshot.stream_mode_installed, latest, blockers)


def print_gate_verdict(verdict: GateVerdict) -> None:
    print("\n=== 最终判定（门槛模型）===")
    print(f"gate_model: {verdict.gate_model}")
    print(f"stream_mode: {verdict.stream_mode}")
    if verdict.latest:
        slow = (
            f" pre_network={verdict.latest.pre_network_ms:.0f}ms"
            if verdict.latest.pre_network_ms is not None
            else ""
        )
        print(
            f"最近记录: outcome={verdict.latest.outcome} err={verdict.latest.error_code or '-'}"
            f"{slow}"
        )
        if verdict.latest.error_text:
            print(f"  detail: {verdict.latest.error_text[:160]}")
    else:
        print("最近记录: （无）")
    if verdict.passed:
        print("结果: ✅ PASS — fable 5.1 在 IDE 内可用且补丁就绪")
    else:
        print(f"结果: ❌ FAIL（exit {verdict.exit_code}）")
        for i, item in enumerate(verdict.blockers, 1):
            print(f"  {i}. {item}")


def summarize_log_outcomes(rows: List[LogOutcome]) -> Dict[str, Counter]:
    by_model: Dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        key = f"{row.outcome}/{row.error_code or '-'}"
        by_model[row.model][key] += 1
    return by_model


def _load_jwt(token_file: Optional[str]) -> Tuple[str, dict]:
    if token_file:
        raw = Path(token_file).read_text(encoding="utf-8").strip()
        _uid, jwt, claims = parse_token(raw)
        return jwt, claims
    acc = local_cursor.read_local_account()
    if not acc or not acc.get("token"):
        raise SystemExit("未读到本机 Cursor token，请先登录或传 --token-file")
    _uid, jwt, claims = parse_token(acc["token"])
    return jwt, claims


def print_account_section(jwt: str, claims: dict) -> bool:
    acc = local_cursor.read_local_account() or {}
    usage_ok = False
    print("\n=== 账号 / Sand 资格（可不经补丁）===")
    print(f"email: {acc.get('email')}")
    print(f"membership(local): {acc.get('membership')}")
    print(f"token.type: {claims.get('type')}")
    try:
        usage = fetch_usage(jwt)
        usage_ok = bool(usage and usage.get("unlocked"))
        print(f"sand_usage: {usage}")
    except Exception as exc:
        print(f"sand_usage: error {exc}")
    try:
        uid, _, _ = parse_token(jwt)
        print(f"sand_access: {fetch_access(uid, jwt)}")
    except Exception as exc:
        print(f"sand_access: error {exc}")
    return usage_ok


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sand 诊断（账号 / 补丁状态 / 日志 / 可选裸 RunInference·Stream）"
    )
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--gate-model", default=GATE_MODEL)
    parser.add_argument("--token-file", default="")
    parser.add_argument("--max-mode", action="store_true")
    parser.add_argument("--from-logs", action="store_true")
    parser.add_argument("--routes-only", action="store_true")
    parser.add_argument("--account-only", action="store_true")
    parser.add_argument("--stream-probe", action="store_true")
    parser.add_argument("--patch-only", action="store_true")
    parser.add_argument("--no-gate", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    gate_model = _normalize_gate_model(args.gate_model)

    resolve.install()
    snapshot = load_patch_snapshot()
    print(f"sand-verify {TOOL_VERSION}")
    print_patch_status(snapshot)

    if args.patch_only:
        return 0 if snapshot.stream_mode_installed else 2

    exclusive = args.account_only or args.routes_only or args.from_logs or args.stream_probe
    show_account = args.account_only or not exclusive
    show_routes = args.routes_only or not exclusive
    show_logs = args.from_logs or not exclusive
    show_stream = args.stream_probe

    usage_ok = False
    jwt = ""
    claims: dict = {}
    if show_account or show_stream:
        jwt, claims = _load_jwt(args.token_file or None)
        if show_account:
            usage_ok = print_account_section(jwt, claims)
        elif show_stream:
            try:
                usage_ok = bool(fetch_usage(jwt))
            except Exception:
                usage_ok = False

    if args.account_only:
        return 0 if usage_ok else 1

    routes = load_agent_host_routes()
    if show_routes:
        print_agent_host_routes(routes)

    if args.routes_only:
        bad = [r for r in routes if r.runtime == "connect" and "unavailable" in r.reason]
        return 0 if not bad else 1

    rows: List[LogOutcome] = []
    if show_logs:
        rows = load_log_outcomes()
        print("\n=== Cursor 实机日志 agent.turn.outcome（权威）===")
        if not rows:
            print("（无记录）请在 Cursor 里分别用目标模型各发一条消息后重跑")
        else:
            for row in rows[-30:]:
                mark = "★" if _normalize_gate_model(row.model) == gate_model else " "
                slow = (
                    f" pre_network={row.pre_network_ms:.0f}ms"
                    if row.pre_network_ms is not None
                    else ""
                )
                print(
                    f"{mark} {row.model:20} outcome={row.outcome:7} err={row.error_code or '-':8}"
                    f"{slow} {row.error_text[:80]}"
                )
            print("\n汇总：")
            for model, counter in sorted(summarize_log_outcomes(rows).items()):
                star = "★" if _normalize_gate_model(model) == gate_model else " "
                print(f"  {star} {model:20} {dict(counter)}")
            gate_hits = gate_outcomes(rows, gate_model)
            if gate_hits:
                print(f"\n门槛模型 {gate_model} 共 {len(gate_hits)} 条；以最近一条为准。")

    if show_stream:
        version, commit = _product_meta()
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        print("\n=== AgentService/Run 裸探针（HTTP/2 双工 + accessToken + ide）===")
        print(
            "说明：服务端会先发 requestContextArgs；探针会回 requestContextResult。"
            "这是 Cursor IDE 新路由，不是 grokBotToken / InferenceService 旧口。"
        )
        for model in models:
            r = probe_agent_run(
                jwt,
                model,
                "ide",
                version,
                commit,
                max_mode=args.max_mode,
                usage_ok=usage_ok,
            )
            mark = "OK" if r.ok else "FAIL"
            print(
                f"[{mark}] client=ide  model={model:28} err={r.error_code!r} "
                f"ttfb={r.ttfb_ms}ms text={r.text_preview!r}"
            )
            print(f"       → {r.interpretation}")

    print("\n=== 判读提示 ===")
    print(
        f"1) IDE 内权威结果：{gate_model} 在日志 outcome=success 且 stream_mode=true。\n"
        "2) Agent Host 应为 connect / sand-agent-run（AgentService/Run）；managed-local-unavailable 仍表示补丁未装。\n"
        "3) 高级模型走 AgentService/Run（HTTP/2 双工）+ accessToken + ide；须回 requestContextResult。\n"
        "4) composer / grok 在 legacy 路由下也可能 success，不能代替 fable 门槛。\n"
        "5) connect + upgrade → 未付账单或走了 IDE 计费通道。"
    )

    run_gate = not args.no_gate and (show_logs or not exclusive)
    if not run_gate:
        return 0
    verdict = evaluate_gate(snapshot, rows, gate_model)
    print_gate_verdict(verdict)
    return verdict.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
