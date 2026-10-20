// 请求日志的错误码分类展示 —— 前端契约。
//
// 对应 v24（`status` 与 `error_code` 分列 + outcome 细化）。后端语义由
// `tests/admin/test_log_error_classification.py` 与
// `tests/pool/test_account_pool.py` 守着，这里守**前端这一侧**的约定：
//
//   1. **业务码不与 HTTP 状态码混在一格里**。上游码可能是 11128 这种
//      数字，也可能是 USER_LIMIT 这种文本 —— 混进状态徽标会让人以为
//      「HTTP 状态是 11128」。所以：徽标只放 status，码挂下面的小字。
//   2. **不新增列**。表格已经横向溢出（用户 2026-10-17 报过），业务码走
//      徽标下面的小字 + 悬停提示。
//   3. **悬停提示要带全三样**：业务码、结果分类、来源 IP/UA —— 少了哪一样
//      都要另开一个弹框才看得到。
//   4. **前端不自己判定分类**：分类的唯一权威是后端的 outcome，前端只做
//      「翻译成人话」；翻译表必须盖住后端会返回的每一个取值。
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

// --- 1. 分类翻译表盖住后端会返回的每一个取值 ---------------------------
// 后端 admin/metrics.py 的 classify_outcome 只会返回这些值；
// 少一个 → 那一类在界面上退化成「请求失败」，等于白分类。
const OUTCOMES = ["success", "model_cooling", "no_account", "client_error",
                  "rate_limited", "auth_error", "quota_exhausted",
                  "upstream_error", "waf_blocked", "queue",
                  "stream_error", "interrupted", "http_error"];
for (const name of OUTCOMES) {
  check(`翻译表里有 ${name}`, new RegExp(`\\b${name}\\s*:`).test(js), true);
}
check("翻译表是老式的字面量映射（不是散落的 if）", js.includes("const outcomes={"), true);

// --- 2. 业务码单独一小字，不塞进状态徽标 -------------------------------
check("有 errorCodeCell", js.includes("function errorCodeCell(r)"), true);
check("业务码渲染成「码 xxx」的小字",
  /码 \$\{esc\(code\)\}/.test(js), true);
check("业务码小字带 title 提示",
  js.includes('title="上游业务错误码：${esc(code)}"'), true);
check("业务码为空时不渲染（老日志是 NULL）",
  /if \(!code\) return "";/.test(js), true);
check("徽标里只放 HTTP 状态码", /esc\(r\.status \?\? "—"\)/.test(js), true);

// --- 3. 悬停提示带全三样 -----------------------------------------------
check("有 logTip", js.includes("function logTip(r)"), true);
check("提示里带业务码", js.includes('parts.push("业务码 " + code);'), true);
check("提示里带结果分类", js.includes("outcomes[r.outcome] || \"请求失败\""), true);
check("提示里带来源 IP/UA", js.includes("sourceTip(r)"), true);
check("提示内容进属性前转义", js.includes('data-tip="${esc(logTip(r))}"'), true);

// --- 4. 三处日志表共用同一个状态列 -------------------------------------
check("请求日志页用 statusCell", js.includes("${statusCell(r)}"), true);
const statusCellUses = (js.match(/\$\{statusCell\(r\)\}/g) || []).length;
check("状态列共三处（请求日志页 / 账号日志弹框 / 密钥日志弹框）",
  statusCellUses, 3);
check("旧的「只显示 outcome 或来源」写法已下线",
  js.includes("sourceTip(r) || outcomes[r.outcome]"), false);

// --- 5. 没有新增列（表格已经横向溢出） ----------------------------------
const dashHeaders = html.slice(html.indexOf('id="dash-recent-body"'));
check("请求日志表头没有多出「业务码」列",
  /业务码|error_code/.test(html.slice(html.indexOf("dash-recent"), html.indexOf("dash-recent") + 2000)), false);
check("状态列占位仍是 1 格（没把行撑宽）",
  js.includes('colspan="10"'), true);

console.log(bad ? `=== ${bad} 项不符 ===` : "=== 日志错误码分类前端检查全部通过 ===");
process.exit(bad ? 1 : 0);
