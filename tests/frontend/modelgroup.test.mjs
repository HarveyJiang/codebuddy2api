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
  slice("function modelBaseName(id) {", "\n}"),
  slice("const creditsText = m =>", ";\n"),
  slice("function modelsInGroup(list, key) {", "\n}"),
  // 后端元数据现在来自服务端（需求 2），这几个 helper 是它的唯一消费口。
  // 测试里注入一份与注册表一致的元数据，验证「元数据驱动」确实生效。
  slice("let backends = [];", "\n"), 
  slice("function applyBackends(list) {", "\n}"),
  slice("const backendMeta = b =>", ";\n"),
  slice("const backendOrder = b =>", ";\n"),
].join("\n");
const { groupModelsByName, modelBaseName, creditsText, modelsInGroup, applyBackends } =
  new Function(code + "\nreturn {groupModelsByName, modelBaseName, creditsText, modelsInGroup, applyBackends};")();

// 与 GET /admin/api/backends 的返回同构（顺序故意打乱，验证 applyBackends 会排序）。
applyBackends([
  { name: "trae", label: "Trae", short: "Trae", icon: "⚡", color: "#7c4dff", order: 1, prefix: "trae/" },
  { name: "workbuddy", label: "WorkBuddy", short: "WB", icon: "◈", color: "#2f6fed", order: 0, prefix: "" },
]);

let bad = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) bad++;
  console.log(`${ok ? "  ok" : "FAIL"}  ${label}${ok ? "" : `\n       得到 ${JSON.stringify(got)}\n       期望 ${JSON.stringify(want)}`}`);
};

// 用线上真实数据：7 个重名模型里最危险的是 step-5-preview（免费 vs 0.48）。
// v16 去前缀后，两条同名行的 `id` **相同**（`step-5-preview`），靠 `backend` 区分 ——
// 这正是「同 id 两行」的新形状，归组必须仍然把它们合到一起。
const list = [
  { id: "step-5-preview", backend: "workbuddy", credits: 0.0, max_input_tokens: 200000, canonical: "step-5-preview" },
  { id: "step-5-preview", backend: "trae", credits: 0.48, max_input_tokens: 200000, canonical: "step-5-preview" },
  { id: "hy3", backend: "workbuddy", credits: 1, max_input_tokens: 128000, canonical: "hy3" },
  { id: "kimi-k2.6", backend: "trae", credits: 0.75, max_input_tokens: 200000, canonical: "kimi-k2.6" },
  { id: "kimi-k2.6", backend: "workbuddy", credits: 0.52, max_input_tokens: 256000, canonical: "kimi-k2.6" },
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
check("两条的 id 现在相同（靠 backend 区分）",
      step.items.map(m => m.id).sort(), ["step-5-preview", "step-5-preview"]);

const kimi = groups.find(g => g.key === "kimi-k2.6");
check("kimi-k2.6 合并两条", kimi.items.length, 2);
check("上下文各自保留（256K / 200K 不能被平均）",
      kimi.items.map(m => m.max_input_tokens).sort((a, b) => a - b), [200000, 256000]);

check("hy3 单独成组（没有同名后端）",
      groups.find(g => g.key === "hy3").items.length, 1);

// ---- 跨后端归并靠**规范名**（canonical，用户 2026-10-16 拍板）----
// 上游两边 id 命名不一致（腾讯 `kimi-k3-1` vs 字节 `kimi-k3`），
// 只按裸名分组会显示成两行。且**不能按展示名猜**：hy3 / hy3-x 展示名都是
// 「Hy3」却是两个真模型。规范名由服务端按各后端 `canonical_for` 声明算好。
const aliasList = [
  // 裸名不同、规范名相同 → 应该合（服务端已把 canonical 都算成 kimi-k3）。
  { id: "kimi-k3-1", backend: "workbuddy", credits: 1.62, name: "Kimi-K3", canonical: "kimi-k3", max_input_tokens: 200000 },
  { id: "kimi-k3", backend: "trae", credits: 1.83, name: "Kimi-K3", canonical: "kimi-k3", max_input_tokens: 200000 },
  // 反例：展示名相同、**规范名不同** → 绝不能合（同一后端的两个模型，倍率 0 vs 0.05）。
  { id: "hy3", backend: "workbuddy", credits: 0, name: "Hy3", canonical: "hy3", max_input_tokens: 200000 },
  { id: "hy3-x", backend: "workbuddy", credits: 0.05, name: "Hy3", canonical: "hy3-x", max_input_tokens: 200000 },
];
const aliasGroups = groupModelsByName(aliasList);
check("同规范名合成一行（kimi-k3-1 + kimi-k3）", aliasGroups.length, 3);
const k3 = aliasGroups.find(g => g.items.some(m => m.id === "kimi-k3"));
check("归并行的两条都在（不是只留下一侧）",
      k3.items.map(m => m.id).sort(), ["kimi-k3", "kimi-k3-1"]);
check("归并行的组键就是规范名", k3.key, "kimi-k3");
// 这是最容易写错的一条：展示名相同**不是**同一个模型的判据。
check("展示名相同但规范名不同 → 不合并（hy3 / hy3-x 必须分开）",
      aliasGroups.filter(g => g.items.some(m => m.id === "hy3" || m.id === "hy3-x"))
                 .every(g => g.items.length === 1), true);
check("hy3 与 hy3-x 各自成组、倍率未被混",
      [aliasGroups.find(g => g.items[0].id === "hy3").items[0].credits,
       aliasGroups.find(g => g.items[0].id === "hy3-x").items[0].credits], [0, 0.05]);

// canonical 缺失（老库迁移后尚未同步）时回落到裸名：同名两行仍同组（id 相同）。
const legacyList = [
  { id: "glm-5.3", backend: "workbuddy", credits: 0.1 },
  { id: "glm-5.3", backend: "trae", credits: 0.2 },
  { id: "kimi-k2.6", backend: "workbuddy", credits: 0.5 },
];
const legacyGroups = groupModelsByName(legacyList);
check("canonical 缺失时回落到裸名分组（glm-5.3 两侧仍同组）",
      legacyGroups.find(g => g.items[0].id === "glm-5.3").items.length, 2);
check("canonical 缺失时不把所有行挤成一组", legacyGroups.length, 2);

// modelsInGroup 必须与分组结果一致（渲染与批量操作共用它，见 app.js 的说明）。
// 归并行的成员来自两个不同裸名，按裸名过滤只会切到一条 —— 这条钉住那个 bug。
check("modelsInGroup 对归并行返回全部成员",
      modelsInGroup(aliasList, k3.key).map(m => m.id).sort(), ["kimi-k3", "kimi-k3-1"]);
check("modelsInGroup 对普通组仍按规范名命中",
      modelsInGroup(list, "step-5-preview").length, 2);
check("modelsInGroup 对不存在的组返回空", modelsInGroup(list, "没有这个").length, 0);

// 顺序稳定：workbuddy 在前。
check("组内 workbuddy 排在 trae 前", step.items[0].backend, "workbuddy");

// **组的先后必须原样跟随输入顺序**（= 服务端 snapshot() 的四层排序结果）。
// 回归：前端曾在这里按「后端顺序 + 名字字母序」再排一次，把服务端排好的
// 来源/倍率顺序整个覆盖 —— 表现就是「设置页排序规则改了但页面不变」。
const ordered = groupModelsByName([
  { id: "zzz-custom", backend: "workbuddy", credits: 9 },
  { id: "aaa-upstream", backend: "workbuddy", credits: 0.1 },
  { id: "mmm-builtin", backend: "trae", credits: 5 },
]);
check("组间顺序 = 输入顺序（后端不再重排）",
      ordered.map(g => g.key), ["zzz-custom", "aaa-upstream", "mmm-builtin"]);
check("trae 组不会因为后端顺序被提到最前",
      ordered[0].key, "zzz-custom");

// modelBaseName 现在是**恒等函数**：v16 去前缀后 id 就是上游裸名，
// 没有前缀可剥。保留它是给归组一个明确的「裸名」概念（且不再读 backends[].prefix）。
check("modelBaseName 不再剥任何前缀", modelBaseName("trae/kimi-k2.6"), "trae/kimi-k2.6");
check("modelBaseName 原样返回", modelBaseName("glm-5.3"), "glm-5.3");
check("modelBaseName 对空值安全", modelBaseName(null), "");
// 上游自带 `/` 的名字也是原样（`openrouter/free` 不是我们的前缀）。
check("上游自带斜杠的名字原样", modelBaseName("openrouter/free"), "openrouter/free");

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
