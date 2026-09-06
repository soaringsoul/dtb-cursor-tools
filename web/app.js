"use strict";

// 与 Python 侧 Api 通过 window.pywebview.api 通信；批量操作用并发池并行驱动，逐行更新状态。
//
// 三个动作各司其职：
//   导入      → 自动「验证账号」（邮箱 / 套餐 / 订阅剩余 / 三池用量），不领取
//   验证账号  → 判定 token 是否有效（过期 / 401 / 403 = 失效）+ 刷新信息，不领取
//   批量领取  → 领 Sand（Grok Bot）资格，领完只轻量刷 Bot 周用量这一池

let accounts = [];
const rowState = {}; // id -> 行状态：kind + 有效性 + Bot/Auto/高级 三池 + 订阅
const selected = new Set(); // 勾选的账号 id；为空表示「验证 / 领取」对全部生效
let busy = false;
let settings = {}; // settings.json：hideHelp / autoVerify
let lastPersisted = {}; // 上次落盘的稳定状态，避免瞬时失败把已保存的数据冲掉
let localUserId = null; // 本机 Cursor 当前登录的 user_ id；未登录为 null

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

function sessionPills(a, st) {
  const bits = [];
  if (localUserId && a.id === localUserId) {
    bits.push(`<span class="pill info mini" title="本机 Cursor 当前登录的是这个号">本机</span>`);
  }
  if (st && st.alive === false) return bits.join("");
  if (!st || !Object.prototype.hasOwnProperty.call(st, "sessionCount")) return bits.join("");
  if (st.sessionError) {
    bits.push(
      `<button type="button" class="pill idle mini" data-act="sessions" data-id="${esc(a.id)}" title="${esc(st.sessionError)}">会话 —</button>`
    );
    return bits.join("");
  }
  const n = st.sessionCount || 0;
  const c = st.sessionClientCount || 0;
  const w = st.sessionWebCount || 0;
  bits.push(
    `<button type="button" class="pill info mini" data-act="sessions" data-id="${esc(a.id)}" title="查看云端登录会话">` +
      `${n} 会话 · 客户端${c} / 网页${w}</button>`
  );
  return bits.join("");
}

function openSessions(id) {
  const a = accounts.find((x) => x.id === id);
  const st = rowState[id] || {};
  const mail = a && a.label && a.label.includes("@") ? a.label : (a && (a.label || a.id)) || id;
  $("sessionTitle").textContent = "登录会话 · " + mail;
  const rows = Array.isArray(st.sessions) ? st.sessions : [];
  let html = "";
  if (st.sessionError) html += `<p>${esc(st.sessionError)}</p>`;
  if (!rows.length && !st.sessionError) html += `<p>没有返回任何会话。</p>`;
  if (rows.length) {
    html += `<ul class="session-list">`;
    for (const s of rows) {
      const sid = s.sessionId || "";
      const short = sid.slice(0, 8);
      html +=
        `<li><div><b>${esc(sessionTypeLabel(s.type))}</b> · <span class="sid" title="${esc(sid)}">${esc(short)}</span></div>` +
        `<div class="hint">创建 ${esc(sessionTimeLabel(s.createdAt))}　过期 ${esc(sessionTimeLabel(s.expiresAt))}</div></li>`;
    }
    html += `</ul>`;
  }
  html += `<p class="hint">Cursor 接口不提供电脑名或 IP。本机标记来自本机登录库，无法对应到上面某一条会话。</p>`;
  $("sessionBody").innerHTML = html;
  $("sessionMask").hidden = false;
}

function hideSessions() {
  $("sessionMask").hidden = true;
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

// 列表排序：有剩余时间的按最快到期在前；没数据的其次；失效的最后。
function sortRank(a) {
  const st = rowState[a.id];
  if (st && st.alive === false) return [2, 0];
  const r = remainMs(a, st);
  return isNaN(r) ? [1, 0] : [0, r];
}

function orderedAccounts() {
  return accounts
    .map((a, i) => ({ a, i, r: sortRank(a) }))
    .sort((x, y) => x.r[0] - y.r[0] || x.r[1] - y.r[1] || x.i - y.i)
    .map((o) => o.a);
}

function render() {
  const tbody = $("rows");
  tbody.innerHTML = orderedAccounts()
    .map((a) => {
      const st = rowState[a.id];
      const mail = a.label && a.label.includes("@") ? a.label : a.label || a.id;
      const checked = selected.has(a.id) ? " checked" : "";
      const webTok = String(a.tokenType || "").toLowerCase() === "web";
      const tokTag = webTok
        ? `<span class="pill warn mini" title="网站会话：切号时会自动换成客户端登录票，稍慢几秒">网站会话</span>`
        : "";
      const addedAt = a.addedAt ? fmtTs(toMs(a.addedAt)) : "";
      const checkedAt = st && st.checkedAt ? fmtTs(toMs(st.checkedAt)) : "";
      const meta =
        `<div class="meta">${validityPill(st)}${tokTag}${sessionPills(a, st)}` +
        `<span title="导入时间">导入 ${esc(addedAt || "—")}</span>` +
        (checkedAt ? `<span title="上次验证时间">验证 ${esc(checkedAt)}</span>` : "") +
        `</div>`;
      const dead = st && st.alive === false;
      const dis = busy ? " disabled" : "";
      return `<tr data-id="${esc(a.id)}"${dead ? ' class="dead"' : ""}>
        <td class="col-chk"><input type="checkbox" class="rowchk" data-id="${esc(a.id)}"${checked}${dis} /></td>
        <td><div class="mail">${esc(mail)}</div><div class="uid">${esc(a.id)}</div>${meta}</td>
        <td>${planCell(st)}</td>
        <td>${expiryCell(a, st)}</td>
        <td>${statusCell(st)}</td>
        <td>${quotaCell(st)}</td>
        <td class="col-act"><div class="act-wrap">
          <button class="btn tiny primary" data-act="claim" data-id="${esc(a.id)}"${dis} title="领取 Sand 资格">领取</button>
          <button class="btn tiny" data-act="verify" data-id="${esc(a.id)}"${dis} title="验证有效性并刷新用量 / 订阅">验证</button>
          <button class="btn tiny" data-act="switch" data-id="${esc(a.id)}"${dis} title="${webTok ? "网站会话：切号时自动换客户端登录票（稍慢几秒）" : "切到本机 Cursor"}">切号</button>
          <button class="btn tiny" data-act="browser" data-id="${esc(a.id)}"${dis}>网页领取</button>
          <button class="btn tiny" data-act="copy" data-id="${esc(a.id)}"${dis} title="复制：邮箱----user_id::token">复制</button>
          <button class="btn tiny danger" data-act="remove" data-id="${esc(a.id)}"${dis}>移除</button>
        </div></td>
      </tr>`;
    })
    .join("");
  $("emptyHint").hidden = accounts.length > 0;
  $("countPill").textContent = accounts.length + " 个";
  syncSelectAll();
  updateStats();
}

function syncSelectAll() {
  const all = $("chkAll");
  if (!all) return;
  const n = accounts.length;
  const sel = accounts.filter((a) => selected.has(a.id)).length;
  all.checked = n > 0 && sel === n;
  all.indeterminate = sel > 0 && sel < n;
}

function updateStats() {
  let done = 0;
  let card = 0;
  let alive = 0;
  let dead = 0;
  for (const a of accounts) {
    const st = rowState[a.id];
    if (!st) continue;
    if (st.kind === "ok") done += 1;
    else if (st.kind === "card") card += 1;
    if (st.alive === true) alive += 1;
    else if (st.alive === false) dead += 1;
  }
  $("statTotal").textContent = accounts.length;
  $("statAlive").textContent = alive;
  $("statDead").textContent = dead;
  $("statDone").textContent = done;
  $("statCard").textContent = card;
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
    render();
    toast("已探测本机账号：" + (res.email || res.id || "本机"));
    if (res.id && autoVerifyEnabled()) runBatch("verify", [res.id]);
  } catch (e) {
    toast("探测失败：" + String(e));
  }
}

async function switchAccount(id) {
  const resetMid = !!($("resetMidChk") && $("resetMidChk").checked);
  const a = accounts.find((x) => x.id === id);
  const webTok = a && String(a.tokenType || "").toLowerCase() === "web";
  toast(
    (webTok ? "网站会话切号：正在换客户端登录票（约几秒）… " : "") +
      (resetMid ? "正在切号并重置机器码，Cursor 将自动重启…" : "正在切号，Cursor 将自动重启…")
  );
  try {
    const res = await api().switch_account(id, resetMid);
    if (res && res.ok) {
      toast(
        `已切换到 ${res.email || id}${res.resetMachineId ? "（已重置机器码）" : ""}` +
          `${res.exchanged ? "（网站会话已换客户端票）" : ""}，Cursor 正在重启` +
          (res.warning ? "　⚠ " + res.warning : "")
      );
      await refreshLocalIdentity();
      render();
    } else {
      toast("切号失败：" + ((res && res.error) || "未知原因"));
    }
  } catch (e) {
    toast("切号失败：" + String(e));
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
  // 有勾选就只处理勾选的，否则处理全部。
  return selected.size ? selectedIds() : accounts.map((a) => a.id);
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
    toast("没有可处理的账号");
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
  const scope = idsOverride ? "（刚导入）" : selected.size ? "（仅选中）" : "";
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
    toast(`${verb} ${ids.length} 个账号（未自动验证，可点「验证账号」）`);
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

// 单条记录：复制该账号 + token（邮箱----user_id::token，和导入格式一致，可直接粘回「添加到列表」）。
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
    `Sand 资格领取器 导出 ${fmtTs(Date.now())} · 共 ${seen.size} 个账号 · 先按套餐分大类（Ultra > Pro+ > Pro > 团队 > Free），大类里再分「未用 Bot / 已用 Bot / 不续费」，段内按剩余时间从短到长（最先到期在最前）`,
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
  const id = btn.getAttribute("data-id");
  const act = btn.getAttribute("data-act");
  if (act === "sessions") {
    openSessions(id);
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
        selected.delete(id);
        schedulePersist();
        render();
      });
  } else if (act === "browser") {
    openLogin(id);
  } else if (act === "switch") {
    switchAccount(id);
  } else if (act === "copy") {
    copyAccount(id);
  } else if (act === "claim") {
    claimOne(id);
  } else if (act === "verify") {
    verifyOne(id);
  }
}

function onTableChange(e) {
  const chk = e.target.closest("input.rowchk");
  if (!chk) return;
  const id = chk.getAttribute("data-id");
  if (chk.checked) selected.add(id);
  else selected.delete(id);
  syncSelectAll();
}

function onSelectAll(e) {
  if (e.target.checked) accounts.forEach((a) => selected.add(a.id));
  else selected.clear();
  render();
}

// ---- 补丁面板：逐条规则 + 逐步报告 + 日志验证 ----

const RULE_PILL = { applied: "ok", partial: "warn", pending: "idle", missing: "bad" };

function ruleRow(r) {
  const cls = r.optional && r.status === "missing" ? "idle" : RULE_PILL[r.status] || "idle";
  const label = r.optional && r.status === "missing" ? "可选·无锚点" : r.statusLabel || r.status;
  const files = r.files && r.files.length ? `<div class="rule-files mono">${esc(r.files.join("  "))}</div>` : "";
  const fix = r.fix && !(r.optional && r.status === "missing") ? `<div class="rule-fix">修法：${esc(r.fix)}</div>` : "";
  return (
    `<div class="rule ${r.stream ? "stream" : ""}">` +
    `<span class="pill mini ${cls}">${esc(label)}</span>` +
    `<div class="rule-body"><div class="rule-title">${esc(r.title)}${r.optional ? '<span class="hint"> · 可选</span>' : ""}</div>` +
    `<div class="hint">${esc(r.why)}</div>${files}${fix}</div></div>`
  );
}

function renderRules(res) {
  const box = $("patchRules");
  const rules = (res && res.rules) || [];
  if (!rules.length) {
    box.hidden = true;
    return;
  }
  const s = res.summary || {};
  const verdictPill =
    s.verdict === "full"
      ? `<span class="pill ok mini">${s.applied}/${s.required} 条必需规则全部生效</span>`
      : s.verdict === "partial"
        ? `<span class="pill warn mini">只有 ${s.applied}/${s.required} 条生效——补丁没打全</span>`
        : `<span class="pill idle mini">0/${s.required} 条生效——未打补丁</span>`;
  let run = "";
  if (res.runningElsewhere && res.runningElsewhere.length && !res.runningFromPatched) {
    run = `<div class="rule-fix">⚠ 正在运行的 Cursor 不是这一份（${esc(res.runningElsewhere.join("；"))}）——补丁打在 ${esc(res.path || "")}，你打开的却是另一个安装，当然「没生效」。用「设置路径」指到你实际用的那份再打。</div>`;
  } else if (res.otherInstalls && res.otherInstalls.length) {
    run = `<div class="hint">本机还有其他 Cursor 安装：${esc(res.otherInstalls.join("；"))}（补丁只写进当前这份）</div>`;
  }
  box.innerHTML =
    `<div class="rules-head">${verdictPill}<span class="hint">每条规则单独判定：已生效 = 标记已写入；未打 = 找到锚点但还没改；锚点缺失 = 这个 Cursor 构建里找不到该代码（版本不符）</span></div>` +
    run +
    rules.map(ruleRow).join("");
  box.hidden = false;
}

const STEP_GLYPH = { ok: "✓", warn: "⚠", fail: "✗", skip: "–" };

function renderInstallReport(rep) {
  const box = $("patchReport");
  if (!rep) {
    box.hidden = true;
    return;
  }
  const cls = rep.verdict === "full" ? "ok" : rep.verdict === "partial" ? "warn" : "bad";
  const steps = (rep.steps || [])
    .map(
      (s) =>
        `<li class="step ${s.status}"><span class="glyph">${STEP_GLYPH[s.status] || "·"}</span>` +
        `<div><b>${esc(s.title)}</b>${s.detail ? `<div class="hint">${esc(s.detail)}</div>` : ""}` +
        `${s.fix ? `<div class="rule-fix">修法：${esc(s.fix)}</div>` : ""}</div></li>`
    )
    .join("");
  box.innerHTML =
    `<div class="report-head"><span class="pill ${cls}">${rep.verdict === "full" ? "补丁成功" : rep.verdict === "partial" ? "部分成功 / 需确认" : "补丁失败"}</span> ${esc(rep.headline || "")}</div>` +
    `<ol class="steps">${steps}</ol>` +
    (rep.backupDir ? `<div class="hint mono">备份：${esc(rep.backupDir)}</div>` : "") +
    `<div class="hint">把这一段截图发群里，群主就能看出到底卡在哪一步。</div>`;
  box.hidden = false;
}

function renderRuntimeReport(rep) {
  const box = $("patchReport");
  if (!rep) {
    box.hidden = true;
    return;
  }
  const cls = rep.verdict === "working" || rep.verdict === "ready" ? "ok" : rep.verdict === "partial" ? "warn" : rep.verdict === "broken" ? "bad" : "idle";
  const label = { working: "已生效", ready: "已就绪", partial: "部分回落云端", broken: "未生效", unknown: "无法判断", "no-log": "没有日志" }[rep.verdict] || rep.verdict;
  const checks = (rep.checks || [])
    .map((c) => `<li class="step ${c.ok ? "ok" : "fail"}"><span class="glyph">${c.ok ? "✓" : "✗"}</span><div><b>${esc(c.title)}</b>${c.detail ? `<div class="hint">${esc(c.detail)}</div>` : ""}</div></li>`)
    .join("");
  const turns = (rep.turns || [])
    .map(
      (t) =>
        `<tr><td class="mono">${esc((t.time || "").slice(5, 19))}</td><td>${esc(t.action || "")}</td>` +
        `<td><span class="pill mini ${t.runtime === "managed-local" ? "ok" : "bad"}">${esc(t.runtime || "?")}</span></td>` +
        `<td class="mono">${esc(t.reason || "")}</td><td class="hint">${esc(t.hint || "")}</td></tr>`
    )
    .join("");
  const errors = (rep.errors || []).map((e) => `<div class="mono err">${esc(e)}</div>`).join("");
  box.innerHTML =
    `<div class="report-head"><span class="pill ${cls}">${esc(label)}</span> ${esc(rep.headline || "")}</div>` +
    (checks ? `<ol class="steps">${checks}</ol>` : "") +
    (turns
      ? `<div class="hint">最近几轮对话实际走的路（managed-local = 本地 Bot 回路；connect = 回落云端，Bot 额度没用上）：</div>` +
        `<table class="turns"><thead><tr><th>时间</th><th>动作</th><th>路径</th><th>原因</th><th>说明</th></tr></thead><tbody>${turns}</tbody></table>`
      : "") +
    (errors ? `<div class="hint">最近错误：</div>${errors}` : "") +
    (rep.log ? `<div class="hint mono">日志：${esc(rep.log)}</div>` : "") +
    (rep.detail ? `<div class="hint">${esc(rep.detail)}</div>` : "");
  box.hidden = false;
}

async function refreshPatch() {
  const pill = $("patchPill");
  const info = $("patchInfo");
  pill.className = "pill idle";
  pill.textContent = "检测中…";
  const res = await api().patch_status();
  if (!res || !res.ok) {
    pill.className = "pill bad";
    pill.textContent = "未检测到 Cursor";
    info.textContent = (res && res.error) || "未找到本机 Cursor 安装。";
    $("patchRules").hidden = true;
    return;
  }
  const versionMismatch = !res.streamMode && !res.streamCapable;
  const s = res.summary || {};
  if (res.streamMode && s.verdict !== "partial") {
    pill.className = "pill ok";
    pill.textContent = "Stream 模式";
  } else if (versionMismatch) {
    pill.className = "pill bad";
    pill.textContent = "版本不符";
  } else if (res.installed) {
    pill.className = "pill warn";
    pill.textContent = s.verdict === "partial" ? "补丁不完整" : "补丁需升级";
  } else {
    pill.className = "pill idle";
    pill.textContent = "未打补丁";
  }
  if (versionMismatch) {
    // 版本不对：打补丁不会生效。醒目提示 + 直接给对应平台的下载按钮。
    const req = res.requiredVersion || "3.18.9 / 3.18.25 / 3.19.13";
    const dlVer = res.downloadVersion || "3.19.13";
    const dl = res.downloadUrl || "";
    const dlSys = res.downloadUrlSystem || "";
    info.innerHTML =
      `⚠ 需 Cursor ${req} 才能打补丁：当前 ${res.version || "?"} 不含 agent-host 锚点，打了也不生效。先装对应版本并关自动更新。 ` +
      (dl ? `<button class="btn tiny primary" id="btnDlCursor">下载 Cursor ${dlVer}</button> ` : "") +
      (dlSys ? `<button class="btn tiny" id="btnDlCursorSys">管理员版</button>` : "");
    const b1 = document.getElementById("btnDlCursor");
    if (b1) b1.onclick = () => downloadCursor(dl);
    const b2 = document.getElementById("btnDlCursorSys");
    if (b2) b2.onclick = () => downloadCursor(dlSys);
  } else {
    let streamHint = "";
    if (res.streamMode && s.verdict !== "partial") streamHint = "Stream 回路已启用（后台任务完成 / 子代理也走本地 Bot 回路）。点「验证生效」可从 Cursor 日志确认实际走的路";
    else if (res.installed && s.verdict === "partial") streamHint = "⚠ 补丁只生效了一部分，下面逐条看哪条没打上";
    else if (res.streamCapable && res.installed) {
      // 旧版补丁：后台任务完成、子代理仍会回落云端被 401，表现为反复弹 "unexpected error"。
      streamHint = "⚠ 已打的是旧版补丁，请重新点「打补丁」升级（修复：每次后台任务结束弹 unexpected error、子代理用不了 Bot）。会自动重启 Cursor";
    } else if (res.streamCapable) streamHint = "可打 Stream 补丁，尚未启用";
    info.textContent = `Cursor ${res.version || "?"} · ${res.path || ""} · ${streamHint}`;
  }
  renderRules(res);
}

async function doVerifyRuntime() {
  toast("正在读取 Cursor 的 agent-host 日志…");
  try {
    const rep = await api().verify_runtime();
    renderRuntimeReport(rep);
    toast(rep && rep.headline ? rep.headline.slice(0, 80) : "已读取日志");
  } catch (e) {
    toast("验证失败：" + String(e));
  }
}

async function downloadCursor(url) {
  if (!url) return;
  toast("正在打开下载页…");
  try {
    const r = await api().open_url(url);
    if (!r || !r.ok) toast("打开失败，请手动复制链接：" + url);
  } catch (e) {
    toast("打开失败，请手动复制链接：" + url);
  }
}

async function doPatch() {
  // 版本不对就先拦一下：已测试版本之外没有 agent-host 锚点，打了也白打。
  try {
    const st = await api().patch_status();
    if (st && st.ok && st.streamCapable === false) {
      const req = st.requiredVersion || "3.18.9 / 3.18.25 / 3.19.13";
      const dlVer = st.downloadVersion || "3.19.13";
      const dl = st.downloadUrl || "";
      const msg =
        `当前 Cursor ${st.version || ""} 没有 ${req} 的 agent-host 锚点：\n` +
        `打补丁不会生效，Sand 工具依然用不了。\n\n` +
        `请先安装 Cursor ${dlVer}（并关闭自动更新）：\n${dl}\n\n` +
        `（取消后可点补丁面板的「下载 Cursor ${dlVer}」按钮直接下载）\n\n仍要继续尝试吗？`;
      if (window.confirm(msg) === false) {
        toast("已取消：请先安装 Cursor " + dlVer + " 再打补丁");
        return;
      }
    }
  } catch (e) {}
  $("btnPatch").disabled = true;
  $("btnRestore").disabled = true;
  toast("正在打补丁：关闭 Cursor → 写入 → 校验 → 重启，请稍候…");
  const res = await api().apply_patch();
  $("btnPatch").disabled = false;
  $("btnRestore").disabled = false;
  renderInstallReport(res);
  if (res && res.verdict === "full") toast("补丁全部生效，Cursor 已重启");
  else if (res && res.verdict === "partial") toast("补丁部分生效 / 有待确认项，看下方报告");
  else toast("打补丁失败：" + ((res && res.headline) || (res && res.error) || "未知原因"));
  refreshPatch();
}

async function doRestore() {
  $("btnPatch").disabled = true;
  $("btnRestore").disabled = true;
  toast("正在回退，随后会自动重启 Cursor…");
  const res = await api().restore_patch();
  $("btnPatch").disabled = false;
  $("btnRestore").disabled = false;
  $("patchReport").hidden = true;
  toast(res && res.ok ? "已回退，Cursor 将自动重启" : "回退失败：" + ((res && res.error) || ""));
  refreshPatch();
}

// 设置统一合并后整体写回，避免某一项覆盖掉另一项。
async function saveSettings(patch) {
  settings = { ...settings, ...patch };
  const bridge = api();
  if (bridge && bridge.set_settings) {
    try {
      await bridge.set_settings(settings);
    } catch (e) {}
  }
}

function showHelp() {
  $("helpMask").hidden = false;
}

function hideHelp() {
  $("helpMask").hidden = true;
  if ($("helpHide").checked) saveSettings({ hideHelp: true });
}

async function doSetPath() {
  const path = $("cursorPathInput").value.trim();
  toast("正在设置 Cursor 路径…");
  try {
    const res = await api().set_cursor_path(path);
    toast(res && res.ok ? (path ? "已设置路径" : "已恢复自动检测") : "设置失败：" + ((res && res.error) || "路径无效"));
  } catch (e) {
    toast("设置失败：" + String(e));
  }
  refreshPatch();
}

async function boot() {
  $("btnDetectLocal").addEventListener("click", detectLocal);
  $("btnImportFile").addEventListener("click", importFiles);
  $("btnAddText").addEventListener("click", addText);
  $("btnClear").addEventListener("click", clearAll);
  $("btnRemoveSel").addEventListener("click", removeSelected);
  $("btnClaimAll").addEventListener("click", () => runBatch("claim"));
  $("btnVerify").addEventListener("click", () => runBatch("verify"));
  $("btnExport").addEventListener("click", exportAllClassified);
  $("btnPatch").addEventListener("click", doPatch);
  $("btnRestore").addEventListener("click", doRestore);
  $("btnPatchCheck").addEventListener("click", () => {
    $("patchReport").hidden = true;
    refreshPatch();
  });
  $("btnVerifyRuntime").addEventListener("click", doVerifyRuntime);
  $("btnSetPath").addEventListener("click", doSetPath);
  $("rows").addEventListener("click", onTableClick);
  $("rows").addEventListener("change", onTableChange);
  $("chkAll").addEventListener("change", onSelectAll);
  $("helpOk").addEventListener("click", hideHelp);
  $("sessionOk").addEventListener("click", hideSessions);
  $("autoVerifyChk").addEventListener("change", (e) => saveSettings({ autoVerify: !!e.target.checked }));
  refreshPatch();

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
  } catch (e) {
    await refreshLocalIdentity();
    render();
  }

  try {
    settings = (await api().get_settings()) || {};
  } catch (e) {
    settings = {};
  }
  $("autoVerifyChk").checked = settings.autoVerify !== false;
  if (!settings.hideHelp) showHelp();
}

if (window.pywebview && window.pywebview.api) {
  boot();
} else {
  window.addEventListener("pywebviewready", boot);
}
