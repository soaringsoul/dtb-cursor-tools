# Changelog

## 1.3.0

**请重打补丁；支持 Cursor 3.18.9 / 3.18.25 / 3.19.13。**

- Stream 回路双轨：先走现有 3.18 结构正则；对应 marker 还没有时，再打 3.19.13 字面量（与仓库内参考树已验证的锚点对齐）。
- 未测试的 Cursor 版本仍拒绝写入。版本不符时列出已测试版本，下载链按版本给官方 SHA（默认指向 3.19.13）。
- `patch_report` 同一条语义规则同时认 3.18 / 3.19 锚点。3.18.9 机器不会因为没有 3.19 字面量被判失败。
- 卸载揭本工具 marker，并还原参考树打过的同语义 3.18 / 3.19 字面量（若本机被插件打过）。若曾用 cursor-account-manager 注入，建议先点「回退」再打本工具补丁。
- **不**注入 3.19 `InferenceService` 直连流 / `promptModelInfo`。本工具回路是 managed-local；若现场日志仍走 RunInference，再开 1.3.1。
- 本环境无 3.19.13 真机。外业来自参考树 Changelog 2.3.20 + 官方下载 SHA。`runtime_report` 日志关键字沿用现有假设。

## 1.2.2

质检与发版债。**无需重打补丁**（补丁规则与 1.2.1 相同）。

- 补齐不联网单测：`test_accounts_usage.py`（token / 去重 / 三池 / alive / 导出）、`test_sand_patch.py`（合成夹具打补丁再卸载，字节回到原样）、`test_patch_report.py`（规则判定与 agent-host 日志解析）。夹具只含锚点字面量，不是 Cursor 发行包。
- Codemagic macOS 产物的 `--product-version` 改为构建时读取 `sand_patch.TOOL_VERSION`，不再写死 `1.0.0`。
- 登录会话规格标记为已落地；第二期（踢会话等）仍不做，见迭代 PRD。

## 1.2.1

本机 Cursor 补丁。**已打旧补丁的机器请重新点一次「打补丁」**（面板会提示「补丁需升级」）。

- **后台任务结束弹 An unexpected error occurred。** 命令跑完后 Cursor 发 `backgroundTaskCompletionAction`，旧准入规则把它踢回云端 `connect`，sand 身份下被 401。本版 `SAND_LOCAL_ACTIONS_V1` 把本地 runtime 真正支持的动作放进白名单。
- **子代理用不了 Bot 额度。** `subagentTypeName` 等 run options 导致 `run-options-not-supported` → connect → 401。本版 `SAND_SUBAGENT_LOCAL_V1` 把这三项短路，子代理走本地回路。

钉死 Cursor **3.18.9**。其它版本仍会因锚点缺失拒绝写入。
