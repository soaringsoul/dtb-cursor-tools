"""本机设备保护：每隔一段时间拉一次该账号的云端登录会话，自动踢掉不在「保留名单」里的设备。

用法（app.py）：
    guard = DeviceGuardManager(os.path.join(_STATE_DIR, "device_guard.json"))
    guard.start(account_id, user_id, jwt, keep_ids, interval_minutes=1)  # 每账号一轮；间隔按分钟
    guard.stop(account_id) / guard.stop_all()
    guard.status()                                     # account_id -> 运行状态（给 UI 轮询）

一轮 = fetch_sessions → 列表里每个 sessionId 不在保留名单的都 revoke_session（官网 body）。
开启之后新冒出来的设备自然也不在名单里，同样会被踢。sand_api 只负责 HTTP，这里只负责循环与状态。

安全边界：
  - 保留名单为空一律拒绝启动（否则会把包括本机在内的所有设备全踢掉）；每轮踢之前再核一次。
  - 同一账号同时只跑一个循环：重复 start 会先叫停旧循环，新线程等旧线程完全退出后才开始。
  - 每轮任何异常都只记到 lastError，线程不会因此死掉；拉会话失败的那一轮不做任何踢下线。
  - 踢下线请求与官网 Revoke 一致：`{"session_id", "type": 数字}`。HTTP 200 不等于真踢掉
    （错误 body 也回 `{}`）；同一轮会再拉一次列表，只有设备消失才计入已踢，否则短间隔重试。
  - 连续 AUTH_FAIL_LIMIT 轮 401/403（本工具用的这张票自己失效了）→ 自动停止，不拿死票反复打接口。
  - 落盘只记「保留名单 + 检测间隔 + 上次是否在跑 + 已踢数」，程序重启后绝不自动恢复踢人，只用来回填勾选与间隔。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

import sand_api

TICK_SECONDS = 30.0
DEFAULT_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 120
SECONDS_PER_MINUTE = 60.0
REVOKE_COOLDOWN = 30.0
REVOKE_RETRY = 5.0
AUTH_FAIL_LIMIT = 30
LAST_KICKED_MAX = 8

_AUTH_ERROR_RE = re.compile(r"HTTP\s*(401|403)\b")


def _now() -> float:
    return time.time()


def clean_ids(ids) -> list:
    """去空、去重、转字符串，保持原顺序。"""
    out: list = []
    seen: set = set()
    for value in ids or []:
        sid = str(value or "").strip()
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def clean_interval_minutes(value, default: int = DEFAULT_INTERVAL_MINUTES) -> int:
    """检测间隔：整数分钟，夹在 1–MAX_INTERVAL_MINUTES。非法值回退 default。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        try:
            n = int(default)
        except (TypeError, ValueError):
            n = DEFAULT_INTERVAL_MINUTES
    if n < 1:
        n = 1
    if n > MAX_INTERVAL_MINUTES:
        n = MAX_INTERVAL_MINUTES
    return n


class _Guard:
    """单个账号的保护循环状态。字段读写统一在 DeviceGuardManager._lock 里进行。"""

    def __init__(
        self,
        account_id: str,
        user_id: str,
        jwt: str,
        keep_ids: list,
        predecessor=None,
        tick_seconds: float = TICK_SECONDS,
        interval_minutes=None,
    ) -> None:
        self.account_id = account_id
        self.user_id = user_id
        self.jwt = jwt
        self.keep_ids = set(keep_ids)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        # 上一个同账号的线程：新循环先等它退出，保证同一账号绝不重叠两个循环。
        self.predecessor = predecessor
        self.tick_seconds = max(0.01, float(tick_seconds))
        self.interval_minutes = None if interval_minutes is None else int(interval_minutes)
        # 账号已被删除：线程退出时不再留「最后一帧」，status() 也不再报它。
        self.forgotten = False
        self.started_at = _now()
        self.stopped_at = None
        self.stop_reason = ""
        self.tick_count = 0
        self.last_tick_at = None
        self.last_error = ""
        self.kicked_ids: set = set()
        self.last_kicked: list = []
        self.session_count = None
        self.next_revoke_at: dict = {}
        self.auth_failures = 0

    def snapshot(self, running: bool) -> dict:
        alive = bool(self.thread and self.thread.is_alive())
        out = {
            "running": running,
            "stopping": (not running) and alive,
            "keepIds": sorted(self.keep_ids),
            "startedAt": self.started_at,
            "stoppedAt": self.stopped_at,
            "stopReason": self.stop_reason,
            "tickCount": self.tick_count,
            "lastTickAt": self.last_tick_at,
            "lastError": self.last_error,
            "kickedCount": len(self.kicked_ids),
            "lastKicked": list(self.last_kicked),
            "sessionCount": self.session_count,
        }
        if self.interval_minutes is not None:
            out["intervalMinutes"] = int(self.interval_minutes)
        return out


class DeviceGuardManager:
    """管理多个账号的保护循环；fetch_sessions / revoke_session 可注入以便单测（不联网）。"""

    def __init__(
        self,
        state_path: str | None = None,
        fetch_sessions=None,
        revoke_session=None,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self._lock = threading.RLock()
        self._guards: dict = {}
        self._finished: dict = {}
        self._state_path = state_path
        self._fetch = fetch_sessions or sand_api.fetch_sessions
        self._revoke = revoke_session or sand_api.revoke_session
        self._tick_seconds = max(0.01, float(tick_seconds))
        self._saved: dict = self._load()

    # ---- 持久化：只存名单与统计，不存 token ----

    def _load(self) -> dict:
        if not self._state_path:
            return {}
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict = {}
        for account_id, row in data.items():
            if not isinstance(row, dict):
                continue
            keep = clean_ids(row.get("keepIds"))
            if not keep:
                continue
            out[str(account_id)] = {
                "keepIds": keep,
                "updatedAt": row.get("updatedAt"),
                "kickedCount": int(row.get("kickedCount") or 0),
                # 上次退出时保护是否开着：只用于 UI 提示「上次开着，需重新启动」，绝不自动恢复。
                "wasRunning": bool(row.get("running")),
            }
            if row.get("intervalMinutes") is not None:
                out[str(account_id)]["intervalMinutes"] = clean_interval_minutes(row.get("intervalMinutes"))
        return out

    def _save(self) -> None:
        if not self._state_path:
            return
        data = {}
        for account_id, row in self._saved.items():
            guard = self._guards.get(account_id)
            entry = {
                "keepIds": list(row.get("keepIds") or []),
                "updatedAt": row.get("updatedAt"),
                "kickedCount": int(row.get("kickedCount") or 0),
                "running": bool(guard is not None and not guard.stop_event.is_set()),
            }
            if row.get("intervalMinutes") is not None:
                entry["intervalMinutes"] = clean_interval_minutes(row.get("intervalMinutes"))
            data[account_id] = entry
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(tmp, self._state_path)
        except Exception:
            pass

    # ---- 对外：启动 / 停止 / 状态 ----

    def saved_keep_ids(self, account_id: str) -> list:
        with self._lock:
            row = self._saved.get(str(account_id or ""))
            return list(row.get("keepIds") or []) if row else []

    def is_running(self, account_id: str) -> bool:
        with self._lock:
            guard = self._guards.get(str(account_id or ""))
            return bool(guard is not None and not guard.stop_event.is_set())

    def start(self, account_id: str, user_id: str, jwt: str, keep_ids, interval_minutes=None) -> dict:
        """开启保护。名单为空拒绝；同账号已在跑则先叫停旧循环再接上新名单（不重叠）。

        interval_minutes 为 None 时沿用 manager 的 tick_seconds（单测用短间隔）。
        传入数值则按分钟（1–120）换算等待时间。
        """
        account_id = str(account_id or "").strip()
        keep = clean_ids(keep_ids)
        if not account_id:
            return {"ok": False, "error": "账号不存在"}
        if not user_id or not jwt:
            return {"ok": False, "error": "账号缺少登录票，无法拉取设备列表"}
        if not keep:
            return {
                "ok": False,
                "error": "保留名单为空：至少勾选一台要保留的设备，否则会把所有设备（含本机）全部踢下线",
            }
        if interval_minutes is None:
            minutes = None
            tick = self._tick_seconds
        else:
            minutes = clean_interval_minutes(interval_minutes)
            tick = max(0.01, float(minutes) * SECONDS_PER_MINUTE)
        with self._lock:
            old = self._guards.get(account_id)
            predecessor = None
            if old is not None:
                old.stop_event.set()
                old.stop_reason = old.stop_reason or "restart"
                predecessor = old.thread
            guard = _Guard(
                account_id,
                user_id,
                jwt,
                keep,
                predecessor=predecessor,
                tick_seconds=tick,
                interval_minutes=minutes,
            )
            guard.thread = threading.Thread(
                target=self._run, args=(guard,), name=f"device-guard-{account_id}", daemon=True
            )
            self._guards[account_id] = guard
            self._finished.pop(account_id, None)
            saved = {"keepIds": keep, "updatedAt": _now(), "kickedCount": 0, "wasRunning": False}
            if minutes is not None:
                saved["intervalMinutes"] = minutes
            self._saved[account_id] = saved
            self._save()
            guard.thread.start()
            return {"ok": True, "status": guard.snapshot(running=True)}

    def stop(self, account_id: str, wait: bool = False, timeout: float = 30.0) -> dict:
        """叫停某账号的保护。wait=True 时等线程真正退出（单测 / 退出程序用）。"""
        account_id = str(account_id or "").strip()
        with self._lock:
            guard = self._guards.get(account_id)
            if guard is None:
                return {"ok": True, "running": False, "wasRunning": False}
            was_running = not guard.stop_event.is_set()
            guard.stop_event.set()
            if was_running:
                guard.stop_reason = guard.stop_reason or "user"
            self._save()
            thread = guard.thread
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        with self._lock:
            snap = guard.snapshot(running=False)
        return {"ok": True, "running": False, "wasRunning": was_running, "status": snap}

    def stop_all(self, wait: bool = False, timeout: float = 30.0) -> dict:
        with self._lock:
            ids = list(self._guards.keys())
        stopped = [aid for aid in ids if self.stop(aid, wait=wait, timeout=timeout).get("wasRunning")]
        return {"ok": True, "stopped": stopped}

    def forget(self, account_id: str) -> None:
        """账号被删除时：叫停并清掉它的名单记忆。"""
        account_id = str(account_id or "").strip()
        self.stop(account_id)
        with self._lock:
            guard = self._guards.get(account_id)
            if guard is not None:
                guard.forgotten = True
            self._saved.pop(account_id, None)
            self._finished.pop(account_id, None)
            self._save()

    def status(self) -> dict:
        """account_id -> 状态。在跑的给实时快照；跑完的给最后一帧；只有记忆的给名单 + running=False。"""
        with self._lock:
            out: dict = {}
            for account_id, guard in self._guards.items():
                if guard.forgotten:
                    continue
                out[account_id] = guard.snapshot(running=not guard.stop_event.is_set())
            for account_id, snap in self._finished.items():
                if account_id not in out:
                    out[account_id] = dict(snap)
            for account_id, row in self._saved.items():
                if account_id in out:
                    continue
                out[account_id] = {
                    "running": False,
                    "stopping": False,
                    "keepIds": list(row.get("keepIds") or []),
                    "startedAt": None,
                    "stoppedAt": None,
                    "stopReason": "",
                    "tickCount": 0,
                    "lastTickAt": None,
                    "lastError": "",
                    "kickedCount": int(row.get("kickedCount") or 0),
                    "lastKicked": [],
                    "sessionCount": None,
                    "wasRunning": bool(row.get("wasRunning")),
                    "saved": True,
                }
                if row.get("intervalMinutes") is not None:
                    out[account_id]["intervalMinutes"] = int(row["intervalMinutes"])
            return out

    # ---- 循环 ----

    def _run(self, guard: _Guard) -> None:
        try:
            pred = guard.predecessor
            if pred is not None and pred is not threading.current_thread():
                pred.join()
            while not guard.stop_event.is_set():
                self._tick(guard)
                if guard.stop_event.is_set():
                    break
                guard.stop_event.wait(guard.tick_seconds)
        finally:
            with self._lock:
                guard.stop_event.set()
                guard.stopped_at = _now()
                if not guard.stop_reason:
                    guard.stop_reason = "exit"
                # 被 restart 顶掉的旧循环不再是「当前」循环：不覆盖新循环的记忆与最后一帧。
                if self._guards.get(guard.account_id) is guard:
                    del self._guards[guard.account_id]
                    if not guard.forgotten:
                        snap = guard.snapshot(running=False)
                        snap["stopping"] = False
                        self._finished[guard.account_id] = snap
                        row = self._saved.get(guard.account_id)
                        if row is not None:
                            row["kickedCount"] = len(guard.kicked_ids)
                            row["updatedAt"] = _now()
                self._save()

    def _tick(self, guard: _Guard) -> None:
        """一轮：拉会话 → 踢掉不在保留名单里的。任何异常只记 lastError，不抛。"""
        try:
            self._tick_inner(guard)
        except Exception as exc:
            with self._lock:
                guard.last_error = f"{type(exc).__name__}: {exc}"

    def _tick_inner(self, guard: _Guard) -> None:
        now = _now()
        try:
            block = self._fetch(guard.user_id, guard.jwt)
        except Exception as exc:
            block = sand_api.empty_session_block(f"{type(exc).__name__}: {exc}")
        if not isinstance(block, dict):
            block = sand_api.empty_session_block("会话数据无法解析")
        error = str(block.get("sessionError") or "")

        with self._lock:
            guard.tick_count += 1
            guard.last_tick_at = now
            if error:
                guard.last_error = error
                if _AUTH_ERROR_RE.search(error):
                    guard.auth_failures += 1
                    if guard.auth_failures >= AUTH_FAIL_LIMIT:
                        guard.stop_reason = "auth"
                        guard.last_error = (
                            f"登录态已失效（连续 {AUTH_FAIL_LIMIT} 轮 401/403），保护已自动停止；"
                            "请重新导入该号的新 token 后再开启"
                        )
                        guard.stop_event.set()
                return
            guard.auth_failures = 0
            sessions = [row for row in (block.get("sessions") or []) if isinstance(row, dict)]
            guard.session_count = len(sessions)
            present = {str(row.get("sessionId") or "") for row in sessions}
            for sid in [s for s in guard.next_revoke_at if s not in present]:
                del guard.next_revoke_at[sid]
            keep = set(guard.keep_ids)
            targets = []
            if keep:  # 安全闸：名单为空这一轮什么都不踢
                for row in sessions:
                    sid = str(row.get("sessionId") or "").strip()
                    if not sid or sid in keep:
                        continue
                    if guard.next_revoke_at.get(sid, 0) > now:
                        continue
                    targets.append(row)

        kicked = []
        errors = []
        submitted = []
        for row in targets:
            if guard.stop_event.is_set():
                break
            sid = str(row.get("sessionId") or "").strip()
            try:
                res = self._revoke(guard.user_id, guard.jwt, sid, row.get("typeRaw") or row.get("type"))
            except Exception as exc:
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            ok = bool(isinstance(res, dict) and res.get("ok"))
            with self._lock:
                guard.next_revoke_at[sid] = _now() + REVOKE_RETRY
                if ok:
                    submitted.append((sid, row))
                else:
                    detail = (res or {}).get("error") if isinstance(res, dict) else ""
                    errors.append(f"{sid[:8]}：{detail or '踢下线失败'}")

        disappeared = set()
        if submitted and not guard.stop_event.is_set():
            try:
                block2 = self._fetch(guard.user_id, guard.jwt)
            except Exception as exc:
                block2 = sand_api.empty_session_block(f"{type(exc).__name__}: {exc}")
            if not isinstance(block2, dict):
                block2 = sand_api.empty_session_block("会话数据无法解析")
            check_error = str(block2.get("sessionError") or "")
            if check_error:
                errors.append("踢后复检失败：" + check_error)
            else:
                still = {
                    str(item.get("sessionId") or "")
                    for item in (block2.get("sessions") or [])
                    if isinstance(item, dict)
                }
                disappeared = {sid for sid, _row in submitted if sid and sid not in still}
                with self._lock:
                    guard.session_count = len(still)

        with self._lock:
            now2 = _now()
            for sid, row in submitted:
                if sid in disappeared:
                    guard.kicked_ids.add(sid)
                    kicked.append(
                        {
                            "sessionId": sid,
                            "type": row.get("type") or "other",
                            "createdAt": row.get("createdAt"),
                            "at": now2,
                        }
                    )
                    guard.next_revoke_at.pop(sid, None)
                else:
                    guard.next_revoke_at[sid] = now2 + REVOKE_RETRY
                    errors.append(f"{sid[:8]}：已提交但设备仍在列表，将重试")
            if kicked:
                guard.last_kicked = (list(reversed(kicked)) + guard.last_kicked)[:LAST_KICKED_MAX]
                row = self._saved.get(guard.account_id)
                if row is not None:
                    row["kickedCount"] = len(guard.kicked_ids)
                    row["updatedAt"] = now2
                self._save()
            guard.last_error = "；".join(errors)
