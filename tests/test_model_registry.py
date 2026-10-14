"""Model registry: upstream pull, builtin fallback, enable/disable management."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]

from admin.models import ModelRegistry, BUILTIN_ORDER, BUILTIN_MODELS, PROMOTIONS, active_promo, CN, _credits_multiplier


class FakeManager:
    def __init__(self, headers=None):
        self.headers = headers or {"Authorization": "Bearer token", "X-Domain": "workbuddy.cn"}
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

    def test_fetch_parses_upstream_and_skips_disabled(self):
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            self.assertTrue(registry.fetch())
        snap = registry.snapshot()
        ids = [m["id"] for m in snap["models"]]
        # disabled 上游模型与不在 cli agent 列表里的 ghost 均不出现。
        self.assertEqual(ids, ["glm-5.2", "step-5-preview", "space-bunny"])
        glm = snap["models"][0]
        self.assertEqual(glm["credits"], 0.79)
        self.assertEqual(glm["max_input_tokens"], 1000000)
        self.assertEqual(glm["source"], "upstream")
        self.assertTrue(glm["enabled"])
        # modelPromotions 被解析（enabled: False 的活动被剔除）。
        self.assertEqual(len(registry.promotions), 1)
        self.assertEqual(registry.promotions[0]["label"], "夜间折扣")
        self.assertEqual(registry.promotions[0]["factor"], 0.5)

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

    def test_disable_and_persist_state(self):
        registry = ModelRegistry(get_credential=lambda: FakeManager())
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = upstream_payload()
        with patch("admin.models.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.get.return_value = response
            registry.fetch()
        state = registry.set_enabled("step-5-preview", False)
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
