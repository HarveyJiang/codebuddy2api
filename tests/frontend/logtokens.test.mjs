// 请求日志的 token 明细列（用户 2026-10-19）：输入 / 输出 / 缓存（读 / 写）。
//
// 从 app.js 抽出 fmtLogToken / tokenCells 单独求值，避免依赖 DOM；同时校验
// 三张日志表的表头都补上了这三列。用法：必须从仓库根目录运行。
import { readFileSync } from "node:fs";

const src = readFileSync("admin/static/app.js", "utf8");
const start = src.indexOf("function fmtLogToken");
const end = src.indexOf("\n}", src.indexOf("function tokenCells")) + 2;
const { fmtLogToken, tokenCells } = new Function(
  src.slice(start, end) + "; return { fmtLogToken, tokenCells };")();

let bad = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) { bad++; console.log(`  ✗  ${label}\n       得到 ${JSON.stringify(got)}\n       期望 ${JSON.stringify(want)}`); }
  else console.log(`  ok ${label}`);
}

// 1. 数字压成 K/M；缺失是 NULL，显示「—」而不是 0（0 与「没记录」语义不同）。
check("null → —", fmtLogToken(null), "—");
check("undefined → —", fmtLogToken(undefined), "—");
check("异常值 → —", fmtLogToken("abc"), "—");
check("0 → 0（确实记录了 0）", fmtLogToken(0), "0");
check("999 → 999", fmtLogToken(999), "999");
check("1200 → 1.2K", fmtLogToken(1200), "1.2K");
check("12345 → 12K", fmtLogToken(12345), "12K");
check("1500000 → 1.5M", fmtLogToken(1500000), "1.5M");

// 2. tokenCells 出三个 <td>：输入/输出/缓存（读 / 写）。
const cells = tokenCells({ tokens_in: 1200, tokens_out: 340, tokens_cache_read: 8000, tokens_cache_creation: 200 });
check("三格", (cells.match(/<td /g) || []).length, 3);
check("含输入", cells.includes("1.2K"), true);
check("含输出", cells.includes("340"), true);
check("缓存读 / 写", cells.includes("8K / 200"), true);

const missing = tokenCells({ tokens_in: null, tokens_out: null, tokens_cache_read: null, tokens_cache_creation: null });
check("全缺显示 —", (missing.match(/—/g) || []).length, 3);  // 输入/输出/缓存各一
check("缓存读写都缺时合一格 —", missing.includes("— / —"), false);

const onlyRead = tokenCells({ tokens_in: null, tokens_out: null, tokens_cache_read: 500, tokens_cache_creation: null });
check("只缺写时仍显示读", onlyRead.includes("500 / —"), true);

// 3. 三张日志表表头都补了「输入/输出/缓存」三列。
const html = readFileSync("admin/static/index.html", "utf8");
check("三张日志表都有 token 表头",
      (html.match(/<th class="align-right">输入<\/th><th class="align-right">输出<\/th><th class="align-right">缓存<\/th>/g) || []).length,
      3);
check("三处行渲染都调 tokenCells", (src.match(/\$\{tokenCells\(r\)\}|\+ tokenCells\(r\)/g) || []).length, 3);

console.log(bad ? `=== ${bad} 个失败 ===` : "=== 日志 token 列全部通过 ===");
process.exit(bad ? 1 : 0);
