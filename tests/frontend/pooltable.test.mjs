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
const css = readFileSync(join(here, "..", "..", "admin", "static", "style.css"), "utf8");

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
// 行内点击分派：新 action 都必须在 [data-action] 分支里被接住
//（否则会掉进行情分支，弹详情弹框而不是执行操作）。
const dispatchAction = (action) => {
  const handled = ["settings", "checkin", "toggle", "logs"];
  return handled.includes(action) ? action : "detail";
};
check("行内签到按钮 → 执行签到", dispatchAction("checkin"), "checkin");
check("行内停用开关 → 执行停用", dispatchAction("toggle"), "toggle");
check("设置按钮 → 开设置弹框", dispatchAction("settings"), "settings");
// 操作列「日志」按钮（用户 2026-10-17：请求日志直接放操作列、设置后面，
// 不再藏在设置弹框里）——渲染与分派都要在。
check("账号池行模板渲染日志按钮且带 data-action=logs",
      src.includes('data-action="logs"') && src.includes('=== "logs") { openAccountLogs(a)'), true);
check("设置弹框里 as-logs 已撤掉（入口挪到操作列）", /id="as-logs"/.test(html), false);
// 行模板里不能引用未定义变量（2026-10-17 线上 bug：${checkinBtn} 让整个
// renderAccounts 抛 ReferenceError，概览账号表整片消失）。
check("账号行模板无未定义变量引用（checkinBtn 事故回归）",
      /\$\{checkinBtn\}/.test(src), false);
check("账号行模板只引用已定义的 checkinCell",
      /\$\{checkinCell\}/.test(src), true);
// 账号日志弹框带分页（用户 2026-10-17：最近 7 天 + 每页 100 条 + 翻页）
check("账号日志弹框有分页控件", /id="account-logs-prev"/.test(html) && /id="account-logs-next"/.test(html), true);
check("账号日志默认最近 7 天 + 每页 100 条", /since=\$\{since\}/.test(src) && /ACCOUNT_LOGS_SIZE = 100/.test(src), true);
check("账号日志分页有迟到的响应守卫", /requestedOffset !== accountLogsOffset/.test(src), true);
// 详情弹框不再露上游昵称（用户需求：全站统一显示备注名，昵称只读不可改）
check("详情弹框已无「上游昵称」行", /\["上游昵称"/.test(src), false);
// 弹框底部间距（用户 2026-10-17：内容不再贴底）
check("弹框主体有上下内边距（不再贴底）", /dialog>form>:not\(\.dialog-heading\):not\(\.dialog-footer\)\{[^}]*padding:22px 29px/.test(css), true);
check("带 footer 的弹框主体底部收到 4px", /dialog>form>\.dialog-body\{padding-bottom:4px\}/.test(css), true);
check("只读弹框主体底部留 22px", /dialog>\.dialog-body\{padding-bottom:22px\}/.test(css), true);
// 设置弹框里重复入口已撤掉：签到/暂停只该出现在账号池行内。
check("设置弹框已无 as-checkin", /id="as-checkin"/.test(html), false);
check("设置弹框已无 as-toggle", /id="as-toggle"/.test(html), false);
check("JS 已无 as-checkin 监听", /\$\("as-checkin"\)/.test(src), false);
check("JS 已无 as-toggle 监听", /\$\("as-toggle"\)/.test(src), false);

// ---- 5. 详情弹框是只读的：里面不该有写操作按钮 ----
// 底部「关闭」已取消（用户 2026-10-17：标题栏的 × 是钉住的，不随滚动跑，
// 底部再放一个纯关闭按钮是冗余）—— 现在整个弹框只剩标题栏一个关闭钮。
const detailDialog = /<dialog id="account-detail-dialog">[\s\S]*?<\/dialog>/.exec(html);
check("详情弹框存在且可抽取", detailDialog !== null, true);
// ---- 6.5 日志状态列只显示状态码数字，说明进美化 tooltip（用户 2026-10-17）----
check("日志状态列不再有行内 cell-note 文字",
      /log-status[\s\S]{0,120}cell-note inline/.test(src), false);
check("日志状态列带 data-tip 气泡（三处：概览/账号日志/密钥明细）",
      (src.match(/pill log-status/g) || []).length >= 3, true);
check("日志弹框已加宽到 760px（治横向滚动）",
      /#account-logs-dialog,#key-requests-dialog\{width:min\(760px/.test(css), true);

// ---- 6.6 复选框救回：select 美化不能把 checkbox/radio 剥成下拉（用户 2026-10-17 报 bug）----
check("checkbox/radio 恢复原生外观（appearance:auto）",
      /input\[type=checkbox\][\s\S]{0,180}?appearance:auto/.test(css), true);
check("复选框规则清掉了 select 的 chevron 背景图",
      /input\[type=checkbox\][^}]*background-image:none/.test(css), true);

// ---- 6.6.1 chevron 只给 select（用户 2026-10-19：密码框与其他文本框不该有下拉符号）----
const inputRule = /input,textarea\{([^}]*)\}/.exec(css);
check("input/textarea 基础规则可抽取", inputRule !== null, true);
check("input/textarea 规则没有 appearance:none（保留原生外观）",
      /appearance:(\s*)none/.test(inputRule ? inputRule[1] : ""), false);
check("input/textarea 规则没有自绘 chevron 背景图",
      /background-image/.test(inputRule ? inputRule[1] : ""), false);
check("input/textarea 规则仍有边框/圆角/内边距",
      inputRule ? /border:1px solid/.test(inputRule[1]) && /border-radius:12px/.test(inputRule[1]) : false, true);
check("全站只有 select 用自绘 chevron（下拉箭头不外泄到输入框）",
      (css.match(/background-image:url\("data:image\/svg\+xml/g) || []).length === 1, true);
check("chevron 背景图挂在 select 规则上",
      /select\{[^}]*background-image:url\("data:image\/svg\+xml/.test(css), true);

// ---- 6.7 设置页卡片标题 inline-flex：q-help 是 h3 兄弟节点，flex 会把它挤下行 ----
check("settings-card h3 用 inline-flex（问号贴标题不被挤走）",
      /\.settings-card h3\{[^}]*display:inline-flex/.test(css), true);

// ---- 6.10 请求日志页与用量明细页语言对齐（用户 2026-10-17：不要筛选，只整理）----
check("请求日志页有 banner 说明条", /id="page-logs"[\s\S]{0,200}class="banner"/.test(html), true);
check("请求日志页有刷新按钮", /id="logs-refresh"/.test(html), true);
check("JS 接了 logs-refresh 点击", /\$\("logs-refresh"\)\.addEventListener/.test(src), true);
check("日志页没有多余筛选下拉（用户明确不要）",
      /id="logs-filter"|id="logs-key"|id="logs-backend"|id="logs-outcome"/.test(html), false);
check("日志页分页条与表格拉开间距", /#page-logs \.pager\{margin-top:18px\}/.test(css), true);

// ---- 6.9 用量明细页按后端 tab（用户 2026-10-17）----
check("用量页有 tab 容器（JS 填充）", /id="usage-tabs"/.test(html), true);
check("usageBackend 全局且默认空（=全部后端）",
      /let usageBackend = ""/.test(src), true);
check("usageQuery 带 backend 参数", /if \(usageBackend\) q\.set\("backend", usageBackend\)/.test(src), true);
check("syncUsage 拉取也按当前 tab 筛", /backend: usageBackend \|\| null/.test(src), true);
check("paintUsageTabs 含「全部」tab 且默认选中",
      /\{name: "", label: "全部", all: true\}/.test(src) && /t\.name === usageBackend/.test(src), true);
check("tab 点击分派存在且切 tab 后重新加载",
      /closest\("\[data-usage-tab\]"\)/.test(src) && /usageBackend = tab\.dataset\.usageTab/.test(src), true);
check("loadUsage 拿到报表后回填 tab 记录数",
      /paintUsageTabs\(counts\)/.test(src), true);
check("筛选区改固定列宽（不再 auto-fit 铺满拉伸）",
      /\.usage-filter\{[^}]*grid-template-columns:repeat\(4,auto\)/.test(css), true);

// ---- 6.8 备份只留一个「保存设置」+ 备份卡内「立即备份」（用户 2026-10-17）----
check("HTML 已无 backup-save 按钮", /id="backup-save"/.test(html), false);
check("JS 已无 backup-save 监听", /\$\("backup-save"\)/.test(src), false);
check("主表单 PATCH 带上备份三字段",
      /backup_enabled:\$\("backup-enabled"\)\.checked/.test(src), true);
check("立即备份按钮仍在备份卡", /id="backup-now"/.test(html), true);

// ---- 6.9 手动解除冷却（用户 2026-10-18：上游按模型限流却冷整个账号）----
check("账号行有解除冷却按钮且带 data-action=clear-cooldown",
      /data-action="clear-cooldown"/.test(src), true);
check("解除冷却按钮只在冷却中渲染（cooldown_until 未过期才拼）",
      src.includes('const cooling = a.cooldown_until > Date.now() / 1000;')
      && src.includes('const unfreezeBtn = cooling ?'), true);
check("行内分派接住 clear-cooldown 并弹确认",
      /=== "clear-cooldown"\) \{[\s\S]{0,300}?confirmAction/.test(src), true);
check("解除冷却走 POST accounts/{id}/actions/clear-cooldown",
      (src.match(/actions\/clear-cooldown/g) || []).length >= 2
      && src.includes('actions/clear-cooldown`, {method:"POST"}'), true);
check("设置弹框有 as-unfreeze 入口", /id="as-unfreeze"/.test(html), true);
check("as-unfreeze 按冷却状态显隐", /\$\("as-unfreeze"\)\.hidden = !\(/.test(src), true);

check("详情弹框只有标题栏一个关闭钮（底部关闭已取消）",
      detailDialog ? (detailDialog[0].match(/<button/g) || []).length : -1, 1);

// ---- 6. 详情弹框重设计为卡片式（用户 2026-10-17：原生 dl 两列太素）----
// 结构：头部身份区（图标+徽标）→ 三张指标卡 → 四张分组字段卡 → 错误条带。
check("详情弹框有头部身份区", detailDialog ? /class="detail-hero"/.test(detailDialog[0]) : false, true);
check("详情弹框有图标容器", detailDialog ? /id="account-detail-avatar"/.test(detailDialog[0]) : false, true);
check("详情弹框有徽标容器", detailDialog ? /id="account-detail-badges"/.test(detailDialog[0]) : false, true);
check("详情弹框有三张指标卡容器", detailDialog ? /id="account-detail-metrics"/.test(detailDialog[0]) : false, true);
check("详情弹框有分组卡片容器", detailDialog ? /id="account-detail-body"/.test(detailDialog[0]) : false, true);
check("详情弹框有错误条带容器", detailDialog ? /id="account-detail-error"/.test(detailDialog[0]) : false, true);
// 旧的原生 dl 直接挂在弹框下已移除：字段现在都在 .detail-section 里。
check("详情弹框不再直接挂裸 detail-grid", detailDialog ? /<\/div><dl class="detail-grid">/.test(detailDialog[0]) : true, false);
check("JS 渲染了指标卡", /account-detail-metrics.*detail-metric-value/.test(src) || /detail-metric-value/.test(src), true);
check("JS 渲染了分组卡", /const section = \(title, rows\)/.test(src), true);
check("JS 头部徽标含状态 pill", /account-detail-badges/.test(src) && /poolLabel\[0\]/.test(src), true);
check("CSS 有 detail-hero 样式", /\.detail-hero\{/.test(css), true);
check("CSS 有 detail-metrics 样式", /\.detail-metrics\{/.test(css), true);
check("CSS 有 detail-section 样式", /\.detail-section\{/.test(css), true);
check("CSS 有 detail-error 样式", /\.detail-error\{/.test(css), true);
check("CSS 指标卡窄屏单列", /\.detail-metrics\{grid-template-columns:1fr\}/.test(css), true);

// ---- 7. 弹框公共外壳（用户 2026-10-17：关闭按钮随内容滚动跑掉）----
// 所有弹框共用同一套结构：.dialog-heading（固定标题栏 + 关闭钮）
//   → .dialog-body（唯一滚动区）→ .dialog-footer（固定底部操作栏）。
// 只读弹框例外（用户 2026-10-17 拍板）：底部栏如果只剩一个「关闭」就是
// 冗余 —— 标题栏的 × 已经钉住不随滚动跑，所以这 5 个弹框直接不要 footer。
// 其余 6 个有真正操作（保存/创建/确认/撤销/删除）的照旧三段齐全。
const READONLY_DIALOGS = new Set(["model-debug-dialog", "key-requests-dialog",
  "account-detail-dialog", "usage-history-dialog", "account-logs-dialog",
  // 每日积分弹框（用户 2026-10-19）：只读表格 + 档位 + 「拉取上游补算」。
  // 底部不放「关闭」（标题栏 × 已钉住），footer 整栏取消；「拉取上游」是
  // 一个动作按钮而非关闭按钮，所以仍归只读类。
  "credit-daily-dialog"]);
const dialogs = [...html.matchAll(/<dialog id="([a-z-]+)">/g)].map(m => m[1]);
check("共 12 个弹框", dialogs.length, 12);
for (const d of dialogs) {
  const seg = new RegExp(`<dialog id="${d}">[\\s\\S]*?</dialog>`).exec(html)[0];
  check(`${d} 有公共标题栏`, /class="dialog-heading"/.test(seg), true);
  check(`${d} 标题栏有关闭按钮`, /close-dialog" aria-label="关闭"/.test(seg), true);
  check(`${d} 有滚动主体`, /class="dialog-body"/.test(seg), true);
  check(`${d} 不再用旧 dialog-actions`, /class="dialog-actions/.test(seg), false);
  check(`${d} form 标签配对`, seg.split("<form").length - 1, seg.split("</form>").length - 1);
  if (READONLY_DIALOGS.has(d)) {
    // 只读弹框：底部整栏取消，且底部不该再有任何 close-dialog 按钮。
    check(`${d} 只读弹框已取消底部栏`, /class="dialog-footer/.test(seg), false);
    check(`${d} 底部不再有关闭按钮`, /dialog-footer[\s\S]*close-dialog/.test(seg), false);
  } else {
    check(`${d} 有固定底部操作栏`, /class="dialog-footer/.test(seg), true);
  }
}
// CSS：dialog 本体零内边距 + flex column，三段各自 padding。
check("CSS dialog 本体零内边距", /dialog\{[^}]*padding:0/.test(css), true);
check("CSS dialog 是 flex column", /dialog\{[^}]*flex-direction:column/.test(css), true);
check("CSS 标题栏固定不滚（flex:0 0 auto）", /dialog \.dialog-heading\{[^}]*flex:0 0 auto/.test(css), true);
check("CSS 标题栏不再用 sticky", /dialog \.dialog-heading\{[^}]*position:sticky/.test(css), false);
check("CSS 底部栏固定（flex:0 0 auto）", /dialog>\.dialog-footer\{[^}]*flex:0 0 auto/.test(css), true);
check("CSS 主体是唯一滚动区", /dialog>:not\(\.dialog-heading\):not\(\.dialog-footer\)\{[^}]*overflow-y:auto/.test(css), true);
check("CSS 旧 max-height:92vh 已移除", /max-height:92vh/.test(css), false);
check("CSS 旧负边距技巧已移除", /[^{}\n]*margin:-29px/.test(css.replace(/\/\*[\s\S]*?\*\//g, "")), false);
// key-dialog 是双面板（表单 / 明文展示），footer 也要跟着切，否则两组按钮同屏。
check("key-dialog 有独立 footer", /id="created-key-footer"/.test(html), true);
check("JS 切面板时同步 footer", /\$\("created-key-footer"\)\.hidden = false/.test(src) && /\$\("created-key-footer"\)\.hidden = true/.test(src), true);

// ---- 7b. 全站按钮风格统一（用户 2026-10-17：撤销像纯文本、一处裸文字一处带样式）----
// 三件事：.danger 收敛为一个红字白底细边定义；表格行尾/备份行共用小号规格
// .btn-ghost；底部纯「关闭」按钮全取消（只读弹框连 footer 一起删）。
check("CSS .danger 只有一处定义（白底细边红字）",
      (css.match(/(?:^|[,{}\n])\s*\.danger\s*\{/g) || []).length, 1);
check("CSS .danger 是白底细边红字",
      /\.danger\{[^}]*color:#a46762[^}]*background:#fff[^}]*border:1px solid #e6cfcb/.test(css), true);
check("CSS .danger 不再有 background:none 的裸文字版",
      /\.danger\{[^}]*background:none/.test(css), false);
check("CSS 行尾特例已删（models/backup/settings-footer）",
      /#models-body \.danger|#backup-body \.danger|\.account-settings-footer button\.danger/.test(css), false);
check("CSS 有统一小号按钮规格 .btn-ghost", /\.btn-ghost\{/.test(css), true);
check("CSS .btn-ghost 是白底细边", /\.btn-ghost\{[^}]*background:#fff[^}]*border:1px solid var\(--line\)/.test(css), true);
check("CSS 旧的 .actions button 裸文字规格已删", /\.actions button\{/.test(css), false);
check("密钥表行尾按钮用 .btn-ghost",
      /class="btn-ghost" data-bind=/.test(src) && /class="btn-ghost" data-requests=/.test(src), true);
check("账号池设置按钮用 .btn-ghost", /class="btn-ghost" data-action="settings"/.test(src), true);
check("备份行按钮用 .btn-ghost", /class="btn-ghost" data-backup-download=/.test(src) && /class="btn-ghost danger" data-backup-delete=/.test(src), true);
check("模型删除按钮用 .btn-ghost danger", /class="btn-ghost danger model-del"/.test(src), true);
check("弹框 footer 里已无裸「取消」按钮（标题栏 × 即取消）",
      /close-dialog">取消</.test(html), false);
check("弹框 footer 里已无裸「关闭」按钮", /close-dialog">关闭</.test(html), false);

console.log(bad ? `\n${bad} 项不符` : "\n=== 概览内联账号表（需求 6）全部通过 ===");
process.exit(bad ? 1 : 0);
