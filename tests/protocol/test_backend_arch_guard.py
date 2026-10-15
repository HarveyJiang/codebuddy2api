"""架构守卫：调用方**不得**按后端名分支（需求 2 的核心不变量）。

## 为什么需要这个文件

第 3 阶段把「后端名硬编码」全部挪进了 `BackendSpec` 的能力声明，承诺是：

> 加第三个后端 = 新增一个 `BackendSpec` 子类 + 注册，pool / server / models /
> converter / 前端零改动。

这个承诺有一个讨厌的性质：**破坏它不会让任何行为测试变红。** 有人下次图省事再写一句
``if backend == "trae"``，当前两个后端的行为完全不变，640 条测试全绿——直到真要加
第三个后端时才发现要改 15 处。作者做过变异验证：把软冷却、默认后端、账号操作分派三处
改回按名字分支，跑全量 640 条**全部通过**。

所以「零改动」只能靠**结构**来守，不能靠行为。这个文件用源码扫描把两类写法钉死：

1. 调用方对后端名做**相等/包含比较**（`== "trae"` / `in ("workbuddy", "trae")`）；
2. 调用方使用**带后端名的标识符**（`trae_backend` / `_operate_trae` / `any_trae_manager`）。

哪些文件算「调用方」？见下面的 `CALLERS` —— 就是那些加了新后端本该不用打开的文件。
`core/backends/` 自己（`trae.py` / `workbuddy.py` 里写自己的名字天经地义）与
注释 / 文档字符串不在扫描范围内。

## 命中之后怎么办

先问「这处差异能不能声明成能力」。答案几乎总是能：

- 404 冷却时长 → `BackendSpec.soft_cooldown`
- reasoning / count_tokens 行为 → `keep_reasoning_delta` / `count_tokens_upstream`
- 凭据归属、expiresAt 单位 → `credential_backend` / `expires_at_to_ms`
- 积分签到由谁实现 → `account_ops_backend`
- 展示用的 label / icon / 颜色 / 顺序 → `label` / `icon` / `color` / `order`

如果**确实**是「全局唯一、与具体后端无关」的开关，用 `# arch-guard: ok` 在该行注明
理由（例如 `trae_enabled()` 里的 kill switch 环境变量），守卫会放过该行。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 被视为「调用方」的源码：加新后端时这些文件本应一行都不用改。
CALLERS = [
    "admin/pool.py",
    "admin/server.py",
    "admin/models.py",
    "admin/rotation.py",
    "admin/metrics.py",
    "admin/guards.py",
    "admin/static/app.js",
    "core/converter.py",
    "core/middleware.py",
]

#: 后端名。写死成字面量是判断「有人加了第三个后端但没更新这里」的依据。
BACKEND_NAMES = ("workbuddy", "trae")

#: 允许按后端名分支的地方（后端自己的实现文件 + 它的登录模块）。
ALLOWED_FILES = ("core/backends/",)


def _caller_files():
    for rel in CALLERS:
        p = ROOT / rel
        if p.exists():
            yield rel, p


def _iter_code_lines(path: Path):
    """产出 (行号, 代码行, 整行原文)，**跳过注释与文档字符串**。

    注释里提「trae」是在解释历史，不是在分支；只有能执行到的代码才算渗透点。
    第三个返回值是**未剥离的原文**：``arch-guard: ok`` 豁免标记写在注释里，
    剥完注释就看不见了。
    """
    src = path.read_text(encoding="utf-8")
    if path.suffix == ".js":
        # JS：逐行剥掉 // 与 /* */（app.js 里没有跨行模板字符串里嵌注释的情况）。
        in_block = False
        for i, line in enumerate(src.splitlines(), 1):
            out = line
            if in_block:
                end = out.find("*/")
                if end < 0:
                    continue
                out, in_block = out[end + 2:], False
            while True:
                start = out.find("/*")
                if start < 0:
                    break
                end = out.find("*/", start + 2)
                if end < 0:
                    out, in_block = out[:start], True
                    break
                out = out[:start] + out[end + 2:]
            slash = out.find("//")
            if slash >= 0:
                out = out[:slash]
            if out.strip() or "arch-guard: ok" in line:
                yield i, out, line
        return

    # Python：用 AST 找出所有 docstring 的行号，再据此剥掉注释与 docstring 行。
    tree = ast.parse(src)
    docstring_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                first = body[0]
                docstring_lines.update(range(first.lineno, first.end_lineno + 1))
    for i, line in enumerate(src.splitlines(), 1):
        if i in docstring_lines:
            continue
        code = line.split("#", 1)[0]
        if code.strip() or "arch-guard: ok" in line:
            yield i, code, line


#: 相等 / 包含比较：把后端名当**判据**用。
_COMPARISON = re.compile(
    r"""(!=|==)\s*["'](?P<lit1>%s)["']""" % "|".join(BACKEND_NAMES) +
    r"""|["'](?P<lit2>%s)["']\s*(!=|==)""" % "|".join(BACKEND_NAMES) +
    r"""|\bin\s*\((?P<tup>[^)]*)\)""" % {}
)


def _compares_backend_name(line: str) -> bool:
    """这一行是否把后端名当判据（相等比较，或 in 元组里出现后端名字面量）。

    只认**字面量**：`backend == spec.name` 是允许的（那是在读声明）。
    """
    for m in _COMPARISON.finditer(line):
        lit = m.groupdict().get("lit1") or m.groupdict().get("lit2")
        if lit in BACKEND_NAMES:
            return True
        tup = m.groupdict().get("tup")
        if tup is not None and any(f'"{n}"' in tup or f"'{n}'" in tup for n in BACKEND_NAMES):
            return True
    # 元组常量里的形式：("workbuddy", "trae")
    if re.search(r'\(\s*["\'](%s)["\']\s*,' % "|".join(BACKEND_NAMES), line):
        return True
    return False


#: 标识符形式的渗透：**为某一个后端写的分派入口**。
#:
#: 只认这两类，都是「加第三个后端就得再抄一份」的形状：
#:   - ``any_trae_manager`` / ``ensure_trae_builtin`` / ``refresh_trae_models``
#:     （为某一个后端写的便捷函数 —— 应改为带 backend 参数的通用函数）
#:   - ``_operate_trae`` / ``_trae_flow``
#:     （为某一个后端写的专用私有实现）
#:
#: **不**认变量名与模块名（``trae_flows`` / ``trae_login`` / ``workbuddy_admin``）：
#: 命名一个 Trae 相关的数据结构天经地义，不构成「加后端要改这里」。
_DISPATCH_VERB = r"(?:any|ensure|refresh|operate|select|load|fetch|make|build|get|run|do|note|apply|handle)"
_DISPATCH_IDENT = re.compile(
    r"\b(?:" + _DISPATCH_VERB + r")_(?:" + "|".join(BACKEND_NAMES) + r")\w*\b"
    r"|\b_\w*_(?:" + "|".join(BACKEND_NAMES) + r")\b"
)


def _uses_backend_identifier(line: str) -> str | None:
    m = _DISPATCH_IDENT.search(line)
    return m.group(0) if m else None


#: 调用方直接 import / __import__ 某一个后端的模块 —— 这也是按名字分派，
#: 只是形态更隐蔽：它不一定出现 ``== "trae"``，但一样是「加第三个后端就要再改这里」，
#: 而且更糟：它在 import 期就把那个后端的整套私有协议拖进来，破坏懒构造。
#:
#: 实例（真实踩过）：``from core.backends.trae import TraeAPIError`` 只为了给
#: ``except`` 挑异常类。改成 ``spec.upstream_error_class()`` 后顶部 import 消失，
#: 后端模块可以保持按需 import。
_IMPORTS_BACKEND = re.compile(
    r"\bfrom\s+core\.backends\.(?P<mod1>" + "|".join(BACKEND_NAMES) + r")\b"
    r"|\bimport\s+core\.backends\.(?P<mod2>" + "|".join(BACKEND_NAMES) + r")\b"
    r"|\bfrom\s+\.\s*import\s+(?P<mod3>" + "|".join(BACKEND_NAMES) + r")\b"
)


def _imports_backend_module(line: str) -> str | None:
    m = _IMPORTS_BACKEND.search(line)
    return m.group(0) if m else None


def test_callers_do_not_branch_on_backend_name():
    """调用方不得用 ``== "trae"`` / ``in ("workbuddy", "trae")`` 判断后端。

    这类判断是「加后端要改 N 处」的根因。改用 ``BackendSpec`` 的声明：
    软冷却、flags、account_ops_backend、credential_name 等。
    """
    offenders = []
    for rel, path in _caller_files():
        if any(rel.startswith(a) for a in ALLOWED_FILES):
            continue
        for lineno, line, raw in _iter_code_lines(path):
            if "arch-guard: ok" in raw:
                continue
            if _compares_backend_name(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "调用方出现按后端名分支的代码（加新后端时这些地方就得改）：\n  "
        + "\n  ".join(offenders)
        + "\n\n请把差异声明成 BackendSpec 的能力；确有必要时在该行加 `# arch-guard: ok` 并注明理由。"
    )


def test_callers_do_not_use_backend_specific_names():
    """调用方不得出现 ``trae_backend`` / ``any_trae_manager`` / ``_operate_trae`` 这类
    「为某一个后端写的」标识符 —— 它们是上一个后端留下的专用分支，加后端就要再抄一份。

    例外：``registry.trae_backend`` 那个**唯一**的懒构造入口（backend 模块自己的
    工厂），以及 kill switch 的 ``trae_enabled``——后者的理由是它读的是环境变量，
    与后端能力无关。两者都在注册表/后端模块内部，不在调用方文件里。
    """
    offenders = []
    for rel, path in _caller_files():
        if any(rel.startswith(a) for a in ALLOWED_FILES):
            continue
        for lineno, line, raw in _iter_code_lines(path):
            if "arch-guard: ok" in raw:
                continue
            tok = _uses_backend_identifier(line)
            if tok:
                offenders.append(f"{rel}:{lineno}: {tok}   |   {line.strip()}")
    assert not offenders, (
        "调用方出现「为某一个后端写的」标识符：\n  "
        + "\n  ".join(offenders)
        + "\n\n请改成通用名字（如 any_manager(backend) / _operate_nondefault(...)），"
          "确有例外时在该行加 `# arch-guard: ok`。"
    )


def test_callers_do_not_import_a_backend_module():
    """调用方不得 import 某一个后端的模块 —— 那是「按名字分派」的隐藏形态。

    它比 ``== "trae"`` 更难发现：代码里看不到后端名做判据，但「加第三个后端」
    时仍然要回来改这里（再写一个 import）。而且它在 **import 期**就把那个后端的
    整套私有协议（OAuth 流程、ExchangeToken 刷新、登录态解析）拖进了**所有**
    部署形态，即便这个部署根本没用那个后端 —— registry 的懒构造设计（见
    ``registry._load``）正是为了避免这件事。

    正确做法：把「用这个后端的什么」声明成能力。
    例如异常类 → ``BackendSpec.error_class_name`` / ``upstream_error_class()``。

    注意 ``registry.py`` 自己**不在** CALLERS 里：它是唯一的注册与懒构造入口，
    那里出现后端模块名是它的职责。
    """
    offenders = []
    for rel, path in _caller_files():
        if any(rel.startswith(a) for a in ALLOWED_FILES):
            continue
        for lineno, line, raw in _iter_code_lines(path):
            if "arch-guard: ok" in raw:
                continue
            tok = _imports_backend_module(line)
            if tok:
                offenders.append(f"{rel}:{lineno}: {tok}   |   {line.strip()}")
    assert not offenders, (
        "调用方 import 了某一个后端的模块（加新后端时要改，且会破坏懒构造）：\n  "
        + "\n  ".join(offenders)
        + "\n\n请把差异声明成 BackendSpec 的能力；确实需要该模块时在 registry/后端内部取。"
    )


def test_frontend_has_no_backend_literals():
    """前端不得自行登记后端元数据（图标 / 名称 / 顺序 / 前缀）。

    元数据只有一处来源：``GET /admin/api/backends``。前端自己留一张表，
    加了后端忘了同步就会**静默**显示成默认值 —— 正是需求 2 要消灭的那类 bug。
    """
    js = (ROOT / "admin/static/app.js").read_text(encoding="utf-8")
    offenders = []
    for i, line in enumerate(js.splitlines(), 1):
        code = line.split("//", 1)[0]
        if "arch-guard: ok" in line or not code.strip():
            continue
        # BACKEND_ICON / BACKEND_NAME 这类前端自留表（曾经就是这么写的）。
        if re.search(r"BACKEND_(ICON|NAME|LABEL|ORDER|COLOR)\s*=", code):
            offenders.append(f"admin/static/app.js:{i}: {line.strip()}")
        # 后端名当判据。
        if _compares_backend_name(code):
            offenders.append(f"admin/static/app.js:{i}: {line.strip()}")
        # 硬编码的前缀剥离（应走 modelBaseName 读元数据）。
        if re.search(r"replace\(/\^trae", code):
            offenders.append(f"admin/static/app.js:{i}: {line.strip()}")
    assert not offenders, (
        "前端出现后端名硬编码 / 自留元数据表：\n  " + "\n  ".join(offenders)
        + "\n\n元数据一律经 /admin/api/backends 获取；确有必要时加 `// arch-guard: ok`。"
    )


def test_backend_metadata_has_a_single_source():
    """服务端也只有一个元数据来源：BackendSpec 的声明。

    防止有人图省事在 admin 侧另起一张 ``{workbuddy: {...}, trae: {...}}`` 的表——
    那样后端声明就形同虚设了。
    """
    for rel in ("admin/server.py", "admin/models.py", "admin/pool.py", "admin/static/app.js"):
        path = ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if "arch-guard: ok" in line:
                continue
            code = line.split("#", 1)[0]
            if re.search(r'["\'](%s)["\']\s*:\s*\{' % "|".join(BACKEND_NAMES), code):
                pytest.fail(
                    f"{rel}:{i} 疑似在前端/管理侧自建后端元数据表：{line.strip()}\n"
                    "  元数据应由 BackendSpec 声明并经 backend_meta() 输出。"
                )
