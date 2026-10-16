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

#: 允许按后端名分支的地方（后端自己的实现文件、它的登录模块，以及注册表本身）。
#:
#: registry.py 在这里是**有意**的：它是唯一允许出现后端名的地方 —— 它持有
#: `_REGISTRY` 那张表，其它模块一律从声明派生。这条豁免配了两条锁
#: （见 tests/protocol/test_backend_registry.py）：
#: `test_registration_is_a_single_table` 管「表还在」，
#: `test_registry_modules_have_no_stray_backend_branches` 管「表外的按名分支不超两处」。
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
    # 从包里直接取模块名（from core.backends import trae）。
    # ``\b`` 边界保证不会误伤 ``from core.backends import trae_login``。
    r"|\bfrom\s+core\.backends\s+import\s+(?P<mod4>" + "|".join(BACKEND_NAMES) + r")\b"
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
    与后端能力无关（总闸上线后它已降级为「只看环境门」的兼容入口，整体启停问
    ``is_enabled``）。两者都在注册表/后端模块内部，不在调用方文件里。
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


def test_callers_do_not_route_by_model_prefix():
    """调用方**不得**再用「模型 id 前缀」判归属（v16 去前缀的核心不变量）。

    去前缀前，`trae/glm-5.2` 靠 `prefix_for` / `strip_prefix` 判后端。改造后
    这些函数**已删除**，id 就是上游裸名 —— 归属只有两个正确来源：

    - 某一行**存下来**的归属：读 `models.backend` 列（`admin/models.py` 的 `load`）；
    - 此刻**该走谁**：`admin/pool.py` 的裸名策略（§5）/ `converter._owner_backend_for`。

    为什么必须守：`model_prefix` 声明**故意保留**（`admin/db.py` 的两个历史迁移
    要靠它认老库里的前缀行）。只要声明还在，就有人会再写一句
    `strip_prefix(model)` 去做运行期判定 —— 那会立刻把 `openrouter/free` 这类
    **真实含斜杠的上游名**误伤成别的模型，且没有任何行为测试会红（今天没有
    部署真的用这种模型名调过）。

    允许出现的地方：`admin/db.py`（迁移）与 `core/backends/`（后端自己的声明）。
    """
    offenders = []
    for rel, path in _caller_files():
        if any(rel.startswith(a) for a in ALLOWED_FILES):
            continue
        for lineno, line, raw in _iter_code_lines(path):
            if "arch-guard: ok" in raw:
                continue
            if re.search(r"\b(prefix_for|strip_prefix|backend_name_for_prefix)\b", line) \
                    or re.search(r"\.model_prefix\b", line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "调用方按模型 id 前缀判归属（v16 已去前缀，这会把含斜杠的真名误伤）：\n  "
        + "\n  ".join(offenders)
        + "\n\n请改用 `models.backend` 列（存下来的归属）或 pool 的裸名策略（此刻该走谁）。"
    )


def test_callers_do_not_test_model_ids_for_a_backend_prefix():
    """调用方**不得**对模型 id 做「带不带某后端前缀」的字符串判定。

    去前缀前这类写法到处都是（`mid.startswith("trae/")` 判归属、
    `model.split("/")[0]` 取后端名）。v16 后 id 就是上游裸名，这类判定
    **恒为假**（不会有 `trae/` 了），却会静默把真正含斜杠的上游名
    （`openrouter/free`）误判成「Trae 的模型」。

    为什么必须守：这类代码**不会让任何行为测试变红** —— 今天没有部署真的用
    含斜杠的上游名调过接口，所以误判永远走不到。它是纯粹的定时炸弹。

    正确的来源：
    - 某一行**存下来的**归属 → `models.backend` 列；
    - 此刻**该走谁** → `admin/pool.py` 的裸名策略 / `registry.pinned_model_owner`。
    """
    offenders = []
    # 判据要分清「模型 id」与「API 路径」：
    #   - `mid.startswith("trae/")`、`mid.split("/")[0]` —— 模型 id 判据，**要拦**；
    #   - `api("trae/flow/" + id)` —— 那是 **URL 路径**（登录流程端点），不是模型
    #     id。它的 `"trae/"` 是路由前缀，与后端声明无关，**不能**拦。
    # 区分方式：模型 id 的判据出现在 `.startswith(` / `.split("/")[0]` 里
    # （下面两条），而 URL 拼接不是；单独的字面量 `"trae/"` 只在**没有** api( 前缀时
    # 才算可疑。
    id_prefix_check = re.compile(
        r"""\.startswith\(\s*["'](?:%s)""" % "|".join(BACKEND_NAMES)
        + r"""|\.split\(\s*["']/["']\s*\)\s*\[\s*0\s*\]"""
    )
    # 裸的前缀字面量（如 `"trae/" + mid`）：排除 URL 拼接（`api("trae/...")`）。
    literal_prefix = re.compile(r"""["'](?:%s)/["']""" % "|".join(BACKEND_NAMES))
    for rel, path in _caller_files():
        if any(rel.startswith(a) for a in ALLOWED_FILES):
            continue
        for lineno, line, raw in _iter_code_lines(path):
            if "arch-guard: ok" in raw:
                continue
            hit = id_prefix_check.search(line)
            if not hit and literal_prefix.search(line):
                # 排除 API 路径拼接：`api("trae/flow/" …)` / `"trae/callback/"` 等。
                # 判据是「这个字面量紧跟在 `api(` 或一个 URL-ish 上下文里」。
                if not re.search(r"""\bapi\(\s*["'](?:%s)/""" % "|".join(BACKEND_NAMES), line) \
                        and not re.search(r"""["']/(?:admin/)?api/(?:%s)/""" % "|".join(BACKEND_NAMES), line):
                    hit = literal_prefix.search(line)
            if hit:
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "调用方按「模型 id 带某后端前缀」判归属（v16 已去前缀，这类判定恒假，"
        "且会误伤含斜杠的真实上游名）：\n  "
        + "\n  ".join(offenders)
        + "\n\n请改用 `models.backend` 列（存下来的归属）或 pool 的裸名策略（此刻该走谁）；"
          "确有必要时加 `# arch-guard: ok` 并注明理由。"
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
        # 硬编码的前缀剥离（v16 已去前缀：id 就是上游裸名，没有前缀可剥）。
        # 曾经这里靠 `replace(/^trae/, ...)` 与 `modelBaseName()` 剥前缀 —— 两者都删了。
        # 拦住「有人再把前缀处理加回来」：那会把上游真实名（`openrouter/free`）误伤。
        if re.search(r"replace\(/\^\s*(?:trae|workbuddy)", code):
            offenders.append(f"admin/static/app.js:{i}: {line.strip()}")
        if re.search(r"\bmodelBaseName\b", code):
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
