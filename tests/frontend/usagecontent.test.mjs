// 用量明细「请求内容」列（用户 2026-10-19 需求）：
// 删「客户端」「用途」两列，原位置改为 上游 inputTrunc 摘要 + 小字 requestId。
// 这条测试盯着静态源码结构（表头/列数/colspan/helper），改坏了立刻红。
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../admin/static/", import.meta.url);
const html = readFileSync(new URL("index.html", ROOT), "utf8");
const js = readFileSync(new URL("app.js", ROOT), "utf8");
const css = readFileSync(new URL("style.css", ROOT), "utf8");

let bad = 0;
const check = (name, ok, detail = "") => {
  if (!ok) bad++;
  console.log(`${ok ? "  ok" : "FAIL"}  ${name}${ok || !detail ? "" : "   " + detail}`);
};

// 1) 表头：5 列，顺序为 时间/账号/模型/请求内容/积分；客户端与用途两列必须消失。
const bodyAt = html.indexOf('<tbody id="usage-body">');
const tableAt = html.lastIndexOf("<table", bodyAt);
const theadBlock = html.slice(tableAt, html.indexOf("</thead>", tableAt));
const cols = [...theadBlock.matchAll(/<th[^>]*>(.*?)<\/th>/g)].map(m => m[1].replace(/<[^>]+>/g, ""));
check("用量表 5 列", cols.length === 5, `实际 ${cols.length} 列: ${cols.join("|")}`);
check("第 4 列是请求内容", cols[3] === "请求内容", cols[3]);
check("删掉了客户端列", !cols.includes("客户端"));
check("删掉了用途列", !cols.includes("用途"));
check("积分列仍在末列且右对齐", /<th class="align-right">积分<\/th>/.test(theadBlock));

// 2) 渲染：行模板 5 个 td，用 usageContent() 出请求内容列；不再出现 client/agent_purpose。
const render = js.slice(js.indexOf("async function loadUsage"));
const row = render.slice(0, render.indexOf("} catch (e)"));
check("行模板调用 usageContent", /usageContent\(r\)/.test(row));
check("行模板不再渲染 client", !/r\.client/.test(row));
check("行模板不再渲染 agent_purpose", !/r\.agent_purpose/.test(row));
check("空态 colspan=5", /colspan="5" class="history-empty">该区间没有记录/.test(row));

// 3) 错误态同样 5 列。
const errState = render.slice(render.indexOf("} catch (e) {"), render.indexOf("} catch (e) {") + 300);
check("错误态 colspan=5", /colspan="5"/.test(errState));

// 4) usageContent 的行为：截断、省略、title 完整内容、requestId 小字、空值不误导。
check("helper 在 loadUsage 之前定义", js.indexOf("function usageContent") < js.indexOf("async function loadUsage"));
check("有截断上限常量", /const USAGE_INPUT_MAX = \d+;/.test(js));
const helper = js.slice(js.indexOf("const USAGE_INPUT_MAX"), js.indexOf("async function loadUsage"));
check("超长截断加省略号", /slice\(0, USAGE_INPUT_MAX\) \+ "…"/.test(helper));
check("完整内容走 title", /title="\$\{esc\(raw\)\}"/.test(helper));
check("requestId 用小字等宽", /cell-note mono/.test(helper) && /r\.request_id/.test(helper));
check("空摘要显示占位 —", /muted">—</.test(helper));
check("引用 input_trunc 字段", /r\.input_trunc/.test(helper));
check("HTML 转义后才插入", /esc\(text\)/.test(helper));

// 5) 样式：这一列必须能限宽换行，否则长摘要把积分列挤出屏幕。
check("CSS 有 usage-content 规则", /\.usage-content\{/.test(css));
check("CSS 限宽", /max-width:\d+px/.test(css));
check("CSS 允许换行", /white-space:normal/.test(css));
check("CSS 长词折断", /overflow-wrap:anywhere/.test(css));

console.log(bad ? `=== ${bad} 个失败 ===` : "=== usage-content 全部通过 ===");
process.exit(bad ? 1 : 0);
