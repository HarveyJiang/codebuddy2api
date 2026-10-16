"""裸名路由规则单测（specs/model-routing-by-policy.md §5 + §9.3）。

`core/bare_routing.py` 是纯函数，所以这里不建 app、不碰 DB —— 直接把候选与顺序
喂进去，逐条钉住规则。每一条都对应文档里的一句话（在断言前注明出处）。

## 为什么规则要测得这么细

§5 的三步（候选集 / 排序 / 降级）是「一次请求落到哪个后端」的唯一判据，
错了不会崩，只会**悄悄打错后端**（用户的钱与延迟都受影响）。而且它有两个调用方
（pool 中间件 + converter），两处必须一致 —— 规则本身正确是前提。
"""
from __future__ import annotations

import unittest

from core.bare_routing import (
    BackendOrderSpec,
    Candidate,
    Decision,
    NoCandidate,
    Rejection,
    UNPRICED,
    build_order,
    decide,
)

# 注册表顺序：默认后端（WorkBuddy）在前（§9.2）。
REGISTRY = ("workbuddy", "trae")


def cand(backend, credits, mid=None):
    """构造候选。`mid` 省略时用带前缀的 id（贴近真实：Trae 行 id 带前缀）。"""
    if mid is None:
        mid = f"{backend}/glm-5.3" if backend != "workbuddy" else "glm-5.3"
    return Candidate(backend=backend, credits_micro=credits, model_id=mid)


class OrderBuildingTests(unittest.TestCase):
    """顺序的来源与「是否显式配置」（§9.4 / §9.3 豁免二）。"""

    def test_default_order_is_registry_order(self):
        """不配置 → 注册表顺序，且不算「显式配置」（价格红线仍生效）。"""
        order, explicit = build_order(REGISTRY)
        self.assertEqual(order, REGISTRY)
        self.assertFalse(explicit)

    def test_global_config_overrides_and_counts_as_explicit(self):
        order, explicit = build_order(REGISTRY, configured=["trae", "workbuddy"])
        self.assertEqual(order, ("trae", "workbuddy"))
        self.assertTrue(explicit)

    def test_key_config_wins_over_global(self):
        """单 key 非空 → 用它（照抄 `_routing_for` 的 `item.get(...) or pool...` 形状）。"""
        order, explicit = build_order(REGISTRY, configured=["workbuddy", "trae"],
                                      key_configured=["trae", "workbuddy"])
        self.assertEqual(order, ("trae", "workbuddy"))
        self.assertTrue(explicit)

    def test_empty_key_config_falls_back_to_global(self):
        """单 key 留空 = 跟随全局（NULL 的语义）。"""
        order, explicit = build_order(REGISTRY, configured=["trae", "workbuddy"],
                                      key_configured=None)
        self.assertEqual(order, ("trae", "workbuddy"))
        self.assertTrue(explicit)
        # 空列表与 None 同义（迁移默认值可能是 '[]'）。
        order2, _ = build_order(REGISTRY, configured=["trae", "workbuddy"],
                                key_configured=[])
        self.assertEqual(order2, ("trae", "workbuddy"))

    def test_unknown_backend_names_are_ignored(self):
        """用户配了已不存在的后端名 → 忽略，不能让它占位次（否则顺序被搅乱）。"""
        order, _ = build_order(REGISTRY, configured=["ghost", "trae"])
        self.assertEqual(order, ("trae", "workbuddy"))
        self.assertNotIn("ghost", order)

    def test_backends_missing_from_config_are_appended(self):
        """只配了一个后端 → 其余按注册表顺序追加（不是「只认配过的」）。"""
        order, _ = build_order(REGISTRY, configured=["trae"])
        self.assertEqual(order, ("trae", "workbuddy"))

    def test_duplicates_collapse(self):
        order, _ = build_order(REGISTRY, configured=["trae", "trae", "workbuddy"])
        self.assertEqual(order, ("trae", "workbuddy"))


class CandidateSelectionTests(unittest.TestCase):
    """§5.1 候选集 + §5.2 排序：选谁、没有怎么办。"""

    def order(self, **kw):
        return BackendOrderSpec(order=REGISTRY, **kw)

    def test_single_candidate_is_chosen_without_ambiguity(self):
        """§5.4：只有一个候选时直接走，策略退化（不写特例分支，是候选集的推论）。"""
        d = decide("glm-5.3", [cand("workbuddy", 790_000)], self.order())
        self.assertEqual(d.backend, "workbuddy")
        self.assertFalse(d.is_ambiguous)

    def test_default_order_picks_default_backend_first(self):
        """§9.2：静态优先级 = 注册表顺序，默认后端在前。"""
        d = decide("glm-5.3", [cand("trae", 790_000), cand("workbuddy", 790_000)], self.order())
        self.assertEqual(d.backend, "workbuddy")
        self.assertTrue(d.is_ambiguous)
        self.assertEqual(d.candidates, ("workbuddy", "trae"))

    def test_user_order_can_put_trae_first(self):
        """用户调序 → 第一名随之改变（这是「策略」而非「写死默认后端」的证据）。"""
        spec = BackendOrderSpec(order=("trae", "workbuddy"), explicit_order=True)
        d = decide("glm-5.3", [cand("workbuddy", 790_000), cand("trae", 790_000)], spec)
        self.assertEqual(d.backend, "trae")

    def test_no_candidate_lists_every_rejection(self):
        """§5.1 末段：候选为空 → 400，且逐个说明「哪个后端因为什么出局」。"""
        with self.assertRaises(NoCandidate) as ctx:
            decide("glm-5.3", [], self.order(),
                   rejections=[Rejection("trae", "后端已被管理员禁用")])
        msg = str(ctx.exception)
        self.assertIn("glm-5.3", msg)
        self.assertIn("trae", msg)
        self.assertIn("后端已被管理员禁用", msg)

    def test_single_backend_deployment_works(self):
        """§5.4 大前提：只有默认后端的部署必须完整可用（Trae 下线后照常）。"""
        spec = BackendOrderSpec(order=("workbuddy",))
        d = decide("glm-5.3", [cand("workbuddy", 790_000)], spec)
        self.assertEqual(d.backend, "workbuddy")


class PriceRedlineTests(unittest.TestCase):
    """§9.3 价格红线：自动选择只往便宜/同价方向切。"""

    def order(self, **kw):
        return BackendOrderSpec(order=REGISTRY, **kw)

    def test_equal_price_keeps_first(self):
        d = decide("m", [cand("workbuddy", 500_000), cand("trae", 500_000)], self.order())
        self.assertEqual(d.backend, "workbuddy")

    def test_cheaper_later_candidate_is_kept_not_chosen(self):
        """便宜的一方**保留**但不改选（策略选第一，红线只负责踢贵的）。

        静态优先级下第一名恒为默认后端 —— 用户拍板的语义就是「策略选后端」，
        价格红线是护栏不是排序键。这条防止有人把红线误实现成「选最便宜的」。
        """
        d = decide("m", [cand("workbuddy", 900_000), cand("trae", 100_000)], self.order())
        self.assertEqual(d.backend, "workbuddy", "仍选策略第一名，不是最便宜的")
        self.assertEqual(d.candidates, ("workbuddy", "trae"),
                         "便宜的候选保留在候选集里")

    def test_more_expensive_candidate_is_rejected(self):
        """贵的方向出局（§9.3 表格：高于基准 → 出局）。"""
        d = decide("m", [cand("workbuddy", 100_000), cand("trae", 900_000)], self.order())
        self.assertEqual(d.backend, "workbuddy")
        self.assertEqual(list(d.candidates), ["workbuddy"],
                         "贵的候选不在（保留的）候选集里")
        self.assertTrue(any(r.backend == "trae" for r in d.rejected),
                        "出局原因里必须有 trae（排障要看）")

    def test_unpriced_is_treated_as_most_expensive(self):
        """§9.3：`credits_micro` 为 NULL 视为 +∞，只有显式前缀能选中。"""
        d = decide("m", [cand("workbuddy", 1), cand("trae", None)], self.order())
        self.assertEqual(d.backend, "workbuddy")
        self.assertTrue(any(r.backend == "trae" for r in d.rejected))
        self.assertEqual(cand("trae", None).price, UNPRICED)

    def test_unpriced_first_does_not_reject_cheaper_second(self):
        """基准为「按标准计费」时，更便宜的候选不该被踢（基准 = 第一名，不是最小值）。"""
        d = decide("m", [cand("workbuddy", None), cand("trae", 100)], self.order())
        self.assertEqual(d.backend, "workbuddy")
        self.assertEqual(list(d.candidates), ["workbuddy", "trae"])

    def test_explicit_order_skips_the_price_redline(self):
        """§9.3 豁免二：用户显式配过顺序 = 知情选择，**贵的一方排第一也能选中**。

        构造要让红线**真的会拦**：把贵的 `trae` 放在策略第一位、便宜的 `workbuddy`
        放第二位。红线只踢「比基准贵」的候选（基准 = 第一名 = trae），所以
        workbuddy 便宜、不会被踢 —— 光这样还证明不了豁免。真正证明豁免的是
        **候选集里 workbuddy 必须存活**，而这只在豁免生效时……两者都会存活。

        所以要构造**反向**：让**便宜的在第一位、贵的在第二位**，此时红线会踢
        第二位的贵候选。豁免生效 ⇒ 贵的被保留；豁免失效 ⇒ 贵的出局。
        这条正是「贵的方向要显式前缀」在**用户已显式配序**时的例外。
        """
        # 便宜的前置（workbuddy），贵的后置（trae）——红线本会踢掉 trae。
        spec = BackendOrderSpec(order=("workbuddy", "trae"), explicit_order=True)
        d = decide("m", [cand("workbuddy", 100_000), cand("trae", 900_000)], spec)
        self.assertEqual(d.backend, "workbuddy")
        self.assertEqual(list(d.candidates), ["workbuddy", "trae"],
                         "显式配序时贵的候选也要存活（豁免二）")
        self.assertEqual([r.backend for r in d.rejected], [],
                         "豁免生效时不该有任何价格出局原因")

    def test_explicit_prefix_skips_the_price_redline(self):
        """§9.3 豁免一：带显式前缀的请求整条规则跳过（用户自己的选择）。

        构造同上：便宜在前、贵在后。豁免生效 ⇒ 贵候选存活。
        """
        spec = BackendOrderSpec(order=("workbuddy", "trae"), explicit_prefix=True)
        d = decide("m", [cand("workbuddy", 100_000), cand("trae", 900_000)], spec)
        self.assertEqual(list(d.candidates), ["workbuddy", "trae"])
        self.assertEqual([r.backend for r in d.rejected], [])

    def test_free_vs_paid_never_auto_selects_paid(self):
        """真实数据形态：step-5-preview 腾讯免费(0) / 字节 0.48。

        「免费变收费是不可接受的惊喜」（§9.3 原话）—— 这条是那条款的具象化。
        """
        d = decide("step-5-preview",
                   [cand("workbuddy", 0, "step-5-preview"),
                    cand("trae", 480_000, "trae/step-5-preview")],
                   self.order())
        self.assertEqual(d.backend, "workbuddy")
        self.assertEqual(list(d.candidates), ["workbuddy"])


class DecisionShapeTests(unittest.TestCase):
    """返回值的形状契约（调用方据此反查上游模型名）。"""

    def test_decision_carries_the_rows_model_id(self):
        """选中的 `model_id` 必须是**该后端那一行**的 id（带前缀），调用方要用它。"""
        d = decide("glm-5.3",
                   [cand("workbuddy", 1, "glm-5.3"), cand("trae", 1, "trae/glm-5.3")],
                   BackendOrderSpec(order=("trae", "workbuddy"), explicit_order=True))
        self.assertEqual(d.backend, "trae")
        self.assertEqual(d.model_id, "trae/glm-5.3")

    def test_decision_is_immutable_and_serializable_shape(self):
        d = decide("m", [cand("workbuddy", 1)], BackendOrderSpec(order=REGISTRY))
        self.assertIsInstance(d, Decision)
        with self.assertRaises(Exception):
            d.backend = "other"


if __name__ == "__main__":
    unittest.main()
