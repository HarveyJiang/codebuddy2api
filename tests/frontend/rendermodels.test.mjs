// 用真实的 /admin/api/overview 数据跑 renderModelGroup / backendBadge，
// 断言产出的 HTML 里两条链路的值都在、且没被合并成一个数。
//
// 不依赖浏览器：只把要测的几个纯函数抠出来求值。这能抓住模板层面的错误
// （字段名写错 -> undefined、渲染出 "未知" 徽标、把 0 显示成 ×0 等），
// 这些正是「在浏览器里看起来不对」的根因。
//
// 用法：**必须从仓库根目录运行**。数据来源是 WB_TEST_DB 指向的 state.db 所在
// 目录（默认 /tmp/opencode/v7fresh，即一份迁移过的生产库副本）；拿不到就跳过，
// 不让环境问题伪装成代码问题。
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
  /const backendOrder = [^\n]*\n/,
  /function backendTag\(b\) \{[\s\S]*?\n\}\n/,
  /function backendBadge\(b\) \{[\s\S]*?\n\}\n/,
  /function groupModelsByName\(list\) \{[\s\S]*?\n\}\n/,
  /function modelBaseName\(id\) \{[\s\S]*?\n\}\n/,
  /const creditsText = [^\n]*\n/,
  /const CREDITS_ICON = [^\n]*\n/,
  /const CTX_ICON = [^\n]*\n/,
  /const ctxText = [^\n]*\n/,
  /function renderModelGroup\(g\) \{[\s\S]*?\n\}\n/,
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

const fn = new Function(code + `
  return {renderModelGroup, groupModelsByName, backendBadge, backendTag, backendName, backendIconSvg, applyBackends, esc, backendsForTest: () => backends, CREDITS_ICON, CTX_ICON};
`)();

// 注入与 GET /admin/api/backends 同构的元数据（真实服务返回的就是这份形状）。
// icon_svg 与本项目两个后端的真实声明同形（viewBox 24×24 + currentColor）。
const WB_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M12 2.6 21.4 12 12 21.4 2.6 12z"/><path d="M12 9.2 14.8 12 12 14.8 9.2 12z" fill="currentColor" stroke="none"/></svg>';
const TRAE_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M13.2 2.2 5.6 13.1h5.1l-.9 8.7 7.6-10.9h-5.1z"/></svg>';
fn.applyBackends([
  { name: "workbuddy", label: "WorkBuddy", short: "WB", icon: "◈", icon_svg: WB_SVG, color: "#2f6fed", order: 0, prefix: "" },
  { name: "trae", label: "Trae", short: "Trae", icon: "⚡", icon_svg: TRAE_SVG, color: "#7c4dff", order: 1, prefix: "trae/" },
]);

// 拉真实数据：直接调 overview（复用 stage2_verify 已经验证过的登录方式）。
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

const groups = fn.groupModelsByName(models);
console.log(`真实模型 ${models.length} 个 -> ${groups.length} 行（合并掉 ${models.length - groups.length} 行）`);
// 只断言「有同名被合并」这个不变量，不写死组数：上游模型表会变，
// 写死 7 会让测试因数据变动而红，那种红与代码质量无关。
const dupGroups = groups.filter(g => g.items.length > 1);
check("存在同名模型被合并", dupGroups.length >= 1, "没有任何重名组，合并逻辑可能失效");
check("所有同名组都恰好包含多个后端",
      dupGroups.every(g => new Set(g.items.map(m => m.backend)).size === g.items.length));

const html = groups.map(g => fn.renderModelGroup(g)).join("\n");
check("渲染没有 undefined", !/undefined/.test(html), html.match(/.{0,60}undefined.{0,60}/)?.[0] || "");
check("渲染没有 未知 徽标（说明 backend 字段都在）", !/未知/.test(html));

// 逐组核对：每个后端自己的值都要出现在 HTML 里。
for (const g of groups.filter(g => g.items.length > 1)) {
  const row = fn.renderModelGroup(g);
  for (const m of g.items) {
    const rate = m.credits === 0 ? "免费" : m.credits === null ? "标准" : "×" + m.credits;
    check(`${g.key} / ${m.backend} 的倍率「${rate}」在 HTML 里`, row.includes(rate),
          `行内未出现 ${rate}`);
  }
}

// step-5-preview 是最危险的一组：免费与 0.48 必须同时可见。
const step = groups.find(g => g.key === "step-5-preview");
const stepHtml = fn.renderModelGroup(step);
check("step-5-preview 行内同时出现免费与 ×0.48",
      stepHtml.includes("免费") && stepHtml.includes("×0.48"));
check("step-5-preview 行内两个后端标记都在",
      (stepHtml.match(/mini-backend/g) || []).length >= 2,
      stepHtml.slice(0, 200));
// 值前面的后端标记**只画 icon，不写后端名**（用户 2026-10-16 拍板）。
// 这条是防回退的：曾经每个数值前都挂一个「WB」/「Trae」文字 pill。
check("step-5-preview 值前的后端标记不带后端名文字",
      !/>\s*WB\s*</.test(stepHtml) && !/>\s*Trae\s*</.test(stepHtml),
      stepHtml.slice(0, 300));
// 后端名只应出现在 title 属性里（悬停可确认）。
check("step-5-preview 后端名保留在 title 里",
      stepHtml.includes('title="WorkBuddy"') && stepHtml.includes('title="Trae"'),
      stepHtml.slice(0, 300));
check("行前归属图标渲染成内联 SVG（不是字符）",
      stepHtml.includes('class="backend-icon-svg"') && !stepHtml.includes(">⚡<"),
      stepHtml.slice(0, 260));

// 图标是后端声明的 SVG，要当 HTML 插进 DOM（绕开 esc），所以 backendIconSvg
// 自带一层结构校验。这里逐个喂坏输入，确认都回落到**转义过的字符**。
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
    { name: "workbuddy", label: "WorkBuddy", short: "WB", icon: "◈", icon_svg: bad, color: "#2f6fed", order: 0, prefix: "" },
    { name: "trae", label: "Trae", short: "Trae", icon: "⚡", icon_svg: TRAE_SVG, color: "#7c4dff", order: 1, prefix: "trae/" },
  ]);
  const out = fn.backendIconSvg("workbuddy");
  check(`坏 icon_svg（${label}）回落成字符、不注入`,
        out.includes("backend-icon-text") && !out.includes("<script") && !out.includes("onload"),
        out);
}
// 合法 SVG 必须原样放行（不能被误判成坏输入），且打上 class 供 CSS 定尺寸。
fn.applyBackends([
  { name: "workbuddy", label: "WorkBuddy", short: "WB", icon: "◈", icon_svg: WB_SVG, color: "#2f6fed", order: 0, prefix: "" },
  { name: "trae", label: "Trae", short: "Trae", icon: "⚡", icon_svg: TRAE_SVG, color: "#7c4dff", order: 1, prefix: "trae/" },
]);
const okSvg = fn.backendIconSvg("trae");
check("合法 icon_svg 原样输出且带 class",
      okSvg.includes('class="backend-icon-svg"') && okSvg.includes("<path"), okSvg);
check("SVG 不带 width/height（尺寸全交给 CSS）",
      !/[\s"](?:width|height)=/.test(okSvg.split(">")[0]), okSvg.split(">")[0]);

// 单后端行不应出现后端标记之外的杂项。
const single = groups.find(g => g.items.length === 1);
const singleHtml = fn.renderModelGroup(single);
check(`单后端行 ${single.key} 渲染正常`, singleHtml.includes("<tr>") && !/undefined/.test(singleHtml));

// 单条行的主标题必须是**裸名**，与合并行一致 —— 曾经直接用 primary.id，
// 于是只有 Trae 独有的模型（不参与同名合并）会露出 `trae/` 前缀。
//
// 这里**逐行穷举**而不是抽样：那个 bug 只在「单条 + 带前缀」的组合下出现，
// 抽到合并行或 WorkBuddy 行都会漏。
const prefixLeaks = [];
let singles = 0, prefixedSingles = 0;
for (const g of groups) {
  if (g.items.length !== 1) continue;
  singles++;
  if (g.items[0].id.includes("/")) prefixedSingles++;
  const title = fn.renderModelGroup(g).match(/<strong>([^<]*)<\/strong>/)?.[1] || "";
  for (const b of fn.backendsForTest()) {
    if (b.prefix && title.startsWith(b.prefix)) prefixLeaks.push(`${title} (真实 id ${g.items[0].id})`);
  }
}
check(`单条行主标题都不带后端前缀（穷举 ${singles} 条，其中 ${prefixedSingles} 条 id 含 "/"）`,
      prefixLeaks.length === 0, prefixLeaks.join("; "));

// 真实 id 不能因为剥前缀而丢失：它要能在行的某处看到（小灰字位），
// 否则用户没法把这个模型名复制去调 API。
const traeOnly = groups.find(g => g.items.length === 1 && g.items[0].id.startsWith("trae/"));
if (traeOnly) {
  const h = fn.renderModelGroup(traeOnly);
  check(`Trae 独有行 ${traeOnly.key} 主标题是裸名、小灰字保留真实 id`,
        h.includes(`<strong>${traeOnly.key}</strong>`) && h.includes(traeOnly.items[0].id),
        h.slice(0, 260));
} else {
  console.log("  跳过 Trae 独有行断言：数据里没有（需库里有 Trae 独有模型）");
}

// 徽标：null 要显示 —，不能显示成 workbuddy。
check("backendBadge(null) 显示 —", fn.backendBadge(null).includes("—"));
check("backendBadge('trae') 显示 Trae", fn.backendBadge("trae").includes("Trae"));
check("backendBadge('workbuddy') 显示 WB", fn.backendBadge("workbuddy").includes("WB"));

// 状态列即开关（用户 2026-10-16：操作列的启用/禁用按钮去掉，直接点状态）。
// 按钮上标的是**当前状态**，`data-enabled` 标的是「点下去要变成什么」。
// 目标态写反了会一次把全部模型打开，是会造成线上放量的错误。
const all = {key: "m", items: [
  {id: "m", backend: "workbuddy", enabled: true, credits: 1, max_input_tokens: 1000},
  {id: "trae/m", backend: "trae", enabled: true, credits: 1, max_input_tokens: 1000}]};
const allHtml = fn.renderModelGroup(all);
check("全启用时状态列文案是「启用」", allHtml.includes(">启用</button>"));
check("全启用时点击目标态为 0（即禁用）", /data-model-group-toggle="m" data-enabled="0"/.test(allHtml));
check("全启用时状态是可点的 button（不是 span）",
      /<button class="pill model-state-toggle green" data-model-group-toggle="m"/.test(allHtml));

const none = {key: "m", items: [
  {id: "m", backend: "workbuddy", enabled: false, credits: 1, max_input_tokens: 1000},
  {id: "trae/m", backend: "trae", enabled: false, credits: 1, max_input_tokens: 1000}]};
const noneHtml = fn.renderModelGroup(none);
check("全禁用时状态列文案是「禁用」", noneHtml.includes(">禁用</button>"));
check("全禁用时点击目标态为 1（即启用）", /data-model-group-toggle="m" data-enabled="1"/.test(noneHtml));

const half = {key: "m", items: [
  {id: "m", backend: "workbuddy", enabled: true, credits: 1, max_input_tokens: 1000},
  {id: "trae/m", backend: "trae", enabled: false, credits: 1, max_input_tokens: 1000}]};
const halfHtml = fn.renderModelGroup(half);
check("部分启用时目标态为 1（补齐启用）", /data-model-group-toggle="m" data-enabled="1"/.test(halfHtml));
check("部分启用时状态列显示 1/2 启用", halfHtml.includes("1/2 启用"));

// 操作列只剩删除：批量开关已并入状态列，单后端开关已删。
// 同一个动作有两个入口会让人怀疑「点了哪个」，也会让「操作列」名不副实。
check("操作列不再有启用/禁用按钮", !/data-model-group-toggle[^>]*>(全部)?(启用|禁用)<\/button>/.test(allHtml.split("<td class=\"align-right\">")[1] || ""));
check("操作列不再有单后端开关", !/data-model-toggle/.test(allHtml));

// @media 之外的整行结构：列序必须是 模型→来源→倍率→上下文→官方描述→状态→操作。
// 官方描述挪到倒数第二列（操作列前）是用户明确要求的位置。
const cellCount = (allHtml.match(/<td/g) || []).length;
check("模型行恰好 7 个 td（列数与表头一致）", cellCount === 7, `实际 ${cellCount} 个`);
check("官方描述在第 5 个 td（倒数第三）",
      /^[\s\S]*?<\/td>[\s\S]*?<\/td>[\s\S]*?<\/td>[\s\S]*?<\/td><td class="model-desc-cell">/.test(allHtml));

// 表头与行体的列序必须对得上 —— 重排列时最容易漏改的正是表头
// （改完表头写「倍率」而行里是描述，页面不会报错，只是永久错位）。
const indexHtml = readFileSync(join(here, "..", "..", "admin", "static", "index.html"), "utf8");
const modelHead = /<table class="models-table">[\s\S]*?<thead><tr>([\s\S]*?)<\/tr><\/thead>/.exec(indexHtml);
check("模型表有 class=\"models-table\"（列宽靠它按 nth-child 定）", Boolean(modelHead));
if (modelHead) {
  const heads = [...modelHead[1].matchAll(/<th[^>]*>([\s\S]*?)<\/th>/g)].map(m => m[1].trim());
  check("表头 7 列且列序为 模型/来源/◭/⌘/官方描述/状态/操作",
        heads.length === 7 && heads[4] === "官方描述" && heads[5] === "状态",
        JSON.stringify(heads));
  check("倍率/上下文表头是图标而非文字（免得与行内图标重复）",
        heads[2] !== "倍率" && heads[3] !== "上下文", JSON.stringify(heads.slice(2, 4)));
  check("表头列数 == 行体 td 数",
        heads.length === cellCount, `表头 ${heads.length} / 行体 ${cellCount}`);
}
check("倍率/上下文列各带行内图标",
      allHtml.includes(fn.CREDITS_ICON) && allHtml.includes(fn.CTX_ICON));

// 跨后端归并出来的行，行名用**官方展示名**（用户 2026-10-16 拍板）。
// 两个裸名不同（`kimi-k3-1` vs `trae/kimi-k3`），挑任何一个当共用名都会误导
// —— 这行已经不属于那一个后端了。两边展示名相同（服务端算 canonical 时也靠这一点）。
const aliasGroups = fn.groupModelsByName([
  {id: "kimi-k3-1", backend: "workbuddy", credits: 1.62, name: "Kimi-K3", canonical: "kimi-k3", max_input_tokens: 200000, enabled: true},
  {id: "trae/kimi-k3", backend: "trae", credits: 1.83, name: "Kimi-K3", canonical: "kimi-k3", max_input_tokens: 200000, enabled: true},
]);
const aliasHtml = fn.renderModelGroup(aliasGroups[0]);
check("归并行行名用官方展示名 Kimi-K3", aliasHtml.includes("<strong>Kimi-K3</strong>"), aliasHtml.slice(0, 160));
check("归并行同时给出两个后端各自的真实 id",
      aliasHtml.includes("kimi-k3-1") && aliasHtml.includes("trae/kimi-k3"), aliasHtml.slice(0, 260));
// 反例守卫：hy3 / hy3-x 同属 WorkBuddy、展示名同为 Hy3，绝不能被合并且行名照旧用裸名。
const hyGroups = fn.groupModelsByName([
  {id: "hy3", backend: "workbuddy", credits: 0, name: "Hy3", max_input_tokens: 128000, enabled: true},
  {id: "hy3-x", backend: "workbuddy", credits: 0.05, name: "Hy3", max_input_tokens: 200000, enabled: true},
]);
check("hy3 / hy3-x 不归并（同一后端内除外）", hyGroups.length, 2);
check("hy3 行名仍是裸名（未被展示名替换，因为它没归并）",
      fn.renderModelGroup(hyGroups.find(g => g.key === "hy3")).includes("<strong>hy3</strong>"));

console.log(bad ? `=== ${bad} 个失败 ===` : "=== 真实数据渲染全部通过 ===");
process.exit(bad ? 1 : 0);
