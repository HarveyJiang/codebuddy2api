"""Model registry: upstream pull, builtin fallback, enable/disable management."""
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

# 项目根由 tests/conftest.py 统一放进 sys.path（测试按功能域分布在子目录里，
# 各自拼相对层级会在移动目录时算错）。

from admin.models import ModelRegistry, BUILTIN_ORDER, BUILTIN_MODELS, PROMOTIONS, USER_AGENT, active_promo, CN, _credits_multiplier


class FakeManager:
    def __init__(self, headers=None):
        self.headers = headers or {"Authorization": "Bearer token", "X-Domain": "workbuddy.cn",
                                   "User-Agent": "codebuddy2openai/2.0"}
    def get_headers(self):
        return self.headers

def upstream_payload():
    """产品配置接口（/console/enterprises/personal/product-config）返回形态。"""
    return {
        "models": [
            {"id": "glm-5.2", "name": "GLM-5.2", "credits": "x0.79", "maxInputTokens": 1000000},
            {"id": "step-5-preview", "name": "Step-5-Preview", "credits": "x0.00", "maxInputTokens": 128000},
            {"id": "space-bunny", "name": "Space-Bunny", "credits": "", "maxInputTokens": 64000},
            {"id": "old-model", "name": "Old", "disabled": True},
        ],
        "agents": [{"name": "cli", "models": ["glm-5.2", "step-5-preview", "space-bunny", "old-model", "ghost-model"]}],
        "modelPromotions": [
            {"kind": "discount", "enabled": True, "modelIds": ["glm-5.2"],
             "badge": {"label": "夜间折扣"},
             "discount": {"factor": 0.5},
             "schedule": {"daily": [{"start": "23:00", "end": "7:50"}], "timezone": "Asia/Shanghai"}},
            {"kind": "discount", "enabled": False, "modelIds": ["glm-5.2"],
             "badge": {"label": "停用"}, "discount": {"factor": 0.1},
             "schedule": {"daily": [{"start": "00:00", "end": "24:00"}]}},
        ],
    }


class ModelRegistryTests(unittest.TestCase):
    def test_credits_multiplier(self):
        self.assertEqual(_credits_multiplier("x0.06 credits"), 0.06)
        self.assertEqual(_credits_multiplier("x5.00 credits"), 5.0)
        self.assertIsNone(_credits_multiplier(""))
        self.assertIsNone(_credits_multiplier(None))

    def test_snapshot_does_not_deadlock_with_promotions(self):
        """snapshot() 持锁期间调用 active_promo() → promotions 再取锁。
        非重入锁会自死锁，表现为 /admin/api/overview 永久挂起。"""
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            registry.fetch()
        # 主线程内联调用 snapshot()，另起看门狗线程在超时后置位。**不要**写成
        # 「另起线程调 snapshot()，主线程 done.wait(5)」：后者一旦真的死锁，主线程
        # 会先把这 5 秒耗光，测试才失败——那 5 秒就是这条测试的被测行为，所以标 slow。
        result = {}

        def call():
            try:
                result["snap"] = registry.snapshot()
            except BaseException as exc:
                result["exc"] = exc

        watcher = threading.Thread(target=call, daemon=True)
        watcher.start()
        watcher.join(timeout=5)
        if watcher.is_alive():
            self.fail("snapshot() 自死锁：active_promo 在持锁时回取 promotions 锁")
        if "exc" in result:
            raise result["exc"]
        self.assertTrue(result["snap"]["models"])

    def test_fetch_empty_shell_reports_credential_hint(self):
        # 上游对无凭据请求返回 {"code":0,"data":{"agents":null,"models":null}}
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = {"code": 0, "data": {"agent": {"agents": None}, "models": None}}
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            self.assertFalse(registry.fetch())
        self.assertIn("凭据", registry.snapshot()["error"])
        # 失败保留内置列表，不崩溃。
        registry.ensure_builtin()
        self.assertEqual(registry.enabled_models(), BUILTIN_ORDER)

    def test_fetch_parses_upstream_and_skips_disabled(self):
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            self.assertTrue(registry.fetch())
            # 未登录时上游只返回空壳，必须带账号凭据（Authorization）请求；
            # 账号凭据里的 UA 必须让位给 CLI UA，否则上游 400 check ua。
            sent = client_cls.return_value.__enter__.return_value.get.call_args.kwargs["headers"]
            self.assertEqual(sent.get("Authorization"), "Bearer token")
            self.assertEqual(sent.get("User-Agent"), USER_AGENT)
        snap = registry.snapshot()
        ids = [m["id"] for m in snap["models"]]
        # disabled 上游模型与不在 cli agent 列表里的 ghost 均不出现；
        # 三档智能路由固定附加（source=builtin），展示按四层排序
        # （specs/model-backend-attribution.md §6）：builtin 整体排在 upstream 之前。
        self.assertEqual(ids, ["fast-model", "balanced-model", "deep-model",
                               "step-5-preview", "space-bunny", "glm-5.2"])
        # 上游无 descriptionZh 时用内置中文描述兜底，且不掺英文。
        step = next(m for m in snap["models"] if m["id"] == "step-5-preview")
        self.assertEqual(step["description"],
                         BUILTIN_MODELS["step-5-preview"]["description"])
        glm = next(m for m in snap["models"] if m["id"] == "glm-5.2")
        self.assertEqual(glm["credits"], 0.79)
        self.assertEqual(glm["max_input_tokens"], 1000000)
        self.assertEqual(glm["source"], "upstream")
        self.assertTrue(glm["enabled"])
        # modelPromotions 被解析（enabled: False 的活动被剔除）。
        self.assertEqual(len(registry.promotions), 1)
        self.assertEqual(registry.promotions[0]["label"], "夜间折扣")
        self.assertEqual(registry.promotions[0]["factor"], 0.5)

    def test_description_chinese_only(self):
        """描述只保留中文：有 descriptionZh 时不掺英文，缺失时用内置兜底。"""
        payload = upstream_payload()
        payload["models"] = [
            {"id": "glm-5.2", "name": "GLM-5.2", "credits": "x0.79", "maxInputTokens": 1000000,
             "descriptionZh": "旗舰模型，擅长复杂软件工程", "descriptionEn": "Flagship model"},
            # 上游无任何描述 -> 内置中文兜底。
            {"id": "space-bunny", "name": "Space-Bunny", "credits": "", "maxInputTokens": 64000},
        ]
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = payload
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            self.assertTrue(registry.fetch())
        desc = {m["id"]: m["description"] for m in registry.snapshot()["models"]}
        self.assertEqual(desc["glm-5.2"], "旗舰模型，擅长复杂软件工程")
        self.assertEqual(desc["space-bunny"], BUILTIN_MODELS["space-bunny"]["description"])

    def test_snapshot_orders_by_source_then_credits(self):
        """三层排序（specs/model-backend-attribution.md §6，启用已撤销）：

        ① 来源 custom(0) → builtin(1) → upstream(2)
        ② 归属（注册表顺序；本用例单后端）
        ③ 倍率升序（None 视为 0）
        禁用与否**不参与**：开关前后行序纹丝不动（点开关不能让行跳走）。
        """
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            registry.fetch()
        # 禁用两条（内置 deep-model、上游最便宜的 step-5-preview）：
        # 若「启用」还在排序键里，被禁用的 deep-model 会掉进上游段、
        # step-5-preview 会沉到上游末尾 —— 断言立刻红。
        registry.set_enabled("step-5-preview", False)
        registry.set_enabled("deep-model", False)
        ids = [m["id"] for m in registry.snapshot()["models"]]
        # ① builtin 整体在 upstream 之前（与启用状态无关）；
        # ③ 上游组内按倍率升序（step-5-preview 免费 = 0 最前，glm-5.2 ×0.79 最后），
        #    step-5-preview 被禁用也**不沉底**。
        self.assertEqual(ids, ["fast-model", "balanced-model", "deep-model",
                               "step-5-preview", "space-bunny", "glm-5.2"])

    def test_fetch_failure_keeps_previous_snapshot(self):
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            registry.fetch()
        first = registry.enabled_models()
        # 第二次失败：保持上次缓存。
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.side_effect = Exception("network down")
            self.assertFalse(registry.fetch())
        self.assertEqual(registry.enabled_models(), first)
        self.assertIsNotNone(registry.snapshot()["error"])

    def test_fetch_always_appends_routing_tiers(self):
        # /v3/config 的 models 列表不含 fast/balanced/deep，但官方固定提供，
        # fetch 后必须无条件出现在可用列表并以内置倍率填充。
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            self.assertTrue(registry.fetch())
        for tier in ("fast-model", "balanced-model", "deep-model"):
            self.assertIn(tier, registry.enabled_models())
            info = registry._catalog[tier]
            self.assertEqual(info["credits"], BUILTIN_MODELS[tier]["credits"])
            self.assertEqual(info["max_input_tokens"], 300000)
            self.assertEqual(info["name"], BUILTIN_MODELS[tier]["name"])

    def test_fetch_network_failure_records_error(self):
        registry = ModelRegistry(get_credential=lambda: None)
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.side_effect = Exception("network down")
            self.assertFalse(registry.fetch())
        self.assertIn("拉取失败", registry.snapshot()["error"])
        # 无数据时回落内置列表。
        registry.ensure_builtin()
        self.assertEqual(registry.enabled_models(), BUILTIN_ORDER)

    def test_custom_model_persists_across_restart(self):
        """手动添加的模型要落库、重启后仍在。

        available=0 时若只按 available=1 加载，重启后用户保存的模型会消失——
        这正是 load_models 需要 include_custom 的原因。
        """
        import tempfile
        from admin.db import Database
        root = tempfile.mkdtemp()
        path = Path(root) / "state.db"
        db = Database(path)
        registry = ModelRegistry(get_credential=lambda: None)
        registry.ensure_builtin()
        registry.add("ghost-model", name="幽灵", credits=0.25, max_input_tokens=64000)
        registry.persist(db)
        row = db.query("SELECT source, available, enabled FROM models WHERE model_id='ghost-model'")[0]
        self.assertEqual((row["source"], row["available"], row["enabled"]), ("custom", 1, 1))
        db.close()

        # 模拟重启：新连接 + 新 registry。
        db2 = Database(path)
        restored = ModelRegistry(get_credential=lambda: None)
        restored.load(db2)
        self.assertTrue(restored.is_custom("ghost-model"))
        self.assertIn("ghost-model", restored.enabled_models())
        self.assertIn("ghost-model", restored.snapshot_ids())
        entry = next(m for m in restored.snapshot()["models"] if m["id"] == "ghost-model")
        self.assertEqual(entry["name"], "幽灵")
        self.assertEqual(entry["credits"], 0.25)
        self.assertTrue(entry["custom"])
        db2.close()

    def test_custom_model_loads_when_available_zero(self):
        """上游没返回（available=0）的自定义行也必须被加载进来。"""
        import tempfile
        from admin.db import Database
        root = tempfile.mkdtemp()
        path = Path(root) / "state.db"
        db = Database(path)
        db.save_models({"ghost-model": {"name": "幽灵", "source": "custom",
                                        "available": 0, "enabled": 1, "backend": "workbuddy"}})
        registry = ModelRegistry(get_credential=lambda: None)
        registry.load(db)
        self.assertIn("ghost-model", registry.enabled_models())
        db.close()

    def test_disable_and_persist_state(self):
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            registry.fetch()
        registry.set_enabled("step-5-preview", False)
        self.assertEqual(registry.enabled_models(),
                         ["glm-5.2", "space-bunny", "fast-model", "balanced-model", "deep-model"])
        self.assertIn("step-5-preview", registry.snapshot_ids())
        # 落库后由新实例读回，禁用状态保留（走真实的持久化路径）。
        import tempfile
        from admin.db import Database
        root = tempfile.mkdtemp()
        db = Database(Path(root) / "state.db")
        registry.persist(db)
        restored = ModelRegistry(get_credential=lambda: FakeManager())
        restored.load(db)
        db.close()
        self.assertEqual(restored.enabled_models(),
                         ["glm-5.2", "space-bunny", "fast-model", "balanced-model", "deep-model"])
        # 再拉取时禁用跟随仍存在的模型。
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            restored.fetch()
        self.assertEqual(restored.enabled_models(),
                         ["glm-5.2", "space-bunny", "fast-model", "balanced-model", "deep-model"])

    def test_custom_model_survives_fetch(self):
        """手动添加的模型必须活过每次自动拉取（启动 + 每 6 小时）。

        fetch() 原来是整体替换 _catalog/_available，用户加的模型会被直接冲掉——
        这是本功能最容易写错的地方，所以单独钉一条。
        """
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            registry.fetch()
            registry.add("ghost-model", name="幽灵模型", credits=0.25,
                         max_input_tokens=64000, description="上游菜单没列出")
            self.assertEqual(registry.snapshot_ids() & {"ghost-model"}, {"ghost-model"})
            # 再拉一次：上游仍然不返回它，但它必须还在。
            registry.fetch()
        self.assertIn("ghost-model", registry.snapshot_ids())
        entry = next(m for m in registry.snapshot()["models"] if m["id"] == "ghost-model")
        self.assertTrue(entry["custom"])
        self.assertEqual(entry["source"], "custom")
        self.assertEqual(entry["name"], "幽灵模型")
        self.assertEqual(entry["credits"], 0.25)
        self.assertTrue(entry["enabled"])
        # _available 里自定义项追加在末尾，不打乱上游按单价排好的顺序
        # （snapshot() 展示时会再按单价重排，所以这里查内部顺序）。
        self.assertEqual(registry._available[-1], "ghost-model")

    def test_custom_model_kept_when_upstream_later_lists_it(self):
        """上游后来也返回了同一 id：元数据以上游为准，但来源仍是自定义。

        否则下一次拉取时 _custom 就断了，用户保存的项会「变成上游的」而被冲掉。
        """
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        registry.ensure_builtin()
        # 一个上游目录与内置列表里都没有的 id：必须走「新建为自定义」分支。
        registry.add("ghost-model", name="我自己起的名字", credits=9.9)
        self.assertTrue(registry.is_custom("ghost-model"))
        response = unittest.mock.Mock(status_code=200)
        payload = upstream_payload()
        payload["models"] = [{"id": "ghost-model", "name": "上游的名字",
                              "credits": "x0.79", "maxInputTokens": 1000000}]
        payload["agents"][0]["models"] = ["ghost-model"]
        response.json.return_value = payload
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            registry.fetch()
        entry = next(m for m in registry.snapshot()["models"] if m["id"] == "ghost-model")
        self.assertTrue(entry["custom"])
        self.assertEqual(entry["source"], "custom")
        # 上游的元数据生效（以上游为准），而不是用户填的。
        self.assertEqual(entry["name"], "上游的名字")
        self.assertEqual(entry["credits"], 0.79)
        self.assertEqual(entry["max_input_tokens"], 1000000)

    def test_add_existing_upstream_model_stays_upstream(self):
        """给上游已有的模型改名字不会把它变成自定义，否则它就不跟着上游刷新了。"""
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        registry.ensure_builtin()
        self.assertEqual(registry.add("hy3", name="新名字"), "builtin")
        self.assertFalse(registry.is_custom("hy3"))
        self.assertEqual(registry.info("hy3")["name"], "新名字")
        self.assertIn("hy3", registry.snapshot_ids())

    def test_remove_only_allows_custom(self):
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        registry.ensure_builtin()
        registry.add("mine", name="我的模型")
        registry.remove("mine")
        self.assertNotIn("mine", registry.snapshot_ids())
        self.assertFalse(registry.is_custom("mine"))
        # 上游/内置模型只能禁用，不能删。
        with self.assertRaises(ValueError):
            registry.remove("hy3")
        with self.assertRaises(KeyError):
            registry.remove("never-existed")

    def test_removed_custom_model_does_not_come_back_on_reload(self):
        """删除必须落库 available=0，否则**重启后复活**。

        既有测试只验了「fetch 后不回来」——那条走内存，抓不住这个 bug
        （实测：删完重启，模型原样回来）。这条走 load(db)，是真路径。
        """
        import tempfile
        from admin.db import Database
        path = Path(tempfile.mkdtemp()) / "state.db"
        db = Database(path)
        try:
            registry = ModelRegistry(get_credential=lambda: FakeManager())
            registry.ensure_builtin()
            registry.add("doomed-model", name="待删")
            registry.persist(db)
            registry.remove("doomed-model", db=db)
            # 库里列为保留行（供日志 join），但必须 deleted=1。
            row = db.query("SELECT deleted, available FROM models WHERE model_id='doomed-model'")[0]
            self.assertEqual(row["deleted"], 1)
            self.assertNotIn("doomed-model", db.load_models(only_available=False))
            # 重启：重新 load，模型不得复活。
            fresh = ModelRegistry(get_credential=lambda: FakeManager())
            fresh.load(db)
            self.assertNotIn("doomed-model", fresh.snapshot_ids())
            self.assertNotIn("doomed-model", fresh.enabled_models())
        finally:
            db.close()

    def test_removed_prefixed_custom_model_does_not_come_back_on_reload(self):
        """带前缀的自定义模型同样要能删掉且不复活（`:path` 路由 + 落库）。"""
        import tempfile
        from admin.db import Database
        path = Path(tempfile.mkdtemp()) / "state.db"
        db = Database(path)
        try:
            registry = ModelRegistry(get_credential=lambda: FakeManager())
            registry.ensure_builtin()
            registry.add("trae/doomed-k3", name="待删 K3")
            registry.persist(db)
            registry.remove("trae/doomed-k3", db=db)
            row = db.query("SELECT deleted FROM models WHERE model_id='trae/doomed-k3'")[0]
            self.assertEqual(row["deleted"], 1)
            fresh = ModelRegistry(get_credential=lambda: FakeManager())
            fresh.load(db)
            self.assertNotIn("trae/doomed-k3", fresh.snapshot_ids())
        finally:
            db.close()

    def test_removed_custom_model_does_not_come_back_on_fetch(self):
        """删除必须同时清掉 _custom，否则下一次合并又把它加回来。"""
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            registry.fetch()
            registry.add("temp-model", name="临时")
            registry.remove("temp-model")
            registry.fetch()
        self.assertNotIn("temp-model", registry.snapshot_ids())

    def test_custom_model_available_even_when_upstream_absent(self):
        """自定义模型 enable 即可调用，不看 available —— 上游没返回它就是它的定义。"""
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        registry.ensure_builtin()
        registry.add("hidden-one", name="隐藏模型")
        self.assertIn("hidden-one", registry.enabled_models())
        registry.set_enabled("hidden-one", False)
        self.assertNotIn("hidden-one", registry.enabled_models())
        # 但仍是「已知」：PoolMiddleware 靠这个把它判成「已禁用」而不是「未知」。
        self.assertIn("hidden-one", registry.snapshot_ids())

    def test_set_enabled_unknown_model_raises(self):
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        registry.ensure_builtin()
        with self.assertRaises(KeyError):
            registry.set_enabled("no-such-model", True)

    def test_builtin_list_matches_official_cli_menu(self):
        # 与 /v3/config 的 agents[cli].models 顺序一致；三档智能路由（官方固定提供）
        # 不在 cli 菜单里，附加在末尾。
        self.assertEqual(BUILTIN_ORDER, [
            "hy4-preview", "hy3", "hy3-x", "step-5-preview", "space-bunny",
            "deepseek-v4.1-flash", "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1",
            "glm-5v-turbo", "minimax-m3", "minimax-m2.7", "kimi-k3-1", "kimi-k2.8-preview",
            "kimi-k2.7", "kimi-k2.6", "deepseek-v4-pro",
            "fast-model", "balanced-model", "deep-model",
        ])
        self.assertEqual(set(BUILTIN_ORDER), set(BUILTIN_MODELS))
        # 官方倍率抽查。
        self.assertEqual(BUILTIN_MODELS["space-bunny"]["credits"], 0.08)
        self.assertEqual(BUILTIN_MODELS["step-5-preview"]["credits"], 0.0)
        self.assertEqual(BUILTIN_MODELS["glm-5.3"]["credits"], 0.79)
        self.assertEqual(BUILTIN_MODELS["kimi-k3-1"]["credits"], 1.62)

    def test_active_promo_daytime_and_night(self):
        from datetime import datetime as dt
        # 深夜 00:30：glm-5.2 命中夜间折扣 0.5。
        night = dt(2026, 10, 14, 0, 30, tzinfo=CN)
        promo = active_promo("glm-5.2", night)
        self.assertEqual(promo, ("夜间折扣", 0.5))
        # 白天 12:00：glm-5.2 无活动。
        self.assertIsNone(active_promo("glm-5.2", dt(2026, 10, 14, 12, 0, tzinfo=CN)))
        # hy3 限时免费在有效期内全天生效。
        self.assertEqual(active_promo("hy3", dt(2026, 10, 14, 12, 0, tzinfo=CN)), ("限时免费", 0.0))
        # hy3 免费期结束（11 月 1 日起）后无活动。
        self.assertIsNone(active_promo("hy3", dt(2026, 11, 1, 12, 0, tzinfo=CN)))
        # 无活动的模型不受影响。
        self.assertIsNone(active_promo("kimi-k3-1", night))

    def test_snapshot_includes_promo_pricing(self):
        from datetime import datetime
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        registry.ensure_builtin()
        # 2026-10-14 深夜：glm-5.2 折后 0.79 * 0.5 = 0.395。
        night = datetime(2026, 10, 14, 0, 30, tzinfo=CN)
        with patch("admin.models.datetime") as mock_dt:
            mock_dt.now.return_value = night
            snap = registry.snapshot()
        glm = next(m for m in snap["models"] if m["id"] == "glm-5.2")
        self.assertEqual(glm["promo"]["label"], "夜间折扣")
        self.assertEqual(glm["promo"]["credits"], 0.395)
        # 无活动的模型不带 promo 字段。
        kimi = next(m for m in snap["models"] if m["id"] == "kimi-k3-1")
        self.assertNotIn("promo", kimi)


if __name__ == "__main__":
    unittest.main()

import pytest  # noqa: E402  (标记需要)

# ---------------------------------------------------------------------------
# 分层标记（见 pytest.ini / tests/README.md）
#
# 本文件整体属于「core」。标记写在文件里而不是逐条标注：同一文件的用例
# 保护的是同一块行为，分域跑（`pytest tests/pool`）时就该一起跑。
#   core    : 必须守住的不变量，默认只跑这些
#   e2e     : 起整套 create_app 走真实请求链路
#   network : 经由 mock 传输层模拟上游交互
# 若某条用例其实不属于本文件默认域（例如纯形状断言、只测常量的），
# 请在该用例上单独加 @pytest.mark.<域>，或在下面改用更准确的一档。
# ---------------------------------------------------------------------------
pytestmark = [pytest.mark.core]

