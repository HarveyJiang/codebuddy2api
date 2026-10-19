// 每日积分消费弹框（用户 2026-10-19）—— 两个入口 + 一个只读弹框。
//
// 要挡的回归都是「点了没反应」或「一下弹出两个框」这类真浏览器里才现形的：
//   1. 积分数字分支没排在 data-action 前面 → 点积分连带弹出账号详情；
//   2. URL 漏 group_by=day 或账号入口漏 account_id → 拉回来是全站/明细数据；
//   3. 弹框外壳（标题栏/关闭钮/滚动主体）不合规 → 标题栏跟着内容滚跑。
// 都是纯字符串/顺序断言，不依赖 DOM。
//
// 用法：**必须从仓库根目录运行**（与 pooltable / modelgroup 一致）。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "admin", "static", "app.js"), "utf8");
const html = readFileSync(join(here, "..", "..", "admin", "static", "index.html"), "utf8");
const css = readFileSync(join(here, "..", "..", "admin", "static", "style.css"), "utf8");

let bad = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) { bad++; console.log(`  ✗  ${label}\n       得到 ${JSON.stringify(got)}\n       期望 ${JSON.stringify(want)}`); }
  else console.log(`  ok ${label}`);
}

// ---- 1. 弹框骨架 ----
const dialog = /<dialog id="credit-daily-dialog">[\s\S]*?<\/dialog>/.exec(html);
check("弹框可抽取", dialog !== null, true);
const seg = dialog ? dialog[0] : "";
check("有公共标题栏", /class="dialog-heading"/.test(seg), true);
check("标题栏有关闭按钮", /close-dialog" aria-label="关闭"/.test(seg), true);
check("有滚动主体", /class="dialog-body"/.test(seg), true);
check("只读弹框取消底部栏（与其余只读弹框一致）", /class="dialog-footer/.test(seg), false);
check("底部不再有关闭按钮", /dialog-footer[\s\S]*close-dialog/.test(seg), false);
for (const id of ["credit-daily-title", "credit-daily-sub", "credit-daily-body", "credit-daily-total", "credit-daily-sync"]) {
  check(`有容器 ${id}`, seg.includes(`id="${id}"`), true);
}
check("有四个范围档位", (seg.match(/data-credit-range=/g) || []).length, 4);
check("默认档位是近 30 天", /data-credit-range="30" aria-pressed="true"/.test(seg), true);

// ---- 2. 两个入口 ----
check("仪表盘总积分卡片可点（包了 article）",
      /<article id="dash-credit-card"/.test(html), true);
check("卡片内仍是 dash-credits 数字（没被换掉）",
      /id="dash-credit-card"[\s\S]{0,200}id="dash-credits"/.test(html), true);
check("JS 接了卡片点击", /\$\("dash-credit-card"\)\.addEventListener\("click"/.test(src), true);
check("账号行积分数字可点且带 data-credits",
      /class="credit-number credit-link" data-credits="\$\{esc\(a\.id\)\}"/.test(src), true);
check("积分入口有 title 说明", /data-credits="\$\{esc\(a\.id\)\}" title="点击查看该账号每天的积分消费"/.test(src), true);

// ---- 3. 分派顺序：积分分支必须排在 data-action 之前 ----
// 账号行整行点击会开详情弹框；不拦这一下就会一次弹出两个框。
const at = s => src.indexOf(s);
check("accounts-body 回调里积分分支存在", at('closest("[data-credits]")') > -1, true);
check("积分分支排在 data-action 分支之前",
      at('closest("[data-credits]")') < at('closest("[data-action]")'), true);
check("积分分支命中后 return（不继续走详情）",
      /const credit = e\.target\.closest\("\[data-credits\]"\);\s*\n\s*if \(credit\) \{ openCreditDaily\(credit\.dataset\.credits\); return; \}/.test(src), true);
check("openCreditDaily 接受空串（全站入口）",
      /function openCreditDaily\(accountId\) \{[\s\S]{0,80}creditAccountId = accountId \|\| "";/.test(src), true);

// ---- 4. 接口用法 ----
check("报表走 group_by=day", /new URLSearchParams\(\{group_by: "day"\}\)/.test(src), true);
check("账号入口带 account_id", /if \(creditAccountId\) q\.set\("account_id", creditAccountId\)/.test(src), true);
check("start 用 unix 秒（天 × 86400）",
      /Math\.floor\(Date\.now\(\) \/ 1000\) - creditDays \* 86400/.test(src), true);
check("「全部」档位不传 start（null）", /if \(!creditDays\) return null;/.test(src), true);
check("复用 usage/report 接口", /api\("usage\/report\?" \+ q\.toString\(\)\)/.test(src), true);
check("合计取 report.total（不受 500 组上限影响）", /const total = report\.total \|\| \{\};/.test(src), true);
check("天数取 bucket_total", /report\.bucket_total \?\? groups\.length/.test(src), true);
check("积分展示复用 creditsFmt", /creditsFmt\(g\.credit \?\? 0\)/.test(src), true);
check("空态文案", /这段时间没有账单记录。/.test(src), true);
check("标题按入口切换（全站 / 账号「备注名」）",
      /a \? `账号「\$\{accountLabel\(a\)\}」的每日积分` : "每日积分消费"/.test(src), true);

// ---- 5. 档位切换 + 补算 ----
check("档位点击只改 creditDays 并重拉",
      /creditDays = Number\(b\.dataset\.creditRange\) \|\| 0;/.test(src)
      && /paintCreditRange\(\);\s*\n\s*loadCreditDaily\(\);/.test(src), true);
check("档位选中态走 aria-pressed", /b\.setAttribute\("aria-pressed"/.test(src), true);
check("补算按钮复用 usage/sync",
      /api\("usage\/sync", \{method: "POST"/.test(src), true);
check("补算按当前账号筛", /account_id: creditAccountId \|\| null/.test(src), true);
check("补算成功后刷新表格", /await loadCreditDaily\(\);/.test(src), true);
check("补算有按钮禁用态（防连点）", /btn\.disabled = true; const label = btn\.textContent/.test(src), true);
check("补算失败不设冷却（与用量页口径一致）", /catch \(err\) \{\s*\n\s*toast\(err\.message\);/.test(src), true);

// ---- 6. 样式 ----
check("积分数字是可点的 button（不是 span）", /\.credit-link\{[^}]*cursor:pointer/.test(css), true);
check("卡片有 hover 反馈", /\.credit-card:hover\{background:#f4f8f2\}/.test(css), true);
check("档位选中态换主色实底", /\.credit-range \.pill\[aria-pressed="true"\]\{[^}]*background:var\(--deep\)/.test(css), true);
check("合计行有分隔线", /\.credit-total\{[^}]*border-top:1px solid var\(--line\)/.test(css), true);
// button 继承 .credit-number 的字体：行高/字号不能被浏览器默认值改掉，
// 否则账号行积分会突然变大（用户 2026-10-16 调过的视觉）。
check("button 继承 .credit-number 字体（font:inherit）",
      /\.credit-link\{[^}]*font:inherit/.test(css), true);

console.log(bad ? `\n${bad} 项不符` : "\n=== 每日积分消费弹框全部通过 ===");
process.exit(bad ? 1 : 0);
