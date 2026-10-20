// 单 key 的后端优先顺序（`keys.model_backend_order`）—— 前端契约。
//
// 对应 `specs/bare-name-routing-plan.md` §6.2(a)。后端语义由
// `tests/admin/test_key_backend_order.py` 守着，这里守**前端这一侧**的约定：
//
//   1. 「未配置」必须交 null —— 不能把当前显示的顺序固化成显式配置。
//      固化的坏处很具体：将来加了新后端，跟随默认会按注册表顺序插进来，
//      而被固化的顺序会把它甩到最后（用户会以为「新后端不生效」）。
//   2. 单 key 的顺序只在「全部」模式下提交 —— 隐藏的字段不带上，
//      否则在「模型范围」弹框里点保存会把用户的顺序清空。
//   3. 两个编辑器共用一套渲染，但基线不同：全局 = 注册表顺序，
//      单 key = 全局生效顺序。
//
// 用法：**必须从仓库根目录运行**（与其它 frontend 测试一致）。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const js = readFileSync(join(root, "admin", "static", "app.js"), "utf8");
const html = readFileSync(join(root, "admin", "static", "index.html"), "utf8");
const css = readFileSync(join(root, "admin", "static", "style.css"), "utf8");

let bad = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) { bad++; console.log(`  ✗  ${label}\n       得到 ${JSON.stringify(got)}\n       期望 ${JSON.stringify(want)}`); }
  else console.log(`  ok ${label}`);
}

// --- 1. 入口：全局设置 + 单 key 弹框各有一个编辑器 -----------------------
check("设置页有全局顺序编辑器", html.includes('data-order-editor="backend-order"'), true);
check("全局编辑器基线是注册表顺序", html.includes('data-order-base="registry"'), true);
check("设置页有全局顺序容器", html.includes('id="backend-order"'), true);
check("密钥弹框有单 key 顺序编辑器", html.includes('data-order-editor="key-bind-order"'), true);
check("单 key 编辑器基线是全局顺序", html.includes('data-order-base="global"'), true);
check("密钥弹框有单 key 顺序容器", html.includes('id="key-bind-order"'), true);
check("单 key 顺序区块有独立 id（用于显隐）", html.includes('id="key-bind-order-block"'), true);

// --- 2. 未配置 = 交 null（不固化当前顺序）------------------------------
check("orderEditorValue 未配置时返回 null",
  /const orderEditorValue = hostId => \{[\s\S]{0,200}?return null;/.test(js), true);
check("orderEditorValue 显式配置时交列表",
  /return st\.order\.slice\(\);/.test(js), true);

// --- 3. 提交路径 -------------------------------------------------------
check("设置页提交带全局顺序", js.includes("model_backend_order:orderEditorValue(\"backend-order\")"), true);
check("密钥弹框提交带单 key 顺序", js.includes('model_backend_order: orderEditorValue("key-bind-order")'), true);

// 「单区块模式只提交可见的那一组」：顺序属于「全部」模式，
// 在 models/accounts 模式下不能带上（否则会清空用户配的顺序）。
const submitBody = js.slice(js.indexOf('const body = mode === "models"'));
const allBranch = submitBody.slice(submitBody.indexOf(": {"), submitBody.indexOf("await api(\"keys/\""));
check("单 key 顺序只在「全部」模式提交", allBranch.includes('orderEditorValue("key-bind-order")'), true);
check("models 模式分支不含顺序", /mode === "models" \? \{models:[^}]*\}\s*:\s*mode === "accounts" \? \{accounts:[^}]*\}/.test(submitBody), true);

// --- 4. 显隐与渲染 -----------------------------------------------------
check("非「全部」模式隐藏顺序区块",
  js.includes('$("key-bind-order-block").hidden = mode !== "all";'), true);
check("打开密钥弹框时渲染顺序编辑器",
  js.includes('renderOrderEditor("key-bind-order", k.model_backend_order, orderEditorBase())'), true);
check("刷新时渲染全局顺序编辑器",
  js.includes('renderOrderEditor("backend-order", overview.pool?.model_backend_order'), true);
check("渲染时读的是全局生效顺序（未点名的补在末尾）",
  js.includes("orderEditorBase"), true);
check("移动后标记为显式配置", js.includes("st.explicit = true;"), true);
check("恢复默认 = 回到未配置", js.includes("st.explicit = false;"), true);

// --- 5. 样式 -----------------------------------------------------------
for (const cls of [".order-block", ".order-editor", ".order-row", ".order-index",
                   ".order-name", ".order-foot"]) {
  check(`样式里有 ${cls}`, css.includes(cls), true);
}

console.log(bad ? `=== ${bad} 项不符 ===` : "=== 单 key 顺序前端检查全部通过 ===");
process.exit(bad ? 1 : 0);
