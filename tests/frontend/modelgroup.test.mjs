// 模型管理页的「按上游分页签」逻辑单测（用户 2026-10-16 拍板）。
//
// 这一组测试的来历：v16 去前缀之前，同名模型（`glm-5.3` 在 WorkBuddy 与 Trae
// 各有一个）驱动了一套「跨后端合并成一行」的展示 —— 前端按 `canonical` 归组、
// 一行里并排画两个后端的倍率，批量的启用/禁用要一次作用到「这个名字的全部后端」。
//
// 用户改成「按上游分页签、各管各的、不做合并」后那整套被删了。这里钉住的是
// **取而代之的三条不变量**：
//
//   1. 页签 = 可见后端（`enabled !== false`），**无「全部」页签**；
//   2. 关掉的上游 → 页签与它的行**一起**消失（计数不能数出看不见的行）；
//   3. 一个页签里只画一个上游的行，**不再画归属 icon**（整页同一归属，画了是噪声）。
//
// 还有一条容易漏的：当前页签被关闭时要**自动切到可见页签** —— 否则界面上
// 一个页签都没选中，表格却是空的，看着像「模型全丢了」。
import { readFileSync } from "node:fs";
const src = readFileSync("admin/static/app.js", "utf8");

// 抽取几个纯函数。app.js 里 const 箭头函数与 function 混用、行尾还有分号差异，
// 所以每个片段各有明确的起止串。
function slice(from, to) {
  const start = src.indexOf(from);
  if (start < 0) throw new Error(`找不到 ${from}`);
  const end = src.indexOf(to, start);
  if (end < 0) throw new Error(`找不到 ${from} 的结尾 ${to}`);
  return src.slice(start, end + to.length);
}

const code = [
  slice("function backendIsVisible(name) {", "\n}"),
  slice("function visibleBackends() {", "\n}"),
  slice("function paintModelsTabs(tabs, list) {", "\n}"),
  slice("const creditsText = m =>", ";\n"),
  slice("const ctxText = m =>", ";\n"),
  slice("function renderModelRow(m) {", "\n}"),
  // 元数据驱动：这几个 helper 是 /admin/api/backends 的唯一消费口。
  slice("let backends = [];", "\n"),
  slice("function applyBackends(list) {", "\n}"),
  slice("const backendMeta = b =>", ";\n"),
  slice("const backendName = b =>", ";\n"),
  slice("const backendIcon = b =>", ";\n"),
  slice("const SVG_ALLOWED_TAGS = ", ";\n"),
  slice("const backendIconSvg = b => {", "\n};\n"),
  slice("const esc = v =>", ";\n"),
  slice("const CREDITS_ICON = ", ";\n"),
  slice("const CTX_ICON = ", ";\n"),
  slice("let modelsTab = ", ";\n"),
].join("\n");

// 页签渲染要写 DOM（$("models-tabs")）；测试里给一个最小的桩，只记录 innerHTML。
const box = { hidden: false, innerHTML: "" };
const code2 = code.replace(/const \$ = .*\n/, "");
const api = new Function("$", code2 + "\nreturn {backendIsVisible, visibleBackends, paintModelsTabs, renderModelRow, applyBackends, creditsText,"
  + " getTab: () => modelsTab, setTab: v => { modelsTab = v; }};")(
  id => (id === "models-tabs" ? box : null)
);
const { backendIsVisible, visibleBackends, paintModelsTabs, renderModelRow, applyBackends, creditsText, getTab, setTab } = api;
// 页签渲染要读 modelsTab 才知道哪个 selected；测「选中态」时先设好再画。
setTab("workbuddy");
// 与 GET /admin/api/backends 的返回同构（顺序故意打乱，验证 applyBackends 会排序）。
applyBackends([
  { name: "trae", label: "Trae", short: "Trae", icon: "⚡", color: "#7c4dff", order: 1, login_kind: "trae", enabled: true, default: false },
  { name: "workbuddy", label: "WorkBuddy", short: "WB", icon: "◈", color: "#2f6fed", order: 0, login_kind: "browser", enabled: true, default: true },
]);

let bad = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) bad++;
  console.log(`${ok ? "  ok" : "FAIL"}  ${label}${ok ? "" : `\n       得到 ${JSON.stringify(got)}\n       期望 ${JSON.stringify(want)}`}`);
};

// ---- 1. 页签 = 可见后端，无「全部」页签 ----
check("两个后端都启用 → 两个页签", visibleBackends().map(b => b.name), ["workbuddy", "trae"]);
check("页签顺序来自元数据 order（workbuddy 在前）", visibleBackends()[0].name, "workbuddy");
check("没有「全部」这个后端", visibleBackends().some(b => b.name === "all" || b.name === "全部"), false);

// 服务端返回 enabled:false 表示「该上游已被管理员关闭」（总闸 ∧ 环境逃生门）。
applyBackends([
  { name: "trae", label: "Trae", icon: "⚡", color: "#7c4dff", order: 1, enabled: false, default: false },
  { name: "workbuddy", label: "WorkBuddy", icon: "◈", color: "#2f6fed", order: 0, enabled: true, default: true },
]);
check("关闭的 Trae 不出现在页签里", visibleBackends().map(b => b.name), ["workbuddy"]);
// 判据是**后端自己的 enabled**，不是「有没有模型行」：一个被关闭但目录里
// 仍有模型的后端必须隐藏页签，而不是显示成空页签。
check("被关闭的后端即使有模型行也不可见", backendIsVisible("trae"), false);
check("启用中的后端可见", backendIsVisible("workbuddy"), true);
// 拿不到元数据（老接口 / 后端被删）→ 默认可见：宁可多一个页签，
// 也不要让整个模型页因为缺一份元数据而空白（那会被当成「模型全丢了」）。
check("未知后端默认可见（不让页面空掉）", backendIsVisible("nope"), true);

// ---- 2. 页签上的计数：只数**本上游**的行，且不数被隐藏的上游 ----
applyBackends([
  { name: "trae", label: "Trae", icon: "⚡", color: "#7c4dff", order: 1, enabled: true, default: false },
  { name: "workbuddy", label: "WorkBuddy", icon: "◈", color: "#2f6fed", order: 0, enabled: true, default: true },
]);
const list = [
  { id: "glm-5.3", backend: "workbuddy", credits: 0.1, max_input_tokens: 200000, enabled: true, source: "upstream" },
  { id: "glm-5.3", backend: "trae", credits: 0.2, max_input_tokens: 200000, enabled: false, source: "upstream" },
  { id: "hy3", backend: "workbuddy", credits: 0, max_input_tokens: 128000, enabled: true, source: "upstream" },
];
paintModelsTabs(visibleBackends(), list);
check("页签条被画出来（非 hidden）", box.hidden, false);
check("页签数 = 可见后端数", (box.innerHTML.match(/data-model-tab=/g) || []).length, 2);
check("页签里不出现后端名字面量以外的硬编码（用 label）", box.innerHTML.includes("WorkBuddy"), true);
// workbuddy: 2 条全开 → 2 / 2；trae: 1 条禁用 → 0 / 1。
check("workbuddy 页签计数 2 / 2", /data-model-tab="workbuddy"[\s\S]*?<span class="count-badge">2 \/ 2<\/span>/.test(box.innerHTML), true);
check("trae 页签计数 0 / 1（禁用行也计入总数）", /data-model-tab="trae"[\s\S]*?<span class="count-badge">0 \/ 1<\/span>/.test(box.innerHTML), true);
// 选中态：当前页签带 .selected + aria-selected（可访问性，别只靠颜色）。
check("当前页签标记 selected", /class="model-tab selected"[^>]*data-model-tab="workbuddy"/.test(box.innerHTML), true);
check("当前页签 aria-selected=true", /data-model-tab="workbuddy"[^>]*aria-selected="true"|aria-selected="true"[^>]*data-model-tab="workbuddy"/.test(box.innerHTML)
  || (box.innerHTML.match(/aria-selected="true"/g) || []).length === 1, true);
setTab("trae");
paintModelsTabs(visibleBackends(), list);
check("切到 trae 后 selected 跟着走", box.innerHTML.includes('class="model-tab selected" role="tab" aria-selected="true" data-model-tab="trae"'), true);
check("切到 trae 后只有一个 selected", (box.innerHTML.match(/class="model-tab selected"/g) || []).length, 1);
setTab("workbuddy");

// 关掉 Trae → 页签只剩一个；trae 的行也不在可见列表里（由 renderModels 过滤，
// 这里用它过滤后喂给 paintModelsTabs，验证不会再出现 trae 的页签）。
applyBackends([
  { name: "trae", label: "Trae", icon: "⚡", color: "#7c4dff", order: 1, enabled: false, default: false },
  { name: "workbuddy", label: "WorkBuddy", icon: "◈", color: "#2f6fed", order: 0, enabled: true, default: true },
]);
const visibleList = list.filter(m => backendIsVisible(m.backend));
paintModelsTabs(visibleBackends(), visibleList);
check("关掉 Trae 后只剩一个页签", (box.innerHTML.match(/data-model-tab=/g) || []).length, 1);
check("关掉 Trae 后没有 trae 页签", box.innerHTML.includes('data-model-tab="trae"'), false);
check("关掉 Trae 后它的行也被过滤掉", visibleList.some(m => m.backend === "trae"), false);

// 完全没有可见后端 → 页签条整块隐藏（不给一个空的 role=tablist）。
applyBackends([{ name: "trae", label: "Trae", icon: "⚡", order: 1, enabled: false, default: true }]);
paintModelsTabs(visibleBackends(), []);
check("没有可见后端 → 页签条隐藏", box.hidden, true);

// ---- 3. 单行渲染：一个页签里只有同一归属，**不画归属 icon** ----
applyBackends([
  { name: "trae", label: "Trae", icon: "⚡", color: "#7c4dff", order: 1, enabled: true, default: false },
  { name: "workbuddy", label: "WorkBuddy", icon: "◈", color: "#2f6fed", order: 0, enabled: true, default: true },
]);
const row = renderModelRow({ id: "glm-5.3", backend: "workbuddy", credits: 0.1, max_input_tokens: 200000, enabled: true, source: "upstream", name: "GLM-5.3" });
// 归属 icon（.account-icon-backend）是「多后端并排」时代的产物：整页都是同一
// 归属时每行再画一遍会读成噪声，用户已拍板去掉。
check("单行不再画归属 icon", row.includes("account-icon-backend"), false);
check("单行显示裸名 id", row.includes("glm-5.3"), true);
check("单行状态列是开关（带 data-model-toggle）", row.includes("data-model-toggle"), true);
check("开关带 backend（去前缀后裸名不唯一确定后端）", row.includes('data-model-backend="workbuddy"'), true);
check("启用的行按钮为「启用」（点一下禁用）", row.includes('data-enabled="0"'), true);
check("单行不出现跨后端合并的「N 个后端」话术", row.includes("个后端"), false);

const rowOff = renderModelRow({ id: "hy3", backend: "workbuddy", credits: 0, max_input_tokens: 128000, enabled: false, source: "upstream" });
check("禁用的行按钮为「禁用」（点一下启用）", rowOff.includes('data-enabled="1"'), true);
check("倍率 0 显示「免费」", rowOff.includes("免费"), true);

const rowCustom = renderModelRow({ id: "my-model", backend: "trae", credits: null, enabled: true, source: "custom", custom: true });
check("自定义项有删除按钮（带 backend）", rowCustom.includes('data-model-delete="my-model"'), true);
check("自定义项删除按钮带 backend", rowCustom.includes('data-model-backend="trae"'), true);
const rowUpstream = renderModelRow({ id: "glm-5.3", backend: "workbuddy", credits: 0.1, enabled: true, source: "upstream" });
check("上游模型没有删除按钮", rowUpstream.includes("data-model-delete"), false);

// 倍率措辞：0 必须显示「免费」而不是「×0」（否则用户以为收费但为 0）。
check("倍率 0 显示免费", creditsText({ credits: 0 }), "免费");
check("倍率 null 显示标准", creditsText({ credits: null }), "标准");
check("倍率 0.48 原样显示", creditsText({ credits: 0.48 }), "×0.48");

// ---- 4. 被删的那套不能再回来 ----
// 合并展示的整套（归组 / 行内并排多后端）已随「按上游分页」删除。
// 若有人把它加回来，两套展示会打架（一行既有归属 icon 又分页），且它们是
// 静默冲突 —— 没有人会报错，只是页面同时呈现两种组织方式。
for (const dead of ["groupModelsByName", "renderModelGroup", "modelsInGroup", "modelBaseName", "model-group-toggle"]) {
  check(`已删除的 ${dead} 不再出现`, src.includes(dead + "(") || src.includes("data-" + dead) || new RegExp(`\\b${dead}\\b\\s*[=(]`).test(src), false);
}

console.log(bad ? `=== ${bad} 个失败 ===` : "=== 按上游分页签全部通过 ===");
process.exit(bad ? 1 : 0);
