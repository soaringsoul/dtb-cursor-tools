"use strict";

// 与 Python 侧 Api 通过 window.pywebview.api 通信；批量操作用并发池并行驱动，逐行更新状态。
//
// 三个动作各司其职：
//   导入      → 自动「验证账号」（邮箱 / 套餐 / 订阅剩余 / 三池用量），不领取
//   验证账号  → 判定 token 是否有效（过期 / 401 / 403 = 失效）+ 刷新信息，不领取
//   批量领取  → 领 Sand（Grok Bot）资格，领完只轻量刷 Bot 周用量这一池
//
// 行内账号运维动作：
//   进控制台 / 查看设备 / 本机保护  → 与批量领取、验证互不影响，忙碌时也可用
//   一键本机保护 → 须本机正登录该号；用 Cursor JWT 认 IDE，必要时连同本工具会话一起保留；不刷票认设备；忙碌时禁用
//   本机保护  → 勾选保留设备，可立即删除未勾选；可同时保护多个账号；启动后按间隔自动下线未保留设备

let accounts = [];
const rowState = {}; // id -> 行状态：kind + 有效性 + Bot/Auto/高级 三池 + 订阅
const selected = new Set(); // 勾选的账号 id；为空表示「验证 / 领取」对全部生效
let busy = false;
let settings = {}; // settings.json：hideHelp / hideNotice / autoVerify / mainTab / loginDetectSec / importOpen
let listQuery = "";
let listFilter = "all";
let listSort = { key: "added", dir: 1 };
let helpJobs = [];
let lastPersisted = {}; // 上次落盘的稳定状态，避免瞬时失败把已保存的数据冲掉
let localUserId = null; // 本机 Cursor 当前登录的 user_ id；未登录为 null
let guardStatus = {}; // id -> device_guard_status() 的一项；running=true 表示该号的保护线程在跑
let tokenViews = {}; // id -> {worksessionToken, refreshToken}
const tokenOpenIds = new Set(); // 单行展开；工具栏「显示 Token」打开时全部展开
const pinBusyIds = new Set(); // 一键本机保护进行中的账号，防止连点
let apiKeys = []; // {id, apiKeyName, userEmail, userId, addedAt}，不含完整密钥
const agentLists = {}; // keyId -> {loading, error, agents}
let menuKind = ""; // ticket | row；登录票弹窗点「显示 Token」时要留着并刷新

const $ = (id) => document.getElementById(id);

function api() {
  return window.pywebview && window.pywebview.api;
}

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), 2600);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function fmtPercent(p) {
  if (p == null || isNaN(p)) return "";
  const v = Math.max(0, Number(p));
  return (v < 1 && v > 0 ? v.toFixed(2) : v.toFixed(1)) + "%";
}

function fmtUsd(v) {
  if (v == null || isNaN(v)) return "";
  const n = Number(v);
  return "$" + (n >= 100 ? n.toFixed(0) : n.toFixed(2));
}

function centsToUsd(c) {
  if (c == null || isNaN(c)) return "";
  return fmtUsd(Number(c) / 100);
}

// 秒 / 毫秒时间戳 / ISO 字符串 -> 毫秒；解析失败 NaN。
function toMs(v) {
  if (v == null || v === "") return NaN;
  if (typeof v === "number" || /^\d+(\.\d+)?$/.test(String(v))) {
    let n = Number(v);
    if (n < 1e12) n *= 1000;
    return n;
  }
  const t = Date.parse(v);
  return isNaN(t) ? NaN : t;
}

// 精确到分：YYYY-MM-DD HH:MM（本地时区）。
function fmtTs(ms) {
  if (ms == null || isNaN(ms)) return "";
  const d = new Date(ms);
  if (isNaN(d.getTime())) return "";
  const p2 = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
}

function fmtReset(v) {
  return fmtTs(toMs(v));
}

// 短格式：MM-DD HH:MM（表格里省空间用）。
function fmtTsShort(ms) {
  if (ms == null || isNaN(ms)) return "";
  const d = new Date(ms);
  if (isNaN(d.getTime())) return "";
  const p2 = (x) => String(x).padStart(2, "0");
  return `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
}

// 账号到期时间：来自 token（JWT）的 exp 声明，秒级时间戳。添加账号时即解析。
function expMs(exp) {
  return toMs(exp);
}

// token（登录凭证）到期：来自 JWT 的 exp（秒级），约 60 天有效——这不是订阅到期。
function fmtExpiry(exp) {
  return fmtTs(expMs(exp));
}

function relRemain(ms) {
  if (ms <= 0) return "已到期";
  const days = Math.floor(ms / 86400000);
  const hours = Math.floor((ms % 86400000) / 3600000);
  const mins = Math.floor((ms % 3600000) / 60000);
  if (days > 0) return `剩 ${days}天${hours}小时`;
  if (hours > 0) return `剩 ${hours}小时${mins}分`;
  return `剩 ${mins}分`;
}

// 真实订阅到期/续费日：优先“待取消日”（真会停），否则本计费周期结束（续费日）。ISO 字符串。
function subEndMs(st) {
  if (!st) return NaN;
  const v = st.pendingCancellationDate || st.billingCycleEnd;
  if (!v) return NaN;
  const t = Date.parse(v);
  return isNaN(t) ? NaN : t;
}

// 「剩余时间」统一口径：订阅到期优先，其次 token 到期；都没有 NaN。列表排序与导出排序都用它。
function remainMs(a, st) {
  const sub = subEndMs(st);
  if (!isNaN(sub)) return sub - Date.now();
  const t = expMs(a && a.exp);
  return isNaN(t) ? NaN : t - Date.now();
}

const EXPIRING_WINDOW_MS = 7 * 86400000;

function isBotFull(st) {
  if (!st) return false;
  if (st.hasAvailableUsage === false) return true;
  return st.percent != null && Number(st.percent) >= 100;
}

function isExpiring(a, st) {
  if (st && st.alive === false) return false;
  const r = remainMs(a, st);
  return !isNaN(r) && r > 0 && r <= EXPIRING_WINDOW_MS;
}

function claimVisible(st) {
  return !(st && st.kind === "ok");
}

function membershipKey(st) {
  return String((st && st.membership) || "")
    .toLowerCase()
    .replace(/\+/g, "plus")
    .replace(/[^a-z0-9]/g, "");
}

function isPaidPlan(st) {
  const key = membershipKey(st);
  return key === "pro" || key === "proplus" || key === "ultra" || key === "enterprise" || key === "team" || key === "business";
}

function isFreePlan(st) {
  const key = membershipKey(st);
  return key === "free" || key === "freetrial";
}

function accountMatches(a) {
  const q = (listQuery || "").trim().toLowerCase();
  if (q) {
    const blob = ((a.label || "") + " " + (a.id || "")).toLowerCase();
    if (!blob.includes(q)) return false;
  }
  const st = rowState[a.id];
  const guarding = !!(guardStatus[a.id] && guardStatus[a.id].running);
  if (listFilter === "local") return !!(localUserId && a.id === localUserId);
  if (listFilter === "guarding") return guarding;
  if (listFilter === "dead") return !!(st && st.alive === false);
  if (listFilter === "card") return !!(st && st.kind === "card");
  if (listFilter === "botFull") return isBotFull(st);
  if (listFilter === "expiring") return isExpiring(a, st);
  if (listFilter === "paid") return isPaidPlan(st);
  if (listFilter === "free") return isFreePlan(st);
  return true;
}

function compareAccounts(a, b, ia, ib) {
  const key = listSort.key;
  if (key === "remain") {
    const da = rowState[a.id] && rowState[a.id].alive === false ? 1 : 0;
    const db = rowState[b.id] && rowState[b.id].alive === false ? 1 : 0;
    if (da !== db) return da - db;
    const ra = remainMs(a, rowState[a.id]);
    const rb = remainMs(b, rowState[b.id]);
    const ma = isNaN(ra) ? 1 : 0;
    const mb = isNaN(rb) ? 1 : 0;
    if (ma !== mb) return ma - mb;
    if (!ma && ra !== rb) return ra - rb;
    return ia - ib;
  }
  if (key === "bot") {
    const pa = rowState[a.id] && rowState[a.id].percent != null ? Number(rowState[a.id].percent) : NaN;
    const pb = rowState[b.id] && rowState[b.id].percent != null ? Number(rowState[b.id].percent) : NaN;
    const ma = isNaN(pa) ? 1 : 0;
    const mb = isNaN(pb) ? 1 : 0;
    if (ma !== mb) return ma - mb;
    if (!ma && pa !== pb) return pb - pa;
    return ia - ib;
  }
  if (key === "added") {
    const aa = toMs(a.addedAt);
    const ba = toMs(b.addedAt);
    const ma = isNaN(aa) ? 1 : 0;
    const mb = isNaN(ba) ? 1 : 0;
    if (ma !== mb) return ma - mb;
    if (!ma && aa !== ba) return ba - aa;
    return ia - ib;
  }
  const ra = sortRank(a);
  const rb = sortRank(b);
  return ra[0] - rb[0] || ra[1] - rb[1] || ia - ib;
}

function expiryCell(a, st) {
  const subMs = subEndMs(st);
  if (!isNaN(subMs)) {
    const s = fmtTs(subMs);
    const ms = subMs - Date.now();
    const willCancel = !!(st && st.pendingCancellationDate);
    const active = st && st.subscriptionStatus === "active";
    let tag;
    if (willCancel) tag = `<span class="pill bad">到期不续</span>`;
    else if (active) tag = `<span class="pill ok">自动续费</span>`;
    else tag = `<span class="pill warn">${esc(st.subscriptionStatus || "未续费")}</span>`;
    const yr = st.isYearlyPlan ? "年付" : "月付";
    const tokenExp = (st && st.tokenExp) || (a && a.exp);
    const tokenHint = tokenExp ? `<div class="hint mono">登录至 ${esc(fmtExpiry(tokenExp))}</div>` : "";
    return `<div class="mono qmain">${esc(s)}</div><div class="hint">${tag} ${esc(relRemain(ms))} · ${yr}</div>${tokenHint}`;
  }
  // 还没验证到订阅信息：退回显示 token 登录有效期，并提示验证。
  const s = fmtExpiry(a && a.exp);
  if (!s) return `<span class="hint">—</span>`;
  return `<div class="mono qmain">${esc(s)}</div><div class="hint">登录token有效期 · 验证看订阅</div>`;
}

// bot 额度是周期性重置的：给出距下次重置的倒计时。
function fmtResetRelative(v) {
  const t = toMs(v);
  if (isNaN(t)) return "";
  const ms = t - Date.now();
  if (ms <= 0) return "可重置";
  const totalHours = Math.floor(ms / 3600000);
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  if (days > 0) return `剩 ${days}天${hours}小时`;
  const mins = Math.floor((ms % 3600000) / 60000);
  return `剩 ${hours}小时${mins}分`;
}

// 本周期起始在 24 小时内 → 视为「今天刚重置」。
function isJustReset(v) {
  const t = toMs(v);
  if (isNaN(t)) return false;
  const ms = Date.now() - t;
  return ms >= 0 && ms <= 24 * 3600000;
}

// 领取/验证拿到真实邮箱后回写行 label（并持久化到 Python 侧）。
function applyEmail(id, email) {
  if (!email || !String(email).includes("@")) return;
  const a = accounts.find((x) => x.id === id);
  if (a && a.label !== email) {
    a.label = email;
    const bridge = api();
    if (bridge && bridge.set_label) bridge.set_label(id, email);
  }
}

// 只记忆有意义的稳定状态；run/bad 不落盘，避免重开显示"处理中/失败"。
// 处于 run/bad 的号沿用上次落盘的数据，不会因为一次瞬时失败把已保存的信息冲掉。
const PERSIST_KINDS = new Set(["ok", "idle", "card", "dead"]);
let persistTimer = null;
function schedulePersist() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    const clean = {};
    for (const a of accounts) {
      const st = rowState[a.id];
      if (st && PERSIST_KINDS.has(st.kind)) clean[a.id] = st;
      else if (lastPersisted[a.id]) clean[a.id] = lastPersisted[a.id];
    }
    lastPersisted = clean;
    const bridge = api();
    if (bridge && bridge.save_status) bridge.save_status(clean);
  }, 400);
}

// 「已开通」的来源：付费套餐自带（Pro / Pro+ / Ultra 无需领取）还是本次领取到的。
function sandSourceHint(st) {
  if (!st || st.kind !== "ok") return "";
  if (st.claimedNow) return `<div class="hint" title="本次点「领取」后服务端才开通的">本次领取</div>`;
  if (st.accessGranted && st.planGrantsAccess && tierOf(st) !== "free") {
    return `<div class="hint" title="get-sand-access-status：资格已授予；Pro / Pro+ / Ultra 套餐自带 Sand，验证只是读到了这个事实，不是领取">套餐自带</div>`;
  }
  return "";
}

function statusCell(st) {
  if (!st) return `<span class="pill idle">待领取</span>`;
  switch (st.kind) {
    case "run":
      return `<span class="pill run">处理中…</span>`;
    case "ok":
      return (
        `<span class="pill ok" title="验证是只读的、不会领取：显示已开通说明该号本来就有 Sand 资格 / Bot 额度">${esc(st.label || "已开通")}</span>` +
        sandSourceHint(st)
      );
    case "card":
      return `<span class="pill warn" title="${esc(st.detail || "")}">需绑卡</span>`;
    case "bad":
      return `<span class="pill bad" title="${esc(st.detail || "")}">${esc(st.label || "失败")}</span>`;
    case "dead":
      return `<span class="pill bad" title="${esc(st.aliveReason || st.detail || "")}">失效</span>`;
    case "idle":
      return `<span class="pill idle">${esc(st.label || "未开通")}</span>`;
    default:
      return `<span class="pill idle">待领取</span>`;
  }
}

// 有效性：来自「验证账号」（探活 + 过期判断）；领取被服务端接受也视为有效。
function validityPill(st) {
  if (!st || st.alive === undefined) return `<span class="pill idle mini" title="尚未验证，点「验证账号」">未验证</span>`;
  if (st.alive === false) return `<span class="pill bad mini" title="${esc(st.aliveReason || "")}">失效</span>`;
  if (st.alive === true) return `<span class="pill ok mini">有效</span>`;
  return `<span class="pill warn mini" title="${esc(st.aliveReason || "探活无响应")}">未判定</span>`;
}

async function refreshLocalIdentity() {
  try {
    const res = await api().local_identity();
    localUserId = res && res.ok && res.userId ? res.userId : null;
  } catch (e) {
    localUserId = null;
  }
}

function sessionTypeLabel(t) {
  if (t === "client") return "客户端";
  if (t === "web") return "网页";
  return "其他";
}

function sessionTimeLabel(iso) {
  if (!iso) return "—";
  const text = fmtTs(toMs(iso));
  return text || "—";
}

function createdAgoTag(iso) {
  const ms = toMs(iso);
  if (isNaN(ms)) return "";
  const sec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  const label = sec < 60 ? `${sec}秒前` : `${Math.floor(sec / 60)}分钟前`;
  return `<span class="pill mini idle">${esc(label)}</span>`;
}

// 与 login_detect.py 保持一致：设备创建时间距检测时刻落在窗口内 → 刚登录。
const LOGIN_DETECT_DEFAULT_SEC = 120;
const LOGIN_DETECT_MIN_SEC = 5;
const LOGIN_DETECT_MAX_SEC = 3600;
const LOGIN_DETECT_FUTURE_SKEW_MS = 5000;

function clampLoginDetectSec(raw) {
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n)) return LOGIN_DETECT_DEFAULT_SEC;
  return Math.max(LOGIN_DETECT_MIN_SEC, Math.min(LOGIN_DETECT_MAX_SEC, n));
}

function isRecentLogin(createdAt, nowMs, windowSec) {
  const created = toMs(createdAt);
  const now = Number(nowMs);
  if (isNaN(created) || !Number.isFinite(now)) return false;
  const windowMs = clampLoginDetectSec(windowSec) * 1000;
  const delta = now - created;
  if (delta < -LOGIN_DETECT_FUTURE_SKEW_MS) return false;
  if (delta < 0) return true;
  return delta <= windowMs;
}

function recentLoginIds(rows, nowMs, windowSec) {
  return (rows || [])
    .filter((s) => s && s.sessionId && isRecentLogin(s.createdAt, nowMs, windowSec))
    .map((s) => s.sessionId);
}

function recentLoginCount(rows, nowMs, windowSec) {
  return recentLoginIds(rows, nowMs, windowSec).length;
}

function sessionDisplayPriority(s) {
  if (!s) return 9;
  if (s.localMark === "local") return 0;
  if (s.tokenDiff === "new") return 1;
  if (s.toolMark === "tool") return 2;
  if (s.freshMark === "fresh") return 3;
  if (s.localMark === "maybe-local") return 4;
  return 5;
}

function sortSessionsForDisplay(rows) {
  const list = Array.isArray(rows) ? rows.slice() : [];
  list.sort((a, b) => {
    const pa = sessionDisplayPriority(a);
    const pb = sessionDisplayPriority(b);
    if (pa !== pb) return pa - pb;
    return (toMs(b && b.createdAt) || 0) - (toMs(a && a.createdAt) || 0);
  });
  return list;
}

function sessionTypePill(t) {
  const cls = t === "client" ? "info" : t === "web" ? "amount" : "idle";
  return `<span class="pill mini ${cls}">${esc(sessionTypeLabel(t))}</span>`;
}

function resolveLocalMark(s, opts) {
  // 后端 annotate 总会带 localMark（含 null）。钉死后其余客户端是 null，不能再走「多条=可能是本机」。
  if (s && Object.prototype.hasOwnProperty.call(s, "localMark")) {
    return s.localMark || "";
  }
  const id = opts && opts.accountId;
  const rows = (opts && opts.sessions) || [];
  if (!localUserId || !id || id !== localUserId || (s && s.type) !== "client") return "";
  const n = rows.filter((x) => x.type === "client").length;
  if (n === 1) return "local";
  if (n > 1) return "maybe-local";
  return "";
}

// 本工具用的登录票是哪一类会话：JWT type=web → 网页会话；type=session → 客户端会话。
function tokenSessionKind(tokenType) {
  const t = String(tokenType || "").toLowerCase();
  if (t === "web") return "web";
  if (t === "session") return "client";
  return null;
}

function accountMail(a, id) {
  if (a && a.label && a.label.includes("@")) return a.label;
  return (a && (a.label || a.id)) || id;
}

function guardPill(a) {
  const g = guardStatus[a.id];
  if (!g || !g.running) return "";
  const kicked = g.kickedCount || 0;
  const tip =
    `本机设备保护运行中：${guardIntervalLabel(g.intervalSeconds)}，自动下线未保留设备。保留 ${(g.keepIds || []).length} 台，已踢 ${kicked} 台` +
    (g.lastTickAt ? `，最近检测 ${fmtTs(toMs(g.lastTickAt))}` : "") +
    (g.lastError ? `。⚠ ${g.lastError}` : "") +
    "。点击查看详情";
  return (
    `<button type="button" class="pill guard mini${g.lastError ? " has-error" : ""}" data-act="guardinfo" data-id="${esc(a.id)}" title="${esc(tip)}">` +
    `<i class="dot"></i>保护中${kicked ? ` · 已踢 ${kicked}` : ""}</button>`
  );
}

function sessionPills(a, st) {
  const bits = [];
  if (localUserId && a.id === localUserId) {
    bits.push(`<span class="pill info mini" title="本机 Cursor 当前登录的是这个号">本机</span>`);
  }
  bits.push(guardPill(a));
  if (st && st.alive === false) return bits.join("");
  if (!st || !Object.prototype.hasOwnProperty.call(st, "sessionCount")) return bits.join("");
  if (st.sessionError) {
    const waf = isWafError(st.sessionError, st.sessionWaf);
    bits.push(
      `<button type="button" class="pill idle mini" data-act="sessions" data-id="${esc(a.id)}" title="${esc(st.sessionError)}（点击${waf ? "可去浏览器过校验" : "实时重拉"}）">${waf ? "设备校验" : "设备 —"}</button>`
    );
    return bits.join("");
  }
  const n = st.sessionCount || 0;
  const c = st.sessionClientCount || 0;
  const w = st.sessionWebCount || 0;
  bits.push(
    `<button type="button" class="pill info mini" data-act="sessions" data-id="${esc(a.id)}" title="查看云端登录设备（实时拉取，可踢下线）">` +
      `${n} 设备 · 客户端${c} / 网页${w}</button>`
  );
  return bits.join("");
}

// 实时拉到的设备列表回写到行状态，让账号列的设备标签立刻跟上（踢下线后数量会变）。
function applySessionBlock(id, res) {
  if (!res || !Object.prototype.hasOwnProperty.call(res, "sessionCount")) return;
  const prev = rowState[id];
  if (!prev) return;
  rowState[id] = {
    ...prev,
    sessions: Array.isArray(res.sessions) ? res.sessions : [],
    sessionCount: res.sessionCount,
    sessionClientCount: res.sessionClientCount,
    sessionWebCount: res.sessionWebCount,
    sessionError: res.sessionError || "",
    sessionWaf: !!res.sessionWaf,
  };
  schedulePersist();
}

// ---- 查看设备：实时拉取 + 踢下线 ----

const sessionModal = {
  id: null,
  loading: false,
  error: "",
  waf: false,
  sessions: [],
  email: "",
  tokenType: null,
  confirmSid: "",
  busySid: "",
  via: "",
  browserOpen: false,
  checked: new Set(),
  confirmBatch: false,
  busyBatch: false,
};

function isWafError(err, wafFlag) {
  if (wafFlag) return true;
  const text = String(err || "");
  return text.includes("人机校验") || /vercel|checkpoint/i.test(text);
}

function wafHintHtml() {
  return (
    `<div class="waf-box">` +
    `<p class="hint">这不是 token 失效。每个账号只开一扇隔离浏览器：点一次打开，之后检测和踢下线都复用这扇窗口，不会每 30 秒再开新的。请在那个窗口里拖动完成校验，并保持 cursor.com 标签不要关。已经打开过就直接去那个窗口，不必再点。</p>` +
    `<button type="button" class="btn primary" data-sact="browser">打开 / 显示浏览器</button>` +
    `</div>`
  );
}

function sessionRowHtml(s, opts) {
  const sid = s.sessionId || "";
  const short = sid.slice(0, 8);
  const tags = [sessionTypePill(s.type)];
  const localMark = resolveLocalMark(s, opts);
  if (localMark === "local") {
    const host = (s && s.localHost) || "";
    const label = host ? `本机 · ${host}` : "本机";
    tags.push(
      `<span class="pill mini ok" title="本机 Cursor 的登录票签发时间与这条客户端创建时间一致，判定就是这台电脑${host ? "（" + esc(host) + "）" : ""}">${esc(label)}</span>`
    );
  } else if (localMark === "maybe-local") {
    tags.push(`<span class="pill mini warn" title="本机 Cursor 正登录此号，但没对上签发时间。接口没有电脑名，这些客户端里可能有一台是这台机器">可能是本机</span>`);
  }
  if (s && s.toolMark === "tool") {
    tags.push(
      `<span class="pill mini info" title="本工具正在用的登录票对上了这条客户端。它和 Cursor IDE 不是同一条，一键保护会两条都留；踢掉后该号在本工具里会失效">本工具</span>`
    );
  } else if (opts && opts.mineKind && s.type === opts.mineKind && !opts.hasToolMark && !(s && s.freshMark === "fresh")) {
    tags.push(`<span class="pill mini warn" title="本工具用的登录票是${esc(sessionTypeLabel(opts.mineKind))}会话，还没对上唯一一条：同类型里可能有一条是它，踢掉后该号在本工具里会失效">同类·可能是本工具</span>`);
  }
  if (s && s.tokenDiff === "new") {
    tags.push(
      `<span class="pill mini info" title="这次换票之后才出现的客户端。它是新登录票开的 Desktop App，不是 Cursor IDE">新票</span>`
    );
  }
  if (s && s.freshMark === "fresh") {
    tags.push(
      `<span class="pill mini ok" title="刷登录票会新开一台 Desktop App。官方列表通常把它排在最后，这里标出来并提前，方便找到">刚换票</span>`
    );
  }
  if (opts && opts.keepIds) {
    tags.push(
      opts.keepIds.has(sid)
        ? `<span class="pill mini ok">保留</span>`
        : `<span class="pill mini bad" title="不在保留名单里，检测到就会被踢下线">待踢</span>`
    );
  }
  if (opts && opts.recentAt && isRecentLogin(s.createdAt, opts.recentAt, opts.recentSec)) {
    const ago = Math.round((opts.recentAt - toMs(s.createdAt)) / 1000);
    const label = Number.isFinite(ago) && ago >= 0 ? `刚登录 · ${ago}秒前` : "刚登录";
    tags.push(
      `<span class="pill mini ok" title="设备创建时间与本机检测时刻相差 ${esc(String(ago))} 秒，落在设定的 ${esc(String(opts.recentSec))} 秒内">${esc(label)}</span>`
    );
  }
  return (
    `<div class="session-main">` +
    `<div class="session-head">${tags.join(" ")} <span class="sid mono" title="${esc(sid)}">${esc(short)}</span></div>` +
    `<div class="hint">创建时间 ${esc(sessionTimeLabel(s.createdAt))} ${createdAgoTag(s.createdAt)}　过期 ${esc(sessionTimeLabel(s.expiresAt))}</div>` +
    `</div>`
  );
}

function renderSessionModal() {
  const m = sessionModal;
  const rows = sortSessionsForDisplay(Array.isArray(m.sessions) ? m.sessions : []);
  $("sessionTitle").textContent = "登录设备 · " + (m.email || m.id || "");
  $("sessionSub").textContent = m.loading
    ? "正在实时拉取云端登录设备…"
    : rows.length
      ? `共 ${rows.length} 台设备` + (m.via === "browser" ? " · 经已打开的隔离浏览器读取" : "")
      : m.via === "browser"
        ? "经已打开的隔离浏览器读取"
        : "";
  let html = "";
  if (m.loading) {
    html += `<p class="modal-state"><span class="pill run">读取中…</span></p>`;
  } else if (m.error) {
    html += `<p class="modal-state"><span class="pill bad">读取失败</span> ${esc(m.error)}</p>`;
    if (isWafError(m.error, m.waf)) html += wafHintHtml();
  } else if (!rows.length) {
    html += `<p class="modal-state">当前没有登录设备。</p>`;
  }
  if (rows.length) {
    const mineKind = tokenSessionKind(m.tokenType);
    const hasToolMark = rows.some((s) => s && s.toolMark === "tool");
    const selectedN = rows.filter((s) => m.checked.has(s.sessionId)).length;
    const busy = !!(m.busySid || m.busyBatch);
    html += `<div class="session-toolbar">`;
    html += `<button type="button" class="btn tiny" data-sact="all"${busy ? " disabled" : ""}>全选</button>`;
    html += `<button type="button" class="btn tiny" data-sact="none"${busy ? " disabled" : ""}>全不选</button>`;
    html += `<span class="hint">已选 ${selectedN} 台</span>`;
    html += `<button type="button" class="btn tiny danger" data-sact="kick-batch"${busy || !selectedN ? " disabled" : ""}>${m.busyBatch ? "踢下线中…" : "踢掉所选"}</button>`;
    html += `</div>`;
    if (m.confirmBatch && selectedN) {
      html += `<div class="kick-confirm"><div class="hint">确定踢掉已勾选的 ${selectedN} 台设备？成功后应立刻从列表消失。官网最多约 10 分钟才真正下线。</div>` +
        `<div class="kick-actions"><button type="button" class="btn tiny" data-sact="batch-cancel">取消</button>` +
        `<button type="button" class="btn tiny danger" data-sact="batch-confirm">确认踢掉所选</button></div></div>`;
    }
    html += `<ul class="session-list">`;
    for (const s of rows) {
      const sid = s.sessionId || "";
      const on = m.checked.has(sid) ? " checked" : "";
      let right;
      if (m.confirmSid === sid) {
        const warn =
          (rows.length <= 1 ? " 这是仅剩的一台，很可能就是本工具正在使用的会话，踢掉后该号在本工具里会失效，需重新导入。" : "") +
          (mineKind && s.type === mineKind && rows.length > 1 ? " 它与本工具使用的登录票同类型，若正是那一条，踢掉后该号在本工具里会失效。" : "");
        right =
          `<div class="kick-confirm"><div class="hint">确定踢掉这台设备？成功后应立刻从列表消失。${esc(warn)}</div>` +
          `<div class="kick-actions"><button type="button" class="btn tiny" data-sact="cancel">取消</button>` +
          `<button type="button" class="btn tiny danger" data-sact="confirm" data-sid="${esc(sid)}" data-stype="${esc(s.typeRaw || s.type || "")}">确认踢下线</button></div></div>`;
      } else {
        const dis = busy ? " disabled" : "";
        right = `<button type="button" class="btn tiny danger" data-sact="kick" data-sid="${esc(sid)}"${dis}>${m.busySid === sid ? "踢下线中…" : "踢下线"}</button>`;
      }
      html += `<li class="session-row selectable${on ? " picked" : ""}${m.confirmSid === sid ? " confirming" : ""}${s && s.freshMark === "fresh" ? " fresh-token" : ""}"><label class="session-pick">` +
        `<input type="checkbox" class="sesschk" data-sid="${esc(sid)}"${on}${busy ? " disabled" : ""} />` +
        sessionRowHtml(s, { mineKind, hasToolMark, accountId: m.id, sessions: rows }) +
        `</label>${right}</li>`;
    }
    html += `</ul>`;
  }
  $("sessionBody").innerHTML = html;
  $("sessionRefresh").disabled = !!m.loading;
  const browserBtn = $("sessionBrowser");
  if (browserBtn) {
    browserBtn.disabled = !!m.loading || !m.id;
    browserBtn.textContent = m.browserOpen || m.via === "browser" ? "显示已打开的浏览器" : isWafError(m.error, m.waf) ? "去浏览器过校验" : "浏览器打开";
  }
}

async function loadSessions() {
  const id = sessionModal.id;
  if (!id) return;
  sessionModal.loading = true;
  sessionModal.error = "";
  sessionModal.waf = false;
  sessionModal.via = "";
  sessionModal.confirmSid = "";
  renderSessionModal();
  let res = null;
  try {
    res = await api().list_sessions(id);
  } catch (e) {
    res = { ok: false, error: String(e) };
  }
  if (sessionModal.id !== id) return; // 用户已切到别的号 / 关掉了
  sessionModal.loading = false;
  if (res && res.email) {
    sessionModal.email = res.email;
    applyEmail(id, res.email);
  }
  if (res && res.tokenType !== undefined) sessionModal.tokenType = res.tokenType;
  sessionModal.via = (res && res.sessionVia) || "";
  sessionModal.browserOpen = !!(res && res.browserOpen) || sessionModal.via === "browser";
  if (!res || !res.ok) {
    sessionModal.error = (res && res.error) || "读取设备失败";
    sessionModal.waf = !!(res && res.sessionWaf) || isWafError(sessionModal.error);
    sessionModal.sessions = Array.isArray(res && res.sessions) ? res.sessions : [];
  } else {
    sessionModal.waf = false;
    sessionModal.sessions = Array.isArray(res.sessions) ? res.sessions : [];
  }
  applySessionBlock(id, res);
  const present = new Set((sessionModal.sessions || []).map((s) => s.sessionId));
  sessionModal.checked = new Set([...sessionModal.checked].filter((sid) => present.has(sid)));
  renderSessionModal();
  render();
}

function openSessions(id) {
  const a = accounts.find((x) => x.id === id);
  sessionModal.id = id;
  sessionModal.email = accountMail(a, id);
  sessionModal.sessions = [];
  sessionModal.tokenType = a && a.tokenType ? a.tokenType : null;
  sessionModal.confirmSid = "";
  sessionModal.busySid = "";
  sessionModal.checked = new Set();
  sessionModal.confirmBatch = false;
  sessionModal.busyBatch = false;
  sessionModal.error = "";
  sessionModal.waf = !!(a && rowState[id] && rowState[id].sessionWaf);
  $("sessionMask").hidden = false;
  loadSessions();
}

function hideSessions() {
  $("sessionMask").hidden = true;
  sessionModal.id = null;
}

function openLoginDetect() {
  const input = $("loginDetectSec");
  if (input) input.value = String(clampLoginDetectSec(settings.loginDetectSec || guardModal.recentSec));
  $("loginDetectMask").hidden = false;
  if (input) {
    input.focus();
    input.select();
  }
}

function hideLoginDetect() {
  const el = $("loginDetectMask");
  if (el) el.hidden = true;
}

async function runLoginDetect() {
  const sec = clampLoginDetectSec($("loginDetectSec") && $("loginDetectSec").value);
  guardModal.recentSec = sec;
  guardModal.recentAt = Date.now();
  settings.loginDetectSec = sec;
  saveSettings({ loginDetectSec: sec });
  hideLoginDetect();
  if (guardModal.id && isGuardPanelOpen()) await loadGuardSessions();
  const next = new Set(recentLoginIds(guardModal.sessions, guardModal.recentAt, guardModal.recentSec));
  if (guardModal.running) {
    guardModal.kickChecked = next;
    guardModal.confirmKick = false;
  } else {
    guardModal.checked = next;
    guardModal.warnEmpty = false;
    guardModal.confirmKick = false;
  }
  renderGuardModal();
  const n = next.size;
  toast(n ? `已标出 ${n} 台刚登录设备（${sec}秒内）` : `没有设备落在 ${sec} 秒内`);
}

function sessionRevokeItem(s) {
  return { sessionId: s.sessionId || "", type: s.typeRaw || s.type || "" };
}

async function revokeSessions(id, sessions) {
  const items = (sessions || []).map(sessionRevokeItem).filter((x) => x.sessionId);
  if (!items.length) return { ok: false, error: "没有要踢下线的设备", kickedCount: 0, failedCount: 0, waf: false };
  const bridge = api();
  if (bridge && bridge.revoke_sessions) {
    try {
      return await bridge.revoke_sessions(id, items);
    } catch (e) {
      return { ok: false, error: String(e), kickedCount: 0, failedCount: items.length, waf: false };
    }
  }
  if (!bridge || !bridge.revoke_session) {
    return { ok: false, error: "接口不可用", kickedCount: 0, failedCount: items.length, waf: false };
  }
  const kicked = [];
  const failed = [];
  for (const item of items) {
    let res = null;
    try {
      res = await bridge.revoke_session(id, item.sessionId, item.type || "");
    } catch (e) {
      res = { ok: false, error: String(e) };
    }
    const row = {
      sessionId: item.sessionId,
      ok: !!(res && res.ok),
      error: (res && res.error) || "",
      status: (res && res.status) || 0,
    };
    if (res && res.waf) row.waf = true;
    (row.ok ? kicked : failed).push(row);
  }
  return {
    ok: !failed.length,
    error: failed.length ? failed[0].error || "部分设备踢下线失败" : "",
    kicked,
    failed,
    kickedCount: kicked.length,
    failedCount: failed.length,
    waf: failed.some((x) => x.waf),
  };
}

function toastBatchKick(res, targets, stillRows) {
  const still = new Set((stillRows || []).map((s) => s.sessionId));
  const lingering = (targets || []).filter((s) => still.has(s.sessionId)).length;
  const submitted = (res && res.kickedCount) || 0;
  const failed = (res && res.failedCount) || 0;
  if (res && (res.waf || isWafError(res.error))) {
    toast("批量踢下线被网站人机校验拦截，可点「去浏览器过校验」在官方页手动操作");
  } else if (failed && !submitted) {
    toast("批量踢下线失败：" + ((res && res.error) || "未知原因"));
  } else if (lingering) {
    toast(`已提交踢下线 ${submitted} 台，其中 ${lingering} 台列表里还在，没有真正踢掉`);
  } else if (submitted) {
    toast(`已踢下线 ${submitted} 台`);
  } else {
    toast("批量踢下线失败：" + ((res && res.error) || "未知原因"));
  }
}

async function kickSession(sid, stype) {
  const id = sessionModal.id;
  if (!id || !sid) return;
  sessionModal.busySid = sid;
  sessionModal.confirmSid = "";
  renderSessionModal();
  toast("正在踢下线…");
  let res = null;
  try {
    res = await api().revoke_session(id, sid, stype || "");
  } catch (e) {
    res = { ok: false, error: String(e) };
  }
  sessionModal.busySid = "";
  if (sessionModal.id === id) await loadSessions();
  const still = (sessionModal.sessions || []).some((s) => s.sessionId === sid);
  if (res && res.ok && still) toast("已提交踢下线，但列表里还在，没有真正踢掉，将自动重试");
  else if (res && res.ok) toast("已踢下线");
  else if (res && (res.waf || isWafError(res.error))) toast("踢下线被网站人机校验拦截，可点「去浏览器过校验」在官方页手动操作");
  else toast("踢下线失败：" + ((res && res.error) || "未知原因"));
}

async function kickSelectedSessions() {
  const id = sessionModal.id;
  if (!id || sessionModal.busyBatch || sessionModal.busySid) return;
  const want = sessionModal.checked;
  const targets = (sessionModal.sessions || []).filter((s) => s.sessionId && want.has(s.sessionId));
  if (!targets.length) {
    toast("请先勾选要删除的设备");
    sessionModal.confirmBatch = false;
    renderSessionModal();
    return;
  }
  sessionModal.busyBatch = true;
  sessionModal.confirmBatch = false;
  sessionModal.confirmSid = "";
  renderSessionModal();
  toast(`正在踢下线 ${targets.length} 台…`);
  let res = null;
  try {
    res = await revokeSessions(id, targets);
  } catch (e) {
    res = { ok: false, error: String(e) };
  }
  sessionModal.busyBatch = false;
  if (sessionModal.id === id) await loadSessions();
  toastBatchKick(res, targets, sessionModal.sessions);
}

function onSessionBodyClick(e) {
  const btn = e.target.closest("button[data-sact]");
  if (!btn) return;
  const act = btn.getAttribute("data-sact");
  const sid = btn.getAttribute("data-sid") || "";
  if (act === "kick") {
    sessionModal.confirmSid = sid;
    sessionModal.confirmBatch = false;
    renderSessionModal();
  } else if (act === "cancel") {
    sessionModal.confirmSid = "";
    renderSessionModal();
  } else if (act === "confirm") {
    kickSession(sid, btn.getAttribute("data-stype") || "");
  } else if (act === "browser") {
    openSessionsPage(sessionModal.id);
  } else if (act === "all") {
    sessionModal.checked = new Set((sessionModal.sessions || []).map((s) => s.sessionId).filter(Boolean));
    sessionModal.confirmBatch = false;
    renderSessionModal();
  } else if (act === "none") {
    sessionModal.checked = new Set();
    sessionModal.confirmBatch = false;
    renderSessionModal();
  } else if (act === "kick-batch") {
    sessionModal.confirmSid = "";
    sessionModal.confirmBatch = true;
    renderSessionModal();
  } else if (act === "batch-cancel") {
    sessionModal.confirmBatch = false;
    renderSessionModal();
  } else if (act === "batch-confirm") {
    kickSelectedSessions();
  }
}

function onSessionBodyChange(e) {
  const chk = e.target.closest("input.sesschk");
  if (!chk) return;
  const sid = chk.getAttribute("data-sid");
  if (chk.checked) sessionModal.checked.add(sid);
  else sessionModal.checked.delete(sid);
  sessionModal.confirmBatch = false;
  renderSessionModal();
}

// ---- 进控制台 ----

function browserName(name) {
  return name === "edge" ? "Edge" : "Chrome";
}

async function openDashboard(id) {
  toast("正在打开浏览器并注入登录，请稍候…");
  try {
    const res = await api().open_dashboard(id);
    if (res && res.ok) {
      toast(
        res.reused
          ? `已复用已打开的 ${browserName(res.browser)}，没有新开窗口`
          : `已在 ${browserName(res.browser)} 打开 Cursor 控制台，浏览器会保持打开；之后检测复用这扇窗口`
      );
    } else {
      toast("打开失败：" + ((res && res.error) || "未知原因"));
    }
  } catch (e) {
    toast("打开失败：" + String(e));
  }
}

async function openSessionsPage(id) {
  if (!id) return;
  toast("正在打开官方会话页并注入登录，请稍候…");
  try {
    const res = await api().open_sessions_page(id);
    if (res && res.ok) {
      toast(
        res.reused
          ? `已回到已打开的 ${browserName(res.browser)}，没有新开窗口。检测会继续用它读设备`
          : `已在 ${browserName(res.browser)} 打开官方会话页。请保持窗口不要关，之后检测都走这扇窗口`
      );
    } else {
      toast("打开失败：" + ((res && res.error) || "未知原因"));
    }
  } catch (e) {
    toast("打开失败：" + String(e));
  }
}

// ---- 本机设备保护：开启前勾选保留哪些设备、设置检测间隔；开启后按间隔检测并自动下线未保留设备 ----

const guardModal = {
  id: null,
  loading: false,
  error: "",
  waf: false,
  via: "",
  browserOpen: false,
  sessions: [],
  checked: new Set(),
  prechecked: false,
  running: false,
  email: "",
  tokenType: null,
  warnEmpty: false,
  timer: null,
  confirmKick: false,
  kicking: false,
  kickChecked: new Set(),
  recentAt: 0,
  recentSec: LOGIN_DETECT_DEFAULT_SEC,
};

function updateGuardGlobal() {
  const el = $("guardGlobal");
  if (!el) return;
  const running = Object.values(guardStatus).filter((g) => g && g.running);
  if (!running.length) {
    el.hidden = true;
    return;
  }
  const kicked = running.reduce((n, g) => n + (g.kickedCount || 0), 0);
  el.innerHTML = `<i class="dot"></i>保护中 ${running.length} 个账号${kicked ? ` · 已踢 ${kicked} 台` : ""}`;
  el.hidden = false;
}

// 只在「谁在跑 / 踢了多少 / 有没有报错」变化时才重画表格，避免每两秒把整张表刷一遍。
function guardSignature(map) {
  return JSON.stringify(
    Object.entries(map || {})
      .map(([k, v]) => [k, !!(v && v.running), (v && v.kickedCount) || 0, v && v.lastError ? 1 : 0])
      .sort()
  );
}

async function refreshGuardStatus(force) {
  let next;
  try {
    next = (await api().device_guard_status()) || {};
  } catch (e) {
    return;
  }
  const changed = guardSignature(next) !== guardSignature(guardStatus);
  guardStatus = next;
  updateGuardGlobal();
  if (changed || force) render();
  if (guardModal.id && isGuardPanelOpen()) {
    const g = guardStatus[guardModal.id];
    const nowRunning = !!(g && g.running);
    if (guardModal.running && !nowRunning) {
      // 线程自己停了（如登录态失效自动停止）：切回勾选视图并提示原因。
      guardModal.running = false;
      guardModal.prechecked = false;
      if (g && g.lastError) toast("本机保护已停止：" + g.lastError);
      loadGuardSessions();
    } else {
      renderGuardModal();
    }
  }
}

function guardTimeLabel(ts) {
  const ms = toMs(ts);
  if (isNaN(ms)) return "—";
  return fmtTsShort(ms) || "—";
}

const GUARD_INTERVAL_SEC_DEFAULT = 30;
const GUARD_INTERVAL_SEC_MIN = 5;
const GUARD_INTERVAL_SEC_MAX = 3600;

function cleanIntervalSeconds(value) {
  let n = parseInt(value, 10);
  if (!Number.isFinite(n)) return GUARD_INTERVAL_SEC_DEFAULT;
  if (n < GUARD_INTERVAL_SEC_MIN) n = GUARD_INTERVAL_SEC_MIN;
  if (n > GUARD_INTERVAL_SEC_MAX) n = GUARD_INTERVAL_SEC_MAX;
  return n;
}

function readGuardIntervalSeconds() {
  const el = $("guardInterval");
  return cleanIntervalSeconds(el && el.value);
}

function fillGuardIntervalSeconds(value) {
  const el = $("guardInterval");
  if (el) el.value = String(cleanIntervalSeconds(value));
}

function guardIntervalLabel(seconds) {
  return `每 ${cleanIntervalSeconds(seconds)} 秒检测一次`;
}

function recentLoginRowOpts() {
  return { recentAt: guardModal.recentAt, recentSec: guardModal.recentSec };
}

function isRecentLoginRow(s) {
  return !!(guardModal.recentAt && isRecentLogin(s && s.createdAt, guardModal.recentAt, guardModal.recentSec));
}

function recentLoginSubtext(rows) {
  if (!guardModal.recentAt) return "";
  const n = recentLoginCount(rows, guardModal.recentAt, guardModal.recentSec);
  return ` · 刚登录 ${n} 台（${guardModal.recentSec}秒内）`;
}

function renderGuardModal() {
  const m = guardModal;
  paintGuardPage();
  const id = m.id;
  if (!id) {
    const diff = $("guardDiff");
    if (diff) diff.hidden = true;
    return;
  }
  const g = guardStatus[id] || {};
  const kickedIds = new Set((g.lastKicked || []).map((k) => k && k.sessionId).filter(Boolean));
  const rows = sortSessionsForDisplay(
    (Array.isArray(m.sessions) ? m.sessions : []).map((s) => ({
      ...s,
      tokenDiff: m.tokenDiffIds && m.tokenDiffIds.has(s.sessionId) ? "new" : null,
    }))
  ).filter((s) => !kickedIds.has(s.sessionId));
  const mineKind = tokenSessionKind(m.tokenType);
  const hasToolMark = rows.some((s) => s && s.toolMark === "tool");
  const isLocal = !!(localUserId && id === localUserId);
  $("guardTitle").textContent = "本机保护";
  const emailEl = $("guardEmail");
  if (emailEl) emailEl.textContent = m.email || id || "";
  $("guardStart").hidden = m.running;
  $("guardStop").hidden = !m.running;
  const pinBtn = $("guardPinLocal");
  if (pinBtn) {
    pinBtn.hidden = !isLocal;
    pinBtn.disabled = !isLocal || !!m.running || !!(m.loading || m.kicking) || pinBusyIds.has(id);
  }
  const opts = $("guardOpts");
  if (opts) opts.classList.toggle("is-running", !!m.running);
  $("guardRefresh").disabled = !!(m.loading || m.kicking);
  $("guardStart").disabled = !!(m.loading || m.kicking);
  let html = guardSwitcherHtml() + guardDiffHtml(rows);

  if (m.running) {
    const keepIds = new Set(g.keepIds || []);
    $("guardSub").innerHTML = `<span class="pill guard"><i class="dot"></i>保护中</span> ${guardIntervalLabel(g.intervalSeconds)}` + (m.via === "browser" ? "，经已打开的隔离浏览器读取" : "，未保留设备会被自动踢下线") + recentLoginSubtext(rows);
    html += `<div class="guard-stats">`;
    html += `<div class="gstat"><span>启动时间</span><b>${esc(guardTimeLabel(g.startedAt))}</b></div>`;
    html += `<div class="gstat"><span>最近检测</span><b>${esc(guardTimeLabel(g.lastTickAt))}</b></div>`;
    html += `<div class="gstat"><span>检测轮次</span><b>${esc(String(g.tickCount || 0))}</b></div>`;
    html += `<div class="gstat"><span>当前设备</span><b>${esc(String(rows.length))}</b></div>`;
    html += `<div class="gstat"><span>保留设备</span><b>${esc(String(keepIds.size))}</b></div>`;
    html += `<div class="gstat kicked"><span>已踢下线</span><b>${esc(String(g.kickedCount || 0))} 台</b></div>`;
    html += `</div>`;
    if (g.lastError) {
      html += `<p class="modal-state"><span class="pill warn">最近一轮异常</span> ${esc(g.lastError)}</p>`;
      if (isWafError(g.lastError)) html += wafHintHtml();
    }
    if (Array.isArray(g.lastKicked) && g.lastKicked.length) {
      html += `<p class="guard-sec">最近踢下线</p><ul class="session-list compact">`;
      for (const k of g.lastKicked) {
        html += `<li class="session-row"><div class="session-main"><div class="session-head">${sessionTypePill(k.type)} <span class="sid mono" title="${esc(k.sessionId || "")}">${esc(String(k.sessionId || "").slice(0, 8))}</span></div>` +
          `<div class="hint">创建时间 ${esc(sessionTimeLabel(k.createdAt))} ${createdAgoTag(k.createdAt)}　踢于 ${esc(guardTimeLabel(k.at))}</div></div></li>`;
      }
      html += `</ul>`;
    }
    html += `<p class="guard-sec">设备列表${m.loading ? "（刷新中…）" : ""}</p>`;
    if (m.error) {
      html += `<p class="modal-state"><span class="pill bad">读取失败</span> ${esc(m.error)}</p>`;
      if (isWafError(m.error, m.waf)) html += wafHintHtml();
    }
    if (rows.length) {
      const selectedN = rows.filter((s) => m.kickChecked.has(s.sessionId)).length;
      const busy = !!(m.kicking || m.loading);
      html += `<div class="guard-toolbar">`;
      html += `<button type="button" class="btn tiny" data-sact="kick-all"${busy ? " disabled" : ""}>全选</button>`;
      html += `<button type="button" class="btn tiny" data-sact="kick-none"${busy ? " disabled" : ""}>全不选</button>`;
      html += `<span class="hint">已选 ${selectedN} 台，可马上踢下线（不必等下一轮检测）</span>`;
      html += `<button type="button" class="btn tiny danger" data-sact="kick-selected"${busy || !selectedN ? " disabled" : ""}>${m.kicking ? "踢下线中…" : "踢掉所选"}</button>`;
      html += `</div>`;
      if (m.confirmKick && selectedN) {
        html += `<div class="kick-confirm"><div class="hint">确定立即踢掉已勾选的 ${selectedN} 台？成功后应立刻从列表消失。官网最多约 10 分钟才真正下线。</div>` +
          `<div class="kick-actions"><button type="button" class="btn tiny" data-sact="kick-cancel">取消</button>` +
          `<button type="button" class="btn tiny danger" data-sact="kick-go">确认踢掉所选</button></div></div>`;
      }
      html += `<ul class="session-list compact">`;
      for (const s of rows) {
        const sid = s.sessionId || "";
        const on = m.kickChecked.has(sid) ? " checked" : "";
        html += `<li class="session-row selectable${on ? " picked" : ""}${isRecentLoginRow(s) ? " recent-login" : ""}${s && s.tokenDiff === "new" ? " token-diff" : ""}${s && s.freshMark === "fresh" ? " fresh-token" : ""}"><label class="session-pick">` +
          `<input type="checkbox" class="kickchk" data-sid="${esc(sid)}"${on}${busy ? " disabled" : ""} />` +
          sessionRowHtml(s, { mineKind, hasToolMark, keepIds, accountId: id, sessions: rows, ...recentLoginRowOpts() }) +
          `</label></li>`;
      }
      html += `</ul>`;
    } else if (!m.loading && !m.error) {
      html += `<p class="modal-state">当前没有登录设备。</p>`;
    }
    html += `<p class="hint">未保留的设备（含之后新登录的）检测到即踢。列表里还在会自动重试。改保留名单需先停止再启动。</p>`;
  } else {
    const n = rows.length;
    const checkedN = rows.filter((s) => m.checked.has(s.sessionId)).length;
    $("guardSub").textContent = m.loading
      ? "正在实时拉取云端登录设备…"
      : n
        ? `共 ${n} 台 · 已勾保留 ${checkedN} 台。可先删除未勾选，再启动持续保护` + recentLoginSubtext(rows)
        : "";
    if (m.loading) {
      html += `<p class="modal-state"><span class="pill run">读取中…</span></p>`;
    } else if (m.error) {
      html += `<p class="modal-state"><span class="pill bad">读取失败</span> ${esc(m.error)}</p>`;
      if (isWafError(m.error, m.waf)) html += wafHintHtml();
    } else if (!n) {
      html += `<p class="modal-state">当前没有登录设备，无需保护。</p>`;
    }
    if (n) {
      const dropN = n - checkedN;
      const busy = !!(m.kicking || m.loading);
      html += `<div class="guard-toolbar">`;
      html += `<button type="button" class="btn tiny" data-sact="all"${busy ? " disabled" : ""}>全选保留</button>`;
      html += `<button type="button" class="btn tiny" data-sact="none"${busy ? " disabled" : ""}>全不选</button>`;
      html += `<button type="button" class="btn tiny" data-sact="local"${busy ? " disabled" : ""}>只留本机</button>`;
      html += `<span class="hint">未勾选 ${dropN} 台</span>`;
      html += `<button type="button" class="btn tiny danger" data-sact="kick-unchecked"${busy || !dropN ? " disabled" : ""}>${m.kicking ? "删除中…" : "立即删除未勾选"}</button>`;
      html += `</div>`;
      if (m.confirmKick && dropN) {
        const allWarn = !checkedN ? " 当前一台都没勾，会把所有设备（含本机 Cursor / 本工具）全部踢下线。" : "";
        html += `<div class="kick-confirm"><div class="hint">确定立即踢掉未勾选的 ${dropN} 台？成功后应立刻从列表消失。官网最多约 10 分钟才真正下线。${esc(allWarn)}</div>` +
          `<div class="kick-actions"><button type="button" class="btn tiny" data-sact="kick-cancel">取消</button>` +
          `<button type="button" class="btn tiny danger" data-sact="kick-go">确认删除未勾选</button></div></div>`;
      }
      html += `<ul class="session-list">`;
      for (const s of rows) {
        const sid = s.sessionId || "";
        const on = m.checked.has(sid) ? " checked" : "";
        html +=
          `<li class="session-row selectable${on ? " kept" : ""}${isRecentLoginRow(s) ? " recent-login" : ""}${s && s.tokenDiff === "new" ? " token-diff" : ""}${s && s.freshMark === "fresh" ? " fresh-token" : ""}"><label class="session-pick">` +
          `<input type="checkbox" class="guardchk" data-sid="${esc(sid)}"${on}${busy ? " disabled" : ""} />` +
          sessionRowHtml(s, { mineKind, hasToolMark, accountId: id, sessions: rows, ...recentLoginRowOpts() }) +
          `<span class="pick-tag ${on ? "ok" : "bad"}">${on ? "保留" : "将被踢"}</span></label></li>`;
      }
      html += `</ul>`;
      if (m.warnEmpty && !checkedN) {
        html += `<p class="modal-state"><span class="pill bad">至少保留一台</span> 一台都不保留会把所有设备（含本机 Cursor）全部踢下线。</p>`;
      }
      const hints = [];
      if (isLocal) {
        const nClient = rows.filter((s) => s.type === "client").length;
        const pinned = rows.filter((s) => s.localMark === "local");
        if (pinned.length === 1 && nClient > 1) {
          const host = pinned[0].localHost ? `（${pinned[0].localHost}）` : "";
          hints.push(`已用本机 Cursor 登录票对上一条客户端，标为「本机」${host}。本工具若另开了一条客户端会标「本工具」，刚刷出来的会标「刚换票」并排到前面——这是同一台电脑上的两个会话，不是两台电脑。其余才是共号的其他设备。`);
        } else if (nClient === 1) {
          hints.push("已标出本机：云端只有一条客户端会话，对应本机 Cursor。");
        } else if (nClient > 1) {
          hints.push("本机 Cursor 正登录此号，但签发时间没对上唯一一条：客户端都标了「可能是本机」（接口不区分电脑）。请自行确认后勾选要保留的；不要用一键保护硬猜。");
        }
      }
      if (g.wasRunning && (g.keepIds || []).length) hints.push("上次退出时保护开着：已回填当时的勾选，需重新点「启动保护」。");
      else if (g.saved && (g.keepIds || []).length) hints.push("已回填上次的勾选。");
      if (mineKind) {
        hints.push(`本工具用的是${sessionTypeLabel(mineKind)}登录票，它也对应上面某一条同类型会话：若不保留任何同类型会话，保护会在最多 10 分钟后随票失效并自动停止。`);
      }
      if (hints.length) html += `<p class="hint">${hints.map(esc).join("<br />")}</p>`;
    }
  }
  $("guardBody").innerHTML = html;
  const iv = $("guardInterval");
  if (iv) iv.disabled = !!m.running;
  if (m.running && g.intervalSeconds != null) fillGuardIntervalSeconds(g.intervalSeconds);
  const detectBtn = $("guardDetect");
  if (detectBtn) detectBtn.disabled = !!m.loading || !id || !!m.kicking;
  syncGuardSwapButtons(m);
  const browserBtn = $("guardBrowser");
  if (browserBtn) {
    const viaBrowser = m.via === "browser" || m.browserOpen;
    const waf = isWafError(m.error, m.waf) || isWafError((g && g.lastError) || "");
    browserBtn.disabled = !!m.loading || !id;
    browserBtn.textContent = viaBrowser ? "显示已打开的浏览器" : waf ? "去浏览器过校验" : "浏览器打开";
  }
}

async function loadGuardSessions() {
  const id = guardModal.id;
  if (!id) return;
  guardModal.loading = true;
  guardModal.error = "";
  guardModal.waf = false;
  guardModal.via = "";
  renderGuardModal();
  let res = null;
  try {
    res = await api().list_sessions(id);
  } catch (e) {
    res = { ok: false, error: String(e) };
  }
  if (guardModal.id !== id) return;
  guardModal.loading = false;
  if (res && res.email) {
    guardModal.email = res.email;
    applyEmail(id, res.email);
  }
  if (res && res.tokenType !== undefined) guardModal.tokenType = res.tokenType;
  guardModal.via = (res && res.sessionVia) || "";
  guardModal.browserOpen = !!(res && res.browserOpen) || guardModal.via === "browser";
  const rows = res && Array.isArray(res.sessions) ? res.sessions : [];
  if (!res || !res.ok) {
    guardModal.error = (res && res.error) || "读取设备失败";
    guardModal.waf = !!(res && res.sessionWaf) || isWafError(guardModal.error);
  } else {
    guardModal.waf = false;
  }
  guardModal.sessions = rows;
  applySessionBlock(id, res);
  {
    const present = new Set(rows.map((s) => s.sessionId));
    guardModal.kickChecked = new Set([...guardModal.kickChecked].filter((sid) => present.has(sid)));
  }
  if (!guardModal.running) {
    const present = new Set(rows.map((s) => s.sessionId));
    if (!guardModal.prechecked && rows.length) {
      // 预勾选：优先回填上次的保留名单；否则本机账号默认保留全部客户端会话；其余不勾，让用户自己选。
      const g = guardStatus[id] || {};
      const saved = (g.keepIds || []).filter((sid) => present.has(sid));
      const picked = new Set(saved);
      if (!picked.size && localUserId && id === localUserId) {
        const pinned = rows.filter((s) => s.localMark === "local").map((s) => s.sessionId).filter(Boolean);
        if (pinned.length) {
          for (const sid of pinned) picked.add(sid);
          for (const s of rows) if (s.toolMark === "tool" && s.sessionId) picked.add(s.sessionId);
        } else {
          for (const s of rows) if (s.type === "client") picked.add(s.sessionId);
        }
      }
      guardModal.checked = picked;
      guardModal.prechecked = true;
    } else {
      // 刷新：保留用户已勾的（仍在线的），新出现的设备默认不勾。
      guardModal.checked = new Set([...guardModal.checked].filter((sid) => present.has(sid)));
    }
  }
  renderGuardModal();
  render();
}

async function reloadOpenDeviceLists(id) {
  const sid = String(id || "");
  if (!sid) return;
  const sessionMask = $("sessionMask");
  if (sessionModal.id === sid && sessionMask && !sessionMask.hidden) {
    await loadSessions();
  }
  if (guardModal.id === sid && isGuardPanelOpen()) {
    await loadGuardSessions();
  }
}

function isGuardPanelOpen() {
  const pane = $("paneGuard");
  return !!(pane && !pane.hidden);
}

function paintGuardAccountSelect() {
  const sel = $("guardAccountSelect");
  if (!sel) return;
  const paid = orderedAccounts().filter((a) => isPaidPlan(rowState[a.id]));
  const sig = paid.map((a) => a.id + "\t" + accountMail(a, a.id) + "\t" + (guardStatus[a.id] && guardStatus[a.id].running ? "1" : "0")).join("\n");
  const current = guardModal.id && paid.some((a) => a.id === guardModal.id) ? guardModal.id : "";
  if (sel.dataset.sig !== sig) {
    sel.dataset.sig = sig;
    const placeholder = paid.length ? "选择付费账号" : "没有已验证的付费账号";
    sel.innerHTML =
      `<option value="">${esc(placeholder)}</option>` +
      paid
        .map((a) => {
          const running = !!(guardStatus[a.id] && guardStatus[a.id].running);
          const label = accountMail(a, a.id) + (running ? " · 保护中" : "");
          return `<option value="${esc(a.id)}">${esc(label)}</option>`;
        })
        .join("");
  }
  if (sel.value !== current) sel.value = current;
}

function paintGuardPage() {
  const has = !!guardModal.id;
  const empty = $("guardEmpty");
  const work = $("guardWork");
  const acts = document.querySelector("#guardCard .drawer-head-acts");
  if (empty) empty.hidden = has;
  if (work) work.hidden = !has;
  if (acts) acts.hidden = !has;
  paintGuardAccountSelect();
}

function startGuardPanelTimer() {
  stopGuardPanelTimer();
  guardModal.timer = setInterval(() => {
    if (!isGuardPanelOpen()) return stopGuardPanelTimer();
    if (guardModal.running) refreshGuardStatus(false);
  }, 1000);
}

function stopGuardPanelTimer() {
  if (guardModal.timer) clearInterval(guardModal.timer);
  guardModal.timer = null;
}

function clearTicketDiff() {
  guardModal.tokenDiffIds = null;
  guardModal.droppedSessionId = "";
  guardModal.tokenDiffOn = false;
}

function guardDiffHtml(rows) {
  const el = $("guardDiff");
  if (!guardModal.tokenDiffOn) {
    if (el) el.hidden = true;
    return "";
  }
  const hasLocal = (rows || []).some((s) => s && s.localMark === "local");
  let text = hasLocal ? "本机在最上面" : "没对上本机 Cursor，请看标了新票的那一台";
  const dropped = String(guardModal.droppedSessionId || "");
  if (dropped) text += ` · 已踢旧客户端 ${dropped.slice(0, 8)}`;
  if (el) {
    el.hidden = false;
    el.textContent = text;
  }
  return "";
}

function syncGuardSwapButtons(m) {
  const account = accounts.find((a) => a.id === (m && m.id));
  const hasRefresh = !!(account && account.hasRefresh);
  const locked = !!(m && (m.loading || m.kicking)) || busy || !(m && m.id);
  const probe = $("guardProbe");
  const refresh = $("guardRefreshLogin");
  const kick = $("guardRefreshKick");
  if (probe) probe.disabled = locked;
  if (refresh) refresh.disabled = locked || !hasRefresh;
  if (kick) kick.disabled = locked || !hasRefresh;
}

function sessionIdSet(rows) {
  return new Set((rows || []).map((s) => s && s.sessionId).filter(Boolean));
}

function applyTicketDiff(id, before, dropped) {
  if (guardModal.id !== id) return;
  const after = guardModal.sessions || [];
  const fresh = [];
  for (const row of after) {
    if (!row || row.type !== "client") continue;
    const sid = row.sessionId;
    if (sid && !before.has(sid)) fresh.push(sid);
  }
  guardModal.tokenDiffIds = new Set(fresh);
  guardModal.droppedSessionId = dropped || "";
  guardModal.tokenDiffOn = true;
  renderGuardModal();
}

async function guardProbeTicket() {
  const id = guardModal.id;
  if (!id) return;
  await probeRefreshOne(id);
  syncGuardSwapButtons(guardModal);
}

async function guardRefreshTicket(kick) {
  const id = guardModal.id;
  if (!id) return;
  const before = sessionIdSet(guardModal.sessions);
  const res = kick ? await refreshLoginKickOld(id) : await refreshLoginOne(id);
  if (!res || !res.ok) {
    clearTicketDiff();
    renderGuardModal();
    return;
  }
  applyTicketDiff(id, before, kick ? res.droppedSessionId || "" : "");
}

function firstRunningGuardId() {
  const running = Object.entries(guardStatus || {}).filter(([, g]) => g && g.running);
  if (!running.length) return "";
  if (guardModal.id && running.some(([k]) => k === guardModal.id)) return guardModal.id;
  return running[0][0];
}

function runningGuardIds() {
  return Object.entries(guardStatus || {})
    .filter(([, g]) => g && g.running)
    .map(([k]) => k);
}

function guardSwitcherHtml() {
  const ids = runningGuardIds().filter((id) => accounts.some((a) => a.id === id) || id === guardModal.id);
  if (ids.length < 2) return "";
  let html = `<div class="guard-switch"><span class="hint">保护中 ${ids.length} 个账号，点邮箱切换</span>`;
  for (const id of ids) {
    const a = accounts.find((x) => x.id === id);
    const on = id === guardModal.id;
    html += `<button type="button" class="btn tiny${on ? " primary" : ""}" data-sact="switch" data-id="${esc(id)}">${esc(accountMail(a, id))}</button>`;
  }
  html += `</div>`;
  return html;
}

function openGuardFromGlobal() {
  const id = firstRunningGuardId() || guardModal.id;
  if (id) openGuard(id);
}

async function openGuard(id, opts) {
  const a = accounts.find((x) => x.id === id);
  if (!a) return;
  setMainTab("guard");
  const keepIds = opts && Array.isArray(opts.keepIds) ? opts.keepIds.map((sid) => String(sid || "").trim()).filter(Boolean) : null;
  const same = guardModal.id === id && isGuardPanelOpen();
  if (!same) {
    guardModal.id = id;
    guardModal.email = accountMail(a, id);
    guardModal.tokenType = a.tokenType || null;
    guardModal.sessions = [];
    guardModal.checked = new Set();
    guardModal.prechecked = false;
    guardModal.warnEmpty = false;
    guardModal.error = "";
    guardModal.waf = false;
    guardModal.via = "";
    guardModal.browserOpen = false;
    guardModal.confirmKick = false;
    guardModal.kicking = false;
    guardModal.kickChecked = new Set();
    guardModal.recentAt = 0;
    guardModal.recentSec = clampLoginDetectSec(settings.loginDetectSec);
    clearTicketDiff();
  }
  if (keepIds) {
    guardModal.checked = new Set(keepIds);
    guardModal.prechecked = true;
  }
  paintGuardPage();
  await refreshGuardStatus(false);
  if (guardModal.id !== id) return;
  const g = guardStatus[id];
  guardModal.running = !!(g && g.running);
  if (g && g.intervalSeconds != null) fillGuardIntervalSeconds(g.intervalSeconds);
  else if (!same) fillGuardIntervalSeconds(GUARD_INTERVAL_SEC_DEFAULT);
  paintGuardPage();
  renderGuardModal();
  startGuardPanelTimer();
  await loadGuardSessions();
}

function hideGuard() {
  hideLoginDetect();
  clearTicketDiff();
  stopGuardPanelTimer();
  setMainTab("accounts");
}

function onGuardBodyClick(e) {
  const btn = e.target.closest("button[data-sact]");
  if (!btn) return;
  const act = btn.getAttribute("data-sact");
  if (act === "browser") {
    openSessionsPage(guardModal.id);
  } else if (act === "switch") {
    const id = btn.getAttribute("data-id");
    if (id && id !== guardModal.id) openGuard(id);
  } else if (act === "all") {
    guardModal.checked = new Set((guardModal.sessions || []).map((s) => s.sessionId).filter(Boolean));
    guardModal.warnEmpty = false;
    guardModal.confirmKick = false;
    renderGuardModal();
  } else if (act === "none") {
    guardModal.checked = new Set();
    guardModal.warnEmpty = false;
    guardModal.confirmKick = false;
    renderGuardModal();
  } else if (act === "local") {
    keepLocalOnly();
  } else if (act === "kick-unchecked") {
    guardModal.confirmKick = true;
    renderGuardModal();
  } else if (act === "kick-all") {
    guardModal.kickChecked = new Set((guardModal.sessions || []).map((s) => s.sessionId).filter(Boolean));
    guardModal.confirmKick = false;
    renderGuardModal();
  } else if (act === "kick-none") {
    guardModal.kickChecked = new Set();
    guardModal.confirmKick = false;
    renderGuardModal();
  } else if (act === "kick-selected") {
    guardModal.confirmKick = true;
    renderGuardModal();
  } else if (act === "kick-cancel") {
    guardModal.confirmKick = false;
    renderGuardModal();
  } else if (act === "kick-go") {
    if (guardModal.running) kickGuardSelected();
    else kickGuardUnchecked();
  }
}

function onGuardBodyChange(e) {
  const keepChk = e.target.closest("input.guardchk");
  if (keepChk) {
    const sid = keepChk.getAttribute("data-sid");
    if (keepChk.checked) guardModal.checked.add(sid);
    else guardModal.checked.delete(sid);
    guardModal.warnEmpty = false;
    guardModal.confirmKick = false;
    renderGuardModal();
    return;
  }
  const kickChk = e.target.closest("input.kickchk");
  if (!kickChk) return;
  const sid = kickChk.getAttribute("data-sid");
  if (kickChk.checked) guardModal.kickChecked.add(sid);
  else guardModal.kickChecked.delete(sid);
  guardModal.confirmKick = false;
  renderGuardModal();
}

function keepLocalOnly() {
  const rows = guardModal.sessions || [];
  const id = guardModal.id;
  const exact = [];
  const tools = [];
  for (const s of rows) {
    const mark = resolveLocalMark(s, { accountId: id, sessions: rows });
    if (mark === "local" && s.sessionId) exact.push(s.sessionId);
    if (s.toolMark === "tool" && s.sessionId) tools.push(s.sessionId);
  }
  if (!exact.length) {
    toast("认不出本机 Cursor 那条客户端，请手动勾选要保留的。不要在认不出时硬留一台。");
    return;
  }
  guardModal.checked = new Set(exact.concat(tools));
  guardModal.warnEmpty = false;
  guardModal.confirmKick = false;
  renderGuardModal();
}

async function kickGuardUnchecked() {
  const id = guardModal.id;
  if (!id || guardModal.kicking) return;
  const targets = (guardModal.sessions || []).filter((s) => s.sessionId && !guardModal.checked.has(s.sessionId));
  await kickGuardTargets(id, targets);
}

async function kickGuardSelected() {
  const id = guardModal.id;
  if (!id || guardModal.kicking) return;
  const want = guardModal.kickChecked;
  const targets = (guardModal.sessions || []).filter((s) => s.sessionId && want.has(s.sessionId));
  await kickGuardTargets(id, targets);
}

async function kickGuardTargets(id, targets) {
  if (!targets.length) {
    toast("没有要删除的设备");
    guardModal.confirmKick = false;
    renderGuardModal();
    return;
  }
  guardModal.kicking = true;
  guardModal.confirmKick = false;
  renderGuardModal();
  toast(`正在踢下线 ${targets.length} 台…`);
  let res = null;
  try {
    res = await revokeSessions(id, targets);
  } catch (e) {
    res = { ok: false, error: String(e) };
  }
  guardModal.kicking = false;
  if (guardModal.id === id) await loadGuardSessions();
  toastBatchKick(res, targets, guardModal.sessions);
}

async function startGuard() {
  const id = guardModal.id;
  if (!id || guardModal.loading || guardModal.kicking) return;
  const present = new Set((guardModal.sessions || []).map((s) => s.sessionId));
  const keep = [...guardModal.checked].filter((sid) => present.has(sid));
  if (!keep.length) {
    guardModal.warnEmpty = true;
    renderGuardModal();
    toast("至少勾选一台要保留的设备，否则会把所有设备（含本机）全部踢下线");
    return;
  }
  const others = present.size - keep.length;
  const seconds = readGuardIntervalSeconds();
  fillGuardIntervalSeconds(seconds);
  $("guardStart").disabled = true;
  let res = null;
  try {
    res = await api().device_guard_start(id, keep, seconds);
  } catch (e) {
    res = { ok: false, error: String(e) };
  }
  $("guardStart").disabled = false;
  if (!res || !res.ok) {
    toast("开启失败：" + ((res && res.error) || "未知原因"));
    return;
  }
  toast(`已开启本机保护：保留 ${keep.length} 台，${guardIntervalLabel(seconds)}，并自动下线其他设备` + (others ? `（当前将踢 ${others} 台）` : ""));
  if (guardModal.id === id) guardModal.running = true;
  await refreshGuardStatus(true);
}

async function pinLocalGuard(id) {
  if (busy || pinBusyIds.has(id)) return;
  if (!localUserId || id !== localUserId) {
    toast("请先在本机 Cursor 登录这个号");
    return;
  }
  const bridge = api();
  if (!bridge || !bridge.device_guard_pin_local) {
    toast("当前版本不支持一键本机保护");
    return;
  }
  pinBusyIds.add(id);
  render();
  toast("正在识别本机 Cursor…");
  const step = setTimeout(() => toast("正在核对本机与本工具会话…"), 500);
  let res = null;
  try {
    res = await bridge.device_guard_pin_local(id, GUARD_INTERVAL_SEC_DEFAULT);
  } catch (e) {
    res = { ok: false, error: String(e) };
  } finally {
    clearTimeout(step);
    pinBusyIds.delete(id);
    render();
  }
  if (!res || !res.ok) {
    toast((res && res.error) || "认不出本机 Cursor 那条客户端，未开启保护");
    return;
  }
  const kept = (res.keepIds || []).length;
  toast(kept > 1 ? `已保留本机 Cursor 和本工具（${kept} 条），并开启保护` : "已保留本机 Cursor 并开启保护");
  await refreshGuardStatus(true);
  await openGuard(id, { keepIds: res.keepIds || [] });
}

async function stopGuard(id) {
  let res = null;
  try {
    res = await api().device_guard_stop(id);
  } catch (e) {
    res = { ok: false, error: String(e) };
  }
  if (res && res.ok) {
    const n = res.status && res.status.kickedCount;
    toast("已停止本机保护" + (n ? `（本次共踢下线 ${n} 台）` : ""));
  } else {
    toast("停止失败：" + ((res && res.error) || "未知原因"));
  }
  if (guardModal.id === id && isGuardPanelOpen()) {
    guardModal.running = false;
    guardModal.prechecked = false;
    await refreshGuardStatus(true);
    await loadGuardSessions();
  } else {
    await refreshGuardStatus(true);
  }
}

async function startGuardAutoOne(id, seconds) {
  const bridge = api();
  if (bridge && bridge.device_guard_start_auto) {
    return await bridge.device_guard_start_auto(id, seconds);
  }
  const listed = await bridge.list_sessions(id);
  if (!listed || !listed.ok) {
    return { ok: false, error: (listed && listed.error) || "读取设备失败", waf: !!(listed && listed.sessionWaf) };
  }
  const saved = ((guardStatus[id] || {}).keepIds) || [];
  const isLocal = !!(localUserId && id === localUserId);
  const rows = listed.sessions || [];
  const present = [];
  const clients = [];
  const seen = new Set();
  for (const s of rows) {
    const sid = s && s.sessionId;
    if (!sid || seen.has(sid)) continue;
    seen.add(sid);
    present.push(sid);
    if (s.type === "client") clients.push(sid);
  }
  const keepSaved = saved.filter((sid) => seen.has(sid));
  const keep = keepSaved.length ? keepSaved : isLocal && clients.length ? clients : present;
  if (!keep.length) return { ok: false, error: "当前没有登录设备，无法开启保护" };
  const res = await bridge.device_guard_start(id, keep, seconds);
  if (res && res.ok) res.keepCount = keep.length;
  return res;
}

async function runGuardBatch(kind) {
  if (busy) return;
  const ids = selectedIds();
  if (!ids.length) {
    toast(kind === "stop" ? "请先勾选要停止保护的账号" : "请先勾选要保护的账号（可多选）");
    return;
  }
  if (kind === "start") {
    const pending = ids.filter((id) => !(guardStatus[id] && guardStatus[id].running));
    if (!pending.length) {
      toast("勾选的账号都已在保护中");
      return;
    }
    if (
      !window.confirm(
        `将为 ${pending.length} 个账号开启本机保护。每个号保留当前在线设备（有上次勾选则沿用），之后新登录会被自动踢下线。已在保护中的会跳过。`
      )
    ) {
      return;
    }
    const seconds = readGuardIntervalSeconds();
    fillGuardIntervalSeconds(seconds);
    const wrap = $("progressWrap");
    const bar = $("progressBar");
    const text = $("progressText");
    wrap.hidden = false;
    const total = pending.length;
    let done = 0;
    let next = 0;
    const conc = readConcurrency();
    const tally = { ok: 0, skipped: 0, failed: 0 };
    let firstOk = "";
    bar.style.width = "0%";
    text.textContent = `保护 0/${total}（并发 ${conc}）`;
    busy = true;
    render();
    async function worker() {
      while (next < pending.length) {
        const id = pending[next++];
        let res = null;
        try {
          res = await startGuardAutoOne(id, seconds);
        } catch (e) {
          res = { ok: false, error: String(e) };
        }
        if (res && res.skipped) tally.skipped += 1;
        else if (res && res.ok) {
          tally.ok += 1;
          if (!firstOk) firstOk = id;
        } else tally.failed += 1;
        done += 1;
        bar.style.width = ((done / total) * 100).toFixed(1) + "%";
        text.textContent = `保护 ${done}/${total}（并发 ${conc}）`;
        await refreshGuardStatus(true);
      }
    }
    const workers = [];
    for (let i = 0; i < Math.min(conc, total); i++) workers.push(worker());
    await Promise.all(workers);
    busy = false;
    render();
    bar.style.width = "100%";
    text.textContent = `完成 ${done}/${total}`;
    setTimeout(() => (wrap.hidden = true), 1500);
    toast(
      `批量保护完成：新开 ${tally.ok}` +
        (tally.skipped ? `，已在保护中 ${tally.skipped}` : "") +
        (tally.failed ? `，失败 ${tally.failed}` : "")
    );
    if (firstOk) openGuard(firstOk);
    else await refreshGuardStatus(true);
    return;
  }

  const running = ids.filter((id) => guardStatus[id] && guardStatus[id].running);
  if (!running.length) {
    toast("勾选的账号都没有在保护中");
    return;
  }
  if (!window.confirm(`停止 ${running.length} 个账号的本机保护？`)) return;
  let stopped = 0;
  let failed = 0;
  for (const id of running) {
    let res = null;
    try {
      res = await api().device_guard_stop(id);
    } catch (e) {
      res = { ok: false, error: String(e) };
    }
    if (res && res.ok) stopped += 1;
    else failed += 1;
  }
  if (guardModal.id && running.includes(guardModal.id) && isGuardPanelOpen()) {
    guardModal.running = false;
    guardModal.prechecked = false;
    await refreshGuardStatus(true);
    await loadGuardSessions();
  } else {
    await refreshGuardStatus(true);
  }
  toast(`已停止 ${stopped} 个账号的保护` + (failed ? `，失败 ${failed}` : ""));
}

// 行操作分组：
//   右侧操作列 = 账号主操作；账号格「显示 Token」右侧 = 票/浏览器类。
//   窄屏隐藏操作列，改为账号格里的「操作」按钮弹出全部动作。

function rowMainActions(a, st) {
  const webTok = String(a.tokenType || "").toLowerCase() === "web";
  const g = guardStatus[a.id];
  const guarding = !!(g && g.running);
  const dis = !!busy;
  return [
    { act: "verify", label: "验证", title: "验证有效性并刷新用量 / 订阅", disabled: dis },
    { act: "switch", label: "切号", title: webTok ? "网站会话：切号时自动换客户端登录票（稍慢几秒）" : "切到本机 Cursor", disabled: dis },
    { act: "loginBot", label: "登录 Bot", title: webTok ? "网站会话：先换客户端票，再写入 Grok Bot 的 Cursor 账户并切换（不关 Cursor）" : "写入 Grok Bot 自带账户列表并切换（不关 Cursor）", disabled: dis },
    { act: "devices", label: "查看设备", title: "实时查看云端登录设备，可踢下线（成功后应立刻从列表消失）" },
    {
      act: "guard",
      label: "本机保护",
      cls: guarding ? "guard guarding" : "guard",
      title: guarding
        ? `打开本机保护页（运行中：已踢 ${(g && g.kickedCount) || 0} 台）。停止保护在该页里操作`
        : "打开本机保护页：勾选要保留的设备，可批量删除未勾选的，再设置检测间隔自动下线新设备",
    },
  ];
}

function ticketMenuGroups(a, st) {
  const tokenOn = rowTokensOn(a.id);
  const spec = {
    pills: a.hasRefresh ? ["已有 Refresh"] : ["未探测 Refresh"],
    groups: [],
  };
  if (tokenOn) spec.pills.push("Token 已展开");
  const dis = !!busy;
  const view = [
    {
      act: "showToken",
      label: tokenOn ? "隐藏 Token" : "显示 Token",
      title: "显示或隐藏 Worksession / Refresh token",
      keepOpen: true,
    },
    { act: "copy", label: "复制", title: "复制：邮箱----user_id::token", disabled: dis },
    { act: "dashboard", label: "进控制台", title: "用该账号登录态打开隔离浏览器到 Cursor 控制台" },
    { act: "browser", label: "网页领取", title: "用该账号登录态打开隔离浏览器到 Sand 领取页", disabled: dis },
  ];
  if (claimVisible(st)) {
    view.push({ act: "claim", label: "领取", title: "领取 Sand 资格", disabled: dis, span: true });
  }
  spec.groups.push({ id: "view", title: "查看", items: view });
  spec.groups.push({
    id: "danger",
    title: "",
    kind: "danger",
    items: [{ act: "remove", label: "移除这个账号", cls: "danger", disabled: dis, span: true }],
  });
  return spec;
}

function ticketMenuHtml(a) {
  return `<button type="button" class="btn tiny" data-act="ticketMenu" data-id="${esc(a.id)}" title="Token / 领取 / 进控制台。换票在本机保护页">登录信息 ▾</button>`;
}

function actionButtonsHtml(id, list, opts) {
  const tiny = !(opts && opts.tiny === false);
  return list
    .map((b) => {
      const cls = ["btn", tiny ? "tiny" : "", b.cls || "", b.span ? "span2" : ""].filter(Boolean).join(" ");
      return (
        `<button type="button" class="${cls}" data-act="${b.act}" data-id="${esc(id)}"` +
        `${b.disabled ? " disabled" : ""}` +
        `${b.keepOpen ? ' data-keep-open="1"' : ""}` +
        `${b.title ? ` title="${esc(b.title)}"` : ""}>${esc(b.label)}</button>`
      );
    })
    .join("\n");
}

function menuGroupsHtml(id, spec) {
  const pills = (spec.pills || [])
    .map((p) => {
      const cls = p.indexOf("未探测") >= 0 ? "warn" : p.indexOf("已有") >= 0 ? "ok" : "idle";
      return `<span class="pill mini ${cls}">${esc(p)}</span>`;
    })
    .join("");
  const groups = (spec.groups || [])
    .map((g) => {
      const title = g.title ? `<div class="menu-group-title">${esc(g.title)}</div>` : "";
      const hint = g.hint ? `<p class="menu-group-hint">${esc(g.hint)}</p>` : "";
      const kind = g.kind ? ` ${esc(g.kind)}` : "";
      return (
        `<section class="menu-group${kind}">${title}${hint}` +
        `<div class="menu-group-acts">${actionButtonsHtml(id, g.items, { tiny: false })}</div></section>`
      );
    })
    .join("");
  return { pills, body: `<div class="menu-sheet">${groups}</div>` };
}

function fillActionSheet(id, title, sub, spec) {
  $("menuTitle").textContent = title;
  $("menuSub").textContent = sub || "";
  const painted = menuGroupsHtml(id, spec);
  $("menuMeta").innerHTML = painted.pills;
  $("menuBody").innerHTML = painted.body;
  $("menuMask").hidden = false;
}

function openTicketMenu(id) {
  const a = accounts.find((x) => x.id === id);
  if (!a) return;
  menuKind = "ticket";
  fillActionSheet(id, "登录信息", accountMail(a, id), ticketMenuGroups(a, rowState[id]));
}

function openRowMenu(id) {
  const a = accounts.find((x) => x.id === id);
  if (!a) return;
  menuKind = "row";
  const spec = ticketMenuGroups(a, rowState[id]);
  spec.groups = [{ id: "account", title: "账号", items: rowMainActions(a, rowState[id]) }].concat(spec.groups);
  fillActionSheet(id, accountMail(a, id), a.id, spec);
}

function hideRowMenu() {
  $("menuMask").hidden = true;
  menuKind = "";
}

async function onMenuClick(e) {
  const btn = e.target.closest("button[data-act]");
  if (!btn || btn.disabled) return;
  const act = btn.getAttribute("data-act");
  const id = btn.getAttribute("data-id");
  const keep = btn.getAttribute("data-keep-open") === "1" || act === "showToken";
  const kind = menuKind;
  if (!keep) hideRowMenu();
  const ret = dispatchAction(act, id, btn);
  if (keep) {
    await Promise.resolve(ret);
    if (kind === "ticket") openTicketMenu(id);
    else if (kind === "row") openRowMenu(id);
  }
}

// 一个池子一行：标签 + 百分比 + 细条。三池互不相加。
function poolRow(cls, key, pct, extra) {
  if (pct == null || isNaN(pct)) {
    return `<div class="pool ${cls}"><span class="pool-k">${key}</span><div class="pool-v"><span class="hint">—</span></div></div>`;
  }
  const v = Math.min(100, Math.max(0, Number(pct)));
  const full = v >= 100 ? "full" : "";
  return (
    `<div class="pool ${cls}"><span class="pool-k">${key}</span><div class="pool-v">` +
    `<span class="mono">${esc(fmtPercent(pct))}</span><div class="bar"><i class="${full}" style="width:${v}%"></i></div>${extra || ""}` +
    `</div></div>`
  );
}

function tokenCell(a) {
  if (!rowTokensOn(a.id)) return "";
  const view = tokenViews[a.id] || {};
  const ws = view.worksessionToken || "";
  const rt = view.refreshToken || "";
  const wsBody = ws
    ? `<code class="token-v">${esc(ws)}</code>`
    : `<span class="token-v missing">未读取到 Worksession</span>`;
  const rtBody = rt
    ? `<code class="token-v">${esc(rt)}</code>`
    : `<span class="token-v missing">未记录 · 可先点「探测 Refresh」</span>`;
  const wsCopy = ws
    ? `<button type="button" class="btn tiny" data-act="copyToken" data-id="${esc(a.id)}" data-kind="worksession" title="复制 Worksession token">复制</button>`
    : "";
  const rtCopy = rt
    ? `<button type="button" class="btn tiny" data-act="copyToken" data-id="${esc(a.id)}" data-kind="refresh" title="复制 refresh_token">复制</button>`
    : "";
  return (
    `<tr class="token-detail" data-id="${esc(a.id)}"><td colspan="7">` +
    `<div class="token-box">` +
    `<div class="token-row"><span class="token-k">Worksession</span>${wsBody}${wsCopy}</div>` +
    `<div class="token-row"><span class="token-k">Refresh</span>${rtBody}${rtCopy}</div>` +
    `</div></td></tr>`
  );
}

function showTokensOn() {
  const btn = $("btnShowTokens");
  if (btn) return btn.getAttribute("aria-pressed") === "true";
  const el = $("showTokensChk");
  return el ? el.checked : false;
}

function rowTokensOn(id) {
  return showTokensOn() || tokenOpenIds.has(id);
}

function syncShowTokensButton() {
  const btn = $("btnShowTokens");
  if (!btn) return;
  const on = showTokensOn();
  btn.textContent = on ? "隐藏 Token" : "显示 Token";
  btn.classList.toggle("primary", on);
}

async function loadTokenViews() {
  if (!showTokensOn() && tokenOpenIds.size === 0) {
    tokenViews = {};
    return;
  }
  try {
    const res = await api().list_account_tokens();
    tokenViews = (res && res.ok && res.tokens) || {};
  } catch (e) {
    tokenViews = {};
  }
}

async function setShowTokens(on) {
  const btn = $("btnShowTokens");
  if (btn) btn.setAttribute("aria-pressed", on ? "true" : "false");
  const chk = $("showTokensChk");
  if (chk) chk.checked = !!on;
  if (on) tokenOpenIds.clear();
  else tokenOpenIds.clear();
  syncShowTokensButton();
  await saveSettings({ showTokens: !!on });
  await loadTokenViews();
  render();
}

async function toggleRowToken(id) {
  if (showTokensOn()) {
    await setShowTokens(false);
    return;
  }
  if (tokenOpenIds.has(id)) tokenOpenIds.delete(id);
  else tokenOpenIds.add(id);
  await loadTokenViews();
  render();
}

function quotaCell(st) {
  if (!st || st.kind === "dead") return `<span class="hint">—</span>`;
  const hasAny = st.percent != null || st.autoPercent != null || st.apiPercent != null;
  if (!hasAny) return `<span class="hint">— 点「验证账号」获取</span>`;
  // 混合值 / 月总消费只进 tooltip：它们是汇总口径，不能当「用量」看。
  const tips = [];
  if (st.totalPercent != null) tips.push(`Cursor 混合总用量 ${fmtPercent(st.totalPercent)}（Auto+高级 两池加权，仅参考）`);
  if (st.cycleTotalCents != null) tips.push(`账单月总消费 ${centsToUsd(st.cycleTotalCents)}（含 Bot 与全部模型）`);
  if (st.includedLimitCents != null) tips.push(`已含额度 ${centsToUsd(st.includedUsedCents)} / ${centsToUsd(st.includedLimitCents)}`);
  if (st.hasAvailableUsage === false) tips.push("Bot 本周额度已用尽");
  let html = `<div class="pools" title="${esc(tips.join("\n"))}">`;
  const fresh = isJustReset(st.periodStart) ? `<span class="tag-reset">刚重置</span>` : "";
  html += poolRow("bot", "Bot 周", st.percent, fresh);
  html += poolRow("auto", "Auto 月", st.autoPercent);
  html += poolRow("api", "高级 月", st.apiPercent);
  html += `</div>`;
  const resetMs = toMs(st.nextReset);
  if (st.percent != null && !isNaN(resetMs)) {
    const rel = fmtResetRelative(st.nextReset);
    const est = st.nextResetEstimated ? " · 推算" : "";
    html += `<div class="pool-sub" title="Bot 周额度下次重置：${esc(fmtTs(resetMs))}${st.nextResetEstimated ? "（接口未回重置时间，按周期起点 + 7 天推算）" : ""}">Bot 重置 ${esc(fmtTsShort(resetMs))}${rel ? " · " + esc(rel) : ""}${est}</div>`;
  }
  if (st.onDemandUsedCents != null && Number(st.onDemandUsedCents) > 0) {
    html += `<div class="pool-sub"><span class="pill warn mini" title="按量付费（on-demand）已开且产生了真实扣费">按量已扣 ${esc(centsToUsd(st.onDemandUsedCents))}</span></div>`;
  }
  if (st.spendUsd != null) html += `<div class="pool-sub">团队账单月消费 ${esc(fmtUsd(st.spendUsd))}</div>`;
  return html;
}

function membershipLabel(m) {
  const map = { free: "Free", pro: "Pro", pro_plus: "Pro+", "pro-plus": "Pro+", ultra: "Ultra", enterprise: "企业", team: "Team", business: "Business" };
  return map[String(m).toLowerCase()] || String(m);
}

function planCell(st) {
  if (!st) return `<span class="hint">—</span>`;
  const parts = [];
  if (st.unlimited) parts.push(`<span class="pill ok">无限</span>`);
  else if (st.membership) parts.push(`<span class="pill info">${esc(membershipLabel(st.membership))}</span>`);
  if (st.tierLabel) parts.push(`<span class="pill amount" title="Cursor 档位标签（非美元金额）">档 ${esc(st.tierLabel)}</span>`);
  if (st.teamId) parts.push(`<span class="pill idle">团队</span>`);
  return parts.length ? parts.join(" ") : `<span class="hint">—</span>`;
}

// 点表头时：剩余时间最短在前；Bot 用量高的在前；导入时间新的在前。
// 打开列表时，本机 Cursor 当前登录的账号固定在最上面，其余按导入时间，新添加的在上。
function sortRank(a) {
  const st = rowState[a.id];
  if (st && st.alive === false) return [2, 0];
  const r = remainMs(a, st);
  return isNaN(r) ? [1, 0] : [0, r];
}

function orderedAccounts() {
  return accounts
    .map((a, i) => ({ a, i }))
    .sort((x, y) => {
      const px = localUserId && x.a.id === localUserId ? 0 : 1;
      const py = localUserId && y.a.id === localUserId ? 0 : 1;
      if (px !== py) return px - py;
      const cmp = compareAccounts(x.a, y.a, x.i, y.i);
      return listSort.dir < 0 ? -cmp : cmp;
    })
    .map((o) => o.a);
}

function visibleAccounts() {
  return orderedAccounts().filter(accountMatches);
}

function render() {
  const tbody = $("rows");
  const shown = visibleAccounts();
  tbody.innerHTML = shown
    .map((a) => {
      const st = rowState[a.id];
      const mail = a.label && a.label.includes("@") ? a.label : a.label || a.id;
      const checked = selected.has(a.id) ? " checked" : "";
      const webTok = String(a.tokenType || "").toLowerCase() === "web";
      const tokTag = webTok
        ? `<span class="pill warn mini" title="网站会话：切号时会自动换成客户端登录票，稍慢几秒">网站会话</span>`
        : "";
      const refreshAt = a.refreshRecordedAt ? fmtTs(toMs(a.refreshRecordedAt)) : "";
      const refreshTag = a.hasRefresh
        ? `<span class="pill ok mini" title="已记录 refresh_token${refreshAt ? " · " + refreshAt : ""}">Refresh</span>`
        : "";
      const addedAt = a.addedAt ? fmtTs(toMs(a.addedAt)) : "";
      const checkedAt = st && st.checkedAt ? fmtTs(toMs(st.checkedAt)) : "";
      const meta =
        `<div class="meta">${validityPill(st)}${tokTag}${refreshTag}${sessionPills(a, st)}` +
        `<span title="导入时间">导入 ${esc(addedAt || "—")}</span>` +
        (checkedAt ? `<span title="上次验证时间">验证 ${esc(checkedAt)}</span>` : "") +
        `</div>`;
      const dead = st && st.alive === false;
      const dis = busy ? " disabled" : "";
      const guarding = !!(guardStatus[a.id] && guardStatus[a.id].running);
      const rowCls = [dead ? "dead" : "", guarding ? "guarding" : ""].filter(Boolean).join(" ");
      return `<tr data-id="${esc(a.id)}"${rowCls ? ` class="${rowCls}"` : ""}>
        <td class="col-chk"><input type="checkbox" class="rowchk" data-id="${esc(a.id)}"${checked}${dis} /></td>
        <td><div class="mail">${esc(mail)}</div><div class="uid">${esc(a.id)}</div>${meta}${ticketMenuHtml(a)}
          <div class="row-menu"><button type="button" class="btn tiny" data-act="menu" data-id="${esc(a.id)}" title="全部操作">操作 ▾</button></div></td>
        <td>${planCell(st)}</td>
        <td>${expiryCell(a, st)}</td>
        <td>${statusCell(st)}</td>
        <td class="col-quota">${quotaCell(st)}</td>
        <td class="col-act"><div class="act-wrap">
          ${actionButtonsHtml(a.id, rowMainActions(a, st))}
        </div></td>
      </tr>${tokenCell(a)}`;
    })
    .join("");
  const empty = $("emptyHint");
  const filterEmpty = $("filterEmpty");
  if (empty) empty.hidden = accounts.length > 0;
  if (filterEmpty) filterEmpty.hidden = !(accounts.length > 0 && shown.length === 0);
  $("countPill").textContent = (listFilter !== "all" || listQuery ? shown.length + " / " : "") + accounts.length + " 个";
  syncSelectAll();
  syncBatchButtons();
  syncSortHeaders();
  syncFilterChrome();
  updateStats();
  paintGuardAccountSelect();
}

function syncSelectAll() {
  const all = $("chkAll");
  if (!all) return;
  const vis = visibleAccounts();
  const n = vis.length;
  const sel = vis.filter((a) => selected.has(a.id)).length;
  all.checked = n > 0 && sel === n;
  all.indeterminate = sel > 0 && sel < n;
}

function syncBatchButtons() {
  const none = selectedIds().length === 0 || busy;
  const noAcct = accounts.length === 0 || busy;
  ["btnVerify", "btnClaimAll", "btnGuardSel", "btnGuardStopSel", "btnRemoveSel"].forEach((id) => {
    const el = $(id);
    if (el) el.disabled = none;
  });
  if ($("btnExport")) $("btnExport").disabled = noAcct;
  if ($("btnClear")) $("btnClear").disabled = noAcct;
}

function syncSortHeaders() {
  document.querySelectorAll("th.sortable").forEach((th) => {
    const key = th.getAttribute("data-sort");
    if (listSort.key !== key) {
      th.removeAttribute("aria-sort");
      return;
    }
    const desc = key === "added" ? listSort.dir > 0 : listSort.dir < 0;
    th.setAttribute("aria-sort", desc ? "descending" : "ascending");
  });
}

function updateStats() {
  let expiring = 0;
  let dead = 0;
  let guarding = 0;
  let botFull = 0;
  for (const a of accounts) {
    const st = rowState[a.id];
    if (st && st.alive === false) dead += 1;
    if (guardStatus[a.id] && guardStatus[a.id].running) guarding += 1;
    if (isBotFull(st)) botFull += 1;
    if (isExpiring(a, st)) expiring += 1;
  }
  if ($("statTotal")) $("statTotal").textContent = accounts.length;
  if ($("statExpiring")) $("statExpiring").textContent = expiring;
  if ($("statDead")) $("statDead").textContent = dead;
  if ($("statGuard")) $("statGuard").textContent = guarding;
  if ($("statBotFull")) $("statBotFull").textContent = botFull;
}

// ---- 状态写入：全部「合并」进现有行状态，领取 / 验证 / 轻量刷新互不冲掉对方的数据 ----

function applyClaim(id, res) {
  const prev = rowState[id] || {};
  if (!res) {
    rowState[id] = { ...prev, kind: "bad", label: "无响应", detail: "" };
    return;
  }
  applyEmail(id, res.email);
  const base = { ...prev, detail: "", url: "" };
  if (res.outcome === "already") {
    // 服务端说本来就有资格：不是本次领到的，套餐自带的标记沿用验证结果。
    rowState[id] = { ...base, kind: "ok", label: "已开通", percent: res.percent ?? prev.percent, teamId: res.teamId ?? prev.teamId, alive: true, accessGranted: true };
  } else if (res.outcome === "activated") {
    rowState[id] = { ...base, kind: "ok", label: "新开通", alive: true, accessGranted: true, claimedNow: true };
  } else if (res.outcome === "team_ok") {
    rowState[id] = { ...base, kind: "ok", label: "团队新开通", teamId: res.teamId ?? prev.teamId, alive: true, accessGranted: true, claimedNow: true };
  } else if (res.outcome === "card_required") {
    rowState[id] = { ...base, kind: "card", detail: res.detail || "需验证信用卡", url: res.url || "", alive: true };
  } else {
    // 后端已判死（outcome=dead），或领取请求被服务端 401/403 拒绝 → 标失效，和「验证账号」口径一致。
    const dead = res.outcome === "dead" || /HTTP 40[13]\b/.test(res.detail || "");
    rowState[id] = {
      ...base,
      kind: dead ? "dead" : "bad",
      label: dead ? "失效" : "失败",
      detail: res.detail || "未知原因",
      ...(dead ? { alive: false, aliveReason: res.detail || "" } : {}),
    };
  }
  schedulePersist();
}

function applyStatus(id, res) {
  const prev = rowState[id] || {};
  if (!res || res.error) {
    rowState[id] = { ...prev, kind: "bad", label: "查询失败", detail: (res && res.error) || "" };
    return;
  }
  applyEmail(id, res.email);
  // 已开通 = 资格接口说已授予（权威），或用量接口有非零 Bot 额度（兜底）。
  const unlocked = !!res.unlocked || res.accessGranted === true;
  const keepCard = !unlocked && prev.kind === "card";
  rowState[id] = {
    kind: unlocked ? "ok" : keepCard ? "card" : "idle",
    label: unlocked ? (prev.claimedNow ? prev.label : "已开通") : "未开通",
    claimedNow: unlocked ? !!prev.claimedNow : false,
    detail: keepCard ? prev.detail : "",
    url: keepCard ? prev.url : "",
    alive: res.alive !== undefined ? res.alive : prev.alive,
    aliveReason: res.aliveReason !== undefined ? res.aliveReason : prev.aliveReason,
    checkedAt: res.checkedAt || prev.checkedAt,
    // Sand 资格（权威口径）
    accessGranted: res.accessGranted,
    accessState: res.accessState,
    blockReason: res.blockReason,
    planGrantsAccess: res.planGrantsAccess,
    // 池 1：Bot 周用量
    percent: res.percent,
    hasAvailableUsage: res.hasAvailableUsage,
    nextReset: res.nextReset,
    nextResetEstimated: res.nextResetEstimated,
    periodStart: res.periodStart,
    // 池 2 / 3：Auto / 高级(API)，账单月
    autoPercent: res.autoPercent,
    apiPercent: res.apiPercent,
    totalPercent: res.totalPercent,
    includedUsedCents: res.includedUsedCents,
    includedLimitCents: res.includedLimitCents,
    cycleTotalCents: res.cycleTotalCents,
    onDemandEnabled: res.onDemandEnabled,
    onDemandUsedCents: res.onDemandUsedCents,
    // 套餐 / 订阅
    teamId: res.teamId,
    membership: res.membership,
    unlimited: res.unlimited,
    billingCycleStart: res.billingCycleStart,
    billingCycleEnd: res.billingCycleEnd,
    subscriptionStatus: res.subscriptionStatus,
    pendingCancellationDate: res.pendingCancellationDate,
    isYearlyPlan: res.isYearlyPlan,
    tokenExp: res.tokenExp,
    tierLabel: res.tierLabel,
    spendUsd: res.spendUsd,
    teamPercent: res.teamPercent,
    sessions: Array.isArray(res.sessions) ? res.sessions : [],
    sessionCount: res.sessionCount,
    sessionClientCount: res.sessionClientCount,
    sessionWebCount: res.sessionWebCount,
    sessionError: res.sessionError || "",
    sessionWaf: !!res.sessionWaf,
  };
  schedulePersist();
}

// 领取后的轻量刷新：只更新 Bot 这一池，不碰套餐 / 订阅 / Auto / 高级。
function applySandStatus(id, res) {
  if (!res) return;
  const prev = rowState[id] || {};
  if (res.alive === false) {
    rowState[id] = { ...prev, kind: "dead", label: "失效", detail: res.aliveReason || res.error || "", alive: false, aliveReason: res.aliveReason || "" };
    schedulePersist();
    return;
  }
  if (res.error) return;
  let kind = prev.kind;
  if (res.unlocked) kind = "ok";
  else if (kind === "ok") kind = "idle";
  rowState[id] = {
    ...prev,
    kind,
    label: kind === "ok" ? prev.label || "已开通" : kind === "idle" ? "未开通" : prev.label,
    alive: res.alive === true ? true : prev.alive,
    percent: res.percent,
    hasAvailableUsage: res.hasAvailableUsage,
    nextReset: res.nextReset,
    nextResetEstimated: res.nextResetEstimated,
    periodStart: res.periodStart,
  };
  schedulePersist();
}

function applyVerify(id, res) {
  const prev = rowState[id] || {};
  if (!res || res.error) {
    rowState[id] = { ...prev, kind: "bad", label: "验证失败", detail: (res && res.error) || "" };
    return;
  }
  if (res.alive === false) {
    applyEmail(id, res.email);
    rowState[id] = {
      ...prev,
      kind: "dead",
      label: "失效",
      detail: res.aliveReason || "",
      alive: false,
      aliveReason: res.aliveReason || "",
      checkedAt: res.checkedAt,
      tokenExp: res.tokenExp ?? prev.tokenExp,
    };
    schedulePersist();
    return;
  }
  applyStatus(id, res);
}

// ---- 单个动作 ----

// 行里有没有「完整信息」（套餐 / 订阅 / Auto、高级 用量）——没有的话领取后要补一次完整查询。
function hasFullInfo(st) {
  return !!(st && (st.membership != null || st.subscriptionStatus != null || st.billingCycleEnd != null || st.autoPercent != null));
}

async function claimThenRefresh(id) {
  const res = await api().claim_one(id);
  applyClaim(id, res);
  try {
    const st = rowState[id];
    if (st && st.kind !== "dead") {
      // 已经验证过的号只轻量刷 Bot 池；从没验证过的补一次完整查询，免得表里只有 Bot 一行。
      if (hasFullInfo(st)) applySandStatus(id, await api().sand_status_one(id));
      else applyStatus(id, await api().status_one(id));
    }
  } catch (e) {}
  return res;
}

function markRunning(id) {
  rowState[id] = { ...(rowState[id] || {}), kind: "run" };
}

async function claimOne(id) {
  if (busy) return;
  markRunning(id);
  render();
  const res = await claimThenRefresh(id);
  render();
  return res;
}

async function verifyOne(id) {
  if (busy) return;
  markRunning(id);
  render();
  try {
    applyVerify(id, await api().verify_one(id));
  } catch (e) {
    rowState[id] = { ...(rowState[id] || {}), kind: "bad", label: "异常", detail: String(e) };
  }
  render();
}

async function probeRefreshOne(id) {
  if (busy) return;
  toast("正在探测 refresh_token…");
  try {
    const res = await api().probe_refresh_one(id);
    if (res && res.ok) {
      accounts = res.accounts || accounts;
      const src = res.source === "local" ? "本机" : res.source === "stored" ? "已存" : res.source || "";
      toast(
        `已记录 Refresh${src ? "（" + src + "）" : ""}` +
          (res.sameAsAccess ? " · 与 access 相同（session 票常见）" : "")
      );
      await loadTokenViews();
      render();
      return res;
    } else {
      toast("探测失败：" + ((res && res.error) || "未知原因"));
      return res;
    }
  } catch (e) {
    toast("探测失败：" + String(e));
    return null;
  }
}

async function refreshLoginOne(id) {
  if (busy) return;
  toast("正在用 refresh_token 换取新登录票…");
  try {
    const res = await api().refresh_login_one(id);
    if (res && res.ok) {
      accounts = res.accounts || accounts;
      toast(
        "登录票已刷新" +
          (res.tokenType ? "（" + res.tokenType + "）" : "") +
          (res.usedAccessAsRefresh ? " · 旧 refresh 已被顶替，已改用当前登录票续期" : "")
      );
      await loadTokenViews();
      await reloadOpenDeviceLists(id);
      if (autoVerifyEnabled()) verifyOne(id);
      else render();
      return res;
    } else {
      toast("刷新失败：" + ((res && res.error) || "未知原因"));
      return res;
    }
  } catch (e) {
    toast("刷新失败：" + String(e));
    return null;
  }
}

async function refreshLoginKickOld(id) {
  if (busy) return;
  const bridge = api();
  if (!bridge || !bridge.refresh_login_kick_old) {
    toast("当前版本不支持刷票并踢旧");
    return;
  }
  toast("正在换登录票…");
  try {
    const res = await bridge.refresh_login_kick_old(id);
    if (res && res.ok) {
      accounts = res.accounts || accounts;
      if (res.droppedSessionId) {
        toast(
          "登录票已刷新，并已踢掉旧客户端" +
            (res.usedAccessAsRefresh ? " · 旧 refresh 已被顶替" : "")
        );
      } else {
        toast("登录票已刷新，但没踢到旧客户端：" + (res.kickError || "未知原因"));
      }
      await loadTokenViews();
      await reloadOpenDeviceLists(id);
      if (autoVerifyEnabled()) verifyOne(id);
      else render();
      return res;
    } else {
      toast("刷票并踢旧失败：" + ((res && res.error) || "未知原因"));
      return res;
    }
  } catch (e) {
    toast("刷票并踢旧失败：" + String(e));
    return null;
  }
}

function importPanelOpen() {
  const card = $("importCard");
  return !!(card && !card.classList.contains("is-collapsed"));
}

function setImportPanelOpen(open, persist) {
  const card = $("importCard");
  const btn = $("btnToggleImport");
  if (!card) return;
  const on = !!open;
  card.classList.toggle("is-collapsed", !on);
  if (btn) btn.setAttribute("aria-expanded", on ? "true" : "false");
  if (persist !== false && settings.importOpen !== on) saveSettings({ importOpen: on });
}

function toggleImportPanel() {
  const next = !importPanelOpen();
  setImportPanelOpen(next);
  if (next) {
    const ta = $("tokenInput");
    if (ta) ta.focus();
  }
}

function autoVerifyEnabled() {
  const el = $("autoVerifyChk");
  return el ? el.checked : true;
}

async function detectLocal() {
  toast("正在读取本机 Cursor 登录账号…");
  try {
    const res = await api().detect_local_account();
    if (!res || !res.ok) {
      toast("探测失败：" + ((res && res.error) || "未知原因"));
      return;
    }
    accounts = res.accounts || [];
    await refreshLocalIdentity();
    await loadTokenViews();
    render();
    toast("已探测本机账号：" + (res.email || res.id || "本机"));
    if (res.id && autoVerifyEnabled()) runBatch("verify", [res.id]);
  } catch (e) {
    toast("探测失败：" + String(e));
  }
}

function loginBotConfirmCopy(email, resetMid, webTok, refreshFirst) {
  const who = String(email || "").trim() || "该账号";
  const lines = [
    `确定用 ${who} 登录独立的 Grok Bot 客户端？`,
    "不会关闭 Cursor。会把该号写入 Grok Bot 自带的「Cursor 账户」列表并切过去，然后重启 Grok Bot。",
  ];
  if (refreshFirst) {
    lines.push("会先换新登录票，再用新票写入 Grok Bot。");
  } else {
    lines.push("不会换新登录票，用的是列表里当前这张票。");
  }
  if (webTok) lines.push("这是网站会话票，会先换成客户端票再写入 Grok Bot。");
  if (resetMid) lines.push("「切号重置机器码」对登录 Bot 无效：不会改 Cursor 的机器码。");
  return lines;
}

function switchConfirmCopy(email, resetMid, webTok, refreshFirst) {
  const who = String(email || "").trim() || "该账号";
  const lines = [
    `确定把本机 Cursor 切到 ${who}？`,
    "会先关掉当前 Cursor，写入登录态后再自动重启。",
  ];
  if (refreshFirst) {
    lines.push("会先换新登录票，再用新票写入 Cursor，本机标签会落在最新那台设备上。");
  } else {
    lines.push("不会换新登录票，写入的是列表里当前这张票。");
  }
  if (webTok) lines.push("这是网站会话，切号时会先换成客户端登录票，大约多几秒。");
  if (resetMid) lines.push("已勾选「切号重置机器码」，本机机器码也会一起换掉。");
  return lines;
}

function syncSwitchKickOldEnabled() {
  const refresh = $("switchRefreshFirstChk");
  const kick = $("switchKickOldChk");
  if (!kick) return;
  const on = !!(refresh && refresh.checked);
  kick.disabled = !on;
}

let pendingSwitchId = null;
let pendingSwitchKind = "switch";

function paintSwitchConfirmChrome() {
  const title = $("switchConfirmTitle");
  const ok = $("switchConfirmOk");
  const urlWrap = $("loginBotUrlWrap");
  const opts = $("switchConfirmOpts");
  if (urlWrap) urlWrap.hidden = true;
  const inp = $("loginBotUrlInput");
  if (inp) inp.value = "";
  const loginBot = pendingSwitchKind === "loginBot";
  if (opts) opts.hidden = loginBot;
  if (loginBot) {
    if (title) title.textContent = "登录 Bot 确认";
    if (ok) ok.textContent = "确认登录 Bot";
  } else {
    if (title) title.textContent = "切号确认";
    if (ok) ok.textContent = "确认切号";
  }
}

function paintSwitchConfirmBody() {
  const id = pendingSwitchId;
  if (!id) return;
  const a = accounts.find((x) => x.id === id);
  const resetMid = !!($("resetMidChk") && $("resetMidChk").checked);
  const webTok = a && String(a.tokenType || "").toLowerCase() === "web";
  const refreshFirst = pendingSwitchKind === "loginBot"
    ? false
    : !!($("switchRefreshFirstChk") && $("switchRefreshFirstChk").checked);
  const copyFn = pendingSwitchKind === "loginBot" ? loginBotConfirmCopy : switchConfirmCopy;
  const body = $("switchConfirmBody");
  if (body) {
    body.innerHTML = copyFn(accountMail(a, id), resetMid, webTok, refreshFirst)
      .map((line) => `<p class="hint">${esc(line)}</p>`)
      .join("");
  }
  paintSwitchConfirmChrome();
}

function openSwitchConfirm(id) {
  if (!id) return;
  pendingSwitchId = id;
  pendingSwitchKind = "switch";
  const refreshChk = $("switchRefreshFirstChk");
  const kickChk = $("switchKickOldChk");
  if (refreshChk) refreshChk.checked = true;
  if (kickChk) kickChk.checked = true;
  syncSwitchKickOldEnabled();
  paintSwitchConfirmBody();
  const el = $("switchConfirmMask");
  if (el) el.hidden = false;
}

function openLoginBotConfirm(id) {
  if (!id) return;
  pendingSwitchId = id;
  pendingSwitchKind = "loginBot";
  paintSwitchConfirmBody();
  const el = $("switchConfirmMask");
  if (el) el.hidden = false;
}

function hideSwitchConfirm() {
  pendingSwitchId = null;
  pendingSwitchKind = "switch";
  const el = $("switchConfirmMask");
  if (el) el.hidden = true;
}

function isSwitchConfirmOpen() {
  const el = $("switchConfirmMask");
  return !!(el && !el.hidden);
}

function confirmSwitch() {
  const id = pendingSwitchId;
  const kind = pendingSwitchKind;
  const refreshFirst = !!($("switchRefreshFirstChk") && $("switchRefreshFirstChk").checked);
  const kickOld =
    refreshFirst && !!($("switchKickOldChk") && $("switchKickOldChk").checked);
  hideSwitchConfirm();
  if (!id) return;
  if (kind === "loginBot") loginBot(id, false, false);
  else switchAccount(id, refreshFirst, kickOld);
}

async function switchAccount(id, refreshFirst, kickOld) {
  const resetMid = !!($("resetMidChk") && $("resetMidChk").checked);
  const a = accounts.find((x) => x.id === id);
  const webTok = a && String(a.tokenType || "").toLowerCase() === "web";
  const willRefresh = refreshFirst !== false;
  const willKick = willRefresh && !!kickOld;
  toast(
    (willRefresh ? "正在换新登录票并切号… " : "") +
      (webTok ? "网站会话切号：正在换客户端登录票（约几秒）… " : "") +
      (resetMid ? "正在切号并重置机器码，Cursor 将自动重启…" : "正在切号，Cursor 将自动重启…")
  );
  try {
    const res = await api().switch_account(id, resetMid, willRefresh, willKick);
    if (res && res.ok) {
      const bits = ["已切换到 " + (res.email || id)];
      if (res.refreshed) bits.push("已换新登录票");
      if (res.droppedSessionId) bits.push("已踢旧客户端");
      if (res.pinnedSessionId) bits.push("本机已标在最新设备");
      if (res.resetMachineId) bits.push("已重置机器码");
      if (res.exchanged) bits.push("网站会话已换客户端票");
      bits.push("Cursor 正在重启");
      toast(bits.join(" · ") + (res.warning ? "　⚠ " + res.warning : ""));
      applySessionBlock(id, res);
      await refreshLocalIdentity();
      render();
    } else {
      toast("切号失败：" + ((res && res.error) || "未知原因"));
    }
  } catch (e) {
    toast("切号失败：" + String(e));
  }
}

async function loginBot(id, refreshFirst, kickOld) {
  const resetMid = !!($("resetMidChk") && $("resetMidChk").checked);
  const a = accounts.find((x) => x.id === id);
  const webTok = a && String(a.tokenType || "").toLowerCase() === "web";
  const willRefresh = refreshFirst !== false;
  const willKick = willRefresh && !!kickOld;
  toast(
    (willRefresh ? "正在换新登录票并登录 Bot… " : "") +
      (webTok ? "网站会话：正在换客户端票… " : "") +
      "正在写入 Grok Bot 账户列表并重启客户端（不会关闭 Cursor）…"
  );
  try {
    const res = await api().login_bot(id, resetMid, willRefresh, willKick);
    if (res && res.ok) {
      const bits = ["已写入 Grok Bot 账户并切换：" + (res.email || id)];
      if (res.refreshed) bits.push("已换新登录票");
      if (res.droppedSessionId) bits.push("已踢旧客户端");
      if (res.exchanged) bits.push("网站会话已换客户端票");
      bits.push("Grok Bot 正在重启");
      toast(bits.join(" · ") + (res.warning ? "　⚠ " + res.warning : ""));
      applySessionBlock(id, res);
      await refreshLocalIdentity();
      render();
    } else {
      toast("登录 Bot 失败：" + ((res && res.error) || "未知原因"));
    }
  } catch (e) {
    toast("登录 Bot 失败：" + String(e));
  }
}

async function openLogin(id) {
  toast("正在打开浏览器并注入登录，请稍候…");
  try {
    const res = await api().open_login(id);
    if (res && res.ok) {
      toast(`已在 ${res.browser === "edge" ? "Edge" : "Chrome"} 打开领取页，请在浏览器里手动完成`);
    } else {
      toast("打开失败：" + ((res && res.error) || "未知原因"));
    }
  } catch (e) {
    toast("打开失败：" + String(e));
  }
}

// ---- 批量 ----

function selectedIds() {
  return accounts.filter((a) => selected.has(a.id)).map((a) => a.id);
}

function targetIds() {
  return selectedIds();
}

function readConcurrency() {
  const el = $("concInput");
  let n = parseInt(el && el.value, 10);
  if (isNaN(n)) n = 3;
  return Math.min(10, Math.max(1, n));
}

// kind: "claim" 领取 | "verify" 验证。idsOverride 给「导入后自动验证」用，只处理刚导入的号。
async function runBatch(kind, idsOverride) {
  if (busy || accounts.length === 0) return;
  const known = new Set(accounts.map((a) => a.id));
  const ids = idsOverride ? idsOverride.filter((id) => known.has(id)) : targetIds();
  if (ids.length === 0) {
    toast(idsOverride ? "没有可处理的账号" : "先勾选要处理的账号");
    return;
  }
  const conc = readConcurrency();
  busy = true;
  render();
  const wrap = $("progressWrap");
  const bar = $("progressBar");
  const text = $("progressText");
  wrap.hidden = false;
  const total = ids.length;
  const label = kind === "claim" ? "领取" : "验证";
  let done = 0;
  let next = 0;
  bar.style.width = "0%";
  text.textContent = `${label} 0/${total}（并发 ${conc}）`;
  const tally = { activated: 0, already: 0, card: 0, failed: 0, alive: 0, dead: 0, unknown: 0 };

  async function worker() {
    while (next < ids.length) {
      const id = ids[next++];
      markRunning(id);
      render();
      try {
        if (kind === "claim") {
          const res = await claimThenRefresh(id);
          const o = res && res.outcome;
          if (o === "activated" || o === "team_ok") tally.activated += 1;
          else if (o === "already") tally.already += 1;
          else if (o === "card_required") tally.card += 1;
          else if (o === "dead") tally.dead += 1;
          else tally.failed += 1;
        } else {
          applyVerify(id, await api().verify_one(id));
          const st = rowState[id];
          if (st && st.alive === true) tally.alive += 1;
          else if (st && st.alive === false) tally.dead += 1;
          else tally.unknown += 1;
        }
      } catch (e) {
        rowState[id] = { ...(rowState[id] || {}), kind: "bad", label: "异常", detail: String(e) };
        tally.failed += 1;
      }
      done += 1;
      bar.style.width = ((done / total) * 100).toFixed(1) + "%";
      text.textContent = `${label} ${done}/${total}（并发 ${conc}）`;
      render();
    }
  }

  const workers = [];
  for (let i = 0; i < Math.min(conc, total); i++) workers.push(worker());
  await Promise.all(workers);

  bar.style.width = "100%";
  text.textContent = `完成 ${done}/${total}`;
  busy = false;
  render();
  setTimeout(() => (wrap.hidden = true), 1500);
  const scope = idsOverride ? "（刚导入）" : "（仅选中）";
  if (kind === "claim") {
    toast(
      `领取完成 ${total} 个${scope}：新开通 ${tally.activated}，本来就已开通 ${tally.already}，` +
        `需绑卡 ${tally.card}，失败 ${tally.failed}` +
        (tally.dead ? `，失效 ${tally.dead}` : "")
    );
  } else {
    // 一批里全军覆没时多半不是账号问题（也可能是网络 / DNS 劫持），提示一句免得误删。
    const allDead = total >= 3 && tally.dead === total;
    toast(
      `验证完成 ${total} 个${scope}：有效 ${tally.alive}，失效 ${tally.dead}` +
        (tally.unknown ? `，未判定 ${tally.unknown}` : "") +
        (tally.failed ? `，异常 ${tally.failed}` : "") +
        (allDead ? "。全部失效？也可能是网络 / DNS 问题，建议稍后再验证一次再决定删除" : "")
    );
  }
}

// 导入之后：刷新列表，并按设置自动验证刚导入的账号（识别邮箱 / 用量 / 订阅剩余）。
async function afterImport(res, verb) {
  accounts = (res && res.accounts) || [];
  await loadTokenViews();
  render();
  const ids = (res && res.ids) || [];
  if (!ids.length) {
    toast(verb === "导入" ? "未识别到账号" : "未识别到有效 token");
    return;
  }
  if (autoVerifyEnabled()) {
    toast(`${verb} ${ids.length} 个账号，正在自动验证…`);
    await runBatch("verify", ids);
  } else {
    toast(`${verb} ${ids.length} 个账号（未自动验证，勾选后点「验证账号」）`);
  }
}

async function importFiles() {
  if (busy) return;
  const res = await api().import_files();
  await afterImport(res, "导入");
}

async function addText() {
  if (busy) return;
  const text = $("tokenInput").value.trim();
  if (!text) {
    toast("先粘贴 token 或 JSON");
    return;
  }
  const res = await api().import_text(text);
  if (res && res.ids && res.ids.length) $("tokenInput").value = "";
  await afterImport(res, "添加");
}

async function clearAll() {
  if (busy) return;
  if (accounts.length && !window.confirm(`确定清空全部 ${accounts.length} 个账号？此操作不可恢复。`)) return;
  accounts = await api().clear_accounts();
  for (const k of Object.keys(rowState)) delete rowState[k];
  selected.clear();
  lastPersisted = {};
  tokenViews = {};
  api().save_status({});
  render();
  toast("已清空");
}

async function removeSelected() {
  if (busy) return;
  const ids = selectedIds();
  if (!ids.length) {
    toast("先勾选要删除的账号（左侧复选框）");
    return;
  }
  if (!window.confirm(`确定删除勾选的 ${ids.length} 个账号？此操作不可恢复。`)) return;
  try {
    const res = await api().remove_accounts(ids);
    accounts = (res && res.accounts) || [];
    for (const id of ids) {
      delete rowState[id];
      delete lastPersisted[id];
      delete tokenViews[id];
      selected.delete(id);
    }
    schedulePersist();
    render();
    toast(`已删除 ${(res && res.removed) ?? ids.length} 个账号`);
  } catch (e) {
    toast("删除失败：" + String(e));
  }
}

// 写剪贴板：优先浏览器 API，失败退回隐藏 textarea + execCommand，再失败交给 Python 的 clip.exe。
async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) {}
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (ok) return true;
  } catch (e) {}
  try {
    const bridge = api();
    if (bridge && bridge.clip_set) {
      const r = await bridge.clip_set(text);
      return !!(r && r.ok);
    }
  } catch (e) {}
  return false;
}

async function copyTokenOne(id, kind) {
  const view = tokenViews[id] || {};
  const text = kind === "refresh" ? view.refreshToken : view.worksessionToken;
  if (!text) {
    toast(kind === "refresh" ? "还没有 refresh_token，请先探测 Refresh" : "没有 Worksession token");
    return;
  }
  const ok = await copyText(text);
  toast(ok ? (kind === "refresh" ? "已复制 Refresh token" : "已复制 Worksession token") : "复制失败");
}

async function copyAccount(id) {
  let res;
  try {
    res = await api().account_export_text(id);
  } catch (e) {
    res = null;
  }
  if (!res || !res.ok) {
    toast("复制失败：" + ((res && res.error) || "账号不存在"));
    return;
  }
  const ok = await copyText(res.text);
  toast(ok ? `已复制 ${res.email || id}（邮箱----user::token）` : "复制失败：请重试或手动复制");
}

// ---- 导出：先按套餐分大类（Ultra > Pro+ > Pro > 团队 > Free），大类里再分「未用 Bot / 已用 Bot / 不续费」；
//      分类互斥、段内按剩余时间从短到长、每段头部注明分类依据与每个号的到期 / 用量 ----

// 是否用过 bot：Sand 本周用量 > 0（就是列表里「Bot 周」那个百分比）。
function usedBotQuota(st) {
  const p = Number(st && st.percent);
  return !isNaN(p) && p > 0;
}

// 套餐归一：接口给的是 pro / pro_plus / ultra / enterprise / team / free / free_trial …
function tierOf(st) {
  const raw = String((st && st.membership) || "")
    .toLowerCase()
    .replace(/\+/g, "plus")
    .replace(/[\s_\-]/g, "");
  if (raw === "ultra") return "ultra";
  if (raw === "proplus") return "proplus";
  if (raw === "pro") return "pro";
  if (raw === "enterprise" || raw === "team" || raw === "business") return "team";
  if (!raw && st && st.unlimited) return "team";
  return "free";
}

const TIER_ORDER = ["ultra", "proplus", "pro", "team", "free"];
const TIER_TITLE = { ultra: "Ultra", proplus: "Pro+", pro: "Pro", team: "团队 / 企业", free: "Free / 未知套餐" };
// 付费大类里的小类；Free 大类只分有没有 Sand。
const SUB_SPECS = {
  clean: { title: "未用 Bot 额度 · 自动续费", note: "订阅 active 且未申请取消 · 本周 Bot 用量为 0（干净号）" },
  used: { title: "已用 Bot 额度 · 自动续费（慎用）", note: "订阅 active 且未申请取消 · 本周 Bot 用量 > 0" },
  norenew: { title: "到期不续 / 未续费", note: "订阅已申请取消、非 active 或试用中；含已用与未用，看每行注释里的 Bot 用量" },
  sand: { title: "有 Sand 资格", note: "免费 / 未知套餐，但 Sand 已开通" },
  nosand: { title: "无 Sand（需绑卡 / 未开通 / 领取失败）", note: "免费号领取 Sand 需先绑卡" },
};
const TAIL_SPECS = {
  dead: { title: "失效账号", note: "验证为失效：登录票过期或服务端拒绝（401/403/无会话），切号 / 领取都不可用" },
  unverified: { title: "未验证 / 查询失败", note: "没有套餐与订阅数据，先点「验证账号」再导出更准" },
};

function bucketOrder() {
  const keys = [];
  for (const tier of TIER_ORDER) {
    if (tier === "free") keys.push("free:sand", "free:nosand");
    else keys.push(`${tier}:clean`, `${tier}:used`, `${tier}:norenew`);
  }
  keys.push("dead", "unverified");
  return keys;
}

function bucketSpec(key) {
  if (TAIL_SPECS[key]) return TAIL_SPECS[key];
  const [tier, sub] = key.split(":");
  const spec = SUB_SPECS[sub];
  return { title: `${TIER_TITLE[tier]} · ${spec.title}`, note: `套餐 ${TIER_TITLE[tier]} · ${spec.note}` };
}

// 互斥分类：一个号只进一段。失效 / 未验证单独放最后；其余先看套餐，再看有没有用过 Bot、是否续费。
function exportBucket(a) {
  const st = rowState[a.id];
  if (!st || st.kind === "run") return "unverified";
  if (st.alive === false || st.kind === "dead") return "dead";
  // 套餐 / 订阅从没查到过：不能硬判套餐，归到未验证段。
  if (!hasFullInfo(st)) return "unverified";
  const tier = tierOf(st);
  if (tier === "free") return `free:${st.kind === "ok" ? "sand" : "nosand"}`;
  const renewing = st.subscriptionStatus === "active" && !st.pendingCancellationDate;
  if (!renewing) return `${tier}:norenew`;
  return `${tier}:${usedBotQuota(st) ? "used" : "clean"}`;
}

// 每个号在段头的一行注释：到期 + 剩余 + 套餐 + Sand + 三池用量（账号行本身保持干净）。
function annotationFor(a, st) {
  const parts = [];
  const subMs = subEndMs(st);
  if (!isNaN(subMs)) {
    const renew = st.pendingCancellationDate ? "，到期不续" : st.subscriptionStatus === "active" ? "，自动续费" : st.subscriptionStatus ? `，${st.subscriptionStatus}` : "";
    parts.push(`订阅到期 ${fmtTs(subMs)}（${relRemain(subMs - Date.now())}${renew}）`);
  } else {
    const t = expMs(a.exp);
    if (!isNaN(t)) parts.push(`登录票到期 ${fmtTs(t)}（${relRemain(t - Date.now())}）`);
  }
  if (st) {
    if (st.membership) parts.push(membershipLabel(st.membership) + (st.isYearlyPlan ? "年付" : ""));
    if (st.kind === "ok") parts.push(st.claimedNow ? "Sand 本次领取" : "Sand 已开通" + (st.planGrantsAccess && tierOf(st) !== "free" ? "（套餐自带）" : ""));
    else if (st.kind === "card") parts.push("Sand 需绑卡");
    else if (st.kind === "idle" || st.kind === "bad") parts.push("Sand 未开通");
    const full = (p) => (Number(p) >= 100 ? "（已满）" : "");
    if (st.percent != null) parts.push(`Bot ${fmtPercent(st.percent)}${usedBotQuota(st) ? "（已用）" : ""}`);
    if (st.autoPercent != null) parts.push(`Auto ${fmtPercent(st.autoPercent)}${full(st.autoPercent)}`);
    if (st.apiPercent != null) parts.push(`高级 ${fmtPercent(st.apiPercent)}${full(st.apiPercent)}`);
    if (st.onDemandUsedCents != null && Number(st.onDemandUsedCents) > 0) parts.push(`按量已扣 ${centsToUsd(st.onDemandUsedCents)}`);
    if (st.alive === false) parts.push(`失效：${st.aliveReason || ""}`);
  }
  if (a.addedAt) parts.push(`导入 ${fmtTs(toMs(a.addedAt))}`);
  return parts.join(" · ");
}

async function exportAllClassified() {
  if (accounts.length === 0) {
    toast("列表为空，没有可导出的账号");
    return;
  }
  const order = bucketOrder();
  const buckets = {};
  for (const key of order) buckets[key] = [];
  const seen = new Set();
  for (const a of accounts) {
    if (seen.has(a.id)) continue;
    seen.add(a.id);
    const key = exportBucket(a);
    (buckets[key] || buckets.unverified).push(a);
  }
  // 段内按剩余时间从短到长（最先到期在最前）；没有时间数据的排最后。
  const byRemain = (x, y) => {
    const rx = remainMs(x, rowState[x.id]);
    const ry = remainMs(y, rowState[y.id]);
    if (isNaN(rx) && isNaN(ry)) return 0;
    if (isNaN(rx)) return 1;
    if (isNaN(ry)) return -1;
    return rx - ry;
  };
  let no = 0;
  const sections = order
    .filter((key) => buckets[key].length)
    .map((key) => {
      const spec = bucketSpec(key);
      const list = buckets[key].slice().sort(byRemain);
      const annotations = {};
      for (const a of list) annotations[a.id] = annotationFor(a, rowState[a.id]);
      no += 1;
      return { title: `${no}. ${spec.title}`, note: spec.note, ids: list.map((a) => a.id), annotations };
    });
  // 大类汇总：Ultra x · Pro+ y · Pro z …（放在文件头，一眼看到各套餐有多少个）。
  const tierCount = {};
  for (const key of order) {
    if (!buckets[key].length) continue;
    const tier = TAIL_SPECS[key] ? key : key.split(":")[0];
    tierCount[tier] = (tierCount[tier] || 0) + buckets[key].length;
  }
  const summary = [...TIER_ORDER, "dead", "unverified"]
    .filter((t) => tierCount[t])
    .map((t) => `${TIER_TITLE[t] || TAIL_SPECS[t].title} ${tierCount[t]}`)
    .join(" · ");
  const unrefreshed = buckets.unverified.length;
  const header = [
    `cursor账号管理器 导出 ${fmtTs(Date.now())} · 共 ${seen.size} 个账号 · 先按套餐分大类（Ultra > Pro+ > Pro > 团队 > Free），大类里再分「未用 Bot / 已用 Bot / 不续费」，段内按剩余时间从短到长（最先到期在最前）`,
    `套餐分布：${summary}`,
    "账号行格式：邮箱----user_id::token（可原样粘回导入）；以 # 开头的是注释，[n] 与下方第 n 行账号一一对应",
  ];
  try {
    const res = await api().export_accounts({ sections, header });
    if (res && res.ok) {
      if (res.text) await copyText(res.text);
      toast(
        `已导出 ${res.count} 个账号，${sections.length} 段（${summary}），已复制` +
          (unrefreshed ? `；其中 ${unrefreshed} 个未验证` : "")
      );
    } else if (res && res.error === "已取消") {
      toast("已取消导出");
    } else {
      toast("导出失败：" + ((res && res.error) || "未知原因"));
    }
  } catch (e) {
    toast("导出失败：" + String(e));
  }
}

// ---- 事件 ----

function onTableClick(e) {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  dispatchAction(btn.getAttribute("data-act"), btn.getAttribute("data-id"), btn);
}

// 表格操作列 / 账号格标签 / 窄屏「操作」菜单 共用一个分发口。
function dispatchAction(act, id, btn) {
  if (!id) return;
  // 查看设备 / 进控制台 / 本机保护 与批量领取、验证互不影响，忙碌时也可用。
  if (act === "sessions" || act === "devices") {
    openSessions(id);
    return;
  }
  if (act === "guardinfo") {
    openGuard(id);
    return;
  }
  if (act === "guard") {
    openGuard(id);
    return;
  }
  if (act === "dashboard") {
    openDashboard(id);
    return;
  }
  if (act === "menu") {
    openRowMenu(id);
    return;
  }
  if (act === "ticketMenu") {
    openTicketMenu(id);
    return;
  }
  if (act === "showToken") {
    return toggleRowToken(id);
  }
  if (act === "copyToken") {
    copyTokenOne(id, btn && btn.getAttribute("data-kind"));
    return;
  }
  if (busy) return;
  if (act === "remove") {
    api()
      .remove_account(id)
      .then((list) => {
        accounts = list || [];
        delete rowState[id];
        delete lastPersisted[id];
        delete tokenViews[id];
        selected.delete(id);
        schedulePersist();
        render();
      });
  } else if (act === "browser") {
    openLogin(id);
  } else if (act === "switch") {
    openSwitchConfirm(id);
  } else if (act === "loginBot") {
    openLoginBotConfirm(id);
  } else if (act === "copy") {
    copyAccount(id);
  } else if (act === "claim") {
    claimOne(id);
  } else if (act === "verify") {
    verifyOne(id);
  } else if (act === "probeRefresh") {
    probeRefreshOne(id);
  } else if (act === "refreshLogin") {
    refreshLoginOne(id);
  } else if (act === "refreshLoginKickOld") {
    refreshLoginKickOld(id);
  } else if (act === "pinLocal") {
    pinLocalGuard(id);
  }
}

function onTableChange(e) {
  const chk = e.target.closest("input.rowchk");
  if (!chk) return;
  const id = chk.getAttribute("data-id");
  if (chk.checked) selected.add(id);
  else   selected.delete(id);
  syncSelectAll();
  syncBatchButtons();
}

function onSelectAll(e) {
  if (e.target.checked) visibleAccounts().forEach((a) => selected.add(a.id));
  else visibleAccounts().forEach((a) => selected.delete(a.id));
  render();
}

async function saveSettings(patch) {
  settings = { ...settings, ...patch };
  const bridge = api();
  if (bridge && bridge.set_settings) {
    try {
      await bridge.set_settings(settings);
    } catch (e) {}
  }
}

function setMainTab(name, persist) {
  const tab = name === "agents" || name === "guard" ? name : "accounts";
  const buttons = {
    accounts: $("tabAccounts"),
    guard: $("tabGuard"),
    agents: $("tabAgents"),
  };
  const panes = {
    accounts: $("paneAccounts"),
    guard: $("paneGuard"),
    agents: $("paneAgents"),
  };
  for (const key of ["accounts", "guard", "agents"]) {
    const on = key === tab;
    const btn = buttons[key];
    const pane = panes[key];
    if (btn) {
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    }
    if (pane) pane.hidden = !on;
  }
  if (tab === "guard") {
    paintGuardPage();
    if (guardModal.id) startGuardPanelTimer();
  } else {
    stopGuardPanelTimer();
  }
  if (persist !== false && settings.mainTab !== tab) saveSettings({ mainTab: tab });
}

function onMainTabClick(e) {
  const btn = e.target.closest("[data-tab]");
  if (!btn) return;
  const name = btn.getAttribute("data-tab");
  if (name === "guard") {
    const id = guardModal.id || firstRunningGuardId();
    if (id) {
      openGuard(id);
      return;
    }
  }
  setMainTab(name);
}

function fmtAgentTime(iso) {
  const ms = toMs(iso);
  return fmtTs(ms) || String(iso || "—");
}

function renderApiKeys() {
  const pill = $("apiKeyCountPill");
  if (pill) pill.textContent = `${apiKeys.length} 把密钥`;
  const empty = $("apiKeyEmpty");
  const list = $("apiKeyList");
  if (!list) return;
  if (empty) empty.hidden = apiKeys.length > 0;
  list.innerHTML = apiKeys
    .map((k) => {
      const id = esc(k.id);
      const email = esc(k.userEmail || "（未返回邮箱）");
      const name = esc(k.apiKeyName || "未命名密钥");
      const uid = k.userId != null && k.userId !== "" ? esc(String(k.userId)) : "—";
      const block = agentLists[k.id] || {};
      let body = "";
      if (block.loading) {
        body = `<p class="hint agent-empty">正在拉取云端 Agent…</p>`;
      } else if (block.error) {
        body = `<p class="agent-error">${esc(block.error)}</p>`;
      } else if (block.agents) {
        if (!block.agents.length) {
          body = `<p class="hint agent-empty">没有云端 Agent</p>`;
        } else {
          const rows = block.agents
            .map((a) => {
              const aid = esc(a.id || "");
              const st = esc(a.status || "");
              const created = esc(fmtAgentTime(a.createdAt));
              const nm = esc((a.name || "").replace(/\n/g, " "));
              return (
                `<tr><td class="mono">${aid}</td><td>${st}</td><td>${created}</td><td>${nm}</td>` +
                `<td class="agent-act"><button type="button" class="btn tiny danger" data-act="deleteAgent" data-id="${id}" data-agent="${aid}">删除</button></td></tr>`
              );
            })
            .join("");
          body =
            `<div class="agent-table-wrap"><table><thead><tr>` +
            `<th>ID</th><th>STATUS</th><th>CREATED</th><th>NAME</th><th></th>` +
            `</tr></thead><tbody>${rows}</tbody></table></div>`;
        }
      }
      return (
        `<div class="api-key-block" data-id="${id}">` +
        `<div class="api-key-head">` +
        `<div><div class="mail">${email}</div>` +
        `<div class="uid">${name} · userId ${uid}</div></div>` +
        `<div class="act-wrap">` +
        `<button type="button" class="btn tiny" data-act="listAgents" data-id="${id}">列出 Agent</button>` +
        `<button type="button" class="btn tiny danger" data-act="cleanAgents" data-id="${id}">一键清理</button>` +
        `<button type="button" class="btn tiny" data-act="removeApiKey" data-id="${id}">移除密钥</button>` +
        `</div></div>${body}</div>`
      );
    })
    .join("");
}

async function loadApiKeys() {
  const bridge = api();
  if (!bridge || !bridge.list_api_keys) return;
  try {
    apiKeys = (await bridge.list_api_keys()) || [];
  } catch (e) {
    apiKeys = [];
  }
  renderApiKeys();
  await restoreLastApiKeyInput();
}

let lastApiKeySaveTimer = 0;

async function restoreLastApiKeyInput() {
  const ta = $("apiKeyInput");
  if (!ta || ta.value.trim()) return;
  const bridge = api();
  if (!bridge || !bridge.get_last_api_key_input) return;
  try {
    const text = await bridge.get_last_api_key_input();
    if (typeof text === "string" && text) ta.value = text;
  } catch (e) {}
}

function scheduleSaveLastApiKeyInput() {
  const ta = $("apiKeyInput");
  const text = (ta && ta.value) || "";
  clearTimeout(lastApiKeySaveTimer);
  lastApiKeySaveTimer = setTimeout(() => {
    saveLastApiKeyInput(text);
  }, 400);
}

async function saveLastApiKeyInput(text) {
  const bridge = api();
  if (!bridge || !bridge.set_last_api_key_input) return;
  try {
    await bridge.set_last_api_key_input(text || "");
  } catch (e) {}
}

async function addApiKeys() {
  const ta = $("apiKeyInput");
  const text = (ta && ta.value) || "";
  if (!text.trim()) {
    toast("先粘贴 crsr_ API Key");
    return;
  }
  const bridge = api();
  if (!bridge || !bridge.import_api_keys) {
    toast("当前预览不支持添加密钥");
    return;
  }
  let res;
  try {
    res = await bridge.import_api_keys(text);
  } catch (e) {
    toast("添加失败：" + String(e));
    return;
  }
  apiKeys = (res && res.keys) || apiKeys;
  const added = (res && res.added) || [];
  const failed = (res && res.failed) || [];
  renderApiKeys();
  const bits = [];
  if (added.length) bits.push(`已添加 ${added.length} 把`);
  if (failed.length) bits.push(`失败 ${failed.length}：${failed.map((f) => f.error || f.line).join("；")}`);
  toast(bits.join("。") || "未识别到 API Key");
}

async function listCloudAgents(keyId) {
  const bridge = api();
  if (!bridge || !bridge.list_cloud_agents) return { ok: false, error: "接口不可用", agents: [] };
  agentLists[keyId] = { loading: true, error: "", agents: null };
  renderApiKeys();
  try {
    const res = await bridge.list_cloud_agents(keyId);
    if (!res || !res.ok) {
      agentLists[keyId] = { loading: false, error: (res && res.error) || "列出失败", agents: [] };
      renderApiKeys();
      return res || { ok: false, error: "列出失败", agents: [] };
    }
    agentLists[keyId] = { loading: false, error: "", agents: res.agents || [] };
    renderApiKeys();
    return res;
  } catch (e) {
    agentLists[keyId] = { loading: false, error: String(e), agents: [] };
    renderApiKeys();
    return { ok: false, error: String(e), agents: [] };
  }
}

async function cleanCloudAgents(keyId) {
  const listed = await listCloudAgents(keyId);
  if (!listed || !listed.ok) {
    toast("列出失败：" + ((listed && listed.error) || "未知错误"));
    return;
  }
  const n = (listed.agents || []).length;
  if (!n) {
    toast("该密钥下没有云端 Agent");
    return;
  }
  if (!window.confirm(`确定永久删除该密钥下 ${n} 个云端 Agent？此操作不可恢复。`)) return;
  const bridge = api();
  agentLists[keyId] = { loading: true, error: "", agents: listed.agents };
  renderApiKeys();
  try {
    const out = await bridge.delete_all_cloud_agents(keyId);
    if (!out || !out.ok) {
      const extra = out && out.failed ? `成功 ${out.deleted || 0}，失败 ${out.failed}` : (out && out.error) || "清理失败";
      toast("清理未完成：" + extra);
      await listCloudAgents(keyId);
      return;
    }
    agentLists[keyId] = { loading: false, error: "", agents: [] };
    renderApiKeys();
    toast(`已永久删除 ${out.deleted || n} 个云端 Agent`);
  } catch (e) {
    toast("清理失败：" + String(e));
    await listCloudAgents(keyId);
  }
}

async function removeApiKey(keyId) {
  const row = apiKeys.find((k) => k.id === keyId);
  const label = (row && (row.userEmail || row.apiKeyName)) || keyId;
  if (!window.confirm(`确定移除密钥 ${label}？不会删除云端 Agent，只是从本机列表拿掉。`)) return;
  try {
    const res = await api().remove_api_key(keyId);
    apiKeys = (res && res.keys) || [];
    delete agentLists[keyId];
    renderApiKeys();
    toast(res && res.ok ? "已移除密钥" : (res && res.error) || "移除失败");
  } catch (e) {
    toast("移除失败：" + String(e));
  }
}

async function deleteCloudAgent(keyId, agentId) {
  const aid = String(agentId || "").trim();
  if (!aid) return;
  const block = agentLists[keyId] || {};
  const agent = (block.agents || []).find((a) => a && a.id === aid);
  const label = ((agent && agent.name) || aid).replace(/\n/g, " ");
  if (!window.confirm(`确定永久删除云端 Agent「${label}」？此操作不可恢复。`)) return;
  const bridge = api();
  if (!bridge || !bridge.delete_cloud_agent) {
    toast("当前预览不支持单独删除");
    return;
  }
  try {
    const out = await bridge.delete_cloud_agent(keyId, aid);
    if (!out || !out.ok) {
      toast("删除失败：" + ((out && out.error) || "未知错误"));
      await listCloudAgents(keyId);
      return;
    }
    const cur = agentLists[keyId];
    if (cur && Array.isArray(cur.agents)) {
      agentLists[keyId] = {
        loading: false,
        error: "",
        agents: cur.agents.filter((a) => a && a.id !== aid),
      };
      renderApiKeys();
    } else {
      await listCloudAgents(keyId);
    }
    toast("已永久删除 1 个云端 Agent");
  } catch (e) {
    toast("删除失败：" + String(e));
    await listCloudAgents(keyId);
  }
}

function onApiKeyListClick(e) {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const act = btn.getAttribute("data-act");
  const id = btn.getAttribute("data-id");
  if (act === "listAgents") listCloudAgents(id);
  else if (act === "cleanAgents") cleanCloudAgents(id);
  else if (act === "deleteAgent") deleteCloudAgent(id, btn.getAttribute("data-agent"));
  else if (act === "removeApiKey") removeApiKey(id);
}

function paintHelpJobs() {
  const box = $("helpJobs");
  if (!box) return;
  if (!helpJobs.length) {
    helpJobs = [
      { title: "看额度", body: "导入或探测本机账号后点「验证」，看 Bot / Auto / 高级 三池和到期日。验证只读，不会领取。" },
      { title: "切到本机", body: "点该号「切号」。会先关掉当前 Cursor，写入登录态后再打开。默认先换新登录票。" },
      { title: "守设备", body: "先在本机 Cursor 登录该号，再打开「本机保护」页勾选要留的设备。一键本机保护在该页里，避免误踢 IDE。" },
    ];
  }
  box.innerHTML = helpJobs
    .map((j) => `<div class="help-job"><b>${esc(j.title || "")}</b><span>${esc(j.body || "")}</span></div>`)
    .join("");
}

function setHelpDetailOpen(open) {
  const detail = $("helpDetail");
  const more = document.querySelector(".help-more-row");
  if (detail) detail.hidden = !open;
  if (more) more.hidden = !!open;
}

function showHelp(full) {
  $("helpMask").hidden = false;
  paintHelpJobs();
  setHelpDetailOpen(full === true);
}

function hideHelp() {
  $("helpMask").hidden = true;
  if ($("helpHide").checked) saveSettings({ hideHelp: true });
}

async function applyAppInfo() {
  try {
    const info = await api().app_info();
    if (info && info.version && $("appVersion")) $("appVersion").textContent = "v" + info.version;
    if (info && Array.isArray(info.helpJobs)) helpJobs = info.helpJobs;
    paintHelpJobs();
  } catch (e) {}
}

function applyNoticeHidden() {
  const el = $("freeNotice");
  if (el) el.hidden = !!settings.hideNotice;
}

function onSortHeaderClick(e) {
  const th = e.target.closest("th.sortable");
  if (!th) return;
  const key = th.getAttribute("data-sort");
  if (!key) return;
  if (listSort.key === key) listSort.dir *= -1;
  else {
    listSort.key = key;
    listSort.dir = 1;
  }
  render();
}

function setListFilter(filt) {
  listFilter = filt || "all";
  render();
}

function closeToolbarMore() {
  const el = $("toolbarMore");
  if (el) el.open = false;
}

function syncFilterChrome() {
  const sel = $("acctFilter");
  if (sel && sel.value !== listFilter) sel.value = listFilter;
  document.querySelectorAll(".hero-stats .stat").forEach((el) => {
    el.classList.toggle("is-on", el.getAttribute("data-filter") === listFilter);
  });
}

function showQuitConfirm() {
  const el = $("quitMask");
  if (el) el.hidden = false;
}

function hideQuitConfirm() {
  const el = $("quitMask");
  if (el) el.hidden = true;
}

function confirmQuit() {
  hideQuitConfirm();
  const bridge = api();
  if (bridge && bridge.request_quit) {
    // 不要 await，也不要 window.close()：桥回传也走 evaluate_js，
    // 等它会卡住；WKWebView 的 window.close() 关不掉桌面窗口。
    // Python 会置位后延迟 destroy。
    try {
      bridge.request_quit();
    } catch (e) {}
    return;
  }
  window.close();
}

window.showQuitConfirm = showQuitConfirm;

async function boot() {
  const bridge = api();
  if (bridge && bridge.mark_ui_ready) {
    try { await bridge.mark_ui_ready(); } catch (e) {}
  }
  $("btnHelp").addEventListener("click", () => showHelp(false));
  $("btnToggleImport").addEventListener("click", toggleImportPanel);
  const btnHideNotice = $("btnHideNotice");
  if (btnHideNotice) {
    btnHideNotice.addEventListener("click", () => {
      saveSettings({ hideNotice: true });
      applyNoticeHidden();
    });
  }
  const btnEmptyDetect = $("btnEmptyDetect");
  if (btnEmptyDetect) btnEmptyDetect.addEventListener("click", detectLocal);
  const btnEmptyPaste = $("btnEmptyPaste");
  if (btnEmptyPaste) {
    btnEmptyPaste.addEventListener("click", () => {
      setImportPanelOpen(true);
      const ta = $("tokenInput");
      if (ta) ta.focus();
    });
  }
  const btnHelpDetail = $("btnHelpDetail");
  if (btnHelpDetail) btnHelpDetail.addEventListener("click", () => setHelpDetailOpen(true));
  const acctSearch = $("acctSearch");
  if (acctSearch) {
    acctSearch.addEventListener("input", () => {
      listQuery = acctSearch.value || "";
      render();
    });
  }
  const acctFilter = $("acctFilter");
  if (acctFilter) {
    acctFilter.addEventListener("change", () => {
      setListFilter(acctFilter.value || "all");
    });
  }
  const heroStats = document.querySelector(".hero-stats");
  if (heroStats) {
    heroStats.addEventListener("click", (e) => {
      const stat = e.target.closest(".stat[data-filter]");
      if (!stat) return;
      const filt = stat.getAttribute("data-filter") || "all";
      setListFilter(listFilter === filt && filt !== "all" ? "all" : filt);
    });
  }
  const thead = document.querySelector("#paneAccounts thead");
  if (thead) thead.addEventListener("click", onSortHeaderClick);
  const moreMenu = document.querySelector(".toolbar-more-menu");
  if (moreMenu) moreMenu.addEventListener("click", closeToolbarMore);
  const tabBar = document.querySelector(".tab-bar");
  if (tabBar) tabBar.addEventListener("click", onMainTabClick);
  $("btnDetectLocal").addEventListener("click", detectLocal);
  $("btnImportFile").addEventListener("click", importFiles);
  $("btnAddText").addEventListener("click", addText);
  $("btnClear").addEventListener("click", clearAll);
  $("btnRemoveSel").addEventListener("click", removeSelected);
  $("btnClaimAll").addEventListener("click", () => runBatch("claim"));
  $("btnGuardSel").addEventListener("click", () => runGuardBatch("start"));
  $("btnGuardStopSel").addEventListener("click", () => runGuardBatch("stop"));
  $("btnVerify").addEventListener("click", () => runBatch("verify"));
  $("btnExport").addEventListener("click", exportAllClassified);
  $("btnAddApiKey").addEventListener("click", addApiKeys);
  $("apiKeyInput").addEventListener("input", scheduleSaveLastApiKeyInput);
  $("apiKeyInput").addEventListener("blur", () => {
    const ta = $("apiKeyInput");
    saveLastApiKeyInput((ta && ta.value) || "");
  });
  $("apiKeyList").addEventListener("click", onApiKeyListClick);
  $("rows").addEventListener("click", onTableClick);
  $("rows").addEventListener("change", onTableChange);
  $("chkAll").addEventListener("change", onSelectAll);
  $("helpOk").addEventListener("click", hideHelp);
  const helpClose = $("helpClose");
  if (helpClose) helpClose.addEventListener("click", hideHelp);
  $("quitCancel").addEventListener("click", hideQuitConfirm);
  $("quitOk").addEventListener("click", confirmQuit);
  $("quitMask").addEventListener("click", (e) => {
    if (e.target === $("quitMask")) hideQuitConfirm();
  });
  $("switchConfirmCancel").addEventListener("click", hideSwitchConfirm);
  $("switchConfirmOk").addEventListener("click", confirmSwitch);
  $("switchConfirmMask").addEventListener("click", (e) => {
    if (e.target === $("switchConfirmMask")) hideSwitchConfirm();
  });
  const switchRefreshChk = $("switchRefreshFirstChk");
  if (switchRefreshChk) {
    switchRefreshChk.addEventListener("change", () => {
      syncSwitchKickOldEnabled();
      if (isSwitchConfirmOpen()) paintSwitchConfirmBody();
    });
  }
  $("sessionOk").addEventListener("click", hideSessions);
  $("sessionRefresh").addEventListener("click", () => loadSessions());
  $("sessionBrowser").addEventListener("click", () => openSessionsPage(sessionModal.id));
  $("loginDetectCancel").addEventListener("click", hideLoginDetect);
  $("loginDetectOk").addEventListener("click", runLoginDetect);
  $("loginDetectMask").addEventListener("click", (e) => {
    if (e.target === $("loginDetectMask")) hideLoginDetect();
  });
  $("loginDetectSec").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      runLoginDetect();
    }
  });
  $("sessionBody").addEventListener("click", onSessionBodyClick);
  $("sessionBody").addEventListener("change", onSessionBodyChange);
  $("guardCancel").addEventListener("click", hideGuard);
  const guardAccountSelect = $("guardAccountSelect");
  if (guardAccountSelect) {
    guardAccountSelect.addEventListener("change", () => {
      const id = guardAccountSelect.value;
      if (!id || id === guardModal.id) return;
      openGuard(id);
    });
  }
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if ($("menuMask") && !$("menuMask").hidden) {
      hideRowMenu();
      return;
    }
    if ($("loginDetectMask") && !$("loginDetectMask").hidden) {
      hideLoginDetect();
      return;
    }
    if (isSwitchConfirmOpen()) {
      hideSwitchConfirm();
      return;
    }
    if (isGuardPanelOpen()) hideGuard();
  });
  $("guardRefresh").addEventListener("click", () => loadGuardSessions());
  const guardProbe = $("guardProbe");
  if (guardProbe) guardProbe.addEventListener("click", () => guardProbeTicket());
  const guardRefreshLogin = $("guardRefreshLogin");
  if (guardRefreshLogin) guardRefreshLogin.addEventListener("click", () => guardRefreshTicket(false));
  const guardRefreshKick = $("guardRefreshKick");
  if (guardRefreshKick) guardRefreshKick.addEventListener("click", () => guardRefreshTicket(true));
  $("guardDetect").addEventListener("click", openLoginDetect);
  $("guardBrowser").addEventListener("click", () => openSessionsPage(guardModal.id));
  $("guardBody").addEventListener("click", onGuardBodyClick);
  $("guardStart").addEventListener("click", startGuard);
  $("guardStop").addEventListener("click", () => guardModal.id && stopGuard(guardModal.id));
  const guardPin = $("guardPinLocal");
  if (guardPin) {
    guardPin.addEventListener("click", () => guardModal.id && pinLocalGuard(guardModal.id));
  }
  $("guardInterval").addEventListener("change", () => fillGuardIntervalSeconds(readGuardIntervalSeconds()));
  $("guardBody").addEventListener("change", onGuardBodyChange);
  $("guardGlobal").addEventListener("click", openGuardFromGlobal);
  $("guardGlobal").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openGuardFromGlobal();
    }
  });
  $("menuClose").addEventListener("click", hideRowMenu);
  $("menuBody").addEventListener("click", onMenuClick);
  $("menuMask").addEventListener("click", (e) => {
    if (e.target === $("menuMask")) hideRowMenu();
  });
  $("autoVerifyChk").addEventListener("change", (e) => saveSettings({ autoVerify: !!e.target.checked }));
  const btnShowTokens = $("btnShowTokens");
  if (btnShowTokens) {
    btnShowTokens.addEventListener("click", () => setShowTokens(!showTokensOn()));
  }
  const showTokensChk = $("showTokensChk");
  if (showTokensChk) {
    showTokensChk.addEventListener("change", (e) => setShowTokens(!!e.target.checked));
  }

  try {
    const [list, status] = await Promise.all([api().list_accounts(), api().load_status()]);
    accounts = list || [];
    if (status) {
      const ids = new Set(accounts.map((a) => a.id));
      for (const [id, st] of Object.entries(status)) {
        if (ids.has(id) && st && typeof st === "object") {
          rowState[id] = st;
          lastPersisted[id] = st;
        }
      }
    }
    await refreshLocalIdentity();
    render();
    await loadApiKeys();
  } catch (e) {
    await refreshLocalIdentity();
    render();
    await loadApiKeys();
  }

  // 本机设备保护跑在 Python 守护线程里；这里只是定时把状态拉过来画标签（本地调用，不联网）。
  await refreshGuardStatus(true);
  setInterval(() => refreshGuardStatus(false), 2000);

  try {
    settings = (await api().get_settings()) || {};
  } catch (e) {
    settings = {};
  }
  $("autoVerifyChk").checked = settings.autoVerify !== false;
  setImportPanelOpen(!!settings.importOpen, false);
  setMainTab("accounts", false);
  applyNoticeHidden();
  await applyAppInfo();
  syncBatchButtons();
  const btnShowTokensBoot = $("btnShowTokens");
  if (btnShowTokensBoot) btnShowTokensBoot.setAttribute("aria-pressed", settings.showTokens ? "true" : "false");
  if ($("showTokensChk")) $("showTokensChk").checked = !!settings.showTokens;
  syncShowTokensButton();
  if (showTokensOn()) {
    await loadTokenViews();
    render();
  }
  if (!settings.hideHelp) showHelp(false);
}

if (window.pywebview && window.pywebview.api) {
  boot();
} else {
  window.addEventListener("pywebviewready", boot);
}
