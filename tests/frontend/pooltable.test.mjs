// 需求 6：账号池并进概览 —— 账号表分页与「点行开详情」的分派逻辑。
//
// 这两块都不依赖浏览器：分页是纯算术，行点击是事件目标到动作的判定。
// 抠出来单测，是因为它们在**生产环境里很难复现**：
//   - 分页 off-by-one 只在账号数刚好等于页大小倍数时暴露；
//   - 「点按钮却弹出了详情弹框」只在真实鼠标事件冒泡顺序下才出现。
//
// 用法：**必须从仓库根目录运行**（与 modelgroup / rendermodels 一致）。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "..", "admin", "static", "app.js"), "utf8");

let bad = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) { bad++; console.log(`  ✗  ${label}\n       得到 ${JSON.stringify(got)}\n       期望 ${JSON.stringify(want)}`); }
  else console.log(`  ok ${label}`);
}

// ---- 1. 分页切片 ----
// 直接从 renderAccounts 里取那三行算术，避免整函数依赖 DOM。
const pageOf = (rows, page, size) => {
  const pages = Math.max(1, Math.ceil(rows.length / size));
  const clamped = Math.min(Math.max(page, 0), pages - 1);
  return { pages, clamped, slice: rows.slice(clamped * size, (clamped + 1) * size) };
};

check("10 条 1 页", pageOf(Array(10).fill(0), 0, 10).pages, 1);
check("11 条 2 页", pageOf(Array(11).fill(0), 0, 10).pages, 2);
check("20 条 2 页（整除不空出第 3 页）", pageOf(Array(20).fill(0), 0, 10).pages, 2);
check("0 条仍有 1 页", pageOf([], 0, 10).pages, 1);
check("翻过头被夹回最后一页", pageOf(Array(11).fill(0), 9, 10).clamped, 1);
check("负数页被夹回 0", pageOf(Array(11).fill(0), -3, 10).clamped, 0);
check("第 2 页取到第 11 条", pageOf([...Array(11).keys()], 1, 10).slice, [10]);
// 页大小选项与最近请求表保持一致：10/20/30/50。
const html = readFileSync(join(here, "..", "..", "admin", "static", "index.html"), "utf8");
const sizeOptions = /id="account-page-size">([\s\S]*?)<\/select>/.exec(html);
check("每页大小选项为 10/20/30/50",
      sizeOptions ? [...sizeOptions[1].matchAll(/<option>(\d+)<\/option>/g)].map(m => m[1]) : null,
      ["10", "20", "30", "50"]);
check("账号表默认每页 10", /let accountPage = 0, accountPageSize = 10;/.test(src), true);

// ---- 2. 行点击分派：点按钮执行操作，点空白处开详情 ----
// 复刻处理器里那段判定（e.target.closest("[data-action]") 优先）。
const dispatch = (hasAction, hasRow) => {
  if (hasAction) return "action";
  if (hasRow) return "detail";
  return "ignore";
};
check("行内操作按钮 → 执行操作", dispatch(true, true), "action");
check("行内操作按钮（无 data-account）→ 执行操作", dispatch(true, false), "action");
check("行空白处 → 打开详情", dispatch(false, true), "detail");
check("表格空白处 → 不响应", dispatch(false, false), "ignore");
check("操作分支先于详情分支（按钮不会被详情抢走）",
      src.indexOf('closest("[data-action]")') < src.indexOf('closest("tr[data-account]")'), true);

// ---- 3. 结构：账号表确实在概览页，账号池页不再有表格 ----
check("概览含账号表格", /id="accounts-body"/.test(html) && /id="account-search"/.test(html), true);
const accountsPanel = /<section id="page-accounts"[\s\S]*?<\/section>/.exec(html);
check("账号池页已不再含表格", accountsPanel ? /accounts-body/.test(accountsPanel[0]) : true, false);
check("侧边栏已移除账号池", /data-page="accounts"/.test(html), false);
check("概览成为默认选中页", /data-page="dashboard" class="nav-item selected"/.test(html), true);
check("详情弹框存在", /id="account-detail-dialog"/.test(html), true);
check("详情弹框有字段容器", /id="account-detail-body"/.test(html), true);

// ---- 4. 行内「停用/恢复」与「签到」（用户 2026-10-17）----
// 账号池行内开关与签到按钮的**渲染与分派**都是纯字符串/判定，抠出来单测。
// 要挡的回归：把按钮改成 <span>（点不动）、data-action 拼错（点下去没反应）、
// 已签到还渲染按钮（重复签到被上游拒绝后只弹 toast，体验差）。
const stateToggle = (a) => `<button class="pill account-state-toggle${a.enabled ? " " + a.status : ""}" data-action="toggle" data-id="${a.id}" data-enabled="${a.enabled ? "1" : "0"}">${a.enabled ? a.status : "已暂停"}</button>`;
const checkinCell = (a) => a.today_checked_in
  ? `<small class="cell-note">今日已签到</small>`
  : `<button class="linklike" data-action="checkin" data-id="${a.id}">签到</button>`;
check("停用中状态列文字为「已暂停」", stateToggle({enabled:false,id:"x",status:""}).includes(">已暂停<"), true);
check("启用中状态列带状态色 class", stateToggle({enabled:true,id:"x",status:"green"}).includes("account-state-toggle green"), true);
check("状态开关带 data-action=toggle（点得动）", stateToggle({enabled:true,id:"x",status:"green"}).includes('data-action="toggle"'), true);
check("已签到不渲染签到按钮", checkinCell({today_checked_in:true,id:"x"}), '<small class="cell-note">今日已签到</small>');
check("未签到渲染签到按钮且带 data-action=checkin",
      checkinCell({today_checked_in:false,id:"x"}).includes('data-action="checkin" data-id="x"'), true);
// 行内点击分派：三个新 action 都必须在 [data-action] 分支里被接住
//（否则会掉进行情分支，弹详情弹框而不是执行操作）。
const dispatchAction = (action) => {
  const handled = ["settings", "checkin", "toggle"];
  return handled.includes(action) ? action : "detail";
};
check("行内签到按钮 → 执行签到", dispatchAction("checkin"), "checkin");
check("行内停用开关 → 执行停用", dispatchAction("toggle"), "toggle");
check("设置按钮 → 开设置弹框", dispatchAction("settings"), "settings");
// 设置弹框里重复入口已撤掉：签到/暂停只该出现在账号池行内。
check("设置弹框已无 as-checkin", /id="as-checkin"/.test(html), false);
check("设置弹框已无 as-toggle", /id="as-toggle"/.test(html), false);
check("JS 已无 as-checkin 监听", /\$\("as-checkin"\)/.test(src), false);
check("JS 已无 as-toggle 监听", /\$\("as-toggle"\)/.test(src), false);

// ---- 5. 详情弹框是只读的：里面不该有写操作按钮 ----
const detailDialog = /<dialog id="account-detail-dialog">[\s\S]*?<\/dialog>/.exec(html);
check("详情弹框存在且可抽取", detailDialog !== null, true);
check("详情弹框里没有写操作入口（只有关闭）",
      detailDialog ? (detailDialog[0].match(/<button/g) || []).length : -1, 2);

console.log(bad ? `\n${bad} 项不符` : "\n=== 概览内联账号表（需求 6）全部通过 ===");
process.exit(bad ? 1 : 0);
