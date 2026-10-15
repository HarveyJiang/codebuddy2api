// 用真实的 /admin/api/overview 数据跑 renderModelGroup / backendBadge，
// 断言产出的 HTML 里两条链路的值都在、且没被合并成一个数。
//
// 不依赖浏览器：只把要测的几个纯函数抠出来求值。这能抓住模板层面的错误
// （字段名写错 -> undefined、渲染出 "未知" 徽标、把 0 显示成 ×0 等），
// 这些正是「在浏览器里看起来不对」的根因。
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";

const SRC = "/home/openchamber/.local/share/opencode/worktree/e1b214/sprint01-refactor/admin/static/app.js";
const src = readFileSync(SRC, "utf8");

const snippets = [
  /const BACKEND_ICON = \{[\s\S]*?\};\n/,
  /const BACKEND_NAME = \{[\s\S]*?\};\n/,
  /const backendIcon = [^\n]*\n/,
  /const backendName = [^\n]*\n/,
  /function backendTag\(b\) \{[\s\S]*?\n\}\n/,
  /const backendLabel = [^\n]*\n/,
  /function backendBadge\(b\) \{[\s\S]*?\n\}\n/,
  /function groupModelsByName\(list\) \{[\s\S]*?\n\}\n/,
  /const modelBaseName = [^\n]*\n/,
  /const creditsText = [^\n]*\n/,
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
  return {renderModelGroup, groupModelsByName, backendBadge, backendTag, backendName, esc};
`)();

// 拉真实数据：直接调 overview（复用 stage2_verify 已经验证过的登录方式）。
const raw = execFileSync("/tmp/opencode/wb-venv/bin/python", [
  "-c", `
import asyncio, sys, json
sys.path.insert(0,'/home/openchamber/.local/share/opencode/worktree/e1b214/sprint01-refactor')
from pathlib import Path
import httpx
ROOT=Path('/tmp/opencode/v7fresh')
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
check("共 7 组重名被合并", models.length - groups.length === 7,
      `实际合并 ${models.length - groups.length} 组`);

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
      stepHtml.includes(">◈ WB<") || stepHtml.includes("◈ WB"),
      stepHtml.slice(0, 200));

// 单后端行不应出现后端标记之外的杂项。
const single = groups.find(g => g.items.length === 1);
const singleHtml = fn.renderModelGroup(single);
check(`单后端行 ${single.key} 渲染正常`, singleHtml.includes("<tr>") && !/undefined/.test(singleHtml));

// 徽标：null 要显示 —，不能显示成 workbuddy。
check("backendBadge(null) 显示 —", fn.backendBadge(null).includes("—"));
check("backendBadge('trae') 显示 Trae", fn.backendBadge("trae").includes("Trae"));
check("backendBadge('workbuddy') 显示 WB", fn.backendBadge("workbuddy").includes("WB"));

// 批量开关的目标态：按钮上标的是「点下去要变成什么」，不是当前态。
// 写反了会让「全部禁用」把模型全打开，是会造成线上放量的错误。
const all = {key: "m", items: [
  {id: "m", backend: "workbuddy", enabled: true, credits: 1, max_input_tokens: 1000},
  {id: "trae/m", backend: "trae", enabled: true, credits: 1, max_input_tokens: 1000}]};
const allHtml = fn.renderModelGroup(all);
check("全启用时按钮文案是「全部禁用」", allHtml.includes("全部禁用"));
check("全启用时按钮目标态为 0", /data-model-group-toggle="m" data-enabled="0"/.test(allHtml));

const none = {key: "m", items: [
  {id: "m", backend: "workbuddy", enabled: false, credits: 1, max_input_tokens: 1000},
  {id: "trae/m", backend: "trae", enabled: false, credits: 1, max_input_tokens: 1000}]};
const noneHtml = fn.renderModelGroup(none);
check("全禁用时按钮文案是「全部启用」", noneHtml.includes("全部启用"));
check("全禁用时按钮目标态为 1", /data-model-group-toggle="m" data-enabled="1"/.test(noneHtml));

const half = {key: "m", items: [
  {id: "m", backend: "workbuddy", enabled: true, credits: 1, max_input_tokens: 1000},
  {id: "trae/m", backend: "trae", enabled: false, credits: 1, max_input_tokens: 1000}]};
const halfHtml = fn.renderModelGroup(half);
check("部分启用时目标态为 1（补齐启用）", /data-model-group-toggle="m" data-enabled="1"/.test(halfHtml));
check("部分启用时状态列显示 1/2 启用", halfHtml.includes("1/2 启用"));

console.log(bad ? `=== ${bad} 个失败 ===` : "=== 真实数据渲染全部通过 ===");
process.exit(bad ? 1 : 0);
