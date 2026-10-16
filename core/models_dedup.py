"""``/v1/models`` 的 API 层去重：把「同一个模型的多后端版本」并成一条裸名。

对应 ``specs/model-routing-by-policy.md`` §6.2（去重规则）与
``specs/api-models-dedup-plan.md``（施工图，2026-10-16 用户拍板）。

## 为什么需要它

管理页早就把同名模型合并成一行（前端 `groupModelsByName` 读 `canonical`），
但 **API 侧没有**：dev 实测 ``/v1/models`` 返回 42 条，同一个 ``kimi-k3`` 出现
两次（腾讯 ``kimi-k3-1`` + 字节 ``trae/kimi-k3``）。客户端（Cherry Studio、
Open WebUI 等）因此把同一个模型列成两个条目，用户得自己猜选哪个 ——
正是「把本该由系统承担的复杂度推给用户」这个要消灭的问题。

去重后：同一规范名只出一条**裸名**，客户端传裸名由服务端按策略选后端（§5）。

## 不变量（改这个文件前先读）

1. **带前缀的 id 原样保留**（§6.2 原文）：它是高级用户显式指定后端的入口，
   也是排障时的确定性入口（§附 理由 1、2）。去重只**增加**裸名条目，
   从不删除带前缀条目。
2. **列出的裸名 id 必须可路由**（用户拍板 ②）：若某个组的自然裸名查不到候选，
   就**不返回**这条裸名条目（只留带前缀行）—— 列表里的 id 必须可信，
   不能让客户端拿到一个「列表里有、请求 400」的 id。
3. **本模块是纯函数**：不读 registry、不查库、不碰全局。调用方把已经算好的
   事实集传进来。这样规则可以穷举单测，也不会因为「某处忘了注入 provider」
   而在生产里悄悄退化（与 ``core/bare_routing.py`` 同一考虑）。

## 裸名条目用哪个 id（用户拍板 ①）

**裸名 = 规范名 `canonical`**，与「选哪个后端」无关。

这一点值得写清楚，因为直觉上会以为「默认后端优先」是在**挑名字**。实测
（`registry.canonical_name`）三个 id 的规范名是同一个：

    canonical_name('kimi-k3')      == 'kimi-k3'      # 字节裸名
    canonical_name('kimi-k3-1')    == 'kimi-k3'      # 腾讯裸名（上游 id 写偏了）
    canonical_name('trae/kimi-k3') == 'kimi-k3'      # 字节带前缀

所以「默认后端优先」**不是选名字的规则**，而是「**这条裸名条目该不该出现、
以及它该排在组内什么位置**」的规则 —— 见 ``dedup_for_api`` 的注释。
名字由 canonical 唯一决定，这正是 §4「canonical 是唯一判定处」的直接后果。

反例说明为什么不能用「字面剥前缀」当裸名：腾讯那条字面剥前缀得 ``kimi-k3-1``，
它就是**上游写偏的 id**，暴露给客户端等于把实现细节当对外契约。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApiModelRow:
    """`/v1/models` 去重的输入行 —— 调用方已算好的最小事实集。

    - ``id``：**对外 id**（带前缀，或在默认后端时不带）；
    - ``canonical``：规范名（``registry.canonical_name``，唯一判定处）；
    - ``backend``：归属后端名。
    - ``prefixed``：该 id 是否**带后端前缀**。

    **``prefixed`` 必须由调用方传，不能在这里推断。** 曾经我用
    ``id != canonical`` 当判据，结果把腾讯的 ``kimi-k3-1`` 当成了「显式 id」
    而原样保留 —— 但它**没有前缀**，只是上游把 id 命名成了别的样子
    （``canonical_for`` 声明 ``kimi-k3-1 → kimi-k3``）。按那个判据，
    客户端会看到 ``kimi-k3`` 与 ``kimi-k3-1`` 两条，去重等于没做。
    「带前缀」的唯一事实来源是**各后端声明的 `model_prefix`**，
    调用方拼 id 时就知道（``prefix + bare``），所以由调用方给出。
    """

    id: str
    canonical: str
    backend: str
    prefixed: bool = False

    @property
    def is_explicit(self) -> bool:
        """本行是否是「用户可用来点名后端的显式 id」= **带前缀**。

        只有带前缀的 id 才保留：它是 §附 理由 1/2 的排障入口与价格知情同意入口。
        不带前缀、只是与 canonical 写法不同的 id（如腾讯 ``kimi-k3-1``）
        **不保留** —— 它的对外形式就是规范名 ``kimi-k3``。
        """
        return self.prefixed


def dedup_for_api(rows, routable=None):
    """把多后端重复的模型行并成 API 输出用的 id 列表（§6.2）。

    参数：
    - ``rows``：``ApiModelRow`` 序列，**已按调用方期望的展示顺序排好**；
    - ``routable``：可选回调 ``bare_name -> bool``，判断该裸名**是否可路由**
      （用户拍板 ②）。不传则一律视为可路由（纯展示场景 / 单测）。

    返回：**id 列表**，保持 ``rows`` 的**首现顺序**。

    行为：
    1. 每个规范名**第一次出现**时，插入一条裸名条目（裸名 = ``canonical``）；
       ``routable`` 判定为假时**不插入**（不变量 2）；
    2. **带前缀**的 id 原样保留（不变量 1），位置不变；不带前缀但写法与
       规范名不同的 id（如腾讯 ``kimi-k3-1``）**不单独输出**——它的对外形式
       就是规范名；
    3. 同一规范名**只插一次**裸名条目。

    为什么裸名条目插在「组内首次出现处」：客户端看到的顺序与管理员在管理页
    看到的顺序（``snapshot()`` 的四层排序）保持一致 —— 同一个模型在两处的
    相对位置相同，排障时不会「管理页说它在第 3 行、API 说第 9 条」。
    由于 ``rows`` 已是策略顺序，组内第一行就是策略第一名，
    这与 §9.6 的 ``owned_by`` 口径（「传裸名实际会落到谁」）一致。
    """
    out: list[str] = []
    emitted: set[str] = set()

    for row in rows:
        bare = row.canonical
        # 组内首次遇到 → 决定要不要插裸名条目（用户拍板 ②：不可路由就不插）。
        if bare not in emitted:
            emitted.add(bare)
            if (routable is None or routable(bare)) and bare not in out:
                out.append(bare)
        # 显式 id 永远保留（不变量 1）。
        if row.is_explicit and row.id not in out:
            out.append(row.id)

    return out
