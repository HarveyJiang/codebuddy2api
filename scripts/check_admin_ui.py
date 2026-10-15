"""静态检查：设置页的 DOM 结构与 JS 引用是否自洽。

不替代真浏览器，但能挡住最常见的一类回归——把元素搬走却忘了改 JS 里的 id
引用（反之亦然），表现是「点了没反应」或控制台报 null。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
html = (ROOT / "admin/static/index.html").read_text(encoding="utf-8")
js = (ROOT / "admin/static/app.js").read_text(encoding="utf-8")

failures = []

# 1) 每个 $("id") / getElementById("id") 引用的元素都必须存在。
ids = set(re.findall(r'\bid="([^"]+)"', html))
for ref in set(re.findall(r'\$\("([^"]+)"\)', js)):
    if ref not in ids:
        failures.append(f'JS 引用了不存在的 id: "{ref}"')

# 2) 导航按钮与页面容器必须一一对应（少一个就是点不进去/白屏）。
nav = set(re.findall(r'data-page="([^"]+)"', html))
# 只认「页面容器」：id="page-xxx" 出现在 class="page-panel" 的元素上。
panels = set(re.findall(r'id="page-([\w-]+)" class="page-panel"', html))
# 需求 6 之后「账号池」并进概览：page-accounts 仍存在，但只是给旧深链
# （#accounts）一个「已并入概览」的落地页，侧边栏不再有它。
REDIRECT_PAGES = {"accounts"}
if nav - panels:
    failures.append(f"有导航按钮但没有对应页面: {sorted(nav - panels)}")
if panels - nav - REDIRECT_PAGES:
    failures.append(f"有页面但没有导航入口: {sorted(panels - nav - REDIRECT_PAGES)}")
# 反向：redirect 页不该再出现在侧边栏（需求 6 明确要求移除）。
if REDIRECT_PAGES & nav:
    failures.append(f"侧边栏不应再有这些页签: {sorted(REDIRECT_PAGES & nav)}")

# 3) labels 里必须给每个页面写好标题/眉题/说明，否则 goPage 解构会 undefined。
labels = dict(re.findall(r'^\s*(\w+): \[("[^"]*", ){0,1}', js, re.M))
label_block = re.search(r"const labels = \{(.*?)\n\};", js, re.S)
if not label_block:
    failures.append("找不到 labels 定义")
else:
    defined = set(re.findall(r"^\s*(\w+):", label_block.group(1), re.M))
    if panels - defined - REDIRECT_PAGES:
        failures.append(f"页面缺少 labels 条目: {sorted(panels - defined - REDIRECT_PAGES)}")

# 4) 设置页必须真的装了那些设置控件，别只搬了标题把控件落在账号池页。
settings_panel = re.search(r'<section id="page-settings".*?</section>', html, re.S)
if not settings_panel:
    failures.append("找不到 page-settings 区块")
else:
    block = settings_panel.group(0)
    for element in ("pool-settings", "auto-checkin", "checkin-time", "batch-spread",
                    "credit-interval", "sync-start", "sync-end", "pool-routing",
                    "queue-failover", "queue-model-fallback", "queue-retry",
                    "queue-cooldown", "queue-hold-ms", "queue-fallback-model",
                    "backup-card", "backup-enabled", "backup-interval", "backup-keep",
                    "backup-now", "backup-save", "backup-body", "backup-empty",
                    "backup-badge", "backup-schedule", "backup-dir-note"):
        if f'id="{element}"' not in block:
            failures.append(f"设置页缺少控件: {element}")

# 5) 账号池页不该再留着设置控件（搬走了就得搬干净）。
accounts_panel = re.search(r'<section id="page-accounts".*?</section>', html, re.S)
if accounts_panel:
    for element in ("auto-checkin", "queue-failover", "backup-enabled", "pool-routing"):
        if f'id="{element}"' in accounts_panel.group(0):
            failures.append(f"账号池页仍残留设置控件: {element}")
    # 需求 6：账号池并进概览后，这一页只该是一句「已并入概览」的说明。
    if 'id="accounts-body"' in accounts_panel.group(0):
        failures.append("账号池页仍留着账号表格 —— 它应该只在概览里")
else:
    failures.append("找不到 page-accounts 区块")

# 6) 标签配对（正则粗查，够挡住漏闭合）。
for tag in ("section", "form", "article", "table", "dialog", "tbody", "thead"):
    opened = len(re.findall(rf"<{tag}[\s>]", html))
    closed = len(re.findall(rf"</{tag}>", html))
    if opened != closed:
        failures.append(f"<{tag}> 开 {opened} 闭 {closed}，不匹配")

# 7) JS 语法（交给调用方跑 node --check，这里只提示）。
if "node_modules" in js:
    failures.append("app.js 里出现 node_modules 引用")

if failures:
    print("❌ 静态检查未通过：")
    for item in failures:
        print("   -", item)
    sys.exit(1)
print(f"✅ 静态检查通过（{len(ids)} 个 id、{len(panels)} 个页面、导航与 labels 一致）")
