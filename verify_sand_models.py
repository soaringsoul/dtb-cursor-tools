#!/usr/bin/env python3
"""按 Sand 补丁原理验证模型可用性；**最终成功标志：claude-fable-5-1 在 Cursor 内正常跑通**。

补丁有两条历史路径（本仓库 1.1.9 为 B）：

  A) sand_rpc.js（旧）：AgentService/Run → 改写为 api2 InferenceService/Stream + x-cursor-client-type=sand
  B) sand_patch 1.1.x（当前）：强制 managed-local + agent-host + move_exec，在扩展内 runInference，
     Agent Run 仍走 api5，x-cursor-client-type 对 Agent 出 ide（HDRFIX）

因此：
  - 裸 HTTP 调 InferenceService/Stream 不能代表 Cursor 内真实链路（常返回 ERROR_OUTDATED_CLIENT）。
  - 权威验证应看 Cursor 结构化日志里的 agent.turn.outcome（本脚本 --from-logs）。
  - composer/grok 成功只说明链路部分可用；**只有 gate 模型 fable 5.1 outcome=success 才算通过**。

用法：
  python3 verify_sand_models.py              # 补丁 + 日志 + fable 门槛判定（exit 0=通过）
  python3 verify_sand_models.py --from-logs
  python3 verify_sand_models.py --patch-only
  python3 verify_sand_models.py --stream-probe --models claude-fable-5-1

退出码：
  0  gate 模型（默认 claude-fable-5-1）在日志中 outcome=success，且 stream_mode 已就绪
  1  gate 模型有记录但失败（upgrade / 超时等）
  2  stream_mode 未完整安装（先打补丁、重启 Cursor、再测 fable）
  3  日志中尚无 gate 模型测试记录（请在 Cursor 里选 fable 5.1 发一条消息后重跑）
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
sys.path.insert(0, str(ROOT))

import local_cursor  # noqa: E402
import resolve  # noqa: E402
import sand_patch  # noqa: E402
from sand_api import fetch_access, fetch_usage, parse_token  # noqa: E402

STREAM_URL = "https://api2.cursor.sh/aiserver.v1.InferenceService/Stream"
# 最终成功门槛：Sand 补丁 + 高级 Anthropic 模型 fable 5.1 在 IDE 内跑通
GATE_MODEL = "claude-fable-5-1"
GATE_MODEL_ALIASES = ("claude-fable-5-1", "fable-5.1", "fable5.1", "fable-5")
DEFAULT_MODELS = (
    GATE_MODEL,
    "grok-4.6",
    "composer-2.5",
    "gpt-5.6-sol",
    "default",
)
PROMPT = "Reply with exactly: pong"
LOG_GLOB = os.path.expanduser("~/Library/Application Support/Cursor/logs/**/*.log")


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
class GateVerdict:
    passed: bool
    exit_code: int
    gate_model: str
    stream_mode: bool
    latest: Optional[LogOutcome]
    blockers: List[str]


def _product_meta() -> Tuple[str, str]:
    try:
        layout = sand_patch.resolve_cursor_layout()
        product = json.loads(layout.product_json.read_bytes().decode("utf-8-sig"))
        return str(product.get("version") or layout.version), str(product.get("commit") or "")
    except Exception:
        return "3.18.9", ""


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
    """返回 (text_preview, connect_code, server_error/debug)。"""
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
                    server_err = str(dbg.get("error") or dbg.get("details") or err.get("message") or "")
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
        "user-agent": "sand-verify/1.1",
    }
    if commit:
        h["x-cursor-client-commit"] = commit
    if client_type == "sand":
        h["x-sand-box-namespace"] = "prod"
    return h


def probe_stream(
    jwt: str,
    model_id: str,
    client_type: str,
    version: str,
    commit: str,
    max_mode: bool = False,
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
        return ProbeResult(
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
        return ProbeResult(
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

    text, connect_code, server_err = _parse_connect_trailer(raw)
    ok = bool(text.strip()) and not server_err
    err_code = server_err or connect_code or f"http_{resp.status_code}"
    return ProbeResult(
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


def gate_outcomes(rows: List[LogOutcome], gate_model: str) -> List[LogOutcome]:
    gate = _normalize_gate_model(gate_model)
    return [r for r in rows if _normalize_gate_model(r.model) == gate]


def evaluate_gate(st: sand_patch.PatchStatus, rows: List[LogOutcome], gate_model: str) -> GateVerdict:
    gate = _normalize_gate_model(gate_model)
    blockers: List[str] = []
    if not st.stream_mode_installed:
        blockers.append(
            "stream_mode 未完整安装（managed-local / agent-host / move_exec 缺 marker）"
        )
    hits = gate_outcomes(rows, gate)
    if not hits:
        blockers.append(f"日志中尚无 {gate} 的 agent.turn.outcome（请在 Cursor 选该模型发一条消息）")
        return GateVerdict(False, 3 if st.stream_mode_installed else 2, gate, st.stream_mode_installed, None, blockers)

    latest = hits[-1]
    if latest.outcome == "success":
        if not st.stream_mode_installed:
            blockers.append("fable 曾成功但当前 stream_mode 未就绪，建议重新打补丁后复测")
            return GateVerdict(False, 2, gate, False, latest, blockers)
        return GateVerdict(True, 0, gate, True, latest, [])

    if latest.error_code == "upgrade":
        blockers.append(
            "服务端返回 upgrade（常见：未付账单）。补丁 UI 解锁不能绕过 api5 鉴权；"
            "需 stream_mode 就绪且 Sand 路由生效，或先结清 cursor.com/dashboard 账单。"
        )
    elif latest.pre_network_ms and latest.pre_network_ms > 10_000:
        blockers.append(
            f"pre_network_ms≈{latest.pre_network_ms:.0f}ms，易出现 Taking longer…；"
            "优先确认 agent_host gate enabled + stream_mode。"
        )
    else:
        blockers.append(
            f"最近 outcome={latest.outcome} err={latest.error_code or '-'}: {latest.error_text[:120]}"
        )
    exit_code = 2 if not st.stream_mode_installed else 1
    return GateVerdict(False, exit_code, gate, st.stream_mode_installed, latest, blockers)


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
        print("结果: ✅ PASS — fable 5.1 可用，Sand 补丁链路验证通过")
    else:
        print(f"结果: ❌ FAIL（exit {verdict.exit_code}）")
        for i, item in enumerate(verdict.blockers, 1):
            print(f"  {i}. {item}")
        print(
            "\n复测步骤：① Sand 工具打补丁并确认 stream_mode=true "
            "② 重启 Cursor ③ 模型选 claude-fable-5-1 发「回复 pong」"
            "④ 本脚本重跑"
        )


def summarize_log_outcomes(rows: List[LogOutcome]) -> Dict[str, Counter]:
    by_model: Dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        key = f"{row.outcome}/{row.error_code or '-'}"
        by_model[row.model][key] += 1
    return by_model


def print_patch_status() -> sand_patch.PatchStatus:
    layout = sand_patch.resolve_cursor_layout()
    st = sand_patch.inspect_status(layout)
    print("=== 补丁状态（sand_patch 原理 B）===")
    print(f"Cursor: {layout.version} @ {layout.install_root}")
    print(f"installed: {st.installed}")
    print(f"stream_mode: {st.stream_mode_installed}")
    print(
        "markers:",
        {
            "managed_local": st.managed_local_route_markers,
            "runtime_load": st.local_runtime_load_markers,
            "agent_host": st.agent_host_enablement_markers,
            "identity": st.agent_host_identity_markers,
            "move_exec": st.move_exec_markers,
            "model_unlock": "via workbench markers",
        },
    )
    if not st.stream_mode_installed:
        print(
            "⚠ 未完整 Stream 模式：高级模型在 UI 可选，但 agent-host/managed-local 未就绪时"
            "易出现长时间等待（connect 回退或等 agent-exec）。"
        )
    return st


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


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="按 Sand 补丁原理验证模型（门槛：fable 5.1）")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument(
        "--gate-model",
        default=GATE_MODEL,
        help=f"最终成功门槛模型（默认 {GATE_MODEL}）",
    )
    parser.add_argument("--token-file", default="")
    parser.add_argument("--max-mode", action="store_true")
    parser.add_argument("--from-logs", action="store_true", help="解析 Cursor agent.turn.outcome 日志")
    parser.add_argument(
        "--stream-probe",
        action="store_true",
        help="额外探测裸 InferenceService/Stream（旧 sand_rpc 路径，非 1.1.9 主链路）",
    )
    parser.add_argument("--patch-only", action="store_true", help="只检查补丁状态")
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="跳过 fable 门槛判定（仅输出诊断，exit 0）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    gate_model = _normalize_gate_model(args.gate_model)

    resolve.install()
    st = print_patch_status()
    if args.patch_only:
        return 0 if st.stream_mode_installed else 2

    jwt, claims = _load_jwt(args.token_file or None)
    acc = local_cursor.read_local_account() or {}
    print("\n=== 账号 / Sand 资格 ===")
    print(f"email: {acc.get('email')}")
    print(f"membership(local): {acc.get('membership')}")
    print(f"token.type: {claims.get('type')}")
    try:
        print(f"sand_usage: {fetch_usage(jwt)}")
    except Exception as exc:
        print(f"sand_usage: error {exc}")
    try:
        uid, _, _ = parse_token(jwt)
        print(f"sand_access: {fetch_access(uid, jwt)}")
    except Exception as exc:
        print(f"sand_access: error {exc}")

    do_logs = args.from_logs or not args.stream_probe
    rows: List[LogOutcome] = []
    if do_logs:
        rows = load_log_outcomes()
        print("\n=== Cursor 实机日志 agent.turn.outcome（权威）===")
        if not rows:
            print("（无记录）请在 Cursor 里分别用目标模型各发一条消息后重跑 --from-logs")
        else:
            for row in rows:
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

    if args.stream_probe:
        version, commit = _product_meta()
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        print("\n=== InferenceService/Stream 裸探针（sand_rpc 原理 A · 参考）===")
        print(
            "说明：1.1.9 主链路在 Cursor 扩展内 runInference；此处若见 ERROR_OUTDATED_CLIENT"
            "表示裸 HTTP 不能代替 IDE 内调用，不等于模型不可用。"
        )
        for model in models:
            for client in ("sand", "ide"):
                r = probe_stream(
                    jwt, model, client, version, commit, max_mode=args.max_mode
                )
                mark = "OK" if r.ok else "FAIL"
                print(
                    f"[{mark}] client={client:4} model={model:18} err={r.error_code!r} "
                    f"ttfb={r.ttfb_ms}ms text={r.text_preview!r}"
                )

    print("\n=== 判读提示 ===")
    print(
        f"1) **唯一通过标准**：{gate_model} 在日志里 outcome=success 且 stream_mode=true。\n"
        "2) composer / grok 成功只说明部分模型可用，不能代替 fable 门槛。\n"
        "3) error/upgrade → 服务端账单/资格拦截；未打补丁时常走 api5 正常计费路径。\n"
        "4) UI 能选但长时间 Taking longer → stream_mode + agent_host gate。\n"
        "5) 裸 Stream 探针 ERROR_OUTDATED_CLIENT 可忽略；以 agent.turn.outcome 为准。"
    )

    if args.no_gate:
        return 0
    verdict = evaluate_gate(st, rows, gate_model)
    print_gate_verdict(verdict)
    return verdict.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
