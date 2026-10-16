"""X-Request-ID 用 UUIDv7（`core/converter._uuid7_hex`）。

## 为什么这个值重要

上游从 ``X-Request-ID`` 的**时间位**还原请求时刻。UUIDv4 的那 48 位是随机值，
解析不出任何有意义的毫秒数，于是上游要么渲染出错乱的时刻，要么判为不可解析、
改用自己那套 ``crb-`` 前缀 ID。UUIDv7 把 unix 毫秒时间戳放在最高 48 位
（RFC 9562 §5.7），上游不需要额外的时间头就能还原时刻。

## 为什么两条实现路径都要测

生产镜像是 Python 3.14，走标准库的 ``uuid.uuid7()``；本地 / 某些 CI 若低于
3.14，走 :func:`_uuid7_fallback_hex` 的手工实现。两条路径产出的 ID 必须
**同样符合规范**——否则本地测过的那条在生产上是另一套行为，而本地根本测不到。
所以这里对两条路径跑同一组断言，而不是只测当前解释器碰巧走的那条。

另外单独钉住：Trae 后端**不得**受影响（它的 header 是自建的 `_solo_headers`）。
"""
from __future__ import annotations

import time
import unittest
import uuid

from core import converter
from core.converter import _uuid7_fallback_hex, _uuid7_hex


def _assert_conforming_v7(testcase, hex_id, label):
    """RFC 9562 的形状断言 + 时间位可还原，两条实现路径共用。"""
    testcase.assertEqual(len(hex_id), 32, f"{label}: 应为 32 位无连字符 hex")
    testcase.assertTrue(all(c in "0123456789abcdef" for c in hex_id),
                        f"{label}: 应为小写 hex，实际 {hex_id}")
    # version nibble 固定为 7（第 13 个字符）
    testcase.assertEqual(hex_id[12], "7", f"{label}: version nibble 应为 7，实际 {hex_id[12]}")
    # variant nibble 取 0b10 → 落在 8/9/a/b（第 17 个字符）
    testcase.assertIn(hex_id[16], "89ab",
                      f"{label}: variant nibble 应为 8/9/a/b，实际 {hex_id[16]}")
    # 高 48 位是 unix 毫秒时间戳 —— 这是整个改动的意义所在，必须能还原
    ts_ms = int(hex_id[:12], 16)
    now_ms = time.time_ns() // 1_000_000
    testcase.assertLess(abs(ts_ms - now_ms), 60_000,
                        f"{label}: 时间位 {hex_id[:12]} 还原出来距今过远，不是有效的 v7")


class Uuid7ShapeTests(unittest.TestCase):
    """两条实现路径的形状都要对。"""

    def test_active_implementation_is_conformant(self):
        """当前解释器实际走的那个（3.14 内置 / 低版本兜底）必须合规。"""
        for _ in range(50):
            _assert_conforming_v7(self, _uuid7_hex(), "当前实现")

    def test_fallback_implementation_is_conformant(self):
        """手工兜底实现必须与内置产出同样的规范。"""
        for _ in range(50):
            _assert_conforming_v7(self, _uuid7_fallback_hex(), "兜底实现")

    def test_both_paths_agree_on_shape(self):
        """两条路径的**结构**一致（不只是各自碰巧合规）。

        只比结构、不比时间位：两次调用可能跨毫秒，比前缀必然随机红。
        结构位（长度 / version 位 / variant 位）才是「两条实现是同一个格式」
        真正的证据 —— 前缀里的时间位各测各的已在上面两条里断言过了。
        """
        mine = _uuid7_fallback_hex()
        theirs = _uuid7_hex()
        self.assertEqual(len(mine), len(theirs))
        self.assertEqual(mine[12], "7")
        self.assertEqual(theirs[12], "7")
        # variant nibble 是 0b10 + rand_b 高 2 位，本身**随机**（8/9/a/b 都合法），
        # 两个合法 v7 在这一位不同是正常的 —— 所以检查「都在合法集合内」而不是
        # 「彼此相等」。早先写成 assertEqual 就是这条测试随机红的真因。
        for name, value in (("兜底", mine), ("内置", theirs)):
            self.assertIn(value[16], "89ab", f"{name} 的 variant nibble 非法：{value[16]}")
        # 时间位必须彼此接近（同一次测试内），证明两条路径都嵌了**当前**时刻
        # 而不是各自的历史值。
        gap = abs(int(mine[:12], 16) - int(theirs[:12], 16))
        self.assertLess(gap, 60_000, "两条路径的时间位相差过大，至少有一条没在取当前时刻")


class Uuid7UniquenessTests(unittest.TestCase):
    """随机位要真的随机。"""

    def test_ids_are_unique(self):
        ids = [_uuid7_hex() for _ in range(5000)]
        self.assertEqual(len(set(ids)), len(ids), "UUIDv7 出现重复，随机位失效")

    def test_time_bits_are_non_decreasing(self):
        """时间位非递减 —— v7 自带时间序，这是它相对 v4 的核心价值。"""
        stamps = [int(x[:12], 16) for x in (_uuid7_hex() for _ in range(2000))]
        self.assertTrue(all(a <= b for a, b in zip(stamps, stamps[1:])),
                        "时间位出现倒退，v7 的单调性没了")


class Uuid7HeaderTests(unittest.TestCase):
    """接进请求头的那一处。"""

    def test_workbuddy_header_carries_a_v7(self):
        """WorkBuddy 的 X-Request-ID 必须是合规的 v7。"""
        # 走真方法：_build_headers_from 是 CredentialManager 的实例方法，
        # 借一个不经过 __init__ 的实例调它，避免为了造一个 header 去写凭据文件。
        built = converter.CredentialManager._build_headers_from(
            object(), {"accessToken": "t", "domain": "d"}, {"uid": "u", "enterpriseId": "e"})
        _assert_conforming_v7(self, built["X-Request-ID"], "请求头")

    def test_trae_backend_does_not_use_this_header(self):
        """Trae 不受影响：它的 header 自建（``_solo_headers``），不共用上面这条。

        用模块的 __file__ 定位源码而不是相对路径 —— 测试不一定在仓库根目录下跑。
        """
        import core.backends.trae as trae_mod
        from pathlib import Path as _P
        src = _P(trae_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_build_headers_from", src,
                         "Trae 不应引用 WorkBuddy 的 header 构造，否则改 UUIDv7 会波及它")


class BuiltinAvailabilityTests(unittest.TestCase):
    """版本策略：生产 3.14 用内置，低版本兜底。"""

    def test_falls_back_when_builtin_is_missing(self):
        """内置不可用时必须自动走兜底，而不是抛异常。"""
        original = converter._builtin_uuid7
        try:
            converter._builtin_uuid7 = None
            _assert_conforming_v7(self, _uuid7_hex(), "强制兜底")
        finally:
            converter._builtin_uuid7 = original

    def test_builtin_branch_is_used_verbatim_when_available(self):
        """3.14 上应**直接复用**标准库实现，不要自己再包一层。

        自己重写一遍的意义只在于兼容 < 3.14；到了有内置的版本，多一层封装就是
        纯负担 —— 而且会让「我们在跑哪条路径」变得不透明。这里用身份断言
        （``is``）而不是形状断言：形状对但换了实现，正是要防的事。
        """
        if not hasattr(uuid, "uuid7"):
            self.skipTest("当前解释器低于 3.14，生产镜像才是这条路径")
        self.assertIs(converter._builtin_uuid7, uuid.uuid7,
                      "内置分支应直接复用标准库实现")


if __name__ == "__main__":
    unittest.main()