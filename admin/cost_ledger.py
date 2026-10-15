"""实测成本账本：按 (账号, 模型) 记录**观测到的**单价。

## 为什么需要「实测」

上游对不同账号在同一模型上的计费并不一致，而**是否收费无法从配置推断**：
限免额度、夜间免费、试用期，这些优惠只体现在每次响应的 ``usage`` 里，
模型注册表里的静态倍率（``x0.79``）只是标价，不是这个账号实际被扣的钱。

结果是同一个请求走不同的号，成本可能差几倍。选号时优先挑**实测免费**的账号，
比按静态倍率排序更贴近现实。

## 关键纪律：观测不到就什么都不做

本项目的真实约束是——**我们尚未确认上游到底发不发 credit 字段**。
所以这个账本被刻意设计成「不观测就不生效」：

- 只有当响应里真的带回了可解析的 credit 数值，才写入账本；
- 账本为空时，``prefer_free`` 阶段**原样返回候选**，选号行为与加这个功能之前
  逐个字节一致。

这样即使上游从不发 credit，这个模块也是零行为变化的安全赘物，而不是
一个悄悄改变选号逻辑的黑盒。

## TTL 为什么必须存在

优惠是时段性的：夜间免费的账号到了白天照常收费。拿夜间的观测去指导白天的
选号会把成本算反，所以观测带 6 小时有效期，过期即视为「未知」。
"""

from __future__ import annotations

import threading
import time

# 观测有效期。见模块开头：优惠是时段性的，陈旧价格不能拿来选号。
OBSERVATION_TTL = 6 * 3600

# 单价的「免费」判定阈值。credit 是浮点数，理论上应恰为 0；留一点余量
# 是为了容忍上游的舍入误差，而不是给「便宜」开后门。
FREE_EPSILON = 1e-9


class CostLedger:
    """``(账号, 模型) -> 实测单价`` 的内存账本。

    线程安全：选号在请求热路径上，刷新/签到在后台线程，两者都会读写。
    """

    def __init__(self, *, ttl: float = OBSERVATION_TTL, clock=time.time):
        self._rows: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._clock = clock
        # 观测计数：用来判断「账本是不是在用」，也方便排障时确认上游到底发不发。
        self.observed = 0
        self.skipped = 0

    # -- 写入 ---------------------------------------------------------------

    def observe(self, account_id, model, credits, tokens) -> bool:
        """记录一次成功请求的实测单价。返回是否真的写入了。

        任何一项缺失或不可解析都直接放弃（返回 False）——**绝不猜测**：
        把「不知道」当成 0 会让我们误以为某个号免费，从而把流量全压上去。
        """
        if not account_id or not model or tokens is None:
            self.skipped += 1
            return False
        try:
            credits_value = float(credits)
            token_count = int(tokens)
        except (TypeError, ValueError):
            self.skipped += 1
            return False
        if token_count <= 0 or credits_value < 0:
            self.skipped += 1
            return False

        # 归一成「每千 token 的 credit」，便于不同长度的请求互相比较。
        per_1k = credits_value / token_count * 1000.0
        key = (str(account_id), str(model))
        now = self._clock()
        with self._lock:
            row = self._rows.get(key)
            if row is None or (now - row["at"]) > self._ttl:
                row = {"value": per_1k, "at": now, "samples": 1}
            else:
                # 指数滑动平均：单次请求的抖动不该立刻改变对账号的判断。
                alpha = 0.3
                row["value"] = row["value"] * (1 - alpha) + per_1k * alpha
                row["at"] = now
                row["samples"] += 1
            self._rows[key] = row
        self.observed += 1
        return True

    # -- 读取 ---------------------------------------------------------------

    def cost_of(self, account_id, model):
        """实测单价；无记录或已过期返回 None。"""
        key = (str(account_id), str(model))
        with self._lock:
            row = self._rows.get(key)
            if row is None:
                return None
            if (self._clock() - row["at"]) > self._ttl:
                return None
            return float(row["value"])

    def is_free(self, account_id, model):
        """该账号跑该模型是否**实测**免费。

        未观测 → 返回 None（未知），而不是 False。调用方必须区别对待：
        「未知」不该被当成「收费」，否则新加的号永远轮不上。
        """
        cost = self.cost_of(account_id, model)
        if cost is None:
            return None
        return cost <= FREE_EPSILON

    def known_free_ids(self, model, candidate_ids) -> set[str]:
        """候选里**实测确认免费**的账号 id 集合。"""
        return {str(aid) for aid in candidate_ids if self.is_free(aid, model) is True}

    # -- 决策 ---------------------------------------------------------------

    def prefer_free(self, candidates, model, id_of):
        """在候选里优先挑实测免费的；一个都没有就原样返回。

        这是整个模块的**唯一**决策点，语义刻意保守：

        - 没有任何「实测免费」的账号（含账本为空、模型未知）→ **原样返回**，
          选号行为与加这个功能之前完全一致。
        - 有实测免费的 → 只保留这些，把流量集中过去，把其它账号的额度省下来。

        注意「未知」不等于「收费」：未知的号在有免费号时会被让开，但在没有
        免费号时仍然正常参与轮转——否则新加的账号永远等不到第一次观测。
        """
        if not model or not candidates:
            return candidates
        free = self.known_free_ids(model, [id_of(c) for c in candidates])
        if not free:
            return candidates
        kept = [c for c in candidates if str(id_of(c)) in free]
        return kept or candidates

    # -- 展示 ---------------------------------------------------------------

    def snapshot(self, limit: int = 200) -> list[dict]:
        """导出账本内容，供后台透出与排障。"""
        now = self._clock()
        with self._lock:
            rows = [
                {"account_id": aid, "model": model, "cost_per_1k": round(row["value"], 6),
                 "samples": row["samples"], "age_seconds": int(now - row["at"]),
                 "stale": (now - row["at"]) > self._ttl}
                for (aid, model), row in self._rows.items()
            ]
        rows.sort(key=lambda r: (r["stale"], r["cost_per_1k"], r["account_id"]))
        return rows[:limit]

    def stats(self) -> dict:
        """账本规模与健康度：``observed`` 长期为 0 就说明上游不发 credit。"""
        with self._lock:
            active = sum(1 for row in self._rows.values()
                         if (self._clock() - row["at"]) <= self._ttl)
        return {"entries": len(self._rows), "active": active,
                "observed": self.observed, "skipped": self.skipped,
                "ttl_seconds": int(self._ttl)}
