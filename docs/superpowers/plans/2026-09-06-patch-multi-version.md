# 1.3.0 补丁多版本 · 实现计划

**规格：** `docs/superpowers/specs/2026-09-06-patch-multi-version-design.md`  
**版本：** `TOOL_VERSION = 1.3.0`

## 任务

1. `sand_patch.py`：3.18 正则之后加 3.19.13 字面量轨；卸载还原本工具 + 参考树同语义字面量。
2. `patch_report.py`：同一 RuleSpec 认双轨锚点；失败文案列已测试版本。
3. `app.py` / `web/app.js`：已测试版本表 + 按版本 SHA 下载。
4. 合成夹具 `VANILLA` / `VANILLA_319` 往返与规则判定测试。
5. Changelog 第一句：请重打补丁；支持 Cursor 3.18.9 / 3.18.25 / 3.19.13。

## 失败测试（先红后绿）

- 3.18 `VANILLA` 打上再卸，字节回到原样；打完没有 `/*Ms*/` 等 3.19 插入。
- 3.19 `VANILLA_319` 七类 Stream 统计 > 0，再卸回到原样；二次 apply 幂等。
- `patch_report`：319 夹具 stream 规则为 pending/applied，不是 missing；318 夹具不因缺少 3.19 字面量失败。
- `is_tested_cursor_version` 认三版本、拒 3.19.7。

## 非目标

不注入 3.19 直连流；不移植踢设备 / 路由汉化 / Grok prompt；不改号池。
