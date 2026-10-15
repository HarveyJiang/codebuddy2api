"use strict";
const $ = id => document.getElementById(id);
// 积分余额可能带小数（Trae 按消耗比例扣减），统一保留最多 2 位小数展示。
const creditsFmt = v => {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return (Math.round(n * 100) / 100).toLocaleString("zh-CN", {maximumFractionDigits: 2});
};
let csrf = "", overview = null, pendingConfirm = null, page = "dashboard", busy = false;
let accountMode = "browser", oauthFlow = null, oauthTimer = null, oauthGeneration = 0, traeFlow = null, traeTimer = null;
const labels = {
  dashboard: ["让每个账号，各尽其用", "WORKBUDDY WORKSPACE", "在这里查看服务运行、账号积分和请求表现。"],
  accounts: ["账号池", "ACCOUNT POOL", "集中管理登录凭据、积分与可用状态，让请求自动分配到可用账号。"],
  keys: ["API 密钥", "CLIENT ACCESS", "为每个客户端分配独立密钥，让连接清晰可控。"],
  models: ["模型管理", "MODEL CATALOG", "与官方客户端一致的模型列表，可按需启用或禁用。"],
  test: ["连接测试", "CONNECTION LAB", "经账号池按路由规则发起请求，确认模型能否正常响应。"],
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
  renderDashboard();
  $("account-count").textContent = accounts.length; $("account-badge").textContent = accounts.length;
  $("active-name").textContent = "账号池轮转";
  $("active-state").textContent = "新请求轮流分配，跳过暂停与冷却账号";
  $("test-account").textContent = "账号池轮转 · 自动选择可用账号";
  $("uptime").textContent = uptime < 60 ? "已启动不到 1 分钟" : `持续运行 ${Math.floor(uptime / 3600)} 小时 ${Math.floor(uptime % 3600 / 60)} 分钟`;
  $("accounts-empty").hidden = accounts.length > 0;
  renderAccounts();
  const count = status => accounts.filter(a => a.pool_state === status).length;
  $("pool-counts").textContent = `全部 ${accounts.length}  ·  可用 ${count("available")}  ·  冷却 ${count("cooling")}  ·  暂停 ${count("paused")}  ·  耗尽 ${count("exhausted")}`;
  const known = accounts.filter(a => a.remaining !== null && a.remaining !== undefined);
  $("total-credits").textContent = known.length ? creditsFmt(known.reduce((s,a) => s + a.remaining, 0)) + (known.length < accounts.length ? "（部分）" : "") : "待查询";
  if (!$("pool-settings").contains(document.activeElement)) {
    $("pool-routing").value = overview.pool?.routing || "round_robin"; $("auto-checkin").checked = overview.pool?.auto_checkin || false; $("checkin-time").value = overview.pool?.checkin_time || "09:00"; $("credit-interval").value = overview.pool?.credit_min_interval ?? 60; $("batch-spread").value = overview.pool?.batch_spread ?? 90; const w = overview.pool?.sync_window || ["10:00","20:00"]; $("sync-start").value = w[0]; $("sync-end").value = w[1];
  }
  $("keys-body").innerHTML = keys.map(k => { const u = k.usage || {}; const limited = k.daily_limit !== null && k.daily_limit !== undefined; const ratio = limited && k.daily_limit > 0 ? (u.count || 0) / k.daily_limit : 0; const warn = ratio >= 1 ? "red" : ratio >= 0.8 ? "amber" : ""; return `<tr><td><strong>${esc(k.name)}</strong></td><td><code>${esc(k.hint)}</code></td><td><span class="pill ${warn}">${u.count || 0}${limited ? ` / ${k.daily_limit}` : ""}</span>${warn ? `<small class="cell-note">${ratio >= 1 ? "已达上限，新请求返回 429" : "接近每日上限"}</small>` : `<small class="cell-note">${u.tokens ? u.tokens + " tokens · " : ""}${esc(u.date || "")}</small>`}</td><td class="align-right"><div class="actions"><button data-limit="${k.id}" title="设置每日最大请求数">限额</button><button class="danger" data-revoke="${k.id}">撤销</button></div></td></tr>`; }).join("");
  renderModels();
  const modelIds = Array.isArray(models?.models) ? models.models.filter(m => m.enabled).map(m => m.id) : [];
  const selected = $("model").value || "deepseek-v4-flash";
  $("model").replaceChildren(...modelIds.map(model => { const o = document.createElement("option"); o.value = o.textContent = model; return o; }));
  if (modelIds.includes(selected)) $("model").value = selected;
  $("test-submit").disabled = !accounts.some(a => a.enabled) || busy;
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
    // max 模式（上游 max_mode=true）下的上下文上限；无 max 模式则不显示
    const ctxNote = m.max_context_length && m.max_context_length > (m.max_input_tokens || 0)
      ? `<small class="cell-note">max 模式 ${Math.round(m.max_context_length / 1000)}K</small>` : "";
    return `<tr><td><strong>${esc(m.id)}</strong>${m.backend === "trae" ? '<span class="mini-trae">Trae</span>' : ""}<small class="cell-note">${esc(m.name || "")}</small></td><td>${esc(m.description || "—")}</td><td><span class="pill ${m.credits === 0 ? "green" : m.credits && m.credits >= 1 ? "amber" : ""}">${credits}</span>${promoNote}</td><td class="mono">${ctx}${ctxNote}</td><td><span class="pill ${m.enabled ? "green" : ""}">${m.enabled ? "启用" : "禁用"}</span></td><td class="align-right"><button data-model-toggle="${esc(m.id)}" data-enabled="${m.enabled ? "0" : "1"}">${m.enabled ? "禁用" : "启用"}</button></td></tr>`;
  }).join("") || '<tr><td colspan="6" class="muted">模型列表为空，请点击「立即拉取」。</td></tr>';
}
function renderDashboard() {
  const {accounts, metrics:m = {}, pool} = overview;
  const fmt = value => Math.floor(Number(value)).toLocaleString("zh-CN");
  const rate = value => value === null || value === undefined ? "—" : value.toFixed(1) + "%";
  $("dash-available").textContent = accounts.filter(a=>a.pool_state === "available").length + " / " + accounts.length;
  // 两后端积分是不同体系，分开汇总展示（WorkBuddy 积分 / Trae 积分）。
  const known = accounts.filter(a=>typeof a.remaining === "number");
  const parts = ["workbuddy","trae"].map(b => known.filter(a=>(a.backend||"workbuddy")===b)).filter(l=>l.length).map(l=>creditsFmt(l.reduce((s,a)=>s+a.remaining,0)));
  $("dash-credits").textContent = parts.length ? parts.join("  ·  ") : "—";
  $("dash-credit-note").textContent = known.length < accounts.length ? `已查询 ${known.length}/${accounts.length} 个账号，余额可能不完整` : accounts.some(a=>a.credits_stale) ? "包含待刷新余额，以最近一次查询为准" : "以最近一次上游查询为准";
  $("dash-requests").textContent = fmt(m.completed || 0);
  $("dash-request-note").textContent = `API ${m.api_count || 0} · 后台测试 ${m.test_count || 0}`;
  $("dash-success").textContent = rate(m.success_rate); $("dash-http").textContent = rate(m.http_success_rate);
  $("dash-failed").textContent = m.failed || 0; $("dash-inflight").textContent = m.in_flight || 0;
  $("dash-latency").textContent = m.avg_duration_ms === null || m.avg_duration_ms === undefined ? "—" : fmt(m.avg_duration_ms) + " ms";
  $("dash-since").textContent = "统计开始于 " + stamp((m.started_at || 0)*1000);
  $("dash-routing").textContent = "轮流分配请求，自动跳过不可用账号";
  const states={available:"可用",paused:"已暂停",cooling:"冷却中",exhausted:"积分耗尽",invalid:"凭据异常"};
  $("dash-account-list").innerHTML = accounts.slice(0,8).map(a=>`<div class="dashboard-account"><span class="account-icon">${esc(a.name.slice(0,1))}</span><div><strong>${esc(a.name)}</strong>${a.nickname ? `<small class="cell-note">${esc(a.nickname)}</small>` : ""}</div><div class="dashboard-account-credit"><strong>${a.remaining === null || a.remaining === undefined ? "待查询" : creditsFmt(a.remaining)}</strong><small class="cell-note">${a.backend === "trae" ? "Trae 积分" : "积分"}</small></div><span class="pill ${a.pool_state === 'available' ? 'green' : 'amber'}">${states[a.pool_state] || '待查询'}</span></div>`).join("") || '<p class="history-empty">尚未添加账号，点击“添加账号”开始。</p>';
  if(accounts.length > 8) $("dash-account-list").insertAdjacentHTML("beforeend",'<p class="muted">更多账号请前往账号池查看。</p>');
  const outcomes={success:"完成",stream_error:"流式错误",interrupted:"未完整结束",http_error:"请求失败"};
  $("dash-recent-body").innerHTML=(m.recent || []).map(r=>`<tr><td class="mono muted">${esc(stamp(r.time*1000))}</td><td>${r.source === 'test' ? '后台测试' : 'API'}<small class="cell-note mono">${esc(r.path)}</small></td><td><span class="pill ${r.ok ? 'green' : 'red'}">${r.status ?? '—'}</span><small class="cell-note">${outcomes[r.outcome] || '请求失败'}</small></td><td class="align-right mono">${fmt(r.duration_ms)} ms</td></tr>`).join("") || '<tr><td colspan="4" class="history-empty">尚无请求记录。发起 API 调用或后台测试后，这里会自动更新。</td></tr>';
}
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
    const credits = creditsFmt(a.remaining);
    const expiring = creditsFmt(a.expiring_soon);
    const expiringCell = `<td><strong class="credit-number ${a.expiring_soon > 0 ? "credit-warn" : ""}">${expiring}</strong><small class="cell-note">3 天内到期</small></td>`;
    return `<tr><td><div class="account-cell"><span class="account-icon">${esc(a.name.slice(0,1))}</span><div><strong>${esc(a.name)}${a.backend === "trae" ? '<span class="mini-trae">Trae</span>' : ""}</strong>${a.nickname ? `<small>${esc(a.nickname)}</small>` : ""}</div></div></td><td><span class="pill ${status[1]}">${status[0]}</span><small class="cell-note">${a.today_checked_in ? "今日已签到" : a.checkin_when ? `今日 ${esc(a.checkin_slot || "")} ${esc(a.checkin_when)} 签到` : "今日未确认签到"}</small>${a.cooldown_until > Date.now()/1000 ? `<small class="cell-note">至 ${esc(stamp(a.cooldown_until*1000))}</small>` : ""}</td><td><strong class="credit-number">${credits}</strong><small class="cell-note">${a.credits_updated ? esc(stamp(a.credits_updated*1000)) : "点击查询积分"}${a.credits_stale && a.credits_updated ? " · 待刷新" : ""}</small>${a.earliest_expiry ? `<small class="cell-note">最早一批 ${esc(stamp(a.earliest_expiry*1000))} 过期（余 ${creditsFmt(a.earliest_expiry_remaining)}）</small>` : ""}${a.daily_rate ? `<small class="cell-note">日耗约 ${creditsFmt(a.daily_rate)}${a.days_to_empty !== null ? ` · 按当前速率约 ${a.days_to_empty} 天用完` : ""}</small>` : ""}${a.expire_unused ? `<small class="cell-note field-error">预计 ${creditsFmt(a.expire_unused)} 积分将过期浪费</small>` : ""}${a.last_error ? `<small class="cell-note field-error">${esc(a.last_error)}</small>` : ""}</td>${expiringCell}<td class="mono">${esc(stamp(a.expires_at))}<small class="cell-note">${a.expired ? "已到期 · 调用时尝试刷新" : "支持自动刷新"}</small></td><td><button class="weight-btn" data-action="weight" data-id="${a.id}" title="权重越大越优先分配请求">权重 ${a.weight || 0}</button></td><td><div class="actions pool-actions">${a.enabled ? `<button data-action="status" data-id="${a.id}" title="查询积分与签到状态">查询积分</button><button data-action="checkin" data-id="${a.id}">签到</button><button data-action="refresh" data-id="${a.id}">刷新凭据</button>` : ""}<button data-action="rename" data-id="${a.id}">备注</button><button data-action="toggle" data-id="${a.id}">${a.enabled ? "暂停" : "恢复"}</button><button class="danger" data-action="delete" data-id="${a.id}">删除</button></div></td></tr>`;
  }).join("") || (overview.accounts.length ? '<tr><td colspan="6" class="muted">没有符合筛选条件的账号。</td></tr>' : "");
}
$("account-search").addEventListener("input", () => { if(overview) renderAccounts(); });
$("account-filter").addEventListener("change", () => { if(overview) renderAccounts(); });
async function refresh(loadCredits) {
  $("refresh").disabled = true;
  try {
    if (loadCredits) { try { await api("pool/refresh-credits", {method:"POST"}); } catch(e) {} }
    overview = await api("overview"); render(); $("load-error").hidden = true;
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
$("pool-settings").addEventListener("submit", async e => { e.preventDefault();e.submitter.disabled=true;try {await api("pool/settings",{method:"PATCH",body:{routing:$("pool-routing").value,auto_checkin:$("auto-checkin").checked,checkin_time:$("checkin-time").value,credit_min_interval:Number($("credit-interval").value),sync_window:[$("sync-start").value,$("sync-end").value],batch_spread:Number($("batch-spread").value)}});await refresh();toast("账号池设置已保存");}catch(err){toast(err.message);}finally{e.submitter.disabled=false;} });
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
  clearOAuth(); clearTrae(); accountMode = mode;
  $("browser-login-panel").hidden = mode !== "browser";
  $("trae-login-panel").hidden = mode !== "trae";
  $("file-import-panel").hidden = $("account-import").hidden = mode !== "file";
  $("mode-browser").setAttribute("aria-pressed", String(mode === "browser"));
  $("mode-trae").setAttribute("aria-pressed", String(mode === "trae"));
  $("mode-file").setAttribute("aria-pressed", String(mode === "file"));
  $("account-error").textContent = "";
}
function clearTrae() {
  const previous = traeFlow; traeFlow = null; clearTimeout(traeTimer);
  if (previous && csrf) api("trae/" + previous.id, {method:"DELETE"}).catch(() => {});
  $("trae-link-box").hidden = true; $("trae-open").removeAttribute("href");
  $("trae-start").disabled = false; $("trae-start").textContent = "生成登录链接 ↗";
  $("trae-callback").value = ""; $("trae-submit").disabled = false;
  $("trae-status").className = "banner oauth-status";
  $("trae-status").textContent = "点击上方按钮生成登录链接，10 分钟内有效。";
}
$("mode-browser").addEventListener("click", () => setAccountMode("browser"));
$("mode-trae").addEventListener("click", () => setAccountMode("trae"));
$("mode-file").addEventListener("click", () => setAccountMode("file"));
async function pollTrae() {
  if (!traeFlow) return;
  const flow = traeFlow;
  try {
    const result = await api("trae/flow/" + flow.id);
    if (!traeFlow || traeFlow.id !== flow.id) return;
    if (result.status === "done") {
      traeFlow = null;
      $("trae-status").className = "banner oauth-status";
      $("account-dialog").close(); await refresh();
      toast(`Trae 账号已添加${result.nickname ? "：" + result.nickname : ""}`);
      return;
    }
    if (result.status === "error") {
      $("trae-status").className = "banner warning oauth-status";
      $("trae-status").textContent = result.message || "登录失败，请重新生成链接";
      return;
    }
    if (result.status === "expired") {
      $("trae-status").className = "banner warning oauth-status";
      $("trae-status").textContent = "登录链接已过期，请重新生成。";
      return;
    }
    $("trae-status").textContent = $("trae-status").textContent.replace(/\s*·\s*等待中\.\.\.$/, "") + " · 等待中…";
    traeTimer = setTimeout(pollTrae, 3000);
  } catch (e) {
    traeTimer = setTimeout(pollTrae, 5000);
  }
}
$("trae-start").addEventListener("click", async () => {
  clearTrae();
  $("trae-start").disabled = true; $("trae-start").textContent = "正在生成…"; $("account-error").textContent = "";
  try {
    const flow = await api("trae/authorize", {method:"POST", body:{}});
    traeFlow = flow;
    $("trae-open").href = flow.url; $("trae-link-box").hidden = false;
    $("trae-start").textContent = "重新生成链接";
    $("trae-status").textContent = "登录链接已就绪。在浏览器完成登录后，复制页面显示的回调地址，粘贴到下方完成绑定。";
    traeTimer = setTimeout(pollTrae, 4000);
  } catch (e) { $("account-error").textContent = e.message; }
  finally { $("trae-start").disabled = false; }
});
$("trae-link-copy").addEventListener("click", () => { if (traeFlow) copy(traeFlow.url); });
$("trae-submit").addEventListener("click", async () => {
  if (!traeFlow) { $("account-error").textContent = "请先生成登录链接"; return; }
  const callback = $("trae-callback").value.trim();
  if (!callback) { $("account-error").textContent = "请粘贴浏览器地址栏的完整回调链接"; return; }
  $("trae-submit").disabled = true; $("account-error").textContent = "";
  $("trae-status").className = "banner oauth-status"; $("trae-status").textContent = "正在换取 Trae 凭据…";
  try {
    const result = await api("trae/login", {method:"POST", body:{id:traeFlow.id, callback_url:callback}});
    traeFlow = null;
    $("account-dialog").close(); await refresh();
    toast(result.updated ? "Trae 账号凭据已更新" : "Trae 账号已添加");
  } catch (e) {
    $("account-error").textContent = e.message;
    $("trae-status").className = "banner warning oauth-status"; $("trae-status").textContent = e.message;
  } finally { $("trae-submit").disabled = false; }
});
$("account-dialog").addEventListener("close", () => { clearOAuth(); clearTrae(); $("account-form").reset(); $("file-label").textContent = "选择或拖入 .info / .json 文件"; $("account-error").textContent = ""; });
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
$("keys-body").addEventListener("click", e => {
  const limit = e.target.closest("[data-limit]");
  if (limit) { const k = overview.keys.find(x => x.id === limit.dataset.limit); confirmAction("设置每日请求上限", "每天最多允许的请求数，0 或留空表示不限制。超限的请求会返回 429。", () => api("keys/" + k.id, {method:"PATCH", body:{daily_limit:Number($("rename-value").value) || null}}), k.daily_limit ?? ""); return; }
  const b = e.target.closest("[data-revoke]"); if (!b) return; const k = overview.keys.find(x => x.id === b.dataset.revoke); confirmAction("撤销客户端密钥？", `使用「${k.name}」的客户端将无法发起新请求。此操作不可撤销。`, () => api("keys/" + k.id, {method:"DELETE"}));
});
$("add-key").addEventListener("click", () => { $("key-form").hidden = false; $("created-key").hidden = true; $("key-dialog").showModal(); });
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
