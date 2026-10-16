"""``core/models_dedup.dedup_for_api`` 的单测（§6.2，施工图 §五 提交 1）。

纯函数，所以这里不启 app、不查库：输入输出都是数据。规则分支多（不变量的
三条 + 用户拍板的两个决策），值得穷举。
"""
from __future__ import annotations

import pytest

from core.models_dedup import ApiModelRow, dedup_for_api


def row(id, canonical=None, backend="workbuddy"):
    """构造输入行；``canonical`` 省略时按「id 去掉 trae/ 前缀」当默认。

    测试里显式写 canonical 更清楚，这个默认只是省字。
    ``prefixed`` 也按 id 是否以 ``trae/`` 开头推出 —— 与真实调用方的
    ``prefix + bare`` 拼接口径一致（前缀来自后端声明）。
    """
    prefixed = id.startswith("trae/") or id.startswith("third/")
    return ApiModelRow(id=id, canonical=canonical or id.removeprefix("trae/"),
                       backend=backend, prefixed=prefixed)


# ---------------------------------------------------------------------------
# 不变量 1：带前缀的显式 id 原样保留
# ---------------------------------------------------------------------------

def test_explicit_id_is_preserved():
    """带前缀行必须原样留在输出里（§6.2 原文，高级用户显式指定后端的入口）。"""
    rows = [row("kimi-k3", backend="workbuddy"), row("trae/kimi-k3", backend="trae")]
    assert dedup_for_api(rows) == ["kimi-k3", "trae/kimi-k3"]


def test_explicit_id_survives_when_bare_is_unroutable():
    """裸名不可路由时，带前缀行仍要保留 —— 否则客户端一个都拿不到。"""
    rows = [row("kimi-k3"), row("trae/kimi-k3", backend="trae")]
    assert dedup_for_api(rows, routable=lambda _: False) == ["trae/kimi-k3"]


def test_single_backend_with_prefix_only():
    """某模型只在带前缀的后端有 → 输出裸名 + 带前缀两条。"""
    rows = [row("trae/openrouter-free", backend="trae")]
    assert dedup_for_api(rows) == ["openrouter-free", "trae/openrouter-free"]


# ---------------------------------------------------------------------------
# 去重核心：同一 canonical 只出一条裸名
# ---------------------------------------------------------------------------

def test_same_canonical_collapses_to_one_bare():
    """腾讯 kimi-k3-1 与字节 trae/kimi-k3 同组 → 裸名只出一条。"""
    rows = [
        row("kimi-k3-1", canonical="kimi-k3", backend="workbuddy"),
        row("trae/kimi-k3", canonical="kimi-k3", backend="trae"),
    ]
    out = dedup_for_api(rows)
    # 裸名 kimi-k3 一次；kimi-k3-1 **不作为独立条目出现**（它的对外形式就是 kimi-k3）
    assert out.count("kimi-k3") == 1
    assert "kimi-k3-1" not in out
    assert out == ["kimi-k3", "trae/kimi-k3"]


def test_bare_name_is_emitted_once_across_many_members():
    """三个后端成员也只出一条裸名。"""
    rows = [
        row("glm-5.3", backend="workbuddy"),
        row("trae/glm-5.3", backend="trae"),
        row("third/glm-5.3", canonical="glm-5.3", backend="third"),
    ]
    assert dedup_for_api(rows) == ["glm-5.3", "trae/glm-5.3", "third/glm-5.3"]


def test_distinct_canonicals_are_not_merged():
    """hy3 与 hy3-x 是两个真模型（同一后端内），不能并成一个。"""
    rows = [row("hy3"), row("hy3-x")]
    assert dedup_for_api(rows) == ["hy3", "hy3-x"]


def test_single_backend_bare_only():
    """只有一个后端、没有前缀的裸名 → 原样一条，不重复。"""
    rows = [row("hy4-preview")]
    assert dedup_for_api(rows) == ["hy4-preview"]


# ---------------------------------------------------------------------------
# 不变量 2：裸名必须可路由（用户拍板 ②）
# ---------------------------------------------------------------------------

def test_unroutable_bare_is_not_emitted():
    """routable 为假 → 不插裸名条目（列表里的 id 必须可信）。"""
    rows = [row("kimi-k3"), row("trae/kimi-k3", backend="trae")]
    out = dedup_for_api(rows, routable=lambda bare: bare != "kimi-k3")
    assert "kimi-k3" not in out
    assert out == ["trae/kimi-k3"]


def test_routable_receives_each_bare_once():
    """routable 对每个规范名只问一次（不是每行问一次）。"""
    seen = []

    def routable(bare):
        seen.append(bare)
        return True

    rows = [
        row("kimi-k3-1", canonical="kimi-k3"),
        row("trae/kimi-k3", canonical="kimi-k3", backend="trae"),
        row("glm-5.3"),
    ]
    dedup_for_api(rows, routable=routable)
    assert seen == ["kimi-k3", "glm-5.3"]


def test_routable_none_means_all_routable():
    """不传 routable = 纯展示场景，一律视为可路由。"""
    rows = [row("kimi-k3"), row("trae/kimi-k3", backend="trae")]
    assert dedup_for_api(rows) == ["kimi-k3", "trae/kimi-k3"]


# ---------------------------------------------------------------------------
# 顺序：裸名插在「组内首次出现处」，保持首现序
# ---------------------------------------------------------------------------

def test_bare_is_inserted_at_first_occurrence():
    """裸名插在组内第一行处 —— 与管理页 snapshot() 的顺序一致。"""
    rows = [
        row("hy3"),
        row("trae/glm-5.3", canonical="glm-5.3", backend="trae"),  # 组里先出现 trae
        row("glm-5.3", backend="workbuddy"),
    ]
    assert dedup_for_api(rows) == ["hy3", "glm-5.3", "trae/glm-5.3"]


def test_output_preserves_input_order_for_singletons():
    """无重名的行保持输入顺序（客户端看到的顺序 = 策略顺序）。"""
    rows = [row("a"), row("b"), row("c")]
    assert dedup_for_api(rows) == ["a", "b", "c"]


def test_empty_input():
    assert dedup_for_api([]) == []


# ---------------------------------------------------------------------------
# 防错：分错组要炸，不能静默少一个模型
# ---------------------------------------------------------------------------

def test_bare_looking_alias_id_is_not_emitted():
    """腾讯 ``kimi-k3-1`` 没有前缀 → **不单独输出**，对外形式就是规范名。

    这条锁住一个真实踩过的坑：判据若是「id != canonical」，``kimi-k3-1``
    会被当成显式 id 保留，客户端就看到两条（``kimi-k3`` + ``kimi-k3-1``），
    去重等于没做。
    """
    rows = [
        row("kimi-k3-1", canonical="kimi-k3", backend="workbuddy"),  # 无前缀
        row("trae/kimi-k3", canonical="kimi-k3", backend="trae"),   # 有前缀
    ]
    out = dedup_for_api(rows)
    assert out == ["kimi-k3", "trae/kimi-k3"]
    assert "kimi-k3-1" not in out
