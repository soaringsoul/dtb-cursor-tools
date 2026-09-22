
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import textwrap
import time
import uuid
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    TextIO,
    Tuple,
    Union,
)


SUPPORTED_CURSOR_VERSION = "3.21.12"
SCRIPT_NAME = Path(__file__).name
CONFIG_VERSION = 1
DEFAULT_BACKUP_KEEP = 3

STREAM_MODE_DIRECT = "direct"
STREAM_MODES = (STREAM_MODE_DIRECT,)
STREAM_MODE_LABELS = {
    STREAM_MODE_DIRECT: "AgentService/Run 双工（ide + accessToken）",
}


def _stream_mode_label(mode: str) -> str:
    return STREAM_MODE_LABELS[mode]

SAND_CLIENT_MARKER = "/*SAND_CLIENT_MODE_V1*/"
SAND_CLIENT_EXISTING_MARKER = "/*SAND_CLIENT_EXISTING_V1*/"
SAND_CLIENT_GLASS_MARKER = "/*SAND_CLIENT_GLASS_V1*/"
SAND_ELIGIBILITY_MARKER = "/*SAND_ELIGIBILITY_MODE_V1*/"
SAND_MANAGED_LOCAL_ROUTE_MARKER = "/*SAND_MANAGED_LOCAL_ROUTE_V1*/"
SAND_DIRECT_STREAM_MARKER = "/*SAND_DIRECT_INFERENCE_STREAM_V1*/"
SAND_AGENT_HOST_ENABLEMENT_MARKER = "/*SAND_AGENT_HOST_ENABLEMENT_V1*/"
SAND_LOCAL_RUNTIME_LOAD_MARKER = "/*SAND_LOCAL_RUNTIME_LOAD_V1*/"
SAND_AGENT_HOST_IDENTITY_MARKER = "/*SAND_AGENT_HOST_IDENTITY_V1*/"
SAND_EXEC_BRIDGE_MARKER = "/*SAND_EXEC_BRIDGE_V1*/"
SAND_MOVE_EXEC_MARKER = "/*SAND_MOVE_EXEC_V1*/"
SAND_NATIVE_CLOUD_SUBAGENT_MARKER = "/*SAND_NATIVE_CLOUD_SUBAGENT_V1*/"
SAND_SUBAGENT_INTERACTIONS_MARKER = "/*SAND_SUBAGENT_INTERACTIONS_V1*/"
SAND_TASK_TOOL_MARKER = "/*SAND_TASK_TOOL_V1*/"
SAND_SUBAGENT_ROUTE_MARKER = "/*SAND_SUBAGENT_ROUTE_V1*/"
SAND_ACTION_ROUTE_MARKER = "/*SAND_ACTION_ROUTE_V1*/"
SAND_PRIVACY_GATE_MARKER = "/*SAND_PRIVACY_GATE_V1*/"
SAND_TOPOLOGY_MARKER = "/*SAND_TOPOLOGY_V1*/"

KNOWN_SAND_MARKERS = frozenset(
    (
        SAND_CLIENT_MARKER,
        SAND_CLIENT_EXISTING_MARKER,
        SAND_CLIENT_GLASS_MARKER,
        SAND_ELIGIBILITY_MARKER,
        SAND_MANAGED_LOCAL_ROUTE_MARKER,
        SAND_DIRECT_STREAM_MARKER,
        SAND_AGENT_HOST_ENABLEMENT_MARKER,
        SAND_LOCAL_RUNTIME_LOAD_MARKER,
        SAND_AGENT_HOST_IDENTITY_MARKER,
        SAND_EXEC_BRIDGE_MARKER,
        SAND_MOVE_EXEC_MARKER,
        SAND_NATIVE_CLOUD_SUBAGENT_MARKER,
        SAND_SUBAGENT_INTERACTIONS_MARKER,
        SAND_TASK_TOOL_MARKER,
        SAND_SUBAGENT_ROUTE_MARKER,
        SAND_ACTION_ROUTE_MARKER,
        SAND_PRIVACY_GATE_MARKER,
        SAND_TOPOLOGY_MARKER,
    )
)
ANY_SAND_MARKER_RE = re.compile(r"/\*[A-Z0-9_]*SAND_[A-Z0-9_]+\*/")

MARKER_OWNER_TABLE: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "SandClaimer",
        (
            "HDRFIX",
            "GLASSFIX",
            "MEMBERSHIP_SPOOF",
            "MODEL_UNLOCK",
            "MEM_PRO",
            "MAXMODE",
            "AGENT_IDE",
            "AGENTEXEC_KEEP",
            "STREAM_HOOK",
            "RPC_REWRITE",
            "STREAM_WRAP",
            "TRANSPORT_HOST",
        ),
    ),
    (
        "cursor-sand-toolkit",
        (
            "DSV3_DEGRADE",
            "TTFT",
            "MODE_RELAX",
            "MACHINE_ID",
            "MACHINE_MAC",
            "MACHINE_DEV",
            "PLAN_BUILD",
            "SUBAGENT_TURN",
            "SUBAGENT_FOLLOWUP",
            "CLIENT_SIDE_SUBAGENT",
            "INTERACTION_SEQ",
            "TASK_TOOL_V3",
            "AGENT_HOST_MOVE_EXEC",
            "SESSION_INFERENCE_STREAM",
            "MAX_TOKENS",
            "RULES_SKILLS",
            "MCP_FILESYSTEM",
            "USER_RULES",
        ),
    ),
)
UNKNOWN_MARKER_OWNER = "未知工具"


def _marker_owner(marker: str) -> str:
    name = "_" + marker.strip("/*") + "_"
    for owner, tokens in MARKER_OWNER_TABLE:
        if any(f"_{token}_" in name for token in tokens):
            return owner
    return UNKNOWN_MARKER_OWNER


def _assert_known_markers_complete() -> None:
    for name, value in list(globals().items()):
        if "_MARKER" in name and isinstance(value, str) and value.startswith("/*"):
            if value not in KNOWN_SAND_MARKERS:
                raise RuntimeError(f"{name} 未登记到 KNOWN_SAND_MARKERS")
    for marker in KNOWN_SAND_MARKERS:
        owner = _marker_owner(marker)
        if owner != UNKNOWN_MARKER_OWNER:
            raise RuntimeError(f"{marker} 被归属表误判为 {owner}")

BARE_HEADER_SITE_RE = re.compile(
    r"header\.set\(\s*[\"']x-cursor-client-type[\"']\s*,\s*"
    r"([\"'])(?:ide|sand)\1\s*\)"
)
CLIENT_MARKER_GUARD_PATTERN = (
    r"/\*[A-Z0-9_]*SAND_CLIENT(?:_(?:MODE|EXISTING|GLASS))?_V1\*/"
)

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[36m"

_COLOR_ENABLED = True
_PAUSE_ON_EXIT = False
_INTERACTIVE = False
_PROGRESS_ACTIVE = False
_LOG_PATH: Optional[Path] = None
_LOG_FP: Optional[TextIO] = None
_ORIG_STDOUT: Optional[TextIO] = None
_CANCEL_FILE: Optional[Path] = None


TARGET_SPECS: Tuple[Tuple[str, Optional[str]], ...] = (
    ("out/main.js", None),
    ("out/vs/workbench/api/worker/extensionHostWorkerMain.js", None),
    ("out/vs/workbench/api/node/extensionHostProcess.js", None),
    ("out/vs/workbench/workbench.glass.main.js", None),
    ("out/vs/workbench/workbench.desktop.main.js", None),
    ("extensions/cursor-always-local/dist/main.js", "cursor-always-local"),
    (
        "extensions/cursor-local-agent-runtime/dist/main.js",
        "cursor-local-agent-runtime",
    ),
    ("extensions/cursor-agent-host/dist/main.js", "cursor-agent-host"),
    ("extensions/cursor-agent-exec/dist/main.js", "cursor-agent-exec"),
)

EXT_HOST_REL = "out/vs/workbench/api/node/extensionHostProcess.js"


LAYOUT_KIND_DESKTOP = "desktop"
LAYOUT_KIND_SERVER = "server"
SERVER_MAIN_REL = "out/server-main.js"
SERVER_LAUNCHER_RELS: Tuple[str, ...] = ("bin/cursor-server", "bin/cursor-server.cmd")

SUBAGENT_MODEL_CATALOG_VERSION = 2
WORKBENCH_APPLICATION_USER_KEY = (
    "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl"
    ".persistentStorage.applicationUser"
)
NATIVE_ONLY_MODEL_IDS: Tuple[str, ...] = (
    "default",
    "auto-smart",
    "premium",
    "auto-low",
    "auto-medium",
    "auto-high",
)
NATIVE_ONLY_MODEL_PREFIXES: Tuple[str, ...] = (
    "composer",
    "grok",
    "gpt-5",
    "claude",
    "gemini",
    "fable",
)
BUILTIN_SUBAGENT_MODEL_SNAPSHOT_DATE = "2026-09-03（Cursor 3.18.25）"
BUILTIN_SUBAGENT_MODEL_CATALOG: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("default", "Auto", ("auto",)),
    ("grok-4.6", "Cursor Grok 4.6", ()),
    ("composer-2.5", "Composer 2.5", ("composer-latest", "composer", "composer-2-5")),
    ("claude-opus-5", "Claude Opus 5", ("opus-latest", "opus", "opus-5")),
    ("claude-opus-4-8", "Claude Opus 4.8", ("opus-latest", "opus", "opus-4.8", "opus-4-8")),
    ("gpt-5.6-sol", "GPT-5.6 Sol", ("gpt-latest", "gpt", "gpt-5-6-sol", "gpt-5.6")),
    ("gpt-5.5", "GPT-5.5", ("gpt-5-5",)),
    ("claude-fable-5-1", "Claude Fable 5.1", ("fable-5-1",)),
    ("claude-fable-5", "Claude Fable 5", ("fable", "fable-5")),
    ("grok-4.5", "Cursor Grok 4.5", ()),
    ("gemini-3.8-flash", "Gemini 3.8 Flash", ()),
    ("gemini-3.7-flash", "Gemini 3.7 Flash", ("gemini-flash-latest", "gemini-flash")),
    ("gpt-5.6-terra", "GPT-5.6 Terra", ("gpt-5-6-terra",)),
    ("claude-sonnet-5", "Claude Sonnet 5", ("sonnet-latest", "sonnet-5")),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6", ("sonnet-latest", "sonnet", "sonnet-4.6", "sonnet-4-6")),
    ("gpt-5.3-codex", "Codex 5.3", ("codex-latest", "codex", "codex-5.3")),
    ("claude-opus-4-7", "Claude Opus 4.7", ("opus-4.7", "opus-4-7")),
    ("gpt-5.4", "GPT-5.4", ("gpt",)),
    ("claude-opus-4-6", "Claude Opus 4.6", ("opus", "opus-4.6", "opus-4-6")),
    ("claude-opus-4-5", "Claude Opus 4.5", ("opus", "opus-4.5", "opus-4-5")),
    ("gpt-5.2", "GPT-5.2", ("gpt",)),
    ("gpt-5.6-luna", "GPT-5.6 Luna", ("gpt-5-6-luna",)),
    ("gemini-3.6-flash", "Gemini 3.6 Flash", ("gemini-flash-latest", "gemini-flash")),
    ("gemini-3.1-pro", "Gemini 3.1 Pro", ("gemini-latest", "gemini-pro-latest", "gemini", "gemini-pro")),
    ("gpt-5.4-mini", "GPT-5.4 Mini", ("gpt-mini-latest", "gpt-mini")),
    ("gpt-5.4-nano", "GPT-5.4 Nano", ("gpt-nano-latest", "gpt-nano")),
    ("claude-haiku-4-5", "Claude Haiku 4.5", ("haiku-latest", "haiku", "haiku-4.5", "haiku-4-5")),
    ("claude-sonnet-4-5", "Claude Sonnet 4.5", ("sonnet", "sonnet-4.5", "sonnet-4-5")),
    ("gpt-5.1", "GPT-5.1", ("gpt",)),
    ("gemini-3-flash", "Gemini 3 Flash", ()),
    ("gemini-3.5-flash", "Gemini 3.5 Flash", ("gemini-flash-latest", "gemini-flash")),
    ("claude-sonnet-4", "Claude Sonnet 4", ("sonnet", "sonnet-4")),
    ("gpt-5-mini", "GPT-5 Mini", ("gpt-mini",)),
    ("gemini-2.5-flash", "Gemini 2.5 Flash", ("gemini-flash",)),
    ("kimi-k3", "Kimi K3", ()),
    ("kimi-k2.7-code", "Kimi K2.7 Code", ("kimi-latest", "kimi")),
    ("glm-5.2", "GLM 5.2", ()),
)

MODEL_NUDGE_SILENT_SWITCH_PREFIXES: Tuple[str, ...] = (
    "function H$f(e){const{adminSettingsService:t",
    "function CCS(t){const{adminSettingsService:e",
)


def _silent_switch_patched(prefix: str) -> str:
    return prefix.replace(
        "{const{adminSettingsService:",
        "{return!1;" + SAND_ELIGIBILITY_MARKER + "const{adminSettingsService:",
    )


class SandToolError(RuntimeError):
    pass


class SandCancelled(SandToolError):
    pass


@dataclass(frozen=True)
class PlannedFile:
    original: bytes
    next_bytes: bytes
    mode: int


@dataclass(frozen=True)
class MarkerExpectations:

    task_tool: int
    agent_host_enablement: int
    topology: int
    nested_task_guard: int = 3
    glass_client_identity: int = 4
    model_nudge_silent_switch: int = 2


@dataclass(frozen=True)
class CursorLayout:
    install_root: Path
    app_root: Path
    product_json: Path
    executable: Path
    target_paths: Tuple[Path, ...]
    ext_host_path: Optional[Path]
    version: str
    kind: str = LAYOUT_KIND_DESKTOP

    @property
    def is_server(self) -> bool:
        return self.kind == LAYOUT_KIND_SERVER

    @property
    def expectations(self) -> MarkerExpectations:
        return SERVER_MARKER_EXPECTATIONS if self.is_server else DESKTOP_MARKER_EXPECTATIONS


@dataclass
class PatchStats:
    managed_local_route: int = 0
    action_route: int = 0
    privacy_gate: int = 0
    local_runtime_load: int = 0
    direct_stream: int = 0
    agent_host_enablement: int = 0
    agent_host_identity: int = 0
    exec_bridge: int = 0
    move_exec: int = 0
    native_cloud_subagent: int = 0
    subagent_interactions: int = 0
    task_tool: int = 0
    subagent_route: int = 0
    nested_task_guard: int = 0
    topology: int = 0

    def add(self, field: str, count: int) -> None:
        setattr(self, field, getattr(self, field) + count)

    def merge(self, other: "PatchStats") -> None:
        for field, count in vars(other).items():
            self.add(field, count)


@dataclass(frozen=True)
class PatchStatus:
    client_markers: int
    glass_client_markers: int
    eligibility_markers: int
    ide_matches: int
    glass_matches: int
    external_marker_count: int
    foreign_markers: Tuple[Tuple[str, int, Tuple[str, ...]], ...]
    bare_header_sites: int
    managed_local_route_markers: int
    action_route_markers: int
    privacy_gate_markers: int
    local_runtime_load_markers: int
    direct_stream_markers: int
    agent_host_enablement_markers: int
    agent_host_identity_markers: int
    exec_bridge_markers: int
    move_exec_markers: int
    native_cloud_subagent_markers: int
    subagent_interactions_markers: int
    task_tool_markers: int
    subagent_route_markers: int
    nested_task_guard_markers: int
    topology_markers: int
    expectations: MarkerExpectations

    @property
    def installed(self) -> bool:
        return (
            self.client_markers
            + self.glass_client_markers
            + self.eligibility_markers
            + self.managed_local_route_markers
            + self.action_route_markers
            + self.privacy_gate_markers
            + self.local_runtime_load_markers
            + self.direct_stream_markers
            + self.agent_host_enablement_markers
            + self.agent_host_identity_markers
            + self.exec_bridge_markers
            + self.move_exec_markers
            + self.native_cloud_subagent_markers
            + self.subagent_interactions_markers
            + self.task_tool_markers
            + self.subagent_route_markers
            + self.nested_task_guard_markers
            + self.topology_markers
            > 0
        )

    @property
    def stream_mode_installed(self) -> bool:
        return (
            self.managed_local_route_markers > 0
            and self.agent_host_identity_markers > 0
        )

    @property
    def tool_exec_bridge_installed(self) -> bool:
        return (
            self.move_exec_markers > 0
            and self.exec_bridge_markers > 0
        )


def _compile_client_rules() -> Tuple[re.Pattern[str], ...]:
    marker_guard = rf"(?!{CLIENT_MARKER_GUARD_PATTERN})"
    return (
        re.compile(
            rf"(isGlass\s*\?\s*[\"']glass[\"']\s*:\s*)([\"'])(ide|sand)\2{marker_guard}"
        ),
        re.compile(
            rf"(clientType:[A-Za-z_$][A-Za-z0-9_$]*\s*\?\s*[\"']glass[\"']\s*:\s*)"
            rf"([\"'])(ide|sand)\2{marker_guard}"
        ),
        re.compile(
            rf"(getDesktopBackendClientType\(\)\{{return[^}}]*?)"
            rf"([\"'])(ide|sand)\2{marker_guard}"
        ),
        re.compile(
            rf"([\"']x-cursor-client-type[\"']\s*:\s*)([\"'])(ide|sand)\2{marker_guard}"
        ),
        re.compile(
            rf"(header\.set\(\s*[\"']x-cursor-client-type[\"']\s*,\s*"
            rf"[A-Za-z_$][A-Za-z0-9_$.]*\s*(?:\?\?|\|\|)\s*)"
            rf"([\"'])(ide|sand)\2{marker_guard}"
        ),
        re.compile(
            rf"(header\.set\(\s*[\"']x-cursor-client-type[\"']\s*,\s*)"
            rf"([\"'])(ide|sand)\2{marker_guard}(?=\s*\))"
        ),
    )


CLIENT_RULES = _compile_client_rules()

GLASS_CLIENT_IDENTITY_RULES: Tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(clientType:[A-Za-z_$][A-Za-z0-9_$]*\.environment\.isGlass\s*\?\s*)"
        r"([\"'])glass\2(?=\s*:\s*[\"'](?:ide|sand)[\"'])"
    ),
    re.compile(
        r"(clientType:[A-Za-z_$][A-Za-z0-9_$]*\s*\?\s*)"
        r"([\"'])glass\2(?=\s*:\s*[\"'](?:ide|sand)[\"'])"
    ),
    re.compile(
        r"(getDesktopBackendClientType\(\)\{return "
        r"[A-Za-z_$][A-Za-z0-9_$.]*\.isGlass\s*\?\s*)"
        r"([\"'])glass\2(?=\s*:\s*[\"'](?:ide|sand)[\"'])"
    ),
)

MANAGED_LOCAL_ROUTE_ORIGINAL = (
    'if(!s)return{runtime:"connect",reason:"gate-off"};'
    'if("subscriptionNotificationAction"===t.action.action.case)'
    'return{runtime:"connect",reason:"subscription-notification-connect-routed"};'
    "const o=Ds(t),i=qs(o,e);"
    'return void 0!==i?Ms(i,o):{runtime:"managed-local",reason:"eligible"}'
)
MANAGED_LOCAL_ROUTE_PATCHED = (
    "const o=Ds(t),i=qs(o,e);"
    + SAND_MANAGED_LOCAL_ROUTE_MARKER
    + 'return{runtime:"connect",reason:void 0!==i?i:"sand-agent-run"}'
)
ACTION_ROUTE_ORIGINAL = (
    '"backgroundTaskCompletionAction"===e.actionCase?'
    "e.conversationMode!==P.xy.AGENT?"
    '"mode-not-supported":Fs(e):'
    '"userMessageAction"!==e.actionCase?"action-not-supported":'
    "function(e){if(e.requestedMode===P.xy.AGENT)return!0;"
    "return e.isHostedSubagentChild&&e.requestedMode===P.xy.UNSPECIFIED}(e)?"
    'e.simulatedUserMessage?"simulated-message-not-supported":Fs(e):'
    '"mode-not-supported"'
)


def _native_only_model_regex_source() -> str:
    alternatives = [
        re.escape(model_id) + "$" for model_id in NATIVE_ONLY_MODEL_IDS
    ] + [re.escape(prefix) for prefix in NATIVE_ONLY_MODEL_PREFIXES]
    return "^(?:cursor-)?(?:" + "|".join(alternatives) + ")"


NATIVE_ONLY_MODEL_REGEX_SOURCE = _native_only_model_regex_source()
NATIVE_ONLY_MODEL_ROUTE_REASON = "sand-native-model"
ACTION_ROUTE_PATCHED = (
    " void 0!==e.modelId&&/"
    + NATIVE_ONLY_MODEL_REGEX_SOURCE
    + '/.test(e.modelId)?"'
    + NATIVE_ONLY_MODEL_ROUTE_REASON
    + '":'
    '"userMessageAction"!==e.actionCase?void 0:'
    'e.requestedMode===P.xy.PROJECT?"mode-not-supported":'
    'e.simulatedUserMessage?"simulated-message-not-supported":Fs(e)'
    + SAND_ACTION_ROUTE_MARKER
)
PRIVACY_GATE_ORIGINAL = (
    "Ns(t.isManagedInferenceHttp2Available)?"
    + ACTION_ROUTE_ORIGINAL
    + ':"managed-local-http2-unavailable"'
)
PRIVACY_GATE_PATCHED = SAND_PRIVACY_GATE_MARKER + ACTION_ROUTE_ORIGINAL
LOCAL_RUNTIME_LOAD_ORIGINAL = (
    "let t=!1;try{t=await a.cursor.checkFeatureGate(pYe)}"
)
LOCAL_RUNTIME_LOAD_PATCHED = (
    "let t=!0;"
    + SAND_LOCAL_RUNTIME_LOAD_MARKER
    + "try{t=!0}"
)
AGENT_HOST_IDENTITY_ORIGINAL = 'clientIdentity:{clientType:"ide"}'
AGENT_HOST_IDENTITY_PATCHED = (
    'clientIdentity:{clientType:"ide"'
    + SAND_AGENT_HOST_IDENTITY_MARKER
    + "}"
)
MANAGED_TASK_FLAGS_ORIGINAL = (
    "const TRe={disableBackgroundTaskFollowUp:!1,enableAwaitForSubagents:!0,"
    "enableBackgroundTaskProgress:!0,enableBoundedSubagentAwait:!0,"
    "enableDebugSubagent:!0,enableEmptyResponseRetry:!0,"
    "enableExploreSubagent:!0,enableGrepBroadGlobGuard:!0,"
    "enableReadToolNegativeOffset:!0,enableSandboxSharedBuildCache:!0,"
    "longRunningJobs:!0,nalLoopDetection:!0,outputNotificationLimit:1e3,"
    "useClientSideSubagent:!0};"
)
MANAGED_TASK_FLAGS_PATCHED = (
    "const TRe={disableBackgroundTaskFollowUp:!1,enableAwaitForSubagents:!0,"
    "enableBackgroundTaskProgress:!0,enableBoundedSubagentAwait:!0,"
    "enableDebugSubagent:!0,enableEmptyResponseRetry:!0,"
    "enableExploreSubagent:!0,enableGrepBroadGlobGuard:!0,"
    "enableReadToolNegativeOffset:!0,enableSandboxSharedBuildCache:!0,"
    "longRunningJobs:!0,nalLoopDetection:!0,outputNotificationLimit:1e3,"
    "useClientSideSubagent:!0,enableCloudAsyncSubagents:!0,"
    "environmentParamForSubagent:!0,cloudSubagentTargeting:!0,"
    "enableMCPFileSystem:!0,enableShellSubagent:!0,enableBrowserSubagent:!0,"
    "enableGrindSwarmSubagent:!0,enableMultitaskMode:!0,enableGoalTools:!0,"
    "enableHookAdditionalContext:!0,enableAgentStoreConflictNotices:!0,"
    "enableSecurityReviewSubagent:!0,enableBugbotSubagent:!0,"
    "enableCiInvestigatorSubagent:!0,stripCredentialedUrls:!0,"
    "localAgentMarkdownImages:!0"
    + SAND_TASK_TOOL_MARKER
    + "};"
)
MANAGED_TASK_RUNTIME_ORIGINAL = (
    "})};function HRe(e){const{parentModelId:t,modelInfo:r}=e;return{"
)
SUBAGENT_MODEL_CATALOG_FILENAME = "sand-subagent-models.json"
SAND_SUBAGENT_MODEL_CATALOG_HELPER = (
    '_sand_norm=_sand_v=>String(_sand_v??"").toLowerCase()'
    '.replace(/[^a-z0-9]+/g,""),'
    "_sand_tail=/(nothinking|thinking|extrahigh|xhigh|high|medium|minimal|low|none"
    "|max|fast|300k|272k|200k|1m)$/,"
    "_sand_pick=(_sand_map,_sand_id)=>void 0===_sand_id?void 0:"
    "Map.prototype.get.call(_sand_map,_sand_id),"
    "_sand_tier=(_sand_map,_sand_hit,_sand_words)=>{"
    "if(void 0===_sand_hit||0===_sand_words.length)return _sand_hit;"
    "const _sand_meta=_sand_map._sand_meta?.get(_sand_hit.slug);"
    "if(void 0===_sand_meta)return _sand_hit;"
    "const _sand_p=new Map((Array.isArray(_sand_hit.parameters)?_sand_hit.parameters:"
    "_sand_map._sand_parent_max?_sand_meta.max:_sand_meta.nonMax)"
    ".map(_sand_x=>[_sand_x.id,_sand_x.value]));"
    "let _sand_used=!1,_sand_neg=!1,_sand_extra=!1,_sand_pend;"
    "for(let _sand_w of _sand_words){"
    'if("extra"===_sand_w){_sand_extra=!0;continue}'
    'if("extrahigh"===_sand_w||_sand_extra&&"high"===_sand_w)_sand_w="xhigh";'
    "_sand_extra=!1;"
    'if("no"===_sand_w){_sand_neg=!0;continue}'
    'if("nothinking"===_sand_w){_sand_w="thinking";_sand_neg=!0}'
    "if(void 0!==_sand_pend){const _sand_id=_sand_pend;_sand_pend=void 0;"
    "if(_sand_meta.params.get(_sand_id).includes(_sand_w)){"
    "_sand_p.set(_sand_id,_sand_w);_sand_used=!0;_sand_neg=!1;continue}}"
    "const _sand_vals=_sand_meta.params.get(_sand_w);"
    "if(void 0!==_sand_vals){"
    'if(_sand_vals.includes("true")){_sand_p.set(_sand_w,_sand_neg?"false":"true");_sand_used=!0}'
    "_sand_pend=_sand_w;_sand_neg=!1;continue}"
    "for(const[_sand_id,_sand_v]of _sand_meta.params)"
    "if(_sand_v.includes(_sand_w)){_sand_p.set(_sand_id,_sand_w);_sand_used=!0;break}"
    "_sand_neg=!1}"
    "return _sand_used?{slug:_sand_hit.slug,parameters:[..._sand_p]"
    ".map(([_sand_id,_sand_v])=>({id:_sand_id,value:_sand_v}))}:_sand_hit},"
    "_sand_variant=(_sand_map,_sand_n)=>{"
    "const _sand_list=_sand_map._sand_variants?.get(_sand_n);"
    "if(void 0===_sand_list)return;"
    "const _sand_v=_sand_list.find(_sand_x=>_sand_x.maxMode===_sand_map._sand_parent_max)"
    "??_sand_list[0];"
    "return 0===_sand_v.parameters.length?{slug:_sand_v.slug}:"
    "{slug:_sand_v.slug,parameters:_sand_v.parameters}},"
    "_sand_vocab=(_sand_map,_sand_hit,_sand_w)=>{"
    'if("no"===_sand_w||"nothinking"===_sand_w||"extra"===_sand_w||"extrahigh"===_sand_w)return!0;'
    "const _sand_meta=_sand_map._sand_meta?.get(_sand_hit.slug);"
    "if(void 0===_sand_meta)return _sand_tail.test(_sand_w);"
    "if(_sand_meta.params.has(_sand_w))return!0;"
    "for(const _sand_v of _sand_meta.params.values())if(_sand_v.includes(_sand_w))return!0;"
    "return!1},"
    "_sand_alias_lookup=(_sand_map,_sand_key)=>{"
    "const _sand_al=_sand_map._sand_aliases;"
    "if(void 0===_sand_al)return;"
    'const _sand_raw=String(_sand_key??"").toLowerCase().trim(),_sand_n=_sand_norm(_sand_raw),'
    "_sand_slugged=/^[a-z0-9.-]+$/.test(_sand_raw);"
    "if(0===_sand_n.length)return;"
    "const _sand_exact=_sand_slugged?_sand_variant(_sand_map,_sand_n):void 0;"
    "if(void 0!==_sand_exact)return _sand_exact;"
    "const _sand_words=_sand_raw.split(/[^a-z0-9]+/).filter(_sand_w=>_sand_w.length>0);"
    "let _sand_best;"
    "for(let _sand_i=_sand_words.length;_sand_i>0;_sand_i--){"
    'const _sand_k=_sand_words.slice(0,_sand_i).join(""),_sand_id=_sand_al.get(_sand_k);'
    "if(void 0===_sand_id)continue;"
    "if(void 0!==_sand_best&&_sand_id!==_sand_best.id)break;"
    "const _sand_isv=!0===_sand_map._sand_variants?.has(_sand_k);"
    "if(_sand_isv&&!_sand_slugged)continue;"
    "const _sand_seed=_sand_isv?_sand_variant(_sand_map,_sand_k):_sand_pick(_sand_map,_sand_id);"
    "if(void 0===_sand_seed)continue;"
    "const _sand_rest=_sand_words.slice(_sand_i);"
    "if(_sand_rest.every(_sand_w=>_sand_vocab(_sand_map,_sand_seed,_sand_w)))"
    "_sand_best={id:_sand_id,seed:_sand_seed,rest:_sand_rest}}"
    "if(void 0!==_sand_best)return _sand_tier(_sand_map,_sand_best.seed,_sand_best.rest);"
    "let _sand_r=_sand_n;const _sand_tk=[];"
    "while(_sand_r.length>0){"
    "const _sand_hit=_sand_pick(_sand_map,_sand_al.get(_sand_r));"
    "if(void 0!==_sand_hit)return _sand_tier(_sand_map,_sand_hit,_sand_tk);"
    "const _sand_m=_sand_tail.exec(_sand_r);"
    "if(null===_sand_m)break;"
    "_sand_tk.unshift(_sand_m[1]);_sand_r=_sand_r.slice(0,-_sand_m[1].length)}"
    "if(_sand_r.length<3)return;"
    "const _sand_keys=[..._sand_al.keys()],"
    "_sand_prefix=_sand_keys.filter(_sand_q=>_sand_r.startsWith(_sand_q))"
    ".sort((_sand_a,_sand_b)=>_sand_b.length-_sand_a.length);"
    "if(_sand_prefix.length>0)return _sand_tier(_sand_map,"
    "_sand_pick(_sand_map,_sand_al.get(_sand_prefix[0])),_sand_tk);"
    "const _sand_outer=new Set(_sand_keys.filter(_sand_q=>_sand_q.includes(_sand_r))"
    ".map(_sand_q=>_sand_al.get(_sand_q)));"
    "return 1===_sand_outer.size?_sand_tier(_sand_map,"
    "_sand_pick(_sand_map,[..._sand_outer][0]),_sand_tk):void 0},"
    "_sand_subagent_models=(_sand_parent,_sand_parent_max)=>{"
    "const _sand_map=new(class extends Map{get(_sand_key){"
    "return super.get(_sand_key)??_sand_alias_lookup(this,_sand_key)}});"
    "_sand_map._sand_parent_max=!0===_sand_parent_max;"
    "_sand_map.set(_sand_parent,{slug:_sand_parent});"
    "try{"
    'const _sand_fs=require("fs"),_sand_path=require("path"),'
    "_sand_doc=JSON.parse(_sand_fs.readFileSync("
    '_sand_path.join(__dirname,"' + SUBAGENT_MODEL_CATALOG_FILENAME + '"),"utf8")),'
    "_sand_list=Array.isArray(_sand_doc?.models)?_sand_doc.models:[],"
    "_sand_al=new Map,_sand_meta=new Map,_sand_vars=new Map,_sand_lines=[];"
    "for(const _sand_x of _sand_list){"
    'const _sand_id="string"==typeof _sand_x?.id?_sand_x.id.trim():"";'
    "if(0===_sand_id.length)continue;"
    "_sand_map.has(_sand_id)||_sand_map.set(_sand_id,{slug:_sand_id});"
    "for(const _sand_a of[_sand_id,_sand_x.displayName,"
    "...(Array.isArray(_sand_x.aliases)?_sand_x.aliases:[])]){"
    "const _sand_n=_sand_norm(_sand_a);"
    "_sand_n.length>0&&!_sand_al.has(_sand_n)&&_sand_al.set(_sand_n,_sand_id)}"
    "if(!Array.isArray(_sand_x.parameters))continue;"
    "const _sand_params=new Map;"
    'for(const _sand_d of _sand_x.parameters)"string"==typeof _sand_d?.id&&'
    "Array.isArray(_sand_d.values)&&_sand_d.values.length>0&&"
    "_sand_params.set(_sand_d.id,_sand_d.values.map(String));"
    "if(0===_sand_params.size)continue;"
    "const _sand_nm=Array.isArray(_sand_x.defaults?.nonMax)?_sand_x.defaults.nonMax:[],"
    "_sand_mx=Array.isArray(_sand_x.defaults?.max)?_sand_x.defaults.max:_sand_nm;"
    "_sand_meta.set(_sand_id,{params:_sand_params,nonMax:_sand_nm,max:_sand_mx});"
    "for(const _sand_v of Array.isArray(_sand_x.variants)?_sand_x.variants:[]){"
    "const _sand_s=_sand_norm(_sand_v?.slug);"
    "if(0===_sand_s.length||!Array.isArray(_sand_v.parameters))continue;"
    "const _sand_e={slug:_sand_id,maxMode:!0===_sand_v.maxMode,parameters:_sand_v.parameters};"
    "_sand_vars.has(_sand_s)?_sand_vars.get(_sand_s).push(_sand_e):_sand_vars.set(_sand_s,[_sand_e])}"
    '_sand_lines.push("- "+_sand_id+": "+[..._sand_params]'
    '.map(([_sand_k,_sand_v])=>_sand_k+" ("+_sand_v.join("|")+")").join(", "))}'
    "_sand_map._sand_aliases=_sand_al;_sand_map._sand_meta=_sand_meta;"
    "_sand_map._sand_variants=_sand_vars;"
    "_sand_lines.length>0&&(_sand_map._sand_tier_guidance="
    '"\\n\\nModel parameters (reasoning tier, context, thinking, fast): append them '
    "to the slug, either legacy-slug style (`claude-opus-5-thinking-xhigh`, "
    "`gpt-5.6-sol-high`, `kimi-k3-max`) or bracket style "
    "(`claude-opus-5[effort=high,context=1m]`). Parameters you omit take the "
    "model's default tier; a bare slug inherits the parent's parameters when the "
    "model is the same. Supported parameters and values per model:\\n\""
    '+_sand_lines.join("\\n"))'
    "}catch{}"
    "return _sand_map},"
    "_sand_subagent_overrides=_sand_list=>{"
    'const _sand_o={explore:{type:"inherit"}};'
    "for(const _sand_x of Array.isArray(_sand_list)?_sand_list:[]){"
    'const _sand_t=String(_sand_x?.subagentType??_sand_x?.subagent_type??"").trim(),'
    "_sand_c=_sand_x?.selection?.case??(!0===_sand_x?.inherit?\"inherit\":"
    '!0===_sand_x?.disabled?"disabled":void 0!==_sand_x?.model?"model":void 0),'
    "_sand_v=_sand_x?.selection?.value??_sand_x?.model;"
    "if(0===_sand_t.length)continue;"
    'if("inherit"===_sand_c)_sand_o[_sand_t]={type:"inherit"};'
    'else if("disabled"===_sand_c)_sand_o[_sand_t]={type:"disabled"};'
    'else if("model"===_sand_c&&"string"==typeof _sand_v?.modelId&&_sand_v.modelId.length>0)'
    '_sand_o[_sand_t]={type:"model",modelId:_sand_v.modelId}}'
    "return _sand_o}"
)
MANAGED_TASK_RUNTIME_PATCHED = (
    "})};const "
    + SAND_SUBAGENT_MODEL_CATALOG_HELPER
    + ";"
    + SAND_TASK_TOOL_MARKER
    + "function HRe(e){const{parentModelId:t,modelInfo:r}=e;return{"
    "parentModelParameters:e.parentModelParameters,parentMaxMode:e.parentMaxMode,"
)
TASK_PARENT_PARAMETERS_ORIGINAL = (
    "taskToolProps:HRe({parentModelId:null!=d?d:r.modelName,modelInfo:r})"
)
TASK_PARENT_PARAMETERS_PATCHED = (
    "taskToolProps:HRe({parentModelId:null!=d?d:r.modelName,modelInfo:r,"
    "parentModelParameters:(e.requestedModel?.parameters??[])"
    ".map(_sand_q=>({id:String(_sand_q.id),value:String(_sand_q.value)})),"
    "parentMaxMode:h,subagentModelOverrides:e.runOptions?.subagentModelOverrides})"
    + SAND_TASK_TOOL_MARKER
)
MANAGED_TASK_PROPS_ORIGINAL = (
    "subagentModels:{modelsBySlug:new Map([[t,{slug:t}]])},"
    "subagentModelOverrides:{},"
)
MANAGED_TASK_PROPS_PATCHED = (
    "subagentModels:{modelsBySlug:_sand_subagent_models(t,!0===e.parentMaxMode)},"
    "subagentModelOverrides:_sand_subagent_overrides(e.subagentModelOverrides),"
    + SAND_TASK_TOOL_MARKER
)
TASK_TIER_PARAMETERS_ORIGINAL = (
    "resolvedModelParameters:x===(a.parentRequestedModelName??i.modelName)&&"
    "(a.parentModelParameters?.length??0)>0?a.parentModelParameters:void 0,"
)
TASK_TIER_PARAMETERS_PATCHED = (
    "resolvedModelParameters:(_sand_t=>void 0!==_sand_t?.parameters&&"
    "_sand_t.parameters.length>0&&_sand_t.slug===x?"
    "_sand_t.parameters.map(_sand_p=>({id:_sand_p.id,value:_sand_p.value})):"
    "x===(a.parentRequestedModelName??i.modelName)&&"
    "(a.parentModelParameters?.length??0)>0?a.parentModelParameters:void 0)"
    '("string"==typeof n.model?a.subagentModels?.modelsBySlug?.get(n.model):void 0),'
    + SAND_TASK_TOOL_MARKER
)
TASK_ARGS_MODEL_TIER_ORIGINAL = (
    "subagentType:B.subagent_type,model:M,resume:f.resume,"
)
TASK_ARGS_MODEL_TIER_PATCHED = (
    "subagentType:B.subagent_type,"
    'model:(O?.length??0)>0?M+"["+O.map(_sand_q=>_sand_q.id+"="+_sand_q.value)'
    '.join(",")+"]":M,'
    "resume:f.resume,"
    + SAND_TASK_TOOL_MARKER
)
TASK_TIER_GUIDANCE_ORIGINAL = (
    'when they requested it.`}(p,w):"";return d&&_.push(y?"Available model slugs '
    'for subagents are listed in <available_subagent_models> in the initial '
    'user-info message at the start of this conversation.":S)'
)
TASK_TIER_GUIDANCE_PATCHED = (
    'when they requested it.`}(p,w)+(p.modelsBySlug._sand_tier_guidance??""):"";'
    + SAND_TASK_TOOL_MARKER
    + 'return d&&_.push(y?"Available model slugs '
    'for subagents are listed in <available_subagent_models> in the initial '
    'user-info message at the start of this conversation.":S)'
)
SUBAGENT_TASK_FILTER_TAIL_ORIGINAL = (
    'return(e.toolsOverride?e.toolsOverride(o,n,r):o).filter(e=>'
    '"ASK_QUESTION"!==e.toolIdentifier&&'
    '!("PLATFORM_ACTION"===e.toolIdentifier&&mX(e.name)))'
)
SUBAGENT_TASK_FILTER_ORIGINAL = (
    'const o=e.preserveTaskTool?t:t.filter(e=>"TASK"!==e.toolIdentifier);'
    + SUBAGENT_TASK_FILTER_TAIL_ORIGINAL
)
SUBAGENT_TASK_FILTER_PATCHED = (
    'const o=e.preserveTaskTool?t:t.filter(e=>"TASK"!==e.toolIdentifier),'
    "_sand_keep=!0===e.preserveTaskTool;"
    + SAND_TASK_TOOL_MARKER
    + 'return(e.toolsOverride?e.toolsOverride(o,n,r):o).filter(e=>'
    '(_sand_keep||"TASK"!==e.toolIdentifier)&&"ASK_QUESTION"!==e.toolIdentifier&&'
    '!("PLATFORM_ACTION"===e.toolIdentifier&&mX(e.name)))'
)
SUBAGENT_GUIDANCE_ORIGINAL = (
    "- Launch multiple agents concurrently whenever possible, to maximize "
    "performance; to do that, use a single message with multiple tool uses."
)
SUBAGENT_GUIDANCE_PATCHED = (
    "- Use the smallest useful number of subagents. Prefer one agent for broad "
    "exploration and at most five for normal independent complex areas. Use "
    "more than five only when the user explicitly requests broad parallelism; "
    "never exceed ten. Do not delegate simple work, duplicate scopes, or ask a "
    "subagent to create another subagent. Keep the parent model and reasoning "
    "settings unless the user explicitly requests another model. Gather and "
    "synthesize results before responding."
    + '${""'
    + SAND_TASK_TOOL_MARKER
    + "}"
)
SUBAGENT_PARALLEL_GUIDANCE_ORIGINAL = (
    "If it is possible to explore different areas of the codebase in parallel, "
    "you should launch multiple agents concurrently."
)
SUBAGENT_PARALLEL_GUIDANCE_PATCHED = (
    "Use parallel subagents only when clearly independent complex areas require "
    "separate context; otherwise work directly. Use no more than five by "
    "default. Only use up to ten when the user explicitly requests broad "
    "parallelism and every scope is non-overlapping."
    + '${""'
    + SAND_TASK_TOOL_MARKER
    + "}"
)
SUBAGENT_LIMIT_CAP_ORIGINAL = (
    "...void 0===_?{}:{localSubagentLimits:{maxRunning:_.maxRunning}},"
)
SUBAGENT_LIMIT_CAP_PATCHED = (
    "...{localSubagentLimits:{maxRunning:Math.min(Number(_?.maxRunning)||10,10)"
    + SAND_TASK_TOOL_MARKER
    + "}},"
)
SUBAGENT_ROUTE_ORIGINAL = (
    '!0===e.directMetaParentChildSubagent?"direct-meta-subagent-not-supported":void 0'
)
SUBAGENT_ROUTE_PATCHED = (
    "void 0"
    + SAND_SUBAGENT_ROUTE_MARKER
)

JS_IDENTIFIER_PATTERN = r"[A-Za-z_$][A-Za-z0-9_$]*"
SUBAGENT_TASK_FILTER_GENERIC_RE = re.compile(
    rf'const (?P<filtered>{JS_IDENTIFIER_PATTERN})='
    rf'(?P<config>{JS_IDENTIFIER_PATTERN})\.preserveTaskTool\?'
    rf'(?P<tools>{JS_IDENTIFIER_PATTERN}):(?P=tools)\.filter\('
    rf'(?P<source_item>{JS_IDENTIFIER_PATTERN})=>"TASK"!=='
    rf'(?P=source_item)\.toolIdentifier\);'
    rf'return\((?P=config)\.toolsOverride\?'
    rf'(?P=config)\.toolsOverride\((?P=filtered),'
    rf'(?P<context>{JS_IDENTIFIER_PATTERN}),'
    rf'(?P<mode>{JS_IDENTIFIER_PATTERN})\):(?P=filtered)\)\.filter\('
    rf'(?P<final_item>{JS_IDENTIFIER_PATTERN})=>"ASK_QUESTION"!=='
    rf'(?P=final_item)\.toolIdentifier&&!\("PLATFORM_ACTION"==='
    rf'(?P=final_item)\.toolIdentifier&&'
    rf'(?P<platform_check>{JS_IDENTIFIER_PATTERN})\('
    rf'(?P=final_item)\.name\)\)\)'
)
SUBAGENT_TASK_FILTER_HARDENED_RE = re.compile(
    rf'const (?P<filtered>{JS_IDENTIFIER_PATTERN})='
    rf'(?P<config>{JS_IDENTIFIER_PATTERN})\.preserveTaskTool\?'
    rf'(?P<tools>{JS_IDENTIFIER_PATTERN}):(?P=tools)\.filter\('
    rf'(?P<source_item>{JS_IDENTIFIER_PATTERN})=>"TASK"!=='
    rf'(?P=source_item)\.toolIdentifier\),'
    rf'_sand_keep=!0===(?P=config)\.preserveTaskTool;'
    rf'{re.escape(SAND_TASK_TOOL_MARKER)}'
    rf'return\((?P=config)\.toolsOverride\?'
    rf'(?P=config)\.toolsOverride\((?P=filtered),'
    rf'(?P<context>{JS_IDENTIFIER_PATTERN}),'
    rf'(?P<mode>{JS_IDENTIFIER_PATTERN})\):(?P=filtered)\)\.filter\('
    rf'(?P<final_item>{JS_IDENTIFIER_PATTERN})=>\(_sand_keep\|\|"TASK"!=='
    rf'(?P=final_item)\.toolIdentifier\)&&"ASK_QUESTION"!=='
    rf'(?P=final_item)\.toolIdentifier&&!\("PLATFORM_ACTION"==='
    rf'(?P=final_item)\.toolIdentifier&&'
    rf'(?P<platform_check>{JS_IDENTIFIER_PATTERN})\('
    rf'(?P=final_item)\.name\)\)\)'
)
AGENT_HOST_ENABLEMENT_RE = re.compile(
    r"(this\._agentHostEnabled=)([A-Za-z_$][A-Za-z0-9_$]*)(,)"
)
MOVE_EXEC_GATE_ORIGINAL = (
    "Promise.resolve(a.cursor.checkFeatureGate(mYe)).catch(()=>!1)"
)
MOVE_EXEC_GATE_PATCHED = "Promise.resolve(!0)" + SAND_MOVE_EXEC_MARKER
HOST_TERMINAL_HINT_ORIGINAL = (
    'vYe=H({workspacePaths:k,projectDir:B,ripgrepPath:F||void 0,network:"inherit",'
    "resourceDefaults:{cursorRulesService:"
)
HOST_TERMINAL_HINT_PATCHED = (
    'vYe=H({workspacePaths:k,projectDir:B,ripgrepPath:F||void 0,network:"inherit",'
    "resourceDefaults:{userTerminalHint:(()=>{"
    'let _sand_shell="";'
    "try{"
    '_sand_shell=a.env.shell??"";'
    'const _sand_cfg=a.workspace.getConfiguration("terminal.integrated"),'
    '_sand_os="win32"===process.platform?"windows":'
    '"darwin"===process.platform?"osx":"linux",'
    "_sand_profile=_sand_cfg.get(`defaultProfile.${_sand_os}`);"
    "if(_sand_profile){"
    "const _sand_path=_sand_cfg.get(`profiles.${_sand_os}`)?.[_sand_profile]?.path;"
    "_sand_path&&(_sand_shell=_sand_path)}"
    "}catch{}"
    "return _sand_shell})(),"
    + SAND_TOPOLOGY_MARKER
    + "cursorRulesService:"
)
LOCAL_MODEL_IDENTITY_ORIGINAL = (
    "nameWeTellTheModelToCallItself:pRe(t),strictArgParsing:!1,"
)
LOCAL_MODEL_IDENTITY_PATCHED = (
    'nameWeTellTheModelToCallItself:"default"===e.modelId?'
    "e.resolvedModel?.modelId??e.modelId:e.modelId,"
    + SAND_TOPOLOGY_MARKER
    + "strictArgParsing:!1,"
)
LOCAL_MODEL_IDENTITY_STATEMENT_ORIGINAL = (
    'r?t0("p",{children:"You operate in Cursor."}):'
    'n0(r0,{children:[t0("p",{children:"You operate in Cursor."}),'
    't0("p",{children:"You are a coding agent in the Cursor IDE that helps '
    'the USER with software engineering tasks."}),'
)
LOCAL_MODEL_IDENTITY_STATEMENT = (
    'n0("p",{children:["Your model identifier is `",'
    "e.nameWeTellTheModelToCallItself,"
    '"`. When the USER asks which model you are, state this identifier '
    'verbatim rather than only a model family name."]})'
)
LOCAL_MODEL_IDENTITY_STATEMENT_PATCHED = (
    'r?n0(r0,{children:[t0("p",{children:"You operate in Cursor."}),'
    + LOCAL_MODEL_IDENTITY_STATEMENT
    + "]}):"
    'n0(r0,{children:[t0("p",{children:"You operate in Cursor."}),'
    't0("p",{children:"You are a coding agent in the Cursor IDE that helps '
    'the USER with software engineering tasks."}),'
    + LOCAL_MODEL_IDENTITY_STATEMENT
    + SAND_TOPOLOGY_MARKER
    + ","
)
_WRITE_PAYLOAD_GUARD_ANCHOR_HEAD = (
    'n0("li",{children:["Do NOT add comments that just narrate what the code does. '
    "Avoid obvious, redundant comments like \",'\"// Import the module\"',\",\",\" \","
    "'\"// Define the function\"',\", \",'\"// Increment the counter\"',\",\",\" \","
    "'\"// Return the result\"',\", or \",'\"// Handle the error\"',\". Comments should "
    "only explain non-obvious intent, trade-offs, or constraints that the code itself "
    'cannot convey. NEVER explain the change your are making in code comments."]})'
)
_WRITE_PAYLOAD_GUARD_ANCHOR_TAIL = "]})]})}"
WRITE_PAYLOAD_GUARD_ITEM = (
    'n0("li",{children:["Keep every single text argument of a file-writing tool call small: '
    'one contents value of ",'
    'e.toolInfo.allTools.WRITE?.name??"Write"," or one new_string value of ",'
    'e.toolInfo.allTools.STR_REPLACE?.name??"StrReplace",'
    '" must stay under about 8,000 characters of ASCII text or 2,500 CJK '
    "characters (roughly 200 lines). In this runtime a text argument reaches the "
    "editor only after that whole value has been generated, and the connection is "
    "dropped after about two minutes of silence, so a longer single value never "
    "lands and every retry fails the same way. For a bigger file or replacement, "
    "continue the text in contents_2, contents_3, ... (or new_string_2, ...) of the "
    "same call, in order and each under the limit; the tool writes the "
    'concatenation of all parts."]})'
)
WRITE_PAYLOAD_GUARD_ORIGINAL = _WRITE_PAYLOAD_GUARD_ANCHOR_HEAD + _WRITE_PAYLOAD_GUARD_ANCHOR_TAIL
WRITE_PAYLOAD_GUARD_PATCHED = (
    _WRITE_PAYLOAD_GUARD_ANCHOR_HEAD
    + ","
    + WRITE_PAYLOAD_GUARD_ITEM
    + SAND_TOPOLOGY_MARKER
    + _WRITE_PAYLOAD_GUARD_ANCHOR_TAIL
)
WRITE_CONTENTS_PARTS = 8
STR_REPLACE_PARTS = 4
_PART_LIMIT_SENTENCE = (
    "Keep each part under about 8,000 characters of ASCII text or 2,500 CJK "
    "characters (roughly 200 lines): in this runtime a longer single value "
    "never reaches the editor and every retry fails the same way."
)


def _part_fields(field: str, count: int) -> str:
    return "".join(
        f',{field}_{index}:Us.Yj().optional().describe("Part {index} of {field}, continuing '
        f'part {index - 1}; same size limit; omit when not needed")'
        for index in range(2, count + 1)
    )


WRITE_CONTENTS_PARTS_ORIGINAL = 'contents:Us.Yj().describe("The contents to write to the file")'
WRITE_CONTENTS_PARTS_PATCHED = (
    'contents:Us.Yj().describe("The contents to write to the file, or its first part. '
    + _PART_LIMIT_SENTENCE
    + f" For a longer file continue in contents_2, contents_3, ... contents_{WRITE_CONTENTS_PARTS} "
    'of this same call, in order; the file is written as the concatenation of all parts.")'
    + _part_fields("contents", WRITE_CONTENTS_PARTS)
    + SAND_TOPOLOGY_MARKER
)
STR_REPLACE_PARTS_ORIGINAL = (
    'new_string:Us.Yj().describe("The text to replace it with (must be different from old_string)"),'
    'replace_all:Us.zM().optional().describe("Replace all occurrences of old_string (default false)")'
)
STR_REPLACE_PARTS_PATCHED = (
    'new_string:Us.Yj().describe("The text to replace it with (must be different from old_string), '
    "or its first part. "
    + _PART_LIMIT_SENTENCE
    + f" For a longer replacement continue in new_string_2, ... new_string_{STR_REPLACE_PARTS} "
    'of this same call, in order; the replacement is the concatenation of all parts.")'
    + _part_fields("new_string", STR_REPLACE_PARTS)
    + ',replace_all:Us.zM().optional().describe("Replace all occurrences of old_string (default false)")'
    + SAND_TOPOLOGY_MARKER
)
STREAM_PARTS_CAPTURE_ORIGINAL = (
    "const u=Iwe(n.path),p=n.streamField?Iwe(n.streamField):void 0,"
    "h=new xle({emitPartialTokens:!0,emitPartialValues:!0});"
)
STREAM_PARTS_CAPTURE_PATCHED = (
    "const u=Iwe(n.path),p=n.streamField?Iwe(n.streamField):void 0,"
    '_sand_sf="string"==typeof n.streamField?n.streamField:void 0,'
    "h=new xle({emitPartialTokens:!0,emitPartialValues:!0});"
    + SAND_TOPOLOGY_MARKER
)
STREAM_PARTS_PREVIEW_ORIGINAL = (
    "w=r.length,0!==(t=n).length&&o.emitToolCallDelta(s,a.toolCallId,"
    'new P.V1({delta:{case:"editToolCallDelta",value:new HB.fw({streamContentDelta:t})}}))}}'
    "else e.value!==g&&(f=e.value,g=e.value,v=!0)};"
)
STREAM_PARTS_PREVIEW_PATCHED = (
    "w=r.length,0!==(t=n).length&&o.emitToolCallDelta(s,a.toolCallId,"
    'new P.V1({delta:{case:"editToolCallDelta",value:new HB.fw({streamContentDelta:t})}}))}'
    'else if(void 0!==_sand_sf&&"string"==typeof e.value&&!e.partial&&1===e.stack.length&&'
    '"string"==typeof e.key&&e.key.startsWith(_sand_sf+"_")&&0!==e.value.length){'
    "o.emitToolCallDelta(s,a.toolCallId,"
    'new P.V1({delta:{case:"editToolCallDelta",value:new HB.fw({streamContentDelta:e.value})}}))}}'
    "else e.value!==g&&(f=e.value,g=e.value,v=!0)};"
    + SAND_TOPOLOGY_MARKER
)
STREAM_PARTS_MERGE_ORIGINAL = (
    'if(void 0===A)throw new QZ("Failed to acquire file lock - path may not have been parsed");_=!0;'
)
STREAM_PARTS_MERGE_PATCHED = (
    'if(void 0===A)throw new QZ("Failed to acquire file lock - path may not have been parsed");'
    "if(void 0!==_sand_sf){const _sand_o=n.data;"
    'let _sand_t="string"==typeof _sand_o[_sand_sf]?_sand_o[_sand_sf]:"",_sand_n=!1;'
    f"for(let _sand_i=2;_sand_i<={WRITE_CONTENTS_PARTS};_sand_i++){{"
    'const _sand_k=_sand_sf+"_"+_sand_i;'
    '"string"==typeof _sand_o[_sand_k]&&(_sand_t+=_sand_o[_sand_k],_sand_n=!0),delete _sand_o[_sand_k]}'
    "_sand_n&&(_sand_o[_sand_sf]=_sand_t)}"
    + SAND_TOPOLOGY_MARKER
    + "_=!0;"
)
LOCAL_SUMMARIZATION_ORIGINAL = (
    "nonFileRules:[],enableTerminalFiles:!0}),"
    "void 0===t.agentTokenLimit?{}:{agentTokenLimit:t.agentTokenLimit}"
)
LOCAL_SUMMARIZATION_PATCHED = (
    "nonFileRules:[],enableTerminalFiles:!0,"
    "backgroundSummarizationProps:{"
    "unusedPercentTokensThresholdToStartBackgroundSummarization:.1}"
    + SAND_TOPOLOGY_MARKER
    + "}),void 0===t.agentTokenLimit?{}:{agentTokenLimit:t.agentTokenLimit}"
)
CONTEXT_WINDOW_REPORT_ORIGINAL = (
    'else if("extendedUsage"===e.response.case){const r=e.response.value;'
    "t.resolveExtendedUsage({inputTokens:r.inputTokens,outputTokens:r.outputTokens,"
    "cacheReadTokens:r.cacheReadTokens,cacheWriteTokens:r.cacheWriteTokens,"
    "maxTokens:r.maxTokens}),p=!0}"
)
CONTEXT_WINDOW_REPORT_PATCHED = (
    'else if("extendedUsage"===e.response.case){const r=e.response.value;'
    "t.resolveExtendedUsage({inputTokens:r.inputTokens,outputTokens:r.outputTokens,"
    "cacheReadTokens:r.cacheReadTokens,cacheWriteTokens:r.cacheWriteTokens,"
    "maxTokens:(()=>{try{"
    "const _sand_p=(this.requestedModel?.parameters??[])"
    '.find(_sand_q=>"context"===_sand_q.id),'
    "_sand_m=void 0===_sand_p?null:/^(\\d+)(k|m)$/i.exec(String(_sand_p.value));"
    "if(null===_sand_m)return r.maxTokens;"
    'const _sand_v=Number(_sand_m[1])*("m"===_sand_m[2].toLowerCase()?1e6:1e3);'
    "return _sand_v>r.maxTokens?_sand_v:r.maxTokens"
    "}catch(_sand_e){return r.maxTokens}})()"
    + SAND_TOPOLOGY_MARKER
    + "}),p=!0}"
)
LOCAL_UNAUTH_TRANSPORT_ORIGINAL = (
    "const r=null===(i=null===(o=u.details)||void 0===o?void 0:o.analyticsMetadata)"
    "||void 0===i?void 0:i.actionRequired;"
    'if(void 0!==r&&""!==r)return new gt(Rt(e,u),r,p);'
    'if(wt.has(t))return new gt(Rt(e,u),"login",p);'
    'if(vt.has(t))return new gt(Rt(e,u),"upgrade",p);'
    'if(_t.has(t))return new gt(Rt(e,u),"payment",p);'
    'if(bt.has(t))return new gt(Rt(e,u),"config",p);'
    'if(Et.has(t)&&!0!==(null===(a=u.details)||void 0===a?void 0:a.isRetryable))return new At(Rt(e,u),p);'
    'if(!1===(null===(c=u.details)||void 0===c?void 0:c.isRetryable))return new At(Rt(e,u),p)}'
    'if(e.code===dt.C.Unauthenticated)return new gt(e.message,"login",p);'
)
LOCAL_UNAUTH_TRANSPORT_PATCHED = (
    "const r=null===(i=null===(o=u.details)||void 0===o?void 0:o.analyticsMetadata)"
    "||void 0===i?void 0:i.actionRequired;"
    "if(wt.has(t))return new ft(Rt(e,u),Object.assign({},p,{isTransport:!0}));"
    'if(void 0!==r&&""!==r)return new gt(Rt(e,u),r,p);'
    'if(vt.has(t))return new gt(Rt(e,u),"upgrade",p);'
    'if(_t.has(t))return new gt(Rt(e,u),"payment",p);'
    'if(bt.has(t))return new gt(Rt(e,u),"config",p);'
    'if(Et.has(t)&&!0!==(null===(a=u.details)||void 0===a?void 0:a.isRetryable))return new At(Rt(e,u),p);'
    'if(!1===(null===(c=u.details)||void 0===c?void 0:c.isRetryable))return new At(Rt(e,u),p)}'
    "if(e.code===dt.C.Unauthenticated)return new ft(e.message,Object.assign({},p,{isTransport:!0}))"
    + SAND_TOPOLOGY_MARKER
    + ";"
)
LOCAL_CONVERSATION_ACTION_ORIGINAL = "y=new ume,[w,v]=e.withCancel()"
LOCAL_CONVERSATION_ACTION_PATCHED = (
    "y=(()=>{const _sand_q=[],_sand_ls=new Set;"
    'const _sand_is_inj=_sand_a=>"injectContextAction"===_sand_a?.action?.case;'
    "const _sand_is_user=_sand_a=>{if(!_sand_is_inj(_sand_a))return!1;"
    "const _sand_p=_sand_a.action.value?.payload;"
    'return"userContext"===_sand_p?.case&&void 0!==_sand_p.value?.userMessage};'
    "const _sand_sub=a.submitConversationAction.bind(a);"
    "a.submitConversationAction=_sand_a=>Promise.resolve(_sand_sub(_sand_a))"
    ".then(_sand_v=>(_sand_q.push(_sand_a),_sand_is_user(_sand_a)&&"
    "_sand_ls.forEach(_sand_f=>{try{_sand_f()}catch(_sand_e){}}),_sand_v));"
    "let _sand_claimed=!1,_sand_last;"
    "const _sand_sig={hasPendingUserInjections:()=>_sand_q.some(_sand_is_user),"
    "onUserInjectionAdmitted:_sand_f=>(_sand_ls.add(_sand_f),()=>{_sand_ls.delete(_sand_f)})};"
    "const _sand_as_user=_sand_a=>_sand_is_user(_sand_a)?"
    '{action:{case:"userMessageAction",value:{'
    "userMessage:_sand_a.action.value.payload.value.userMessage,"
    "requestContext:_sand_a.action.value.payload.value.requestContext}}}:_sand_a;"
    "return{peek:async()=>{const _sand_h=_sand_q[0];if(void 0===_sand_h)return;"
    "_sand_claimed=_sand_is_inj(_sand_h);"
    "return _sand_as_user(_sand_h)},pop:async()=>{_sand_last=_sand_q.shift();"
    "_sand_claimed=!1},peekIsClaimedInjection:()=>_sand_claimed,"
    "failConsumedInjectionDelivery:()=>{void 0!==_sand_last&&_sand_q.unshift(_sand_last)},"
    "getContextInjectionToolSignal:()=>_sand_sig,..._sand_sig}})()"
    + SAND_TOPOLOGY_MARKER
    + ",[w,v]=e.withCancel()"
)
_USAGE_LIMIT_AUTO_SWITCH_ORIGINAL_TEMPLATE = (
    '%X%.kind==="switch"&&%HANDLE%&&(%SVC%.aiSettingsService.enableModel(%TARGET%),'
    "%SVC%.modelConfigService.setModelConfigForComposer(%HANDLE%,"
    "{modelName:%X%.modelName,maxMode:%X%.maxMode}),"
    '%SVC%.structuredLogService.info("composer",'
    '"UsageLimitPolicyBanner: auto-switched composer to allowed model",'
    '{subkey:"usage_limit_banner_auto_switch",fromModel:%X%.currentModelName??"",'
    "toModel:%TARGET%,maxMode:String(%X%.maxMode)}),%LAST%=%TARGET%)}%SHOULD%||(%LAST%=null)});"
)
_USAGE_LIMIT_AUTO_SWITCH_PATCHED_TEMPLATE = (
    '%X%.kind==="switch"&&%HANDLE%&&('
    '%SVC%.structuredLogService.info("composer",'
    '"UsageLimitPolicyBanner: auto-switch suppressed under Sand managed-local",'
    '{subkey:"usage_limit_banner_auto_switch_suppressed",'
    'fromModel:%X%.currentModelName??"",toModel:%TARGET%,maxMode:String(%X%.maxMode)}),'
    "%LAST%=%TARGET%)" + SAND_TOPOLOGY_MARKER + "}%SHOULD%||(%LAST%=null)});"
)


_PLACEHOLDER_RE = re.compile(r"%[A-Z][A-Z0-9_]*%")


def _bind_minified_names(
    original_template: str,
    patched_template: str,
    **names: str,
) -> Tuple[str, str]:

    def bind(template: str) -> str:
        bound = template
        for token, name in names.items():
            bound = bound.replace(f"%{token}%", name)
        leftover = _PLACEHOLDER_RE.findall(bound)
        if leftover:
            raise RuntimeError(f"模板存在未绑定占位符：{sorted(set(leftover))}")
        return bound

    used = {token for token in names if f"%{token}%" in original_template + patched_template}
    unused = sorted(set(names) - used)
    if unused:
        raise RuntimeError(f"模板未使用的占位符绑定：{unused}")
    return bind(original_template), bind(patched_template)


def _usage_limit_auto_switch_pair(
    services: str,
    handle: str,
    target: str,
    last_switched: str,
    should_switch: str,
    kind: str = "X",
) -> Tuple[str, str]:
    return _bind_minified_names(
        _USAGE_LIMIT_AUTO_SWITCH_ORIGINAL_TEMPLATE,
        _USAGE_LIMIT_AUTO_SWITCH_PATCHED_TEMPLATE,
        HANDLE=handle, TARGET=target, SHOULD=should_switch, LAST=last_switched,
        SVC=services, X=kind,
    )


USAGE_LIMIT_AUTO_SWITCH_DESKTOP = _usage_limit_auto_switch_pair(
    services="t", handle="q", target="G", last_switched="I", should_switch="K",
    kind="ee",
)
USAGE_LIMIT_AUTO_SWITCH_GLASS = _usage_limit_auto_switch_pair(
    services="e", handle="G", target="H", last_switched="C", should_switch="q",
    kind="X",
)
_USAGE_LIMIT_BANNER_ORIGINAL_TEMPLATE = (
    "%VAR%=%MEMO%(()=>{const %INNER%=r();return %INNER%===null?!1:"
    "%HELPER%({stage:%INNER%.stage,isInSlowPool:%INNER%.isInSlowPool===!0})})"
)
_USAGE_LIMIT_BANNER_PATCHED_TEMPLATE = "%VAR%=%MEMO%(()=>!1)" + SAND_TOPOLOGY_MARKER


def _usage_limit_banner_pair(
    memo_var: str,
    memo: str,
    inner: str,
    helper: str,
) -> Tuple[str, str]:
    return _bind_minified_names(
        _USAGE_LIMIT_BANNER_ORIGINAL_TEMPLATE,
        _USAGE_LIMIT_BANNER_PATCHED_TEMPLATE,
        HELPER=helper, INNER=inner, MEMO=memo, VAR=memo_var,
    )


USAGE_LIMIT_BANNER_DESKTOP = _usage_limit_banner_pair(
    memo_var="c", memo="Me", inner="g", helper="K0b"
)
USAGE_LIMIT_BANNER_GLASS = _usage_limit_banner_pair(
    memo_var="l", memo="ut", inner="p", helper="XHS"
)
_TASK_CARD_MODEL_LABEL_ORIGINAL_TEMPLATE = (
    "function %FN%(%ARG%,%MODELS%,%CFG%){const %RES%=%RESOLVE%(%MODELS%,%ARG%);"
    "if(%RES%)return %DISPLAY%(%RES%,%MODELS%,%CFG%?.maxMode);"
)
_TASK_CARD_MODEL_LABEL_PATCHED_TEMPLATE = (
    "function %FN%(%ARG%,%MODELS%,%CFG%){"
    'const _sand_v="string"==typeof %ARG%&&%ARG%.includes("[")?%PARSE%(%ARG%):void 0,'
    "%RES%=!0===_sand_v?.ok?{modelId:_sand_v.modelId,parameters:_sand_v.parameterValues"
    ".map(_sand_q=>({id:_sand_q.id,value:_sand_q.value}))}:%RESOLVE%(%MODELS%,%ARG%);"
    "if(%RES%){const _sand_s=%CFG%?.selectedModels?.[0];"
    "return %DISPLAY%(0===%RES%.parameters.length&&void 0!==_sand_s&&"
    "_sand_s.modelId===%RES%.modelId&&(_sand_s.parameters?.length??0)>0?"
    "{modelId:%RES%.modelId,parameters:_sand_s.parameters}:%RES%,%MODELS%,%CFG%?.maxMode)}"
    + SAND_TOPOLOGY_MARKER
)


def _task_card_model_label_pair(
    function_name: str,
    name_arg: str,
    models_arg: str,
    config_arg: str,
    resolved: str,
    resolve_helper: str,
    display_helper: str,
    parse_helper: str,
) -> Tuple[str, str]:
    return _bind_minified_names(
        _TASK_CARD_MODEL_LABEL_ORIGINAL_TEMPLATE,
        _TASK_CARD_MODEL_LABEL_PATCHED_TEMPLATE,
        RESOLVE=resolve_helper, DISPLAY=display_helper, PARSE=parse_helper,
        MODELS=models_arg, RES=resolved, ARG=name_arg, CFG=config_arg, FN=function_name,
    )


TASK_CARD_MODEL_LABEL_DESKTOP = _task_card_model_label_pair(
    function_name="Gbd", name_arg="e", models_arg="t", config_arg="n",
    resolved="i", resolve_helper="X4g", display_helper="tSs", parse_helper="K4g",
)
TASK_CARD_MODEL_LABEL_GLASS = _task_card_model_label_pair(
    function_name="o7p", name_arg="t", models_arg="e", config_arg="n",
    resolved="i", resolve_helper="V0h", display_helper="Ofn", parse_helper="z0h",
)
_MODEL_NUDGE_DEFAULT_ORIGINAL_TEMPLATE = "modelNudgesEnabled:%FACTORY%(!0,-1,0),"
_MODEL_NUDGE_DEFAULT_PATCHED_TEMPLATE = (
    "modelNudgesEnabled:%FACTORY%(!1,-1,0)" + SAND_TOPOLOGY_MARKER + ","
)


def _model_nudge_default_pair(factory: str) -> Tuple[str, str]:
    return _bind_minified_names(
        _MODEL_NUDGE_DEFAULT_ORIGINAL_TEMPLATE,
        _MODEL_NUDGE_DEFAULT_PATCHED_TEMPLATE,
        FACTORY=factory,
    )


MODEL_NUDGE_DEFAULT_DESKTOP = _model_nudge_default_pair(factory="Ah")
MODEL_NUDGE_DEFAULT_GLASS = _model_nudge_default_pair(factory="lm")
_TASK_CARD_MODEL_CONFIG_ORIGINAL_TEMPLATE = (
    "%FE%=(()=>{const %HT%=%C%?%A%?.data.modelConfig:void 0,"
    "%LT%=%US%?.modelConfig,"
    "%RT%=!%HT%?.selectedModels?.length&&%LT%?.selectedModels?.length?%LT%:%HT%??%LT%,"
    "%OT%=%FCF%({bestOfNModelName:%BON%?%RNB%(%R%):void 0,"
    "resumeTargetComposerId:%CID%,subagentModelName:%RT%?.modelName,"
    "taskParamModelName:%R%});return %LABEL%(%OT%,%P%,%BON%?void 0:%RT%)})()"
)
_TASK_CARD_MODEL_CONFIG_PATCHED_TEMPLATE = (
    "%FE%=(()=>{const %HT%=%C%?%A%?.data.modelConfig:void 0,"
    "%LT%=%US%?.modelConfig,"
    "_sand_rt=!%HT%?.selectedModels?.length&&%LT%?.selectedModels?.length?%LT%:%HT%??%LT%,"
    "%RT%=(()=>{if(%BON%||_sand_rt?.selectedModels?.length)return _sand_rt;"
    "const _sand_live=%C%?void 0:%A%?.data?.modelConfig;"
    "if(_sand_live?.selectedModels?.length)return _sand_live;"
    "const _sand_id=%R%??%HOSTQ%(%STATE%,%CHILD%)?.modelId??_sand_live?.modelName,"
    "_sand_parent=%PARENT%.data.modelConfig,_sand_sel=_sand_parent?.selectedModels;"
    "return void 0!==_sand_sel&&1===_sand_sel.length&&"
    "(void 0===_sand_id||_sand_id===_sand_sel[0].modelId)?_sand_parent:_sand_rt})(),"
    "%OT%=%FCF%({bestOfNModelName:%BON%?%RNB%(%R%):void 0,"
    "resumeTargetComposerId:%CID%,subagentModelName:%RT%?.modelName,"
    "taskParamModelName:%R%});return %LABEL%(%OT%,%P%,%BON%?void 0:%RT%)})()"
    + SAND_TOPOLOGY_MARKER
)


def _task_card_model_config_pair(**names: str) -> Tuple[str, str]:
    return _bind_minified_names(
        _TASK_CARD_MODEL_CONFIG_ORIGINAL_TEMPLATE,
        _TASK_CARD_MODEL_CONFIG_PATCHED_TEMPLATE,
        **names,
    )


TASK_CARD_MODEL_CONFIG_DESKTOP = _task_card_model_config_pair(
    FE="je", HT="st", C="c", A="a", LT="rt", US="_", RT="Pt", OT="bt", FCF="EEf",
    BON="le", RNB="tr_", R="D", CID="C", LABEL="Gbd", P="m",
    HOSTQ="HXs", STATE="f", CHILD="I", PARENT="s",
)
TASK_CARD_MODEL_CONFIG_GLASS = _task_card_model_config_pair(
    FE="Ce", HT="Pe", C="l", A="a", LT="ge", US="b", RT="Ye", OT="He", FCF="DnS",
    BON="ie", RNB="FE1", R="E", CID="S", LABEL="o7p", P="h",
    HOSTQ="P8r", STATE="g", CHILD="C", PARENT="s",
)
_CHILD_MODEL_CONFIG_ORIGINAL_TEMPLATE = (
    "_resolveChildModelConfig(%SVC%,%CFG%,n){"
    'const i=%CFG%??%SVC%.modelConfigService.getModelConfig("composer"),'
    "r={modelName:n??i.modelName,maxMode:i.maxMode===!0},"
)
_CHILD_MODEL_CONFIG_PATCHED_TEMPLATE = (
    "_resolveChildModelConfig(%SVC%,%CFG%,n){"
    'const _sand_v="string"==typeof n&&n.includes("[")?%PARSE%(n):void 0;'
    "if(!0===_sand_v?.ok){n=_sand_v.modelId;"
    "if(_sand_v.parameterValues.length>0){"
    'const _sand_i=%CFG%??%SVC%.modelConfigService.getModelConfig("composer");'
    "return{modelName:n,maxMode:_sand_i.maxMode===!0,"
    "selectedModels:[{modelId:n,parameters:_sand_v.parameterValues"
    ".map(_sand_q=>({id:_sand_q.id,value:_sand_q.value}))}]}}}"
    + SAND_TOPOLOGY_MARKER
    + 'const i=%CFG%??%SVC%.modelConfigService.getModelConfig("composer"),'
    "r={modelName:n??i.modelName,maxMode:i.maxMode===!0},"
)


def _child_model_config_pair(
    services_arg: str, config_arg: str, parse_helper: str
) -> Tuple[str, str]:
    return _bind_minified_names(
        _CHILD_MODEL_CONFIG_ORIGINAL_TEMPLATE,
        _CHILD_MODEL_CONFIG_PATCHED_TEMPLATE,
        PARSE=parse_helper, SVC=services_arg, CFG=config_arg,
    )


CHILD_MODEL_CONFIG_DESKTOP = _child_model_config_pair(
    services_arg="e", config_arg="t", parse_helper="K4g"
)
CHILD_MODEL_CONFIG_GLASS = _child_model_config_pair(
    services_arg="t", config_arg="e", parse_helper="z0h"
)
_REQUEST_MAX_MODE_ORIGINAL_TEMPLATE = (
    '%IDSRC%=%SELS%[0]?.modelId?"selectedModels[0]":%CNAME%?"modelConfig.modelName":'
    '"modelDetails.modelName";'
    'return %THIS%.logService.info("[buildRequestedModel]",'
    "`composerId=${%HANDLE%.composerId}`,`catalogModelId=${%CAT%}`,"
    "`idSource=${%IDSRC%}`,`experimentalOverrideApplied=${%OVR%}`,"
    "`hasAssistantTurn=${%HAS%}`,"
    "`composerModelName=${%CNAME%??\"(none)\"}`,"
    "`selectedModelIds=${%SELS%.map(%L%=>%L%.modelId).join(\",\")||\"(none)\"}`,"
    "`matchingSelectedModel=${!!%MATCH%}`,`maxMode=${%MAX%}`,"
    "`resolvedParams=${%PARAMS%.length}`),"
    "new %MODELCLS%({modelId:%CAT%,maxMode:%MAX%,"
    "parameters:%PARAMS%.map(%L%=>new %PARAMCLS%({id:%L%.id,value:%L%.value})),"
    "credentials:%CREDS%(%DETAILS%)})}"
)
_REQUEST_MAX_MODE_PATCHED_TEMPLATE = (
    '%IDSRC%=%SELS%[0]?.modelId?"selectedModels[0]":%CNAME%?"modelConfig.modelName":'
    '"modelDetails.modelName",'
    "_sand_model=%THIS%.modelConfigService.getAvailableDefaultModels()"
    ".find(%L%=>%L%.name===%CAT%),"
    '_sand_ctx=%PARAMS%.find(%L%=>"context"===%L%.id)?.value,'
    '_sand_max="1m"===_sand_ctx?!1!==%MAX%:void 0!==_sand_ctx?!1:'
    "%MAX%===!0&&"
    "(!0===_sand_model?.variants?.some(_sand_v=>!0===_sand_v.isMaxMode)?"
    "%REQUIRES%(_sand_model,%PARAMS%):!0)"
    + SAND_TOPOLOGY_MARKER
    + ";"
    'return %THIS%.logService.info("[buildRequestedModel]",'
    "`composerId=${%HANDLE%.composerId}`,`catalogModelId=${%CAT%}`,"
    "`idSource=${%IDSRC%}`,`experimentalOverrideApplied=${%OVR%}`,"
    "`hasAssistantTurn=${%HAS%}`,"
    "`composerModelName=${%CNAME%??\"(none)\"}`,"
    "`selectedModelIds=${%SELS%.map(%L%=>%L%.modelId).join(\",\")||\"(none)\"}`,"
    "`matchingSelectedModel=${!!%MATCH%}`,`maxMode=${%MAX%}`,"
    "`effectiveMaxMode=${_sand_max}`,`resolvedParams=${%PARAMS%.length}`),"
    "new %MODELCLS%({modelId:%CAT%,maxMode:_sand_max,"
    "parameters:%PARAMS%.map(%L%=>new %PARAMCLS%({id:%L%.id,value:%L%.value})),"
    "credentials:%CREDS%(%DETAILS%)})}"
)


def _request_max_mode_pair(**names: str) -> Tuple[str, str]:
    return _bind_minified_names(
        _REQUEST_MAX_MODE_ORIGINAL_TEMPLATE,
        _REQUEST_MAX_MODE_PATCHED_TEMPLATE,
        **names,
    )


_REQUEST_MAX_MODE_DESKTOP_NAMES = dict(
    IDSRC="f", HANDLE="n", DETAILS="t", L="v", MAX="u", OVR="a", HAS="r",
    MODELCLS="EI", PARAMCLS="WM", REQUIRES="hxi",
    SELS="c", CNAME="l", CAT="h", MATCH="m", PARAMS="g",
    THIS="e", CREDS="yBd",
)
_REQUEST_MAX_MODE_GLASS_NAMES = dict(
    IDSRC="g", HANDLE="n", DETAILS="e", L="v", MAX="u", OVR="a", HAS="r",
    MODELCLS="c2", PARAMCLS="r3", REQUIRES="Koi",
    SELS="l", CNAME="c", CAT="d", MATCH="h", PARAMS="p",
    THIS="t", CREDS="Llm",
)
REQUEST_MAX_MODE_DESKTOP = _request_max_mode_pair(**_REQUEST_MAX_MODE_DESKTOP_NAMES)
REQUEST_MAX_MODE_GLASS = _request_max_mode_pair(**_REQUEST_MAX_MODE_GLASS_NAMES)
_ASK_QUESTION_ERROR_STEP_ORIGINAL_TEMPLATE = (
    '%X%=%L%==="submitted"||%L%==="cancelled"?%L%:void 0;'
    'return{kind:"activity",id:`${%BID%}:tool:${o.toolCallId??o.tool}`'
)
_ASK_QUESTION_ERROR_STEP_PATCHED_TEMPLATE = (
    '%X%=%L%==="submitted"||%L%==="cancelled"?%L%:'
    'o.tool===%TOOLS%.ASK_QUESTION&&"error"===o.status?"error":void 0;'
    + SAND_TOPOLOGY_MARKER
    + 'return{kind:"activity",id:`${%BID%}:tool:${o.toolCallId??o.tool}`'
)
_ASK_QUESTION_ERROR_VM_ORIGINAL_TEMPLATE = (
    "a=%OPTS%.askQuestionUiStatus,"
    '%SETTLED%=s!==void 0&&s.case!=="async",'
    '%UISTATE%=a==="cancelled"||a==="submitted",'
    'u=s?.case==="success"&&(o===void 0||o.length===0||'
    "o.every(%ITEM%=>(%ITEM%.selectedOptionIds?.length??0)===0&&"
    "(%ITEM%.freeformText?.trim().length??0)===0));"
    "return{callId:i,outcome:%OUTCOME%(%SETTLED%?s?.case:"
    '%UISTATE%?a==="cancelled"?"rejected":"success":s?.case,%OPTS%),'
    'case:"askQuestionToolCall"'
)
_ASK_QUESTION_ERROR_VM_PATCHED_TEMPLATE = (
    "a=%OPTS%.askQuestionUiStatus,"
    '%SETTLED%=s!==void 0&&s.case!=="async",'
    '%UISTATE%=a==="cancelled"||a==="submitted"||a==="error",'
    'u=s?.case==="success"&&(o===void 0||o.length===0||'
    "o.every(%ITEM%=>(%ITEM%.selectedOptionIds?.length??0)===0&&"
    "(%ITEM%.freeformText?.trim().length??0)===0));"
    "return{callId:i,outcome:%OUTCOME%(%SETTLED%?s?.case:"
    '%UISTATE%?a==="cancelled"?"rejected":a==="error"?"error":"success":'
    "s?.case,%OPTS%),"
    + SAND_TOPOLOGY_MARKER
    + 'case:"askQuestionToolCall"'
)


def _ask_question_error_pair(
    original_template: str,
    patched_template: str,
    **names: str,
) -> Tuple[str, str]:
    return _bind_minified_names(
        original_template,
        patched_template,
        **names,
    )


ASK_QUESTION_ERROR_STEP_DESKTOP = _ask_question_error_pair(
    _ASK_QUESTION_ERROR_STEP_ORIGINAL_TEMPLATE,
    _ASK_QUESTION_ERROR_STEP_PATCHED_TEMPLATE,
    X="x", L="l", BID="e", TOOLS="Je",
)
ASK_QUESTION_ERROR_STEP_GLASS = _ask_question_error_pair(
    _ASK_QUESTION_ERROR_STEP_ORIGINAL_TEMPLATE,
    _ASK_QUESTION_ERROR_STEP_PATCHED_TEMPLATE,
    X="w", L="c", BID="t", TOOLS="St",
)
ASK_QUESTION_ERROR_VM_DESKTOP = _ask_question_error_pair(
    _ASK_QUESTION_ERROR_VM_ORIGINAL_TEMPLATE,
    _ASK_QUESTION_ERROR_VM_PATCHED_TEMPLATE,
    OPTS="t", SETTLED="c", UISTATE="l", ITEM="h", OUTCOME="lP",
)
ASK_QUESTION_ERROR_VM_GLASS = _ask_question_error_pair(
    _ASK_QUESTION_ERROR_VM_ORIGINAL_TEMPLATE,
    _ASK_QUESTION_ERROR_VM_PATCHED_TEMPLATE,
    OPTS="e", SETTLED="l", UISTATE="c", ITEM="d", OUTCOME="KO",
)
TURN_RUNTIME_LOG_ORIGINAL = (
    'uo.info(e.ctx,"Selected Agent Host turn runtime",{runtime:n.runtime,'
    "reason:n.reason,conversationId:e.runOptions.conversationId,"
    "generationUUID:e.runOptions.generationUUID,actionCase:e.action.action.case,"
    "modelId:s||o||void 0})"
)
TURN_RUNTIME_LOG_PATCHED = (
    'uo.info(e.ctx,"Selected Agent Host turn runtime",{runtime:n.runtime,'
    "reason:n.reason,conversationId:e.runOptions.conversationId,"
    "generationUUID:e.runOptions.generationUUID,actionCase:e.action.action.case,"
    "modelId:s||o||void 0,modelMaxMode:e.runOptions.requestedModel?.maxMode,"
    "modelParameters:(()=>{"
    "const _sand_m=e.runOptions.requestedModel;"
    "return void 0!==_sand_m&&_sand_m.parameters.length>0?"
    '_sand_m.parameters.map(_sand_p=>_sand_p.id+"="+_sand_p.value).join(","):'
    "void 0})()})"
    + SAND_TOPOLOGY_MARKER
)
ROOT_INTERACTION_QUERY_ID_ORIGINAL = (
    "Agent host interaction query has no active turn: ${this.sessionId}`);"
    "return this.registry.query(e,r,t)})}"
)
ROOT_INTERACTION_QUERY_ID_PATCHED = (
    "Agent host interaction query has no active turn: ${this.sessionId}`);"
    "0===t.id&&(t.id=this._sand_query_seq=(this._sand_query_seq??0)+1);"
    + SAND_TOPOLOGY_MARKER
    + "return this.registry.query(e,r,t)})}"
)
NATIVE_RUN_IDENTITY_ORIGINAL = (
    "t=d.rootCertificates;this.systemCertificates=[...e||[],...t||[]]}catch(e){"
    'this.debugLog.error("[TransportFactory] Failed to load system certificates:",e),'
    "this.systemCertificates=[...d.rootCertificates]}})}getTransportHost(e){"
    "try{return new URL(e).host}catch(t){return e}}isLocalBaseUrl(e){"
    "return null!==e.match(/(?:[^/]+\\.)?lclhst\\.build(?::\\d+)?(?:\\/|$)/)||"
    "null!==e.match(/(?:[^/]+\\.)?localhost(?::\\d+)?(?:\\/|$)/)}"
    "applyStandardRequestHeaders(e){const t=(0,c.randomUUID)();return "
    'this.host.applyRequestHeaders(e,t,this.clientKey.toString("hex")),'
    'e.header.set("x-cursor-streaming","true"),t}'
)
NATIVE_RUN_IDENTITY_PATCHED = (
    "t=d.rootCertificates;this.systemCertificates=[...e||[],...t||[]]}catch(e){"
    'this.debugLog.error("[TransportFactory] Failed to load system certificates:",e),'
    "this.systemCertificates=[...d.rootCertificates]}})}getTransportHost(e){"
    "try{return new URL(e).host}catch(t){return e}}isLocalBaseUrl(e){"
    "return null!==e.match(/(?:[^/]+\\.)?lclhst\\.build(?::\\d+)?(?:\\/|$)/)||"
    "null!==e.match(/(?:[^/]+\\.)?localhost(?::\\d+)?(?:\\/|$)/)}"
    "applyStandardRequestHeaders(e){const t=(0,c.randomUUID)();return "
    'this.host.applyRequestHeaders(e,t,this.clientKey.toString("hex")),'
    '"agent.v1.AgentService"===e.service?.typeName&&'
    '"string"==typeof e.method?.name&&e.method.name.startsWith("Run")&&'
    'e.header.set("x-cursor-client-type",'
    '"glass"===e.header.get("x-cursor-client-layout")?"glass":"ide"),'
    + SAND_TOPOLOGY_MARKER
    + 'e.header.set("x-cursor-streaming","true"),t}'
)
_STREAM_ROUTE_HEAD = "[l.AgentService.methods.run.name]=e.agentBidiTransport,"
_STREAM_ROUTE_TAIL = (
    "this._overrideMethodNameToTransportMap[A.InferenceService.methods.runInference.name]"
    "=e.agenticComposerTransport,"
)
STREAM_ROUTE_ORIGINAL = _STREAM_ROUTE_HEAD + _STREAM_ROUTE_TAIL
STREAM_HTTP2_ROUTE_PATCHED = (
    _STREAM_ROUTE_HEAD
    + "this._overrideMethodNameToTransportMap[A.InferenceService.methods.stream.name]="
    "!0===e.agenticComposerTransport?.isHttp2?e.agenticComposerTransport:this._backendTransport,"
    + SAND_TOPOLOGY_MARKER
    + _STREAM_ROUTE_TAIL
)
MANAGED_SKILLS_IDENTITY_ORIGINAL = (
    "isAnysphereUser:W.getIsAnysphereUser(),clientType:Zk?"
    '"sand"' + SAND_CLIENT_GLASS_MARKER + ':"sand"' + SAND_CLIENT_MARKER
    + ",clientLayout:he,"
)
MANAGED_SKILLS_IDENTITY_PATCHED = (
    "isAnysphereUser:W.getIsAnysphereUser(),clientType:"
    '"GetManagedSkills"===v.req?.method?.name?'
    '["ide","glass"][Zk?1:0]:'
    "Zk?"
    '"sand"' + SAND_CLIENT_GLASS_MARKER + ':"sand"' + SAND_CLIENT_MARKER + ","
    + SAND_TOPOLOGY_MARKER
    + "clientLayout:he,"
)
USER_RULES_INJECT_DESKTOP = (
    "injectLocalModeNonFileRules(e){if(!jc.localMode)return;",
    "injectLocalModeNonFileRules(e){" + SAND_TOPOLOGY_MARKER,
)
USER_RULES_INJECT_GLASS = (
    "injectLocalModeNonFileRules(t){if(!Al.localMode)return;",
    "injectLocalModeNonFileRules(t){" + SAND_TOPOLOGY_MARKER,
)
SIMULATED_THINKING_TIMEOUT_ORIGINAL = (
    's.getDynamicConfigParam("simulated_thinking_error_timeout","timeout_ms",'
    "{disableExposureLog:!1})??0"
)
SIMULATED_THINKING_TIMEOUT_PATCHED = "(0)" + SAND_TOPOLOGY_MARKER
SIMULATED_THINKING_TIMEOUT_GROUP = "Grok 长思考不弹 Taking longer"
CONNECT_REQUEST_CONTEXT_RULES_ORIGINAL = (
    'Ft=F(e=>new N(e,G("requestContextArgs"),H("requestContextResult")),'
    '(e,t)=>{t.register(new _(e,z("requestContextArgs"),'
    'W("requestContextResult")))})'
)
CONNECT_REQUEST_CONTEXT_RULES_PATCHED = (
    'Ft=F(e=>new N(e,G("requestContextArgs"),H("requestContextResult")),'
    "(e,t)=>{t.register(new _({execute:(_sand_c,_sand_a,_sand_o)=>"
    "Promise.resolve(e.execute(_sand_c,_sand_a,_sand_o)).then(_sand_r=>{"
    'const _sand_rc="success"===_sand_r?.result?.case?'
    "_sand_r.result.value?.requestContext:void 0;"
    "return void 0!==_sand_rc&&Array.isArray(_sand_rc.rules)&&"
    "(_sand_rc.rules=_sand_rc.rules.filter(_sand_x=>"
    "1!==_sand_x.source&&2!==_sand_x.source)),_sand_r})},"
    'z("requestContextArgs"),W("requestContextResult")))'
    + SAND_TOPOLOGY_MARKER
    + "})"
)
SEMANTIC_SEARCH_ORIGINAL = (
    "enableReadLints:!0,enableSemanticSearch:!1,enableTerminalFiles:!0,"
    "enableWebSearch:!0,"
)
SEMANTIC_SEARCH_PATCHED = (
    "enableReadLints:!0,enableSemanticSearch:!0"
    + SAND_TOPOLOGY_MARKER
    + ",enableTerminalFiles:!0,enableWebSearch:!0,"
)
_APPROVAL_BUBBLE_FALLBACK_ORIGINAL_TEMPLATE = (
    "getToolContextOrWait(%H%,%TC%)}catch(%V%){return{approved:!1,"
    "reason:`Failed to find tool call context: "
    "${%V% instanceof Error?%V%.message:String(%V%)}`}}"
)
_APPROVAL_BUBBLE_FALLBACK_PATCHED_TEMPLATE = (
    "getToolContextOrWait(%H%,%TC%)}catch(%V%){"
    "if(void 0!==this._composerDataService.getComposerData(%H%)"
    "?.subagentInfo?.parentComposerId)return{approved:!0}"
    + SAND_TOPOLOGY_MARKER
    + ";return{approved:!1,reason:`Failed to find tool call context: "
    "${%V% instanceof Error?%V%.message:String(%V%)}`}}"
)


def _approval_bubble_fallback_pair(
    handle: str, tool_call: str, catch_var: str
) -> Tuple[str, str]:
    return _bind_minified_names(
        _APPROVAL_BUBBLE_FALLBACK_ORIGINAL_TEMPLATE,
        _APPROVAL_BUBBLE_FALLBACK_PATCHED_TEMPLATE,
        H=handle, TC=tool_call, V=catch_var,
    )


APPROVAL_BUBBLE_FALLBACK_PAIRS = (
    _approval_bubble_fallback_pair("e", "t", "f"),
    _approval_bubble_fallback_pair("e", "t", "l"),
    _approval_bubble_fallback_pair("e", "t", "a"),
    _approval_bubble_fallback_pair("e", "t", "k"),
    _approval_bubble_fallback_pair("e", "t", "N"),
    _approval_bubble_fallback_pair("t", "e", "g"),
    _approval_bubble_fallback_pair("t", "e", "c"),
    _approval_bubble_fallback_pair("t", "e", "a"),
    _approval_bubble_fallback_pair("t", "e", "y"),
    _approval_bubble_fallback_pair("t", "e", "P"),
)

_IMAGE_PROMPT_WORD_COUNT_ORIGINAL_TEMPLATE = (
    "const y=%D%.description.trim(),%W%=(y.length>0?y.split(/\\s+/):[]).length"
)
_IMAGE_PROMPT_WORD_COUNT_PATCHED_TEMPLATE = (
    "const y=%D%.description.trim(),%W%=(()=>{"
    "const _sand_w=(y.length>0?y.split(/\\s+/):[]).length;"
    "try{let _sand_n=0;"
    'for(const _sand_s of new Intl.Segmenter(void 0,{granularity:"word"}).segment(y))'
    "_sand_s.isWordLike&&_sand_n++;"
    "return _sand_n>_sand_w?_sand_n:_sand_w"
    "}catch(_sand_e){return _sand_w}})()"
    + SAND_TOPOLOGY_MARKER
)


def _image_prompt_word_count_pair(description_holder: str, count_var: str) -> Tuple[str, str]:
    return _bind_minified_names(
        _IMAGE_PROMPT_WORD_COUNT_ORIGINAL_TEMPLATE,
        _IMAGE_PROMPT_WORD_COUNT_PATCHED_TEMPLATE,
        D=description_holder, W=count_var,
    )


IMAGE_PROMPT_WORD_COUNT_HOST = (
    "const w=u.description.trim(),v=(w.length>0?w.split(/\\s+/):[]).length",
    "const w=u.description.trim(),v=(()=>{"
    "const _sand_w=(w.length>0?w.split(/\\s+/):[]).length;"
    "try{let _sand_n=0;"
    'for(const _sand_s of new Intl.Segmenter(void 0,{granularity:"word"}).segment(w))'
    "_sand_s.isWordLike&&_sand_n++;"
    "return _sand_n>_sand_w?_sand_n:_sand_w"
    "}catch(_sand_e){return _sand_w}})()"
    + SAND_TOPOLOGY_MARKER
)
IMAGE_PROMPT_WORD_COUNT_RUNTIME = (
    "const v=c.description.trim(),y=(v.length>0?v.split(/\\s+/):[]).length",
    "const v=c.description.trim(),y=(()=>{"
    "const _sand_w=(v.length>0?v.split(/\\s+/):[]).length;"
    "try{let _sand_n=0;"
    'for(const _sand_s of new Intl.Segmenter(void 0,{granularity:"word"}).segment(v))'
    "_sand_s.isWordLike&&_sand_n++;"
    "return _sand_n>_sand_w?_sand_n:_sand_w"
    "}catch(_sand_e){return _sand_w}})()"
    + SAND_TOPOLOGY_MARKER
)
IMAGE_PROMPT_WORD_COUNT_EXEC = _image_prompt_word_count_pair("c", "v")


NATIVE_CLOUD_SUBAGENT_ORIGINAL = (
    "Promise.resolve(a.cursor.checkFeatureGate(fYe)).catch(()=>!1)"
)
NATIVE_CLOUD_SUBAGENT_PATCHED = (
    "Promise.resolve(!0)" + SAND_NATIVE_CLOUD_SUBAGENT_MARKER
)
SUBAGENT_INTERACTIONS_ORIGINAL = (
    "Promise.resolve(a.cursor.checkFeatureGate(gYe)).catch(()=>!1)"
)
SUBAGENT_INTERACTIONS_PATCHED = (
    "Promise.resolve(!0)" + SAND_SUBAGENT_INTERACTIONS_MARKER
)
SUBAGENT_BUBBLE_FALLBACK_ORIGINAL = (
    "bubbleToParent:(e,t,r)=>{const n=this.options.bubbleInteractionToParent;"
    'if(void 0===n)throw new Error("BUBBLE_TO_PARENT requires bubbleInteractionToParent to be configured");'
    "return n(e,t,r)}"
)
SUBAGENT_BUBBLE_FALLBACK_PATCHED = (
    "bubbleToParent:(e,t,r)=>{const n=this.options.bubbleInteractionToParent;"
    'if(void 0===n)throw new Error("BUBBLE_TO_PARENT requires bubbleInteractionToParent to be configured");'
    "return Promise.resolve().then(()=>n(e,t,r)).catch(_sand_err=>{"
    '$u.warn(e,"Bubbled subagent interaction failed; auto-rejecting",'
    "{childSessionId:r.childSessionId,parentToolCallId:r.parentToolCallId,"
    "queryCase:t.query.case,error:String(_sand_err?.message??_sand_err)});"
    'return Gu(t,"Parent turn is no longer active; web search / fetch is unavailable to this background subagent")})'
    + SAND_TOPOLOGY_MARKER
    + "}"
)
EXEC_BRIDGE_GET_ORIGINAL = (
    "get:e=>s.resources.get(e),entries:()=>s.resources.entries()"
)
EXEC_BRIDGE_GET_PATCHED = (
    "get:e=>{const _sand_t=s.resources.get(e);if(void 0!==_sand_t)return _sand_t;"
    + SAND_EXEC_BRIDGE_MARKER
    + 'try{const _sand_b=s.baseResources.get(e);if(void 0!==_sand_b)return _sand_b;'
    "}catch(_sand_e){}return void 0},entries:()=>s.resources.entries()"
)
DIRECT_STREAM_HEAD = (
    "function IRe(e){return t=>ERe(this,void 0,void 0,function*(){"
)
DIRECT_STREAM_TAIL = "const r=yield function"


def _direct_stream_model_metadata_object() -> str:
    return (
        'norm=modelId.replace(/(\\d)-(\\d)/g,"$1.$2"),'
        'meta={vendor:norm.includes("grok")?"xai":norm.includes("gemini")?"gemini":'
        'norm.includes("claude")||norm.includes("opus")||norm.includes("sonnet")||'
        'norm.includes("fable")?"anthropic":'
        'norm.includes("gpt")||norm.includes("codex")?"openai":"unknown",'
        'promptVersion:"latest",reasoningEffort:params.get("effort"),'
        'useDsv3Harness:!1,'
        "agentTokenLimit:(()=>{const _sand_m=/^(\\d+)(k|m)$/.exec(ctx);"
        'return null===_sand_m?void 0:Number(_sand_m[1])*("m"===_sand_m[2]?1e6:1e3)})(),'
        'isGrok46ProductPrompt:norm.includes("grok-4.6"),'
        'isGrok45ProductPrompt:norm.includes("grok")&&!norm.includes("grok-4.6"),'
        "isClaude4x:/(?:opus|sonnet|haiku)-4(?:\\.\\d+)?(?:-|$)/.test(norm),"
        'isFable5:norm.includes("fable-5"),'
        'isFable51:norm.includes("fable-5.1")||norm.includes("fable-51"),'
        "isRawTrainingSlug:!1,"
        'isFruitcake:norm.includes("fruitcake"),'
        'isOpus5:norm.includes("opus-5"),'
        'isOpus48:norm.includes("opus-4.8"),'
        'isOpus46:norm.includes("opus-4.6"),'
        'isOpus45:norm.includes("opus-4.5"),'
        'isSonnet45:norm.includes("sonnet-4.5"),'
        "isSonnet4:/sonnet-4(?:-|$)/.test(norm),"
        'isGemini3:norm.includes("gemini-3"),'
        'isGpt56:norm.includes("gpt-5.6"),'
        'isGpt55:norm.includes("gpt-5.5"),'
        'isGpt54:norm.includes("gpt-5.4"),'
        'isGpt53CodexSpark:norm.includes("gpt-5.3-codex-spark"),'
        'isGpt53Codex:norm.includes("gpt-5.3-codex"),'
        'isGpt52Codex:norm.includes("gpt-5.2-codex"),'
        'isGpt52:norm.includes("gpt-5.2")&&!norm.includes("codex"),'
        'isGpt51:norm.includes("gpt-5.1"),'
        "isGpt5:/^gpt-5(?:-(?!codex)[a-z0-9]+)*$/.test(norm),"
        'isCodexFamily:norm.includes("codex"),'
        'isGpt5Family:norm.includes("gpt-5"),'
        'isComposer2:norm.includes("composer-2"),'
        'isComposer15:norm.includes("composer-1.5"),'
        'isComposer1:norm.includes("composer-1"),'
        'isComposerMatterhorn:norm.includes("matterhorn")};'
    )


DIRECT_STREAM_OVERRIDE_FILENAME = "sand-direct-stream-override.json"


def _direct_stream_override_expression() -> str:
    return (
        "wire=(()=>{try{"
        'const _sand_fs=require("fs"),_sand_path=require("path"),'
        '_sand_f=_sand_path.join(__dirname,"' + DIRECT_STREAM_OVERRIDE_FILENAME + '");'
        "if(!_sand_fs.existsSync(_sand_f))return req;"
        'const _sand_o=JSON.parse(_sand_fs.readFileSync(_sand_f,"utf8")),_sand_c=req;'
        'if("string"==typeof _sand_o.modelId)_sand_c.modelId=_sand_o.modelId;'
        'if("boolean"==typeof _sand_o.maxMode)_sand_c.maxMode=_sand_o.maxMode;'
        'if("boolean"==typeof _sand_o.builtInModel)_sand_c.builtInModel=_sand_o.builtInModel;'
        'if("boolean"==typeof _sand_o.isVariantStringRepresentation)'
        "_sand_c.isVariantStringRepresentation=_sand_o.isVariantStringRepresentation;"
        "if(Array.isArray(_sand_o.parameters))_sand_c.parameters=_sand_o.parameters"
        ".map(_sand_q=>({id:String(_sand_q.id),value:String(_sand_q.value)}));"
        'if(!0===_sand_o.variantString){_sand_c.modelId=_sand_c.modelId+"["+'
        '_sand_c.parameters.map(_sand_q=>_sand_q.id+"="+_sand_q.value).join(",")+"]";'
        "_sand_c.isVariantStringRepresentation=!0}"
        "return _sand_c}catch(_sand_e){return req}})(),"
    )


def _direct_stream_injection() -> str:
    return (
        "{"
        + SAND_DIRECT_STREAM_MARKER
        + "const req=t.requestedModel;"
        'if(void 0===req)throw new Error("Sand direct Stream requires requestedModel");'
        'const id=String(req.modelId||""),modelId=id.toLowerCase(),'
        "params=new Map((req.parameters||[]).map(e=>[e.id,e.value])),"
        'ctx=String(params.get("context")||"").toLowerCase(),'
        + _direct_stream_override_expression()
        + "session=new oRe(e,wire,void 0,void 0).getSession(CRe()),"
        "tools={getExecutor:e=>new Ace(session.getExecutor(e))},"
        + _direct_stream_model_metadata_object()
        + "return{promptSession:session,promptToolSession:tools,attempt:{resolvedModel:req,"
        "supportsSelfSummary:!1,routedModelDisplayName:void 0,"
        "resolvedModelMetadata:{promptModelInfo:gRe(meta,id),useDsv3Harness:!1,"
        "agentTokenLimit:meta.agentTokenLimit},finish:()=>Promise.resolve()}}}"
    )


DIRECT_STREAM_ORIGINAL = DIRECT_STREAM_HEAD + DIRECT_STREAM_TAIL
DIRECT_STREAM_PATCHED = DIRECT_STREAM_HEAD + _direct_stream_injection() + DIRECT_STREAM_TAIL


INTEGRITY_HOST_HARNESS = "host_harness"
INTEGRITY_HOST_ROUTER = "host_router"
INTEGRITY_HOST_MAIN = "host_main"
INTEGRITY_LOCAL_LOOP = "local_loop"
INTEGRITY_EXT_HOST = "ext_host"
INTEGRITY_WORKBENCH = "workbench"
APPROVAL_FALLBACK_GROUP = "子代理审批 bubble 兜底"

T_HOST_MAIN = "cursor-agent-host/dist/main.js"
T_HOST_ROUTE = "657.js"
T_ALWAYS_LOCAL = "cursor-always-local/dist/main.js"
T_LOCAL_LOOP = T_HOST_MAIN
T_EXEC = "cursor-agent-exec/dist/main.js"
T_RUNTIME = "cursor-local-agent-runtime/dist/main.js"
T_DESKTOP = "workbench.desktop.main.js"
T_GLASS = "workbench.glass.main.js"
T_EXT_HOST = "extensionHostProcess.js"
HOST_ONLY = (T_HOST_MAIN,)
HOST_ROUTE_ONLY = (T_HOST_ROUTE,)
LOOP_ONLY = (T_LOCAL_LOOP,)
HARNESS_COPIES = (T_HOST_MAIN, T_EXEC, T_RUNTIME)


@dataclass(frozen=True)
class Patch:

    name: str
    original: str
    patched: str
    stats: str
    targets: Tuple[str, ...] = ()
    hits: int = 1
    workbench: bool = False
    guard_marker: Optional[str] = None
    integrity: Optional[str] = None
    integrity_hits: Optional[int] = None
    integrity_text: Optional[str] = None
    group: Optional[str] = None
    group_hits: int = 1
    readiness: bool = True

    def applies_to(self, target_label: Optional[str]) -> bool:
        return target_label is None or not self.targets or target_label in self.targets

    @property
    def integrity_expected(self) -> int:
        return self.hits if self.integrity_hits is None else self.integrity_hits

    @property
    def integrity_anchor(self) -> str:
        return self.patched if self.integrity_text is None else self.integrity_text


def _workbench_pair(
    group: str,
    desktop: Tuple[str, str],
    glass: Tuple[str, str],
    group_hits: int = 1,
) -> Tuple[Patch, Patch]:
    return (
        Patch(f"{group}（desktop）", desktop[0], desktop[1], "topology", targets=(T_DESKTOP,),
              workbench=True, integrity=INTEGRITY_WORKBENCH, group=group, group_hits=group_hits),
        Patch(f"{group}（glass）", glass[0], glass[1], "topology", targets=(T_GLASS,),
              workbench=True, integrity=INTEGRITY_WORKBENCH, group=group, group_hits=group_hits),
    )


def _approval_fallback_patches() -> Tuple[Patch, ...]:
    return tuple(
        Patch(
            f"{APPROVAL_FALLBACK_GROUP}（{'desktop' if index < 5 else 'glass'} {index % 5 + 1}）",
            original, patched, "topology", targets=(T_DESKTOP if index < 5 else T_GLASS,),
            workbench=True, integrity=INTEGRITY_WORKBENCH, group=APPROVAL_FALLBACK_GROUP, group_hits=5,
        )
        for index, (original, patched) in enumerate(APPROVAL_BUBBLE_FALLBACK_PAIRS)
    )


PATCHES: Tuple[Patch, ...] = (
    Patch("子代理档位参数下发", TASK_TIER_PARAMETERS_ORIGINAL, TASK_TIER_PARAMETERS_PATCHED,
          "task_tool", targets=HOST_ONLY, integrity=INTEGRITY_HOST_HARNESS),
    Patch("子代理档位写法提示", TASK_TIER_GUIDANCE_ORIGINAL, TASK_TIER_GUIDANCE_PATCHED,
          "task_tool", targets=HOST_ONLY, integrity=INTEGRITY_HOST_HARNESS),
    Patch("Task 参数回报档位", TASK_ARGS_MODEL_TIER_ORIGINAL, TASK_ARGS_MODEL_TIER_PATCHED,
          "task_tool", targets=HOST_ONLY, integrity=INTEGRITY_HOST_HARNESS),
    Patch("受控委派提示", SUBAGENT_GUIDANCE_ORIGINAL, SUBAGENT_GUIDANCE_PATCHED,
          "task_tool", targets=HARNESS_COPIES, hits=3, integrity=INTEGRITY_HOST_HARNESS, integrity_hits=1),
    Patch("系统提示模型身份声明", LOCAL_MODEL_IDENTITY_STATEMENT_ORIGINAL,
          LOCAL_MODEL_IDENTITY_STATEMENT_PATCHED, "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_HARNESS),
    Patch("系统提示大负载写入护栏", WRITE_PAYLOAD_GUARD_ORIGINAL, WRITE_PAYLOAD_GUARD_PATCHED,
          "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_HARNESS),
    Patch("Write 分段属性 contents_2..8", WRITE_CONTENTS_PARTS_ORIGINAL,
          WRITE_CONTENTS_PARTS_PATCHED, "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_HARNESS),
    Patch("StrReplace 分段属性 new_string_2..4", STR_REPLACE_PARTS_ORIGINAL,
          STR_REPLACE_PARTS_PATCHED, "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_HARNESS),
    Patch("编辑参数流解析器捕获 streamField", STREAM_PARTS_CAPTURE_ORIGINAL,
          STREAM_PARTS_CAPTURE_PATCHED, "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_HARNESS),
    Patch("编辑参数流解析器逐段预览", STREAM_PARTS_PREVIEW_ORIGINAL,
          STREAM_PARTS_PREVIEW_PATCHED, "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_HARNESS),
    Patch("编辑参数流解析器分段拼接", STREAM_PARTS_MERGE_ORIGINAL,
          STREAM_PARTS_MERGE_PATCHED, "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_HARNESS),
    Patch("生图 prompt 词数按 Intl.Segmenter 计（host）", IMAGE_PROMPT_WORD_COUNT_HOST[0],
          IMAGE_PROMPT_WORD_COUNT_HOST[1], "topology", targets=HOST_ONLY,
          integrity=INTEGRITY_HOST_HARNESS),
    Patch("生图 prompt 词数按 Intl.Segmenter 计（runtime）", IMAGE_PROMPT_WORD_COUNT_RUNTIME[0],
          IMAGE_PROMPT_WORD_COUNT_RUNTIME[1], "topology", targets=(T_RUNTIME,)),
    Patch("privacy 未解析不再回退 Connect（Sand 跳过 privacy gate）", PRIVACY_GATE_ORIGINAL,
          PRIVACY_GATE_PATCHED, "privacy_gate", targets=HOST_ONLY, integrity=INTEGRITY_HOST_ROUTER,
          integrity_text=SAND_PRIVACY_GATE_MARKER),
    Patch("全模式/全动作走 AgentService/Run（Composer/Grok/Fable/GPT 等进 Connect）", ACTION_ROUTE_ORIGINAL,
          ACTION_ROUTE_PATCHED, "action_route", targets=HOST_ONLY, integrity=INTEGRITY_HOST_ROUTER),
    Patch("Connect Run 原生请求身份", NATIVE_RUN_IDENTITY_ORIGINAL, NATIVE_RUN_IDENTITY_PATCHED,
          "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_ROUTER),
    Patch("InferenceService/Stream 走 HTTP/2 PING 保活传输（直连）", STREAM_ROUTE_ORIGINAL,
          STREAM_HTTP2_ROUTE_PATCHED, "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_ROUTER),
    Patch("directMeta 子代理本地路由", SUBAGENT_ROUTE_ORIGINAL, SUBAGENT_ROUTE_PATCHED,
          "subagent_route", targets=HOST_ONLY, integrity=INTEGRITY_HOST_ROUTER),
    Patch("turn 运行时日志含模型参数", TURN_RUNTIME_LOG_ORIGINAL, TURN_RUNTIME_LOG_PATCHED,
          "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_ROUTER),
    Patch("根会话交互查询编号", ROOT_INTERACTION_QUERY_ID_ORIGINAL, ROOT_INTERACTION_QUERY_ID_PATCHED,
          "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_ROUTER),
    Patch("子代理上抛交互失败时自动拒绝", SUBAGENT_BUBBLE_FALLBACK_ORIGINAL, SUBAGENT_BUBBLE_FALLBACK_PATCHED,
          "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_ROUTER),
    Patch("子代理模型目录辅助函数", MANAGED_TASK_RUNTIME_ORIGINAL, MANAGED_TASK_RUNTIME_PATCHED,
          "task_tool", targets=LOOP_ONLY, integrity=INTEGRITY_LOCAL_LOOP, integrity_text=SAND_SUBAGENT_MODEL_CATALOG_HELPER),
    Patch("子代理模型目录（sidecar）", MANAGED_TASK_PROPS_ORIGINAL, MANAGED_TASK_PROPS_PATCHED,
          "task_tool", targets=LOOP_ONLY, integrity=INTEGRITY_LOCAL_LOOP),
    Patch("本地循环 featureFlags（含 MCP FileSystem 提示块）", MANAGED_TASK_FLAGS_ORIGINAL,
          MANAGED_TASK_FLAGS_PATCHED, "task_tool", targets=LOOP_ONLY, integrity=INTEGRITY_LOCAL_LOOP),
    Patch("本地循环预压缩阈值", LOCAL_SUMMARIZATION_ORIGINAL, LOCAL_SUMMARIZATION_PATCHED,
          "topology", targets=LOOP_ONLY, integrity=INTEGRITY_LOCAL_LOOP),
    Patch("Stream 上下文窗口按请求 context 档上报", CONTEXT_WINDOW_REPORT_ORIGINAL,
          CONTEXT_WINDOW_REPORT_PATCHED, "topology", targets=LOOP_ONLY, integrity=INTEGRITY_LOCAL_LOOP),
    Patch("Stream unauthenticated 按传输错误重试", LOCAL_UNAUTH_TRANSPORT_ORIGINAL,
          LOCAL_UNAUTH_TRANSPORT_PATCHED, "topology", targets=LOOP_ONLY, integrity=INTEGRITY_LOCAL_LOOP),
    Patch("本地循环消费 steer/queue 动作", LOCAL_CONVERSATION_ACTION_ORIGINAL,
          LOCAL_CONVERSATION_ACTION_PATCHED, "topology", targets=LOOP_ONLY, integrity=INTEGRITY_LOCAL_LOOP),
    Patch("本地循环 Direct Stream（仅残留 managed-local 回退，默认已改走 AgentService/Run）", DIRECT_STREAM_ORIGINAL, DIRECT_STREAM_PATCHED,
          "direct_stream", targets=LOOP_ONLY, guard_marker=SAND_DIRECT_STREAM_MARKER, integrity=INTEGRITY_LOCAL_LOOP),
    Patch("系统提示模型身份", LOCAL_MODEL_IDENTITY_ORIGINAL, LOCAL_MODEL_IDENTITY_PATCHED,
          "topology", targets=LOOP_ONLY, hits=2, integrity=INTEGRITY_LOCAL_LOOP),
    Patch("语义搜索工具启用", SEMANTIC_SEARCH_ORIGINAL, SEMANTIC_SEARCH_PATCHED,
          "topology", targets=LOOP_ONLY, hits=2, integrity=INTEGRITY_LOCAL_LOOP),
    Patch("本地循环 isModelBlocked/isModelValid",
          "isModelBlocked:e=>e!==t,isModelValid:e=>e===t",
          "isModelBlocked:()=>!1,isModelValid:()=>!0" + SAND_TASK_TOOL_MARKER, "task_tool", targets=LOOP_ONLY),
    Patch("本地循环 forceModelId",
          'forceModelId:t,subagentModelForcePolicy:"parent_pin"',
          'forceModelId:void 0,subagentModelForcePolicy:"none"' + SAND_TASK_TOOL_MARKER, "task_tool", targets=LOOP_ONLY),
    Patch("本地循环 子代理继承父档位", TASK_PARENT_PARAMETERS_ORIGINAL, TASK_PARENT_PARAMETERS_PATCHED,
          "task_tool", targets=LOOP_ONLY),
    *_approval_fallback_patches(),
    *_workbench_pair("用量横幅不再改写会话模型", USAGE_LIMIT_AUTO_SWITCH_DESKTOP, USAGE_LIMIT_AUTO_SWITCH_GLASS),
    *_workbench_pair("用量横幅不再显示", USAGE_LIMIT_BANNER_DESKTOP, USAGE_LIMIT_BANNER_GLASS),
    *_workbench_pair("Task 卡片模型标签含子会话参数", TASK_CARD_MODEL_LABEL_DESKTOP, TASK_CARD_MODEL_LABEL_GLASS),
    *_workbench_pair("模型推荐默认关闭", MODEL_NUDGE_DEFAULT_DESKTOP, MODEL_NUDGE_DEFAULT_GLASS),
    *_workbench_pair("完成后 Task 卡片模型配置回退", TASK_CARD_MODEL_CONFIG_DESKTOP, TASK_CARD_MODEL_CONFIG_GLASS),
    *_workbench_pair("子会话镜像解析变体字串", CHILD_MODEL_CONFIG_DESKTOP, CHILD_MODEL_CONFIG_GLASS),
    *_workbench_pair("请求 maxMode 按选中变体推导", REQUEST_MAX_MODE_DESKTOP, REQUEST_MAX_MODE_GLASS),
    *_workbench_pair("出错 AskQuestion 步骤结算", ASK_QUESTION_ERROR_STEP_DESKTOP, ASK_QUESTION_ERROR_STEP_GLASS),
    *_workbench_pair("出错 AskQuestion 视图模型结算", ASK_QUESTION_ERROR_VM_DESKTOP, ASK_QUESTION_ERROR_VM_GLASS),
    *_workbench_pair("User / Team Rules 注入请求上下文", USER_RULES_INJECT_DESKTOP, USER_RULES_INJECT_GLASS),
    Patch(
        SIMULATED_THINKING_TIMEOUT_GROUP,
        SIMULATED_THINKING_TIMEOUT_ORIGINAL,
        SIMULATED_THINKING_TIMEOUT_PATCHED,
        "topology",
        targets=(T_DESKTOP, T_GLASS),
        hits=2,
        workbench=True,
        integrity=INTEGRITY_WORKBENCH,
        group=SIMULATED_THINKING_TIMEOUT_GROUP,
        group_hits=1,
    ),
    Patch("move_exec ON", MOVE_EXEC_GATE_ORIGINAL, MOVE_EXEC_GATE_PATCHED, "move_exec", targets=HOST_ONLY,
          guard_marker=SAND_MOVE_EXEC_MARKER, integrity=INTEGRITY_HOST_MAIN),
    Patch("agent-host-exec 终端 hint", HOST_TERMINAL_HINT_ORIGINAL, HOST_TERMINAL_HINT_PATCHED,
          "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_MAIN),
    Patch("Connect 请求上下文剔除 User/Team 规则", CONNECT_REQUEST_CONTEXT_RULES_ORIGINAL,
          CONNECT_REQUEST_CONTEXT_RULES_PATCHED, "topology", targets=HOST_ONLY, integrity=INTEGRITY_HOST_MAIN),
    Patch("子代理并发上限 10", SUBAGENT_LIMIT_CAP_ORIGINAL, SUBAGENT_LIMIT_CAP_PATCHED,
          "task_tool", targets=HOST_ONLY, integrity=INTEGRITY_HOST_MAIN),
    Patch("host 默认推理改走 AgentService/Run（connect / sand-agent-run）", MANAGED_LOCAL_ROUTE_ORIGINAL, MANAGED_LOCAL_ROUTE_PATCHED,
          "managed_local_route", targets=HOST_ONLY),
    Patch("cursor-agent-host/dist/main.js 本地运行时加载", LOCAL_RUNTIME_LOAD_ORIGINAL,
          LOCAL_RUNTIME_LOAD_PATCHED, "local_runtime_load", targets=HOST_ONLY),
    Patch("cursor-agent-host/dist/main.js Agent Host 身份", AGENT_HOST_IDENTITY_ORIGINAL,
          AGENT_HOST_IDENTITY_PATCHED, "agent_host_identity", targets=HOST_ONLY),
    Patch("cursor-agent-host/dist/main.js 原生云子代理开关", NATIVE_CLOUD_SUBAGENT_ORIGINAL,
          NATIVE_CLOUD_SUBAGENT_PATCHED, "native_cloud_subagent", targets=HOST_ONLY, guard_marker=SAND_NATIVE_CLOUD_SUBAGENT_MARKER),
    Patch("cursor-agent-host/dist/main.js 子代理交互开关", SUBAGENT_INTERACTIONS_ORIGINAL,
          SUBAGENT_INTERACTIONS_PATCHED, "subagent_interactions", targets=HOST_ONLY, guard_marker=SAND_SUBAGENT_INTERACTIONS_MARKER),
    Patch("托管技能 GetManagedSkills 原生请求身份", MANAGED_SKILLS_IDENTITY_ORIGINAL,
          MANAGED_SKILLS_IDENTITY_PATCHED, "topology", targets=(T_EXT_HOST,), workbench=True,
          integrity=INTEGRITY_EXT_HOST, readiness=False),
    Patch("cursor-agent-exec/dist/main.js 资源桥", EXEC_BRIDGE_GET_ORIGINAL, EXEC_BRIDGE_GET_PATCHED,
          "exec_bridge", targets=(T_EXEC,), guard_marker=SAND_EXEC_BRIDGE_MARKER),
    Patch("host/exec/runtime 并行委派提示", SUBAGENT_PARALLEL_GUIDANCE_ORIGINAL,
          SUBAGENT_PARALLEL_GUIDANCE_PATCHED, "task_tool", targets=HARNESS_COPIES, hits=3),
    Patch("cursor-agent-exec/dist/main.js 生图 prompt 词数", IMAGE_PROMPT_WORD_COUNT_EXEC[0],
          IMAGE_PROMPT_WORD_COUNT_EXEC[1], "topology", targets=(T_EXEC,)),
)

NESTED_TASK_GUARD_SITES = 3


def _patch_hits(stats: str, *, server: bool = False) -> int:
    return sum(
        p.hits
        for p in PATCHES
        if p.stats == stats and not (server and p.workbench)
    )


def expected_marker_count(stats: str, layout_is_server: bool = False) -> int:
    total = _patch_hits(stats, server=layout_is_server)
    if stats == "task_tool":
        total += NESTED_TASK_GUARD_SITES
    return total


EXPECTED_TASK_TOOL_MARKERS = expected_marker_count("task_tool")

DESKTOP_MARKER_EXPECTATIONS = MarkerExpectations(
    task_tool=EXPECTED_TASK_TOOL_MARKERS,
    agent_host_enablement=2,
    topology=expected_marker_count("topology"),
)
SERVER_MARKER_EXPECTATIONS = MarkerExpectations(
    task_tool=expected_marker_count("task_tool", layout_is_server=True),
    agent_host_enablement=0,
    topology=expected_marker_count("topology", layout_is_server=True),
    glass_client_identity=1,
    model_nudge_silent_switch=0,
)


def integrity_anchor_rows(bundle: str) -> Tuple[Tuple[str, str, int], ...]:
    return tuple(
        (p.name, p.integrity_anchor, p.integrity_expected)
        for p in PATCHES
        if p.integrity == bundle
    )


def workbench_integrity_groups() -> Tuple[Tuple[str, Tuple[str, ...], int], ...]:
    groups: Dict[str, Tuple[List[str], int]] = {}
    for p in PATCHES:
        if p.integrity != INTEGRITY_WORKBENCH or p.group is None:
            continue
        forms, _ = groups.setdefault(p.group, ([], p.group_hits))
        forms.append(p.patched)
    return tuple((group, tuple(forms), hits) for group, (forms, hits) in groups.items())


def _enable_windows_ansi() -> bool:
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):
            handle = kernel32.GetStdHandle(handle_id)
            if handle in (0, -1):
                continue
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False


def _configure_console() -> None:
    global _COLOR_ENABLED
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    if os.environ.get("NO_COLOR"):
        _COLOR_ENABLED = False
        return
    _COLOR_ENABLED = _enable_windows_ansi() and bool(
        getattr(sys.stdout, "isatty", lambda: False)()
    )


def colorize(text: str, *codes: str) -> str:
    if not _COLOR_ENABLED or not codes:
        return text
    return "".join(codes) + text + ANSI_RESET


def print_error(text: str) -> None:
    _finish_progress()
    print(colorize(text, ANSI_RED), file=sys.stderr, flush=True)


def print_step(text: str) -> None:
    _finish_progress()
    print(colorize(text, ANSI_BLUE), flush=True)


def print_ok(text: str) -> None:
    _finish_progress()
    print(colorize(text, ANSI_GREEN), flush=True)


def print_warn(text: str) -> None:
    _finish_progress()
    print(colorize(text, ANSI_YELLOW), flush=True)


def _finish_progress() -> None:
    global _PROGRESS_ACTIVE
    if not _PROGRESS_ACTIVE:
        return
    print(flush=True)
    _PROGRESS_ACTIVE = False


def print_progress(index: int, total: int, action: str) -> None:
    global _PROGRESS_ACTIVE
    total = max(int(total), 1)
    index = min(max(int(index), 0), total)
    width = 16
    filled = int(round(width * index / total))
    bar = "#" * filled + "-" * (width - filled)
    text = f"  {action}  [{index}/{total}]  [{bar}]"
    stdout = sys.stdout
    if stdout is not None and stdout.isatty():
        print("\r" + text.ljust(48), end="", flush=True)
        _PROGRESS_ACTIVE = True
        if index >= total:
            _finish_progress()
        return
    if total <= 10:
        if index != total:
            return
    else:
        step = max(total // 10, 1)
        if index != total and index % step != 0:
            return
    print(text, flush=True)


def _prompt(message: str) -> str:
    try:
        return input(colorize(message, ANSI_BLUE))
    except EOFError:
        print()
        return ""


def _pause_before_exit() -> None:
    if not _PAUSE_ON_EXIT:
        return
    if not _interactive_tty():
        return
    try:
        input(colorize("按 Enter 关闭窗口...", ANSI_BLUE))
    except EOFError:
        pass


class _LogTee:

    def __init__(self, stream: TextIO, log: TextIO) -> None:
        self._stream = stream
        self._log = log
        self.encoding = getattr(stream, "encoding", None) or "utf-8"
        self.errors = getattr(stream, "errors", None) or "replace"

    def write(self, data: str) -> int:
        if not data:
            return 0
        try:
            self._log.write(data)
            self._log.flush()
        except OSError:
            pass
        try:
            self._stream.write(data)
            self._stream.flush()
        except OSError:
            pass
        return len(data)

    def flush(self) -> None:
        try:
            self._log.flush()
        except OSError:
            pass
        try:
            self._stream.flush()
        except OSError:
            pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._stream.fileno()

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


def _install_cancel_file(path: str) -> None:
    global _CANCEL_FILE
    _CANCEL_FILE = Path(path)


def _cancel_requested() -> bool:
    path = _CANCEL_FILE
    if path is None:
        return False
    try:
        return path.is_file()
    except OSError:
        return False


def _check_cancelled() -> None:
    if _cancel_requested():
        raise SandCancelled("用户取消")


def _install_log_file(path: str) -> None:
    global _LOG_PATH, _LOG_FP, _ORIG_STDOUT
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8", buffering=1, newline="\n")
    _ORIG_STDOUT = sys.stdout
    _LOG_PATH = log_path
    _LOG_FP = handle
    sys.stdout = _LogTee(sys.stdout, handle)
    sys.stderr = _LogTee(sys.stderr, handle)


def _interactive_tty() -> bool:
    if _LOG_PATH is not None:
        return False
    stdin = sys.stdin
    return stdin is not None and stdin.isatty()


def _log_file_offset() -> int:
    if _LOG_FP is not None:
        try:
            _LOG_FP.flush()
        except OSError:
            pass
    if _LOG_PATH is None:
        return 0
    try:
        return _LOG_PATH.stat().st_size
    except OSError:
        return 0


def _relay_log_tail(offset: int) -> int:
    if _LOG_PATH is None or _ORIG_STDOUT is None:
        return offset
    try:
        with open(_LOG_PATH, "r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            data = handle.read()
            new_offset = handle.tell()
    except OSError:
        return offset
    if data:
        _ORIG_STDOUT.write(data)
        _ORIG_STDOUT.flush()
    return new_offset


def _elevated_relaunch_args(
    action: str, relaunch_args: Optional[Sequence[str]] = None
) -> List[str]:
    if relaunch_args is not None:
        args = [item for item in relaunch_args if item != "--pause"]
    else:
        args = [item for item in sys.argv[1:] if item != "--pause"]
    if not args:
        args = [action]
    if sys.platform == "win32" and "--log-file" not in args:
        args = ["--pause", *args]
    return args


def _install_relaunch_args() -> List[str]:
    args: List[str] = []
    if _LOG_PATH is not None:
        args += ["--log-file", str(_LOG_PATH.resolve())]
    if _CANCEL_FILE is not None:
        args += ["--cancel-file", str(_CANCEL_FILE.resolve())]
    return [*args, "install"]


def _normalize_user_path(value: str) -> str:
    text = value.strip()
    if text.startswith("& "):
        text = text[2:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return text.strip().strip('"').strip("'")


def _is_elevated() -> bool:
    if sys.platform == "win32":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def _can_write_install(layout: CursorLayout) -> bool:
    probe = layout.app_root / f".sand-write-probe-{os.getpid()}-{time.time_ns()}"
    try:
        fd = os.open(str(probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        return True
    except PermissionError:
        return False
    except OSError:
        return True
    finally:
        try:
            if probe.exists():
                probe.unlink()
        except OSError:
            pass


def _windows_quote_arg(arg: str) -> str:
    if not arg:
        return '""'
    if not re.search(r'[\s"]', arg):
        return arg
    result: List[str] = ['"']
    backslashes = 0
    for char in arg:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            result.append("\\" * (backslashes * 2 + 1))
            result.append('"')
            backslashes = 0
            continue
        result.append("\\" * backslashes)
        result.append(char)
        backslashes = 0
    result.append("\\" * (backslashes * 2))
    result.append('"')
    return "".join(result)


class _ShellExecuteInfoW(ctypes.Structure):
    _fields_ = (
        ("cbSize", ctypes.c_uint32),
        ("fMask", ctypes.c_uint32),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int32),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_uint32),
        ("hIcon", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    )


def _windows_relaunch_elevated(args: Sequence[str], *, hide: bool = False) -> int:
    script = Path(__file__).resolve()
    info = _ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(_ShellExecuteInfoW)
    info.fMask = 0x00000040 | (0x00008000 if hide else 0)
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = " ".join(
        _windows_quote_arg(item) for item in (str(script), *args)
    )
    info.lpDirectory = str(script.parent)
    info.nShow = 0 if hide else 1
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfoW)]
    shell32.ShellExecuteExW.restype = ctypes.c_bool
    print(colorize("需要管理员权限，请在弹出的窗口中确认。", ANSI_YELLOW))
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == 1223:
            raise SandToolError("已取消管理员授权")
        raise SandToolError(f"无法请求管理员权限（错误码 {err}）")
    kernel32 = ctypes.windll.kernel32
    if not info.hProcess:
        return 0
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    offset = _log_file_offset()
    wait_timeout = 258
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.TerminateProcess.restype = ctypes.c_bool
    while True:
        try:
            _check_cancelled()
        except SandCancelled:
            kernel32.TerminateProcess(info.hProcess, 130)
            kernel32.CloseHandle(info.hProcess)
            raise
        waited = kernel32.WaitForSingleObject(info.hProcess, 200)
        offset = _relay_log_tail(offset)
        if waited != wait_timeout:
            _relay_log_tail(offset)
            break
    exit_code = ctypes.c_uint32()
    kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code))
    kernel32.CloseHandle(info.hProcess)
    return int(exit_code.value)


def _posix_relaunch_sudo(args: Sequence[str]) -> int:
    script = str(Path(__file__).resolve())
    print(colorize("需要管理员权限，请输入密码。", ANSI_YELLOW))
    os.execvp("sudo", ["sudo", "-p", "密码: ", sys.executable, script, *args])
    return 1


def _ensure_writable(
    layout: CursorLayout,
    action: str,
    relaunch_args: Optional[Sequence[str]] = None,
) -> None:
    if _can_write_install(layout):
        return
    if os.environ.get("SAND_NO_ELEVATE") == "1" or _is_elevated():
        raise PermissionError(str(layout.app_root))
    args = _elevated_relaunch_args(action, relaunch_args)
    hide = "--log-file" in args
    raise SystemExit(
        _windows_relaunch_elevated(args, hide=hide)
        if sys.platform == "win32"
        else _posix_relaunch_sudo(args)
    )


def _config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "SandClientMode" / "sand-client-cli"
        return Path.home() / "AppData" / "Local" / "SandClientMode" / "sand-client-cli"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "SandClientMode"
            / "sand-client-cli"
        )
    return Path.home() / ".config" / "SandClientMode" / "sand-client-cli"


def _config_path() -> Path:
    return _config_dir() / "config.json"


def _json_loads_lenient(data: Union[str, bytes]) -> object:
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data).decode("utf-8-sig", "replace")
    else:
        text = data[1:] if data[:1] == "\ufeff" else data
    return json.loads(text)


def _confirm_sand_account() -> None:
    if not _interactive_tty():
        return
    print_warn(
        "默认推理走 AgentService/Run（HTTP/2 双工）+ 当前登录 accessToken + "
        "x-cursor-client-type: ide。服务端会先发 requestContextArgs，客户端必须回 "
        "requestContextResult。请先确认已在 Cursor 登录目标账号。"
    )
    answer = _normalize_user_path(_prompt("确认继续？回车确认，输入 n 取消> "))
    if answer.casefold() in {"n", "no", "q"}:
        raise SandToolError("已取消：请先在 Cursor 中登录目标账号后重试")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _path_key(path: Path) -> str:
    normalized = str(path.resolve())
    return os.path.normcase(normalized)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _product_checksum(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return base64.b64encode(digest).decode("ascii").rstrip("=")


_BUNDLE_CACHE: Dict[Path, Tuple[Tuple[int, int], bytes, Optional[str]]] = {}
_BUNDLE_CACHE_RACY_NS = 2_000_000_000


def _bundle_bytes(path: Path) -> bytes:
    file_stat = path.stat()
    key = (file_stat.st_mtime_ns, file_stat.st_size)
    cached = _BUNDLE_CACHE.get(path)
    recently_written = time.time_ns() - file_stat.st_mtime_ns < _BUNDLE_CACHE_RACY_NS
    if cached is not None and cached[0] == key and not recently_written:
        return cached[1]
    data = path.read_bytes()
    _BUNDLE_CACHE[path] = (key, data, None)
    return data


def _bundle_text(path: Path) -> str:
    data = _bundle_bytes(path)
    cached = _BUNDLE_CACHE[path]
    if cached[2] is not None:
        return cached[2]
    text = _decode_js(data, path)
    _BUNDLE_CACHE[path] = (cached[0], data, text)
    return text


def _atomic_write(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    _BUNDLE_CACHE.pop(path, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / (
        f".{path.name}.sand-client-{os.getpid()}-{time.time_ns()}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd: Optional[int] = None
    try:
        fd = os.open(str(temp), flags, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp, stat.S_IMODE(mode))
        try:
            os.replace(temp, path)
        except PermissionError:
            original_mode: Optional[int] = None
            if path.exists():
                original_mode = stat.S_IMODE(path.stat().st_mode)
                os.chmod(path, original_mode | stat.S_IWRITE)
            try:
                os.replace(temp, path)
            except BaseException:
                if original_mode is not None and path.exists():
                    try:
                        os.chmod(path, original_mode)
                    except OSError:
                        pass
                raise
        if mode is not None:
            os.chmod(path, stat.S_IMODE(mode))
    finally:
        if fd is not None:
            os.close(fd)
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write(path, data, 0o600)


def _load_config() -> Mapping[str, object]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SandToolError(
            f"配置文件损坏：{path}\n请运行 set-path auto 后重新检测"
        ) from exc
    if not isinstance(value, dict) or value.get("version") != CONFIG_VERSION:
        raise SandToolError(
            f"不支持的配置文件：{path}\n请运行 set-path auto 后重新检测"
        )
    return value


def _read_product(product_path: Path) -> Mapping[str, object]:
    try:
        size = product_path.stat().st_size
        if size <= 0 or size > 1024 * 1024:
            raise SandToolError(f"product.json 大小异常：{product_path}")
        raw = product_path.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
    except SandToolError:
        raise
    except Exception as exc:
        raise SandToolError(f"无法读取 Cursor product.json：{product_path}") from exc
    if not isinstance(value, dict):
        raise SandToolError(f"Cursor product.json 格式错误：{product_path}")
    name = str(value.get("applicationName") or value.get("nameShort") or "")
    if name.casefold() != "cursor":
        raise SandToolError(f"所选目录不是 Cursor 安装：{product_path}")
    return value


def _find_app_bundle(path: Path) -> Optional[Path]:
    for item in (path, *path.parents):
        if item.name.casefold() == "cursor.app":
            return item
    return None


def _app_path(root: Path, rel: str) -> Path:
    return root.joinpath(*rel.split("/"))


def _candidate_app_roots(raw_path: Path) -> Iterable[Path]:
    path = raw_path.parent if raw_path.is_file() else raw_path
    current = path
    for _ in range(8):
        yield current
        yield current / "resources" / "app"
        yield current / "Resources" / "app"
        yield current / "Contents" / "Resources" / "app"
        if current.parent == current:
            break
        current = current.parent


def _detect_layout_kind(app_root: Path) -> str:
    server_main = _app_path(app_root, SERVER_MAIN_REL)
    desktop_main = app_root / "out" / "main.js"
    if server_main.is_file() and not desktop_main.is_file():
        return LAYOUT_KIND_SERVER
    return LAYOUT_KIND_DESKTOP


def _resolve_executable(app_root: Path, kind: str) -> Tuple[Path, Path]:
    if kind == LAYOUT_KIND_SERVER:
        install_root = app_root
        candidates = tuple(_app_path(app_root, rel) for rel in SERVER_LAUNCHER_RELS)
    elif sys.platform == "win32":
        if app_root.parent.name.casefold() == "resources":
            install_root = app_root.parent.parent
        else:
            install_root = app_root
        candidates = (
            install_root / "Cursor.exe",
            install_root / "cursor.exe",
        )
    elif sys.platform == "darwin":
        bundle = _find_app_bundle(app_root)
        if bundle is None:
            raise SandToolError("macOS Cursor 路径必须位于 Cursor.app 内")
        install_root = bundle
        candidates = (bundle / "Contents" / "MacOS" / "Cursor",)
    else:
        raise SandToolError(
            "桌面版 Cursor 仅支持 Windows 和 macOS；"
            "Linux 上只支持 Remote-SSH 的 cursor-server 布局"
            "（~/.cursor-server/bin/<platform>/<commit>）"
        )

    for executable in candidates:
        try:
            resolved = executable.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        if resolved.is_file() and _is_within(resolved, install_root.resolve()):
            return install_root.resolve(), resolved
    raise SandToolError(f"未找到 Cursor 可执行文件：{install_root}")


def layout_from_path(value: Union[str, Path]) -> CursorLayout:
    raw_text = _normalize_user_path(str(value))
    if not raw_text:
        raise SandToolError("Cursor 路径不能为空")
    if sys.platform == "win32" and (
        raw_text.startswith("\\\\") or raw_text.startswith("\\\\?\\")
    ):
        raise SandToolError("不支持 UNC 或 Windows 设备路径")

    raw = Path(raw_text).expanduser()
    if not raw.is_absolute():
        raise SandToolError(f"Cursor 路径必须是绝对路径：{raw}")
    try:
        raw = raw.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise SandToolError(f"Cursor 路径不存在：{raw}") from exc

    seen: Set[str] = set()
    last_error: Optional[Exception] = None
    for candidate in _candidate_app_roots(raw):
        try:
            app_root = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        key = _path_key(app_root)
        if key in seen:
            continue
        seen.add(key)

        product_json = app_root / "product.json"
        if not product_json.is_file():
            continue
        try:
            product_real = product_json.resolve(strict=True)
            if not _is_within(product_real, app_root):
                raise SandToolError("product.json 符号链接逃逸出 Cursor app 目录")
            product = _read_product(product_real)
            kind = _detect_layout_kind(app_root)
            install_root, executable = _resolve_executable(app_root, kind)

            targets: List[Path] = []
            for rel, _extension_name in TARGET_SPECS:
                target = _app_path(app_root, rel)
                if not target.is_file():
                    continue
                target_real = target.resolve(strict=True)
                if not _is_within(target_real, app_root):
                    raise SandToolError(f"目标文件符号链接逃逸：{target}")
                targets.append(target_real)
            if not targets:
                raise SandToolError(
                    "Cursor 使用 app.asar 或当前版本没有可识别的 Sand 目标文件"
                )

            ext_host = _app_path(app_root, EXT_HOST_REL)
            ext_host_real = ext_host.resolve(strict=True) if ext_host.is_file() else None
            version = str(product.get("version") or product.get("commit") or "未知")
            return CursorLayout(
                install_root=install_root,
                app_root=app_root,
                product_json=product_real,
                executable=executable,
                target_paths=tuple(targets),
                ext_host_path=ext_host_real,
                version=version,
                kind=kind,
            )
        except SandToolError as exc:
            last_error = exc
            continue

    if last_error:
        raise SandToolError(f"Cursor 路径校验失败：{last_error}") from last_error
    raise SandToolError(f"路径中未找到 Cursor resources/app：{raw}")


def _powershell_executable() -> Optional[str]:
    return shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")


def _windows_running_candidates_native() -> Optional[List[str]]:
    try:
        import ctypes
        from ctypes import wintypes

        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError, ImportError):
        return None
    process_query_limited_information = 0x1000
    capacity = 4096
    while True:
        pid_array = (wintypes.DWORD * capacity)()
        needed = wintypes.DWORD(0)
        if not psapi.EnumProcesses(pid_array, ctypes.sizeof(pid_array), ctypes.byref(needed)):
            return None
        if needed.value < ctypes.sizeof(pid_array):
            break
        capacity *= 2
    pids = pid_array[: needed.value // ctypes.sizeof(wintypes.DWORD)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    found: List[str] = []
    seen: Set[str] = set()
    for pid in pids:
        if not pid:
            continue
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            continue
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                continue
        finally:
            kernel32.CloseHandle(handle)
        image = buffer.value
        if Path(image).name.casefold() != "cursor.exe":
            continue
        key = image.casefold()
        if key not in seen:
            seen.add(key)
            found.append(image)
    return found


def _windows_running_candidates() -> List[str]:
    native = _windows_running_candidates_native()
    if native is not None:
        return native
    powershell = _powershell_executable()
    if not powershell:
        return []
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();"
        "Get-CimInstance Win32_Process -Filter \"Name='Cursor.exe'\" | "
        "ForEach-Object { if ($_.ExecutablePath) { $_.ExecutablePath } }"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _windows_registry_candidates() -> List[str]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []

    def read_value(key: "winreg.HKEYType", name: str) -> str:
        try:
            return str(winreg.QueryValueEx(key, name)[0] or "")
        except OSError:
            return ""

    candidates: List[str] = []
    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for root in roots:
        for view in views:
            try:
                parent = winreg.OpenKey(root, uninstall, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with parent:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(parent, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        child = winreg.OpenKey(parent, name)
                    except OSError:
                        continue
                    with child:
                        display_name = read_value(child, "DisplayName").strip()
                        publisher = read_value(child, "Publisher").strip()
                        if display_name.casefold() != "cursor" and "anysphere" not in publisher.casefold():
                            continue
                        install_location = read_value(child, "InstallLocation").strip().strip('"')
                        display_icon = read_value(child, "DisplayIcon").strip().strip('"')
                        if install_location:
                            candidates.append(install_location)
                        if display_icon:
                            icon_path = re.sub(r",\s*-?\d+$", "", display_icon).strip('"')
                            candidates.append(icon_path)
    return candidates


def _mac_process_paths(strict: bool = False) -> List[Tuple[int, Path]]:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        proc_pidpath = libproc.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
        result = subprocess.run(
            ["ps", "-axo", "pid="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if strict:
            raise SandToolError("无法读取 macOS 进程可执行路径") from exc
        return []
    if result.returncode != 0:
        if strict:
            raise SandToolError("无法读取 macOS 进程可执行路径")
        return []
    values: List[Tuple[int, Path]] = []
    for line in result.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        buffer = ctypes.create_string_buffer(4096)
        length = proc_pidpath(pid, buffer, len(buffer))
        if length <= 0:
            continue
        try:
            executable = Path(os.fsdecode(buffer.value)).resolve(strict=False)
        except (OSError, ValueError):
            continue
        values.append((pid, executable))
    return values


def _mac_running_candidates() -> List[str]:
    values: Dict[str, str] = {}
    for _pid, executable in _mac_process_paths():
        bundle = _find_app_bundle(executable)
        if bundle is not None:
            values.setdefault(_path_key(bundle), str(bundle))
    return list(values.values())


def _mac_spotlight_candidates() -> List[str]:
    mdfind = shutil.which("mdfind")
    if not mdfind:
        return []
    try:
        result = subprocess.run(
            [
                mdfind,
                "kMDItemCFBundleIdentifier == 'com.todesktop.230313mzl4w4u92'",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _default_candidate_groups() -> Iterable[Tuple[str, Sequence[str]]]:
    env_candidate = os.environ.get("SAND_CURSOR_INSTALL_DIR", "").strip()
    if env_candidate:
        yield "环境变量 SAND_CURSOR_INSTALL_DIR", (env_candidate,)

    if sys.platform == "win32":
        yield "运行中的 Cursor", _windows_running_candidates()
        yield "Windows 安装登记", _windows_registry_candidates()
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
        defaults = [
            str(Path(local) / "Programs" / "Cursor") if local else "",
            str(Path(local) / "Programs" / "cursor") if local else "",
            str(Path(local) / "Cursor") if local else "",
            str(Path(program_files) / "Cursor"),
            str(Path(program_files_x86) / "Cursor") if program_files_x86 else "",
        ]
        yield "Windows 默认目录", tuple(x for x in defaults if x)
    elif sys.platform == "darwin":
        yield "运行中的 Cursor", _mac_running_candidates()
        yield "macOS Spotlight", _mac_spotlight_candidates()
        yield "macOS 默认目录", (
            "/Applications/Cursor.app",
            str(Path.home() / "Applications" / "Cursor.app"),
        )

    server_candidates = _cursor_server_candidates()
    if server_candidates:
        yield "Remote-SSH cursor-server", server_candidates

    path_cursor = shutil.which("cursor")
    if path_cursor:
        yield "PATH", (path_cursor,)


def _cursor_server_candidates() -> Tuple[str, ...]:
    root = Path.home() / ".cursor-server" / "bin"
    if not root.is_dir():
        return ()
    found: List[str] = []
    try:
        for platform_dir in sorted(root.iterdir()):
            if not platform_dir.is_dir():
                continue
            for commit_dir in sorted(platform_dir.iterdir()):
                if (commit_dir / "product.json").is_file():
                    found.append(str(commit_dir))
    except OSError:
        return tuple(found)
    return tuple(found)


def _valid_layouts(values: Sequence[str]) -> List[CursorLayout]:
    layouts: Dict[str, CursorLayout] = {}
    for value in values:
        if not value:
            continue
        try:
            layout = layout_from_path(value)
        except SandToolError:
            continue
        layouts.setdefault(_path_key(layout.app_root), layout)
    return list(layouts.values())


def _discover_all_layouts() -> List[CursorLayout]:
    found: Dict[str, CursorLayout] = {}
    for _source, values in _default_candidate_groups():
        for layout in _valid_layouts(tuple(values)):
            found.setdefault(_path_key(layout.install_root), layout)
    return sorted(found.values(), key=lambda item: str(item.install_root).casefold())


def _prompt_choose_layout(layouts: Sequence[CursorLayout]) -> Optional[CursorLayout]:
    print(colorize("检测到多个 Cursor 安装：", ANSI_YELLOW))
    for index, layout in enumerate(layouts, 1):
        print(f"  {index}) Cursor {layout.version}  {layout.install_root}")
    print("  0) 手动输入路径")
    choice = _normalize_user_path(_prompt("请选择> "))
    if not choice:
        return None
    if choice == "0":
        value = _normalize_user_path(_prompt("路径> "))
        return layout_from_path(value) if value else None
    if choice.isdigit():
        index = int(choice)
        if 1 <= index <= len(layouts):
            return layouts[index - 1]
    try:
        return layout_from_path(choice)
    except SandToolError:
        print_error("无效选择。")
        return None


def resolve_cursor_layout(*, interactive: Optional[bool] = None) -> CursorLayout:
    if interactive is None:
        interactive = _INTERACTIVE
    configured = _load_config().get("cursorInstallRoot")
    if isinstance(configured, str) and configured.strip():
        try:
            return layout_from_path(configured)
        except SandToolError as exc:
            if not interactive:
                raise SandToolError(
                    f"已设置的 Cursor 路径失效：{configured}\n"
                    "请运行 set-path <新路径>，或运行 set-path auto 恢复自动检测"
                ) from exc
            print_error(f"已设置的 Cursor 路径失效：{configured}")

    if interactive:
        layouts = _discover_all_layouts()
        if len(layouts) == 1:
            return layouts[0]
        if len(layouts) > 1:
            chosen = _prompt_choose_layout(layouts)
            if chosen is None:
                raise SandToolError("未选择 Cursor 路径")
            save_cursor_path(str(chosen.install_root))
            print(colorize(f"已记住路径：{chosen.install_root}", ANSI_GREEN))
            return chosen
        value = _normalize_user_path(
            _prompt("未检测到 Cursor，请拖入 Cursor.exe / Cursor.app 或输入路径> ")
        )
        if not value:
            raise SandToolError("未设置 Cursor 路径")
        layout = layout_from_path(value)
        save_cursor_path(str(layout.install_root))
        return layout

    for source, values in _default_candidate_groups():
        layouts = _valid_layouts(tuple(values))
        if len(layouts) == 1:
            return layouts[0]
        if len(layouts) > 1:
            options = "\n".join(f"  - {item.install_root}" for item in layouts)
            raise SandToolError(
                f"{source}检测到多个 Cursor 安装，请先在菜单中选择 3 设置路径：\n{options}"
            )
    raise SandToolError(
        "未检测到 Cursor 安装，请在菜单中选择 3 设置 Cursor 路径"
        "（可把 Cursor.exe / Cursor.app 拖进窗口）"
    )


def save_cursor_path(value: str) -> None:
    if value.strip().casefold() == "auto":
        _write_json_atomic(
            _config_path(),
            {
                "version": CONFIG_VERSION,
                "cursorInstallRoot": "",
            },
        )
        return

    layout = layout_from_path(value)
    _write_json_atomic(
        _config_path(),
        {
            "version": CONFIG_VERSION,
            "cursorInstallRoot": str(layout.install_root),
        },
    )


def _harden_subagent_task_filter(match: re.Match[str]) -> str:
    filtered = match.group("filtered")
    config = match.group("config")
    tools = match.group("tools")
    source_item = match.group("source_item")
    context = match.group("context")
    mode = match.group("mode")
    final_item = match.group("final_item")
    platform_check = match.group("platform_check")
    return (
        f"const {filtered}={config}.preserveTaskTool?{tools}:{tools}.filter({source_item}=>"
        f'"TASK"!=={source_item}.toolIdentifier),'
        f"_sand_keep=!0==={config}.preserveTaskTool;"
        + SAND_TASK_TOOL_MARKER
        + f"return({config}.toolsOverride?"
        f"{config}.toolsOverride({filtered},{context},{mode}):{filtered}).filter("
        f'{final_item}=>(_sand_keep||"TASK"!=={final_item}.toolIdentifier)&&'
        f'"ASK_QUESTION"!=={final_item}.toolIdentifier&&'
        f'!("PLATFORM_ACTION"==={final_item}.toolIdentifier&&'
        f"{platform_check}({final_item}.name)))"
    )


def _replace_counted(content: str, original: str, patched: str) -> Tuple[str, int]:
    count = content.count(original)
    if count:
        content = content.replace(original, patched)
    return content, count


def apply_patch_to_content(
    content: str,
    target_label: Optional[str] = None,
) -> Tuple[str, PatchStats]:
    stats = PatchStats()
    next_content = content

    def replace_client(match: re.Match[str]) -> str:
        current = match.group(3)
        marker = (
            SAND_CLIENT_EXISTING_MARKER
            if current == "sand"
            else SAND_CLIENT_MARKER
        )
        return (
            match.group(1)
            + match.group(2)
            + "sand"
            + match.group(2)
            + marker
        )

    for rule in CLIENT_RULES:
        next_content = rule.sub(replace_client, next_content)

    for rule in GLASS_CLIENT_IDENTITY_RULES:
        next_content = rule.sub(
            lambda match: match.group(1)
            + match.group(2)
            + "sand"
            + match.group(2)
            + SAND_CLIENT_GLASS_MARKER,
            next_content,
        )

    for prefix in MODEL_NUDGE_SILENT_SWITCH_PREFIXES:
        if prefix in next_content:
            next_content = next_content.replace(prefix, _silent_switch_patched(prefix))

    for patch in PATCHES:
        if not patch.applies_to(target_label):
            continue
        if patch.guard_marker is not None and patch.guard_marker in next_content:
            continue
        next_content, count = _replace_counted(next_content, patch.original, patch.patched)
        stats.add(patch.stats, count)

    next_content, nested_task_guard_count = SUBAGENT_TASK_FILTER_GENERIC_RE.subn(
        _harden_subagent_task_filter,
        next_content,
    )
    stats.task_tool += nested_task_guard_count
    stats.nested_task_guard += nested_task_guard_count

    if SAND_AGENT_HOST_ENABLEMENT_MARKER not in next_content:
        def enable_agent_host(match: re.Match[str]) -> str:
            variable = match.group(2)
            return (
                variable
                + "=!0;"
                + SAND_AGENT_HOST_ENABLEMENT_MARKER
                + match.group(1)
                + variable
                + match.group(3)
            )

        next_content, agent_host_count = AGENT_HOST_ENABLEMENT_RE.subn(
            enable_agent_host,
            next_content,
            count=1,
        )
        stats.agent_host_enablement += agent_host_count
    return next_content, stats


def _decode_js(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SandToolError(f"目标文件不是 UTF-8，拒绝修改：{path}") from exc


def _read_planned_file(path: Path) -> PlannedFile:
    original = _bundle_bytes(path)
    return PlannedFile(
        original=original,
        next_bytes=original,
        mode=stat.S_IMODE(path.stat().st_mode),
    )


def _extension_hash_pattern(extension_id: str) -> re.Pattern[str]:
    return re.compile(
        rf'(\"{re.escape(extension_id)}\"\s*:\s*\{{[\s\S]{{0,2400}}?'
        rf'\"main\.js\"\s*:\s*\")([0-9a-f]{{64}})(\")'
    )


def _update_extension_hashes(
    layout: CursorLayout,
    plan: Dict[Path, PlannedFile],
) -> None:
    if layout.ext_host_path is None:
        return
    ext_path = layout.ext_host_path
    extension_bytes: List[Tuple[str, bytes]] = []
    for rel, extension_name in TARGET_SPECS:
        if not extension_name:
            continue
        target = _app_path(layout.app_root, rel)
        if not target.is_file():
            continue
        target = target.resolve()
        planned = plan.get(target)
        extension_bytes.append(
            (
                extension_name,
                planned.next_bytes if planned is not None else _bundle_bytes(target),
            )
        )
    if not extension_bytes:
        return

    existing = plan.get(ext_path) or _read_planned_file(ext_path)
    next_content = _decode_js(existing.next_bytes, ext_path)
    original_content = _decode_js(existing.original, ext_path)

    for extension_name, next_main in extension_bytes:
        extension_id = "anysphere." + extension_name
        if f'"{extension_id}"' not in next_content:
            continue
        digest = hashlib.sha256(next_main).hexdigest()
        pattern = _extension_hash_pattern(extension_id)
        if len(pattern.findall(next_content)) > 1:
            raise SandToolError(f"{extension_id} 的内嵌 main.js 哈希不唯一")
        next_content = pattern.sub(
            lambda match: match.group(1) + digest + match.group(3),
            next_content,
            count=1,
        )

    if next_content == original_content:
        plan.pop(ext_path, None)
        return
    plan[ext_path] = PlannedFile(
        original=existing.original,
        next_bytes=next_content.encode("utf-8"),
        mode=existing.mode,
    )


def _checksum_target(out_root: Path, key: str) -> Path:
    parts = [part for part in re.split(r"[\\/]", key) if part]
    return out_root.joinpath(*parts).resolve()


def _sync_product_checksums(
    layout: CursorLayout,
    plan: Dict[Path, PlannedFile],
) -> None:
    product_file = _read_planned_file(layout.product_json)
    has_bom = product_file.original.startswith(b"\xef\xbb\xbf")
    try:
        product = json.loads(product_file.original.decode("utf-8-sig"))
    except Exception as exc:
        raise SandToolError("product.json 无法解析，拒绝提交补丁") from exc
    if not isinstance(product, dict):
        raise SandToolError("product.json 顶层必须是对象")
    checksums = product.get("checksums")
    if not isinstance(checksums, dict):
        return

    out_root = (layout.app_root / "out").resolve()
    changed = False
    for key in list(checksums.keys()):
        if not isinstance(key, str):
            continue
        target = _checksum_target(out_root, key)
        if not _is_within(target, out_root):
            raise SandToolError(f"product.json checksum 路径逃逸：{key}")
        planned = plan.get(target)
        if planned is not None:
            data = planned.next_bytes
        elif target.is_file():
            data = target.read_bytes()
        else:
            continue
        digest = _product_checksum(data)
        if checksums.get(key) != digest:
            checksums[key] = digest
            changed = True

    if not changed:
        return
    text = json.dumps(product, ensure_ascii=False, indent="\t")
    next_bytes = text.encode("utf-8")
    if has_bom:
        next_bytes = b"\xef\xbb\xbf" + next_bytes
    plan[layout.product_json] = PlannedFile(
        original=product_file.original,
        next_bytes=next_bytes,
        mode=product_file.mode,
    )


def _extension_hash_issues(layout: CursorLayout) -> List[str]:
    if layout.ext_host_path is None or not layout.ext_host_path.is_file():
        return []
    ext_content = _bundle_text(layout.ext_host_path)
    issues: List[str] = []
    for rel, extension_name in TARGET_SPECS:
        if not extension_name:
            continue
        main_path = _app_path(layout.app_root, rel)
        if not main_path.is_file():
            continue
        extension_id = "anysphere." + extension_name
        if f'"{extension_id}"' not in ext_content:
            continue
        match = _extension_hash_pattern(extension_id).search(ext_content)
        if not match:
            continue
        expected = hashlib.sha256(_bundle_bytes(main_path)).hexdigest()
        if match.group(2) != expected:
            issues.append(
                f"extensionHostProcess.js 内嵌 {extension_id} 哈希与磁盘 main.js 不一致"
            )
    return issues


def _verify_extension_hashes(layout: CursorLayout) -> None:
    issues = _extension_hash_issues(layout)
    if issues:
        raise SandToolError("；".join(issues))


def _verify_product_checksums(layout: CursorLayout) -> None:
    product = json.loads(layout.product_json.read_bytes().decode("utf-8-sig"))
    checksums = product.get("checksums") if isinstance(product, dict) else None
    if not isinstance(checksums, dict):
        return
    out_root = (layout.app_root / "out").resolve()
    for key, written in checksums.items():
        if not isinstance(key, str):
            continue
        target = _checksum_target(out_root, key)
        if not _is_within(target, out_root) or not target.is_file():
            continue
        if written != _product_checksum(_bundle_bytes(target)):
            raise SandToolError(f"product.json 完整性哈希校验失败：{key}")


def _agent_host_dist(layout: CursorLayout) -> Path:
    return layout.app_root / "extensions" / "cursor-agent-host" / "dist"


def _anchor_issues(content: str, rows: Iterable[Tuple[str, str, int]]) -> List[str]:
    issues: List[str] = []
    for name, anchor, expected in rows:
        count = content.count(anchor)
        if count != expected:
            issues.append(f"{name}={count}（期望 {expected}）")
    return issues


def static_integrity_issues(layout: CursorLayout) -> List[str]:
    dist = _agent_host_dist(layout)
    target_host_main = dist / "main.js"
    if not target_host_main.is_file():
        return ["缺少 cursor-agent-host/dist/main.js"]
    content_host_main = _bundle_text(target_host_main)
    issues: List[str] = []
    issues.extend(_anchor_issues(content_host_main, integrity_anchor_rows(INTEGRITY_HOST_HARNESS)))
    issues.extend(_anchor_issues(content_host_main, integrity_anchor_rows(INTEGRITY_HOST_ROUTER)))
    issues.extend(_anchor_issues(content_host_main, integrity_anchor_rows(INTEGRITY_LOCAL_LOOP)))
    direct_stream_count = content_host_main.count(SAND_DIRECT_STREAM_MARKER)
    if direct_stream_count != 1:
        issues.append(f"direct-stream={direct_stream_count}（期望 1）")
    workbench_groups = workbench_integrity_groups()
    for workbench_target in layout.target_paths:
        if workbench_target.name not in (
            "workbench.desktop.main.js",
            "workbench.glass.main.js",
        ):
            continue
        if not workbench_target.is_file():
            continue
        workbench_content = _bundle_text(workbench_target)
        for group, forms, expected in workbench_groups:
            if group != APPROVAL_FALLBACK_GROUP:
                continue
            count = sum(workbench_content.count(form) for form in forms)
            if count != expected:
                issues.append(f"{workbench_target.name} {group}={count}（期望 {expected}）")
        silent_switch_count = workbench_content.count(SAND_ELIGIBILITY_MARKER)
        if silent_switch_count != 1:
            issues.append(
                f"{workbench_target.name} 模型推荐 silentSwitch 拦截={silent_switch_count}（期望 1）"
            )
        for group, forms, expected in workbench_groups:
            if group == APPROVAL_FALLBACK_GROUP:
                continue
            count = sum(workbench_content.count(form) for form in forms)
            if count != expected:
                issues.append(f"{workbench_target.name} {group}={count}（期望 {expected}）")
    issues.extend(_anchor_issues(content_host_main, integrity_anchor_rows(INTEGRITY_HOST_MAIN)))
    if not layout.is_server:
        if layout.ext_host_path is None or not layout.ext_host_path.is_file():
            issues.append(f"缺少 {EXT_HOST_REL}")
        else:
            ext_host_content = _bundle_text(layout.ext_host_path)
            issues.extend(_anchor_issues(ext_host_content, integrity_anchor_rows(INTEGRITY_EXT_HOST)))
    guarded_targets = (
        ("cursor-agent-host/dist/main.js", target_host_main),
        (
            "cursor-agent-exec/dist/main.js",
            layout.app_root / "extensions" / "cursor-agent-exec" / "dist" / "main.js",
        ),
        (
            "cursor-local-agent-runtime/dist/main.js",
            layout.app_root
            / "extensions"
            / "cursor-local-agent-runtime"
            / "dist"
            / "main.js",
        ),
    )
    for label, guarded_target in guarded_targets:
        if not guarded_target.is_file():
            issues.append(f"缺少 {label}")
            continue
        guarded_content = _bundle_text(guarded_target)
        guard_count = len(SUBAGENT_TASK_FILTER_HARDENED_RE.findall(guarded_content))
        if guard_count != 1:
            issues.append(f"{label} 子代理递归保护={guard_count}（期望 1）")
    issues.extend(_extension_hash_issues(layout))
    return issues


def inspect_status(layout: CursorLayout) -> PatchStatus:
    return _inspect_contents(
        ((target, _bundle_text(target)) for target in layout.target_paths),
        layout.expectations,
    )


def _inspect_planned_status(
    layout: CursorLayout,
    plan: Mapping[Path, PlannedFile],
) -> PatchStatus:
    return _inspect_contents(
        (
            (
                target,
                plan[target].next_bytes if target in plan else _bundle_text(target),
            )
            for target in layout.target_paths
        ),
        layout.expectations,
    )


@dataclass(frozen=True)
class MarkerSpec:

    status_field: str
    payload_key: str
    marker: Optional[str]
    stats: Optional[str] = None
    expectation: Optional[str] = None


MARKER_SPECS: Tuple[MarkerSpec, ...] = (
    MarkerSpec("client_markers", "client", None),
    MarkerSpec("glass_client_markers", "clientGlassIdentity", SAND_CLIENT_GLASS_MARKER,
               expectation="glass_client_identity"),
    MarkerSpec("eligibility_markers", "eligibility", SAND_ELIGIBILITY_MARKER,
               expectation="model_nudge_silent_switch"),
    MarkerSpec("managed_local_route_markers", "managedLocalRoute", SAND_MANAGED_LOCAL_ROUTE_MARKER,
               stats="managed_local_route"),
    MarkerSpec("action_route_markers", "actionRoute", SAND_ACTION_ROUTE_MARKER, stats="action_route"),
    MarkerSpec("privacy_gate_markers", "privacyGate", SAND_PRIVACY_GATE_MARKER, stats="privacy_gate"),
    MarkerSpec("local_runtime_load_markers", "localRuntimeLoad", SAND_LOCAL_RUNTIME_LOAD_MARKER,
               stats="local_runtime_load"),
    MarkerSpec("direct_stream_markers", "directStream", SAND_DIRECT_STREAM_MARKER, stats="direct_stream"),
    MarkerSpec("agent_host_enablement_markers", "agentHostEnablement", SAND_AGENT_HOST_ENABLEMENT_MARKER,
               expectation="agent_host_enablement"),
    MarkerSpec("agent_host_identity_markers", "agentHostIdentity", SAND_AGENT_HOST_IDENTITY_MARKER,
               stats="agent_host_identity"),
    MarkerSpec("exec_bridge_markers", "execBridge", SAND_EXEC_BRIDGE_MARKER, stats="exec_bridge"),
    MarkerSpec("move_exec_markers", "moveExec", SAND_MOVE_EXEC_MARKER, stats="move_exec"),
    MarkerSpec("native_cloud_subagent_markers", "nativeCloudSubagent", SAND_NATIVE_CLOUD_SUBAGENT_MARKER,
               stats="native_cloud_subagent"),
    MarkerSpec("subagent_interactions_markers", "subagentInteractions", SAND_SUBAGENT_INTERACTIONS_MARKER,
               stats="subagent_interactions"),
    MarkerSpec("task_tool_markers", "taskTool", SAND_TASK_TOOL_MARKER, expectation="task_tool"),
    MarkerSpec("subagent_route_markers", "subagentRoute", SAND_SUBAGENT_ROUTE_MARKER, stats="subagent_route"),
    MarkerSpec("nested_task_guard_markers", "nestedTaskGuard", None, expectation="nested_task_guard"),
    MarkerSpec("topology_markers", "topology", SAND_TOPOLOGY_MARKER, expectation="topology"),
)

_MARKER_COUNT_FIELDS: Tuple[Tuple[str, str], ...] = tuple(
    (spec.status_field, spec.marker) for spec in MARKER_SPECS if spec.marker is not None
)


def expected_marker_counts(layout: CursorLayout) -> Dict[str, int]:
    expectations = layout.expectations
    counts: Dict[str, int] = {}
    for spec in MARKER_SPECS:
        if spec.expectation is not None:
            counts[spec.payload_key] = getattr(expectations, spec.expectation)
        elif spec.stats is not None:
            counts[spec.payload_key] = expected_marker_count(spec.stats, layout.is_server)
    return counts


def _target_label(target: Path) -> str:
    if target.name != "main.js":
        return target.name
    parts = target.parts
    if len(parts) >= 3 and parts[-2] == "dist":
        return f"{parts[-3]}/dist/main.js"
    return "out/main.js"


def _format_foreign_markers(status: PatchStatus) -> str:
    parts = [
        f"{marker} ×{count}（{_marker_owner(marker)}，{'、'.join(files)}）"
        for marker, count, files in status.foreign_markers
    ]
    return "；".join(parts) if parts else f"{status.external_marker_count} 处未知标记"


def _foreign_marker_block_message(status: PatchStatus, action: str) -> str:
    return (
        "检测到其他 Sand 模式标记："
        + _format_foreign_markers(status)
        + f"。本脚本不会接管或覆盖它，请先卸载产生这些标记的补丁后再{action}"
    )


def _bare_header_notice(count: int, *, installing: bool) -> str:
    head = (
        f"检测到 {count} 处无变量前缀的 x-cursor-client-type 请求头"
        f"（{SUPPORTED_CURSOR_VERSION} 原版为 变量??\"ide\"）"
    )
    if installing:
        return head + "。安装会改成 sand；卸载后仍是裸 ide 字面量，无法还原 ?? 写法。"
    return head + "。这些站点没有本工具标记，卸载不会改写。"


def _inspect_contents(
    sources: Iterable[Tuple[Path, Union[bytes, str]]],
    expectations: MarkerExpectations,
) -> PatchStatus:
    counts: Dict[str, int] = {field: 0 for field, _marker in _MARKER_COUNT_FIELDS}
    counts.update(
        client_markers=0,
        nested_task_guard_markers=0,
        external_marker_count=0,
        ide_matches=0,
        glass_matches=0,
    )
    foreign_counts: Dict[str, int] = {}
    foreign_files: Dict[str, Set[str]] = {}
    bare_header_sites = 0
    for target, raw in sources:
        content = raw if isinstance(raw, str) else _decode_js(raw, target)
        marker_hits = ANY_SAND_MARKER_RE.findall(content)
        marker_counts: Dict[str, int] = {}
        for marker in marker_hits:
            marker_counts[marker] = marker_counts.get(marker, 0) + 1
        for field, marker in _MARKER_COUNT_FIELDS:
            counts[field] += marker_counts.get(marker, 0)
        client_count = marker_counts.get(SAND_CLIENT_MARKER, 0) + marker_counts.get(
            SAND_CLIENT_EXISTING_MARKER, 0
        )
        counts["client_markers"] += client_count
        counts["nested_task_guard_markers"] += len(
            SUBAGENT_TASK_FILTER_HARDENED_RE.findall(content)
        )
        counts["ide_matches"] += sum(
            1
            for rule in CLIENT_RULES
            for match in rule.finditer(content)
            if match.group(3) == "ide"
        )
        counts["glass_matches"] += sum(
            len(rule.findall(content)) for rule in GLASS_CLIENT_IDENTITY_RULES
        )
        bare_header_sites += len(BARE_HEADER_SITE_RE.findall(content))
        label = _target_label(target)
        for marker, marker_count in marker_counts.items():
            if marker in KNOWN_SAND_MARKERS:
                continue
            foreign_counts[marker] = foreign_counts.get(marker, 0) + marker_count
            foreign_files.setdefault(marker, set()).add(label)
    foreign_markers = tuple(
        (marker, count, tuple(sorted(foreign_files[marker])))
        for marker, count in sorted(foreign_counts.items())
    )
    counts["external_marker_count"] = sum(
        count for _marker, count, _files in foreign_markers
    )
    return PatchStatus(
        expectations=expectations,
        foreign_markers=foreign_markers,
        bare_header_sites=bare_header_sites,
        **counts,
    )


def _create_backup(
    layout: CursorLayout,
    plan: Mapping[Path, PlannedFile],
    operation: str,
) -> Tuple[Path, Dict[str, object]]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = _backup_root(layout) / f"{stamp}-{operation}"
    files_dir = backup_dir / "files"
    entries: List[Dict[str, object]] = []
    items = list(plan.items())
    total = len(items)
    print_step(f"正在备份 {total} 个文件...")
    for index, (path, planned) in enumerate(items, 1):
        try:
            relative = path.resolve().relative_to(layout.app_root.resolve())
        except ValueError as exc:
            raise SandToolError(f"计划文件逃逸出 Cursor app：{path}") from exc
        print_progress(index, total, "备份")
        backup_file = files_dir / relative
        _atomic_write(backup_file, planned.original, planned.mode)
        entries.append(
            {
                "path": relative.as_posix(),
                "originalSha256": _sha256(planned.original),
                "nextSha256": _sha256(planned.next_bytes),
                "mode": planned.mode,
            }
        )
    manifest: Dict[str, object] = {
        "version": 1,
        "toolVersion": SUPPORTED_CURSOR_VERSION,
        "operation": operation,
        "status": "prepared",
        "appRoot": str(layout.app_root),
        "cursorVersion": layout.version,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "files": entries,
    }
    _write_json_atomic(backup_dir / "manifest.json", manifest)
    return backup_dir, manifest


def _update_backup_manifest(
    backup_dir: Path,
    manifest: Dict[str, object],
    status_value: str,
    error: Optional[str] = None,
) -> None:
    manifest["status"] = status_value
    manifest["finishedAt"] = datetime.now(timezone.utc).isoformat()
    if error:
        manifest["error"] = error[:1000]
    _write_json_atomic(backup_dir / "manifest.json", manifest)


def _backup_root(layout: CursorLayout) -> Path:
    app_hash = hashlib.sha256(str(layout.app_root).encode("utf-8")).hexdigest()[:16]
    return _config_dir() / "backups" / app_hash


PRISTINE_MANIFEST_VERSION = 1
PRISTINE_DIR_NAME = "pristine"


def _pristine_root(layout: CursorLayout) -> Path:
    return _backup_root(layout) / PRISTINE_DIR_NAME / layout.version


def _target_relative(layout: CursorLayout, path: Path) -> Path:
    try:
        return path.resolve().relative_to(layout.app_root.resolve())
    except ValueError as exc:
        raise SandToolError(f"目标文件逃逸出 Cursor app：{path}") from exc


def _looks_patched(content: str) -> bool:
    return "/*SAND_" in content or "C_SAND_" in content


def _load_pristine(layout: CursorLayout) -> Optional[Dict[Path, bytes]]:
    root = _pristine_root(layout)
    manifest = _read_backup_manifest(root)
    if manifest is None:
        return None
    manifest_files = manifest["files"]
    recorded = {
        str(entry.get("path")): str(entry.get("sha256"))
        for entry in (manifest_files if isinstance(manifest_files, list) else [])
        if isinstance(entry, dict)
    }
    files: Dict[Path, bytes] = {}
    for target in layout.target_paths:
        relative = _target_relative(layout, target).as_posix()
        source = root / "files" / relative
        if relative not in recorded or not source.is_file():
            return None
        data = source.read_bytes()
        if _sha256(data) != recorded[relative]:
            return None
        files[target] = data
    return files


def _record_pristine(
    layout: CursorLayout,
    files: Mapping[Path, bytes],
    source: str,
) -> None:
    root = _pristine_root(layout)
    entries: List[Dict[str, object]] = []
    for target in layout.target_paths:
        relative = _target_relative(layout, target)
        mode = stat.S_IMODE(target.stat().st_mode)
        _atomic_write(root / "files" / relative, files[target], mode)
        entries.append({"path": relative.as_posix(), "sha256": _sha256(files[target])})
    _write_json_atomic(
        root / "manifest.json",
        {
            "version": PRISTINE_MANIFEST_VERSION,
            "toolVersion": SUPPORTED_CURSOR_VERSION,
            "cursorVersion": layout.version,
            "appRoot": str(layout.app_root),
            "source": source,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "files": entries,
        },
    )


def _pristine_from_backups(
    layout: CursorLayout,
    missing: Sequence[Path],
) -> Dict[Path, bytes]:
    found: Dict[Path, bytes] = {}
    root = _backup_root(layout)
    if not root.is_dir():
        return found
    for backup_dir in sorted(child for child in root.iterdir() if child.is_dir()):
        manifest = _read_backup_manifest(backup_dir)
        if manifest is None or manifest.get("cursorVersion") != layout.version:
            continue
        for target in missing:
            if target in found:
                continue
            candidate = backup_dir / "files" / _target_relative(layout, target)
            if not candidate.is_file():
                continue
            data = candidate.read_bytes()
            if not _looks_patched(_decode_js(data, candidate)):
                found[target] = data
        if len(found) == len(missing):
            break
    return found


def _ensure_pristine(layout: CursorLayout) -> Dict[Path, bytes]:
    pristine = _load_pristine(layout)
    if pristine is not None:
        return pristine
    print_step("正在记录原始文件...")
    files: Dict[Path, bytes] = {}
    for target in layout.target_paths:
        if not _looks_patched(_bundle_text(target)):
            files[target] = _bundle_bytes(target)
    missing = [target for target in layout.target_paths if target not in files]
    source = "current-files"
    if missing:
        files.update(_pristine_from_backups(layout, missing))
        source = "current-files+install-backups"
    still_missing = [target for target in layout.target_paths if target not in files]
    if still_missing:
        raise SandToolError(
            "以下文件已被修改且找不到原始副本，无法建立原始文件库：\n  "
            + "\n  ".join(_target_label(target) for target in still_missing)
            + "\n请用 Cursor 安装包重新覆盖安装同版本后再执行 install"
        )
    _record_pristine(layout, files, source)
    print_ok(f"原始文件库已记录：{_pristine_root(layout)}")
    return files


def _read_backup_manifest(backup_dir: Path) -> Optional[Mapping[str, object]]:
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        return None
    return value


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


@dataclass(frozen=True)
class BackupEntry:
    directory: Path
    manifest: Mapping[str, object]

    @property
    def name(self) -> str:
        return self.directory.name

    @property
    def operation(self) -> str:
        return str(self.manifest.get("operation") or "unknown")

    @property
    def status(self) -> str:
        return str(self.manifest.get("status") or "unknown")

    @property
    def tool_version(self) -> str:
        return str(self.manifest.get("toolVersion") or "unknown")

    @property
    def file_count(self) -> int:
        files = self.manifest.get("files")
        return len(files) if isinstance(files, list) else 0


def list_backups(layout: CursorLayout) -> List[BackupEntry]:
    root = _backup_root(layout)
    if not root.is_dir():
        return []
    entries: List[BackupEntry] = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        manifest = _read_backup_manifest(directory)
        if manifest is not None:
            entries.append(BackupEntry(directory=directory, manifest=manifest))
    return sorted(entries, key=lambda item: item.name, reverse=True)


def prune_backups(layout: CursorLayout, keep: int) -> Tuple[int, int]:
    if keep < 0:
        raise SandToolError("保留数量不能为负数")
    entries = list_backups(layout)
    root = _backup_root(layout)
    stale = (
        [
            directory
            for directory in root.iterdir()
            if directory.is_dir()
            and directory.name != PRISTINE_DIR_NAME
            and _read_backup_manifest(directory) is None
        ]
        if root.is_dir()
        else []
    )
    removable = [entry.directory for entry in entries[keep:]] + stale
    removed = 0
    freed = 0
    for directory in removable:
        size = _directory_size(directory)
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            print_error(f"无法删除备份 {directory.name}：{exc}")
            continue
        removed += 1
        freed += size
    return removed, freed


def _restore_from_backup(layout: CursorLayout, entry: BackupEntry) -> int:
    files = entry.manifest.get("files")
    if not isinstance(files, list) or not files:
        raise SandToolError(f"备份没有可恢复的文件记录：{entry.name}")
    app_root = layout.app_root.resolve()
    plan: Dict[Path, PlannedFile] = {}
    print_step(f"正在校验备份 {entry.name} ...")
    for record in files:
        if not isinstance(record, dict):
            raise SandToolError(f"备份记录格式错误：{entry.name}")
        relative = str(record.get("path") or "")
        expected = str(record.get("originalSha256") or "")
        if not relative or not expected:
            raise SandToolError(f"备份记录缺少路径或哈希：{entry.name}")
        parts = [part for part in relative.split("/") if part]
        target = app_root.joinpath(*parts)
        if not _is_within(target.resolve() if target.exists() else target, app_root):
            raise SandToolError(f"备份路径逃逸出 Cursor app：{relative}")
        source = entry.directory / "files" / Path(*parts)
        if not source.is_file():
            raise SandToolError(f"备份缺少文件：{relative}")
        data = source.read_bytes()
        if _sha256(data) != expected:
            raise SandToolError(f"备份文件哈希不匹配，拒绝恢复：{relative}")
        if not target.is_file():
            raise SandToolError(f"目标文件不存在，无法恢复：{target}")
        mode = record.get("mode")
        plan[target] = PlannedFile(
            original=target.read_bytes(),
            next_bytes=data,
            mode=int(mode) if isinstance(mode, int) else stat.S_IMODE(target.stat().st_mode),
        )
    unchanged = [path for path, planned in plan.items() if planned.original == planned.next_bytes]
    if len(unchanged) == len(plan):
        print_ok("所有文件已与备份一致，无需恢复。")
        return 0

    print_ok(f"计划恢复 {len(plan)} 个文件")
    _prepare_write(layout, "restore")

    def validate() -> None:
        _verify_product_checksums(layout)

    _commit_plan(layout, plan, "restore", validate)
    print_ok("恢复完成，" + _relaunch_hint(layout))
    return 0


def _commit_plan(
    layout: CursorLayout,
    plan: Mapping[Path, PlannedFile],
    operation: str,
    validator: Callable[[], None],
) -> None:
    if not plan:
        raise SandToolError("内部错误：提交计划为空")
    items = list(plan.items())
    total = len(items)
    print_step("正在确认文件未被改动...")
    for path, planned in items:
        if _sha256(path.read_bytes()) != _sha256(planned.original):
            raise SandToolError(f"文件在计划生成后发生变化，已停止操作：{path}")
    backup_dir, manifest = _create_backup(layout, plan, operation)
    attempted: List[Path] = []
    try:
        print_step(f"正在写入 {total} 个文件...")
        for index, (path, planned) in enumerate(items, 1):
            if _sha256(path.read_bytes()) != _sha256(planned.original):
                raise SandToolError(f"文件在写入前发生变化，已停止操作：{path}")
            print_progress(index, total, "写入")
            attempted.append(path)
            _atomic_write(path, planned.next_bytes, planned.mode)
        print_step("正在校验结果...")
        validator()
        for path, planned in items:
            if _sha256(path.read_bytes()) != _sha256(planned.next_bytes):
                raise SandToolError(f"写入后哈希校验失败：{path}")
        _update_backup_manifest(backup_dir, manifest, "committed")
        print_ok("写入校验通过")
        removed, freed = prune_backups(layout, DEFAULT_BACKUP_KEEP)
        if removed:
            print_step(
                f"已清理 {removed} 组旧备份，释放 {freed / 1048576:.1f} MB"
            )
    except (Exception, KeyboardInterrupt) as exc:
        rollback_errors: List[str] = []
        if attempted:
            print_error("操作失败，正在回滚已写入文件...")
        for index, path in enumerate(reversed(attempted), 1):
            planned = plan[path]
            print_progress(index, len(attempted), "回滚")
            try:
                current_hash = _sha256(path.read_bytes())
                original_hash = _sha256(planned.original)
                next_hash = _sha256(planned.next_bytes)
                if current_hash == original_hash:
                    continue
                if current_hash != next_hash:
                    rollback_errors.append(f"{path}: 文件已被外部修改，未覆盖")
                    continue
                _atomic_write(path, planned.original, planned.mode)
            except Exception as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        message = str(exc)
        if rollback_errors:
            message += "; rollback errors: " + " | ".join(rollback_errors)
        try:
            _update_backup_manifest(backup_dir, manifest, "rolled_back", message)
        except Exception:
            pass
        if rollback_errors:
            raise SandToolError(
                "补丁失败且有文件未能自动回滚，请保留备份目录："
                f"{backup_dir}\n{message}"
            ) from exc
        raise


_WINDOWS_CLOSE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$target = [System.IO.Path]::GetFullPath($env:SAND_CURSOR_EXE)

$haveWin = $true
try {
Add-Type -ErrorAction Stop @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class SandWin {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  const uint WM_CLOSE = 0x0010;
  static string ClassOf(IntPtr h){ var s = new StringBuilder(256); GetClassName(h, s, s.Capacity); return s.ToString(); }
  // Returns {visible app windows found, WM_CLOSE posts that succeeded}.
  // When the target is higher integrity (Cursor as admin, script not elevated), UIPI fails PostMessage.
  public static int[] CloseAppWindows(uint[] pids, bool post){
    var set = new HashSet<uint>(pids); int found = 0, posted = 0;
    EnumWindows((h, l) => {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (set.Contains(pid) && IsWindowVisible(h) && ClassOf(h) == "Chrome_WidgetWin_1") {
        found++;
        if (post && PostMessage(h, WM_CLOSE, IntPtr.Zero, IntPtr.Zero)) posted++;
      }
      return true;
    }, IntPtr.Zero);
    return new int[]{ found, posted };
  }
  public static int DialogCount(uint[] pids){
    var set = new HashSet<uint>(pids); int n = 0;
    EnumWindows((h, l) => {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (set.Contains(pid) && IsWindowVisible(h)) {
        var c = ClassOf(h);
        if (c == "#32770" || c.StartsWith("TaskDialog")) n++;
      }
      return true;
    }, IntPtr.Zero);
    return n;
  }
}
"@
} catch { $haveWin = $false }

$snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop |
  Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine)

function Get-Targets {
  @($snapshot | Where-Object {
    $_.Name -eq 'Cursor.exe' -and $_.ExecutablePath -and [string]::Equals(
      [System.IO.Path]::GetFullPath($_.ExecutablePath), $target,
      [System.StringComparison]::OrdinalIgnoreCase)
  })
}
function Get-LiveTargets {
  @(Get-CimInstance Win32_Process -Filter "Name='Cursor.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and [string]::Equals(
      [System.IO.Path]::GetFullPath($_.ExecutablePath), $target,
      [System.StringComparison]::OrdinalIgnoreCase)
  })
}
function PidArray { param($procs) ,@($procs | ForEach-Object { [uint32]$_.ProcessId }) }

$before = @(Get-Targets)
if ($before.Count -eq 0) { Write-Output "CLOSED=0"; exit 0 }

# Ancestor chain of this process: skip it during force-kill, or running from
# Cursor's integrated terminal would kill this script.
$byPid = @{}; foreach ($p in $snapshot) { $byPid[[uint32]$p.ProcessId] = $p }
$protected = @{}
$walk = [uint32]$PID
while ($walk -ne 0 -and -not $protected.ContainsKey($walk)) {
  $protected[$walk] = $true
  $row = $byPid[$walk]
  if ($null -eq $row) { break }
  $walk = [uint32]$row.ParentProcessId
}

$pidSet = @{}; foreach ($p in $before) { $pidSet[[uint32]$p.ProcessId] = $true }
foreach ($p in $before) {
  if ($protected.ContainsKey([uint32]$p.ProcessId)) { exit 4 }
}

$mainPids = @($before | Where-Object { -not $pidSet.ContainsKey([uint32]$_.ParentProcessId) } |
  ForEach-Object { [uint32]$_.ProcessId })
if ($mainPids.Count -eq 0) { $mainPids = @(PidArray $before) }

# Graceful phase. When $graceful is $false (no window received WM_CLOSE: no
# windows / UIPI denied / no C# compiler and no main window) skip the 12s
# wait and go straight to force-kill.
$graceful = $false
if ($haveWin) {
  $r = [SandWin]::CloseAppWindows([uint32[]](PidArray $before), $true)
  $graceful = $r[1] -gt 0
  Write-Output ("WINDOWS=" + $r[0] + " POSTED=" + $r[1])
} else {
  # Without a C# compiler, fall back to CloseMainWindow per main process
  # (each main process closes only one window).
  foreach ($mp in $mainPids) {
    try {
      $proc = Get-Process -Id $mp -ErrorAction Stop
      if ($proc.MainWindowHandle -ne 0 -and $proc.CloseMainWindow()) { $graceful = $true }
    } catch {}
  }
  Write-Output "WINDOWS=? POSTED=fallback"
}

$phase = 'none'
if ($graceful) {
  $graceDeadline = [DateTime]::UtcNow.AddSeconds(12)
  $rendererGoneAt = $null
  $phase = 'timeout'
  while ([DateTime]::UtcNow -lt $graceDeadline) {
    $cur = @(Get-LiveTargets)
    if ($cur.Count -eq 0) { Write-Output "PHASE=graceful"; Write-Output ("CLOSED=" + $before.Count); exit 0 }
    $curPids = [uint32[]](PidArray $cur)
    if ($haveWin -and [SandWin]::DialogCount($curPids) -gt 0) { $phase = 'dialog'; break }
    $renderers = @($cur | Where-Object { $_.CommandLine -match '--type=renderer' })
    if ($renderers.Count -eq 0) {
      if ($null -eq $rendererGoneAt) { $rendererGoneAt = [DateTime]::UtcNow }
      elseif (([DateTime]::UtcNow - $rendererGoneAt).TotalSeconds -ge 6) { $phase = 'linger'; break }
    } else { $rendererGoneAt = $null }
    Start-Sleep -Milliseconds 200
  }
}
Write-Output ("PHASE=" + $phase)

# Force-kill: tear down each main process tree (children before parent),
# skipping this process's ancestor chain.
$killSnapshot = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
  Select-Object ProcessId, ParentProcessId)
$childMap = @{}
foreach ($p in $killSnapshot) {
  $pp = [uint32]$p.ParentProcessId
  if (-not $childMap.ContainsKey($pp)) { $childMap[$pp] = New-Object System.Collections.Generic.List[uint32] }
  [void]$childMap[$pp].Add([uint32]$p.ProcessId)
}
$order = New-Object System.Collections.Generic.List[uint32]
$seen = @{}
foreach ($root in $mainPids) {
  $stack = New-Object System.Collections.Generic.Stack[uint32]
  $stack.Push($root)
  $branch = New-Object System.Collections.Generic.List[uint32]
  while ($stack.Count -gt 0) {
    $node = $stack.Pop()
    if ($seen.ContainsKey($node)) { continue }
    $seen[$node] = $true
    $branch.Add($node)
    if ($childMap.ContainsKey($node)) { foreach ($c in $childMap[$node]) { $stack.Push($c) } }
  }
  for ($i = $branch.Count - 1; $i -ge 0; $i--) { $order.Add($branch[$i]) }
}
foreach ($procId in $order) {
  if ($protected.ContainsKey($procId)) { continue }
  try { Stop-Process -Id $procId -Force -ErrorAction Stop } catch {}
}
Start-Sleep -Milliseconds 400

# Sweep leftover Cursor.exe not in the process tree (PID reuse, etc.).
foreach ($pass in 1..2) {
  $leftover = @(Get-LiveTargets | Where-Object { -not $protected.ContainsKey([uint32]$_.ProcessId) })
  if ($leftover.Count -eq 0) { break }
  foreach ($p in $leftover) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }
  Start-Sleep -Milliseconds 400
}
if (@(Get-LiveTargets | Where-Object { -not $protected.ContainsKey([uint32]$_.ProcessId) }).Count -gt 0) { exit 3 }
Write-Output ("CLOSED=" + $before.Count)
""".strip()


def _windows_close_cursor(layout: CursorLayout) -> int:
    powershell = _powershell_executable()
    if not powershell:
        raise SandToolError("未找到 PowerShell，无法安全关闭 Cursor")
    env = dict(os.environ)
    env["SAND_CURSOR_EXE"] = str(layout.executable)
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _WINDOWS_CLOSE_SCRIPT,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandToolError("无法安全关闭所选 Cursor 进程，请手动退出后重试") from exc
    if result.returncode == 4:
        raise SandToolError(
            "检测到当前命令正运行在要关闭的这份 Cursor 的内置终端里，"
            "无法在不结束自身的前提下关闭它。请改用系统终端（如 PowerShell / cmd）"
            "重新运行，或先手动退出 Cursor 再安装。"
        )
    if result.returncode != 0:
        raise SandToolError("无法安全关闭所选 Cursor 进程，请手动退出后重试")
    phase_match = re.search(r"PHASE=(\w+)", result.stdout)
    phase = phase_match.group(1) if phase_match else "unknown"
    if phase == "dialog":
        print_error(
            "Cursor 退出过程中弹出了对话框（常见为渲染进程崩溃 -1073741819），已强制结束。"
            "若反复出现，属 Cursor 自身 GPU/驱动问题：可在 argv.json 加 "
            '"disable-hardware-acceleration": true，或更新显卡驱动。'
        )
    elif phase == "timeout":
        print_step("Cursor 未在 12 秒内自行退出，已强制结束残余进程。")
    elif phase == "none":
        print_step("未能向 Cursor 窗口投递关闭消息，已直接强制结束。")
    elif phase == "linger":
        print_step("Cursor 窗口已全部关闭，主进程收尾超时，已强制结束。")
    match = re.search(r"CLOSED=(\d+)", result.stdout)
    return int(match.group(1)) if match else 0


def _mac_bundle_pids(layout: CursorLayout) -> List[int]:
    bundle = _find_app_bundle(layout.app_root)
    if bundle is None:
        return []
    contents = (bundle.resolve() / "Contents").resolve()
    pids: List[int] = []
    for pid, executable in _mac_process_paths(strict=True):
        if pid != os.getpid() and _is_within(executable, contents):
            pids.append(pid)
    return pids


def _wait_for_mac_exit(layout: CursorLayout, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _mac_bundle_pids(layout):
            return True
        time.sleep(0.25)
    return not _mac_bundle_pids(layout)


def _mac_close_cursor(layout: CursorLayout) -> int:
    before = _mac_bundle_pids(layout)
    if not before:
        return 0
    selected_bundle = _find_app_bundle(layout.app_root)
    running_bundles: Dict[str, Path] = {}
    for _pid, executable in _mac_process_paths(strict=True):
        bundle = _find_app_bundle(executable)
        if bundle is not None:
            running_bundles.setdefault(_path_key(bundle), bundle)
    if selected_bundle is not None and len(running_bundles) == 1:
        osascript = shutil.which("osascript") or "/usr/bin/osascript"
        try:
            subprocess.run(
                [
                    osascript,
                    "-e",
                    'tell application id "com.todesktop.230313mzl4w4u92" to quit',
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if _wait_for_mac_exit(layout, 12):
            return len(before)

    for pid in _mac_bundle_pids(layout):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    if _wait_for_mac_exit(layout, 3):
        return len(before)

    sigkill = getattr(signal, "SIGKILL", 9)
    for pid in _mac_bundle_pids(layout):
        try:
            os.kill(pid, sigkill)
        except (ProcessLookupError, PermissionError):
            pass
    if not _wait_for_mac_exit(layout, 2):
        raise SandToolError("无法安全关闭所选 Cursor 进程，请手动退出后重试")
    return len(before)


def close_cursor(layout: CursorLayout) -> int:
    if layout.is_server:
        return 0
    if sys.platform == "win32":
        return _windows_close_cursor(layout)
    if sys.platform == "darwin":
        return _mac_close_cursor(layout)
    raise SandToolError("桌面版 Cursor 仅支持 Windows 和 macOS")


def _server_extension_host_running(layout: CursorLayout) -> bool:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return False
    needle = str(layout.app_root).encode("utf-8", "surrogateescape")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if needle in cmdline and b"--type=extensionHost" in cmdline:
            return True
    return False


def _layout_kind_label(layout: CursorLayout) -> str:
    return "Remote-SSH cursor-server" if layout.is_server else "桌面版"


def _relaunch_hint(layout: CursorLayout) -> str:
    if layout.is_server:
        return (
            "请在连接到该服务器的 Cursor 远程窗口执行「Developer: Reload Window」"
            "（或断开后重新连接）以重启 extension host 并加载新代码。"
        )
    return "请手动启动 Cursor。"


def _cursor_is_running(layout: CursorLayout) -> bool:
    if layout.is_server:
        return _server_extension_host_running(layout)
    if sys.platform == "win32":
        target = _path_key(layout.executable)
        install_root = layout.install_root.resolve()
        for item in _windows_running_candidates():
            try:
                resolved = Path(item).expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if _path_key(resolved) == target or _is_within(resolved, install_root):
                return True
        return False
    if sys.platform == "darwin":
        return bool(_mac_bundle_pids(layout))
    return False


def _prepare_write(
    layout: CursorLayout,
    action: str,
    relaunch_args: Optional[Sequence[str]] = None,
) -> None:
    _ensure_writable(layout, action, relaunch_args)
    _confirm_close_cursor(layout)
    print_step("正在关闭 Cursor（如已打开）...")
    closed = close_cursor(layout)
    if closed:
        print_ok(f"已关闭 Cursor（{closed} 个进程）")
    else:
        print_step("Cursor 未在运行")


def _confirm_close_cursor(layout: CursorLayout) -> None:
    if not _cursor_is_running(layout):
        return
    if layout.is_server:
        print(
            colorize(
                "检测到远程 extension host 正在使用这份 cursor-server；"
                "写入不会中断它，补丁在远程窗口 Reload Window 后生效。",
                ANSI_YELLOW,
            )
        )
        return
    print(
        colorize(
            "需要先关闭 Cursor（所有窗口，含 Agents 窗口）：先请它正常退出，"
            "收尾超时或弹出对话框时会强制结束，未保存的内容会丢失，请先保存。",
            ANSI_YELLOW,
        )
    )
    if not _interactive_tty():
        return
    answer = _normalize_user_path(_prompt("按 Enter 关闭并继续，输入 n 取消> "))
    if answer.casefold() in {"n", "no", "q"}:
        raise SandToolError("已取消")


def _subagent_catalog_path(layout: CursorLayout) -> Path:
    return _agent_host_dist(layout) / SUBAGENT_MODEL_CATALOG_FILENAME


def _workbench_state_db_path() -> Optional[Path]:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Cursor"
            / "User"
            / "globalStorage"
            / "state.vscdb"
        )
    return None


def _load_official_models_from_state_db(db_path: Path) -> List[Mapping[str, object]]:
    import sqlite3

    def fetch_row(query_suffix: str) -> Optional[Tuple[object, ...]]:
        connection = sqlite3.connect(
            db_path.resolve().as_uri() + query_suffix, uri=True, timeout=5
        )
        try:
            return connection.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                (WORKBENCH_APPLICATION_USER_KEY,),
            ).fetchone()
        finally:
            connection.close()

    try:
        row = fetch_row("?mode=ro")
    except sqlite3.OperationalError:
        row = fetch_row("?mode=ro&immutable=1")
    if row is None:
        raise SandToolError("workbench 状态库中没有 applicationUser 记录")
    raw = row[0]
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    if not isinstance(raw, str):
        raise SandToolError("workbench 状态库的 applicationUser 记录不是文本")
    document = json.loads(raw)
    models = document.get("availableDefaultModels2") if isinstance(document, dict) else None
    del document
    if not isinstance(models, list) or not models:
        raise SandToolError("workbench 状态库中没有缓存的模型表（availableDefaultModels2）")
    return [item for item in models if isinstance(item, dict)]


def _builtin_subagent_models() -> List[Mapping[str, object]]:
    return [
        {"name": model_id, "clientDisplayName": display_name, "idAliases": list(aliases)}
        for model_id, display_name, aliases in BUILTIN_SUBAGENT_MODEL_CATALOG
    ]


ParameterValue = Dict[str, str]


def _parameter_values(item: Mapping[str, object]) -> List[ParameterValue]:
    values = item.get("parameterValues") if "parameterValues" in item else item.get("parameters")
    if not isinstance(values, list):
        return []
    return [
        {"id": str(entry.get("id")), "value": str(entry.get("value"))}
        for entry in values
        if isinstance(entry, dict) and entry.get("id") is not None and entry.get("value") is not None
    ]


def _subagent_model_tiers(model: Mapping[str, object]) -> Optional[Dict[str, object]]:
    definitions = model.get("parameterDefinitions")
    parameters: List[Dict[str, object]] = []
    if isinstance(definitions, list):
        for definition in definitions:
            if not isinstance(definition, dict):
                continue
            param_id = str(definition.get("id") or "").strip()
            kind = definition.get("parameterType")
            values: List[str] = []
            if isinstance(kind, dict):
                for key in ("enumParameter", "booleanParameter"):
                    spec = kind.get(key)
                    if isinstance(spec, dict) and isinstance(spec.get("values"), list):
                        values = [
                            str(entry.get("value"))
                            for entry in spec["values"]
                            if isinstance(entry, dict) and entry.get("value") is not None
                        ]
                        break
            if param_id and values:
                parameters.append({"id": param_id, "values": values})
    if not parameters:
        return None
    variants: List[Dict[str, object]] = []
    seen_variants: Set[str] = set()
    default_non_max: Optional[List[ParameterValue]] = None
    default_max: Optional[List[ParameterValue]] = None
    first_non_max: Optional[List[ParameterValue]] = None
    raw_variants = model.get("variants")
    for variant in raw_variants if isinstance(raw_variants, list) else []:
        if not isinstance(variant, dict):
            continue
        params = _parameter_values(variant)
        max_mode = variant.get("isMaxMode") is True
        if default_non_max is None and variant.get("isDefaultNonMaxConfig") is True:
            default_non_max = params
        if default_max is None and variant.get("isDefaultMaxConfig") is True:
            default_max = params
        if first_non_max is None and not max_mode:
            first_non_max = params
        slug = str(variant.get("legacySlug") or "").strip()
        if not slug:
            continue
        key = json.dumps([slug, max_mode, params], sort_keys=True)
        if key in seen_variants:
            continue
        seen_variants.add(key)
        variants.append({"slug": slug, "maxMode": max_mode, "parameters": params})
    non_max = (
        default_non_max
        if default_non_max is not None
        else first_non_max
        if first_non_max is not None
        else (variants[0]["parameters"] if variants else [])
    )
    return {
        "parameters": parameters,
        "defaults": {"nonMax": non_max, "max": default_max if default_max is not None else non_max},
        "variants": variants,
    }


def _sidecar_tiers(item: Mapping[str, object]) -> Optional[Dict[str, object]]:
    parameters = item.get("parameters")
    if not isinstance(parameters, list):
        return None
    clean_parameters: List[Dict[str, object]] = []
    for entry in parameters:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        raw_values = entry.get("values")
        if isinstance(raw_values, list):
            clean_parameters.append(
                {"id": str(entry.get("id")), "values": [str(value) for value in raw_values]}
            )
    if not clean_parameters:
        return None
    raw_defaults = item.get("defaults")
    defaults: Mapping[str, object] = raw_defaults if isinstance(raw_defaults, dict) else {}
    non_max = _parameter_values({"parameters": defaults.get("nonMax")})
    max_params = _parameter_values({"parameters": defaults.get("max")}) or non_max
    raw_variant_list = item.get("variants")
    variants = [
        {
            "slug": str(variant.get("slug")).strip(),
            "maxMode": variant.get("maxMode") is True,
            "parameters": _parameter_values(variant),
        }
        for variant in (raw_variant_list if isinstance(raw_variant_list, list) else [])
        if isinstance(variant, dict) and str(variant.get("slug") or "").strip()
    ]
    return {
        "parameters": clean_parameters,
        "defaults": {"nonMax": non_max, "max": max_params},
        "variants": variants,
    }


def _build_subagent_catalog(
    models: Sequence[Mapping[str, object]],
    source: str,
) -> Dict[str, object]:
    entries: List[Dict[str, object]] = []
    seen: Set[str] = set()
    for model in models:
        model_id = str(model.get("name") or "").strip()
        if not model_id or model_id in seen:
            continue
        if model.get("supportsAgent") is False:
            continue
        seen.add(model_id)
        aliases: List[str] = []
        for field in ("idAliases", "legacySlugs"):
            values = model.get(field)
            if not isinstance(values, list):
                continue
            for alias in values:
                text = str(alias).strip()
                if text and text != model_id and text not in aliases:
                    aliases.append(text)
        display_name = str(model.get("clientDisplayName") or model_id).strip()
        entry: Dict[str, object] = {"id": model_id, "displayName": display_name, "aliases": aliases}
        tiers = model.get("sandTiers")
        if not isinstance(tiers, dict):
            tiers = _subagent_model_tiers(model)
        if tiers:
            entry.update(tiers)
        entries.append(entry)
    if not entries:
        raise SandToolError("模型表过滤后为空，未写入子代理模型目录")
    return {
        "version": SUBAGENT_MODEL_CATALOG_VERSION,
        "toolVersion": SUPPORTED_CURSOR_VERSION,
        "source": source,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "models": entries,
    }


def _load_subagent_catalog_file(path: Path) -> Dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SandToolError(f"无法读取子代理模型目录文件：{path}（{exc}）") from exc
    models = document.get("models") if isinstance(document, dict) else None
    if not isinstance(models, list):
        raise SandToolError(f"子代理模型目录格式不正确（缺少 models 数组）：{path}")
    normalized = [
        {
            "name": str(item.get("id") or "").strip(),
            "clientDisplayName": item.get("displayName"),
            "idAliases": item.get("aliases") if isinstance(item.get("aliases"), list) else [],
            "sandTiers": _sidecar_tiers(item),
        }
        for item in models
        if isinstance(item, dict)
    ]
    return _build_subagent_catalog(normalized, f"file:{path.name}")


def _resolve_subagent_catalog(
    layout: CursorLayout,
    import_path: Optional[Path],
) -> Dict[str, object]:
    if import_path is not None:
        return _load_subagent_catalog_file(import_path)
    db_path = None if layout.is_server else _workbench_state_db_path()
    if db_path is not None and db_path.is_file():
        try:
            return _build_subagent_catalog(
                _load_official_models_from_state_db(db_path),
                "state.vscdb",
            )
        except Exception as exc:
            print_warn(f"读取 workbench 模型缓存失败，改用内置快照：{exc}")
    elif layout.is_server:
        print_warn(
            "cursor-server 没有官方模型缓存，子代理模型目录改用内置快照；"
            f"要拿到当前模型表请在桌面端 export-models 后执行 {SCRIPT_NAME} refresh-models --from <文件>"
        )
    else:
        print_warn("未找到 workbench 状态库，子代理模型目录改用内置快照")
    print_warn(
        f"内置快照抓取于 {BUILTIN_SUBAGENT_MODEL_SNAPSHOT_DATE}，新模型与档位信息不在其中；"
        "子代理只能按快照里的模型名切换、不能指定档位"
    )
    return _build_subagent_catalog(_builtin_subagent_models(), "builtin")


def _write_subagent_catalog(layout: CursorLayout, catalog: Mapping[str, object]) -> Path:
    path = _subagent_catalog_path(layout)
    reference = path.parent / "main.js"
    mode = stat.S_IMODE(reference.stat().st_mode) if reference.is_file() else 0o644
    data = (json.dumps(catalog, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    _atomic_write(path, data, mode)
    return path


def _remove_subagent_catalog(layout: CursorLayout) -> bool:
    path = _subagent_catalog_path(layout)
    if not path.exists():
        return False
    path.unlink()
    return True


def _read_subagent_catalog(layout: CursorLayout) -> Optional[Dict[str, object]]:
    path = _subagent_catalog_path(layout)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"corrupt": True}
    models = document.get("models") if isinstance(document, dict) else None
    entries = [item for item in (models if isinstance(models, list) else []) if isinstance(item, dict)]
    ids = [str(item.get("id")) for item in entries if item.get("id")]
    return {
        "version": document.get("version") if isinstance(document, dict) else None,
        "source": document.get("source") if isinstance(document, dict) else None,
        "generatedAt": document.get("generatedAt") if isinstance(document, dict) else None,
        "modelIds": ids,
        "tierModelCount": _count_tier_models(entries),
    }


def _count_tier_models(models: Sequence[Mapping[str, object]]) -> int:
    return sum(
        1
        for item in models
        if isinstance(item.get("parameters"), list) and item.get("parameters")
    )


def _print_subagent_catalog(catalog: Mapping[str, object], path: Path) -> None:
    models = catalog.get("models")
    entries = [item for item in models if isinstance(item, dict)] if isinstance(models, list) else []
    print_ok(
        f"子代理模型目录已写入：{path}（{len(entries)} 个模型，"
        f"{_count_tier_models(entries)} 个含档位，来源 {catalog.get('source')}）"
    )
    if entries:
        joined = ", ".join(str(item.get("id")) for item in entries)
        for line in textwrap.wrap(
            joined,
            width=96,
            initial_indent="  ",
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        ):
            print(line)


def refresh_subagent_models(layout: CursorLayout, import_path: Optional[Path]) -> int:
    status = inspect_status(layout)
    if not status.installed:
        raise SandToolError("尚未安装 Sand 补丁，请先执行 install")
    catalog = _resolve_subagent_catalog(layout, import_path)
    relaunch_args = ["refresh-models"]
    if import_path is not None:
        relaunch_args += ["--from", str(import_path.resolve())]
    _ensure_writable(layout, "refresh-models", relaunch_args)
    path = _write_subagent_catalog(layout, catalog)
    _print_subagent_catalog(catalog, path)
    print_step("下一轮对话生效，无需重启 Cursor；Remote-SSH 远端同样无需 Reload。")
    return 0


def export_subagent_models(layout: CursorLayout, out_path: Path) -> int:
    catalog = _resolve_subagent_catalog(layout, None)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    models = catalog.get("models")
    count = len(models) if isinstance(models, list) else 0
    print_ok(f"已导出 {count} 个模型到 {out_path}（来源 {catalog.get('source')}）")
    print_step(f"复制到远端后执行：python3 {SCRIPT_NAME} refresh-models --from <文件>")
    return 0


def _build_plan(
    layout: CursorLayout,
    transform: Callable[[str, str], Tuple[str, PatchStats]],
    baseline: Mapping[Path, bytes],
) -> Tuple[Dict[Path, PlannedFile], PatchStats]:
    plan: Dict[Path, PlannedFile] = {}
    total_stats = PatchStats()
    targets = layout.target_paths
    total = len(targets)
    print_step(f"正在分析 {total} 个目标文件...")
    for index, target in enumerate(targets, 1):
        print_progress(index, total, "分析")
        original = _read_planned_file(target)
        stock = baseline[target]
        stock_text = (
            _bundle_text(target) if stock == original.original else _decode_js(stock, target)
        )
        next_content, stats = transform(stock_text, _target_label(target))
        total_stats.merge(stats)
        if next_content != _bundle_text(target):
            plan[target] = PlannedFile(
                original=original.original,
                next_bytes=next_content.encode("utf-8"),
                mode=original.mode,
            )
    if plan:
        print_step("正在同步校验信息...")
        _update_extension_hashes(layout, plan)
        _sync_product_checksums(layout, plan)
    return plan, total_stats


def _build_install_plan(
    layout: CursorLayout,
    pristine: Mapping[Path, bytes],
) -> Tuple[Dict[Path, PlannedFile], PatchStats]:
    return _build_plan(
        layout,
        lambda content, label: apply_patch_to_content(content, label),
        pristine,
    )


def _build_uninstall_plan(
    layout: CursorLayout,
    pristine: Mapping[Path, bytes],
) -> Dict[Path, PlannedFile]:
    plan, _stats = _build_plan(layout, lambda content, _label: (content, PatchStats()), pristine)
    return plan


def _anchor_variants(original: str, patched: str) -> Tuple[str, ...]:
    return tuple(dict.fromkeys((original, patched)))


def _anchor_variant_hits(
    contents_by_target: Iterable[Tuple[Path, str]],
    original: str,
    patched: str,
) -> Tuple[int, Tuple[Tuple[str, int], ...]]:
    variants = _anchor_variants(original, patched)
    hits = tuple(
        (
            _target_label(target),
            sum(content.count(variant) for variant in variants),
        )
        for target, content in contents_by_target
    )
    return sum(count for _label, count in hits), tuple(
        (label, count) for label, count in hits if count
    )


def _anchor_issue(
    name: str,
    count: int,
    expected: int,
    sites: Iterable[Tuple[str, int]],
) -> str:
    location_text = "、".join(
        f"{label}×{site_count}" for label, site_count in sites
    ) or "无"
    return (
        f"{name}={count}（期望 {expected}；"
        f"命中站点：{location_text}）"
    )


def _install_anchor_readiness_issues(
    layout: CursorLayout,
    pristine: Mapping[Path, bytes],
) -> List[str]:
    contents_by_target = [
        (target, _decode_js(pristine[target], target))
        for target in layout.target_paths
    ]
    checks: List[Tuple[str, str, str, int]] = []
    seen_anchors: Set[Tuple[str, int]] = set()
    for patch in PATCHES:
        if not patch.readiness or (layout.is_server and patch.workbench):
            continue
        key = (patch.original, patch.hits)
        if key in seen_anchors:
            continue
        seen_anchors.add(key)
        checks.append((patch.name, patch.original, patch.patched, patch.hits))
    if not layout.is_server:
        checks.extend(
            (
                f"workbench 模型推荐 silentSwitch 拦截点 {index + 1}",
                prefix,
                _silent_switch_patched(prefix),
                1,
            )
            for index, prefix in enumerate(MODEL_NUDGE_SILENT_SWITCH_PREFIXES)
        )

    issues: List[str] = []
    for name, original, patched, expected in checks:
        count, sites = _anchor_variant_hits(contents_by_target, original, patched)
        if count == 0:
            continue
        if count != expected:
            issues.append(_anchor_issue(name, count, expected, sites))

    filter_sites = tuple(
        (_target_label(target), len(SUBAGENT_TASK_FILTER_GENERIC_RE.findall(content)))
        for target, content in contents_by_target
    )
    filter_sites = tuple((label, count) for label, count in filter_sites if count)
    filter_count = sum(count for _label, count in filter_sites)
    expected_filter = layout.expectations.nested_task_guard
    if filter_count and filter_count != expected_filter:
        issues.append(
            _anchor_issue(
                "host/exec/runtime 子代理递归保护",
                filter_count,
                expected_filter,
                filter_sites,
            )
        )

    enablement_sites = tuple(
        (_target_label(target), len(AGENT_HOST_ENABLEMENT_RE.findall(content)))
        for target, content in contents_by_target
    )
    enablement_sites = tuple(
        (label, count) for label, count in enablement_sites if count
    )
    enablement_count = sum(count for _label, count in enablement_sites)
    expected_enablement = layout.expectations.agent_host_enablement
    if enablement_count and enablement_count != expected_enablement:
        issues.append(
            _anchor_issue(
                "workbench Agent Host 强开",
                enablement_count,
                expected_enablement,
                enablement_sites,
            )
        )
    return issues


def install(layout: CursorLayout) -> int:
    if layout.version != SUPPORTED_CURSOR_VERSION:
        raise SandToolError(
            f"当前 Cursor 版本为 {layout.version}，"
            f"本工具仅适配 Cursor {SUPPORTED_CURSOR_VERSION}。"
            "请更换为适配版本后再安装"
        )
    mode_label = _stream_mode_label(STREAM_MODE_DIRECT)
    print_step(
        f"开始安装  Cursor {layout.version}（{_layout_kind_label(layout)}）"
        f"  Stream 模式：{mode_label}"
    )
    relaunch_args = _install_relaunch_args()
    _confirm_sand_account()
    print_step("正在检测当前状态...")
    before = inspect_status(layout)
    if before.external_marker_count:
        raise SandToolError(_foreign_marker_block_message(before, "安装"))
    if before.bare_header_sites:
        print_warn(_bare_header_notice(before.bare_header_sites, installing=True))
    pristine = _ensure_pristine(layout)
    readiness_issues = _install_anchor_readiness_issues(layout, pristine)
    if readiness_issues:
        raise SandToolError(
            "当前 Cursor 未唯一匹配到安装锚点，未写入任何文件：\n  "
            + "\n  ".join(readiness_issues)
        )
    plan, _ = _build_install_plan(layout, pristine)
    if not plan:
        if (
            before.installed
            and before.stream_mode_installed
            and before.tool_exec_bridge_installed
        ):
            if _read_subagent_catalog(layout) is None:
                catalog = _resolve_subagent_catalog(layout, None)
                _ensure_writable(layout, "install", relaunch_args)
                _print_subagent_catalog(catalog, _write_subagent_catalog(layout, catalog))
            print_ok(f"补丁已是当前版本（Stream 模式：{mode_label}），未改动任何 Cursor 文件，无需重启。")
            return 0
        if not before.installed:
            raise SandToolError("当前 Cursor 版本未匹配到 Sand 客户端模式规则")
        raise SandToolError(
            "Sand 补丁不完整或当前 Cursor 版本不匹配；"
            f"请确认版本为 {SUPPORTED_CURSOR_VERSION} 后重新安装"
        )
    planned = _inspect_planned_status(layout, plan)
    if planned.managed_local_route_markers < 1 or planned.agent_host_identity_markers < 1:
        raise SandToolError(
            "未匹配到 AgentService/Run 默认路由或 ide 身份锚点；"
            "当前 Cursor 包可能不是 3.21.12 对应结构"
        )

    print_ok(f"计划修改 {len(plan)} 个文件")
    catalog = _resolve_subagent_catalog(layout, None)
    _ensure_writable(layout, "install", relaunch_args)
    _prepare_write(layout, "install", relaunch_args)

    def validate() -> None:
        status = inspect_status(layout)
        if not status.stream_mode_installed:
            raise SandToolError(
                "安装后状态校验失败：未写入 AgentService/Run 默认路由或 ide 身份。"
                f" managedLocalRoute={status.managed_local_route_markers},"
                f" agentHostIdentity={status.agent_host_identity_markers}"
            )
        _verify_extension_hashes(layout)
        _verify_product_checksums(layout)

    _commit_plan(layout, plan, "install", validate)
    try:
        _print_subagent_catalog(catalog, _write_subagent_catalog(layout, catalog))
    except Exception as exc:
        print_error(f"子代理模型目录写入失败（子代理暂只能继承父模型）：{exc}")
    print_ok(f"安装完成，Stream 模式：{mode_label}。" + _relaunch_hint(layout))
    return 0


def uninstall(layout: CursorLayout) -> int:
    print_step(f"开始卸载  Cursor {layout.version}（{_layout_kind_label(layout)}）")
    print_step("正在检测当前状态...")
    before = inspect_status(layout)
    if before.external_marker_count:
        raise SandToolError(_foreign_marker_block_message(before, "卸载"))
    if before.bare_header_sites:
        print_warn(_bare_header_notice(before.bare_header_sites, installing=False))
    plan = _build_uninstall_plan(layout, _ensure_pristine(layout))
    if not plan:
        if _subagent_catalog_path(layout).exists():
            _ensure_writable(layout, "uninstall")
            _remove_subagent_catalog(layout)
            print_step("已删除子代理模型目录")
        print_ok("卸载完成，" + _relaunch_hint(layout))
        return 0

    print_ok(f"计划恢复 {len(plan)} 个文件")
    _prepare_write(layout, "uninstall")

    def validate() -> None:
        status = inspect_status(layout)
        if status.installed or status.external_marker_count:
            raise SandToolError(
                "卸载后仍有 Sand marker："
                f"{status.client_markers + status.glass_client_markers + status.eligibility_markers}，"
                f"external={status.external_marker_count}"
            )
        _verify_extension_hashes(layout)
        _verify_product_checksums(layout)

    _commit_plan(layout, plan, "uninstall", validate)
    try:
        if _remove_subagent_catalog(layout):
            print_step("已删除子代理模型目录")
    except Exception as exc:
        print_error(f"子代理模型目录删除失败（stock 代码不会读取它，可手动删除）：{exc}")
    print_ok("卸载完成，" + _relaunch_hint(layout))
    return 0


def _permission_hint() -> str:
    script = Path(__file__).resolve()
    if sys.platform == "win32":
        return "自动提权失败。请在管理员终端中重新运行。"
    return f'自动提权失败。请执行：sudo python3 "{script}"'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Cursor Sand 客户端模式安装/卸载工具"
            "（Windows / macOS 桌面版，以及 Linux 上的 Remote-SSH cursor-server）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"在终端执行 python {SCRIPT_NAME} 打开菜单。\n"
            "Windows 写入系统目录时会自动请求管理员权限。\n"
            "Remote-SSH：把本脚本复制到远端，用远端 python3 执行；"
            "会自动发现 ~/.cursor-server/bin/<platform>/<commit>，\n"
            "写入后在远程窗口执行 Developer: Reload Window 生效。\n"
            "示例：\n"
            f"  python {SCRIPT_NAME}\n"
            f"  python {SCRIPT_NAME} install                 # AgentService/Run 双工\n"
            f"  python {SCRIPT_NAME} uninstall\n"
            f"  python {SCRIPT_NAME} status --json\n"
            f"  python {SCRIPT_NAME} backups\n"
            f"  python {SCRIPT_NAME} restore\n"
            f"  python {SCRIPT_NAME} prune-backups --keep 3\n"
            f"  python {SCRIPT_NAME} refresh-models\n"
            f"  python {SCRIPT_NAME} export-models subagent-models.json\n"
            f"  python3 {SCRIPT_NAME} refresh-models --from ~/subagent-models.json\n"
            f"  python {SCRIPT_NAME} set-path \"E:\\Development\\IDE\\cursor\"\n"
            f"  python3 {SCRIPT_NAME} set-path /Applications/Cursor.app\n"
            f"  python3 {SCRIPT_NAME} set-path ~/.cursor-server/bin/linux-x64/<commit>\n"
            f"  python {SCRIPT_NAME} set-path auto"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s（适配 Cursor {SUPPORTED_CURSOR_VERSION}）",
    )
    parser.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--log-file", default=None, metavar="PATH", help=argparse.SUPPRESS)
    parser.add_argument("--cancel-file", default=None, metavar="PATH", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install", help="安装/注入 Sand 客户端模式（AgentService/Run 双工）")
    commands.add_parser("uninstall", help="卸载 Sand 客户端模式")
    status_command = commands.add_parser("status", help="显示当前补丁状态")
    status_command.add_argument("--json", action="store_true", help="以 JSON 输出")
    commands.add_parser("backups", help="列出可用备份")
    restore_command = commands.add_parser("restore", help="从备份恢复 Cursor 文件")
    restore_command.add_argument(
        "backup",
        nargs="?",
        help="备份名称；省略则使用最近一次成功的备份",
    )
    prune_command = commands.add_parser("prune-backups", help="清理旧备份")
    prune_command.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_BACKUP_KEEP,
        help=f"保留最近的备份组数，默认 {DEFAULT_BACKUP_KEEP}",
    )
    refresh_models = commands.add_parser(
        "refresh-models",
        help="重建子代理可选模型目录（桌面端读 Cursor 缓存的官方模型表；下一轮生效，不关 Cursor）",
    )
    refresh_models.add_argument(
        "--from",
        dest="from_file",
        metavar="FILE",
        help="改用 export-models 导出的 JSON（cursor-server 远端没有官方缓存时使用）",
    )
    export_models = commands.add_parser(
        "export-models",
        help="把本机官方模型表导出为 JSON，供远端 refresh-models --from 使用",
    )
    export_models.add_argument("output", help="输出文件路径")
    set_path = commands.add_parser("set-path", help="设置 Cursor 路径；auto 恢复自动检测")
    set_path.add_argument(
        "path",
        help="Cursor.exe、Cursor.app、resources/app、安装根目录，或 auto",
    )
    return parser


def _status_payload(layout: CursorLayout, status: PatchStatus) -> Dict[str, object]:
    integrity_issues = static_integrity_issues(layout)
    return {
        "toolVersion": SUPPORTED_CURSOR_VERSION,
        "supportedCursorVersion": SUPPORTED_CURSOR_VERSION,
        "cursorVersion": layout.version,
        "installRoot": str(layout.install_root),
        "layoutKind": layout.kind,
        "installed": status.installed,
        "streamModeInstalled": status.stream_mode_installed,
        "toolExecBridgeInstalled": status.tool_exec_bridge_installed,
        "staticIntegrityVerified": not integrity_issues,
        "staticIntegrityIssues": integrity_issues,
        "markers": {
            spec.payload_key: getattr(status, spec.status_field) for spec in MARKER_SPECS
        },
        "remainingIde": status.ide_matches,
        "remainingGlassIdentity": status.glass_matches,
        "externalMarkers": status.external_marker_count,
        "foreignMarkers": [
            {
                "marker": marker,
                "count": count,
                "files": list(files),
                "owner": _marker_owner(marker),
            }
            for marker, count, files in status.foreign_markers
        ],
        "bareHeaderSites": status.bare_header_sites,
        "subagentModelCatalog": _subagent_catalog_status(layout),
    }


def _subagent_catalog_status(layout: CursorLayout) -> Dict[str, object]:
    catalog = _read_subagent_catalog(layout)
    payload: Dict[str, object] = {
        "path": str(_subagent_catalog_path(layout)),
        "present": catalog is not None,
    }
    if catalog is None:
        return payload
    if catalog.get("corrupt"):
        payload["corrupt"] = True
        return payload
    model_ids = catalog.get("modelIds")
    payload["version"] = catalog.get("version")
    payload["source"] = catalog.get("source")
    payload["generatedAt"] = catalog.get("generatedAt")
    payload["modelCount"] = len(model_ids) if isinstance(model_ids, list) else 0
    payload["tierModelCount"] = catalog.get("tierModelCount")
    payload["modelIds"] = model_ids
    return payload


def show_status(layout: CursorLayout, as_json: bool) -> int:
    status = inspect_status(layout)
    if as_json:
        print(json.dumps(_status_payload(layout, status), ensure_ascii=False, indent=2))
        return 0
    for text, code in collect_status_lines(layout, status):
        print(colorize(text, code))
    markers = {spec.payload_key: getattr(status, spec.status_field) for spec in MARKER_SPECS}
    if not status.installed:
        markers = {name: value for name, value in markers.items() if value}
    if markers:
        print()
        for name, value in markers.items():
            print(f"  {name}: {value}")
    return 0


def show_backups(layout: CursorLayout) -> int:
    entries = list_backups(layout)
    if not entries:
        print("没有可用备份。")
        return 0
    print(colorize(f"备份目录：{_backup_root(layout)}", ANSI_BLUE))
    for entry in entries:
        size = _directory_size(entry.directory) / 1048576
        print(
            f"  {entry.name}  {entry.operation}/{entry.status}  "
            f"{entry.file_count} 个文件  {size:.1f} MB  工具 {entry.tool_version}"
        )
    return 0


def restore_backup(layout: CursorLayout, name: Optional[str]) -> int:
    entries = list_backups(layout)
    if not entries:
        raise SandToolError("没有可用备份")
    if name:
        selected = next((item for item in entries if item.name == name), None)
        if selected is None:
            raise SandToolError(f"未找到备份：{name}")
    else:
        selected = next(
            (item for item in entries if item.status == "committed"),
            entries[0],
        )
        print_step(f"使用最近备份：{selected.name}")
    return _restore_from_backup(layout, selected)


def run_prune_backups(layout: CursorLayout, keep: int) -> int:
    removed, freed = prune_backups(layout, keep)
    if removed:
        print_ok(f"已清理 {removed} 组备份，释放 {freed / 1048576:.1f} MB")
    else:
        print_ok("没有需要清理的备份")
    return 0


def collect_status_lines(
    layout: Optional[CursorLayout] = None,
    status: Optional[PatchStatus] = None,
) -> List[Tuple[str, str]]:
    if layout is None:
        try:
            layout = resolve_cursor_layout(interactive=False)
        except SandToolError as exc:
            layouts = _discover_all_layouts()
            if len(layouts) > 1:
                lines: List[Tuple[str, str]] = [
                    (
                        f"检测到 {len(layouts)} 个 Cursor，请选 4 指定要操作的路径：",
                        ANSI_YELLOW,
                    )
                ]
                for item in layouts:
                    lines.append((f"  - {item.install_root}  ({item.version})", ANSI_BLUE))
                return lines
            if not layouts:
                return [(str(exc), ANSI_YELLOW)]
            layout = layouts[0]
    if status is None:
        status = inspect_status(layout)

    lines = [
        (
            f"当前 Cursor：{layout.version}  {layout.install_root}"
            f"  [{_layout_kind_label(layout)}]",
            ANSI_BLUE,
        )
    ]
    if status.stream_mode_installed and status.tool_exec_bridge_installed:
        lines.append(
            (f"状态：已安装 Sand 客户端模式（Stream 模式：{_stream_mode_label(STREAM_MODE_DIRECT)}）", ANSI_GREEN)
        )
    elif status.installed:
        lines.append(("状态：Sand 补丁不完整，请选 1 重新安装", ANSI_YELLOW))
    else:
        lines.append(("状态：未安装 Sand 客户端模式，请选 1 安装", ANSI_YELLOW))
    if status.installed:
        catalog = _subagent_catalog_status(layout)
        if not catalog.get("present"):
            lines.append(
                ("子代理模型目录缺失：子代理只能继承父模型，请选 7 刷新", ANSI_YELLOW)
            )
        elif catalog.get("corrupt"):
            lines.append(("子代理模型目录损坏，请选 7 重新生成", ANSI_YELLOW))
        else:
            tier_count = catalog.get("tierModelCount") or 0
            lines.append(
                (
                    f"子代理模型目录：{catalog.get('modelCount')} 个模型"
                    f"（来源 {catalog.get('source')}，{tier_count} 个含档位）",
                    ANSI_BLUE,
                )
            )
            if not tier_count:
                lines.append(
                    ("子代理模型目录不含档位信息，子代理只能换模型不能指定档位，请选 7 刷新", ANSI_YELLOW)
                )
        integrity_issues = static_integrity_issues(layout)
        if integrity_issues:
            lines.append(
                (
                    f"补丁与当前脚本不一致（{len(integrity_issues)} 处锚点不匹配），"
                    "请选 1 重新安装；明细见 status --json 的 staticIntegrityIssues",
                    ANSI_YELLOW,
                )
            )
    if status.external_marker_count:
        lines.append(
            (
                "检测到其他 Sand 模式标记："
                + _format_foreign_markers(status)
                + "。请先卸载产生这些标记的补丁后再安装",
                ANSI_YELLOW,
            )
        )
    if status.bare_header_sites:
        lines.append(
            (
                f"检测到 {status.bare_header_sites} 处无变量前缀的 "
                f"x-cursor-client-type 请求头（非 {SUPPORTED_CURSOR_VERSION} 原版形态）",
                ANSI_YELLOW,
            )
        )
    return lines


def print_banner() -> None:
    print(colorize(f"Sand 客户端模式安装工具  {SCRIPT_NAME}", ANSI_BOLD))
    print(colorize(f"将 Cursor 切换为 Sand 客户端模式，仅适配 {SUPPORTED_CURSOR_VERSION}。", ANSI_BLUE))
    print(
        colorize(
            "桌面版安装或卸载会关闭 Cursor，完成后请手动重新打开；"
            "cursor-server 不中断进程，需在远程窗口 Reload Window。",
            ANSI_YELLOW,
        )
    )
    for text, code in collect_status_lines():
        print(colorize(text, code))
    print()


def apply_set_path(value: str) -> int:
    save_cursor_path(_normalize_user_path(value))
    return 0


def print_menu() -> None:
    print(colorize("请输入编号后回车；安装/卸载成功后会自动退出。", ANSI_BOLD))
    print(colorize("  1) 安装：直连 api2 模式", ANSI_GREEN))
    print(colorize("  2) 卸载并恢复 Cursor 原状", ANSI_GREEN))
    print(colorize("  3) 设置 Cursor 安装路径", ANSI_GREEN))
    print(colorize("  4) 从备份恢复", ANSI_GREEN))
    print(colorize("  5) 清理旧备份", ANSI_GREEN))
    print(colorize("  6) 刷新子代理可选模型目录（不关 Cursor）", ANSI_GREEN))
    print(colorize("  0) 退出", ANSI_GREEN))


def prompt_set_path() -> int:
    layouts = _discover_all_layouts()
    if layouts:
        print(colorize("检测到的 Cursor：", ANSI_BOLD))
        for index, layout in enumerate(layouts, 1):
            print(f"  {index}) {layout.install_root}  ({layout.version})")
        print("  auto) 恢复自动检测")
        print("输入编号，或把 Cursor.exe / Cursor.app 拖进窗口后回车")
    else:
        print("未自动检测到 Cursor，请拖入 Cursor.exe / Cursor.app 或输入路径")
    value = _normalize_user_path(_prompt("路径> "))
    if not value:
        return 0
    if value.isdigit() and layouts:
        index = int(value)
        if 1 <= index <= len(layouts):
            save_cursor_path(str(layouts[index - 1].install_root))
            print(colorize("Cursor 路径已更新", ANSI_GREEN))
            return 0
        print_error("无效编号。")
        return 0
    apply_set_path(value)
    print(colorize("Cursor 路径已更新", ANSI_GREEN))
    return 0


def run_choice(choice: str) -> bool:
    if choice == "1":
        print()
        install(resolve_cursor_layout())
        return True
    if choice == "2":
        print()
        uninstall(resolve_cursor_layout())
        return True
    if choice == "3":
        prompt_set_path()
        return False
    if choice == "4":
        print()
        layout = resolve_cursor_layout()
        show_backups(layout)
        name = _normalize_user_path(_prompt("输入备份名称，留空使用最近一次> "))
        restore_backup(layout, name or None)
        return True
    if choice == "5":
        print()
        run_prune_backups(resolve_cursor_layout(), DEFAULT_BACKUP_KEEP)
        return False
    if choice == "6":
        print()
        refresh_subagent_models(resolve_cursor_layout(), None)
        return False
    if choice == "0":
        return True
    print_error("无效选项，请输入 0-6。")
    return False


def interactive_loop() -> int:
    while True:
        print_banner()
        print_menu()
        try:
            raw = input(colorize("请输入编号> ", ANSI_BLUE))
        except EOFError:
            print()
            return 0
        choice = _normalize_user_path(raw)
        try:
            if run_choice(choice):
                return 0
        except SystemExit as exc:
            code = 0 if exc.code is None else exc.code
            if isinstance(code, int) and code == 0:
                return 0
            if isinstance(code, int):
                print_error(f"管理员窗口返回错误码 {code}")
            else:
                print_error(str(code))
        except PermissionError as exc:
            print_error(f"错误：没有写入权限：{exc}")
            print_error(_permission_hint())
        except SandCancelled as exc:
            print_error(str(exc) or "已取消。")
        except SandToolError as exc:
            print_error(f"错误：{exc}")
        except KeyboardInterrupt:
            print()
            return 0
        except Exception as exc:
            print_error(f"未预期错误：{exc}")
        print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    global _INTERACTIVE, _PAUSE_ON_EXIT
    _configure_console()
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        if not args_list:
            _INTERACTIVE = True
            return interactive_loop()

        args = build_parser().parse_args(args_list)
        _PAUSE_ON_EXIT = bool(args.pause)
        if args.log_file:
            _install_log_file(args.log_file)
        if args.cancel_file:
            _install_cancel_file(args.cancel_file)
        if args.command == "set-path":
            result = apply_set_path(args.path)
            print(colorize("Cursor 路径已更新", ANSI_GREEN))
            return result
        layout = resolve_cursor_layout(interactive=False)
        if args.command == "install":
            return install(layout)
        if args.command == "uninstall":
            return uninstall(layout)
        if args.command == "status":
            return show_status(layout, bool(args.json))
        if args.command == "backups":
            return show_backups(layout)
        if args.command == "restore":
            return restore_backup(layout, args.backup)
        if args.command == "prune-backups":
            return run_prune_backups(layout, int(args.keep))
        if args.command == "refresh-models":
            import_path = (
                Path(_normalize_user_path(args.from_file)).expanduser().resolve()
                if args.from_file
                else None
            )
            return refresh_subagent_models(layout, import_path)
        if args.command == "export-models":
            return export_subagent_models(
                layout,
                Path(_normalize_user_path(args.output)).expanduser().resolve(),
            )
        raise SandToolError(f"未知命令：{args.command}")
    except PermissionError as exc:
        print_error(f"错误：没有写入权限：{exc}")
        print_error(_permission_hint())
        return 3
    except SandCancelled as exc:
        print_error(str(exc) or "已取消。")
        return 130
    except SandToolError as exc:
        print_error(f"错误：{exc}")
        return 2
    except KeyboardInterrupt:
        print_error("操作已取消。")
        return 130
    except Exception as exc:
        print_error(f"未预期错误：{exc}")
        return 1


def _assert_registry_consistent() -> None:
    hardened = SUBAGENT_TASK_FILTER_GENERIC_RE.sub(
        _harden_subagent_task_filter, SUBAGENT_TASK_FILTER_ORIGINAL
    )
    if hardened != SUBAGENT_TASK_FILTER_PATCHED:
        raise RuntimeError("SUBAGENT_TASK_FILTER_GENERIC_RE 与 host 参考形态不一致")
    if not SUBAGENT_TASK_FILTER_HARDENED_RE.search(SUBAGENT_TASK_FILTER_PATCHED):
        raise RuntimeError("SUBAGENT_TASK_FILTER_HARDENED_RE 认不出加固后的形态")
    names = [patch.name for patch in PATCHES]
    if len(set(names)) != len(names):
        raise RuntimeError("PATCHES 存在重名条目")
    stats_fields = set(vars(PatchStats()))
    for patch in PATCHES:
        if patch.stats not in stats_fields:
            raise RuntimeError(f"{patch.name} 的 stats 字段 {patch.stats} 不在 PatchStats 中")
        if patch.guard_marker is None and patch.original in patch.patched:
            raise RuntimeError(f"{patch.name} 的原文仍是补丁形态的子串，二次 apply 会重复注入")


_assert_known_markers_complete()
_assert_registry_consistent()


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
    finally:
        _pause_before_exit()
    raise SystemExit(exit_code)
