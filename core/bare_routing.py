"""裸名路由：把「无前缀的模型名」按策略解析到一个后端。

对应 ``specs/model-routing-by-policy.md`` §5（候选集 / 排序 / 降级），
施工图见 ``specs/bare-name-routing-plan.md``。

## 为什么单独一个文件

这段逻辑有**两个调用方**，而且它们必须给出一致的答案：

- ``admin/pool.py``：中间件在**账号选择之前**定后端（选号要按 backend 过滤）；
- ``core/converter.py``：``resolve_backend`` 要给出 ``(spec, upstream_model)``。

两处各写一遍必然漂移（今天已经有一处是「谁都没算、直接兜默认后端」）。
所以规则只写在这里，两个调用方都调它。

## 为什么是纯函数

``decide()`` 不读全局状态、不碰数据库：候选、顺序、可用性全部作为**参数**传进来。
这样它可以在没有 app、没有 DB 的情况下单测（§5 的规则很多，值得全覆盖），
也不会因为「某处忘了注入 provider」而在生产里悄悄退化。

真实取值由 ``admin/routing.py`` 之外的调用方组装（见 ``BackendOrderSpec``）。

## 大前提（§5.4）

**只有默认后端的部署必须完整可用**。候选集为空时不是「无解」，而是「只剩一个
候选」——因为总闸不变量保证启用后端恒 ≥ 1，且默认后端一定启用。所以这里
**不写**「如果只有 WorkBuddy 就……」这类特例；那正是 §5.4 第 3 条禁止的形态。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 「按标准计费」的模型（credits_micro 为 NULL）在价格比较里的取值。
#:
#: §9.3：「`credits_micro` 为 NULL → **视为最贵**（等于 +∞）：只有显式前缀才能
#: 选中。『按标准计费』的模型不该被自动选中——那正是『免费变收费的惊喜』本身」。
#:
#: 用一个比任何真实单价都大的整数而不是 ``float("inf")``：真实单价是整数 micro，
#: 混用 float 会让比较语义变得依赖类型；而这个常量足够大（1e15 micro = 10 亿倍率），
#: 且仍是 int，比较结果与 inf 等价。
UNPRICED = 10 ** 15


@dataclass(frozen=True)
class Candidate:
    """一个候选后端及其判定依据（都是「为什么它在候选里」的证据）。"""

    backend: str
    #: 该后端这个模型的精确单价（整数 micro）。None = 按标准计费。
    credits_micro: int | None
    #: 该后端这条模型行的对外 id（带前缀，如 ``trae/glm-5.3``）。
    #: 解析成功后要用它反查上游模型名。
    model_id: str

    @property
    def price(self) -> int:
        """价格比较用的值（None 按 §9.3 视为最贵）。"""
        return UNPRICED if self.credits_micro is None else self.credits_micro


@dataclass(frozen=True)
class Rejection:
    """一个后端**没进候选**的原因。失败时逐条回给调用方（§5.1 末段）。"""

    backend: str
    reason: str


@dataclass(frozen=True)
class Decision:
    """一次裸名路由的结果。"""

    backend: str
    #: 该后端这条模型行的对外 id（调用方据此拿上游模型名）。
    model_id: str
    #: 本次被判出局的后端及原因（含价格红线拦下的），供 400 报错/排障。
    rejected: tuple = ()
    #: 价格红线通过后的**存活候选**后端名，按策略排序（选中的是第一个）。
    #: 「贵的方向出局」体现为它不在这个元组里（§9.3）。
    candidates: tuple = ()

    @property
    def is_ambiguous(self) -> bool:
        """是否发生了「多候选择一」（单候选时策略退化，值得区分开）。"""
        return len(self.candidates) > 1


@dataclass
class BackendOrderSpec:
    """排序与价格豁免的**输入快照**（由调用方组装，本模块不读全局）。"""

    #: 生效的后端顺序（不含名字未知的项）。
    order: tuple = ()
    #: 用户是否**显式配置过**顺序（§9.3 豁免二）：全局或单 key 非默认。
    #: 真 = 跳过价格红线（配 key 的是管理员，看得见两边倍率 → 知情选择）。
    explicit_order: bool = False
    #: 请求是否带了显式前缀（§9.3 豁免一）。带前缀的请求根本不该走到这里，
    #: 但保留这个开关让「前缀请求也调 decide 做记录」的场景不误判。
    explicit_prefix: bool = False

    def rank(self, backend: str) -> int:
        """该后端在生效顺序里的位次；不在列表里的排在最后（保持稳定）。"""
        try:
            return self.order.index(backend)
        except ValueError:
            return len(self.order)


def _default_order(names) -> tuple:
    """默认顺序 = 注册表顺序（§9.2：注册表顺序本身就是后端对「我排第几」的声明）。"""
    return tuple(names or ())


def build_order(known_names, configured=None, key_configured=None):
    """组装生效顺序与「是否显式配置」（§9.4 / §9.3 豁免二）。

    - ``known_names``：注册表顺序（默认顺序的来源）；
    - ``configured``：``settings.model_backend_order``（全局，None/空 = 未配置）；
    - ``key_configured``：``keys.model_backend_order``（单 key，None/空 = 跟随全局）。

    **未知后端名要忽略**：用户配了 ``["trae", "已删除的后端"]`` 时，后者不该
    出现在顺序里（否则 `rank` 会把它算进去、把顺序搅乱）。同时也过滤掉重复项。

    返回 ``(order, explicit)``。``explicit`` 的口径（§9.3 豁免二）：**用户提供过
    非空配置**，无论是全局还是单 key。默认顺序不算显式。
    """
    explicit = bool(key_configured) or bool(configured)
    base = list(key_configured or configured or _default_order(known_names))
    known = set(known_names or ())
    seen, order = set(), []
    for name in base:
        # 未知后端名忽略：用户配了已删除的后端时，它不该占位次
        # （否则 `rank` 把它算进去、把真实后端的相对顺序搅乱）。
        if name in seen or (known and name not in known):
            continue
        seen.add(name)
        order.append(name)
    # 配置里没提到的后端**追加到末尾**（保持注册表顺序）：
    # 用户只调了前两个，第三个不该因此消失——那会让「不配 = 全都不管」变成
    # 「配了 = 只认配过的」，与 `_routing_for` 的「留空即跟随」口径不一致。
    for name in _default_order(known_names):
        if name not in seen:
            seen.add(name)
            order.append(name)
    return tuple(order), explicit


def decide(model, candidates, order_spec, rejections=(), known_names=()):
    """核心决策：给定候选与顺序，选出唯一后端（§5.1 + §5.2 + §9.3）。

    ``candidates`` 是 :class:`Candidate` 的可迭代对象（顺序无所谓，本函数排序）。
    ``rejections`` 是已判出局的后端（调用方先做「总闸/账号/模型启用」三道过滤，
    把原因带进来）。返回值 :class:`Decision`。

    异常：**候选集为空**时抛 :class:`NoCandidate`，消息里列出每个后端的出局原因
    （§5.1 末段：这条信息在排障时最有用）。
    """
    ranked = sorted(candidates, key=lambda c: (order_spec.rank(c.backend), c.backend))
    if not ranked:
        raise NoCandidate(model, rejections)

    # §9.3：价格红线。基准 = **排序后第一个**候选（不是全部候选的最低价）。
    # 豁免一（带前缀）/ 豁免二（用户显式配过顺序）时整条规则跳过。
    skipped_price = order_spec.explicit_order or order_spec.explicit_prefix
    winner = ranked[0]
    survivors = [winner]
    dropped = list(rejections)
    if skipped_price:
        # 豁免一/二：整条红线跳过，其余候选全部存活（用户自己的选择）。
        survivors.extend(ranked[1:])
    else:
        # 基准是 winner 的价（它已是候选集里排序最靠前的 —— §9.3「边界情况」
        # 说的「首选不可用则基准下移」在这个函数里体现为「首选本就不在 ranked 里」，
        # 那种情况下基准自然就是新的 ranked[0]）。
        base_price = winner.price
        for cand in ranked[1:]:
            if cand.price > base_price:
                # 贵的方向出局：只有显式前缀/显式配序能选中它（§9.3）。
                dropped.append(Rejection(
                    cand.backend,
                    f"单价高于首选（{cand.price} > {base_price} micro）——"
                    "自动选择只往便宜/同价方向切，贵的请用显式前缀"))
            else:
                survivors.append(cand)

    return Decision(backend=winner.backend, model_id=winner.model_id,
                    rejected=tuple(dropped),
                    candidates=tuple(c.backend for c in survivors))


class NoCandidate(Exception):
    """裸名请求没有任何可用候选后端（§5.1：返回 400，逐条说明出局原因）。"""

    def __init__(self, model, rejections):
        self.model = model
        self.rejections = tuple(rejections)
        lines = [f"模型 {model} 没有可用后端"]
        for r in self.rejections:
            lines.append(f"  · {r.backend}：{r.reason}")
        super().__init__("\n".join(lines))
