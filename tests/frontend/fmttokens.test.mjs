// 从 app.js 里抽出 fmtTokens 源码单独求值，避免依赖 DOM。
import { readFileSync } from "node:fs";
const src = readFileSync("admin/static/app.js", "utf8");
const start = src.indexOf("function fmtTokens");
const end = src.indexOf("\n}", start) + 2;
const fn = new Function(src.slice(start, end) + "; return fmtTokens;")();

// 口径：积分与 token 一律整数展示，向下取整（与 creditsFmt 一致）。
const cases = [
  [842, "842"], [999, "999"], [1000, "1K"], [12300, "12K"],
  [12399, "12K"], [999949, "999K"], [999950, "1M"], [1000000, "1M"],
  [3400000, "3M"], [3400999, "3M"],
  [null, "0"], [undefined, "0"], [NaN, "0"], [0, "0"],
];
let bad = 0;
for (const [input, want] of cases) {
  const got = fn(input);
  const ok = got === want;
  if (!ok) bad++;
  console.log(`${ok ? "  ok" : "FAIL"}  ${String(input).padEnd(9)} -> ${got}${ok ? "" : "   期望 " + want}`);
}
console.log(bad ? `=== ${bad} 个失败 ===` : "=== fmtTokens 全部通过 ===");
process.exit(bad ? 1 : 0);
