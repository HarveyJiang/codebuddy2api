// 日志弹框宽度回归（用户 2026-10-20 报：token 三列加进来后弹框出横向滚动条）。
// 断言两件事：① 两个日志弹框有独立的加宽规则；② 收紧 padding 的规则在；
// ③ 基础 dialog 规则没被顺手改坏（其他 12 个弹框仍 560px）。
import { readFileSync } from "node:fs";
const css = readFileSync("admin/static/style.css", "utf8");

let bad = 0;
const check = (name, ok) => { if (!ok) bad++; console.log(`${ok ? "  ok" : "FAIL"}  ${name}`); };

// 1) 两个日志弹框的加宽规则存在，且宽度比基础 560px 大。
const wide = /#account-logs-dialog,#key-requests-dialog\{width:min\((\d+)px,calc\(100vw - 30px\)\)\}/.exec(css);
check("两个日志弹框有加宽规则", !!wide);
const width = wide ? Number(wide[1]) : 0;
check(`加宽到 ${width}px（> 760，兜住 9 列）`, width > 760);

// 2) 基础 dialog 规则保持 560px —— 其他 12 个弹框不受影响。
//    注意：日志弹框的加宽规则写在 `#account-logs-dialog,#key-requests-dialog{...}` 里，
//    整串里也含 "dialog{" 子串（`-dialog{`），所以裸 /dialog\{/ 会先命中它。
//    用行首锚定：基础规则是独占一行的 `dialog{width:min(560px,`。
const base = /^dialog\{width:min\((\d+)px,/m.exec(css);
check(`基础 dialog 仍 560px（当前 ${base ? base[1] : "未找到"}）`, !!base && base[1] === "560");

// 3) 弹框内 th/td padding 收紧规则存在（只作用于这两个弹框内的表格）。
const pad = /#account-logs-dialog table th,#account-logs-dialog table td,#key-requests-dialog table th,#key-requests-dialog table td\{padding:(\d+)px (\d+)px\}/.exec(css);
check("两个日志弹框内的 th/td 有收紧的 padding 规则", !!pad);
check("收紧后的 padding 小于全局 22px", !!pad && Number(pad[1]) < 22 && Number(pad[2]) < 22);

// 4) 宽度估算：8 列内容 ≈830px + 内容区 padding 58 + 边框 2 = 890；9 列 ≈960 + 60 = 1020。
//    padding 收紧后 8 列省 8×(44-24)=160px → ≈730；9 列省 9×20=180 → ≈840。
//    980px 对两者都有余量，不至于再触发 .table-wrap 的 overflow:auto。
check("加宽后的宽度 ≥ 估算所需（含收紧 padding 后）", width >= 840);

console.log(bad ? `=== ${bad} 个失败 ===` : "=== 日志弹框宽度断言全部通过 ===");
process.exit(bad ? 1 : 0);
