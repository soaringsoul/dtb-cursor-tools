# 切号先刷票再写入 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。TDD：先写失败测试，再写生产代码。

**目标：** 切号默认先刷新登录票，再用新票写入本机 Cursor，使「本机」标在最新设备上。  
**架构：** 扩展 `Api.switch_account(..., refresh_first=True, kick_old_tool=False)`：刷新失败则不关 Cursor；成功后可选踢本工具旧客户端，再走现有写盘/重启，并把 `list_sessions` 合并进返回值。确认框增加两个默认勾选的复选框。  
**技术栈：** Python 3 unittest + 现有 pywebview 前端。不联网。  
**规格：** `dtb-cursor-tools/docs/superpowers/specs/2026-09-17-switch-refresh-then-pin-design.md`

**Commit 约定：** 仅当用户明确要求 commit 时执行 git commit；未要求则跳过。

---

## 文件

| 文件 | 职责 |
|------|------|
| `dtb-cursor-tools/test_switch_confirm.py` | 确认框文案：refresh_first 开/关 |
| `dtb-cursor-tools/switch_confirm.py` | `confirm_lines` 增加 refresh_first |
| `dtb-cursor-tools/test_switch_account.py` | 新建。刷新顺序、失败不关 Cursor、踢旧、list_sessions 失败仍 ok |
| `dtb-cursor-tools/app.py` | 扩展 `switch_account` |
| `dtb-cursor-tools/test_pin_local.py` | 预览桥：refresh_first 默认 bump；kick 丢掉本工具会话 |
| `dtb-cursor-tools/preview_server.py` | mock 四参切号 |
| `dtb-cursor-tools/web/index.html` | 确认框复选框 + 帮助一句 |
| `dtb-cursor-tools/web/app.js` | 四参调用、toast、回写设备块 |

不改：`login_detect.py` 认 IDE 规则、`device_guard_pin_local`、单独 `refresh_login_one` / `refresh_login_kick_old`。

探测失败追加句（固定）：`未写入 Cursor。可取消勾选「先刷新登录票」后仅切当前票。`

---

## 任务 1：确认框文案

**文件：** `dtb-cursor-tools/test_switch_confirm.py`、`dtb-cursor-tools/switch_confirm.py`

**步骤 1：把下面两个测试加进 `SwitchConfirmCopyTest`。**

```python
    def test_refresh_first_mentions_new_ticket_and_latest_device(self):
        lines = switch_confirm.confirm_lines("a@b.com", refresh_first=True)
        blob = "\n".join(lines)
        self.assertIn("新登录票", blob)
        self.assertIn("最新", blob)

    def test_refresh_first_off_uses_current_ticket(self):
        lines = switch_confirm.confirm_lines("a@b.com", refresh_first=False)
        blob = "\n".join(lines)
        self.assertIn("当前这张票", blob)
        self.assertNotIn("最新那台", blob)
```

**步骤 2：跑红灯。**

```bash
cd dtb-cursor-tools && .venv/bin/python -m unittest test_switch_confirm.SwitchConfirmCopyTest.test_refresh_first_mentions_new_ticket_and_latest_device test_switch_confirm.SwitchConfirmCopyTest.test_refresh_first_off_uses_current_ticket -v
```

预期：`confirm_lines() got an unexpected keyword argument 'refresh_first'`。

**步骤 3：实现最少文案。**

`confirm_lines` 增加 `refresh_first: bool = True`。在现有两句之后：

- True：`会先换新登录票，再用新票写入 Cursor，本机标签会落在最新那台设备上。`
- False：`不会换新登录票，写入的是列表里当前这张票。`

`confirm_message` 把该参数传下去。`kick_old_tool` 参数可接受但不追加句子。

**步骤 4：再跑上面两条，须 OK。**

---

## 任务 2：`switch_account` 顺序（核心）

**文件：** 创建 `dtb-cursor-tools/test_switch_account.py`；修改 `dtb-cursor-tools/app.py`

**步骤 1：写失败测试（完整文件如下）。**

```python
"""切号先刷票再写入：刷新失败不关 Cursor。不联网。"""

import unittest
from unittest.mock import MagicMock, patch

import sand_api


def _jwt(sub="user_01SWITCHREFRESH0000000000", typ="session", exp=1893456000):
    import base64
    import json
    payload = {"sub": sub, "type": typ, "exp": exp, "email": "a@b.com"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + encoded + ".x"


OLD = _jwt(exp=1893456000)
NEW = _jwt(exp=1999999999)
AID = "user_01SWITCHREFRESH0000000000"


class SwitchAccountRefreshFirstTest(unittest.TestCase):
    def setUp(self):
        import app
        self.app = app
        self.api = app.Api.__new__(app.Api)
        self.item = {"token": OLD, "refreshToken": "rt", "label": "a@b.com"}
        self.api._store = MagicMock()
        self.api._store.get.side_effect = lambda _id: dict(self.item)
        self.api.refresh_login_one = MagicMock()
        self.api.probe_refresh_one = MagicMock()
        self.api._drop_stale_tool_session_after_refresh = MagicMock(return_value="")
        self.api.list_sessions = MagicMock(
            return_value={
                "ok": True,
                "sessions": [{"sessionId": "sid-new", "type": "client", "localMark": "local"}],
                "sessionCount": 1,
                "sessionClientCount": 1,
                "sessionWebCount": 0,
                "sessionError": "",
            }
        )
        self.api._resolve_pinned_local_session = MagicMock(return_value="sid-new")
        self.layout = object()

    def _patch_write(self):
        return (
            patch.object(self.app.sand_api, "token_exp", return_value=1999999999),
            patch.object(self.app.sand_api, "probe_token_alive", return_value="alive"),
            patch.object(self.app.sand_patch, "resolve_cursor_layout", return_value=self.layout),
            patch.object(self.app.sand_patch, "close_cursor"),
            patch.object(self.app.local_cursor, "write_local_account"),
            patch.object(self.app.sand_patch, "start_cursor"),
        )

    def test_refresh_first_writes_new_token_then_closes_cursor(self):
        def do_refresh(_aid):
            self.item["token"] = NEW
            return {"ok": True, "tokenType": "session", "accessToken": NEW, "usedAccessAsRefresh": False}

        self.api.refresh_login_one.side_effect = do_refresh
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_write()
        with p_exp, p_alive, p_lay, p_close as close, p_write as write, p_start:
            res = self.api.switch_account(AID, False, True, False)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["refreshed"])
        self.api.refresh_login_one.assert_called_once_with(AID)
        close.assert_called_once()
        write.assert_called_once()
        self.assertEqual(write.call_args.kwargs.get("user_id") or write.call_args[1].get("user_id"), AID)
        written_jwt = write.call_args[0][0]
        self.assertEqual(written_jwt, NEW)
        self.assertEqual(res.get("pinnedSessionId"), "sid-new")
        self.assertEqual(res.get("sessionCount"), 1)
        self.assertNotIn("accessToken", res)
        self.api._drop_stale_tool_session_after_refresh.assert_not_called()

    def test_refresh_failure_does_not_close_cursor(self):
        self.api.refresh_login_one.return_value = {"ok": False, "error": "换票失败"}
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_write()
        with p_exp, p_alive, p_lay, p_close as close, p_write, p_start:
            res = self.api.switch_account(AID, False, True, False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "换票失败")
        close.assert_not_called()
        self.api.refresh_login_one.assert_called_once()

    def test_kick_old_after_successful_refresh(self):
        def do_refresh(_aid):
            self.item["token"] = NEW
            return {"ok": True, "accessToken": NEW}

        self.api.refresh_login_one.side_effect = do_refresh
        self.api._drop_stale_tool_session_after_refresh.return_value = "old-tool-sid"
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_write()
        with p_exp, p_alive, p_lay, p_close, p_write, p_start:
            res = self.api.switch_account(AID, False, True, True)
        self.assertTrue(res["ok"], res)
        self.api._drop_stale_tool_session_after_refresh.assert_called_once()
        self.assertEqual(res.get("droppedSessionId"), "old-tool-sid")

    def test_refresh_first_false_skips_oauth(self):
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_write()
        with p_exp, p_alive, p_lay, p_close, p_write as write, p_start:
            res = self.api.switch_account(AID, False, False, True)
        self.assertTrue(res["ok"], res)
        self.assertFalse(res.get("refreshed"))
        self.api.refresh_login_one.assert_not_called()
        self.api._drop_stale_tool_session_after_refresh.assert_not_called()
        self.assertEqual(write.call_args[0][0], OLD)

    def test_list_sessions_error_still_ok(self):
        def do_refresh(_aid):
            self.item["token"] = NEW
            return {"ok": True, "accessToken": NEW}

        self.api.refresh_login_one.side_effect = do_refresh
        self.api.list_sessions.side_effect = RuntimeError("waf")
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_write()
        with p_exp, p_alive, p_lay, p_close, p_write, p_start:
            res = self.api.switch_account(AID, False, True, False)
        self.assertTrue(res["ok"], res)
        self.assertIn("warning", res)
        self.assertTrue(res["warning"])
        self.assertEqual(res.get("pinnedSessionId") or "", "")

    def test_probe_failure_appends_hint_and_skips_refresh(self):
        self.item.pop("refreshToken")
        self.api.probe_refresh_one.return_value = {"ok": False, "error": "未能探测到 refresh_token"}
        p_exp, p_alive, p_lay, p_close, p_write, p_start = self._patch_write()
        with p_exp, p_alive, p_lay, p_close as close, p_write, p_start:
            res = self.api.switch_account(AID, False, True, False)
        self.assertFalse(res["ok"])
        self.assertIn("未能探测到 refresh_token", res["error"])
        self.assertIn("未写入 Cursor", res["error"])
        self.api.refresh_login_one.assert_not_called()
        close.assert_not_called()
```

注意：`write_local_account(jwt, email, refresh_token=..., user_id=...)` 第一位置参数是 jwt。`parse_token` 用真实 JWT：`sub` 必须是 `AID`。上面 `_jwt` 的 default sub 已对齐。

**步骤 2：跑红灯。**

```bash
cd dtb-cursor-tools && .venv/bin/python -m unittest test_switch_account -v
```

预期：`switch_account() takes from 2 to 3 positional arguments but 5 were given` 或 refreshed 字段缺失。

**步骤 3：改 `Api.switch_account`。**

签名：

```python
def switch_account(
    self,
    account_id: str,
    reset_machine_id: bool = False,
    refresh_first: bool = True,
    kick_old_tool: bool = False,
) -> dict:
```

`refresh_first=True` 且无 `refreshToken` → `probe_refresh_one`；失败返回 `error` 为原探测 error + 空格 + 固定追加句。  
然后 `refresh_login_one`；失败原样返回。  
`kick_old_tool` 时用刷新前 claims + 新 access 调 `_drop_stale_tool_session_after_refresh`。  
再 **重新** `get` + `parse_token`，走现有过期闸 / 探活 / web 兑换 / 关-写-开。  
成功返回增加 `refreshed`、`usedAccessAsRefresh`、`droppedSessionId`、`kickError`、`pinnedSessionId`、`warning`，合并 `list_sessions` 的 sessions 计数键；`list_sessions` 抛错则 `ok: True` 且 `warning` 非空。禁止把 `accessToken` 放进返回。

`refresh_first=False` 跳过探测/刷新/踢旧。

**步骤 4：绿灯。** `test_switch_account` 全部 OK，且 `test_refresh_token` / `test_pin_local.DeviceGuardPinLocalTest` 仍绿（pin-local 不得开始刷票）。

---

## 任务 3：预览桥

**文件：** `dtb-cursor-tools/test_pin_local.py`、`dtb-cursor-tools/preview_server.py`

在 `PreviewRpcTest`（或现有 preview 测试类）追加：

```python
    def test_switch_account_refresh_first_bumps_and_returns_sessions(self):
        ps = self.ps
        before = ps._SESSIONS[ps.DEMO_ID][0]["createdAt"]
        res = ps._rpc("switch_account", [ps.DEMO_ID, False, True, False])
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(res.get("refreshed"))
        self.assertGreater(ps._SESSIONS[ps.DEMO_ID][0]["createdAt"], before)
        self.assertIn("sessions", res)
        self.assertTrue(any(s.get("localMark") == "local" or s.get("freshMark") == "fresh" or s.get("toolMark") == "tool" for s in res["sessions"]))

    def test_switch_account_kick_old_drops_preview_tool(self):
        ps = self.ps
        tool = ps._SESSIONS_SEED[ps.DEMO_ID][1]["sessionId"]
        res = ps._rpc("switch_account", [ps.DEMO_ID, False, True, True])
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("droppedSessionId"), tool)
        ids = [s["sessionId"] for s in ps._SESSIONS[ps.DEMO_ID]]
        self.assertNotIn(tool, ids)

    def test_switch_account_refresh_first_false_does_not_bump(self):
        ps = self.ps
        before = ps._SESSIONS[ps.DEMO_ID][0]["createdAt"]
        res = ps._rpc("switch_account", [ps.DEMO_ID, False, False, True])
        self.assertTrue(res.get("ok"), res)
        self.assertFalse(res.get("refreshed"))
        self.assertEqual(ps._SESSIONS[ps.DEMO_ID][0]["createdAt"], before)
        self.assertFalse(res.get("droppedSessionId"))
```

红灯后再改 `preview_server.py` 的 `switch_account`：读 `args[2]`（缺省 True）、`args[3]`（缺省 False）。refresh_first 时先 bump（同 `refresh_login_one`）；kick_old 时同 `refresh_login_kick_old` 丢掉预览本工具会话。返回 `_session_block(aid)` 合并 `refreshed` / `droppedSessionId`。

---

## 任务 4：确认框 UI 与帮助

**文件：** `web/index.html`、`web/app.js`

`#switchConfirmBody` **后面**（不要写进 innerHTML，以免打开时冲掉）加：

```html
<label class="modal-check"><input type="checkbox" id="switchRefreshFirstChk" checked /> 先刷新登录票再写入本机 Cursor</label>
<label class="modal-check"><input type="checkbox" id="switchKickOldChk" checked /> 踢掉刷新前本工具的旧客户端（不会踢 Cursor IDE）</label>
```

`switchConfirmCopy(email, resetMid, webTok, refreshFirst)` 句子与 Python 模块完全一致。

打开确认框时两框重置为勾选；「先刷新」未勾则禁用踢旧。`change` 时重绘说明句。

`switch_account(id, resetMid, refreshFirst, kickOld)`。成功 toast：已切号 + 若 refreshed「已换新登录票」+ 若 droppedSessionId「已踢旧客户端」+ 若 pinnedSessionId「本机已标在最新设备」。`applySessionBlock(id, res)` 若带 `sessionCount`。然后 `refreshLocalIdentity()` + `render()`。

帮助「探测 Refresh / 刷新登录票」那条末尾加：切号默认先换新票再写入本机 Cursor；取消勾选「先刷新登录票」则只切列表里当前这张票。

---

## 任务 5：全量验证

```bash
cd dtb-cursor-tools && .venv/bin/python -m unittest discover -s . -p 'test_*.py' -q
```

预期：全部 OK，比开工前多 8 条左右（文案 2 + switch_account 6 + preview 3，以实际为准）。

---

## 规格覆盖

| 规格 | 任务 |
|------|------|
| §3.1 switch_account 四参与顺序 | 2 |
| §3.1 刷新失败不关 Cursor | 2 |
| §3.1 踢旧不阻断 | 2 |
| §3.1 list_sessions 合并 / warning | 2 |
| §3.2 前端四参与 toast | 4 |
| §3.3 预览 | 3 |
| §4 确认框文案与 DOM | 1、4 |
| §5 探测失败追加句 | 2 |
| §6 测试表 | 1–3、5 |
| 单独刷票行为不变 | 2 步骤 4 回归 |
