"""tests/ 的公共装置：把项目根目录放进 sys.path，并登记自定义标记。

**为什么需要这里**：测试会按功能域放进 tests/<domain>/ 子目录，子目录里的
文件用相对层级去猜项目根（``parent.parent``）在移动目录时就会算错。统一在
这里锚定一次，各测试文件不必再自己拼路径。

跨模块共用的 helper（如 ``credential``）也放这里：此前 tests/ 下的文件互相
import 兄弟测试模块（``from test_admin_server import credential``），一旦按
功能域拆到子目录就会 ImportError。

标记（marker）用于分层跑测试——见 pytest.ini：

- ``core``   ：必须守住的不变量（数据正确性、安全、资金、迁移、后端隔离）。
               当前**全部用例**都标了 core，所以 `-m core` 等同于全量。
- ``slow``   ：内部有固定等待/超时，跑得慢。pytest.ini 默认 `-m "not slow"`，
               即**跑全部、只排除明确标慢的**，而不是「只跑 core」——后者会让忘打
               标记的新测试被静默 deselect 而输出仍是全绿。当前没有用例使用它：
               原来几处固定等待都已改成事件驱动或按需触发。
- ``e2e``    ：起整套 create_app + ASGI 客户端走真实请求链路。
- ``network``：用 mock 上游/传输层模拟网络交互。
- ``real_catalog_check``：需要真实的 ``_stale_catalog``（不被本文件的 autouse
               装置桩掉），见下面的 ``no_upstream_catalog_fetch``。

标记是**叠加**的：一条测试可以同时是 core + e2e。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def no_upstream_catalog_fetch(monkeypatch, request):
    """桩掉**启动路径**上的上游目录拉取，免得测试真连网。

    ``create_app()`` 启动时会判断模型目录是否过期，过期就**同步**去拉上游
    （``admin/server.py`` 的 ``_stale_catalog`` → ``admin/models.py`` 的
    ``ModelRegistry.fetch``，一个真实 HTTPS 请求）。内置目录恒被判为过期，所以
    每个用 ``create_app`` 的用例都会白跑一趟网络：实测全量里有 20 次真实出网，
    正常网络下约 4 秒，而在**出网被丢包**的环境里每次会一路撞到
    ``FETCH_TIMEOUT=15``（``admin/models.py``），最坏能把全量拖长几分钟。

    桩的是 ``_stale_catalog``（启动时问「要不要拉」的那个判断）而不是
    ``fetch`` 本身：直接桩 ``fetch`` 会把「测 fetch 自己」的一批用例一起挡掉
    （它们自己 mock 上游，本来就不出网）。桩掉判断等于回答「不用拉」，效果一样，
    而 ``ModelRegistry`` 的所有行为测试照旧走真实 ``fetch``。

    要测 ``_stale_catalog`` 本身的用例用 ``@pytest.mark.real_catalog_check``
    声明自己需要真函数（见 tests/admin/test_server.py::test_stale_catalog_detection）。
    """
    from admin import server as admin_server
    if "real_catalog_check" in request.keywords:
        return
    monkeypatch.setattr(admin_server, "_stale_catalog", lambda registry: False)
    # 同一处也要挡住「非默认后端没拉到过就拉一次」（``_refresh_missing_catalogs``）：
    # 它同样会在启动时**同步**出网（Trae 拿自己的账号打上游）。测试里的凭据是
    # 假的，真打必然超时；要测这个函数的用例直接 import 它自己调，不经启动路径。
    monkeypatch.setattr(admin_server, "_refresh_missing_catalogs", lambda registry: False)


def credential(uid="one", name="测试账号"):
    """一份最小可用的账号凭据文档（账号身份 + 一份未过期的 token）。

    管理后台、账号池、浏览器登录等多处测试都要造它，所以放在公共装置里，
    而不是让测试互相 import 兄弟模块。
    """
    return {
        "account": {"uid": uid, "nickname": name, "enterpriseId": "test"},
        "auth": {"accessToken": "private-access-token",
                 "refreshToken": "private-refresh-token",
                 "expiresAt": int(time.time() * 1000) + 3600000},
    }


def build_pool(tmp_path, infos, handler, *, clock=1800000000, api_key="api-test-key",
               admin_key="admin-long-enough-test-key"):
    """起一个装了指定凭据文件的账号池，上游由 ``handler`` 打桩。

    几处容易踩的坑都在这里替你处理掉：

    - 凭据文件必须落在 ``auth_dir`` 下（``Store.manager_for`` 会按路径取）。
      前端/协议测试此前各写一遍，容易忘；
    - ``create_app`` 的启动路径会真去拉上游模型目录 —— 这由 ``tests/conftest.py``
      的 autouse 装置统一挡住，这里不必再管；
    - ``client_factory`` 返回的 client 走 ``httpx.MockTransport(handler)``，
      所以积分/签到这类**别人会真打上游**的代码路径在这里全部离线。

    返回 ``AccountPool``。要注意的是本函数**不**构造 FastAPI app（多数用例只
    需要池）；需要 HTTP 层的用例请用 ``admin.server.create_app``。
    """
    import httpx

    from admin.pool import AccountPool
    from admin.server import Store

    tmp_path = Path(tmp_path)
    (tmp_path / "auth").mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "management", tmp_path / "auth", api_key, admin_key)
    for info in infos:
        store.save_browser_account(json.loads(Path(info).read_text(encoding="utf-8")),
                                   Path(info).stem)
    return AccountPool(store, clock=lambda: clock,
                       client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))


class recorded_no_sleep(list):
    """替换 ``asyncio.sleep`` 的替身：立即返回，但把请求的秒数记下来。

    直接是 ``list`` 的子类，所以 ``len`` / 迭代 / 下标 / ``clear()`` 全都能用。
    用途是压掉生产路径里的等待而不丢掉「等了多久」这个可断言的事实——例如换号
    退避（``backoff_after``：0.5s、1s、2s…带抖动）在只验顺序的用例里纯属白等。
    """

    # 绑定真实实现：__call__ 里若写 asyncio.sleep(0)，会被 patch 打回自己身上，
    # 变成无限递归。
    _real_sleep = asyncio.sleep

    async def __call__(self, seconds):
        self.append(seconds)
        await recorded_no_sleep._real_sleep(0)


def pytest_configure(config):
    for line in (
        "core: 必须守住的核心不变量",
        "slow: 内部有固定等待/超时，明显较慢（默认不跑）",
        "e2e: 起整套应用走真实请求链路",
        "network: 通过 mock 模拟上游或传输层",
        "real_catalog_check: 需要真实的 _stale_catalog（不要被 autouse 装置桩掉）",
    ):
        config.addinivalue_line("markers", line)
