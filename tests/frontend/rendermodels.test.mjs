// 用**真实**的 /admin/api/overview 数据跑模型页的渲染（按上游分页、逐行渲染），
// 断言产出的 HTML 里各后端自己的值都在、且没有被合成一个数。
//
// 不依赖浏览器：只把要测的几个纯函数抠出来求值。这能抓住模板层面的错误
// （字段名写错 → undefined、渲染出「未知」徽标、把 0 显示成 ×0 等），
// 这些正是「在浏览器里看起来不对」的根因。
//
// 数据来源是 WB_TEST_DB 指向的 state.db 所在目录（默认 /tmp/opencode/v7fresh，
// 一份迁移过的生产库副本）；拿不到就跳过，不让环境问题伪装成代码问题。
// **必须从仓库根目录运行。**
import { readFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// 相对本文件定位 app.js，避免写死 worktree 的绝对路径。
const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, "..", "..", "admin", "static", "app.js");
const src = readFileSync(SRC, "utf8");

const DB_ROOT = process.env.WB_TEST_DB || "/tmp/opencode/v7fresh";
if (!existsSync(join(DB_ROOT, "management", "state.db"))) {
  console.log(`跳过：未找到 ${DB_ROOT}/management/state.db（可用 WB_TEST_DB 指定）。`);
  process.exit(0);
}

const snippets = [
  // 后端元数据（需求 2）：来自服务端，前端只留消费口。
  /let backends = \[\];\n/,
  /function applyBackends\(list\) \{[\s\S]*?\n\}\n/,
  /const backendMeta = [^\n]*\n/,
  /const backendIcon = [^\n]*\n/,
  /const backendName = [^\n]*\n/,
  /const SVG_ALLOWED_TAGS = [^\n]*\n/,
  /const backendIconSvg = [\s\S]*?\n\};\n/,
  /const backendLabel = [^\n]*\n/,
  /function backendBadge\(b\) \{[\s\S]*?\n\}\n/,
  /const creditsText = [^\n]*\n/,
  /const ctxText = [^\n]*\n/,
  // 按上游分页后的两个渲染函数（合并展示的 groupModelsByName / renderModelGroup
  // 已随用户拍板删除，这里不再抽）。
  /function backendIsVisible\(name\) \{[\s\S]*?\n\}\n/,
  /function visibleBackends\(\) \{[\s\S]*?\n\}\n/,
  /function paintModelsTabs\(tabs, list\) \{[\s\S]*?\n\}\n/,
  /function renderModelRow\(m\) \{[\s\S]*?\n\}\n/,
  /function batchTargets\(rows, enabled\) \{[\s\S]*?\n\}\n/,
  /const modelKey = m =>[^\n]*\n/,
  /function checkedModelRows\(rows\) \{[\s\S]*?\n\}\n/,
  /function paintBatchButtons\(rows\) \{[\s\S]*?\n\}\n/,
  /let modelsTab = [^\n]*\n/,
  /function renderModels\(\) \{[\s\S]*?\n\}\n/,
  /const stamp = [^\n]*\n/,
];
let code = "";
for (const re of snippets) {
  const m = src.match(re);
  if (!m) { console.error("抽不出片段:", re); process.exit(1); }
  code += m[0] + "\n";
}
// esc 是模板里到处用的转义函数，必须一起带上。
const escMatch = src.match(/const esc = [^\n]*\n/);
code += escMatch[0];

// renderModels 要写 DOM（$("models-badge") / $("models-body") / $("models-note") /
// $("models-tabs")）。用最小的桩：只记内容，不做解析。
const dom = {};
const $ = id => (dom[id] ??= { textContent: "", innerHTML: "", hidden: false, classList: { add(){}, remove(){} } });
const fn = new Function("$", "overview", code + `
  return {renderModelRow, backendIsVisible, visibleBackends, paintModelsTabs, renderModels, applyBackends, esc,
          backendIconSvg, backendBadge,
          backendsForTest: () => backends, getTab: () => modelsTab, setTab: v => { modelsTab = v; },
          getDom: () => dom, batchTargets, paintBatchButtons, modelKey, checkedModelRows};
`)($, undefined);

// 注入与 GET /admin/api/backends 同构的元数据（真实服务返回的就是这份形状）。
// icon_svg 与本项目两个后端的真实声明同形（viewBox 24×24 + currentColor）。
const WB_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M12 2.6 21.4 12 12 21.4 2.6 12z"/><path d="M12 9.2 14.8 12 12 14.8 9.2 12z" fill="currentColor" stroke="none"/></svg>';
const TRAE_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M13.2 2.2 5.6 13.1h5.1l-.9 8.7 7.6-10.9h-5.1z"/></svg>';
fn.applyBackends([
  { name: "workbuddy", label: "WorkBuddy", short: "WB", icon: "◈", icon_svg: WB_SVG, color: "#2f6fed", order: 0, login_kind: "browser", enabled: true, default: true },
  { name: "trae", label: "Trae", short: "Trae", icon: "⚡", icon_svg: TRAE_SVG, color: "#7c4dff", order: 1, login_kind: "trae", enabled: true, default: false },
]);

// 拉真实数据：直接调 overview（复用既已验证的登录方式）。
const PY = process.env.WB_TEST_PYTHON || "/tmp/opencode/wb-venv/bin/python";
const raw = execFileSync(PY, [
  "-c", `
import asyncio, sys, json
sys.path.insert(0, ${JSON.stringify(join(here, "..", ".."))})
from pathlib import Path
import httpx
ROOT=Path(${JSON.stringify(DB_ROOT)})
async def m():
    from admin import server as s
    app=s.create_app(ROOT/'management', ROOT/'auth','sk-test-client-key','admin-key-long-enough-1234')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
        r=await c.post('/admin/api/login', json={'key':'admin-key-long-enough-1234'})
        c.headers['Cookie']=r.headers.get('set-cookie','').split(';')[0]
        ov=(await c.get('/admin/api/overview')).json()
        print(json.dumps(ov['models']['models'], ensure_ascii=False))
asyncio.run(m())
`], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
const models = JSON.parse(raw.trim().split("\n").pop());

let bad = 0;
const check = (label, cond, extra = "") => {
  if (!cond) bad++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${label}${cond ? "" : "   " + extra}`);
};

// ---- 真实数据本身的形状（去前缀后必须成立） ----
console.log(`真实模型 ${models.length} 条`);
check("每条都有 backend（归属由后端列决定）", models.every(m => typeof m.backend === "string" && m.backend));
// **不变量 1**：`/v1/models` 的自造前缀已去掉。上游自带 `/` 的名字（`openrouter/free`）
// 是**真实模型名**，必须保留 —— 所以只断言「没有本项目的后端前缀」，不是「没有斜杠」。
const leaks = models.filter(m => m.id.startsWith("trae/"));
check("没有任何 id 还带 trae/ 前缀", leaks.length === 0, leaks.map(m => m.id).join(", "));
// 同名模型在两个上游各是一条（联合主键的可见后果）。
const byId = {};
for (const m of models) (byId[m.id] ??= []).push(m);
const dups = Object.entries(byId).filter(([, v]) => v.length > 1);
check("存在同名跨上游的行（否则联合主键无从体现）", dups.length >= 1,
      "数据里没有同名模型，需库里有 Trae 与 WB 的同名条目");
check("每条同名行的 backend 互不相同（没有重复行）",
      dups.every(([, v]) => new Set(v.map(m => m.backend)).size === v.length),
      dups.map(([k, v]) => `${k}: ${v.map(m => m.backend).join("+")}`).join("; "));

// ---- 分页：一个上游页签只画自己的行，且不画归属 icon ----
const renderAll = backend => models.filter(m => m.backend === backend).map(m => fn.renderModelRow(m)).join("\n");
const wbHtml = renderAll("workbuddy");
const traeHtml = renderAll("trae");
check("WB 页渲染没有 undefined", !/undefined/.test(wbHtml));
check("Trae 页渲染没有 undefined", !/undefined/.test(traeHtml));
check("渲染没有「未知」徽标（说明 backend 字段都在）", !/未知/.test(wbHtml + traeHtml));
// 单页里整列都是同一归属，再画归属 icon 是噪声（用户拍板）。
check("WB 页不画归属 icon", !wbHtml.includes("account-icon-backend"));
check("Trae 页不画归属 icon", !traeHtml.includes("account-icon-backend"));
// 每行的 id 都必须是裸名（页内没有前缀可言）。
const wbIds = [...wbHtml.matchAll(/data-model-toggle="([^"]*)"/g)].map(m => m[1]);
const traeIds = [...traeHtml.matchAll(/data-model-toggle="([^"]*)"/g)].map(m => m[1]);
check(`WB 页 ${wbIds.length} 行的 id 都不带前缀`, wbIds.every(i => !i.startsWith("trae/")));
check(`Trae 页 ${traeIds.length} 行的 id 都不带前缀`, traeIds.every(i => !i.startsWith("trae/")));
// 每一行都带上自己的 backend（PATCH 必须指名是哪一行，裸名不唯一确定后端）。
// 用 `>=`：自定义行会多一个删除按钮，它同样带 backend，属于正确行为。
check("WB 页每行的开关都带 backend=workbuddy",
      (wbHtml.match(/data-model-backend="workbuddy"/g) || []).length >= wbIds.length);
check("Trae 页每行的开关都带 backend=trae",
      (traeHtml.match(/data-model-backend="trae"/g) || []).length >= traeIds.length);
// 反向：WB 页里绝不该出现 trae 的 backend（分页漏过滤会串页）。
check("WB 页不出现 trae 的 backend", !wbHtml.includes('data-model-backend="trae"'));
check("Trae 页不出现 workbuddy 的 backend", !traeHtml.includes('data-model-backend="workbuddy"'));

// 逐条核对：每个模型自己的值都要出现在它那一行里（没有被平均/取首个）。
for (const m of models) {
  const row = fn.renderModelRow(m);
  const rate = m.credits === 0 ? "免费" : m.credits === null ? "标准" : "×" + m.credits;
  check(`${m.backend} / ${m.id} 的倍率「${rate}」在 HTML 里`, row.includes(rate), row.slice(0, 200));
  check(`${m.backend} / ${m.id} 的行主标题是裸名 id`, row.includes(`<strong>${m.id}</strong>`), row.slice(0, 200));
}

// step-5-preview 是最危险的一组：免费与付费在两边。分页后它们各在自己页里，
// 值必须原样保留（成本账本与排队降级按它判断免费/付费）。
const step = models.filter(m => m.id === "step-5-preview");
if (step.length) {
  const rates = [...new Set(step.map(m => fn.renderModelRow(m).match(/pill[^>]*>([^<]*)</)?.[1]))];
  console.log(`  step-5-preview 在两个上游的倍率：${rates.join(" / ")}`);
  check("step-5-preview 的每一条都渲染出自己的倍率",
        step.every(m => fn.renderModelRow(m).includes(m.credits === 0 ? "免费" : `×${m.credits}`)));
} else {
  console.log("  跳过 step-5-preview 断言：数据里没有");
}

// ---- 页签：只列可见上游，关闭的上游整条消失 ----
// renderModels 读 overview；用一个真实形状的 overview 驱动它。
const overviewStub = { models: { models, updated: 1700000000 } };
const fn2 = new Function("$", "overview", code + `
  return {renderModels, applyBackends, getTab: () => modelsTab};
`)($, overviewStub);
fn2.applyBackends([
  { name: "workbuddy", label: "WorkBuddy", short: "WB", icon: "◈", icon_svg: WB_SVG, color: "#2f6fed", order: 0, enabled: true, default: true },
  { name: "trae", label: "Trae", short: "Trae", icon: "⚡", icon_svg: TRAE_SVG, color: "#7c4dff", order: 1, enabled: true, default: false },
]);
fn2.renderModels();
check("首次渲染后选中第一个可见上游", fn2.getTab() === "workbuddy", fn2.getTab());
check("页签条画出了两个上游", (dom["models-tabs"].innerHTML.match(/data-model-tab=/g) || []).length === 2);
check("表格里只画当前页签的行（WB）",
      !dom["models-body"].innerHTML.includes('data-model-backend="trae"'));
check("计数与当前页签的行数一致",
      dom["models-badge"].textContent === `${models.filter(m => m.backend === "workbuddy" && m.enabled).length} / ${models.filter(m => m.backend === "workbuddy").length}`,
      dom["models-badge"].textContent);

// 关掉 Trae（enabled=false）→ 页签与它的行一起消失；当前页签被关时自动切换。
const fn3 = new Function("$", "overview", code + `
  return {renderModels, applyBackends, getTab: () => modelsTab, setTab: v => { modelsTab = v; }};
`)($, overviewStub);
fn3.applyBackends([
  { name: "workbuddy", label: "WorkBuddy", icon: "◈", icon_svg: WB_SVG, color: "#2f6fed", order: 0, enabled: true, default: true },
  { name: "trae", label: "Trae", icon: "⚡", icon_svg: TRAE_SVG, color: "#7c4dff", order: 1, enabled: false, default: false },
]);
fn3.setTab("trae"); // 当前正看着 Trae，此时它被关掉
fn3.renderModels();
check("关掉的上游页签消失", !dom["models-tabs"].innerHTML.includes('data-model-tab="trae"'));
check("只剩一个可见页签", (dom["models-tabs"].innerHTML.match(/data-model-tab=/g) || []).length === 1);
check("当前页签被关闭时自动切到可见页签", fn3.getTab() === "workbuddy", fn3.getTab());
check("被关闭上游的行也不在表格里", !dom["models-body"].innerHTML.includes('data-model-backend="trae"'));
check("被关闭上游的行不参与计数",
      dom["models-badge"].textContent === `${models.filter(m => m.backend === "workbuddy" && m.enabled).length} / ${models.filter(m => m.backend === "workbuddy").length}`,
      dom["models-badge"].textContent);

// 单后端部署（只有 WB）：页签仍要工作，不因「只有一个」而退化成不可用。
const single = new Function("$", "overview", code + `
  return {renderModels, applyBackends, getTab: () => modelsTab};
`)($, overviewStub);
single.applyBackends([
  { name: "workbuddy", label: "WorkBuddy", icon: "◈", icon_svg: WB_SVG, color: "#2f6fed", order: 0, enabled: true, default: true },
]);
single.renderModels();
check("单后端也有一个页签（Trae 下线后必须照常可用）",
      (dom["models-tabs"].innerHTML.match(/data-model-tab=/g) || []).length === 1);
check("单后端页签指向默认后端", dom["models-tabs"].innerHTML.includes('data-model-tab="workbuddy"'));

// 全部上游都关掉 → 页签条隐藏、表格给出说明而不是空白（空表格会被当成「模型全丢了」）。
const none = new Function("$", "overview", code + `
  return {renderModels, applyBackends};
`)($, overviewStub);
none.applyBackends([
  { name: "workbuddy", label: "WorkBuddy", icon: "◈", icon_svg: WB_SVG, order: 0, enabled: false, default: true },
]);
none.renderModels();
check("没有可见上游 → 页签条隐藏", dom["models-tabs"].hidden === true);
check("没有可见上游 → 表格给出说明文字", /没有启用的上游/.test(dom["models-body"].innerHTML), dom["models-body"].innerHTML.slice(0, 160));

// ---- 图标安全：SVG 是当 HTML 插进 DOM 的（绕开 esc），必须自带结构校验 ----
const badIconCases = [
  ["带 script", '<svg viewBox="0 0 24 24"><script>alert(1)</script></svg>'],
  ["带 on* 事件", '<svg viewBox="0 0 24 24" onload="alert(1)"><path d="M0 0"/></svg>'],
  ["带 href", '<svg viewBox="0 0 24 24"><a href="javascript:alert(1)"/></svg>'],
  ["多根元素", '<svg viewBox="0 0 24 24"><path d="M0 0"/></svg><b>x</b>'],
  ["非 svg 根", '<div>hi</div>'],
  ["空串", ""],
];
for (const [label, bad] of badIconCases) {
  fn.applyBackends([
    { name: "workbuddy", label: "WorkBuddy", short: "WB", icon: "◈", icon_svg: bad, color: "#2f6fed", order: 0, enabled: true, default: true },
    { name: "trae", label: "Trae", short: "Trae", icon: "⚡", icon_svg: TRAE_SVG, color: "#7c4dff", order: 1, enabled: true, default: false },
  ]);
  const out = fn.backendIconSvg("workbuddy");
  check(`坏 icon_svg（${label}）回落成字符、不注入`,
        out.includes("backend-icon-text") && !out.includes("<script") && !out.includes("onload"),
        out);
}
// 合法 SVG 必须原样放行（不能被误判成坏输入），且打上 class 供 CSS 定尺寸。
fn.applyBackends([
  { name: "workbuddy", label: "WorkBuddy", short: "WB", icon: "◈", icon_svg: WB_SVG, color: "#2f6fed", order: 0, enabled: true, default: true },
  { name: "trae", label: "Trae", short: "Trae", icon: "⚡", icon_svg: TRAE_SVG, color: "#7c4dff", order: 1, enabled: true, default: false },
]);
const okSvg = fn.backendIconSvg("trae");
check("合法 icon_svg 原样输出且带 class",
      okSvg.includes('class="backend-icon-svg"') && okSvg.includes("<path"), okSvg);
check("SVG 不带 width/height（尺寸全交给 CSS）",
      !/[\s"](?:width|height)=/.test(okSvg.split(">")[0]), okSvg.split(">")[0]);

// ---- 徽标 ----
check("backendBadge(null) 显示 —", fn.backendBadge(null).includes("—"));
check("backendBadge('trae') 显示 Trae", fn.backendBadge("trae").includes("Trae"));
check("backendBadge('workbuddy') 显示 WB", fn.backendBadge("workbuddy").includes("WB"));

// ---- 状态列即开关：文案是**当前状态**，data-enabled 是**点下去的目标态** ----
// 目标态写反了会一次把整页模型全打开，是会造成线上放量的错误。
const onRow = fn.renderModelRow({ id: "m", backend: "workbuddy", enabled: true, credits: 1, max_input_tokens: 1000 });
check("启用行文案是「启用」", onRow.includes(">启用</button>"), onRow.slice(0, 200));
check("启用行目标态为 0（点一下禁用）", onRow.includes('data-enabled="0"'));
check("状态是可点的 button（不是 span）", onRow.includes('<button class="pill model-state-toggle green"'));
const offRow = fn.renderModelRow({ id: "m", backend: "workbuddy", enabled: false, credits: 1, max_input_tokens: 1000 });
check("禁用行文案是「禁用」", offRow.includes(">禁用</button>"));
check("禁用行目标态为 1（点一下启用）", offRow.includes('data-enabled="1"'));
// 操作列只剩删除（批量开关随合并展示一起删了；单行开关在状态列）。
check("上游模型的操作列为空", !/<td class="align-right">[^<]/.test(onRow), onRow.slice(-120));
check("自定义模型的操作列有删除按钮",
      fn.renderModelRow({ id: "x", backend: "workbuddy", custom: true, enabled: true }).includes('data-model-delete="x"'));

// ---- 行体 td 数与表头一致（重排列时最容易漏改表头，页面不报错只是永久错位） ----
const cellCount = (onRow.match(/<td/g) || []).length;
check("模型行恰好 7 个 td（勾选列 + 6 数据列）", cellCount === 7, `实际 ${cellCount} 个`);
const indexHtml = readFileSync(join(here, "..", "..", "admin", "static", "index.html"), "utf8");
const modelHead = /<table class="models-table">[\s\S]*?<thead><tr>([\s\S]*?)<\/tr><\/thead>/.exec(indexHtml);
check("模型表有 class=\"models-table\"（列宽靠它按 nth-child 定）", Boolean(modelHead));
if (modelHead) {
  const heads = [...modelHead[1].matchAll(/<th[^>]*>([\s\S]*?)<\/th>/g)].map(m => m[1].trim());
  check("表头 7 列且列序为 勾选/模型/来源/倍率/上下文/官方描述/状态",
        heads.length === 7 && heads[0].includes("models-check-all")
        && heads[3] === "倍率" && heads[4] === "上下文长度"
        && heads[5] === "官方描述" && heads[6] === "状态",
        JSON.stringify(heads));
  check("没有「操作」表头（操作列已取消）", !heads.includes("操作"), JSON.stringify(heads));
  check("表头列数 == 行体 td 数", heads.length === cellCount, `表头 ${heads.length} / 行体 ${cellCount}`);
}
check("行内不再画列图标（◭/⌘ 已随文本表头删除）",
      !onRow.includes("◭") && !onRow.includes("⌘"));
check("官方描述在第 6 个 td", /<td class="model-desc-cell">/.test(onRow));
check("行首有勾选框（批量只作用于勾选行）", /class="model-check" data-model-key=/.test(onRow), onRow.slice(0, 120));

// 页签条容器必须在 HTML 里（否则 JS 找不到它，整页静默不渲染页签）。
check("index.html 有 #models-tabs 容器", indexHtml.includes('id="models-tabs"'));
check("页签容器在表格之前（视觉上在表头上方）",
      indexHtml.indexOf('id="models-tabs"') < indexHtml.indexOf('class="models-table"'));

console.log(bad ? `=== ${bad} 个失败 ===` : "=== 真实数据渲染全部通过 ===");
process.exit(bad ? 1 : 0);
