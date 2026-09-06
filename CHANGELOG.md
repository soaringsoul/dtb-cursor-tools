# Changelog

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
