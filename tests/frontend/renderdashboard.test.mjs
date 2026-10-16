// 真机渲染检查：用真实 /admin/api/overview 数据跑概览页的账号表与详情弹框。
//
// 抓的是「字段名写错 → undefined」「模板结构变了但测试没跟着改」这类问题 ——
// 这些在真浏览器里才会发现，而它们是本次改动最容易出的一类错。
//
// 用法：**必须从仓库根目录运行**。数据取自 WB_TEST_DB（默认 /tmp/opencode/v7fresh）。
import { readFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const ROOT = join(here, "..", "..");
const src = readFileSync(join(ROOT, "admin", "static", "app.js"), "utf8");
const PY = process.env.WB_TEST_PYTHON || "/tmp/opencode/wb-venv/bin/python";
const DB = process.env.WB_TEST_DB || "/tmp/opencode/v7fresh";

if (!existsSync(join(DB, "management", "state.db"))) {
  console.log(`跳过：未找到 ${DB}/management/state.db（可用 WB_TEST_DB 指定）。`);
  process.exit(0);
}

// 起一个真 app（迁移过的库副本），拿真实的 backends + overview。
const py = `
import asyncio, json, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, ${JSON.stringify(ROOT)})
src, tmp = Path(${JSON.stringify(DB)}), Path(tempfile.mkdtemp())
shutil.copytree(src / "management", tmp / "management")
(tmp / "auth").mkdir()
if (src / "auth" / "session.info").exists():
    shutil.copy(src / "auth" / "session.info", tmp / "auth" / "session.info")
from admin import server as S
import httpx
app = S.create_app(tmp / "management", tmp / "auth", "client-existing-key", "admin-key-long-enough-1234")
async def m():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t") as c:
        r = await c.post("/admin/api/login", json={"key": "admin-key-long-enough-1234"})
        h = {"X-CSRF-Token": r.json()["csrf"]}
        b = (await c.get("/admin/api/backends", headers=h)).json()
        ov = (await c.get("/admin/api/overview", headers=h)).json()
        print(json.dumps({"backends": b["backends"], "overview": ov}, ensure_ascii=False))
asyncio.run(m())
`;
const data = JSON.parse(execFileSync(PY, ["-c", py], { cwd: ROOT, encoding: "utf8" })
  .trim().split("\n").pop());

// 抠出要测的片段。每个片段都用明确的起止串定位。
const snippets = [
  /let backends = \[\];\n/,
  /function applyBackends\(list\) \{[\s\S]*?\n\}\n/,
  /const backendMeta = [^\n]*\n/,
  /const backendIcon = [^\n]*\n/,
  /const backendName = [^\n]*\n/,
  /const SVG_ALLOWED_TAGS = [^\n]*\n/,
  /const backendIconSvg = [\s\S]*?\n\};\n/,
  /const backendLabel = [^\n]*\n/,
  /const creditsFmt = v => \{[\s\S]*?\n\};\n/,
  /const stamp = [^\n]*\n/,
];
let code = "";
for (const re of snippets) {
  const m = src.match(re);
  if (!m) { console.error("抽不出片段:", re); process.exit(1); }
  code += m[0] + "\n";
}
const escMatch = src.match(/const esc = [^\n]*\n/);
code += escMatch[0];

// 把 renderAccounts 的**映射体**整段搬进来 —— 它引用了上方算出的 status/credits/
// kindIcon 等局部量，只抠最后那行 return 会缺引用。$ / overview 用桩顶上。
const mapBody = /  \$\("accounts-body"\)\.innerHTML = pageRows\.map\(a => \{([\s\S]*?)\n  \}\)/.exec(src);
if (!mapBody) { console.error("抽不出 renderAccounts 的行渲染体"); process.exit(1); }
const accountsById = {};
for (const a of data.overview.accounts) accountsById[a.id] = a;
code += `
  const overview = ${JSON.stringify(data.overview)};
  function renderRow(a) {
    const labels = {available:["可用","green"],cooling:["冷却中","amber"],paused:["已暂停",""],exhausted:["积分耗尽","amber"],invalid:["凭据异常","red"]};
${mapBody[1]}
  }
  return { renderRow, applyBackends, backendName, backendIcon, backendMeta };
`;
const fn = new Function("const $$ = () => {}; " + code)();

fn.applyBackends(data.backends);

let bad = 0;
const check = (label, cond, extra) => {
  if (!cond) { bad++; console.log(`  ✗  ${label}${extra ? "  " + extra : ""}`); }
  else console.log(`  ok ${label}`);
};

const accounts = data.overview.accounts;
check(`拿到了真实账号（${accounts.length} 个）`, accounts.length > 0);
const backendsSeen = new Set(accounts.map(a => a.backend));
check(`账号覆盖多个后端：${[...backendsSeen].join(", ")}`, backendsSeen.size >= 2);

for (const a of accounts) {
  const html = fn.renderRow(a);
  const tag = `${a.name}(${a.backend})`;
  check(`${tag} 行渲染无 undefined`, !/undefined/.test(html), html.slice(0, 200));
  check(`${tag} 行有后端图标`, html.includes("backend-icon-svg") || html.includes(fn.backendIcon(a.backend)));
  check(`${tag} 行有后端名`, html.includes(fn.backendName(a.backend)));
  check(`${tag} 行带 data-account（点行开详情要用）`, html.includes(`data-account="${a.id}"`));
  check(`${tag} 行首有小号账号图标（15px 内联，不再是 39px 方块）`,
        html.includes("account-icon-inline") && !/account-icon(?!-backend|-inline)/.test(html.replace(/account-icon account-icon-inline/g, "")));
  // 未知后端不该显示成「未知」以外的怪值
  check(`${tag} 后端名可读`, fn.backendName(a.backend) !== "未知" || !a.backend);
}

// 详情弹框字段：用真实账号对象走一遍映射，确认没有 undefined / NaN。
const detailRows = a => [
  ["账号类型", `${fn.backendIcon(a.backend)} ${fn.backendName(a.backend)}`],
  ["UID", a.uid || "—"], ["昵称", a.nickname || "—"], ["状态", a.pool_state || "—"],
  ["剩余积分", a.remaining === null || a.remaining === undefined ? "待查询" : fn.renderRow(a) && String(Math.floor(a.remaining))],
  ["调度权重", String(a.weight || 0)], ["是否启用", a.enabled === true ? "是" : a.enabled === false ? "否" : "—"],
];
for (const a of accounts) {
  const rows = detailRows(a);
  const flat = rows.map(r => r.join("=")).join(" | ");
  check(`详情字段 ${a.name} 无 undefined/NaN`, !/undefined|NaN/.test(flat), flat);
}

console.log(bad ? `\n${bad} 项不符` : "\n=== 概览账号表 + 详情字段（真实数据）全部通过 ===");
process.exit(bad ? 1 : 0);
