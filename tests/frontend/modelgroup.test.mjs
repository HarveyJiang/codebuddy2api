// 同名模型跨后端合并展示（需求 8）的分组逻辑单测。
//
// 关键不变量：**只合并展示，不合并数值**。step-5-preview 在腾讯免费(0.0)、
// 字节 0.48，若归组时把倍率平均或取首个，成本账本与排队降级会把付费当免费。
// 所以这里既验「同名归到一组」，也验「组内两个 item 的原值都还在」。
import { readFileSync } from "node:fs";
const src = readFileSync("admin/static/app.js", "utf8");

// 抽取几个纯函数。用各自的源码形态精确定位；app.js 里 const 箭头函数与 function
// 混用、行尾还有分号差异，所以每个片段各有明确的起止串。
function slice(from, to) {
  const start = src.indexOf(from);
  if (start < 0) throw new Error(`找不到 ${from}`);
  const end = src.indexOf(to, start);
  if (end < 0) throw new Error(`找不到 ${from} 的结尾 ${to}`);
  return src.slice(start, end + to.length);
}
const code = [
  slice("function groupModelsByName(list) {", "\n}"),
  slice("const modelBaseName = id =>", ";\n"),
  slice("const creditsText = m =>", ";\n"),
  slice("function modelsInGroup(list, key) {", "\n}"),
].join("\n");
const { groupModelsByName, modelBaseName, creditsText, modelsInGroup } =
  new Function(code + "\nreturn {groupModelsByName, modelBaseName, creditsText, modelsInGroup};")();

let bad = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) bad++;
  console.log(`${ok ? "  ok" : "FAIL"}  ${label}${ok ? "" : `\n       得到 ${JSON.stringify(got)}\n       期望 ${JSON.stringify(want)}`}`);
};

// 用线上真实数据：7 个重名模型里最危险的是 step-5-preview（免费 vs 0.48）。
const list = [
  { id: "step-5-preview", backend: "workbuddy", credits: 0.0, max_input_tokens: 200000 },
  { id: "trae/step-5-preview", backend: "trae", credits: 0.48, max_input_tokens: 200000 },
  { id: "hy3", backend: "workbuddy", credits: 1, max_input_tokens: 128000 },
  { id: "trae/kimi-k2.6", backend: "trae", credits: 0.75, max_input_tokens: 200000 },
  { id: "kimi-k2.6", backend: "workbuddy", credits: 0.52, max_input_tokens: 256000 },
];

const groups = groupModelsByName(list);
check("5 个模型归成 3 组", groups.length, 3);

const step = groups.find(g => g.key === "step-5-preview");
check("step-5-preview 合并了两条", step.items.length, 2);
// 原始倍率必须一个都没被改（免费 vs 付费是成本账本的判据）。
check("组内保留 workbuddy 免费价 0.0",
      step.items.find(m => m.backend === "workbuddy").credits, 0);
check("组内保留 trae 价 0.48",
      step.items.find(m => m.backend === "trae").credits, 0.48);
check("两条 id 都还在（未被改写成同一个）",
      step.items.map(m => m.id).sort(), ["step-5-preview", "trae/step-5-preview"]);

const kimi = groups.find(g => g.key === "kimi-k2.6");
check("kimi-k2.6 合并两条", kimi.items.length, 2);
check("上下文各自保留（256K / 200K 不能被平均）",
      kimi.items.map(m => m.max_input_tokens).sort((a, b) => a - b), [200000, 256000]);

check("hy3 单独成组（没有同名后端）",
      groups.find(g => g.key === "hy3").items.length, 1);

// 顺序稳定：workbuddy 在前。
check("组内 workbuddy 排在 trae 前", step.items[0].backend, "workbuddy");

// 前缀剥离只针对 trae/，不能误伤名字里带 trae 的模型。
check("modelBaseName 剥 trae/ 前缀", modelBaseName("trae/kimi-k2.6"), "kimi-k2.6");
check("modelBaseName 不剥非前缀", modelBaseName("mytrae/x"), "mytrae/x");
check("modelBaseName 对空值安全", modelBaseName(null), "");

// 倍率措辞：0 必须显示「免费」而不是「×0」，否则用户以为收费但为 0。
check("倍率 0 显示免费", creditsText({ credits: 0 }), "免费");
check("倍率 null 显示标准", creditsText({ credits: null }), "标准");
check("倍率 0.48 原样显示", creditsText({ credits: 0.48 }), "×0.48");

// 渲染与批量操作必须对「一个组有哪些条目」有**同一个答案**。
// 若两边各算一遍，将来改了归组规则就会出现「显示 2 条、批量只操作到 1 条」，
// 而且界面不会报错——这类静默不一致最难查。
for (const g of groups) {
  const viaHelper = modelsInGroup(list, g.key).map(m => m.id).sort();
  const viaRender = g.items.map(m => m.id).sort();
  check(`组 ${g.key} 的成员：渲染与批量判断一致`,
        JSON.stringify(viaRender), JSON.stringify(viaHelper));
}
check("modelsInGroup 对空/异常输入不抛错",
      JSON.stringify(modelsInGroup(null, "hy3")), JSON.stringify([]));
check("modelsInGroup 只返回本组条目",
      modelsInGroup(list, "hy3").map(m => m.id), ["hy3"]);

console.log(bad ? `=== ${bad} 个失败 ===` : "=== 模型合并分组全部通过 ===");
process.exit(bad ? 1 : 0);
