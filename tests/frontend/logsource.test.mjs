// 请求日志的「来源」切换与 v26 对账三列 —— 前端契约。
//
// 对应 v26（upstream_model / account_uid / client_request_id 三列 +
// `GET /admin/api/logs/upstream`）。后端语义由
// `tests/admin/test_log_reconcile.py` 守着，这里守**前端这一侧**的约定：
//
//   1. **来源可切**：本代理的请求（all/api/test）与上游自己记的流水
//      （upstream）必须能在同一页切换 —— 用户报「日志记 A、云端却是 B」，
//      正是因为这两套视角之前根本没法并排看。
//   2. **不新增列**：表格已经横向溢出（用户 2026-10-17 报过），三列新字段
//      全部走悬停提示，不占表格宽度。
//   3. **上游行有独立的呈现**：没有 HTTP 状态码、没有耗时，徽标显示
//      「上游记录」，耗时列显示「—」而不是「0 ms」。
//   4. **翻译表要盖住上游那一类**：后端会返回 outcome=upstream_record，
//      少一条翻译就退化成「请求失败」。
//
// 用法：**必须从仓库根目录运行**（与其它 frontend 测试一致）。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const js = readFileSync(join(root, "admin", "static", "app.js"), "utf8");
const html = readFileSync(join(root, "admin", "static", "index.html"), "utf8");

let bad = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) { bad++; console.log(`  ✗  ${label}\n       得到 ${JSON.stringify(got)}\n       期望 ${JSON.stringify(want)}`); }
  else console.log(`  ok ${label}`);
}

// --- 1. 来源下拉存在，且四个取值齐全 --------------------------------------
check("页面有来源下拉", html.includes('id="log-source"'), true);
for (const value of ["all", "api", "test", "upstream"]) {
  check(`下拉里有 ${value} 选项`, html.includes(`<option value="${value}">`), true);
}
check("下拉在工具条里（不是塞进表格）", /id="log-source"[\s\S]{0,800}id="logs-refresh"/.test(html), true);

// --- 2. 前端确实按来源切端点 ---------------------------------------------
check("有 logSource() 读下拉", /function logSource\(\)/.test(js), true);
check("上游报表走 logs/upstream 端点", js.includes("logs/upstream?limit="), true);
check("api/test 走 source= 参数", /source=\$\{/.test(js) && js.includes("encodeURIComponent"), true);
check("切来源回到第一页", /\$\("log-source"\)\.addEventListener\("change", \(\) => loadLogs\(0\)\)/.test(js), true);

// --- 3. v26 三列进悬停提示，不占表格 --------------------------------------
check("提示里带上游模型名", js.includes("r.upstream_model"), true);
check("提示里带账号 uid", js.includes("r.account_uid"), true);
check("提示里带本方请求 id", js.includes("r.client_request_id"), true);
check("三列都有中文标签", js.includes("上游模型 ") && js.includes("账号UID ") && js.includes("本方ID "), true);

// --- 4. 上游行有独立的呈现 ----------------------------------------------
check("翻译表有 upstream_record", /\bupstream_record\s*:/.test(js), true);
check("上游行单开一个徽标分支", /r\.source === "upstream"/.test(js), true);
check("上游提示带调用方", js.includes("调用方 "), true);
check("上游提示带内容摘要", js.includes("内容 "), true);
check("上游提示带积分", js.includes("积分 "), true);
check("耗时列对空值显示破折号", /function durationCell\(r\)/.test(js) && js.includes('align-right mono">—</td>'), true);

// --- 5. 没有新增表格列 ----------------------------------------------------
const logSection = (html.split('id="page-logs"')[1] || "").split("</section>")[0];
// 注意：`<th` 会连 `<thead>` 一起匹配上，所以要用 `<th[\s>]`。
const header = (logSection.split("</thead>")[0] || "").match(/<th[\s>]/g) || [];
check("表头仍是 10 列", header.length, 10);
check("空态仍是 colspan=10", /colspan="10"/.test(js), true);
check("没有为 v26 加新列", /<th[^>]*>上游模型<\/th>|<th[^>]*>账号 ?UID<\/th>|<th[^>]*>本方ID<\/th>/.test(html), false);

if (bad) {
  console.log(`\n✗ ${bad} 项未通过`);
  process.exit(1);
}
console.log("\n✓ 全部通过");
