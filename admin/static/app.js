"use strict";
const $ = id => document.getElementById(id);
let csrf = "", overview = null, pendingConfirm = null, page = "dashboard", busy = false;
let accountMode = "browser", oauthFlow = null, oauthTimer = null, oauthGeneration = 0;
const labels = {
  dashboard: ["让每个账号，各尽其用", "WORKBUDDY WORKSPACE", "在这里查看服务运行、账号积分和请求表现。"],
  accounts: ["账号池", "ACCOUNT POOL", "集中管理登录凭据、积分与可用状态，让请求自动分配到可用账号。"],
  keys: ["API 密钥", "CLIENT ACCESS", "为每个客户端分配独立密钥，让连接清晰可控。"],
  models: ["模型管理", "MODEL CATALOG", "与官方客户端一致的模型列表，可按需启用或禁用。"],
  test: ["连接测试", "CONNECTION LAB", "从当前账号发起请求，确认模型能否正常响应。"],
  guide: ["接入指南", "GET CONNECTED", "从导入凭据到客户端接入，只需几步。"]
};
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const stamp = ts => ts ? new Date(ts).toLocaleString("zh-CN", {hour12:false}) : "未提供";
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $("toast").hidden = true, 4500); }
function showLogin() {
  csrf = ""; overview = null; $("workspace").hidden = true; $("login").hidden = false; $("boot").hidden = true;
  document.querySelectorAll("dialog[open]").forEach(d => d.close()); $("admin-key").value = "";
}
async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = {"Content-Type":"application/json", ...(method !== "GET" ? {"X-CSRF-Token":csrf} : {})};
  const response = await fetch("/admin/api/" + path, {method, headers, credentials:"same-origin", body:options.body === undefined ? undefined : JSON.stringify(options.body)});
  let data; try { data = await response.json(); } catch { throw new Error("服务器暂时不可用，请稍后重试"); }
  if (!response.ok) {
    if (response.status === 401 && path !== "login") showLogin();
    throw new Error(typeof data.detail === "string" ? data.detail : "操作失败，请检查输入后重试");
  }
  return data;
}
function goPage(next) {
  if (!(next in labels)) next = "dashboard";
  page = next;
  document.querySelectorAll(".page-panel").forEach(el => el.hidden = el.id !== "page-" + next);
  document.querySelectorAll("[data-page]").forEach(el => { el.classList.toggle("selected", el.dataset.page === next); el.setAttribute("aria-current", el.dataset.page === next ? "page" : "false"); });
  const [title, kicker, desc] = labels[next];
  $("page-title").replaceChildren(document.createTextNode(title));
  const dot = document.createElement("span"); dot.className = "title-dot"; dot.textContent = "."; $("page-title").append(dot);
  $("breadcrumb").textContent = next === "dashboard" ? "概览" : title; $("page-kicker").textContent = kicker; $("page-desc").textContent = desc;
  history.replaceState(null, "", "#" + next);
}
function render() {
  const {accounts, keys, models, uptime, events} = overview;
  const active = accounts.find(a => a.enabled && a.pool_state === "available") || accounts.find(a => a.enabled);
  renderDashboard();
  $("account-count").textContent = accounts.length; $("account-badge").textContent = accounts.length;
  const routing = overview.pool?.default_routing || "round_robin";
  const routingLabel = {round_robin:"账号池轮转", random:"随机分配", expiry_first:"优先用完快过期积分"}[routing] || routing;
  $("active-name").textContent = routingLabel;
  $("active-state").textContent = "新请求按各密钥的分配策略选择账号，自动跳过暂停与冷却账号";
  $("test-account").textContent = active?.name || "尚未选择";
  $("uptime").textContent = uptime < 60 ? "已启动不到 1 分钟" : `持续运行 ${Math.floor(uptime / 3600)} 小时 ${Math.floor(uptime % 3600 / 60)} 分钟`;
  $("accounts-empty").hidden = accounts.length > 0;
  renderAccounts();
  const count = status => accounts.filter(a => a.pool_state === status).length;
  $("pool-counts").textContent = `全部 ${accounts.length}  ·  可用 ${count("available")}  ·  冷却 ${count("cooling")}  ·  暂停 ${count("paused")}  ·  耗尽 ${count("exhausted")}`;
  const known = accounts.filter(a => a.remaining !== null && a.remaining !== undefined);
  $("total-credits").textContent = known.length ? Math.floor(known.reduce((s,a) => s + a.remaining, 0)).toLocaleString("zh-CN") + (known.length < accounts.length ? "（部分）" : "") : "待查询";
  if (!$("pool-settings").contains(document.activeElement)) {
    $("pool-routing").value = overview.pool?.default_routing || "round_robin"; $("auto-checkin").checked = overview.pool?.auto_checkin || false; $("checkin-time").value = overview.pool?.checkin_time || "09:00"; $("credit-interval").value = overview.pool?.credit_min_interval ?? 60; $("batch-spread").value = overview.pool?.batch_spread ?? 90; $("sync-start").value = overview.pool?.sync_start || "10:00"; $("sync-end").value = overview.pool?.sync_end || "20:00";
  }
  $("keys-body").innerHTML = keys.map(k => {
    const u = k.usage || {};
    const limited = k.daily_limit !== null && k.daily_limit !== undefined;
    const ratio = limited && k.daily_limit > 0 ? (u.count || 0) / k.daily_limit : 0;
    const warn = ratio >= 1 ? "red" : ratio >= 0.8 ? "amber" : "";
    // 状态判定与后端 key_unusable 一致：撤销 > 停用 > 过期 > 正常。
    const now = Date.now() / 1000;
    const state = k.revoked_at ? ["已撤销", "red"] : !k.enabled ? ["已停用", "amber"]
      : (k.expires_at && k.expires_at <= now) ? ["已过期", "amber"] : ["正常", "green"];
    const routing = k.routing || "";
    const revoked = !!k.revoked_at;
    const expired = !!k.expires_at && k.expires_at <= now;
    const scope = (list, noun) => (Array.isArray(list) && list.length)
      ? `<span class="pill amber" title="${esc(list.join(", "))}">${list.length} 个${noun}</span>` : `<span class="muted">全部</span>`;
    return `<tr><td><strong>${esc(k.name)}</strong>${k.expires_at ? `<small class="cell-note">${expired ? "已于" : "至"} ${esc(stamp(k.expires_at * 1000))} ${esc(expired ? "过期" : "到期")}</small>` : ""}</td><td><code>${esc(k.hint)}</code></td><td><span class="pill ${warn}">${u.count || 0}${limited ? ` / ${k.daily_limit}` : ""}</span>${warn ? `<small class="cell-note">${ratio >= 1 ? "已达上限，新请求返回 429" : "接近每日上限"}</small>` : `<small class="cell-note">${u.tokens ? u.tokens + " tokens · " : ""}${esc(u.date || "")}</small>`}</td><td>${scope(k.models, "模型")}</td><td>${scope(k.accounts, "账号")}</td><td>${revoked ? "—" : `<select class="routing-select" data-routing="${k.id}" title="该密钥的账号分配策略"><option value=""${routing === "" ? " selected" : ""}>跟随默认</option><option value="round_robin"${routing === "round_robin" ? " selected" : ""}>轮转</option><option value="random"${routing === "random" ? " selected" : ""}>随机</option><option value="expiry_first"${routing === "expiry_first" ? " selected" : ""}>优先快过期</option></select>`}</td><td><span class="pill ${state[1]}">${state[0]}</span></td><td class="align-right"><div class="actions">${revoked ? "" : `<button data-bind="${k.id}" title="设置可用模型与账号">权限</button><button data-limit="${k.id}" title="设置每日最大请求数">限额</button><button data-toggle="${k.id}">${k.enabled ? "停用" : "恢复"}</button>`}<button class="danger" data-revoke="${k.id}">撤销</button></div></td></tr>`;
  }).join("");
  renderModels();
  const modelIds = Array.isArray(models?.models) ? models.models.filter(m => m.enabled).map(m => m.id) : [];
  const selected = $("model").value || "deepseek-v4-flash";
  $("model").replaceChildren(...modelIds.map(model => { const o = document.createElement("option"); o.value = o.textContent = model; return o; }));
  if (modelIds.includes(selected)) $("model").value = selected;
  $("test-submit").disabled = !active || busy;
  $("test-history").innerHTML = events.length ? events.map(e => `<div class="history-row"><span class="mono muted">${esc(stamp(e.time * 1000))}</span><strong>${esc(e.model)}</strong><span class="pill ${e.ok ? "green" : "red"}">${e.ok ? "成功" : "失败"}</span><span class="mono">${e.seconds}s</span></div>`).join("") : '<p class="history-empty">暂无测试记录，发送第一条测试消息。</p>';
}
function renderModels() {
  const catalog = overview?.models;
  const list = Array.isArray(catalog?.models) ? catalog.models : [];
  $("models-badge").textContent = list.filter(m => m.enabled).length + " / " + list.length;
  const note = $("models-note");
  if (catalog?.error) { note.textContent = `上游拉取异常：${catalog.error}（当前展示${catalog.updated ? "上次缓存" : "内置官方列表"}）`; note.classList.add("field-error"); }
  else { note.textContent = catalog?.updated ? `上次拉取：${stamp(catalog.updated * 1000)} · 每 6 小时自动刷新。禁用的模型不会出现在 /v1/models，调用会被拒绝。` : "尚未从上游拉取过，展示内置官方列表。服务启动与每 6 小时会自动尝试拉取。"; note.classList.remove("field-error"); }
  $("models-body").innerHTML = list.map(m => {
    const credits = m.credits === null || m.credits === undefined ? "标准" : m.credits === 0 ? "免费" : "×" + m.credits;
    const promoNote = m.promo ? `<small class="cell-note">${esc(m.promo.label)} ×${m.promo.credits}</small>` : "";
    const ctx = m.max_input_tokens ? (m.max_input_tokens >= 1000 ? Math.round(m.max_input_tokens / 1000) + "K" : m.max_input_tokens) : "—";
    return `<tr><td><strong>${esc(m.id)}</strong><small class="cell-note">${esc(m.name || "")}</small></td><td>${esc(m.description || "—")}</td><td><span class="pill ${m.credits === 0 ? "green" : m.credits && m.credits >= 1 ? "amber" : ""}">${credits}</span>${promoNote}</td><td class="mono">${ctx}</td><td><span class="pill ${m.enabled ? "green" : ""}">${m.enabled ? "启用" : "禁用"}</span></td><td class="align-right"><button data-model-toggle="${esc(m.id)}" data-enabled="${m.enabled ? "0" : "1"}">${m.enabled ? "禁用" : "启用"}</button></td></tr>`;
  }).join("") || '<tr><td colspan="6" class="muted">模型列表为空，请点击「立即拉取」。</td></tr>';
}
function renderDashboard() {
  const {accounts, metrics:m = {}, pool} = overview;
  const fmt = value => Math.floor(Number(value)).toLocaleString("zh-CN");
  const rate = value => value === null || value === undefined ? "—" : value.toFixed(1) + "%";
  $("dash-available").textContent = accounts.filter(a=>a.pool_state === "available").length + " / " + accounts.length;
  const known = accounts.filter(a=>typeof a.remaining === "number");
  $("dash-credits").textContent = known.length ? fmt(known.reduce((s,a)=>s+a.remaining,0)) : "—";
  $("dash-credit-note").textContent = known.length < accounts.length ? `已查询 ${known.length}/${accounts.length} 个账号，余额可能不完整` : accounts.some(a=>a.credits_stale) ? "包含待刷新余额，以最近一次查询为准" : "以最近一次上游查询为准";
  $("dash-requests").textContent = fmt(m.completed || 0);
  $("dash-request-note").textContent = `API ${m.api_count || 0} · 后台测试 ${m.test_count || 0}（历史累计）`;
  $("dash-success").textContent = rate(m.success_rate); $("dash-http").textContent = rate(m.http_success_rate);
  $("dash-failed").textContent = fmt(m.failed || 0); $("dash-inflight").textContent = m.in_flight || 0;
  $("dash-latency").textContent = m.avg_duration_ms === null || m.avg_duration_ms === undefined ? "—" : fmt(m.avg_duration_ms) + " ms";
  $("dash-since").textContent = m.first_time ? "统计最早记录于 " + stamp(m.first_time * 1000) : "尚无历史请求记录";
  $("dash-routing").textContent = ({round_robin:"按权重轮流分配请求，自动跳过不可用账号", random:"在可用账号中随机分配", expiry_first:"优先消耗最早到期的积分"}[pool?.default_routing] || "按各密钥的分配策略选择账号");
  const states={available:"可用",paused:"已暂停",cooling:"冷却中",exhausted:"积分耗尽",invalid:"凭据异常"};
  $("dash-account-list").innerHTML = accounts.slice(0,8).map(a=>`<div class="dashboard-account"><span class="account-icon">${esc(a.name.slice(0,1))}</span><div><strong>${esc(a.name)}</strong>${a.nickname ? `<small class="cell-note">${esc(a.nickname)}</small>` : ""}</div><div class="dashboard-account-credit"><strong>${a.remaining === null || a.remaining === undefined ? "待查询" : fmt(a.remaining)}</strong><small class="cell-note">积分</small></div><span class="pill ${a.pool_state === 'available' ? 'green' : 'amber'}">${states[a.pool_state] || '待查询'}</span></div>`).join("") || '<p class="history-empty">尚未添加账号，点击“添加账号”开始。</p>';
  if(accounts.length > 8) $("dash-account-list").insertAdjacentHTML("beforeend",'<p class="muted">更多账号请前往账号池查看。</p>');
}
const outcomes={success:"完成",stream_error:"流式错误",interrupted:"未完整结束",http_error:"请求失败"};
let logOffset = 0, logTotal = 0;
function renderLogRows(rows) {
  $("dash-recent-body").innerHTML = rows.map(r=>`<tr><td class="mono muted">${esc(stamp(r.time*1000))}</td><td>${r.source === 'test' ? '后台测试' : 'API'}<small class="cell-note mono">${esc(r.path)}</small></td><td class="mono">${esc(r.model_id || "—")}${r.account_name ? `<small class="cell-note">${esc(r.account_name)}</small>` : ""}</td><td><span class="pill ${r.ok ? 'green' : 'red'}">${r.status ?? '—'}</span><small class="cell-note">${outcomes[r.outcome] || '请求失败'}</small></td><td class="align-right mono">${Math.round(Number(r.duration_ms)||0).toLocaleString("zh-CN")} ms</td></tr>`).join("") || '<tr><td colspan="5" class="history-empty">尚无请求记录。发起 API 调用或后台测试后，这里会自动更新。</td></tr>';
}
async function loadLogs(offset) {
  const size = Number($("dash-page-size").value) || 50;
  try {
    const data = await api(`logs?limit=${size}&offset=${offset}`);
    logOffset = data.offset; logTotal = data.total;
    renderLogRows(data.rows);
    const pages = Math.max(1, Math.ceil(logTotal / size));
    const current = Math.floor(data.offset / size) + 1;
    $("dash-page-info").textContent = logTotal ? `第 ${current} / ${pages} 页 · 共 ${logTotal.toLocaleString("zh-CN")} 条` : "暂无记录";
    $("dash-prev").disabled = data.offset <= 0;
    $("dash-next").disabled = data.offset + size >= logTotal;
  } catch (e) { $("dash-recent-body").innerHTML = `<tr><td colspan="5" class="history-empty">${esc(e.message)}</td></tr>`; }
}
$("dash-prev").addEventListener("click", () => loadLogs(Math.max(0, logOffset - Number($("dash-page-size").value))));
$("dash-next").addEventListener("click", () => loadLogs(logOffset + Number($("dash-page-size").value)));
$("dash-page-size").addEventListener("change", () => loadLogs(0));
document.querySelectorAll("[data-dashboard-page]").forEach(b=>b.addEventListener("click",()=>goPage(b.dataset.dashboardPage)));
$("dash-add").addEventListener("click",()=>openAccount());
$("dash-base").textContent=location.origin+"/v1";
$("dash-copy").addEventListener("click",()=>copy(location.origin+"/v1"));
function renderAccounts() {
  const query = $("account-search").value.toLowerCase(), filter = $("account-filter").value;  // 快过期的排前面：先按最早过期时间升序，再按状态严重度，最后按剩余积分升序。
  const severity = {invalid: 0, exhausted: 1, cooling: 2, paused: 3, available: 4};
  const rows = overview.accounts
    .filter(a => (!query || [a.name,a.nickname,a.uid].join(" ").toLowerCase().includes(query)) && (filter === "all" || a.pool_state === filter))
    .sort((x, y) => {
      const xe = x.earliest_expiry ?? Infinity, ye = y.earliest_expiry ?? Infinity;
      if (xe !== ye) return xe - ye;
      const xs = severity[x.pool_state] ?? 9, ys = severity[y.pool_state] ?? 9;
      if (xs !== ys) return xs - ys;
      return (x.remaining ?? Infinity) - (y.remaining ?? Infinity);
    });
  const labels = {available:["可用","green"],cooling:["冷却中","amber"],paused:["已暂停",""],exhausted:["积分耗尽","amber"],invalid:["凭据异常","red"]};
  $("accounts-body").innerHTML = rows.map(a => {
    const status = labels[a.pool_state] || ["待查询", ""];
    const credits = a.remaining === null || a.remaining === undefined ? "—" : Math.floor(Number(a.remaining)).toLocaleString("zh-CN");
    const expiring = a.expiring_soon === null || a.expiring_soon === undefined ? "—" : Math.floor(Number(a.expiring_soon)).toLocaleString("zh-CN");
    const expiringCell = `<td><strong class="credit-number ${a.expiring_soon > 0 ? "credit-warn" : ""}">${expiring}</strong><small class="cell-note">3 天内到期</small></td>`;
    return `<tr><td><div class="account-cell"><span class="account-icon">${esc(a.name.slice(0,1))}</span><div><strong>${esc(a.name)}</strong>${a.nickname ? `<small>${esc(a.nickname)}</small>` : ""}</div></div></td><td><span class="pill ${status[1]}">${status[0]}</span><small class="cell-note">${a.today_checked_in ? "今日已签到" : a.checkin_when ? `今日 ${esc(a.checkin_slot || "")} ${esc(a.checkin_when)} 签到` : "今日未确认签到"}</small>${a.cooldown_until > Date.now()/1000 ? `<small class="cell-note">至 ${esc(stamp(a.cooldown_until*1000))}</small>` : ""}</td><td><strong class="credit-number">${credits}</strong><small class="cell-note">${a.credits_updated ? esc(stamp(a.credits_updated*1000)) : "点击查询积分"}${a.credits_stale && a.credits_updated ? " · 待刷新" : ""}</small>${a.earliest_expiry ? `<small class="cell-note">最早一批 ${esc(stamp(a.earliest_expiry*1000))} 过期（余 ${Math.floor(a.earliest_expiry_remaining)}）</small>` : ""}${a.last_error ? `<small class="cell-note field-error">${esc(a.last_error)}</small>` : ""}</td>${expiringCell}<td class="mono">${esc(stamp(a.expires_at))}<small class="cell-note">${a.expired ? "已到期 · 调用时尝试刷新" : "支持自动刷新"}</small></td><td><button class="weight-btn" data-action="weight" data-id="${a.id}" title="权重越大越优先分配请求">权重 ${a.weight || 0}</button></td><td><div class="actions pool-actions">${a.enabled ? `<button data-action="status" data-id="${a.id}" title="查询积分与签到状态">查询积分</button><button data-action="checkin" data-id="${a.id}">签到</button><button data-action="refresh" data-id="${a.id}">刷新凭据</button>` : ""}<button data-action="rename" data-id="${a.id}">备注</button><button data-action="toggle" data-id="${a.id}">${a.enabled ? "暂停" : "恢复"}</button><button class="danger" data-action="delete" data-id="${a.id}">删除</button></div></td></tr>`;
  }).join("") || (overview.accounts.length ? '<tr><td colspan="6" class="muted">没有符合筛选条件的账号。</td></tr>' : "");
}
$("account-search").addEventListener("input", () => { if(overview) renderAccounts(); });
$("account-filter").addEventListener("change", () => { if(overview) renderAccounts(); });
async function refresh(loadCredits) {
  $("refresh").disabled = true;
  try {
    if (loadCredits) { try { await api("pool/refresh-credits", {method:"POST"}); } catch(e) {} }
    overview = await api("overview"); render(); $("load-error").hidden = true;
    if (page === "dashboard") loadLogs(logOffset);
  }
  catch (e) { $("load-error").textContent = e.message; $("load-error").hidden = false; throw e; }
  finally { $("refresh").disabled = false; }
}
async function enter() { $("boot").hidden = true; $("login").hidden = true; $("workspace").hidden = false; goPage(location.hash.slice(1)); await refresh(true); }
$("login-form").addEventListener("submit", async e => {
  e.preventDefault(); const button = e.submitter; button.disabled = true; $("login-error").textContent = "";
  try { const data = await api("login", {method:"POST",body:{key:$("admin-key").value.trim()}}); csrf = data.csrf; $("admin-key").value = ""; await enter(); }
  catch (error) { $("login-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", async () => { try { await api("logout", {method:"POST"}); showLogin(); } catch (e) { toast(e.message); } });
async function batchAction(action) {
  $("refresh").disabled = $("batch-checkin").disabled = true;
  const spread = Math.max(0, Number(overview.pool?.batch_spread ?? 90));
  const original = $("batch-checkin").textContent;
  if (action === "checkin" && spread) { $("batch-checkin").textContent = `分批签到中…（≤${spread}s）`; }
  try {
    const result = await api("pool/actions/"+action,{method:"POST"});
    $("pool-result").hidden = false;
    $("pool-result").textContent = result.results.map(r => `${overview.accounts.find(a=>a.id===r.id)?.name || "账号"}：${r.message}`).join("；") || "没有启用的账号";
    await refresh();
  } catch(e) { toast(e.message); } finally { $("refresh").disabled = $("batch-checkin").disabled = false; $("batch-checkin").textContent = original; }
}
$("refresh").addEventListener("click", async () => {
  if (page === "accounts") {
    $("refresh").disabled = true;
    try { await refresh(true); toast("已拉取最新积分"); }
    catch(e) { toast(e.message); }
    finally { $("refresh").disabled = false; }
  } else { refresh().catch(e=>toast(e.message)); }
});
$("batch-checkin").addEventListener("click", () => batchAction("checkin"));
$("pool-settings").addEventListener("submit", async e => { e.preventDefault();e.submitter.disabled=true;try {await api("pool/settings",{method:"PATCH",body:{default_routing:$("pool-routing").value,auto_checkin:$("auto-checkin").checked,checkin_time:$("checkin-time").value,credit_min_interval:Number($("credit-interval").value),sync_start:$("sync-start").value,sync_end:$("sync-end").value,batch_spread:Number($("batch-spread").value)}});await refresh();toast("账号池设置已保存");}catch(err){toast(err.message);}finally{e.submitter.disabled=false;} });
setInterval(() => { if(csrf && overview && !document.hidden && !document.querySelector("dialog[open]") && !$("refresh").disabled) refresh().catch(()=>{}); },120000);document.querySelectorAll("[data-page]").forEach(b => b.addEventListener("click", () => goPage(b.dataset.page)));
$("go-guide").addEventListener("click", () => goPage("guide"));
document.querySelectorAll(".close-dialog").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));
function clearOAuth() {
  oauthGeneration++; clearTimeout(oauthTimer);
  const previous = oauthFlow; oauthFlow = null;
  if (previous && csrf) api("oauth/" + previous.id, {method:"DELETE"}).catch(() => {});
  $("oauth-link-box").hidden = true; $("oauth-open").removeAttribute("href");
  $("oauth-start").disabled = false; $("oauth-start").textContent = "生成登录链接 ↗";
  $("oauth-retry").hidden = true; $("account-name").disabled = false;
  $("oauth-status").textContent = "登录链接 5 分钟内有效。同一账号重新登录会更新凭据。";
  $("oauth-status").className = "banner oauth-status";
}
function setAccountMode(mode) {
  clearOAuth(); accountMode = mode;
  $("browser-login-panel").hidden = mode !== "browser";
  $("file-import-panel").hidden = $("account-import").hidden = mode !== "file";
  $("mode-browser").setAttribute("aria-pressed", String(mode === "browser"));
  $("mode-file").setAttribute("aria-pressed", String(mode === "file"));
  $("account-error").textContent = "";
}
$("mode-browser").addEventListener("click", () => setAccountMode("browser"));
$("mode-file").addEventListener("click", () => setAccountMode("file"));
$("account-dialog").addEventListener("close", () => { clearOAuth(); $("account-form").reset(); $("file-label").textContent = "选择或拖入 .info / .json 文件"; $("account-error").textContent = ""; });
function openAccount() { setAccountMode("browser"); $("account-dialog").showModal(); }
async function pollOAuth(generation) {
  if (generation !== oauthGeneration || !oauthFlow) return;
  const flow = oauthFlow;
  if (Date.now() >= flow.expires_at) { clearOAuth(); $("oauth-status").textContent = "登录链接已过期，请重新生成。"; return; }
  try {
    const result = await api("oauth/" + flow.id + "/poll", {method:"POST"});
    if (generation !== oauthGeneration) return;
    if (result.status === "success") {
      $("account-dialog").close(); await refresh();
      toast(result.updated ? "授权成功，账号凭据已更新" : "授权成功，账号已添加；可在列表中切换使用"); return;
    }
    if (result.status === "expired") { clearOAuth(); $("oauth-status").textContent = "登录链接已过期，请重新生成。"; return; }
    $("oauth-status").className = "banner oauth-status";
    $("oauth-status").textContent = `等待你在官方页面完成登录… 链接剩余 ${Math.max(1, Math.ceil((flow.expires_at - Date.now()) / 1000))} 秒。`;
    oauthTimer = setTimeout(() => pollOAuth(generation), 3000);
  } catch (e) {
    if (generation !== oauthGeneration) return;
    $("oauth-status").className = "banner warning oauth-status"; $("oauth-status").textContent = e.message;
    $("oauth-retry").hidden = false;
  }
}
$("oauth-start").addEventListener("click", async () => {
  clearOAuth(); const generation = oauthGeneration;
  $("oauth-start").disabled = true; $("oauth-start").textContent = "正在生成…";
  $("account-error").textContent = "";
  try {
    const flow = await api("oauth/start", {method:"POST",body:{name:$("account-name").value.trim() || undefined}});
    if (generation !== oauthGeneration) { api("oauth/" + flow.id, {method:"DELETE"}).catch(() => {}); return; }
    oauthFlow = flow; $("oauth-open").href = flow.url; $("oauth-link-box").hidden = false;
    $("oauth-start").textContent = "重新生成链接"; $("account-name").disabled = true;
    $("oauth-status").textContent = "登录链接已就绪，请打开官方页面完成登录。";
    oauthTimer = setTimeout(() => pollOAuth(generation), 3000);
  } catch (e) { if (generation === oauthGeneration) $("account-error").textContent = e.message; }
  finally { if (generation === oauthGeneration) $("oauth-start").disabled = false; }
});
$("oauth-copy").addEventListener("click", () => { if (oauthFlow) copy(oauthFlow.url); });
$("oauth-retry").addEventListener("click", () => { $("oauth-retry").hidden = true; pollOAuth(oauthGeneration); });
$("add-account").addEventListener("click", openAccount); $("empty-add").addEventListener("click", openAccount);
async function readFile(file) {
  if (!file) return;
  if (file.size > 1024 * 1024) throw new Error("文件超过 1 MB，请选择登录凭据文件");
  $("credential-json").value = (await file.text()).replace(/^\uFEFF/, "");
  $("file-label").textContent = file.name;
}
$("credential-file").addEventListener("change", e => { readFile(e.target.files[0]).catch(err => $("account-error").textContent = err.message); });
$("file-drop").addEventListener("dragover", e => { e.preventDefault(); $("file-drop").classList.add("drag-over"); });
$("file-drop").addEventListener("dragleave", () => $("file-drop").classList.remove("drag-over"));
$("file-drop").addEventListener("drop", e => { e.preventDefault(); $("file-drop").classList.remove("drag-over"); readFile(e.dataTransfer.files[0]).catch(err => $("account-error").textContent = err.message); });
$("account-form").addEventListener("submit", async e => {
  if (accountMode !== "file") { e.preventDefault(); return; }
  e.preventDefault(); e.submitter.disabled = true; $("account-error").textContent = "";
  try {
    let credential; try { credential = JSON.parse($("credential-json").value); } catch { throw new Error("请选择文件或粘贴有效的 JSON 内容"); }
    await api("accounts", {method:"POST", body:{name:$("account-name").value.trim() || undefined, credential}});
    $("account-dialog").close(); await refresh(); toast("账号已导入");
  } catch (err) { $("account-error").textContent = err.message; } finally { e.submitter.disabled = false; }
});
function confirmAction(title, desc, callback, rename = null) {
  $("confirm-title").textContent = title; $("confirm-desc").textContent = desc; $("confirm-error").textContent = "";
  $("rename-label").hidden = $("rename-value").hidden = rename === null; $("rename-value").value = rename || "";
  pendingConfirm = callback; $("confirm-dialog").showModal();
}
$("confirm-form").addEventListener("submit", async e => { e.preventDefault(); e.submitter.disabled = true; try { await pendingConfirm(); $("confirm-dialog").close(); await refresh(); toast("操作已保存"); } catch (err) { $("confirm-error").textContent = err.message; } finally { e.submitter.disabled = false; } });
$("accounts-body").addEventListener("click", async e => {
  const b = e.target.closest("[data-action]"); if (!b) return;
  const a = overview.accounts.find(x => x.id === b.dataset.id); if (!a) return;
  if (["status","checkin","refresh"].includes(b.dataset.action)) {
    b.disabled = true;
    try { const result=await api(`accounts/${a.id}/actions/${b.dataset.action}`,{method:"POST"});toast(result.message);await refresh(); }
    catch(err){toast(err.message);} finally {b.disabled=false;} return;
  }
  const update = body => api("accounts/" + a.id, {method:"PATCH", body});
  if (b.dataset.action === "rename") confirmAction("编辑账号备注", "备注仅用于在管理后台识别账号。", () => update({name:$("rename-value").value}), a.name);
  if (b.dataset.action === "weight") confirmAction("设置调度权重", "权重 0-100，越大越优先分配请求；同权重时优先消耗积分最早过期的账号。", () => update({weight:Number($("rename-value").value)}), String(a.weight || 0));
  if (b.dataset.action === "toggle") confirmAction(a.enabled ? "暂停这个账号？" : "恢复这个账号？", "暂停后不参与新请求分配和自动签到，已开始的请求继续完成。恢复后重新参与轮转。", () => update({enabled:!a.enabled}));
  if (b.dataset.action === "delete") confirmAction("删除这个账号？", `「${a.name}」将从账号列表移除。服务器保留恢复副本。`, () => api("accounts/" + a.id, {method:"DELETE"}));
});
$("keys-body").addEventListener("change", e => {
  const select = e.target.closest("[data-routing]");
  if (!select) return;
  const kid = select.dataset.routing;
  // 留空 = NULL = 动态跟随 settings.default_routing。
  api("keys/" + kid, {method:"PATCH", body:{routing: select.value || null}})
    .then(() => refresh()).catch(err => { toast(err.message); refresh(); });
});
$("keys-body").addEventListener("click", e => {
  const bind = e.target.closest("[data-bind]");
  if (bind) { openKeyBind(overview.keys.find(x => x.id === bind.dataset.bind)); return; }
  const limit = e.target.closest("[data-limit]");
  if (limit) { const k = overview.keys.find(x => x.id === limit.dataset.limit); confirmAction("设置每日请求上限", "每天最多允许的请求数，0 或留空表示不限制。超限的请求会返回 429。", () => api("keys/" + k.id, {method:"PATCH", body:{daily_limit:Number($("rename-value").value) || null}}), k.daily_limit ?? ""); return; }
  const toggle = e.target.closest("[data-toggle]");
  if (toggle) { const k = overview.keys.find(x => x.id === toggle.dataset.toggle); const next = !k.enabled; confirmAction(next ? "恢复这枚密钥？" : "停用这枚密钥？", next ? `恢复后「${k.name}」可再次发起请求。` : `停用后「${k.name}」立即无法发起新请求，可随时恢复。`, () => api("keys/" + k.id, {method:"PATCH", body:{enabled: next}})); return; }
  const b = e.target.closest("[data-revoke]"); if (!b) return; const k = overview.keys.find(x => x.id === b.dataset.revoke); confirmAction("撤销客户端密钥？", `使用「${k.name}」的客户端将无法发起新请求。此操作不可撤销。`, () => api("keys/" + k.id, {method:"DELETE"}));
});
$("add-key").addEventListener("click", () => { $("key-form").hidden = false; $("created-key").hidden = true; $("key-dialog").showModal(); });
function openKeyBind(k) {
  if (!k) return;
  $("key-bind-name").textContent = `密钥「${k.name}」· ${k.hint}`;
  $("key-bind-error").textContent = "";
  const list = (host, items, selected, render) => {
    host.replaceChildren(...items.map(item => {
      const label = document.createElement("label");
      label.className = "check-item";
      const box = document.createElement("input");
      box.type = "checkbox"; box.value = item.value; box.checked = selected.includes(item.value);
      const span = document.createElement("span");
      span.textContent = render(item);
      label.append(box, span);
      return label;
    }));
    if (!items.length) { const p = document.createElement("p"); p.className = "muted"; p.textContent = "暂无可选项。"; host.replaceChildren(p); }
  };
  const models = (overview.models?.models || []).filter(m => m.enabled).map(m => ({value:m.id, name:m.name || ""}));
  list($("key-bind-models"), models, Array.isArray(k.models) ? k.models : [], m => m.name ? `${m.value}（${m.name}）` : m.value);
  const accounts = overview.accounts.map(a => ({value:a.id, name:a.name, state:a.pool_state}));
  list($("key-bind-accounts"), accounts, Array.isArray(k.accounts) ? k.accounts : [], a => `${a.name}（${a.value.slice(0,6)}…）`);
  $("key-bind-routing").value = k.routing || "";
  $("key-bind-dialog").dataset.kid = k.id;
  $("key-bind-dialog").showModal();
}
const checkedValues = host => [...host.querySelectorAll("input:checked")].map(b => b.value);
$("key-bind-form").addEventListener("submit", async e => {
  e.preventDefault(); e.submitter.disabled = true; $("key-bind-error").textContent = "";
  const kid = $("key-bind-dialog").dataset.kid;
  try {
    await api("keys/" + kid, {method:"PATCH", body:{
      models: checkedValues($("key-bind-models")),
      accounts: checkedValues($("key-bind-accounts")),
      routing: $("key-bind-routing").value || null}});
    $("key-bind-dialog").close(); await refresh(); toast("密钥权限已保存");
  } catch (err) { $("key-bind-error").textContent = err.message; }
  finally { e.submitter.disabled = false; }
});
$("key-dialog").addEventListener("close", () => { $("key-form").reset(); $("new-key-value").textContent = ""; $("key-error").textContent = ""; });
$("key-form").addEventListener("submit", async e => { e.preventDefault(); e.submitter.disabled = true; try { const d = await api("keys", {method:"POST",body:{name:$("key-name").value}}); $("key-form").hidden = true; $("created-key").hidden = false; $("new-key-value").textContent = d.key; await refresh(); } catch (err) { $("key-error").textContent = err.message; } finally { e.submitter.disabled = false; } });
async function copy(value) { try { await navigator.clipboard.writeText(value); toast("已复制"); } catch { toast("无法自动复制，请手动选择文本复制"); } }
$("copy-key").addEventListener("click", () => copy($("new-key-value").textContent));
document.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", () => copy(b.dataset.copy)));
$("base-url").textContent = location.origin + "/v1"; $("anthropic-url").textContent = location.origin;
$("copy-base").addEventListener("click", () => copy(location.origin + "/v1")); $("copy-anthropic").addEventListener("click", () => copy(location.origin));
$("host-label").textContent = location.host;
$("models-refresh").addEventListener("click", async () => {
  $("models-refresh").disabled = true; $("models-refresh").textContent = "拉取中…";
  try { await api("models/refresh", {method:"POST"}); await refresh(); toast("模型列表已更新"); }
  catch (e) { toast(e.message); }
  finally { $("models-refresh").disabled = false; $("models-refresh").innerHTML = '<span>↻</span> 立即拉取'; }
});
$("models-body").addEventListener("click", async e => {
  const button = e.target.closest("[data-model-toggle]");
  if (!button) return;
  button.disabled = true;
  try { await api("models/" + encodeURIComponent(button.dataset.modelToggle), {method:"PATCH", body:{enabled:button.dataset.enabled === "1"}}); await refresh(); }
  catch (err) { toast(err.message); button.disabled = false; }
});
$("test-form").addEventListener("submit", async e => {
  e.preventDefault(); busy = true; $("test-submit").disabled = true; $("test-submit").textContent = "正在调用…"; $("test-status").className = "pill amber"; $("test-status").textContent = "请求中"; $("test-output").textContent = "正在等待上游响应，最长约 90 秒…"; $("test-meta").textContent = "";
  try { const r = await api("test", {method:"POST",body:{model:$("model").value,prompt:$("test-prompt").value}}); $("test-status").className = "pill " + (r.ok ? "green" : "red"); $("test-status").textContent = r.ok ? "连接成功" : "调用失败"; $("test-output").textContent = r.ok ? r.answer : r.error; $("test-meta").textContent = `${r.seconds}s${r.status ? " · HTTP " + r.status : ""}${r.usage?.total_tokens !== undefined ? " · " + r.usage.total_tokens + " tokens" : ""}`; }
  catch (err) { $("test-status").className = "pill red"; $("test-status").textContent = "请求失败"; $("test-output").textContent = err.message; }
  finally { busy = false; $("test-submit").textContent = "发送测试 ↗"; await refresh().catch(() => {}); }
});
(async () => { try { const s = await api("session"); csrf = s.csrf; await enter(); } catch (e) { if (!csrf) showLogin(); } })();
