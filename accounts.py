"""账号导入与存储。

支持两种 token 文本格式：
  - access_token（JWT，eyJ...）
  - ws token（user_01XXX::eyJ...，即 WorkosCursorSessionToken）
  - 以及带邮箱的号池/导出行：邮箱----user_01XXX::eyJ...（导入时直接把邮箱认出来）
以及 JSON 文件（号池导出，如 cursor_accounts_*.json，字段名兼容 access_token/accessToken/token/session_token 等）。
按 user id 去重；同一账号有多个字段时保留优先级最高的可用 token（access/ws > session > token > refresh）。
每个账号记录首次导入时间 addedAt（秒级时间戳），重复导入不覆盖。

账号会持久化到磁盘（LOCALAPPDATA\\SandClaimer\\accounts.json），下次打开自动加载，无需重复导入。
token 属凭据，用 Windows DPAPI 加密后落盘（绑定当前 Windows 用户，文件拷到别处也解不开）；
DPAPI 不可用时回退明文，保证持久化仍然生效。
"""

import base64
import ctypes
import json
import os
import re
import threading
import time
from ctypes import wintypes

from sand_api import parse_token

# ws token（含 :: 或 %3A%3A）优先，其次裸 JWT。
WS_RE = re.compile(r"user_[A-Za-z0-9]+(?:::|%3A%3A)eyJ[A-Za-z0-9_.\-]+")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")
# 导出行 / 号池常见格式：邮箱----token（导入时顺手把邮箱认出来，不用等联网刷新）。
LABELED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s*-{2,}\s*"
    r"(user_[A-Za-z0-9]+(?:::|%3A%3A)eyJ[A-Za-z0-9_.\-]+|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,})"
)

# 字段名 -> 优先级：access_token / ws token 才是调 API 能用的；refresh_token 最低，绝不能覆盖 access。
_TOKEN_PRIORITY = {
    "access_token": 5,
    "accesstoken": 5,
    "ws_token": 5,
    "wstoken": 5,
    "workoscursorsessiontoken": 5,
    "session_token": 4,
    "sessiontoken": 4,
    "token": 3,
    "refresh_token": 1,
    "refreshtoken": 1,
}


def _store_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "SandClaimer", "accounts.json")


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool):
    """Windows DPAPI 加/解密。protect=True 加密，False 解密。失败返回 None。"""
    if os.name != "nt":
        return None
    try:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def _extract_from_obj(obj, out: list) -> None:
    """递归从任意 JSON 结构里抽取 (优先级, token 字符串)。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            prio = _TOKEN_PRIORITY.get(key.lower())
            if isinstance(value, str) and prio is not None:
                out.append((prio, value))
            else:
                _extract_from_obj(value, out)
    elif isinstance(obj, list):
        for item in obj:
            _extract_from_obj(item, out)


def format_export_line(item: dict) -> str:
    """导出一行：邮箱----user_id::jwt（无邮箱则只出 user_id::jwt）。可原样粘回导入。"""
    raw = (item or {}).get("token") or ""
    user_id, jwt, _claims = parse_token(raw)
    label = (item or {}).get("label") or ""
    email = label if "@" in label else ""
    body = f"{user_id}::{jwt}"
    return f"{email}----{body}" if email else body


def labels_from_text(text: str) -> dict:
    """从「邮箱----token」行里认出 token -> 邮箱 的对应关系（导入时离线回填邮箱）。"""
    out: dict = {}
    for m in LABELED_RE.finditer(text or ""):
        out[m.group(2)] = m.group(1)
    return out


def tokens_from_text(text: str) -> list:
    """从纯文本抽取 (优先级, token)。ws / 裸 JWT 都按最高优先级。"""
    out = [(5, m.group(0)) for m in WS_RE.finditer(text)]
    out.extend((5, m.group(0)) for m in JWT_RE.finditer(text))
    return out


def tokens_from_json_text(text: str) -> list:
    out: list = []
    try:
        _extract_from_obj(json.loads(text), out)
    except Exception:
        pass
    return out


class AccountStore:
    """账号表，key 为 user id，天然去重；自动持久化到磁盘。"""

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        path = _store_path()
        try:
            with open(path, "rb") as handle:
                envelope = json.loads(handle.read().decode("utf-8"))
        except Exception:
            return
        items = []
        if isinstance(envelope, dict) and envelope.get("enc") == "dpapi":
            blob = base64.b64decode(envelope.get("data", ""))
            dec = _dpapi(blob, protect=False)
            if dec:
                try:
                    items = json.loads(dec.decode("utf-8"))
                except Exception:
                    items = []
        elif isinstance(envelope, dict):
            items = envelope.get("items", [])
        for it in items:
            uid = it.get("id")
            token = it.get("token")
            if not uid or not token:
                continue
            # 旧数据可能没存到期时间：直接从已保存的 token（JWT）里补解析出来，无需重新导入。
            exp = it.get("exp")
            token_type = it.get("tokenType")
            if not isinstance(exp, (int, float)) or not token_type:
                try:
                    _uid, _jwt, claims = parse_token(token)
                    if not isinstance(exp, (int, float)):
                        cand = claims.get("exp")
                        exp = cand if isinstance(cand, (int, float)) else None
                    if not token_type:
                        token_type = claims.get("type")
                except Exception:
                    pass
            added_at = it.get("addedAt")
            self._items[uid] = {
                "id": uid,
                "label": it.get("label") or uid,
                "token": token,
                "_prio": it.get("_prio", 5),
                "exp": exp,
                "tokenType": token_type,
                # 旧数据没有导入时间就保持 None（UI 显示「—」），不伪造。
                "addedAt": added_at if isinstance(added_at, (int, float)) else None,
            }

    def _save(self) -> None:
        path = _store_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            payload = json.dumps(list(self._items.values()), ensure_ascii=False).encode("utf-8")
            enc = _dpapi(payload, protect=True)
            if enc is not None:
                envelope = {"v": 1, "enc": "dpapi", "data": base64.b64encode(enc).decode("ascii")}
            else:
                envelope = {"v": 1, "enc": "none", "items": list(self._items.values())}
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            pass

    # ---- 导入 ----

    def _add_token(self, token: str, priority: int = 5, label_hint: str | None = None):
        try:
            user_id, _jwt, claims = parse_token(token)
        except Exception:
            return None
        existing = self._items.get(user_id)
        # 只在「更高或同等优先级」时覆盖，避免 refresh_token 覆盖 access_token。
        if existing is None or priority >= existing.get("_prio", 0):
            old_label = existing.get("label") if existing else None
            old_is_email = bool(old_label and "@" in old_label)
            # 邮箱优先级：文本里「邮箱----token」显式给的 > JWT 声明 > 已有邮箱 > user_id。
            label = label_hint or claims.get("email") or (old_label if old_is_email else None) or user_id
            # 从 JWT 的 exp 声明取到期时间（秒级时间戳）；缺失时沿用旧值。添加账号时即解析，无需联网。
            exp = claims.get("exp")
            if not isinstance(exp, (int, float)):
                exp = existing.get("exp") if existing else None
            self._items[user_id] = {
                "id": user_id,
                "label": label,
                "token": token.strip(),
                "_prio": priority,
                "exp": exp,
                "tokenType": claims.get("type"),
                # 导入时间只在首次加入时记录；重复导入/换 token 不改，保留「什么时候进的列表」。
                "addedAt": (existing.get("addedAt") if existing else None) or int(time.time()),
            }
        elif existing is not None and label_hint and "@" not in (existing.get("label") or ""):
            existing["label"] = label_hint
        return self._items[user_id]

    def _ingest(self, pairs: list, labels: dict | None = None) -> list:
        """pairs: [(优先级, token)]；labels: token -> 邮箱。按 user id 去重，返回本次涉及的唯一账号列表。"""
        labels = labels or {}
        touched: dict[str, dict] = {}
        with self._lock:
            for prio, token in pairs:
                item = self._add_token(token, prio, labels.get(token))
                if item:
                    touched[item["id"]] = item
            if touched:
                self._save()
        return [{"id": v["id"], "label": v["label"]} for v in touched.values()]

    def add_text(self, text: str) -> list:
        stripped = (text or "").strip()
        # 粘贴的是 JSON 时先按字段名解析（才能用优先级挑 access_token）；否则走正则。
        if stripped[:1] in "{[":
            pairs = tokens_from_json_text(text) or tokens_from_text(text)
        else:
            pairs = tokens_from_text(text) or tokens_from_json_text(text)
        return self._ingest(pairs, labels_from_text(text))

    def add_json_files(self, paths: list) -> list:
        pairs: list = []
        labels: dict = {}
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8-sig") as handle:
                    text = handle.read()
            except Exception:
                continue
            pairs.extend(tokens_from_json_text(text) or tokens_from_text(text))
            labels.update(labels_from_text(text))
        return self._ingest(pairs, labels)

    # ---- 读取 / 修改 ----

    def set_label(self, account_id: str, label: str) -> None:
        with self._lock:
            item = self._items.get(account_id)
            if item and label and item.get("label") != label:
                item["label"] = label
                self._save()

    def list(self) -> list:
        return [
            {
                "id": v["id"],
                "label": v["label"],
                "exp": v.get("exp"),
                "tokenType": v.get("tokenType"),
                "addedAt": v.get("addedAt"),
            }
            for v in self._items.values()
        ]

    def get(self, account_id: str):
        return self._items.get(account_id)

    def remove(self, account_id: str) -> None:
        with self._lock:
            if self._items.pop(account_id, None) is not None:
                self._save()

    def remove_many(self, account_ids) -> int:
        """批量移除，返回实际删掉的个数；只落盘一次。"""
        removed = 0
        with self._lock:
            for account_id in account_ids or []:
                if self._items.pop(str(account_id), None) is not None:
                    removed += 1
            if removed:
                self._save()
        return removed

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._save()
