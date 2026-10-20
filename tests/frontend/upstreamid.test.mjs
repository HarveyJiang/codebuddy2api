// upstreamIdCell：上游请求 ID 以模型名下小字呈现，不加列。
// 直接抽 app.js 里的函数源码求值，避免依赖 DOM。
import { readFileSync } from "node:fs";
const src = readFileSync("admin/static/app.js", "utf8");

function extract(name) {
  const start = src.indexOf(`function ${name}`);
  if (start < 0) throw new Error(`app.js 里找不到 ${name}`);
  const end = src.indexOf("\n}", start) + 2;
  return src.slice(start, end);
}
// esc 是 helper 的依赖（箭头函数常量，不是 function 声明），一起抽出来。
const escSrc = src.slice(src.indexOf("const esc ="), src.indexOf("\n", src.indexOf("const esc =")) + 1);
const cellSrc = extract("upstreamIdCell");
const upstreamIdCell = new Function(escSrc + "\n" + cellSrc + "\nreturn upstreamIdCell;")();

let bad = 0;
const check = (label, got, want) => {
  const ok = got === want;
  if (!ok) bad++;
  console.log(`${ok ? "  ok" : "FAIL"}  ${label}${ok ? "" : `\n        got:  ${got}\n        want: ${want}`}`);
};

const ID = "crb-e1301a5acb6f11f189f2025e276c2765";
const SHORT = "crb-1234";

// 空值不渲染：老日志没有这个字段，不能拿「—」占位。
check("无 id → 空串", upstreamIdCell({}), "");
check("id 为空串 → 空串", upstreamIdCell({upstream_request_id: ""}), "");
check("id 为 null → 空串", upstreamIdCell({upstream_request_id: null}), "");

// 短 id 原样显示。
check(
  "短 id 不截断",
  upstreamIdCell({upstream_request_id: SHORT}),
  `<small class="cell-note mono" title="${SHORT}">${SHORT}</small>`,
);

// 长 id 截到 16 字符 + 省略号，title 挂全文供悬停核对。
const wantLong = `<small class="cell-note mono" title="${ID}">${ID.slice(0, 16)}…</small>`;
check("长 id 截断 + title 全文", upstreamIdCell({upstream_request_id: ID}), wantLong);

// 正好 16 字符不截断（边界）。
const EXACT16 = "a".repeat(16);
check(
  "16 字符边界不截断",
  upstreamIdCell({upstream_request_id: EXACT16}),
  `<small class="cell-note mono" title="${EXACT16}">${EXACT16}</small>`,
);

// 引号必须转义：id 进了 title 属性，破属性会让 tooltip 错位。
// 注意截断是按**转义后**的长度算的（&quot; 占 6 字符），所以期望值写转义后的字符串。
check(
  "id 含引号被转义",
  upstreamIdCell({upstream_request_id: 'crb-"onmouseover=alert(1)'}),
  '<small class="cell-note mono" title="crb-&quot;onmouseover=alert(1)">crb-&quot;onmouseover…</small>',
);

// 三处日志行模板都要挂上：概览/请求日志页、账号日志弹框、密钥请求弹框。
for (const [label, anchor] of [
  ["概览 / 请求日志页（#dash-recent-body）", '$("dash-recent-body").innerHTML'],
  ["账号日志弹框（#account-logs-body）", '$("account-logs-body").innerHTML'],
  ["密钥请求弹框（#key-requests-body）", '$("key-requests-body").innerHTML'],
]) {
  const at = src.indexOf(anchor);
  check(`${label} 渲染了 upstreamIdCell`, at >= 0 && src.slice(at, at + 1400).includes("upstreamIdCell(r)"), true);
}

// 列数：请求日志页 9 → 10（v22 加「首字」列），两个弹框 8 → 9。
// 只加一列，不再加来源 IP/UA 列（已经很宽，IP/UA 走状态徽标的悬停提示）。
const html = readFileSync("admin/static/index.html", "utf8");
const headThCount = tbodyId => {
  const at = html.indexOf(tbodyId);
  const tableAt = html.lastIndexOf("<table", at);
  const head = html.slice(html.indexOf("<thead>", tableAt), html.indexOf("</thead>", tableAt));
  // 数 </th>：count("<th") 会把 <thead> 自己算进去，多 1。
  return (head.match(/<\/th>/g) || []).length;
};
check("请求日志页 10 列", headThCount('id="dash-recent-body"'), 10);
// 账号日志与密钥请求弹框也是 9 列（时间/密钥/模型/账号/状态/输入/输出/缓存/首字/耗时 去掉密钥列）。
check("账号日志弹框 9 列", headThCount('id="account-logs-body"'), 9);
check("密钥请求弹框 9 列", headThCount('id="key-requests-body"'), 9);

console.log(bad ? `=== ${bad} 个失败 ===` : "=== upstreamIdCell 全部通过 ===");
process.exit(bad ? 1 : 0);
