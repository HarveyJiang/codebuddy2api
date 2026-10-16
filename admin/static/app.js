"use strict";
const $ = id => document.getElementById(id);
// 积分/消耗一律整数展示（向下取整，保守：宁可少显示不虚报）。
// 只影响**显示层** —— 数据库仍存精确的 *_micro 值，对账与护栏判定不受影响。
// 注意取整时机：多账号求和的场合要「先求和再取整」否则误差会累积，
// 所以调用方应传原始 Number，不要传已格式化的字符串。
const creditsFmt = v => {
  // null / undefined 表示「还没查询过」，不是 0 —— 返回占位符而不是 0，
  // 否则「未查询」和「余额耗尽」在界面上无法区分。数值 0 仍正常显示为 0。
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return Math.floor(n).toLocaleString("zh-CN");
};
let csrf = "", overview = null, pendingConfirm = null, page = "dashboard", busy = false;
let accountMode = "import", oauthFlow = null, oauthTimer = null, oauthGeneration = 0, traeFlow = null, traeTimer = null, traeTick = null, traeBase = "", traeExpiresAt = 0, traeTotal = 600;
const labels = {
  dashboard: ["让每个账号，各尽其用", "WORKBUDDY WORKSPACE", "在这里查看服务运行、账号积分和请求表现。"],
  accounts: ["账号池", "ACCOUNT POOL", "集中管理登录凭据、积分与可用状态，让请求自动分配到可用账号。"],
  keys: ["API 密钥", "CLIENT ACCESS", "为每个客户端分配独立密钥，让连接清晰可控。"],
  models: ["模型管理", "MODEL CATALOG", "与官方客户端一致的模型列表，可按需启用或禁用。"],
  test: ["连接测试", "CONNECTION LAB", "经账号池按路由规则发起请求，确认模型能否正常响应。"],
  usage: ["用量明细", "USAGE LEDGER", "查看上游账单级的积分消耗，默认只读本地库，点击「拉取上游」才请求上游。"],
  settings: ["设置", "WORKSPACE SETTINGS", "签到、排队容错、默认分配与数据库备份等全局设置。"],
  guide: ["接入指南", "GET CONNECTED", "从导入凭据到客户端接入，只需几步。"]
};
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const stamp = ts => ts ? new Date(ts).toLocaleString("zh-CN", {hour12:false}) : "未提供";
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $("toast").hidden = true, 4500); }
function showLogin(message) {
  csrf = ""; overview = null; $("workspace").hidden = true; $("login").hidden = false; $("boot").hidden = true;
  document.querySelectorAll("dialog[open]").forEach(d => d.close()); $("admin-key").value = "";
  // 登录页上的说明：恢复流程会主动清掉会话，这时把人踢回登录页是**预期行为**，
  // 不作说明会让人以为「操作失败/被登出」。
  $("login-error").textContent = message || "";
}
// 版本标识：登录页与会话内的侧边栏各一处，统一走 /health（未鉴权、开销极小，
// 登录前也要能显示，所以不能只依赖 overview）。取不到就保持原样，不显示 null。
function paintVersion(label) {
  if (!label) return;
  $("login-version").textContent = label;
  $("sidebar-version").textContent = label;
}
async function loadVersion() {
  try { const health = await (await fetch("/health", {credentials:"same-origin"})).json(); paintVersion(health.version); }
  catch { /* 版本只是展示项，拿不到不影响使用 */ }
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
  if (next === "usage") initUsagePage();
  if (next === "settings") loadBackups().catch(e => toast(e.message));
}
// 密钥列表的排序 / 筛选（specs/keys-last-used-at.md §8）。密钥总数是个位数到几十，
// overview 一次全量返回，所以前端做即可，不引入服务端参数。默认不排序——
// 保持既有插入顺序，不因新增一列就悄悄改变顺序。
let keysSort = {key: null, dir: 0};
const SEC = 1000;
// 「从未使用」（last_used_at 为空）**无论升降序一律排最后**。
// 升序时 0 会天然顶到最前，看起来像「最久没用」——但「从未使用」和「一年前用过
// 一次」不是一回事，前者更可能是配错的 key，混排会让清理优先级判断错。
const lastUsedRank = k => (k.last_used_at ? 0 : 1);
function sortKeys(keys) {
  const {key, dir} = keysSort;
  if (!key || !dir) return keys;
  const val = k => key === "name" ? String(k.name || "")
    : key === "count" ? (k.usage?.count || 0)
    : key === "last_used_at" ? (k.last_used_at || 0)
    : (k.created || 0);
  return [...keys].sort((a, b) => {
    if (key === "last_used_at") {
      const rank = lastUsedRank(a) - lastUsedRank(b);
      if (rank) return rank;
    }
    const x = val(a), y = val(b);
    const cmp = typeof x === "string" ? x.localeCompare(y) : x - y;
    return cmp * dir;
  });
}
function filterKeys(keys) {
  const mode = $("keys-filter-mode")?.value || "";
  if (!mode) return keys;
  const now = Date.now() / 1000;
  if (mode === "never") return keys.filter(k => !k.last_used_at);
  if (mode === "today") {
    // 北京时间今天 00:00（与后端 _today() 同口径）。
    const cn = new Date(Date.now() + (8 * 60 + new Date().getTimezoneOffset()) * 60000);
    const midnight = Math.floor(new Date(cn.getFullYear(), cn.getMonth(), cn.getDate()).getTime() / SEC);
    return keys.filter(k => (k.last_used_at || 0) >= midnight);
  }
  const days = mode === "stale-1d" ? 1 : 7;
  return keys.filter(k => k.last_used_at && k.last_used_at < now - days * 86400);
}
function paintKeysSort() {
  document.querySelectorAll("[data-sort]").forEach(th => {
    const on = th.dataset.sort === keysSort.key && keysSort.dir;
    th.dataset.active = on ? (keysSort.dir > 0 ? "asc" : "desc") : "";
    // 指针样式 + 方向箭头都在 th 上，不另找 CSS：表头本来就该可点。
    th.style.cursor = "pointer";
    th.title = on ? (keysSort.dir > 0 ? "升序（点击改为降序）" : "降序（点击取消排序）")
                  : "点击按此列排序";
  });
}

function render() {
  const {accounts, models, events} = overview;
  const keys = sortKeys(filterKeys(overview.keys || []));
  renderLoginModes();
  const active = accounts.find(a => a.enabled && a.pool_state === "available") || accounts.find(a => a.enabled);
  paintVersion(overview.version?.label);
  renderDashboard();
  // 需求 6：账号池并进概览，账号表格（搜索/筛选/批量签到/逐账号操作）就长在概览里。
  // 侧边栏不再有「账号池」页；下面这些元素都是概览页的。
  $("dash-account-badge").textContent = accounts.length;
  $("test-account").textContent = "账号池轮转 · 自动选择可用账号";
  $("accounts-empty").hidden = accounts.length > 0;
  renderAccounts();
  if (!$("pool-settings").contains(document.activeElement)) {
    $("pool-routing").value = overview.pool?.default_routing || "round_robin"; $("auto-checkin").checked = overview.pool?.auto_checkin || false; $("checkin-time").value = overview.pool?.checkin_time || "09:00"; $("credit-interval").value = overview.pool?.credit_min_interval ?? 60; $("batch-spread").value = overview.pool?.batch_spread ?? 90; $("sync-start").value = overview.pool?.sync_start || "10:00"; $("sync-end").value = overview.pool?.sync_end || "20:00";
    $("queue-failover").checked = overview.pool?.queue_failover || false; $("queue-model-fallback").checked = overview.pool?.queue_model_fallback !== false; $("queue-retry").value = overview.pool?.queue_retry ?? 3; $("queue-cooldown").value = overview.pool?.queue_cooldown ?? 120; $("queue-hold-ms").value = overview.pool?.queue_hold_ms ?? 3000; $("queue-fallback-model").value = overview.pool?.queue_fallback_model || "fast-model";
    paintBackendToggles();
    paintSettings();
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
    return `<tr><td><strong>${esc(k.name)}</strong>${k.expires_at ? `<small class="cell-note">${expired ? "已于" : "至"} ${esc(stamp(k.expires_at * 1000))} ${esc(expired ? "过期" : "到期")}</small>` : ""}</td><td><code>${esc(k.hint)}</code></td><td class="usage-cell" data-usage="${k.id}" title="点击查看近 30 天趋势"><span class="pill ${warn}">${u.count || 0}${limited ? ` / ${k.daily_limit}` : ""}</span>${warn ? `<small class="cell-note">${ratio >= 1 ? "已达上限，新请求返回 429" : "接近每日上限"}</small>` : `<small class="cell-note">${u.tokens ? fmtTokens(u.tokens) + " tokens · " : ""}${esc(u.date || "")}</small>`}</td><td>${k.last_used_at ? `<span class="mono">${esc(stamp(k.last_used_at * 1000))}</span>` : '<span class="muted">从未使用</span>'}</td><td>${scope(k.models, "模型")}</td><td>${scope(k.accounts, "账号")}</td><td>${revoked ? "—" : `<button class="routing-chip" data-routing-edit="${k.id}" title="该密钥的账号分配策略，点击修改">${routingIcon(routing)} ${routingShort(routing)}</button>`}</td><td><span class="pill ${state[1]}">${state[0]}</span></td><td class="align-right"><div class="actions">${revoked ? "" : `<button data-bind="${k.id}" title="设置可用模型、可用账号与分配策略">设置</button><button data-requests="${k.id}" title="查看该密钥最近的请求记录">请求</button><button data-limit="${k.id}" title="设置每日最大请求数">限额</button><button data-toggle="${k.id}">${k.enabled ? "停用" : "恢复"}</button>`}<button class="danger" data-revoke="${k.id}">撤销</button></div></td></tr>`;
  }).join("");
  paintKeysSort();
  const shown = keys.length, total = (overview.keys || []).length;
  $("keys-filter-note").textContent = shown === total ? "" : `显示 ${shown} / ${total} 个`;
  renderModels();
  const modelIds = Array.isArray(models?.models) ? models.models.filter(m => m.enabled).map(m => m.id) : [];
  const selected = $("model").value || "deepseek-v4-flash";
  $("model").replaceChildren(...modelIds.map(model => { const o = document.createElement("option"); o.value = o.textContent = model; return o; }));
  if (modelIds.includes(selected)) $("model").value = selected;
  $("test-submit").disabled = !accounts.some(a => a.enabled) || busy;
  $("test-history").innerHTML = events.length ? events.map(e => `<div class="history-row"><span class="mono muted">${esc(stamp(e.time * 1000))}</span><strong>${esc(e.model)}</strong><span class="pill ${e.ok ? "green" : "red"}">${e.ok ? "成功" : "失败"}</span><span class="mono">${e.seconds}s</span></div>`).join("") : '<p class="history-empty">暂无测试记录，发送第一条测试消息。</p>';
}
// 后端元数据与徽标。**所有与后端名有关的显示都走这几个 helper**，不在各处写
// `a.backend === "trae"`：将来加后端（如阿里千问办公）只在后端侧新增一个类，
// 页面各处不用动。
//
// 元数据来自 `GET /admin/api/backends`（服务端读后端注册表），**不在前端登记**：
// 前端若自己留一张表，加了后端忘了同步就会静默显示成默认值——正是这次要消灭的
// 那类 bug。图标是纯文本符号，与导航栏风格一致，不引入图标字体依赖。
let backends = [];
function applyBackends(list) {
  backends = (Array.isArray(list) ? list : []).slice()
    .sort((a, b) => (a.order ?? 50) - (b.order ?? 50) || String(a.name).localeCompare(String(b.name)));
}
// 后端总闸（settings.backend_enabled）：复选框由注册表驱动，加后端自动多一项。
// enabled 取自后端接口的 is_enabled（已含环境逃生门），前端不自己判断。
function paintBackendToggles() {
  const box = $("backend-toggles");
  if (!box) return;
  box.innerHTML = backends.map(b => `<label class="inline-checkbox" data-env-locked="${b.env_disabled ? "1" : ""}" title="${esc(b.env_disabled ? "该后端已被环境变量 CODEBUDDY_ENABLE_TRAE=0 禁用，开关不生效" : "")}"><input type="checkbox" data-backend-toggle="${esc(b.name)}"${b.enabled ? " checked" : ""}>${backendIconSvg(b.name)} ${esc(b.label)}${b.env_disabled ? '<span class="pill">环境禁用</span>' : ""}</label>`).join("")
    || `<span class="muted">没有可用后端</span>`;
  lockBackendToggles();
}
// 不变量「启用集合恒 ≥ 1」的前端一半：只剩一个启用时把它置灰（服务端也会拒，
// 两处都要有——只靠前端的话 curl 直打接口就能绕过）。
function lockBackendToggles() {
  const boxes = [...document.querySelectorAll("[data-backend-toggle]")];
  // 环境门关着的复选框**永远**置灰，不参与「至少留一个」的计数——
  // 它们本来就不可用，勾它既没效果也不该拿来凑不变量。
  const live = boxes.filter(b => b.closest("label")?.dataset.envLocked !== "1");
  const on = live.filter(b => b.checked);
  boxes.forEach(b => {
    const label = b.closest("label");   // markup 一旦变动这里会变 undefined，
    const envLocked = label?.dataset.envLocked === "1";  // 所以全程用 ?. 与判空
    const solo = !envLocked && on.length === 1 && b.checked;
    b.disabled = envLocked || solo;
    if (envLocked || !label) return;   // title 保留 paint 时写的环境禁用说明
    label.title = solo ? "至少需要保留一个后端启用" : "";
  });
}
const backendToggle = $("backend-toggles");
if (backendToggle) {
  backendToggle.addEventListener("change", lockBackendToggles);
  backendToggle.addEventListener("click", () => setTimeout(lockBackendToggles, 0));
}
// 提交用：{后端名: 是否启用}。置灰的那项仍是 checked，照实提交（它就是那个保留的）。
// 环境门关着的项照实回传现值：服务端会保留库里的原值（规则 4），
// 这里保持同样口径，免得两边对「提交值代表什么」的理解不一致。
const backendFlagsBody = () => Object.fromEntries(
  [...document.querySelectorAll("[data-backend-toggle]")].map(b => [b.dataset.backendToggle, b.checked]));

const backendMeta = b => backends.find(x => x.name === b) || null;
const backendIcon = b => backendMeta(b)?.icon || "◇";
const backendName = b => backendMeta(b)?.label || b || "未知";

// 图形场景的图标：后端声明的内联 SVG（见 BackendSpec.icon_svg）。
//
// **不能过 esc()**：这段要当 HTML 插进 DOM，转义会把标签变成可见的文本
// （`<svg>` 原样显示出来）。代价是这段字符串绕开了 esc 的保护，所以不信任它：
// 这里做一次结构化校验，只放行「一个 <svg> 元素、只含形状/分组/路径标签、
// 不带 script/on*/href」的串，其余一律回落到字符图标（转义后再插）。
// 后端声明是我们自己的代码，这层是纵深防御——万一将来 icon_svg 来自配置/用户，
// 也不会变成注入点。
const SVG_ALLOWED_TAGS = new Set(["svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon"]);
const backendIconSvg = b => {
  const meta = backendMeta(b);
  const raw = meta?.icon_svg;
  if (typeof raw !== "string" || !raw.trim()) return `<span class="backend-icon-text">${esc(backendIcon(b))}</span>`;
  // 只接受单个根 <svg …>…</svg>，内部无 <script/<style/<foreignObject、无 on* 事件、无 href。
  if (!/^<svg[\s>][\s\S]*<\/svg>$/.test(raw.trim())) return `<span class="backend-icon-text">${esc(backendIcon(b))}</span>`;
  if (/<\s*(script|style|foreignObject|use|image|iframe|object|embed)\b/i.test(raw)) return `<span class="backend-icon-text">${esc(backendIcon(b))}</span>`;
  if (/\son[a-z]+\s*=/i.test(raw) || /\b(?:xlink:)?href\s*=/i.test(raw)) return `<span class="backend-icon-text">${esc(backendIcon(b))}</span>`;
  const tags = [...raw.matchAll(/<\/?([a-zA-Z][\w:-]*)/g)].map(m => m[1].toLowerCase());
  if (tags.some(t => !SVG_ALLOWED_TAGS.has(t))) return `<span class="backend-icon-text">${esc(backendIcon(b))}</span>`;
  // 颜色由外层 CSS/内联 color 决定；这里补 currentColor 兜底，避免后端忘了写 fill。
  return raw.replace(/<svg\b(?![^>]*\bclass=)/, '<svg class="backend-icon-svg"');
};
// 列表列宽窄时用短名（最近请求的后端列）。
const backendLabel = b => backendMeta(b)?.short || b || "—";
// 后端在展示顺序里的序号；未知后端排在已知的后面（而不是插到最前）。
const backendOrder = b => backendMeta(b)?.order ?? 50;

// 请求行里的后端徽标；backend 取自**实际选中的账号**，老记录/账号已删显示「—」。
function backendBadge(b) {
  if (!b) return '<span class="muted">—</span>';
  return `<span class="mini-backend mini-${esc(b)}" title="该请求由 ${esc(b)} 账号处理">${esc(backendLabel(b))}</span>`;
}
// 带后端名的徽标（账号表、模型表用），形如「◈ WorkBuddy」。
function backendTag(b) {
  const key = b || backends[0]?.name;
  return `<span class="mini-backend mini-${esc(key)}" title="${esc(backendName(key))} 账号">${backendIconSvg(key)} ${esc(backendName(key))}</span>`;
}

function renderModels() {
  const catalog = overview?.models;
  const list = Array.isArray(catalog?.models) ? catalog.models : [];
  // 同名模型跨后端合并成一行（需求 8）。**只合并展示，底层仍是两个独立模型** ——
  // 倍率/上下文往往不同（如 step-5-preview：腾讯免费、字节 0.48），若合成一个数，
  // 成本账本与排队降级会误判免费/付费。
  const groups = groupModelsByName(list);
  $("models-badge").textContent = groups.filter(g => g.items.some(m => m.enabled)).length + " / " + groups.length;
  const note = $("models-note");
  if (catalog?.error) { note.textContent = `上游拉取异常：${catalog.error}（当前展示${catalog.updated ? "上次缓存" : "内置官方列表"}）`; note.classList.add("field-error"); }
  else { note.textContent = catalog?.updated ? `上次拉取：${stamp(catalog.updated * 1000)} · 每 6 小时自动刷新。同名模型已合并展示，倍率/上下文按后端分行。禁用的模型不会出现在 /v1/models。` : "尚未从上游拉取过，展示内置官方列表。服务启动与每 6 小时会自动尝试拉取。"; note.classList.remove("field-error"); }
  $("models-body").innerHTML = groups.map(renderModelGroup).join("") || '<tr><td colspan="7" class="muted">模型列表为空，请点击「立即拉取」或手动添加。</td></tr>';
}

// 按「剥掉后端前缀后的模型名」归组：同名的不同后端合并到一组。
// 组内保持默认后端在前、其余按注册表顺序，保证渲染顺序稳定。
function groupModelsByName(list) {
  const byName = new Map();
  for (const m of list) {
    const key = modelBaseName(m.id);
    if (!byName.has(key)) byName.set(key, {key, items: []});
    byName.get(key).items.push(m);
  }
  const groups = [...byName.values()];
  // 组的先后**直接用服务端给的顺序**（Map 保留插入序），这里不再重排。
  //
  // 曾经这里按「后端顺序 + 名字字母序」排过一遍，把 `snapshot()` 的四层排序
  // （来源 → 归属 → 启用 → 倍率）**整个覆盖**掉——于是设置页排序规则再怎么调、
  // 页面都不变，看着就是「乱的」。规则只该有一处：服务端。
  // 组内（同一模型跨后端的那几条）保留服务端顺序即可，它同样来自四层排序。
  for (const g of groups) {
    g.items.sort((a, b) => (backendOrder(a.backend) - backendOrder(b.backend)) || a.id.localeCompare(b.id));
  }
  return groups;
}
// 模型 id 去掉后端前缀后的本名。前缀表来自注册表（后端可声明任意前缀），
// 不再写死 `trae/`。
function modelBaseName(id) {
  let s = String(id || "");
  for (const b of backends) if (b.prefix && s.startsWith(b.prefix)) s = s.slice(b.prefix.length);
  return s;
}

// 某个同名组包含哪些条目 —— **渲染与批量操作共用这一处判断**。
// 若两边各算一遍，将来改了归组规则就会「显示 2 条、只操作到 1 条」且界面不报错。
function modelsInGroup(list, key) {
  return (Array.isArray(list) ? list : []).filter(m => modelBaseName(m.id) === key);
}

// 倍率/上下文的统一格式化。倍率保留后端给的精度（0.06 / 0.79 这种不能取整，
// 否则会丢失免费与付费的区别）。0 是「免费」、null 是「按标准计费」，两者必须区分。
const creditsText = m => !Number.isFinite(m.credits) ? "标准" : m.credits === 0 ? "免费" : "×" + m.credits;
const ctxText = m => m.max_input_tokens ? (m.max_input_tokens >= 1000 ? Math.round(m.max_input_tokens / 1000) + "K" : String(m.max_input_tokens)) : "—";

function renderModelGroup(g) {
  const items = g.items;
  const primary = items[0];
  const multi = items.length > 1;
  const enabledCount = items.filter(m => m.enabled).length;
  const allEnabled = enabledCount === items.length;
  const anyEnabled = enabledCount > 0;

  // 后端标记：单后端直接打标；多后端在每个值前面标，读得出「哪个数属于哪个后端」。
  const tagFor = m => `<span class="mini-backend mini-${esc(m.backend || backends[0]?.name)}" title="${esc(backendName(m.backend))}">${backendIconSvg(m.backend)} ${esc(backendLabel(m.backend))}</span>`;
  // 行前归属 icon（specs/model-backend-attribution.md §4.1）：纯 icon 无 pill
  // （复用账号表同款 .account-icon-backend），颜色由后端声明经 /admin/api/backends
  // 提供。多后端组按后端顺序并排，一眼看出这行跨了几条链路。
  const attrIcon = m => `<span class="account-icon account-icon-backend" style="color:${esc(backendMeta(m.backend)?.color || "#888")}" title="${esc(backendName(m.backend))}">${backendIconSvg(m.backend)}</span>`;
  const attrIcons = items.map(attrIcon).join("");

  const creditsCell = multi
    ? items.map(m => `<div class="model-line">${tagFor(m)}<span class="pill ${m.credits === 0 ? "green" : m.credits && m.credits >= 1 ? "amber" : ""}">${esc(creditsText(m))}</span></div>`).join("")
    : `<span class="pill ${primary.credits === 0 ? "green" : primary.credits && primary.credits >= 1 ? "amber" : ""}">${esc(creditsText(primary))}</span>`;
  const promoNote = primary.promo ? `<small class="cell-note">${esc(primary.promo.label)} ×${primary.promo.credits}</small>` : "";

  const ctxCell = multi
    ? items.map(m => `<div class="model-line">${tagFor(m)}<span class="mono">${esc(ctxText(m))}</span></div>`).join("")
    : `<span class="mono">${esc(ctxText(primary))}</span>${primary.max_context_length && primary.max_context_length > (primary.max_input_tokens || 0) ? `<small class="cell-note">max 模式 ${Math.round(primary.max_context_length / 1000)}K</small>` : ""}`;

  const sourceLabel = {upstream: "上游", builtin: "内置", custom: "自定义"}[primary.source] || primary.source || "—";
  const sourceTag = `<span class="pill ${primary.custom ? "amber" : ""}">${esc(sourceLabel)}</span>`;
  const desc = items.map(m => m.description).find(Boolean);

  // 行级批量开关：一次作用于该名字的**全部后端**（决定 8 的「合并显示 + 默认全选禁用/启用」）。
  const batchToggle = `<button data-model-group-toggle="${esc(g.key)}" data-enabled="${allEnabled ? "0" : "1"}" title="一次${allEnabled ? "禁用" : "启用"}该模型在所有后端的条目">${allEnabled ? "全部禁用" : "全部启用"}</button>`;
  // 单后端删除（仅自定义项可删）。
  const del = items.filter(m => m.custom).map(m => `<button class="danger" data-model-delete="${esc(m.id)}">删除${multi ? ` ${esc(backendLabel(m.backend))}` : ""}</button>`).join("");
  // 单后端开关：合并行里仍需能只停其中一个后端。
  const perBackend = multi
    ? `<small class="cell-note">${items.map(m => `<button class="model-mini-toggle" data-model-toggle="${esc(m.id)}" data-enabled="${m.enabled ? "0" : "1"}" title="${esc(m.id)}">${tagFor(m)}${m.enabled ? "已启用" : "已禁用"}</button>`).join(" ")}</small>`
    : "";

  const idCell = multi
    ? `<span class="model-id-line">${attrIcons}</span><strong>${esc(g.key)}</strong><small class="cell-note">${items.length} 个后端：${esc(items.map(m => backendName(m.backend)).join(" / "))}</small>`
    : `<span class="model-id-line">${attrIcons}</span><strong>${esc(primary.id)}</strong><small class="cell-note">${esc(primary.name || "")}</small>`;

  const stateCell = multi
    ? `<span class="pill ${allEnabled ? "green" : anyEnabled ? "amber" : ""}">${allEnabled ? "启用" : anyEnabled ? `${enabledCount}/${items.length} 启用` : "禁用"}</span>`
    : `<span class="pill ${primary.enabled ? "green" : ""}">${primary.enabled ? "启用" : "禁用"}</span>`;

  return `<tr><td>${idCell}</td><td>${sourceTag}</td><td>${esc(desc || "—")}</td><td>${creditsCell}${promoNote}</td><td>${ctxCell}</td><td>${stateCell}</td><td class="align-right">${del}${batchToggle}${perBackend}</td></tr>`;
}
function renderDashboard() {
  const {accounts, metrics:m = {}, pool} = overview;
  const fmt = value => Math.floor(Number(value)).toLocaleString("zh-CN");
  const rate = value => value === null || value === undefined ? "—" : value.toFixed(1) + "%";
  $("dash-available").textContent = accounts.filter(a=>a.pool_state === "available").length + " / " + accounts.length;
  // 各后端积分是不同体系，分开汇总展示（如 WorkBuddy 积分 / Trae 积分）。
  // 后端列表来自注册表：加后端后这里自动多一段，不必改代码。
  const known = accounts.filter(a=>typeof a.remaining === "number");
  const parts = backends.map(b => known.filter(a=>(a.backend || backends[0]?.name)===b.name))
    .filter(l=>l.length).map(l=>creditsFmt(l.reduce((s,a)=>s+a.remaining,0)));
  $("dash-credits").textContent = parts.length ? parts.join("  ·  ") : "—";
  $("dash-credit-note").textContent = known.length < accounts.length ? `已查询 ${known.length}/${accounts.length} 个账号，余额可能不完整` : accounts.some(a=>a.credits_stale) ? "包含待刷新余额，以最近一次查询为准" : "以最近一次上游查询为准";
  $("dash-requests").textContent = fmt(m.completed || 0);
  $("dash-request-note").textContent = `API ${m.api_count || 0} · 后台测试 ${m.test_count || 0}（历史累计）`;
  $("dash-success").textContent = rate(m.success_rate); $("dash-http").textContent = rate(m.http_success_rate);
  $("dash-failed").textContent = fmt(m.failed || 0); $("dash-inflight").textContent = m.in_flight || 0;
  $("dash-latency").textContent = m.avg_duration_ms === null || m.avg_duration_ms === undefined ? "—" : fmt(m.avg_duration_ms) + " ms";
  $("dash-since").textContent = "统计开始于 " + stamp((m.started_at || 0)*1000);
  $("dash-routing").textContent = ({round_robin:"轮流分配请求，自动跳过不可用账号", random:"在可用账号中随机分配", expiry_first:"优先消耗最早到期的积分"}[overview.pool?.default_routing] || "按各密钥的分配策略选择账号");
  const states={available:"可用",paused:"已暂停",cooling:"冷却中",exhausted:"积分耗尽",invalid:"凭据异常"};
}
const outcomes={success:"完成",stream_error:"流式错误",interrupted:"未完整结束",http_error:"请求失败"};
// 分配策略的图标与短名：列表里用「图标 + 短名」展示，编辑集中到设置弹框。
// 图标是纯文本符号，与导航栏风格一致，不引入图标字体依赖。
const ROUTING_ICON = {"": "↺", round_robin: "↻", random: "⁘", expiry_first: "⏳"};
const ROUTING_SHORT = {"": "跟随默认", round_robin: "轮转", random: "随机", expiry_first: "快过期优先"};
// 未知策略值**显示原值**而不是回落到「跟随默认」：回落会把服务端真实的配置
// 显示成另一个策略，比显示一个陌生的英文词更容易误导。空串（未设置）才回落。
const routingShort = r => r === "" || r == null ? ROUTING_SHORT[""] : (ROUTING_SHORT[r] || String(r));
const routingIcon = r => r === "" || r == null ? ROUTING_ICON[""] : (ROUTING_ICON[r] || "•");
let logOffset = 0, logTotal = 0;
function renderLogRows(rows) {
  $("dash-recent-body").innerHTML = rows.map(r=>`<tr><td class="mono muted">${esc(stamp(r.time*1000))}</td><td>${backendBadge(r.backend)}</td><td class="mono">${esc(r.model_id || "—")}${r.account_name ? `<small class="cell-note">${esc(r.account_name)}</small>` : ""}</td><td><span class="pill ${r.ok ? 'green' : 'red'}">${r.status ?? '—'}</span><small class="cell-note">${outcomes[r.outcome] || '请求失败'}</small></td><td class="align-right mono">${Math.round(Number(r.duration_ms)||0).toLocaleString("zh-CN")} ms</td></tr>`).join("") || '<tr><td colspan="5" class="history-empty">尚无请求记录。发起 API 调用或后台测试后，这里会自动更新。</td></tr>';
}
async function loadLogs(offset) {
  // 默认 10 条（需求：默认显示 10，可选 10/20/30/50）。
  const size = Number($("dash-page-size").value) || 10;
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
// 账号表的当前页与每页条数。与最近请求一样默认 10 条，选项 10/20/30/50。
let accountPage = 0, accountPageSize = 10;
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
  // 翻页：账号多时一屏放不下，搜索/筛选仍作用于**全部**账号（先筛后排再切片）。
  const pages = Math.max(1, Math.ceil(rows.length / accountPageSize));
  if (accountPage > pages - 1) accountPage = pages - 1;
  if (accountPage < 0) accountPage = 0;
  const pageRows = rows.slice(accountPage * accountPageSize, (accountPage + 1) * accountPageSize);
  const pager = $("account-pager");
  if (pager) {
    pager.hidden = rows.length <= accountPageSize;
    $("account-page-info").textContent = `第 ${accountPage + 1} / ${pages} 页 · 共 ${rows.length} 个`;
    $("account-prev").disabled = accountPage === 0;
    $("account-next").disabled = accountPage >= pages - 1;
  }
  $("accounts-body").innerHTML = pageRows.map(a => {
    const status = labels[a.pool_state] || ["待查询", ""];
    // 未查询过的账号 remaining 为 null。creditsFmt(null) 会得到 "0"，
    // 显示成「剩余积分 0」与真的耗尽无法区分 —— 明确显示「待查询」。
    const credits = a.remaining === null || a.remaining === undefined ? "待查询" : creditsFmt(a.remaining);
    const expiring = a.expiring_soon === null || a.expiring_soon === undefined ? "—" : creditsFmt(a.expiring_soon);
    const expiringCell = `<td><strong class="credit-number ${a.expiring_soon > 0 ? "credit-warn" : ""}">${expiring}</strong><small class="cell-note">3 天内到期</small></td>`;
    // 账号类型图标（需求 5）：用后端注册表的 icon，而不是固定的首字母。
    // 首字母在两个后端都可能一样时无法区分账号类型，这正是需求要解决的问题。
    const kindIcon = `<span class="account-icon account-icon-backend" style="color:${esc(backendMeta(a.backend)?.color || "#888")}" title="${esc(backendName(a.backend))} 账号">${backendIconSvg(a.backend)}</span>`;
    // data-account + 可点击样式：点行开详情（需求 6），点行内按钮不弹框。
    return `<tr class="account-row" data-account="${esc(a.id)}"><td><div class="account-cell">${kindIcon}<div><strong>${esc(a.name)}</strong>${backendTag(a.backend)}${a.nickname ? `<small>${esc(a.nickname)}</small>` : ""}</div></div></td><td><span class="pill ${status[1]}">${status[0]}</span><small class="cell-note">${a.today_checked_in ? "今日已签到" : a.checkin_when ? `今日 ${esc(a.checkin_slot || "")} ${esc(a.checkin_when)} 签到` : "今日未确认签到"}</small>${a.cooldown_until > Date.now()/1000 ? `<small class="cell-note">至 ${esc(stamp(a.cooldown_until*1000))}</small>` : ""}${a.queued_until > Date.now()/1000 ? `<small class="cell-note">排队让位至 ${esc(stamp(a.queued_until*1000))}</small>` : ""}</td><td><strong class="credit-number">${credits}</strong><small class="cell-note">${a.credits_updated ? esc(stamp(a.credits_updated*1000)) : "点击查询积分"}${a.credits_stale && a.credits_updated ? " · 待刷新" : ""}</small>${a.earliest_expiry ? `<small class="cell-note">最早一批 ${esc(stamp(a.earliest_expiry*1000))} 过期（余 ${creditsFmt(a.earliest_expiry_remaining)}）</small>` : ""}${a.daily_rate ? `<small class="cell-note">日耗约 ${creditsFmt(a.daily_rate)}${a.days_to_empty !== null ? ` · 按当前速率约 ${a.days_to_empty} 天用完` : ""}</small>` : ""}${a.expire_unused ? `<small class="cell-note field-error">预计 ${creditsFmt(a.expire_unused)} 积分将过期浪费</small>` : ""}${a.last_error ? `<small class="cell-note field-error">${esc(a.last_error)}</small>` : ""}</td>${expiringCell}<td class="mono">${esc(stamp(a.expires_at))}<small class="cell-note">${a.expired ? "已到期 · 调用时尝试刷新" : "支持自动刷新"}</small></td><td><button class="weight-btn" data-action="weight" data-id="${a.id}" title="权重越大越优先分配请求">权重 ${a.weight || 0}</button></td><td><div class="actions pool-actions">${a.enabled ? `<button data-action="status" data-id="${a.id}" title="查询积分与签到状态">查询积分</button><button data-action="checkin" data-id="${a.id}">签到</button><button data-action="refresh" data-id="${a.id}">刷新凭据</button>` : ""}<button data-action="rename" data-id="${a.id}">备注</button><button data-action="toggle" data-id="${a.id}">${a.enabled ? "暂停" : "恢复"}</button><button class="danger" data-action="delete" data-id="${a.id}">删除</button></div></td></tr>`;
  }).join("") || (overview.accounts.length ? '<tr><td colspan="7" class="muted">没有符合筛选条件的账号。</td></tr>' : "");
}
$("account-search").addEventListener("input", () => { accountPage = 0; if(overview) renderAccounts(); });
$("account-filter").addEventListener("change", () => { accountPage = 0; if(overview) renderAccounts(); });
$("account-prev").addEventListener("click", () => { accountPage = Math.max(0, accountPage - 1); if(overview) renderAccounts(); });
$("account-next").addEventListener("click", () => { accountPage += 1; if(overview) renderAccounts(); });
$("account-page-size").addEventListener("change", e => { accountPageSize = Number(e.target.value) || 10; accountPage = 0; if(overview) renderAccounts(); });

// 账号详情弹框（需求 6）：点账号行打开。**只读** —— 行内的操作按钮仍承担写操作，
// 这里不重复一份，避免两条写路径各自漂移。
function openAccountDetail(a) {
  if (!a) return;
  const yes = v => v === true ? "是" : v === false ? "否" : "—";
  const stampOr = v => v ? stamp(v * 1000) : "—";
  const rows = [
    ["账号类型", `${backendIcon(a.backend)} ${backendName(a.backend)}`],
    ["UID", a.uid || "—"],
    ["昵称", a.nickname || "—"],
    ["状态", a.pool_state || "—"],
    ["剩余积分", a.remaining === null || a.remaining === undefined ? "待查询" : creditsFmt(a.remaining)],
    ["积分更新时间", stampOr(a.credits_updated)],
    ["3 天内到期", a.expiring_soon === null || a.expiring_soon === undefined ? "—" : creditsFmt(a.expiring_soon)],
    ["最早一批过期", stampOr(a.earliest_expiry)],
    ["预计过期浪费", a.expire_unused ? creditsFmt(a.expire_unused) : "—"],
    ["日耗估算", a.daily_rate ? creditsFmt(a.daily_rate) : "—"],
    ["预计用完天数", a.days_to_empty === null || a.days_to_empty === undefined ? "—" : String(a.days_to_empty)],
    ["今日已签到", yes(a.today_checked_in)],
    ["签到时段", a.checkin_slot ? `${a.checkin_slot} ${a.checkin_when || ""}` : "—"],
    ["冷却至", a.cooldown_until > Date.now()/1000 ? stamp(a.cooldown_until*1000) : "—"],
    ["排队让位至", a.queued_until > Date.now()/1000 ? stamp(a.queued_until*1000) : "—"],
    ["凭据有效期", stamp(a.expires_at) + (a.expired ? "（已到期）" : "")],
    ["调度权重", String(a.weight || 0)],
    ["是否启用", yes(a.enabled)],
    ["最近错误", a.last_error || "—"],
  ];
  $("account-detail-title").textContent = a.name || "账号详情";
  $("account-detail-sub").textContent = `${backendName(a.backend)} 账号 · ${a.id}`;
  $("account-detail-body").innerHTML = rows.map(([k, v]) =>
    `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("");
  $("account-detail-dialog").showModal();
}
async function refresh(loadCredits) {
  $("refresh").disabled = true;
  try {
    if (loadCredits) { try { await api("pool/refresh-credits", {method:"POST"}); } catch(e) {} }
    // 后端元数据先于 overview：渲染函数全依赖它（图标/名称/顺序/前缀）。
    try { applyBackends((await api("backends"))?.backends); } catch (e) { /* 旧版本服务无此接口，helper 会回落默认值 */ }
    overview = await api("overview"); render(); $("load-error").hidden = true;
    // 用量页的账号下拉依赖 overview，deep-link 进来时它还是 null，这里补一次。
    if (usageInitialized) fillUsageAccounts();
    if (page === "dashboard") loadLogs(logOffset);
  }
  catch (e) { $("load-error").textContent = e.message; $("load-error").hidden = false; throw e; }
  finally { $("refresh").disabled = false; }
}
async function enter() { $("boot").hidden = true; $("login").hidden = true; $("workspace").hidden = false; goPage(location.hash.slice(1)); await refresh(true); }
$("login-form").addEventListener("submit", async e => {
  // e.submitter 只在**点击**按钮提交时有值；按回车提交表单或程序化 submit 时为 null。
  // 直接解引用会让整个登录流程抛异常（实测：回车提交即无法登录）。
  e.preventDefault();
  const button = e.submitter || $("login-form").querySelector("button[type=submit]");
  if (button) button.disabled = true;
  $("login-error").textContent = "";
  try { const data = await api("login", {method:"POST",body:{key:$("admin-key").value.trim()}}); csrf = data.csrf; $("admin-key").value = ""; await enter(); }
  catch (error) { $("login-error").textContent = error.message; }
  finally { if (button) button.disabled = false; }
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
$("dash-add").addEventListener("click", () => openAccount());
$("dash-add-account").addEventListener("click", () => openAccount());
$("empty-add").addEventListener("click", () => openAccount());
$("accounts-back-dash").addEventListener("click", e => { e.preventDefault(); goPage("dashboard"); });
$("pool-settings").addEventListener("submit", async e => { e.preventDefault();e.submitter.disabled=true;try {await api("pool/settings",{method:"PATCH",body:{default_routing:$("pool-routing").value,auto_checkin:$("auto-checkin").checked,checkin_time:$("checkin-time").value,credit_min_interval:Number($("credit-interval").value),sync_start:$("sync-start").value,sync_end:$("sync-end").value,batch_spread:Number($("batch-spread").value),queue_failover:$("queue-failover").checked,queue_model_fallback:$("queue-model-fallback").checked,queue_retry:Number($("queue-retry").value),queue_cooldown:Number($("queue-cooldown").value),queue_hold_ms:Number($("queue-hold-ms").value),queue_fallback_model:$("queue-fallback-model").value.trim(),guard_reserve_credits:Number($("guard-reserve-credits").value),guard_daily_token_limit:Number($("guard-daily-token-limit").value),guard_daily_credit_limit:Number($("guard-daily-credit-limit").value),guard_model_daily_token_limit:Number($("guard-model-daily-token-limit").value),backend_enabled:backendFlagsBody()}});await refresh();toast("设置已保存");}catch(err){toast(err.message);}finally{e.submitter.disabled=false;} });
setInterval(() => { if(csrf && overview && !document.hidden && !document.querySelector("dialog[open]") && !$("refresh").disabled) refresh().catch(()=>{}); },120000);document.querySelectorAll("[data-page]").forEach(b => b.addEventListener("click", () => goPage(b.dataset.page)));

// ── 数据库备份 ──────────────────────────────────────────────────────────
let backups = null, backupBusy = false;
function paintSettings() {
  const pool = overview.pool || {};
  $("backup-enabled").checked = !!pool.backup_enabled;
  $("backup-interval").value = pool.backup_interval_hours ?? 24;
  $("backup-keep").value = pool.backup_keep ?? 10;
  // 账号限额护栏。0 = 关闭，是默认值。
  $("guard-reserve-credits").value = pool.guard_reserve_credits ?? 0;
  $("guard-daily-token-limit").value = pool.guard_daily_token_limit ?? 0;
  $("guard-daily-credit-limit").value = pool.guard_daily_credit_limit ?? 0;
  $("guard-model-daily-token-limit").value = pool.guard_model_daily_token_limit ?? 0;
}
async function loadBackups() {
  const data = await api("backups");
  backups = data;
  $("backup-schedule").textContent = data.schedule;
  $("backup-badge").textContent = data.count;
  $("backup-dir-note").textContent = `保存在服务器 ${data.dir}，可按需下载到本地。`;
  $("backup-empty").hidden = data.count > 0;
  $("backup-body").innerHTML = data.items.map(b => `<tr><td><code>${esc(b.name)}</code>${b.is_last ? ' <span class="pill green">最近一次</span>' : ""}</td><td class="mono muted">${esc(b.created_text)}</td><td class="muted">${esc(b.age_text)}</td><td class="align-right mono">${esc(b.size_text)}</td><td class="align-right"><button class="secondary" data-backup-download="${esc(b.name)}">下载</button> <button class="secondary" data-backup-restore="${esc(b.name)}">恢复</button> <button class="danger" data-backup-delete="${esc(b.name)}">删除</button></td></tr>`).join("");
}
function backupGuard(button, fn) {
  return async () => {
    if (backupBusy) return;
    backupBusy = true; const original = button.textContent; button.disabled = true;
    try { await fn(); } catch (e) { toast(e.message); }
    finally { backupBusy = false; button.disabled = false; button.textContent = original; }
  };
}
$("backup-save").addEventListener("click", backupGuard($("backup-save"), async () => {
  await api("pool/settings", {method:"PATCH", body:{backup_enabled:$("backup-enabled").checked, backup_interval_hours:Number($("backup-interval").value), backup_keep:Number($("backup-keep").value)}});
  await refresh(); await loadBackups(); toast("备份设置已保存");
}));
$("backup-now").addEventListener("click", backupGuard($("backup-now"), async () => {
  $("backup-now").textContent = "备份中…";
  const data = await api("backups", {method:"POST"});
  await refresh(); await loadBackups();
  toast(`已生成备份 ${data.name}`);
}));
$("backup-body").addEventListener("click", async e => {
  const button = e.target.closest("button"); if (!button) return;
  const {backupDownload, backupRestore, backupDelete} = button.dataset;
  if (backupDownload) {
    // 备份接口要求管理会话（带 csrf 的 fetch 下拉不动），先取 blob 再造临时链接触发下载。
    try {
      const response = await fetch(`/admin/api/backups/${encodeURIComponent(backupDownload)}/download`, {credentials:"same-origin"});
      if (!response.ok) throw new Error("下载失败，请重新登录后再试");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a"); link.href = url; link.download = backupDownload;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (err) { toast(err.message); }
    return;
  }
  if (backupDelete) {
    confirmAction("删除备份", `确定删除 ${backupDelete} 吗？删除后无法恢复。`, async () => {
      await api(`backups/${encodeURIComponent(backupDelete)}`, {method:"DELETE"});
      await loadBackups();
    });
    return;
  }
  if (backupRestore) {
    confirmAction("恢复数据库", `将用 ${backupRestore} 替换当前数据库，并自动重启服务。恢复前会先备份当前数据，确定继续？`, async () => {
      await api(`backups/${encodeURIComponent(backupRestore)}/restore`, {method:"POST"});
      // 服务正在重启：页面会短暂失联，提示后轮询等它回来。
      $("confirm-dialog").close();
      toast("已开始恢复，服务正在重启…");
      setTimeout(awaitRestart, 3000);
    });
  }
});
async function awaitRestart() {
  // 恢复成功后进程会在 0.5s 左右退出。这里要能穿过「还活着 → 503 → 断开 → 新进程」
  // 整个过渡期：先等一会儿让旧进程真的退出，再轮询到重新能登录为止。
  //
  // 注意：恢复会丢掉内存里的管理会话（os._exit 清空 sessions），所以旧 cookie
  // 必定失效。api() 拿到 401 会调 showLogin() 把人踢回登录页——这在恢复流程里
  // 是预期结果，不是故障，所以轮询期间不弹错误提示，只在登录页上方说明一句。
  await new Promise(r => setTimeout(r, 2000));
  for (let i = 0; i < 40; i++) {
    try {
      await api("overview");
      await refresh(); await loadBackups(); toast("服务已恢复连接"); return;
    } catch { showRestoreNotice(); }
    await new Promise(r => setTimeout(r, 2000));
  }
  toast("服务仍未恢复，请检查容器日志");
}
// 恢复会杀掉内存里的会话，轮询期间被 401 踢回登录页时补一句说明，
// 免得用户以为是「操作失败」或「莫名被登出」。
function showRestoreNotice() {
  if (!$("login").hidden) $("login-error").textContent = "数据库已恢复，请重新登录。";
}
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
// 登录方式：**由后端注册表的 login_kind 声明驱动**（需求 2）。
//
// 这张表是纯 UI 映射 —— 「哪种登录流程对应哪个面板 DOM」，不含后端名。
// 后端声明 login_kind="trae" 就渲染 Trae 授权面板；将来一个后端声明
// login_kind="qwen" 时，这里加一条映射 + 一个面板即可，**不必改后端逻辑**。
// 「导入文件」是通用兜底，任何后端都能用，永远渲染。
const LOGIN_MODE_PANELS = {browser: "browser-login-panel", trae: "trae-login-panel", import: "file-import-panel"};
const LOGIN_MODE_ICONS = {browser: "↗", trae: "⚡", import: "⇧"};
const LOGIN_MODE_TITLES = {browser: "登录", trae: "授权", import: "导入文件"};
const loginKinds = () => [...new Set([
  ...backends.map(b => b.login_kind).filter(Boolean), "import"])];
// 后端声明了 login_kind 才渲染它的登录按钮 —— 加后端时按钮自动出现。
function renderLoginModes() {
  const box = $("account-modes");
  if (!box) return;
  const kinds = loginKinds();
  box.innerHTML = kinds.map(k => {
    const owner = backends.find(b => b.login_kind === k);
    const label = k === "import" ? "导入文件"
      : `${LOGIN_MODE_ICONS[k] || "◆"} ${backendName(owner?.name)} ${LOGIN_MODE_TITLES[k] || ""}`.trim();
    return `<button id="mode-${esc(k)}" type="button" aria-pressed="${k === accountMode}">${esc(label)}</button>`;
  }).join("");
  for (const k of kinds) {
    const btn = $("mode-" + k);
    if (btn) btn.addEventListener("click", () => setAccountMode(k));
  }
}
function setAccountMode(mode) {
  clearOAuth(); clearTrae(); accountMode = mode;
  for (const [kind, panel] of Object.entries(LOGIN_MODE_PANELS)) {
    const el = $(panel);
    // 「导入文件」是通用兜底，任何后端都能用；其余按 login_kind 对号入座。
    if (el) el.hidden = kind !== mode && !(kind === "import" && mode === "import");
  }
  $("file-import-panel").hidden = $("account-import").hidden = mode !== "import";
  for (const kind of loginKinds()) {
    const btn = $("mode-" + kind);
    if (btn) btn.setAttribute("aria-pressed", String(kind === mode));
  }
  $("account-error").textContent = "";
}
function stopTraeWait() { clearTimeout(traeTimer); clearInterval(traeTick); traeTimer = traeTick = null; }
function setTraeStatus(text, warning) {
  traeBase = text;
  $("trae-status").className = "banner oauth-status" + (warning ? " warning" : "");
  $("trae-status").textContent = text;
}
function renderTraeWait() {
  if (!traeFlow) return 0;
  const left = Math.max(0, Math.round((traeExpiresAt - Date.now()) / 1000));
  $("trae-status").textContent = `${traeBase} · 等待中(${left}s/${traeTotal}s)`;
  return left;
}
function tickTraeWait() {
  clearInterval(traeTick);
  traeTick = setInterval(() => {
    if (!traeFlow) { stopTraeWait(); return; }
    if (renderTraeWait() <= 0) {
      traeFlow = null;
      stopTraeWait();
      setTraeStatus("登录链接已过期，请重新生成。", true);
    }
  }, 1000);
}
function startTraeWait(expiresAtMs) {
  // 用相对剩余时长，避免客户端/服务端时钟偏差；服务端 TTL 600s，异常值回落默认。
  const left = Math.round((expiresAtMs - Date.now()) / 1000);
  traeTotal = left > 0 && left <= 3600 ? left : 600;
  traeExpiresAt = Date.now() + traeTotal * 1000;
  renderTraeWait();
  tickTraeWait();
}
function resumeTraeWait() { if (traeFlow) { renderTraeWait(); tickTraeWait(); } }
function clearTrae() {
  const previous = traeFlow; traeFlow = null; stopTraeWait();
  if (previous && csrf) api("trae/" + previous.id, {method:"DELETE"}).catch(() => {});
  $("trae-link-box").hidden = true; $("trae-open").removeAttribute("href");
  $("trae-start").disabled = false; $("trae-start").textContent = "生成登录链接 ↗";
  $("trae-callback").value = ""; $("trae-submit").disabled = false;
  setTraeStatus("点击上方按钮生成登录链接，10 分钟内有效。");
}
async function pollTrae() {
  if (!traeFlow) return;
  const flow = traeFlow;
  try {
    const result = await api("trae/flow/" + flow.id);
    if (!traeFlow || traeFlow.id !== flow.id) return;
    if (result.status === "done") {
      traeFlow = null; stopTraeWait();
      $("account-dialog").close(); await refresh();
      toast(`Trae 账号已添加${result.nickname ? "：" + result.nickname : ""}`);
      return;
    }
    if (result.status === "error") {
      traeFlow = null; stopTraeWait();
      setTraeStatus(result.message || "登录失败，请重新生成链接", true);
      return;
    }
    if (result.status === "expired") {
      traeFlow = null; stopTraeWait();
      setTraeStatus("登录链接已过期，请重新生成。", true);
      return;
    }
    // 等待中：文案由 1s 倒计时统一渲染，这里只负责继续轮询。
    renderTraeWait();
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
    setTraeStatus("登录链接已就绪。在浏览器用手机号／验证码登录后，浏览器会跳到打不开的 127.0.0.1 地址——复制地址栏的完整链接，粘贴到下方完成绑定。");
    startTraeWait(flow.expires_at);
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
  stopTraeWait();                       // 换取凭据期间暂停轮询与倒计时
  setTraeStatus("正在换取 Trae 凭据…");
  try {
    const result = await api("trae/login", {method:"POST", body:{id:traeFlow.id, callback_url:callback}});
    traeFlow = null; stopTraeWait();
    $("account-dialog").close(); await refresh();
    toast(result.updated ? "Trae 账号凭据已更新" : "Trae 账号已添加");
  } catch (e) {
    $("account-error").textContent = e.message;
    setTraeStatus(e.message, true);
    if (traeFlow) { resumeTraeWait(); traeTimer = setTimeout(pollTrae, 3000); }  // 流程仍有效则恢复
  } finally { $("trae-submit").disabled = false; }
});
$("account-dialog").addEventListener("close", () => { clearOAuth(); clearTrae(); $("account-form").reset(); $("file-label").textContent = "选择或拖入 .info / .json 文件"; $("account-error").textContent = ""; });
function openAccount() {
  // 默认选中「第一个有专属登录流程的后端」，没有就用通用的「导入文件」。
  const first = backends.find(b => b.login_kind);
  setAccountMode(first ? first.login_kind : "import");
  $("account-dialog").showModal();
}
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
  if (accountMode !== "import") { e.preventDefault(); return; }
  e.preventDefault(); e.submitter.disabled = true; $("account-error").textContent = "";
  try {
    let credential; try { credential = JSON.parse($("credential-json").value); } catch { throw new Error("请选择文件或粘贴有效的 JSON 内容"); }
    await api("accounts", {method:"POST", body:{name:$("account-name").value.trim() || undefined, credential}});
    $("account-dialog").close(); await refresh(); toast("账号已导入");
  } catch (err) { $("account-error").textContent = err.message; } finally { e.submitter.disabled = false; }
});
// field 为 null 时不显示输入框（纯确认）；否则 {value, label, type, placeholder}。
// label 由调用点给——原先写死成「账号备注」，设置请求上限时文案是错的。
function confirmAction(title, desc, callback, field = null) {
  $("confirm-title").textContent = title; $("confirm-desc").textContent = desc; $("confirm-error").textContent = "";
  const input = $("rename-value");
  $("rename-label").hidden = input.hidden = field === null;
  input.value = field ? (field.value ?? "") : "";
  $("rename-label").textContent = field ? field.label : "";
  input.type = field && field.type ? field.type : "text";
  if (field && field.min !== undefined) input.min = field.min; else input.removeAttribute("min");
  if (field && field.max !== undefined) input.max = field.max; else input.removeAttribute("max");
  if (field && field.placeholder) input.placeholder = field.placeholder; else input.removeAttribute("placeholder");
  pendingConfirm = callback; $("confirm-dialog").showModal();
}
// token 友好格式：842 / 12K / 3M。口径是流式抓的估算值，不是精确账本。
// 与积分同口径：向下取整，不出现小数（12.3K → 12K）。
function fmtTokens(n) {
  const v = Math.floor(Number(n) || 0);
  if (v < 1000) return String(v);
  if (v < 1000000) {
    // 999950 向下取整会得到 "999K"，读起来比 1M 还别扭；这种边界进位到 M。
    // 注意 M 的换算也走 floor，若不先归一到 1000000，会算出 floor(0.99995)=0 → "0M"。
    if (v >= 999950) return (Math.floor(v / 1000000) || 1) + "M";
    return Math.floor(v / 1000) + "K";
  }
  return Math.floor(v / 1000000) + "M";
}
// 回调自行关闭对话框时（恢复备份会重启服务，不能让这里再 refresh）就跳过收尾。
// 判据用 dialog.open，不引进新标记——回调关掉了就说明它接管了后续流程。
$("confirm-form").addEventListener("submit", async e => { e.preventDefault(); e.submitter.disabled = true; const dialog = $("confirm-dialog"); try { await pendingConfirm(); if (!dialog.open) return; dialog.close(); await refresh(); toast("操作已保存"); } catch (err) { $("confirm-error").textContent = err.message; } finally { e.submitter.disabled = false; } });
$("accounts-body").addEventListener("click", async e => {
  const b = e.target.closest("[data-action]");
  if (!b) {
    // 点行**空白处**打开详情弹框（需求 6）；点操作按钮仍执行操作，不弹框。
    const tr = e.target.closest("tr[data-account]");
    if (tr) openAccountDetail(overview.accounts.find(x => x.id === tr.dataset.account));
    return;
  }
  const a = overview.accounts.find(x => x.id === b.dataset.id); if (!a) return;
  if (["status","checkin","refresh"].includes(b.dataset.action)) {
    b.disabled = true;
    try { const result=await api(`accounts/${a.id}/actions/${b.dataset.action}`,{method:"POST"});toast(result.message);await refresh(); }
    catch(err){toast(err.message);} finally {b.disabled=false;} return;
  }
  const update = body => api("accounts/" + a.id, {method:"PATCH", body});
  if (b.dataset.action === "rename") confirmAction("编辑账号备注", "备注仅用于在管理后台识别账号。", () => update({name:$("rename-value").value}), {value:a.name, label:"账号备注"});
  if (b.dataset.action === "weight") confirmAction("设置调度权重", "权重 0-100，越大越优先分配请求；同权重时优先消耗积分最早过期的账号。", () => update({weight:Number($("rename-value").value)}), {value:String(a.weight || 0), label:"调度权重（0-100）", type:"number", min:0, max:100});
  if (b.dataset.action === "toggle") confirmAction(a.enabled ? "暂停这个账号？" : "恢复这个账号？", "暂停后不参与新请求分配和自动签到，已开始的请求继续完成。恢复后重新参与轮转。", () => update({enabled:!a.enabled}));
  if (b.dataset.action === "delete") confirmAction("删除这个账号？", `「${a.name}」将从账号列表移除。服务器保留恢复副本。`, () => api("accounts/" + a.id, {method:"DELETE"}));
});
// 表头排序：点击同一列在 升 → 降 → 取消 之间循环。
document.querySelectorAll("[data-sort]").forEach(th => th.addEventListener("click", () => {
  const key = th.dataset.sort;
  keysSort = keysSort.key === key
    ? {key, dir: keysSort.dir === 1 ? -1 : (keysSort.dir === -1 ? 0 : 1)}
    : {key, dir: 1};
  render();
}));
$("keys-filter-mode").addEventListener("change", render);

$("keys-body").addEventListener("click", e => {
  // 分配策略做成「图标 + 短名」的 chip，点击进设置弹框统一编辑（与「设置」按钮同一个弹框）。
  const chip = e.target.closest("[data-routing-edit]");
  if (chip) { openKeyBind(overview.keys.find(x => x.id === chip.dataset.routingEdit)); return; }
  const bind = e.target.closest("[data-bind]");
  if (bind) { openKeyBind(overview.keys.find(x => x.id === bind.dataset.bind)); return; }
  const limit = e.target.closest("[data-limit]");
  if (limit) { const k = overview.keys.find(x => x.id === limit.dataset.limit); confirmAction("设置每日请求上限", "该密钥每天最多可发起的请求数。留空或填 0 表示不限制。超限后新请求返回 429，次日北京时间 00:00 自动重置。只统计进入转发的对话接口，成功与失败都计入；被限流、被模型白名单拒绝的不计入。", () => api("keys/" + k.id, {method:"PATCH", body:{daily_limit:Number($("rename-value").value) || null}}), {value:k.daily_limit ?? "", label:"每日请求上限（次）", type:"number", min:0, placeholder:"留空或 0 = 不限制"}); return; }
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
  // 批量开关：一次作用于同名模型的**全部后端**（需求 8 的「批量禁用、启用」）。
  // 单后端开关仍保留在行内小字里，便于只停其中一条链路。
  const batch = e.target.closest("[data-model-group-toggle]");
  if (batch) {
    const key = batch.dataset.modelGroupToggle;
    const enabled = batch.dataset.enabled === "1";
    // 与渲染同一处判断（modelsInGroup），避免「显示两组、只切一组」。
    const list = modelsInGroup(overview?.models?.models, key);
    const targets = list.filter(m => !!m.enabled !== enabled);
    if (!targets.length) return;
    batch.disabled = true;
    const verb = enabled ? "启用" : "禁用";
    let done = 0;
    try {
      // 逐个串行：任一失败要能定位到具体模型，已成功的保持生效（PATCH 幂等，可重试）。
      for (const m of targets) {
        await api("models/" + encodeURIComponent(m.id), {method:"PATCH", body:{enabled}});
        done += 1;
      }
      await refresh();
      toast(`已${verb}「${key}」的 ${targets.length} 个后端条目`);
    } catch (err) {
      // 报出「成了几个/共几个」：只说 err.message 会让人以为一个都没动。
      await refresh();
      toast(`「${key}」已${verb} ${done}/${targets.length} 个，失败：${err.message}`);
    }
    return;
  }
  const toggle = e.target.closest("[data-model-toggle]");
  if (toggle) {
    toggle.disabled = true;
    try { await api("models/" + encodeURIComponent(toggle.dataset.modelToggle), {method:"PATCH", body:{enabled:toggle.dataset.enabled === "1"}}); await refresh(); }
    catch (err) { toast(err.message); toggle.disabled = false; }
    return;
  }
  const del = e.target.closest("[data-model-delete]");
  if (del) {
    const mid = del.dataset.modelDelete;
    if (!confirm(`删除自定义模型 ${mid}？`)) return;
    del.disabled = true;
    try { await api("models/" + encodeURIComponent(mid), {method:"DELETE"}); await refresh(); toast(`已删除 ${mid}`); }
    catch (err) { toast(err.message); del.disabled = false; }
  }
});
// -- 添加模型 -------------------------------------------------------------
// 归属多选（specs/model-backend-attribution.md §4.3）：选项来自注册表
// （/admin/api/backends），加后端自动多一项，前端不登记任何后端名。
// 提交语义是「前端展开调 N 次」——后端 ModelRegistry.add 的语义不变，
// 部分失败逐个报告（Trae 侧 400 不影响 WorkBuddy 落盘）。
function renderBackendPicker() {
  const box = $("model-backends");
  const list = $("model-backend-options");
  if (!box || !list) return;
  // 只有多于一个后端时才需要选：单后端时归属别无选择，藏起来减少噪音。
  if (backends.length < 2) { box.hidden = true; list.innerHTML = ""; return; }
  box.hidden = false;
  list.innerHTML = backends.map(b => `<label class="model-backend-option"><input type="checkbox" data-model-backend="${esc(b.name)}"${b.enabled === false ? " disabled" : ""}${b.enabled === false ? "" : " checked"}> ${backendIconSvg(b.name)} ${esc(b.label || b.name)}</label>`).join("");
}
$("models-add").addEventListener("click", () => {
  $("model-form").reset();
  $("model-enabled").checked = true;
  $("model-error").textContent = "";
  renderBackendPicker();
  $("model-dialog").showModal();
});
// 带前缀的 ID 已显式指定归属 → 收敛多选框为仅那一个后端（§4.3）。
$("model-id").addEventListener("input", () => {
  const value = $("model-id").value.trim();
  const named = backends.find(b => b.prefix && value.startsWith(b.prefix));
  $("model-backend-options").querySelectorAll("[data-model-backend]").forEach(input => {
    if (!named) {
      input.disabled = backends.find(x => x.name === input.dataset.modelBackend)?.enabled === false;
      return;
    }
    const isTarget = input.dataset.modelBackend === named.name;
    input.checked = isTarget;
    input.disabled = !isTarget;
  });
});
$("model-form").addEventListener("submit", async e => {
  e.preventDefault();
  const error = $("model-error");
  error.textContent = "";
  const id = $("model-id").value.trim();
  // 前端先挡一道，但后端才是权威（ID 要进 URL 路径与 JSON）。
  if (!/^[A-Za-z0-9._:/-]+$/.test(id)) {
    error.textContent = "模型 ID 只能包含字母、数字与 . _ : / -";
    return;
  }
  const num = el => { const v = el.value.trim(); return v === "" ? null : Number(v); };
  const fields = {
    name: $("model-name").value.trim() || null,
    description: $("model-desc").value.trim() || null,
    credits: num($("model-credits")),
    max_input_tokens: num($("model-ctx")),
    enabled: $("model-enabled").checked,
  };
  // 带前缀的 ID 自带归属（后端按前缀判定），除此之外按多选框展开。
  // 前缀拼接规则来自 backends[].prefix，不是手写 "trae/"。
  const explicit = backends.find(b => b.prefix && id.startsWith(b.prefix));
  const chosen = explicit
    ? [explicit]
    : backends.filter(b => b.enabled !== false
        && $("model-backend-options")?.querySelector(`[data-model-backend="${b.name}"]`)?.checked);
  const targets = chosen.length ? chosen : [backends[0]];
  const button = $("model-submit");
  button.disabled = true;
  const failed = [];
  let saved = 0;
  try {
    for (const backend of targets) {
      // 默认后端前缀为空串，直接用裸名；其它后端带自己的前缀。
      const targetId = explicit ? id : (backend.prefix || "") + id;
      try {
        await api("models", {method: "POST", body: {id: targetId, ...fields}});
        saved += 1;
      } catch (err) {
        failed.push(`${backend.label || backend.name}：${err.message}`);
      }
    }
    if (saved) {
      $("model-dialog").close();
      await refresh();
    }
    if (failed.length) {
      // 部分成功必须如实报告（Trae 上游没这个模型时后端会 400）。
      const message = `已保存 ${saved} 个；${failed.join("；")}`;
      if (saved) toast(message); else error.textContent = message;
    } else {
      toast(`已保存 ${targets.length} 个后端：${id}`);
    }
  } finally {
    button.disabled = false;
  }
});

$("test-form").addEventListener("submit", async e => {
  e.preventDefault(); busy = true; $("test-submit").disabled = true; $("test-submit").textContent = "正在调用…"; $("test-status").className = "pill amber"; $("test-status").textContent = "请求中"; $("test-output").textContent = "正在等待上游响应，最长约 90 秒…"; $("test-meta").textContent = "";
  try { const r = await api("test", {method:"POST",body:{model:$("model").value,prompt:$("test-prompt").value}}); $("test-status").className = "pill " + (r.ok ? "green" : "red"); $("test-status").textContent = r.ok ? "连接成功" : "调用失败"; $("test-output").textContent = r.ok ? r.answer : r.error; $("test-meta").textContent = `${r.seconds}s${r.status ? " · HTTP " + r.status : ""}${r.usage?.total_tokens !== undefined ? " · " + r.usage.total_tokens + " tokens" : ""}`; }
  catch (err) { $("test-status").className = "pill red"; $("test-status").textContent = "请求失败"; $("test-output").textContent = err.message; }
  finally { busy = false; $("test-submit").textContent = "发送测试 ↗"; await refresh().catch(() => {}); }
});
/* ---------- 用量明细页 ---------- */
let usageOffset = 0, usageTotal = 0, usageSyncCooldown = 0;
const usageStart = () => $("usage-start").value, usageEnd = () => $("usage-end").value;
// datetime-local 给的是「无时区的墙上时间」，直接 new Date(v) 会按**浏览器本地
// 时区**解析 —— 非 +08:00 的用户会把区间整体错开，报表按日分桶就偏一整个时区。
// 所以显式钉上 +08:00，让「输入框里填的时间 = 北京时间」在任何时区下都成立。
const usageUnix = v => {
  if (!v) return null;
  const fixed = v.length === 16 ? v + ":00+08:00" : v + "+08:00";
  const ts = new Date(fixed).getTime();
  return Number.isFinite(ts) ? Math.floor(ts / 1000) : null;
};
function usageQuery(extra = {}) {
  const q = new URLSearchParams(extra);
  const a = usageUnix(usageStart()), b = usageUnix(usageEnd());
  if (a) q.set("start", a); if (b) q.set("end", b);
  const acc = $("usage-account").value; if (acc) q.set("account_id", acc);
  return q.toString();
}
function accountName(id) {
  // 报表按账号分组用的是 account_id（改名不拆组），这里再映射回可读名字。
  const hit = (overview && overview.accounts || []).find(a => a.id === id);
  return hit && hit.name ? hit.name : id;
}
let usageInitialized = false;
function fillUsageAccounts() {
  // 账号下拉独立出来：deep-link 进 #usage 时 overview 还是 null，
  // 必须等 refresh() 之后再补一次，否则下拉永远是空的。
  const keep = $("usage-account").value;
  $("usage-account").innerHTML = '<option value="">全部账号</option>' +
    (overview && overview.accounts ? overview.accounts : [])
      .map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
  $("usage-account").value = keep;
}
function initUsagePage() {
  // 用显式标志位而不是「输入框有没有值」：后者在 deep-link 时会永久跳过填充。
  if (usageInitialized) return;
  usageInitialized = true;
  const pad = n => String(n).padStart(2, "0");
  const fmt = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const now = new Date(), past = new Date(now.getTime() - 30 * 86400000);
  $("usage-start").value = fmt(past); $("usage-end").value = fmt(now);
  fillUsageAccounts();
}
function renderUsageSummary(report) {
  const t = report.total || {};
  const card = (label, value, note) => `<article><span>${label}</span><strong>${value}</strong><small>${note}</small></article>`;
  $("usage-summary").innerHTML =
    card("区间积分消耗", creditsFmt(t.credit ?? 0), "来自上游账单") +
    card("记录条数", (t.count ?? 0).toLocaleString("zh-CN"), `免费 ${t.free ?? 0} · 付费 ${t.paid ?? 0}`) +
    card("付费条均", t.paid ? creditsFmt((t.credit ?? 0) / t.paid) : "—", "不含免费调用") +
    card("维度", {day:"按日", account:"按账号", model:"按模型", client:"按客户端"}[report.group_by] || report.group_by, `共 ${report.bucket_total ?? (report.groups || []).length} 组`);
}
function renderUsageGroups(report) {
  // 报表分组直接画在明细表上方：用户切维度时想看的就是这个。
  const host = $("usage-groups");
  if (!host) return;
  const all = report.groups || [];
  const shown = all.slice(0, 20);
  const note = $("usage-groups-note");
  if (note) {
    note.textContent = shown.length < all.length
      ? `显示前 ${shown.length} 组（共 ${all.length} 组）`
      : (report.truncated ? `服务端最多返回 ${all.length} 组，实际共 ${report.bucket_total} 组`
                          : (all.length ? `共 ${all.length} 组` : ""));
  }
  const label = b => (report.group_by === "account" ? accountName(b) : b);
  host.innerHTML = shown.map(g =>
    `<tr><td>${esc(label(g.bucket))}</td><td class="align-right mono">${g.count}</td>`
    + `<td class="align-right mono">${creditsFmt(g.credit ?? 0)}</td>`
    + `<td class="align-right mono muted">${g.free} / ${g.paid}</td></tr>`).join("")
    || '<tr><td colspan="4" class="history-empty">该区间没有记录。</td></tr>';
}
async function loadUsage(offset, withReport = true) {
  const size = Number($("usage-page-size").value) || 100;
  try {
    const data = await api("usage?" + usageQuery({limit: size, offset}));
    usageOffset = data.offset; usageTotal = data.total;
    $("usage-body").innerHTML = data.rows.map(r => `<tr><td class="mono muted">${esc(stamp(r.request_time * 1000))}</td>`
      + `<td>${esc(r.account_name || r.account_id || "—")}</td><td class="mono">${esc(r.model || "—")}</td>`
      + `<td>${esc(r.client || "—")}</td><td class="align-right mono">${creditsFmt(r.credit ?? 0)}</td>`
      + `<td class="muted">${esc(r.agent_purpose || "")}</td></tr>`).join("")
      || '<tr><td colspan="6" class="history-empty">该区间没有记录。可点击「拉取上游」同步。</td></tr>';
    const pages = Math.max(1, Math.ceil(usageTotal / size));
    $("usage-page-info").textContent = usageTotal ? `第 ${Math.floor(data.offset / size) + 1} / ${pages} 页 · 共 ${usageTotal.toLocaleString("zh-CN")} 条` : "暂无记录";
    $("usage-prev").disabled = data.offset <= 0;
    $("usage-next").disabled = data.offset + size >= usageTotal;
    if (withReport) {
      const report = await api("usage/report?" + usageQuery({group_by: $("usage-group").value}));
      renderUsageSummary(report); renderUsageGroups(report);
    }
  } catch (e) {
    $("usage-body").innerHTML = `<tr><td colspan="6" class="history-empty">${esc(e.message)}</td></tr>`;
    // 失败时把分页按钮恢复到可翻页状态：否则会保留上一次的 disabled，
    // 用户可能卡在既不能前进也不能后退的一页里。
    usageTotal = 0; $("usage-page-info").textContent = "加载失败";
    $("usage-prev").disabled = usageOffset <= 0; $("usage-next").disabled = true;
  }
}
// 防抖第 1 层：按钮禁用 + 返回后 3 秒冷却。
async function syncUsage() {
  const now = Date.now();
  if (now < usageSyncCooldown) { toast(`请 ${Math.ceil((usageSyncCooldown - now) / 1000)} 秒后再试`); return; }
  const btn = $("usage-sync");
  btn.disabled = true; const label = btn.textContent; btn.textContent = "拉取中…";
  try {
    const r = await api("usage/sync", {method: "POST", body: {
      start: usageUnix(usageStart()), end: usageUnix(usageEnd()),
      account_id: $("usage-account").value || null}});
    const skipped = (r.skipped || []).length;
    toast(skipped ? `已同步 ${r.synced} 个账号，跳过 ${skipped} 个（${r.message || "刚拉取过或任务进行中"}）`
                  : `已同步 ${r.synced} 个账号`);
    await loadUsage(0);
  } catch (e) {
    toast(e.message);
    // 失败不设冷却：网络错或 500 之后用户马上重试是合理诉求，惩罚它会让人
    // 以为拉取其实成功了。冷却只用来防「成功之后的连点」。
    btn.textContent = label; btn.disabled = false;
    return;
  }
  btn.textContent = label; btn.disabled = false;
  usageSyncCooldown = Date.now() + 3000;
}
$("usage-sync").addEventListener("click", syncUsage);
$("usage-refresh").addEventListener("click", () => loadUsage(usageOffset));
$("usage-prev").addEventListener("click", () => loadUsage(Math.max(0, usageOffset - (Number($("usage-page-size").value) || 100))));
$("usage-next").addEventListener("click", () => loadUsage(usageOffset + (Number($("usage-page-size").value) || 100)));
$("usage-page-size").addEventListener("change", () => loadUsage(0));
$("usage-group").addEventListener("change", () => loadUsage(usageOffset));
$("usage-account").addEventListener("change", () => loadUsage(0));
$("usage-filter").addEventListener("submit", e => { e.preventDefault(); loadUsage(0); });

/* ---------- 单个密钥的请求记录 ---------- */
let keyReqId = null, keyReqOffset = 0, keyReqSize = 10;
function renderKeyRequests(rows) {
  $("key-requests-body").innerHTML = rows.map(r =>
    `<tr><td class="mono muted">${esc(stamp(r.time * 1000))}</td>`
    + `<td>${backendBadge(r.backend)}</td><td class="mono">${esc(r.model_id || "—")}${r.account_name ? `<small class="cell-note">${esc(r.account_name)}</small>` : ""}</td>`
    + `<td><span class="pill ${r.ok ? 'green' : 'red'}">${r.status ?? '—'}</span><small class="cell-note">${outcomes[r.outcome] || '请求失败'}</small></td>`
    + `<td class="align-right mono">${Math.round(Number(r.duration_ms) || 0).toLocaleString("zh-CN")} ms</td></tr>`).join("")
    || '<tr><td colspan="5" class="history-empty">该密钥还没有请求记录。</td></tr>';
}
async function loadKeyRequests(offset) {
  if (!keyReqId) return;
  keyReqOffset = Math.max(0, offset);
  // 记下这次请求属于哪个密钥/哪一页：快速在密钥间点击时，先发的迟到响应
  // 会覆盖后发的列表，导致「显示的记录不属于当前密钥」。
  const requestedId = keyReqId, requestedOffset = keyReqOffset;
  $("key-requests-body").innerHTML = '<tr><td colspan="5" class="history-empty">加载中…</td></tr>';
  try {
    const data = await api(`logs?limit=${keyReqSize}&offset=${keyReqOffset}&key_id=${encodeURIComponent(keyReqId)}`);
    if (requestedId !== keyReqId || requestedOffset !== keyReqOffset) return;  // 已被更新的请求取代
    renderKeyRequests(data.rows);
    const pages = Math.max(1, Math.ceil(data.total / keyReqSize));
    const current = Math.floor(data.offset / keyReqSize) + 1;
    $("key-requests-info").textContent = data.total ? `第 ${current} / ${pages} 页 · 共 ${data.total.toLocaleString("zh-CN")} 条` : "暂无记录";
    $("key-requests-prev").disabled = data.offset <= 0;
    $("key-requests-next").disabled = data.offset + keyReqSize >= data.total;
  } catch (err) {
    if (requestedId !== keyReqId) return;
    $("key-requests-body").innerHTML = `<tr><td colspan="5" class="history-empty">${esc(err.message)}</td></tr>`;
  }
}
$("key-requests-prev").addEventListener("click", () => loadKeyRequests(keyReqOffset - keyReqSize));
$("key-requests-next").addEventListener("click", () => loadKeyRequests(keyReqOffset + keyReqSize));

/* ---------- 密钥用量历史 ---------- */
$("keys-body").addEventListener("click", async e => {
  const reqCell = e.target.closest("[data-requests]");
  if (reqCell) {
    keyReqId = reqCell.dataset.requests;
    const k = overview.keys.find(x => x.id === keyReqId);
    $("key-requests-name").textContent = `密钥「${k ? k.name : keyReqId}」`;
    $("key-requests-dialog").showModal();
    await loadKeyRequests(0);
    return;
  }
  const cell = e.target.closest("[data-usage]");
  if (!cell) return;
  const kid = cell.dataset.usage;
  const k = overview.keys.find(x => x.id === kid);
  $("usage-history-name").textContent = `密钥「${k ? k.name : kid}」`;
  $("usage-history-body").innerHTML = '<tr><td colspan="3" class="history-empty">加载中…</td></tr>';
  $("usage-history-dialog").showModal();
  try {
    const data = await api("keys/usage?days=30");
    const mine = data.keys.find(x => x.id === kid);
    const series = mine ? mine.series : [];
    const sum = series.reduce((acc, d) => ({count: acc.count + d.count, tokens: acc.tokens + d.tokens}), {count: 0, tokens: 0});
    $("usage-history-body").innerHTML = series.map(d =>
      `<tr><td class="mono muted">${esc(d.day)}</td><td class="align-right mono">${d.count}</td>`
      + `<td class="align-right mono">${fmtTokens(d.tokens)}</td></tr>`).join("")
      || '<tr><td colspan="3" class="history-empty">近 30 天还没有记录。</td></tr>';
    $("usage-history-total").textContent = series.length
      ? `区间合计：${sum.count.toLocaleString("zh-CN")} 次 · ${fmtTokens(sum.tokens)} tokens（共 ${series.length} 天有记录）`
      : "";
  } catch (err) { $("usage-history-body").innerHTML = `<tr><td colspan="3" class="history-empty">${esc(err.message)}</td></tr>`; }
});

(async () => { loadVersion(); try { const s = await api("session"); csrf = s.csrf; await enter(); } catch (e) { if (!csrf) showLogin(); } })();
