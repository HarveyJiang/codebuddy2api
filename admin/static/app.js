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
  logs: ["请求日志", "REQUEST LOG", "服务处理过的每一次请求，按时间倒序，支持翻页与按密钥追踪。"],
  models: ["模型管理", "MODEL CATALOG", "与官方客户端一致的模型列表，可按需启用或禁用。"],
  test: ["连接测试", "CONNECTION LAB", "经账号池按路由规则发起请求，确认模型能否正常响应。"],
  usage: ["用量明细", "USAGE LEDGER", "查看上游账单级的积分消耗，默认只读本地库，点击「拉取上游」才请求上游。"],
  settings: ["设置", "WORKSPACE SETTINGS", "签到、排队容错、默认分配与数据库备份等全局设置。"],
  guide: ["接入指南", "GET CONNECTED", "从导入凭据到客户端接入，只需几步。"]
};
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const stamp = ts => ts ? new Date(ts).toLocaleString("zh-CN", {hour12:false}) : "未提供";
// 账号的统一显示名 = 备注名（用户 2026-10-17：全站「上面的大标题」都用它，
// 全局唯一）。上游昵称只作建档默认值，账号列表/详情/设置/日志四个位置都
// 走这个函数，保证同一账号在任何弹框里叫同一个名字。
const accountLabel = a => (a && a.name) || "账号";
// 重名占用者：exceptId 是正在编辑/导入的账号自己（改自己名字不算占用）。
// 返回占用者的显示名，查不到返回空串。overview 可能还没加载（深链进来），
// 用可选链兜底，避免「没法预检」把整个保存流程带崩。
const nameOwner = (name, exceptId) => {
  if (!name) return "";
  const hit = (overview?.accounts || []).find(x => x.name === name && x.id !== exceptId);
  return hit ? accountLabel(hit) : "";
};
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
  if (next === "logs") loadLogs(logOffset);
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
    // 范围列做成可点击（用户拍板：多条路径进同一个选择弹框）—— 点「N 个模型」
    // /「N 个账号」直接打开该密钥的设置弹框，与「设置」按钮殊途同归。
    // 「全部」也可点（用户拍板）：点击同样进设置弹框 —— 想从「全部」改成
    // 指定范围是同一类操作，没有理由只有非空范围才可点。
    const scope = (list, noun, kid, kind) => (Array.isArray(list) && list.length)
      ? `<button class="pill amber scope-pill" data-scope="${esc(kind)}" data-scope-kid="${esc(kid)}" title="点击勾选可用${noun}">${list.length} 个${noun}</button>`
      : `<button class="scope-pill scope-all" data-scope="${esc(kind)}" data-scope-kid="${esc(kid)}" title="当前不限制，点击勾选可用${noun}">全部</button>`;
    // 行尾操作统一小号按钮规格 .btn-ghost（用户 2026-10-17：全站按钮风格统一，
    // 表格行尾、账号池、备份行共用一套小号白底细边按钮）。
    // 操作收敛（用户 2026-10-16）：停用/撤销/限额都进「设置」弹框 —— 撤销与
    // 停用语义上不同（撤销不可逆、停用可恢复），但都是低频操作，占表格按钮位
    // 冗余；表格只留「设置」「请求」两个高频入口。
    const actions = `<td class="align-right"><div class="actions">${revoked ? `<button class="btn-ghost" data-requests="${k.id}">请求</button>`
      : `<button class="btn-ghost" data-bind="${k.id}" title="可用模型、可用账号、分配策略、限额与有效期">设置</button><button class="btn-ghost" data-requests="${k.id}" title="查看该密钥最近的请求记录">请求</button>`}</div></td>`;
    return `<tr><td><strong>${esc(k.name)}</strong>${k.expires_at ? `<small class="cell-note">${expired ? "已于" : "至"} ${esc(stamp(k.expires_at * 1000))} ${esc(expired ? "过期" : "到期")}</small>` : ""}</td><td><code>${esc(k.hint)}</code><small class="cell-note">最后使用：${k.last_used_at ? esc(stamp(k.last_used_at * 1000)) : "从未"}</small></td><td class="usage-cell" data-usage="${k.id}" title="点击查看近 30 天趋势"><span class="pill ${warn}">${u.count || 0}${limited ? ` / ${k.daily_limit}` : ""}</span>${warn ? `<small class="cell-note">${ratio >= 1 ? "已达上限，新请求返回 429" : "接近每日上限"}</small>` : `<small class="cell-note">${u.tokens ? fmtTokens(u.tokens) + " tokens · " : ""}${esc(u.date || "")}</small>`}</td><td>${scope(k.models, "模型", k.id, "models")}</td><td>${scope(k.accounts, "账号", k.id, "accounts")}</td><td>${revoked ? "—" : `<button class="routing-chip" data-routing-edit="${k.id}" title="该密钥的账号分配策略，点击修改">${routingIcon(routing)} ${routingShort(routing)}${routing ? "" : `<span class="muted">（${esc(routingShort(overview.pool?.default_routing || "round_robin"))}）</span>`}</button>`}</td><td><span class="pill ${state[1]}">${state[0]}</span></td>${actions}</tr>`;
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

// 请求行里的后端徽标；backend 取自**实际选中的账号**，老记录/账号已删显示「—」。
function backendBadge(b) {
  if (!b) return '<span class="muted">—</span>';
  return `<span class="mini-backend mini-${esc(b)}" title="该请求由 ${esc(b)} 账号处理">${esc(backendLabel(b))}</span>`;
}
function renderModels() {
  const catalog = overview?.models;
  const list = (Array.isArray(catalog?.models) ? catalog.models : [])
    // 已禁用的后端不参与展示：它的页签整个不出现（用户拍板「关掉一个上游，
    // 它的页签直接隐藏」）。放在这里而不是只过滤页签，是为了让**计数**（页签上
    // 的 N/M 与标题角的 badge）与可见内容一致 —— 否则会数出看不见的行。
    // 判据是后端的 `enabled`（总闸 ∧ 逃生门），不是「有没有模型行」：
    // 一个被关闭但目录里仍有模型的后端，页签必须消失而不是显示成空页签。
    .filter(m => backendIsVisible(m.backend));
  const note = $("models-note");
  if (catalog?.error) { note.textContent = `上游拉取异常：${catalog.error}（当前展示${catalog.updated ? "上次缓存" : "内置官方列表"}）`; note.classList.add("field-error"); }
  else { note.textContent = catalog?.updated ? `上次拉取：${stamp(catalog.updated * 1000)} · 每 6 小时自动刷新。按上游分页查看，各上游独立管理。禁用的模型不会出现在 /v1/models。` : "尚未从上游拉取过，展示内置官方列表。服务启动与每 6 小时会自动尝试拉取。"; note.classList.remove("field-error"); }

  // 页签 = 可见后端，**不写死后端名**（读 /admin/api/backends，顺序也来自它）。
  const tabs = visibleBackends();
  // 当前页签可能因为「这个上游刚被关掉」而失效 —— 自动切到第一个可见页签
  // （用户拍板）。用 find 而不是保留旧值：旧值指向的后端已不在 tabs 里了。
  if (!tabs.some(b => b.name === modelsTab)) modelsTab = tabs[0]?.name || "";
  paintModelsTabs(tabs, list);

  const rows = list.filter(m => m.backend === modelsTab);
  $("models-badge").textContent = rows.filter(m => m.enabled).length + " / " + rows.length;
  $("models-body").innerHTML = rows.map(renderModelRow).join("")
    || `<tr><td colspan="7" class="muted">${tabs.length ? "该上游暂无模型。" : "没有启用的上游。"}</td></tr>`;
  $("models-check-all").checked = false;
  paintBatchButtons(rows);
}

// 批量启用/禁用（用户 2026-10-16 拍板：操作列取消后，批量开关挪到工具栏；
// 随后补充：批量只作用于**勾选的行**，没有勾选时按钮置灰）。
// 勾选是逐行的（复选框在每行行首），作用范围天然被页签框住 —— 换页签会
// 重画表格、勾选清空，跨页签的批量操作没有语义。
// 按钮上有多少可切换的目标就显示成什么样：目标为零（没勾 / 勾的全已是
// 目标状态）时置灰，而不是点了之后 toast 一句「无事可做」。
function batchTargets(rows, enabled) {
  return checkedModelRows(rows).filter(m => !!m.enabled !== enabled);
}

function checkedModelRows(rows) {
  // 无 DOM 环境（测试抽函数裸跑）没有 document：按「全不勾」处理，返回空。
  if (typeof document === "undefined") return [];
  const byKey = new Map(rows.map(m => [modelKey(m), m]));
  const out = [];
  for (const box of document.querySelectorAll(".model-check:checked")) {
    const m = byKey.get(box.dataset.modelKey);
    if (m) out.push(m);
  }
  return out;
}

function paintBatchButtons(rows) {
  for (const [id, enabled] of [["models-batch-enable", true], ["models-batch-disable", false]]) {
    const btn = $(id);
    if (!btn) continue;
    const n = batchTargets(rows, enabled).length;
    btn.disabled = n === 0;
    btn.title = n === 0 ? "先勾选要操作的模型行"
      : `${enabled ? "启用" : "禁用"}勾选的 ${n} 个模型`;
  }
}

async function batchToggle(enabled) {
  const catalog = overview?.models;
  const rows = (Array.isArray(catalog?.models) ? catalog.models : [])
    .filter(m => backendIsVisible(m.backend) && m.backend === modelsTab);
  const targets = batchTargets(rows, enabled);
  if (!targets.length) return;
  const verb = enabled ? "启用" : "禁用";
  if (!confirm(`确定${verb}勾选的 ${targets.length} 个模型？`)) return;
  const btn = $(enabled ? "models-batch-enable" : "models-batch-disable");
  btn.disabled = true;
  let done = 0;
  try {
    // 逐个串行（PATCH 幂等，可重试）：任一失败要能定位，已成功的保持生效。
    for (const m of targets) {
      await api("models/" + encodeURIComponent(m.id), {method:"PATCH", body:{enabled, backend: m.backend}});
      done += 1;
    }
    await refresh();
    toast(`已${verb} ${done} 个模型`);
  } catch (err) {
    // 报出「成了几个/共几个」：只说 err.message 会让人以为一个都没动。
    await refresh();
    toast(`已${verb} ${done}/${targets.length} 个，失败：${err.message}`);
  } finally {
    // refresh() 之后 renderModels 会重算按钮态；这里兜底防 refresh 失败时按钮卡死。
    btn.disabled = false;
  }
}
$("models-batch-enable").addEventListener("click", () => batchToggle(true));
$("models-batch-disable").addEventListener("click", () => batchToggle(false));

// 勾选变化 → 重算批量按钮态（事件委托在表格上，复选框是重画出来的）。
// 全选框 = 勾/消当前页签渲染出来的所有行；行勾选反过来同步它的三态。
$("models-check-all").addEventListener("change", e => {
  const on = e.target.checked;
  document.querySelectorAll(".model-check").forEach(b => { b.checked = on; });
  e.target.indeterminate = false;
  const catalog = overview?.models;
  const rows = (Array.isArray(catalog?.models) ? catalog.models : [])
    .filter(m => backendIsVisible(m.backend) && m.backend === modelsTab);
  paintBatchButtons(rows);
});
$("models-body").addEventListener("change", e => {
  if (!e.target.classList.contains("model-check")) return;
  const boxes = [...document.querySelectorAll(".model-check")];
  const all = $("models-check-all");
  all.checked = boxes.length > 0 && boxes.every(b => b.checked);
  all.indeterminate = !all.checked && boxes.some(b => b.checked);
  const catalog = overview?.models;
  const rows = (Array.isArray(catalog?.models) ? catalog.models : [])
    .filter(m => backendIsVisible(m.backend) && m.backend === modelsTab);
  paintBatchButtons(rows);
});

// 幂等去重用的键：联合主键是 (model_id, backend)，但**渲染前还要再防一手**。
// 第 3/4 步已把读库改成按行取（`load_model_rows`），同名两行不会再互相覆盖；
// 这里加一层是因为前端拿的是 JSON 数组，服务端若哪天回退成按 id 索引，重复行会
// 静默变成「少一行」——那种故障只会表现为「模型不见了」，最难查。
// 行键统一走下面的 modelKey（含 backend，空格分隔）。

// 倍率/上下文的统一格式化。倍率保留后端给的精度（0.06 / 0.79 这种不能取整，
// 否则会丢失免费与付费的区别）。0 是「免费」、null 是「按标准计费」，两者必须区分。
const creditsText = m => !Number.isFinite(m.credits) ? "标准" : m.credits === 0 ? "免费" : "×" + m.credits;
const ctxText = m => m.max_input_tokens ? (m.max_input_tokens >= 1000 ? Math.round(m.max_input_tokens / 1000) + "K" : String(m.max_input_tokens)) : "—";

// 当前选中的上游页签。空串表示「还没选过」（首次渲染时由 renderModels 填）。
let modelsTab = "";

// 某个上游此刻该不该出现在页面上（页签是否显示、它的行是否参与统计）。
//
// 判据是**后端自己的 enabled**（总闸 ∧ 环境逃生门），不是「有没有模型行」：
// - 关掉的上游 → 页签直接隐藏，它的模型也不显示（它们这时也不会出现在
//   /v1/models，两边一致）；
// - 拿不到元数据（老接口 / 后端被删）→ **默认可见**。宁可多显示一个页签，
//   也不要让整个模型页因为缺一份元数据而空白 —— 那会被当成「模型全丢了」。
function backendIsVisible(name) {
  const meta = backendMeta(name);
  if (!meta) return true;
  return meta.enabled !== false;
}

// 可见后端，按元数据的展示顺序（order）—— 页签顺序与设置页、账号表一致。
function visibleBackends() {
  return backends.filter(b => b.enabled !== false);
}

// 画页签条：每个可见后端一个，**无「全部」页签、不做合并**（用户拍板）。
// 文案用后端自己的 label（不写死），图标也用后端声明的（`icon_svg`）。
function paintModelsTabs(tabs, list) {
  const box = $("models-tabs");
  if (!box) return;
  if (!tabs.length) { box.innerHTML = ""; box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = tabs.map(b => {
    const sel = b.name === modelsTab;
    // 每个页签带自己的「启用数 / 总数」：一眼看出哪个上游还有东西没开。
    // 没有模型的后端显示 0 —— 页签仍要在（关掉的是后端才隐藏，不是空目录）。
    const rows = list.filter(m => m.backend === b.name);
    const on = rows.filter(m => m.enabled).length;
    return `<button class="model-tab${sel ? " selected" : ""}" role="tab"`
      + ` aria-selected="${sel ? "true" : "false"}" data-model-tab="${esc(b.name)}"`
      + ` title="只看 ${esc(b.label)} 的模型">${backendIconSvg(b.name)}<span>${esc(b.label)}</span>`
      + `<span class="count-badge">${on} / ${rows.length}</span></button>`;
  }).join("");
}

// 单行渲染：**一个页签里只有一个上游**，所以归属列（后端 icon）不再需要 ——
// 整页都是同一个归属，每行再画一遍是噪声（用户拍板）。
// 也因此不再有「同名模型跨后端合并成一行」那套（`groupModelsByName` /
// `renderModelGroup` 已删）：合并的理由是「同名两行看着像重复」，而现在它们
// 分在两个页签里，各管各的，本来就不会同屏。
function renderModelRow(m) {
  // 来源三色（用户 2026-10-16：内置和上游一个色看不出差别）：
  //   上游 = 默认灰、内置 = 绿（来自后端声明，不是用户动的）、自定义 = 琥珀。
  const sourceLabel = {upstream: "上游", builtin: "内置", custom: "自定义"}[m.source] || m.source || "—";
  const sourceClass = m.custom ? "amber" : m.source === "builtin" ? "green" : "";
  const sourceTag = `<span class="pill ${sourceClass}">${esc(sourceLabel)}</span>`;
  const credits = `<span class="pill ${m.credits === 0 ? "green" : m.credits && m.credits >= 1 ? "amber" : ""}">${esc(creditsText(m))}</span>`;
  const promoNote = m.promo ? `<small class="cell-note">${esc(m.promo.label)} ×${m.promo.credits}</small>` : "";
  const stateCell = `<button class="pill model-state-toggle${m.enabled ? " green" : ""}"`
    + ` data-model-toggle="${esc(m.id)}" data-model-backend="${esc(m.backend || "")}"`
    + ` data-enabled="${m.enabled ? "0" : "1"}"`
    + ` title="点击${m.enabled ? "禁用" : "启用"}「${esc(m.id)}」">${m.enabled ? "启用" : "禁用"}</button>`;
  // 删除入口（用户 2026-10-16 拍板）：操作列整个取消后，删除**做进模型名列**，
  // 只有自定义模型有。非自定义的行不渲染按钮（不是隐藏）—— 操作列不存在了，
  // 「没有按钮」与「不能删」是同一件事。
  const del = m.custom
    ? `<button class="btn-ghost danger model-del" data-model-delete="${esc(m.id)}" data-model-backend="${esc(m.backend || "")}"`
      + ` title="删除自定义模型「${esc(m.id)}」">删除</button>`
    : "";
  // 复选框（用户 2026-10-16 拍板）：批量操作改为只作用于**勾选的行**，
  // 没勾选时批量按钮置灰。key = id + backend（同名模型两边各一行，id 不唯一）。
  // 复选框自带行信息（data-model-key），批量逻辑不必再回头查行对象。
  const key = modelKey(m);
  // 列序与表头一致：选择 → 模型（含删除）→ 来源 → 倍率 → 上下文 → 官方描述 → 状态。
  // 表头是**文字**（倍率 / 上下文）：行内不画列图标 —— 页签已经区分了上游，
  // 行内那一对 ◭/⌘ 每行重复一遍是噪声（用户拍板）。
  return `<tr><td class="model-check-cell"><input type="checkbox" class="model-check"`
    + ` data-model-key="${esc(key)}" aria-label="选择 ${esc(m.id)}"></td>`
    + `<td class="model-name-cell"><span class="model-name-line"><strong>${esc(m.id)}</strong>${del}</span><small class="cell-note">${esc(m.name || "")}</small></td>`
    + `<td>${sourceTag}</td>`
    + `<td>${credits}${promoNote}</td>`
    + `<td><span class="mono">${esc(ctxText(m))}</span></td>`
    + `<td class="model-desc-cell">${esc(m.description || "—")}</td>`
    + `<td>${stateCell}</td></tr>`;
}

// 行的唯一键：id + backend。同名模型跨后端各占一行，单凭 id 区分不开。
// 分隔符用 " "（空格）：模型 id 与后端名都不含空格；不能用 \u0000 ——
// HTML 解析器会把属性值里的 NUL 替换成 U+FFFD，dataset 读回来就对不上
// 了（批量按钮永远置灰的真因，用户 2026-10-16 报告）。
const modelKey = m => m.backend ? `${m.backend} ${m.id}` : m.id;

function renderDashboard() {
  const {accounts, metrics:m = {}, pool} = overview;
  const fmt = value => Math.floor(Number(value)).toLocaleString("zh-CN");
  const rate = value => value === null || value === undefined ? "—" : value.toFixed(1) + "%";
  $("dash-available").textContent = accounts.filter(a=>a.pool_state === "available").length + " / " + accounts.length;
  // 账号总积分 = 各后端体系累加（用户 2026-10-16：就要一个总数）。
  const known = accounts.filter(a=>typeof a.remaining === "number");
  $("dash-credits").textContent = known.length ? creditsFmt(known.reduce((s,a)=>s+a.remaining,0)) : "—";
  $("dash-credits").style.fontSize = "";
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
// 密钥列只显示**名称**（用户 2026-10-17：API Key 不用全显示，打码）。
// key_id 是完整 id， overview.keys 里有 name；找不到（老记录/已删除）显示 id 前 6 位。
const keyLabel = r => {
  const k = overview?.keys?.find(x => x.id === r.key_id);
  return k ? k.name : (r.key_id ? String(r.key_id).slice(0, 6) + "…" : "—");
};
let logOffset = 0, logTotal = 0;
// token 明细列（用户 2026-10-19）：输入 / 输出 / 缓存（读 / 写）。
// 数字压成 12.3K / 1.2M 便于窄列阅读；上游没返回 usage 的项是 NULL，显示「—」，
// 不显示 0（0 与「没记录」语义不同）。
function fmtLogToken(v) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  if (n < 1000) return String(n);
  if (n < 1000000) return (n / 1000).toFixed(n < 10000 ? 1 : 0).replace(/\.0$/, "") + "K";
  return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
}
function tokenCells(r) {
  const read = fmtLogToken(r.tokens_cache_read);
  const creation = fmtLogToken(r.tokens_cache_creation);
  const cache = (read === "—" && creation === "—") ? "—" : `${read} / ${creation}`;
  return `<td class="align-right mono">${fmtLogToken(r.tokens_in)}</td>`
    + `<td class="align-right mono">${fmtLogToken(r.tokens_out)}</td>`
    + `<td class="align-right mono">${cache}</td>`;
}

function renderLogRows(rows) {
  // 账号列 = 后端小图标 + 账号名：图标区分是哪个上游（用户 2026-10-17：去掉
  // 「后端」文字列，账号从模型后面的小字升级为独立列）。
  const accountCell = r => r.account_name
    ? `<span class="account-cell-log">${backendIconSvg(r.backend)}<span>${esc(r.account_name)}</span></span>`
    : "—";
  $("dash-recent-body").innerHTML = rows.map(r=>`<tr><td class="mono muted">${esc(stamp(r.time*1000))}</td><td>${esc(keyLabel(r))}</td><td class="mono">${esc(r.model_id || "—")}</td><td>${accountCell(r)}</td><td><span class="pill log-status ${r.ok ? 'green' : 'red'}" data-tip="${esc(outcomes[r.outcome] || '请求失败')}">${esc(r.status ?? "—")}</span></td>${tokenCells(r)}<td class="align-right mono">${Math.round(Number(r.duration_ms)||0).toLocaleString("zh-CN")} ms</td></tr>`).join("") || '<tr><td colspan="9" class="history-empty">尚无请求记录。发起 API 调用或后台测试后，这里会自动更新。</td></tr>';
}async function loadLogs(offset) {
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
  } catch (e) { $("dash-recent-body").innerHTML = `<tr><td colspan="9" class="history-empty">${esc(e.message)}</td></tr>`; }
}
$("dash-prev").addEventListener("click", () => loadLogs(Math.max(0, logOffset - Number($("dash-page-size").value))));
$("dash-next").addEventListener("click", () => loadLogs(logOffset + Number($("dash-page-size").value)));
$("dash-page-size").addEventListener("change", () => loadLogs(0));
// 请求日志页的「刷新」按钮（用户 2026-10-17：与用量明细页对齐，该页原来只有
// 翻页、没有主动刷新入口）—— 重新拉当前页。
$("logs-refresh").addEventListener("click", () => loadLogs(logOffset));
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
    // 两列分工（用户 2026-10-16）：「7 天内过期」= 数量；「最近过期」= 最近
    // 一批的到期时间 + 该批积分（原来挤在剩余积分备注里，用户要独立成列）。
    const expiringCell = `<td><strong class="credit-number ${a.expiring_soon > 0 ? "credit-warn" : ""}">${expiring}</strong></td>`;
    const hasExpiry = a.earliest_expiry && typeof a.earliest_expiry_remaining === "number" && a.earliest_expiry_remaining > 0;
    const nearestCell = `<td>${hasExpiry
      ? `<strong class="credit-number credit-warn">${creditsFmt(a.earliest_expiry_remaining)}</strong><small class="cell-note">${esc(stamp(a.earliest_expiry * 1000))} 到期</small>`
      : '<span class="muted">—</span>'}</td>`;
    // 账号类型图标（需求 5）：用后端注册表的 icon，而不是固定的首字母。
    // 首字母在两个后端都可能一样时无法区分账号类型，这正是需求要解决的问题。
    // 行首小 icon + 账号名，就这两样（用户 2026-10-16：icon/后端徽标/昵称
    // 全是冗余 —— 归属一列已有「账号类型」语义，后端名在详情弹框里看）。
    const kindIcon = `<span class="account-icon-backend account-icon-inline" style="color:${esc(backendMeta(a.backend)?.color || "#888")}" title="${esc(backendName(a.backend))} 账号">${backendIconSvg(a.backend)}</span>`;
    // 状态列即开关（用户 2026-10-17）：和模型页同款，点 pill 直接停用/恢复，
    // 不必再进设置弹框。文字仍是当前状态，颜色照旧按状态走；停用时置灰。
    const stateToggle = `<button class="pill account-state-toggle${a.enabled ? " " + status[1] : ""}" data-action="toggle" data-id="${esc(a.id)}" data-enabled="${a.enabled ? "1" : "0"}" title="点击${a.enabled ? "暂停" : "恢复"}「${esc(a.name)}」">${a.enabled ? status[0] : "已暂停"}</button>`;
    // 行内签到（用户 2026-10-17）：今天没签到才给按钮；已签到只留一行文字。
    // 暂停的账号也能签到 —— pool.operate 对 checkin/status 不拦停用。
    const checkinCell = a.today_checked_in
      ? `<small class="cell-note">今日已签到${a.checkin_when ? `（${esc(a.checkin_slot || "")} ${esc(a.checkin_when)}）` : ""}</small>`
      : `<button class="linklike" data-action="checkin" data-id="${esc(a.id)}" title="立即签到（每日一次，已签到会提示无需重复）">签到</button>`;
    // data-account + 可点击样式：点行开详情（需求 6），点行内按钮不弹框。
    // 操作列（用户 2026-10-16：按钮太多看着乱）→ 一个「设置」按钮。
    // 全部操作收进设置弹框；查询积分/签到/刷新凭据这类即时动作也在里面。
    // 冷却中才给「解除冷却」（用户 2026-10-18）：上游按单个模型限流，但冷却记在
    // 账号上——期间所有模型都不可用。按钮只在 cooldown_until 未过期时出现。
    const cooling = a.cooldown_until > Date.now() / 1000;
    const unfreezeBtn = cooling ? `<button class="btn-ghost" data-action="clear-cooldown" data-id="${a.id}" title="提前解除账号冷却，立即重新参与分配">解除冷却</button>` : "";
    const actions = `<td><div class="actions">${unfreezeBtn}<button class="btn-ghost" data-action="logs" data-id="${a.id}" title="查看这个账号的请求日志">日志</button><button class="btn-ghost" data-action="settings" data-id="${a.id}" title="备注 / 权重 / 删除">⚙ 设置</button></div></td>`;
    return `<tr class="account-row" data-account="${esc(a.id)}"><td><div class="account-cell">${kindIcon}<strong>${esc(accountLabel(a))}</strong></div></td><td>${stateToggle}${checkinCell}${a.cooldown_until > Date.now()/1000 ? `<small class="cell-note">至 ${esc(stamp(a.cooldown_until*1000))}</small>` : ""}${a.queued_until > Date.now()/1000 ? `<small class="cell-note">排队让位至 ${esc(stamp(a.queued_until*1000))}</small>` : ""}</td><td><button type="button" class="credit-number credit-link" data-credits="${esc(a.id)}" title="点击查看该账号每天的积分消费">${credits}</button><small class="cell-note">${a.credits_updated ? esc(stamp(a.credits_updated*1000)) : "点击查询积分"}${a.credits_stale && a.credits_updated ? " · 待刷新" : ""}</small>${a.last_error ? `<small class="cell-note field-error">${esc(a.last_error)}</small>` : ""}</td>${expiringCell}${nearestCell}${actions}</tr>`;
  }).join("") || (overview.accounts.length ? '<tr><td colspan="6" class="muted">没有符合筛选条件的账号。</td></tr>' : "");
}
$("account-search").addEventListener("input", () => { accountPage = 0; if(overview) renderAccounts(); });
$("account-filter").addEventListener("change", () => { accountPage = 0; if(overview) renderAccounts(); });
$("account-prev").addEventListener("click", () => { accountPage = Math.max(0, accountPage - 1); if(overview) renderAccounts(); });
$("account-next").addEventListener("click", () => { accountPage += 1; if(overview) renderAccounts(); });
$("account-page-size").addEventListener("change", e => { accountPageSize = Number(e.target.value) || 10; accountPage = 0; if(overview) renderAccounts(); });

// 账号详情弹框（需求 6）：点账号行打开。**只读** —— 行内的操作按钮仍承担写操作，
// 这里不重复一份，避免两条写路径各自漂移。
async function openAccountDetail(a) {
  if (!a) return;
  const yes = v => v === true ? "是" : v === false ? "否" : "—";
  const stampOr = v => v ? stamp(v * 1000) : "—";
  const numOr = v => v === null || v === undefined ? "—" : creditsFmt(v);
  const poolLabel = {available:["可用","green"],cooling:["冷却中","amber"],paused:["已暂停",""],exhausted:["积分耗尽","amber"],invalid:["凭据异常","red"]}[a.pool_state] || ["待查询",""];
  $("account-detail-title").textContent = accountLabel(a);
  // 副标题只留「什么后端的账号」——本地 aid 是 hex，对人类不可读（用户 2026-10-17）。
  $("account-detail-sub").textContent = `${backendName(a.backend)} 账号`;
  // 头部：后端图标 + 状态/启用徽标（一眼看到「这个账号现在什么状态」）。
  $("account-detail-avatar").innerHTML =
    `<span class="account-icon-backend" style="color:${esc(backendMeta(a.backend)?.color || "#888")}" title="${esc(backendName(a.backend))} 账号">${backendIconSvg(a.backend)}</span>`;
  $("account-detail-badges").innerHTML =
    `<span class="pill ${poolLabel[1]}">${esc(poolLabel[0])}</span>`
    + `<span class="pill ${a.enabled ? "green" : ""}">${a.enabled ? "已启用" : "已暂停"}</span>`
    + (a.expired ? `<span class="pill red">凭据已到期</span>` : "");
  // 三张关键指标：剩余积分 / 今日签到 / 7 天内到期。
  const checkinValue = a.today_checked_in ? "已签到" : "未签到";
  $("account-detail-metrics").innerHTML = [
    [a.remaining === null || a.remaining === undefined ? "待查询" : creditsFmt(a.remaining), "剩余积分", a.remaining === null || a.remaining === undefined ? "以最近一次查询为准" : `更新于 ${stampOr(a.credits_updated)}`, ""],
    [checkinValue, "今日签到", a.checkin_slot ? `${a.checkin_slot} ${a.checkin_when || ""}` : "自动签到按设置时段执行", a.today_checked_in ? "ok" : "warn"],
    [numOr(a.expiring_soon), "7 天内到期", a.earliest_expiry ? `最早一批 ${stampOr(a.earliest_expiry)}` : "近期没有将过期的积分", a.expiring_soon ? "warn" : ""],
  ].map(([v, label, note, tone]) =>
    `<article><span class="detail-metric-label">${esc(label)}</span><strong class="detail-metric-value${tone ? " " + tone : ""}">${esc(v)}</strong><small>${esc(note)}</small></article>`).join("");
  // 分组字段卡：基本信息 / 积分与过期 / 签到 / 调度。两列 grid，窄屏自动单列。
  const section = (title, rows) =>
    `<article class="detail-section"><h3>${esc(title)}</h3><dl class="detail-grid">`
    + rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("") + `</dl></article>`;
  $("account-detail-body").innerHTML = [
    section("基本信息", [
      ["账号类型", `${backendIcon(a.backend)} ${backendName(a.backend)}`],
      // UID 是上游 uuid（36 位），对人类不可读：只显前 8 位，完整值悬停可见
      // （用户 2026-10-17：「uuid 不用显示，没有人类不可读」）。
      ["UID", a.uid ? `<span class="short-id" title="${esc(a.uid)}">${esc(a.uid.slice(0, 8))}</span>` : "—"],
      ["凭据有效期", stamp(a.expires_at) + (a.expired ? "（已到期）" : "")],
    ]),
    section("积分与过期", [
      ["积分更新时间", stampOr(a.credits_updated)],
      ["最早一批过期", stampOr(a.earliest_expiry)],
      ["最早一批过期积分", numOr(a.earliest_expiry_remaining)],
    ]),
    section("签到", [
      ["今日已签到", yes(a.today_checked_in)],
      ["签到时段", a.checkin_slot ? `${a.checkin_slot} ${a.checkin_when || ""}` : "—"],
    ]),
    section("调度", [
      ["状态", poolLabel[0]],
      ["调度权重", String(a.weight || 0)],
      ["冷却至", a.cooldown_until > Date.now()/1000 ? stamp(a.cooldown_until*1000) : "—"],
      ["排队让位至", a.queued_until > Date.now()/1000 ? stamp(a.queued_until*1000) : "—"],
      ["是否启用", yes(a.enabled)],
    ]),
  ].join("");
  // 最近错误只在真有时才出现，没错误时不留一块空白（用户 2026-10-16：弹框别堆空字段）。
  const err = a.last_error;
  if (err) { $("account-detail-error").hidden = false; $("account-detail-error").innerHTML = `<strong>最近错误</strong><p>${esc(err)}</p>`; }
  else $("account-detail-error").hidden = true;
  // 「最近请求」从详情弹框撤掉（用户 2026-10-17：详情页信息太多，且请求记录
  // 已有专门的「请求日志」页与各弹框可看，这里不再重复）。原来拉日志的
  // /admin/api/logs 请求、account-detail-logs 表格与显示开关一并移除。
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
    // tab 依赖 backends（上一行刚 apply），同样在这里补画：不进用量页时
    // initUsagePage 不会跑，首次进入前 tab 条必须是空的而不是陈旧的。
    if (usageInitialized) { fillUsageAccounts(); paintUsageTabs(); }
    if (page === "dashboard" || page === "logs") loadLogs(logOffset);
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
// 查询全部积分（用户拍板：工具栏直接给一个，不用进设置弹框逐个点）。
// 复用批量动作通道：状态动作里就含积分查询，弹窗逐账号报结果。
$("batch-credits").addEventListener("click", async () => {
  $("batch-credits").disabled = true;
  const original = $("batch-credits").textContent;
  $("batch-credits").textContent = "查询中…";
  try {
    const result = await api("pool/actions/status", {method:"POST"});
    $("pool-result").hidden = false;
    $("pool-result").textContent = result.results.map(r => `${overview.accounts.find(a=>a.id===r.id)?.name || "账号"}：${r.message}`).join("；") || "没有启用的账号";
    await refresh(true); toast("已拉取全部账号积分");
  } catch (e) { toast(e.message); }
  finally { $("batch-credits").disabled = false; $("batch-credits").textContent = original; }
});
$("dash-add").addEventListener("click", () => openAccount());
$("dash-add-account").addEventListener("click", () => openAccount());
$("empty-add").addEventListener("click", () => openAccount());
$("accounts-back-dash").addEventListener("click", e => { e.preventDefault(); goPage("dashboard"); });
$("pool-settings").addEventListener("submit", async e => { e.preventDefault();e.submitter.disabled=true;try {await api("pool/settings",{method:"PATCH",body:{backup_enabled:$("backup-enabled").checked,backup_interval_hours:Number($("backup-interval").value),backup_keep:Number($("backup-keep").value),default_routing:$("pool-routing").value,auto_checkin:$("auto-checkin").checked,checkin_time:$("checkin-time").value,credit_min_interval:Number($("credit-interval").value),sync_start:$("sync-start").value,sync_end:$("sync-end").value,batch_spread:Number($("batch-spread").value),queue_failover:$("queue-failover").checked,queue_model_fallback:$("queue-model-fallback").checked,queue_retry:Number($("queue-retry").value),queue_cooldown:Number($("queue-cooldown").value),queue_hold_ms:Number($("queue-hold-ms").value),queue_fallback_model:$("queue-fallback-model").value.trim(),guard_reserve_credits:Number($("guard-reserve-credits").value),guard_daily_token_limit:Number($("guard-daily-token-limit").value),guard_daily_credit_limit:Number($("guard-daily-credit-limit").value),guard_model_daily_token_limit:Number($("guard-model-daily-token-limit").value),backend_enabled:backendFlagsBody()}});await refresh();await loadBackups();toast("设置已保存");}catch(err){toast(err.message);}finally{e.submitter.disabled=false;} });
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
  $("backup-body").innerHTML = data.items.map(b => `<tr><td><code>${esc(b.name)}</code>${b.is_last ? ' <span class="pill green">最近一次</span>' : ""}</td><td class="mono muted">${esc(b.created_text)}</td><td class="muted">${esc(b.age_text)}</td><td class="align-right mono">${esc(b.size_text)}</td><td class="align-right"><button class="btn-ghost" data-backup-download="${esc(b.name)}">下载</button> <button class="btn-ghost" data-backup-restore="${esc(b.name)}">恢复</button> <button class="btn-ghost danger" data-backup-delete="${esc(b.name)}">删除</button></td></tr>`).join("");
}
function backupGuard(button, fn) {
  return async () => {
    if (backupBusy) return;
    backupBusy = true; const original = button.textContent; button.disabled = true;
    try { await fn(); } catch (e) { toast(e.message); }
    finally { backupBusy = false; button.disabled = false; button.textContent = original; }
  };
}
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
  // 备注名唯一：用户填了非空备注才预检（留空时后端回落上游昵称，前端无从预判）。
  const wantName = $("account-name").value.trim();
  if (wantName) {
    const owner = nameOwner(wantName);
    if (owner) { $("account-error").textContent = `备注「${wantName}」已被账号「${owner}」占用，请换一个`; return; }
  }
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
    // 备注名唯一：同 OAuth 链路，只在用户填了非空备注时预检（见 oauth-start）。
    const wantName = $("account-name").value.trim();
    if (wantName) {
      const owner = nameOwner(wantName);
      if (owner) { $("account-error").textContent = `备注「${wantName}」已被账号「${owner}」占用，请换一个`; e.submitter.disabled = false; return; }
    }
    await api("accounts", {method:"POST", body:{name: wantName || undefined, credential}});
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
  // 积分数字 → 每日积分弹框（用户 2026-10-19）。必须排在 data-action 之前：
  // 账号行整行点击会开详情弹框，不拦这一下就会连带弹出两个框。
  const credit = e.target.closest("[data-credits]");
  if (credit) { openCreditDaily(credit.dataset.credits); return; }
  const b = e.target.closest("[data-action]");
  if (!b) {
    // 点行**空白处**打开详情弹框（需求 6）；点操作按钮仍执行操作，不弹框。
    const tr = e.target.closest("tr[data-account]");
    if (tr) openAccountDetail(overview.accounts.find(x => x.id === tr.dataset.account));
    return;
  }
  const a = overview.accounts.find(x => x.id === b.dataset.id); if (!a) return;
  if (b.dataset.action === "settings") { openAccountSettings(a); return; }
  if (b.dataset.action === "logs") { openAccountLogs(a); return; }
  if (b.dataset.action === "clear-cooldown") {
    // 解除冷却（用户 2026-10-18）：先确认一次——上游限流没重置就放行，下一个
    // 请求大概率再撞一次，退避从当前连续失败计数继续翻倍，不会清零重来。
    confirmAction("解除这个账号的冷却？",
      "上游限流可能还没到重置时间，解除后下一个请求可能再次触发冷却。确定要立即让这个账号重新参与分配吗？",
      async () => {
        await api(`accounts/${a.id}/actions/clear-cooldown`, {method:"POST"});
        await refresh();
      });
    return;
  }
  if (b.dataset.action === "checkin") {
    // 行内签到（用户 2026-10-17）：不在账号池行与设置弹框之间来回跳。
    b.disabled = true;
    try { const r = await api(`accounts/${a.id}/actions/checkin`, {method:"POST"}); toast(r.message); await refresh(); }
    catch (err) { toast(err.message); } finally { b.disabled = false; }
    return;
  }
  if (b.dataset.action === "toggle") {
    // 行内停用/恢复（用户 2026-10-17）：仿模型页状态列，点一下即切。
    // 停用会让账号退出轮转与自动签到，先确认一次，避免误点把账号踢下线。
    const enabling = b.dataset.enabled !== "1";
    confirmAction(enabling ? "恢复这个账号？" : "暂停这个账号？",
      "暂停后不参与新请求分配和自动签到，已开始的请求继续完成。恢复后重新参与轮转。",
      async () => { await api("accounts/" + a.id, {method:"PATCH", body:{enabled:enabling}}); });
  }
});
// -- 账号设置弹框 ----------------------------------------------------------
// 操作列收成一个「设置」按钮（用户 2026-10-16）：备注/权重在这里改，
// 查询积分/签到/刷新凭据这类即时动作也在顶部一排，暂停/删除沉底。
let settingsAccountId = null;

function openAccountSettings(a) {
  if (!a) return;
  settingsAccountId = a.id;
  $("account-settings-title").textContent = accountLabel(a);
  $("account-settings-sub").textContent = `${backendName(a.backend)} 账号`;
  $("as-name").value = a.name || "";
  $("as-weight").value = a.weight || 0;
  $("account-settings-error").textContent = "";
  // 「解除冷却」只在真冷却着时出现（用户 2026-10-18：上游按模型限流却冷整个
  // 账号，设置弹框里也要能放行，与账号行那颗按钮同一入口）。
  $("as-unfreeze").hidden = !(a.cooldown_until > Date.now() / 1000);
  $("account-settings-dialog").showModal();
}

const settingsAccount = () => overview.accounts.find(x => x.id === settingsAccountId);

$("as-status").addEventListener("click", async e => {
  e.preventDefault();
  const a = settingsAccount(); if (!a) return;
  e.target.disabled = true;
  try { const r = await api(`accounts/${a.id}/actions/status`, {method:"POST"}); toast(r.message); await refresh(); }
  catch (err) { toast(err.message); } finally { e.target.disabled = false; }
});
$("as-unfreeze").addEventListener("click", async e => {
  e.preventDefault();
  const a = settingsAccount(); if (!a) return;
  confirmAction("解除这个账号的冷却？",
    "上游限流可能还没到重置时间，解除后下一个请求可能再次触发冷却。确定要立即让这个账号重新参与分配吗？",
    async () => {
      await api(`accounts/${a.id}/actions/clear-cooldown`, {method:"POST"});
      $("account-settings-dialog").close();
      await refresh();
    });
});
$("as-refresh").addEventListener("click", async e => {
  e.preventDefault();
  const a = settingsAccount(); if (!a) return;
  e.target.disabled = true;
  try { const r = await api(`accounts/${a.id}/actions/refresh`, {method:"POST"}); toast(r.message); await refresh(); }
  catch (err) { toast(err.message); } finally { e.target.disabled = false; }
});
// 账号请求日志弹框：复用请求日志页的列与样式（account-cell-log / cell-note.inline），
// 但没有「密钥」列以外的差异——这里不显示账号列（整个弹框就按这个账号筛的）。
// 用户 2026-10-17：操作列「日志」按钮的落点。每页 100 条 + 默认只看最近 7 天
// （更早的记录去「请求日志」页翻），带分页控件。
let accountLogsId = null, accountLogsOffset = 0;
const ACCOUNT_LOGS_SIZE = 100;
async function openAccountLogs(a) {
  if (!a) return;
  accountLogsId = a.id;
  accountLogsOffset = 0;
  await loadAccountLogs();
  $("account-logs-dialog").showModal();
}
async function loadAccountLogs() {
  if (!accountLogsId) return;
  const a = overview.accounts.find(x => x.id === accountLogsId);
  if (!a) return;
  $("account-logs-title").textContent = accountLabel(a);
  $("account-logs-sub").textContent = `${backendName(a.backend)} 账号 · 最近 7 天，每页 ${ACCOUNT_LOGS_SIZE} 条`;
  $("account-logs-body").innerHTML = '<tr><td colspan="8" class="history-empty">加载中…</td></tr>';
  const requestedId = accountLogsId, requestedOffset = accountLogsOffset;
  const since = Math.floor((Date.now() - 7 * 86400000) / 1000);
  try {
    const data = await api(`logs?account_id=${encodeURIComponent(accountLogsId)}&limit=${ACCOUNT_LOGS_SIZE}&offset=${accountLogsOffset}&since=${since}`);
    if (requestedId !== accountLogsId || requestedOffset !== accountLogsOffset) return;  // 已切账号/翻页
    $("account-logs-body").innerHTML = data.rows.map(r =>
      `<tr><td class="mono muted">${esc(stamp(r.time * 1000))}</td>`
      + `<td>${esc(keyLabel(r))}</td>`
      + `<td class="mono">${esc(r.model_id || "—")}</td>`
      + `<td><span class="pill log-status ${r.ok ? 'green' : 'red'}" data-tip="${esc(outcomes[r.outcome] || '请求失败')}">${esc(r.status ?? "—")}</span></td>`
      + tokenCells(r)
      + `<td class="align-right mono">${Math.round(Number(r.duration_ms) || 0).toLocaleString("zh-CN")} ms</td></tr>`).join("")
      || '<tr><td colspan="8" class="history-empty">这个账号最近 7 天没有请求记录。</td></tr>';
    const pages = Math.max(1, Math.ceil(data.total / ACCOUNT_LOGS_SIZE));
    const current = Math.floor(data.offset / ACCOUNT_LOGS_SIZE) + 1;
    $("account-logs-info").textContent = data.total ? `第 ${current} / ${pages} 页 · 共 ${data.total.toLocaleString("zh-CN")} 条` : "暂无记录";
    $("account-logs-prev").disabled = data.offset <= 0;
    $("account-logs-next").disabled = data.offset + ACCOUNT_LOGS_SIZE >= data.total;
  } catch (err) {
    if (requestedId !== accountLogsId) return;
    $("account-logs-body").innerHTML = `<tr><td colspan="8" class="history-empty">${esc(err.message)}</td></tr>`;
  }
}
$("account-logs-prev").addEventListener("click", () => { accountLogsOffset = Math.max(0, accountLogsOffset - ACCOUNT_LOGS_SIZE); loadAccountLogs(); });
$("account-logs-next").addEventListener("click", () => { accountLogsOffset += ACCOUNT_LOGS_SIZE; loadAccountLogs(); });
// 行内「停用/恢复」与「签到」已上线（用户 2026-10-17），设置弹框里那两个
// 重复入口（as-checkin / as-toggle）随之撤掉，这里只留删除。
$("as-delete").addEventListener("click", e => {
  e.preventDefault();
  const a = settingsAccount(); if (!a) return;
  confirmAction("删除这个账号？", `「${a.name}」将从账号列表移除。服务器保留恢复副本。`,
    async () => { await api("accounts/" + a.id, {method:"DELETE"}); $("account-settings-dialog").close(); });
});
$("account-settings-form").addEventListener("submit", async e => {
  e.preventDefault();
  const a = settingsAccount(); if (!a) return;
  const w = Number($("as-weight").value);
  if (!Number.isInteger(w) || w < 0 || w > 100) { $("account-settings-error").textContent = "权重须为 0-100 的整数"; return; }
  // 备注名全局唯一（用户 2026-10-17 定稿：硬拦，不让保存）。改自己名字不算
  // 占用——exceptId 传自己的 id。占用者名字一并给出，用户才知道该换什么。
  const want = $("as-name").value.trim() || a.name;
  const owner = nameOwner(want, a.id);
  if (owner) { $("account-settings-error").textContent = `备注「${want}」已被账号「${owner}」占用，请换一个`; return; }
  $("as-save").disabled = true;
  try {
    await api("accounts/" + a.id, {method:"PATCH", body:{name: want, weight: w}});
    $("account-settings-dialog").close();
    await refresh(); toast("设置已保存");
  } catch (err) { $("account-settings-error").textContent = err.message; }
  finally { $("as-save").disabled = false; }
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
  if (chip) { openKeyBind(overview.keys.find(x => x.id === chip.dataset.routingEdit), "all"); return; }
  // 「可用模型」/「可用账号」列：只开对应的勾选（用户 2026-10-17：别再甩整个
  // 设置弹框——点模型就选模型，点账号就选账号，要改别的去点「设置」）。
  const sc = e.target.closest("[data-scope]");
  if (sc) { openKeyBind(overview.keys.find(x => x.id === sc.dataset.scopeKid), sc.dataset.scope); return; }
  const bind = e.target.closest("[data-bind]");
  if (bind) { openKeyBind(overview.keys.find(x => x.id === bind.dataset.bind), "all"); return; }
  // 限额 / 停用 / 撤销的表格按钮已收敛进「设置」弹框（用户 2026-10-16），
  // 对应的 data-limit / data-toggle / data-revoke 入口随之删除。
});
// 打开「创建密钥」时回到表单面板：两个 body 都要复位，footer 也要切回
// 表单的那一个（created-key-footer 保持 hidden，否则会看到两组按钮）。
$("add-key").addEventListener("click", () => { $("key-form").hidden = false; $("created-key").hidden = true; $("created-key-footer").hidden = true; $("key-dialog").showModal(); });

function openKeyBind(k, mode = "all") {
  if (!k) return;
  // mode: "all"=设置弹框（全部区块）/ "models"=只勾选模型 / "accounts"=只勾选账号。
  const onlyModels = mode === "models", onlyAccounts = mode === "accounts";
  $("key-bind-name").textContent = `密钥「${k.name}」· ${k.hint}`;
  $("key-bind-error").textContent = "";
  // 单区块模式下：标题、说明与无关区块一起换，避免「点模型却看见账号」。
  $("key-bind-title").textContent = onlyModels ? "可用模型" : onlyAccounts ? "可用账号" : "配置密钥权限";
  $("key-bind-sub").textContent = onlyModels
    ? "不勾选 = 全部可用。按上游分页签，保存只改模型范围。"
    : onlyAccounts ? "不勾选 = 全部可用。保存只改账号范围。" : "";
  // 区块显隐：label 与内容成对收/放（models 行含页签）。
  $("key-bind-models-block").hidden = onlyAccounts;
  $("key-bind-accounts-block").hidden = onlyModels;
  $("key-bind-routing-block").hidden = mode !== "all";
  $("key-bind-limit-block").hidden = mode !== "all";
  $("key-bind-expires-block").hidden = mode !== "all";
  $("key-bind-toggle").hidden = mode !== "all";
  $("key-bind-reset").hidden = mode !== "all";
  $("key-bind-revoke").hidden = mode !== "all";
  // 限额与有效期（用户 2026-10-16：都收进设置弹框）。
  $("key-bind-limit").value = k.daily_limit ?? "";
  $("key-bind-expires").value = k.expires_at ? toLocalInput(k.expires_at) : "";
  const toggle = $("key-bind-toggle");
  toggle.textContent = k.enabled ? "停用密钥" : "恢复密钥";
  const list = (host, items, selected, render) => {
    host.replaceChildren(...items.map(item => {
      const label = document.createElement("label");
      label.className = "check-item";
      const box = document.createElement("input");
      box.type = "checkbox"; box.value = item.value; box.checked = selected.includes(item.value);
      const out = render(item);
      const span = document.createElement("span");
      // render 可返回字符串，或 {text, title}（带悬停提示，模型列表在用）。
      if (typeof out === "string") span.textContent = out;
      else { span.textContent = out.text; if (out.title) span.title = out.title; }
      label.append(box, span);
      return label;
    }));
    if (!items.length) { const p = document.createElement("p"); p.className = "muted"; p.textContent = "暂无可选项。"; host.replaceChildren(p); }
  };
  const models = (overview.models?.models || []).filter(m => m.enabled)
    .map(m => ({value: m.id, name: m.name || "", backend: m.backend,
      // creditsText / 活动标签要用：map 收窄字段时把计费信息带上，
      // 否则可选模型全部显示「标准」（用户报告）。
      credits: m.credits, promo: m.promo}));
  const accounts = overview.accounts.map(a => ({value:a.id, name:a.name, state:a.pool_state, backend:a.backend,
    remaining:a.remaining, expiring_soon:a.expiring_soon}));
  // 可用模型按归属分页签（用户拍板）：同一份 check-list，页签只控制**哪些行
  // 显示**，勾选状态保留 —— 切页签不丢已勾的项，保存时仍取全部勾选值。
  // 页签数据来自注册表（/admin/api/backends），不写死后端名。
  const selectedModels = Array.isArray(k.models) ? k.models : [];
  const bindTabs = visibleBackends();
  let bindTab = bindTabs[0]?.name || "";
  const bindTabsBox = $("key-bind-model-tabs");
  const paintBindTabs = () => {
    bindTabsBox.innerHTML = bindTabs.map(b => {
      const rows = models.filter(m => m.backend === b.name);
      // 判据用 m.value（map 后元素的字段），不是 m.id —— map 重构后元素上
      // 没有 id，m.id 恒为 undefined，includes 永远 false → badge 永远 0
      //（用户两次报告的真根因；checkbox 的 checked 判据用的是 item.value，
      // 所以勾选框是对的、badge 是错的，两边对不上）。
      const on = rows.filter(m => selectedModels.includes(m.value)).length;
      const sel = b.name === bindTab;
      return `<button type="button" class="model-tab${sel ? " selected" : ""}" role="tab" aria-selected="${sel}"`
        + ` data-bind-tab="${esc(b.name)}" title="只看 ${esc(b.label)} 的模型">${backendIconSvg(b.name)}<span>${esc(b.label)}</span>`
        + `<span class="count-badge">${on} / ${rows.length}</span></button>`;
    }).join("");
  };
  bindTabsBox.onclick = e => {
    const btn = e.target.closest("[data-bind-tab]");
    if (!btn) return;
    bindTab = btn.dataset.bindTab;
    paintBindTabs();
    paintBindModels();
  };
  const paintBindModels = () => {
    const rows = models.filter(m => m.backend === bindTab);
    // 每行 = **模型 id 为主** + 括号里倍率/活动（用户反馈：id 是必须显示的
    // 标识，上一版只放 tooltip 过度删除了）。name 与 id 同义时不重复显示。
    list($("key-bind-models"), rows, selectedModels, m => {
      const bits = [creditsText(m)];
      if (m.promo) bits.push(`${m.promo.label} ×${m.promo.credits}`);
      // name 只在**不是 id 的简单变体**时显示（用户反馈：MiniMax-M2.7
      // 这类「id 换大小写」的 name 是冗余）。只显示首字母大小写差异的
      // （去掉分隔符后忽略大小写相等）不重复展示。
      const norm = s => String(s).replace(/[-_.\s]/g, "").toLowerCase();
      const showName = m.name && m.name !== m.value && norm(m.name) !== norm(m.value);
      return {text: `${m.value}${showName ? `（${m.name}）` : ""}（${bits.join(" · ")}）`,
        title: m.promo ? `${m.promo.label} 折扣后 ×${m.promo.credits}` : ""};
    });
  };
  bindTabsBox.hidden = !bindTabs.length || models.length < 2;
  paintBindTabs();
  paintBindModels();
  // 勾选/取消模型时页签 badge 要实时刷新（用户报告：已勾选仍显示 0/20）——
  // badge 从 selectedModels 数数，所以先把 DOM 的勾选态同步回数组再重画。
  // 只在打开和切页签时重画是不够的；用 onclick 而非 addEventListener：
  // 本函数每次开弹框都会执行，onclick 赋值幂等，不会叠挂监听器。
  $("key-bind-models").onchange = () => {
    selectedModels.length = 0;
    selectedModels.push(...checkedValues($("key-bind-models")));
    paintBindTabs();
  };
  // 可用账号：名字前带账号类型 icon（用户拍板）—— 多后端账号池里一眼分清
  // 这是哪个上游的账号，与模型页签的 icon 口径一致。括号里不显示 id 前缀
  // （机器可读对人不友好，用户拍板）：改显示 余额 / 最近过期积分。
  list($("key-bind-accounts"), accounts, Array.isArray(k.accounts) ? k.accounts : [],
    a => {
      const bal = typeof a.remaining === "number" ? `余 ${creditsFmt(a.remaining)}` : "待查询";
      const soon = a.expiring_soon > 0 ? ` · ${creditsFmt(a.expiring_soon)} 将过期` : "";
      return `${a.name}（${bal}${soon}）`;
    });
  $("key-bind-accounts").querySelectorAll(".check-item").forEach((el, i) => {
    const a = accounts[i];
    if (!a) return;
    el.prepend(backendIconEl(a.backend));
  });
  $("key-bind-routing").value = k.routing || "";
  // 「跟随默认」必须让人看到默认是什么（用户 2026-10-16）：在 select 后面
  // 追加当前的全局默认策略名，随 overview 实时刷新 —— 设置页改了默认这里
  // 下次打开就是新值。
  $("key-bind-routing-hint").textContent = k.routing
    ? "" : `当前默认：${routingShort(overview.pool?.default_routing || "round_robin")}`;
  // 撤销不可逆：重置一个已死密钥没有意义（后端也会 400），按钮直接禁掉并说明。
  $("key-bind-reset").disabled = !!k.revoked_at;
  $("key-bind-reset").title = k.revoked_at ? "已撤销的密钥不能重置，请直接新建" : "";
  $("key-bind-dialog").dataset.kid = k.id;
  $("key-bind-dialog").dataset.mode = mode;
  $("key-bind-dialog").showModal();
}
// 独立 icon 元素（列表项用）：与 backendIconSvg 同源，颜色来自注册表声明。
function backendIconEl(name) {
  const span = document.createElement("span");
  span.className = "account-icon-backend account-icon-inline";
  span.style.color = backendMeta(name)?.color || "#888";
  span.title = `${backendName(name)} 账号`;
  span.innerHTML = backendIconSvg(name);
  return span;
}
const checkedValues = host => [...host.querySelectorAll("input:checked")].map(b => b.value);
$("key-bind-form").addEventListener("submit", async e => {
  e.preventDefault(); e.submitter.disabled = true; $("key-bind-error").textContent = "";
  const kid = $("key-bind-dialog").dataset.kid;
  const mode = $("key-bind-dialog").dataset.mode || "all";
  try {
    // 单区块模式只提交可见的那一组：未显示的字段不带上，避免把没编辑的
    // 范围清空（后端 PATCH 是「带了键就覆盖」语义）。
    const body = mode === "models" ? {models: checkedValues($("key-bind-models"))}
      : mode === "accounts" ? {accounts: checkedValues($("key-bind-accounts"))}
      : {
        models: checkedValues($("key-bind-models")),
        accounts: checkedValues($("key-bind-accounts")),
        routing: $("key-bind-routing").value || null,
        daily_limit: $("key-bind-limit").value === "" ? null : Number($("key-bind-limit").value),
        expires_at: $("key-bind-expires").value ? fromLocalInput($("key-bind-expires").value) : null};
    await api("keys/" + kid, {method:"PATCH", body});
    $("key-bind-dialog").close(); await refresh(); toast("密钥权限已保存");
  } catch (err) { $("key-bind-error").textContent = err.message; }
  finally { e.submitter.disabled = false; }
});
$("key-dialog").addEventListener("close", () => { $("key-form").reset(); $("new-key-value").textContent = ""; $("key-error").textContent = ""; });
// datetime-local 值 <-> unix 秒。密钥过期时刻按浏览器本地时区理解，
// 与管理页其余时间戳的展示口径一致。
const toLocalInput = sec => {
  const d = new Date(sec * 1000), p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
};
const fromLocalInput = v => Math.floor(new Date(v).getTime() / 1000);
// 停用/恢复与撤销收进设置弹框（用户 2026-10-16）：语义不同（停用可逆、
// 撤销不可逆）所以都保留，但不再各占表格按钮位。
$("key-bind-toggle").addEventListener("click", e => {
  e.preventDefault();
  const k = overview.keys.find(x => x.id === $("key-bind-dialog").dataset.kid); if (!k) return;
  const next = !k.enabled;
  confirmAction(next ? "恢复这枚密钥？" : "停用这枚密钥？",
    next ? `恢复后「${k.name}」可再次发起请求。` : `停用后「${k.name}」立即无法发起新请求，可随时恢复。`,
    async () => { await api("keys/" + k.id, {method:"PATCH", body:{enabled: next}}); $("key-bind-dialog").close(); });
});
$("key-bind-revoke").addEventListener("click", e => {
  e.preventDefault();
  const k = overview.keys.find(x => x.id === $("key-bind-dialog").dataset.kid); if (!k) return;
  confirmAction("撤销客户端密钥？", `使用「${k.name}」的客户端将无法发起新请求。此操作不可撤销。`,
    async () => { await api("keys/" + k.id, {method:"DELETE"}); $("key-bind-dialog").close(); });
});
// 重置密钥：轮换 key 值（旧值立即失效），配置与用量历史全保留。与撤销的区别是
// 可继续用同一枚（kid 不变）——场景是密钥疑似泄露。停用态重置后仍停用。
$("key-bind-reset").addEventListener("click", e => {
  e.preventDefault();
  const k = overview.keys.find(x => x.id === $("key-bind-dialog").dataset.kid); if (!k) return;
  const paused = !k.enabled ? "\n当前为停用状态，重置后仍为停用，需手动恢复。" : "";
  confirmAction(`重置「${k.name}」的密钥？`,
    `将生成新的密钥值，旧值立即失效；限额、权限、用量历史全部保留。新密钥只显示一次，请及时保存。${paused}`,
    async () => {
      const d = await api("keys/" + k.id + "/reset", {method:"POST"});
      $("key-bind-dialog").close();
      showNewKey(d.key, "KEY RESET", "密钥已重置");
      await refresh();
    });
});
// 新密钥明文展示（新建与重置共用）：明文只出现这一次，关闭即不可见。
// 注意 key-dialog 的公共结构：表单面板与 created-key 面板各自带 body，
// 但 footer 只有一个（created-key-footer）——切面板时同步切 footer，
// 否则会同时看到「创建密钥」和「复制密钥」两组按钮。
function showNewKey(value, eyebrow, title) {
  const dlg = $("key-dialog");
  $("created-key").querySelector(".eyebrow").textContent = eyebrow;
  $("created-key").querySelector("h2").textContent = title;
  $("new-key-value").textContent = value;
  $("key-form").hidden = true;
  $("created-key").hidden = false;
  $("created-key-footer").hidden = false;
  dlg.showModal();
}
$("key-form").addEventListener("submit", async e => { e.preventDefault(); e.submitter.disabled = true; try { const d = await api("keys", {method:"POST",body:{name:$("key-name").value}}); showNewKey(d.key, "KEY CREATED", "保存你的新密钥"); await refresh(); } catch (err) { $("key-error").textContent = err.message; } finally { e.submitter.disabled = false; } });
async function copy(value) { try { await navigator.clipboard.writeText(value); toast("已复制"); } catch { toast("无法自动复制，请手动选择文本复制"); } }
$("copy-key").addEventListener("click", () => copy($("new-key-value").textContent));
document.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", () => copy(b.dataset.copy)));
$("base-url").textContent = location.origin + "/v1"; $("anthropic-url").textContent = location.origin;
// -- 接口行内调试（用户拍板：接入指南每个接口行带「调试」按钮）-------------
// GET /v1/models：走 /admin/api/debug/models —— 服务端用临时 test_key 打
// **真实公开接口**（同一条 check_api 鉴权路径），把状态码/耗时/原样 body 带回。
// 不从浏览器直接 fetch：那需要客户端完整 key，而 admin API 不会下发它（安全）。
// POST 接口会真实消耗上游额度且需要请求体，引导到「连接测试」页（那里已有
// 完整的模型选择与响应展示），不在这里重复一份。
async function debugModels(btn) {
  btn.disabled = true; btn.textContent = "请求中…";
  try {
    const r = await api("debug/models");
    $("model-debug-meta").textContent = `${r.status} · ${r.ms}ms` + (r.count !== null && r.count !== undefined ? ` · ${r.count} 个模型` : "");
    $("model-debug-output").textContent = r.body;
    $("model-debug-dialog").showModal();
  } catch (err) { toast("请求失败：" + err.message); }
  finally { btn.disabled = false; btn.textContent = "调试"; }
}
document.querySelectorAll("[data-endpoint-debug]").forEach(b => b.addEventListener("click", () => {
  const ep = b.dataset.endpointDebug;
  if (ep === "models") { debugModels(b); return; }
  // chat / responses / messages：跳连接测试页。models 下拉在测试页打开时
  // 会随 refresh 填充，这里只需切页。
  goPage("test");
}));
$("copy-base").addEventListener("click", () => copy(location.origin + "/v1")); $("copy-anthropic").addEventListener("click", () => copy(location.origin));
$("host-label").textContent = location.host;
$("models-refresh").addEventListener("click", async () => {
  $("models-refresh").disabled = true; $("models-refresh").textContent = "拉取中…";
  try { await api("models/refresh", {method:"POST"}); await refresh(); toast("模型列表已更新"); }
  catch (e) { toast(e.message); }
  finally { $("models-refresh").disabled = false; $("models-refresh").innerHTML = '<span>↻</span> 立即拉取'; }
});
// 页签点击：切到某个上游。页签条由 renderModels 重画（refresh 后计数也要更新），
// 所以这里只改状态 + 立即重渲染一次，不等下一次轮询。
$("models-tabs").addEventListener("click", e => {
  const tab = e.target.closest("[data-model-tab]");
  if (!tab) return;
  const next = tab.dataset.modelTab;
  if (next === modelsTab) return;
  modelsTab = next;
  renderModels();
});
$("models-body").addEventListener("click", async e => {
  // 状态列开关：切换**这一行**（一个上游下的一个模型）。
  //
  // 去前缀前这里是「一次作用于同名模型的全部后端」——那时同名跨后端被合并成
  // 一行，只能批量切。现在按上游分页、各管各的，一行就是一个后端上的一条，
  // 逐个切才对（用户拍板「各管各的、不做合并」），也不再需要「已切 2/3 个」的
  // 部分失败话术。
  const toggle = e.target.closest("[data-model-toggle]");
  if (toggle) {
    const mid = toggle.dataset.modelToggle;
    const enabled = toggle.dataset.enabled === "1";
    // backend 由渲染时写进 dataset：同名模型两边都有，PATCH 必须指名是哪一行
    // （去前缀后裸名不唯一确定后端，服务端要求显式 backend）。
    const backend = toggle.dataset.modelBackend || undefined;
    const verb = enabled ? "启用" : "禁用";
    toggle.disabled = true;
    try {
      await api("models/" + encodeURIComponent(mid), {method:"PATCH", body:{enabled, backend}});
      await refresh();
      toast(`已${verb}「${mid}」`);
    } catch (err) {
      await refresh();
      toast(`${verb}「${mid}」失败：${err.message}`);
    }
    return;
  }
  const del = e.target.closest("[data-model-delete]");
  if (del) {
    const mid = del.dataset.modelDelete;
    if (!confirm(`删除自定义模型 ${mid}？`)) return;
    del.disabled = true;
    // backend 由渲染时写进 dataset：同名模型两边都有，删除必须指名是哪一行。
    const backend = del.dataset.modelBackend || undefined;
    try { await api("models/" + encodeURIComponent(mid), {method:"DELETE", body:{backend}}); await refresh(); toast(`已删除 ${mid}`); }
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
// v16：id 是上游裸名，不再有「带前缀 ⇒ 自带归属」这回事 —— 归属一律由多选框
// 决定，所以这个 input 监听**不再收敛选项**，只负责把「被环境禁用的后端」置灰。
$("model-id").addEventListener("input", () => {
  $("model-backend-options").querySelectorAll("[data-model-backend]").forEach(input => {
    input.disabled = backends.find(x => x.name === input.dataset.modelBackend)?.enabled === false;
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
  // 归属完全由多选框决定：v16 起 id 是**上游裸名**（不再有 `trae/` 之类的前缀
  // 能自带归属），所以「带前缀的 ID 自动收敛为一个后端」那段逻辑已删除。
  const chosen = backends.filter(b => b.enabled !== false
      && $("model-backend-options")?.querySelector(`[data-model-backend="${b.name}"]`)?.checked);
  const targets = chosen.length ? chosen : [backends[0]];
  const button = $("model-submit");
  button.disabled = true;
  const failed = [];
  let saved = 0;
  try {
    for (const backend of targets) {
      try {
        // id 是裸名，归属靠 body 的 `backend` 指定（同名模型两边各一行）。
        await api("models", {method: "POST", body: {id, backend: backend.name, ...fields}});
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
// 用量页 tab（用户 2026-10-17）：按后端筛，空 = 全部后端汇总。
// 与模型页 tab 的区别：**有「全部」页签且默认选中** —— 用量是总账，
// 先看合计再下沉到单个后端更自然（模型页按用户拍板无全部 tab）。
let usageBackend = "";
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
  if (usageBackend) q.set("backend", usageBackend);
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
  paintUsageTabs();
}
// 画用量页 tab：全部（默认）+ 每个后端一个。统计由服务端按 backend 裁，
// 所以 count-badge 用当前区间该后端的记录数（loadUsage 拿到 report 后回填）。
function paintUsageTabs(counts) {
  const box = $("usage-tabs");
  if (!box) return;
  const total = counts && counts.__all__ != null ? counts.__all__ : null;
  const tabs = [{name: "", label: "全部", all: true}]
    .concat(backends.map(b => ({name: b.name, label: b.label || b.name})));
  box.innerHTML = tabs.map(t => {
    const sel = t.name === usageBackend;
    const n = t.all ? total : counts && counts[t.name];
    return `<button class="model-tab${sel ? " selected" : ""}" role="tab"`
      + ` aria-selected="${sel ? "true" : "false"}" data-usage-tab="${esc(t.name)}"`
      + ` title="只看 ${t.all ? "全部后端的用量汇总" : esc(t.label) + " 的用量"}">`
      + `${t.all ? "" : backendIconSvg(t.name)}<span>${esc(t.label)}</span>`
      + (n == null ? "" : `<span class="count-badge">${n.toLocaleString("zh-CN")}</span>`)
      + `</button>`;
  }).join("");
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
      // 回填 tab 上的记录数：只按当前区间统计，切 tab / 改区间后由下一次 loadUsage 更新。
      const byAcc = report.group_by === "account" ? (report.groups || []) : null;
      const counts = {__all__: report.total ? report.total.count : 0};
      if (byAcc) {
        for (const g of byAcc) {
          const hit = (overview && overview.accounts || []).find(a => a.id === g.bucket);
          const key = hit ? hit.backend : null;
          if (key) counts[key] = (counts[key] || 0) + g.count;
        }
      }
      paintUsageTabs(counts);
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
      account_id: $("usage-account").value || null,
      backend: usageBackend || null}});
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
// 用量 tab 点击（用户 2026-10-17）：切后端重新请求（统计走服务端 backend 参数）。
document.addEventListener("click", e => {
  const tab = e.target.closest("[data-usage-tab]");
  if (!tab || tab.dataset.usageTab === usageBackend) return;
  usageBackend = tab.dataset.usageTab;
  paintUsageTabs();
  loadUsage(0);
});
$("usage-filter").addEventListener("submit", e => { e.preventDefault(); loadUsage(0); });

/* ---------- 每日积分消费弹框（用户 2026-10-19）----------
 * 两个入口：仪表盘「账号总积分」卡片（全站每日）与账号行的积分数字
 * （该账号每日）。数据与接口全部复用用量明细页那一套：
 *   GET /admin/api/usage/report?group_by=day&start=<unix>[&account_id=<aid>]
 * 数据库零改动 —— upstream_request_usage 里已经有按天的全部数据，
 * account 维度还命中 idx_upstream_account_time 前导列。
 */
let creditAccountId = "", creditDays = 30;
// start 用 unix 秒：_query_time 认秒也认 'YYYY-MM-DD'，但前端统一走秒更省心。
const creditStart = () => {
  if (!creditDays) return null;
  return Math.floor(Date.now() / 1000) - creditDays * 86400;
};
function openCreditDaily(accountId) {
  creditAccountId = accountId || "";
  const a = creditAccountId ? (overview.accounts || []).find(x => x.id === creditAccountId) : null;
  $("credit-daily-title").textContent = a ? `账号「${accountLabel(a)}」的每日积分` : "每日积分消费";
  $("credit-daily-sub").textContent = "上游账单口径 · 北京时间 · 加载中…";
  $("credit-daily-body").innerHTML = '<tr><td colspan="4" class="history-empty">加载中…</td></tr>';
  $("credit-daily-total").textContent = "";
  paintCreditRange();
  $("credit-daily-dialog").showModal();
  loadCreditDaily();
}
function paintCreditRange() {
  document.querySelectorAll("[data-credit-range]").forEach(b => {
    b.setAttribute("aria-pressed", String((Number(b.dataset.creditRange) || 0) === creditDays));
  });
}
async function loadCreditDaily() {
  const q = new URLSearchParams({group_by: "day"});
  const start = creditStart();
  if (start) q.set("start", start);
  if (creditAccountId) q.set("account_id", creditAccountId);
  try {
    const report = await api("usage/report?" + q.toString());
    renderCreditDaily(report);
  } catch (e) {
    $("credit-daily-body").innerHTML = `<tr><td colspan="4" class="history-empty">${esc(e.message)}</td></tr>`;
    $("credit-daily-sub").textContent = "上游账单口径 · 北京时间";
  }
}
function renderCreditDaily(report) {
  const groups = report.groups || [];
  const total = report.total || {};
  const days = report.bucket_total ?? groups.length;
  $("credit-daily-sub").textContent =
    `上游账单口径 · 北京时间 · 共 ${days} 天有记录`;
  // 行与「用量明细」页的分组报表一致：日期 / 条数 / 积分 / 免费 / 付费。
  $("credit-daily-body").innerHTML = groups.map(g =>
    `<tr><td class="mono">${esc(g.bucket)}</td><td class="align-right mono">${g.count}</td>`
    + `<td class="align-right mono">${creditsFmt(g.credit ?? 0)}</td>`
    + `<td class="align-right mono muted">${g.free} / ${g.paid}</td></tr>`).join("")
    || '<tr><td colspan="4" class="history-empty">这段时间没有账单记录。</td></tr>';
  // 合计取 report.total（服务端同条件全量聚合，不受 500 组上限影响）。
  $("credit-daily-total").textContent = total.count
    ? `合计 ${total.count.toLocaleString("zh-CN")} 条 · ${creditsFmt(total.credit ?? 0)} 积分 · 免费 ${total.free ?? 0} / 付费 ${total.paid ?? 0}`
    : "";
}
// 档位切换：只改 start，重新拉一次。全部 = 不传 start。
document.addEventListener("click", e => {
  const b = e.target.closest("[data-credit-range]");
  if (!b) return;
  creditDays = Number(b.dataset.creditRange) || 0;
  paintCreditRange();
  loadCreditDaily();
});
// 弹框内补算（用户 2026-10-19 拍板要按钮）：复用用量页的 sync，成功后再刷新表格。
$("credit-daily-sync").addEventListener("click", async () => {
  const btn = $("credit-daily-sync");
  btn.disabled = true; const label = btn.textContent; btn.textContent = "拉取中…";
  try {
    const r = await api("usage/sync", {method: "POST", body: {
      start: creditStart(), account_id: creditAccountId || null}});
    const skipped = (r.skipped || []).length;
    toast(skipped ? `已同步 ${r.synced} 个账号，跳过 ${skipped} 个（${r.message || "刚拉取过或任务进行中"}）`
                  : `已同步 ${r.synced} 个账号`);
    await loadCreditDaily();
  } catch (err) {
    toast(err.message);
  } finally {
    btn.textContent = label; btn.disabled = false;
  }
});
// 仪表盘总积分卡片 → 全站每日。
$("dash-credit-card").addEventListener("click", () => openCreditDaily(""));

/* ---------- 单个密钥的请求记录 ---------- */
let keyReqId = null, keyReqOffset = 0, keyReqSize = 10, keyReqDay = null;
async function loadKeyRequests(offset) {
  if (!keyReqId) return;
  keyReqOffset = Math.max(0, offset);
  // 记下这次请求属于哪个密钥/哪一页：快速在密钥间点击时，先发的迟到响应
  // 会覆盖后发的列表，导致「显示的记录不属于当前密钥」。
  const requestedId = keyReqId, requestedOffset = keyReqOffset;
  const day = keyReqDay;
  $("key-requests-body").innerHTML = '<tr><td colspan="8" class="history-empty">加载中…</td></tr>';
  try {
    // 从用量历史点进来时带 since/until，只显示选中那天；从「请求」按钮进来
    // 不带时间条件，显示最近的记录。
    const window = day ? `&since=${day[0]}&until=${day[1]}` : "";
    const data = await api(`logs?limit=${keyReqSize}&offset=${keyReqOffset}&key_id=${encodeURIComponent(keyReqId)}${window}`);
    if (requestedId !== keyReqId || requestedOffset !== keyReqOffset) return;  // 已被更新的请求取代
    // 复用「请求日志」页的表格组件（用户 2026-10-17：直接用同一套，别再抄一份）。
    // 行结构与 key-requests-dialog 的表头一致：时间/模型/账号/状态/耗时（无密钥列）。
    $("key-requests-body").innerHTML = data.rows.map(r =>
      `<tr><td class="mono muted">${esc(stamp(r.time * 1000))}</td>`
      + `<td class="mono">${esc(r.model_id || "—")}</td>`
      + `<td>${r.account_name ? `<span class="account-cell-log">${backendIconSvg(r.backend)}<span>${esc(r.account_name)}</span></span>` : "—"}</td>`
      + `<td><span class="pill log-status ${r.ok ? 'green' : 'red'}" data-tip="${esc(outcomes[r.outcome] || '请求失败')}">${esc(r.status ?? "—")}</span></td>`
      + tokenCells(r)
      + `<td class="align-right mono">${Math.round(Number(r.duration_ms) || 0).toLocaleString("zh-CN")} ms</td></tr>`).join("")
      || '<tr><td colspan="8" class="history-empty">该密钥还没有请求记录。</td></tr>';
    const pages = Math.max(1, Math.ceil(data.total / keyReqSize));
    const current = Math.floor(data.offset / keyReqSize) + 1;
    $("key-requests-info").textContent = data.total ? `第 ${current} / ${pages} 页 · 共 ${data.total.toLocaleString("zh-CN")} 条` : "暂无记录";
    $("key-requests-prev").disabled = data.offset <= 0;
    $("key-requests-next").disabled = data.offset + keyReqSize >= data.total;
  } catch (err) {
    if (requestedId !== keyReqId) return;
    $("key-requests-body").innerHTML = `<tr><td colspan="8" class="history-empty">${esc(err.message)}</td></tr>`;
  }
}
$("key-requests-prev").addEventListener("click", () => loadKeyRequests(keyReqOffset - keyReqSize));
$("key-requests-next").addEventListener("click", () => loadKeyRequests(keyReqOffset + keyReqSize));

/* ---------- 密钥用量历史 ---------- */
$("keys-body").addEventListener("click", async e => {
  const reqCell = e.target.closest("[data-requests]");
  if (reqCell) {
    keyReqId = reqCell.dataset.requests;
    keyReqDay = null;
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
      `<tr><td class="mono muted">${esc(d.day)}</td><td class="align-right mono"><button class="linklike" data-usage-day="${esc(d.day)}" data-usage-kid="${esc(kid)}" title="查看这一天的请求明细">${d.count}</button></td>`
      + `<td class="align-right mono">${fmtTokens(d.tokens)}</td></tr>`).join("")
      || '<tr><td colspan="3" class="history-empty">近 30 天还没有记录。</td></tr>';
    $("usage-history-total").innerHTML = series.length
      ? `区间合计：${sum.count.toLocaleString("zh-CN")} 次 · ${fmtTokens(sum.tokens)} tokens（共 ${series.length} 天有记录）`
      + ` · <button class="linklike" data-usage-all="${esc(kid)}" title="查看该密钥最近的请求记录（不限日期）">查看全部请求</button>`
      : "";
  } catch (err) { $("usage-history-body").innerHTML = `<tr><td colspan="3" class="history-empty">${esc(err.message)}</td></tr>`; }
});

/* 用量历史弹框里点某天的请求数 → 打开该密钥当天的请求明细。 */
$("usage-history-body").addEventListener("click", async e => {
  const cell = e.target.closest("[data-usage-day]");
  if (!cell) return;
  const day = cell.dataset.usageDay, kid = cell.dataset.usageKid;
  const k = overview.keys.find(x => x.id === kid);
  // 当天 00:00（本地时区）起算；截止取次日 00:00 之前，让最后一秒的请求也在内。
  const from = new Date(`${day}T00:00:00`).getTime() / 1000 | 0;
  const to = from + 86400;
  keyReqId = kid;
  $("key-requests-name").textContent = `密钥「${k ? k.name : kid}」 ${day} 的请求明细`;
  $("key-requests-dialog").showModal();
  keyReqDay = [from, to];
  await loadKeyRequests(0);
});

/* 「查看全部请求」：该密钥不限日期的最近请求（分页）。 */
$("usage-history-total").addEventListener("click", async e => {
  const btn = e.target.closest("[data-usage-all]");
  if (!btn) return;
  const kid = btn.dataset.usageAll;
  const k = overview.keys.find(x => x.id === kid);
  keyReqId = kid;
  keyReqDay = null;
  $("key-requests-name").textContent = `密钥「${k ? k.name : kid}」的最近请求`;
  $("key-requests-dialog").showModal();
  await loadKeyRequests(0);
});

(async () => { loadVersion(); try { const s = await api("session"); csrf = s.csrf; await enter(); } catch (e) { if (!csrf) showLogin(); } })();
