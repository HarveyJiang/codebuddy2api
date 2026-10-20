// 成长任务页：菜单入口 + 页面骨架 + 前端调用后端的接口形状。
//
// 守三件事：
// 1) 侧边栏有「成长任务」入口且指向 page-growth（check_admin_ui.py 另会校验
//    nav↔page↔labels 一一对应，这里再钉一次「入口文案别被改掉」）；
// 2) 页面表格与弹框的容器 id 都在（app.js 靠它们渲染，改名会静默失效）；
// 3) 前端调用的接口路径与后端 admin/server.py 注册的一致。
import { readFileSync } from "node:fs";

const html = readFileSync("admin/static/index.html", "utf8");
const js = readFileSync("admin/static/app.js", "utf8");

let bad = 0;
function check(name, got, want) {
  const ok = got === want;
  if (!ok) bad++;
  console.log(`${ok ? "  ok" : "FAIL"}  ${name}${ok ? "" : `  期望 ${want}  得到 ${got}`}`);
}

// --- 菜单入口与页面容器 -------------------------------------------------
check("侧边栏有成长任务入口", /data-page="growth"[^>]*>.*成长任务/.test(html), true);
check("页面容器存在", /<section id="page-growth" class="page-panel" hidden>/.test(html), true);
check("labels 登记了 growth", /growth:\s*\[/.test(js), true);
check("goPage 会加载成长页", /next === "growth"\)\s*loadGrowth\(\)/.test(js), true);

// --- 表格与弹框容器 id ---------------------------------------------------
for (const id of ["growth-body", "growth-refresh", "growth-result",
                  "growth-dialog", "growth-dialog-title", "growth-dialog-sub",
                  "growth-travel", "growth-tasks-body"]) {
  check(`index.html 有 #${id}`, html.includes(`id="${id}"`), true);
  check(`app.js 引用了 #${id}`, js.includes(`$("${id}")`), true);
}

// --- 表格头：四类状态都要有列 -------------------------------------------
// 表头要在 page-growth 段落里找：账号池那张表也以「账号」开头，直接正则
// 全局匹配会抓到那张（列名是「状态 / 签到」），断言就永远不成立。
const growthSeg = /<section id="page-growth"[\s\S]*?<\/section>/.exec(html);
check("成长页段落存在", growthSeg !== null, true);
const head = growthSeg && /<thead><tr>([\s\S]*?)<\/tr>/.exec(growthSeg[0]);
check("账号表头存在", head !== null, true);
for (const col of ["签到", "保活", "活跃"]) {
  check(`表头含「${col}」`, head && head[1].includes(`<th>${col}</th>`), true);
}

// --- 接口形状（与 admin/server.py 注册的一致）---------------------------
// 前端用模板字面量拼 query，所以匹配的是反引号里的路径。
check("拉任务列表用 growth/tasks", js.includes("growth/tasks?account_id="), true);
check("拉旅行状态用 growth/travel", js.includes("growth/travel?account_id="), true);
check("动作走 growth/actions", js.includes('"growth/actions"'), true);
check("动作带 account_id", /growth\/actions"[\s\S]{0,80}account_id/.test(js), true);

// --- 设置项：旅行/活跃时点可配置 ---------------------------------------
check("设置页有旅行时点输入", html.includes('id="growth-travel-hours"'), true);
check("设置页有活跃时点输入", html.includes('id="growth-activity-hours"'), true);
check("读设置时取 travel hours", js.includes("overview.pool?.growth_travel_hours"), true);
check("提交设置时带 travel hours", js.includes('growth_travel_hours:$("growth-travel-hours")'), true);
check("提交设置时带 activity hours", js.includes('growth_activity_hours:$("growth-activity-hours")'), true);

console.log(bad ? `=== ${bad} 项不符 ===` : "=== 成长任务页检查全部通过 ===");
process.exit(bad ? 1 : 0);
