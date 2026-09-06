# SandClaimer 补丁多版本 · 设计规格

**日期**：2026-09-06  
**状态**：已落地（1.3.0）  
**范围**：本机 Cursor Stream 回路补丁，双轨对齐参考树已验证版本  
**代号**：patch-multi-version  
**外业来源**：仓库内 `cursor-account-manager-main`（Changelog 2.3.20，已测 3.18.9 / 3.18.25 / 3.19.13）。本环境无 3.19.13 真机；日志关键字沿用现有 `runtime_report`，若现场不同再开 1.3.1。

---

## 0. 决策摘要

| 决策点 | 结论 |
|---|---|
| 支持注入 | `3.18.9`、`3.18.25`、`3.19.13` |
| 其它版本 | 拒绝写入；文案列出已测试版本，下载链按版本给 SHA |
| 打法 | 先走现有 3.18 结构正则；没有对应 marker 再打 3.19.13 字面量（与参考树相同） |
| 3.19 直连流 | **本包不注入**。本工具回路是 managed-local，且 `stream_mode_installed` 要求 `direct_stream==0`。参考树的 `promptModelInfo` 直连流留 1.3.1，等真机日志证明仍走 RunInference |
| 路由文案 / Grok prompt 互斥 | 不做（PRD 非目标） |
| 卸载 | 揭本工具 marker；顺带还原参考树 3.19 那几条字面量补丁（若本机被插件打过） |
| 规则判定 | 同一条语义规则同时认 3.18 锚点与 3.19 锚点。3.18.9 缺 3.19 字面量 **不算** 失败 |

---

## 1. 问题

3.19.13 重写本地 Agent 内核后，1.2.x 的 3.18 正则打不中：`gate-off` 变成先 `return connect`，runtime / move_exec / 动作准入的 minify 形态都变了。面板可显示「版本不符」或「成功」，实际仍走云端。

3.18.25 与 3.18.9 同结构，现有 `\w+` 正则应已覆盖；本包把它列入已测试表并补下载链。

---

## 2. 成功标准

1. 合成夹具 **3.18.9 形态** 打上再卸，字节回到原样（现有 `VANILLA` 仍绿）。
2. 合成夹具 **3.19.13 形态**（参考树字面量）打上后七类 Stream 标记齐，再卸回到原样。
3. 3.18 夹具打完 **没有** 3.19 独有插入；3.19 夹具不依赖 3.18 的 `try{return(yield…gate-off)}`。
4. `patch_report`：3.19 夹具上 Stream 规则为 pending/applied，不是 missing；3.18 夹具不会因为没有 3.19 字面量判失败。
5. `install()` 在 3.19 七类各 ≥1 之前拒绝落盘（与 3.18 相同「全有或全无」）。
6. UI / `patch_status` 给出已测试版本列表，下载不再永远指向 3.18.9。
7. Changelog 第一句：请重打补丁；支持 Cursor 3.18.9 / 3.18.25 / 3.19.13。

---

## 3. 3.19 字面量（摘自参考树，本包用本工具 marker）

| 语义 | 原串要点 | 写入 |
|---|---|---|
| managed-local | `if(!o)return{runtime:"connect",reason:"gate-off"};…reason:"eligible"` | 前面插入 `return{runtime:"managed-local",reason:"sand-client"}/*SAND_MANAGED_LOCAL_ROUTE_V1*/;`，原文留下 |
| runtime load | `let t=!1;try{t=await r.cursor.checkFeatureGate(Ms)}` | `let t=!0;/*SAND_LOCAL_RUNTIME_LOAD_V1*//*Ms*/try{t=!0}` |
| move_exec | `h=await Promise.resolve(r.cursor.checkFeatureGate(Js)).catch(()=>!1)` | `h=!0/*SAND_MOVE_EXEC_V1*//*Js*/` |
| 动作准入 | 3.19 的 `userMessageAction`/`mode-not-supported` 串 | 放行 summarize/resume；marker 用 `SAND_LOCAL_ACTIONS_V1` |
| 子代理 | `isHostedSubagentChild:Boolean(e.runOptions.subagentTypeName\|\|…)` | 原文后附 `SAND_SUBAGENT_LOCAL_V1`（3.19 内核已认 hosted child，标记用于齐套计数） |
| identity / enablement | 与 3.18 相同字面量 | 沿用 |

仅当对应 **本工具 marker 尚未出现** 时才打 3.19 轨，避免 3.18 打完再被 3.19 轨二次改写。

---

## 4. 版本表

| 版本 | SHA | 轨 |
|---|---|---|
| 3.18.9 | `2ba48ff3f7514cc4643c52ca9f7b3173d9b66137` | 3.18 正则 |
| 3.18.25 | `280eca2911f1774689696e5f1efa5a4f97a87af3` | 3.18 正则 |
| 3.19.13 | `dd066f332fcea7382764400fde902f61920648d5` | 3.19 字面量 |

下载宿主：`https://downloads.cursor.com/production/{sha}/…`

---

## 5. 非目标

- 注入 3.19 `InferenceService` 直连流 / `promptModelInfo`
- 移植参考树踢设备、路由汉化、Grok 4.5/4.6 prompt
- 改号池、领取、导出
- 支持未列表的 Cursor 版本（含 3.19.7）

---

## 6. 测试

- `test_sand_patch.py`：`VANILLA` 往返；`VANILLA_319` 往返；二次 apply 幂等
- `test_patch_report.py`：319 夹具 stream 规则非 missing
- `test_cursor_versions.py` 或附在 patch 测试：`is_tested_cursor_version`
