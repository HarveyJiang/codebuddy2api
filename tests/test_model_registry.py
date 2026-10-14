"""Model registry: upstream pull, builtin fallback, enable/disable management."""
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]

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
        done = threading.Event()

        def call():
            snap = registry.snapshot()
            self.assertTrue(snap["models"])
            done.set()

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        if not done.wait(timeout=5):
            self.fail("snapshot() 自死锁：active_promo 在持锁时回取 promotions 锁")

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
        # 三档智能路由固定附加；展示按单价升序（None 视为 0）。
        self.assertEqual(ids, ["step-5-preview", "space-bunny", "fast-model",
                               "balanced-model", "glm-5.2", "deep-model"])
        # 上游无 descriptionZh 时用内置中文描述兜底，且不掺英文。
        self.assertEqual(snap["models"][0]["description"],
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

    def test_snapshot_orders_disabled_last(self):
        """展示排序：启用的在前、禁用的在后；组内按单价升序（None 视为 0）。"""
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            registry.fetch()
        # 禁用掉最便宜（step-5-preview ×0.00）与最贵（deep-model ×1.20）的。
        registry.set_enabled("step-5-preview", False)
        registry.set_enabled("deep-model", False)
        ids = [m["id"] for m in registry.snapshot()["models"]]
        # 启用组按单价升序；禁用组排在最后，也按单价升序。
        self.assertEqual(ids, ["space-bunny", "fast-model", "balanced-model", "glm-5.2",
                               "step-5-preview", "deep-model"])

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
                                        "available": 0, "enabled": 1}})
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
        state = registry.export_state()
        self.assertEqual(registry.enabled_models(),
                         ["glm-5.2", "space-bunny", "fast-model", "balanced-model", "deep-model"])
        self.assertIn("step-5-preview", registry.snapshot_ids())
        # 重新导入状态后禁用保留。
        restored = ModelRegistry(get_credential=lambda: FakeManager())
        restored.import_state(json.loads(json.dumps(state)))
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
