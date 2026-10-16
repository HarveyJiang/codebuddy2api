"""裸名策略的**只读**入口，供展示层（`/v1/models`）查询「这条模型当前会走谁」。

对应 ``specs/model-routing-by-policy.md`` §9.6：`/v1/models` 里裸名条目的
``owned_by`` 要写**当前策略第一名**，而不是「无前缀 ⇒ 默认后端」的静态答案。

## 为什么单开一个模块、而不是在 converter 里直接算

真正的策略判定需要三样东西，它们在 admin 层：

- 内存模型目录（`ModelRegistry`）——「哪些后端有这个模型」；
- 账号可用性（`Store.has_enabled_account`）——候选集的第二道过滤；
- 设置（`model_backend_order` / `bare_name_routing`）——顺序与开关。

`core/` 不能反向 import `admin/`（依赖方向是 admin → core）。所以由 admin 在
启动时把「查询函数」注入这里，converter 通过 :func:`strategy_winner_for` 使用。
未注入时返回 ``None`` —— 调用方回落到静态归属，展示路径不会因为「admin 没加载」
（比如独立跑的 converter、单测）而报错。

**只读**：它不参与真实请求的路由决策（那是 `admin/pool.py` 的
`_resolve_backend_for`）。这里存在的唯一目的是让展示层说出与请求相同的话，
所以它**复用**同一个 `decide()`，不自己写一套排序。
"""
from __future__ import annotations

#: admin 启动时注入：`(model: str) -> str | None`，返回策略选中的后端名。
#: None 表示「算不出来」（无候选、没注入）——调用方回落静态归属。
_strategy_winner = None


def set_strategy_winner(provider) -> None:
    """注入策略查询函数（`admin/server.py` 启动时调用）。

    传 None 退回「没有策略」——测试与独立运行的 converter 的默认状态。
    """
    global _strategy_winner
    _strategy_winner = provider


def strategy_winner_for(model):
    """该模型的策略第一名后端名；算不出来（或未注入）时返回 None。

    异常一律吞掉并返回 None：这是**展示**路径，`/v1/models` 不该因为
    「此刻没有可用候选」而 500 —— 列出的模型可能确实全部账号都在冷却。
    """
    if _strategy_winner is None:
        return None
    try:
        return _strategy_winner(model)
    except Exception:
        return None


#: admin 启动时注入：`() -> list[(model_id, backend)] | None`，返回 `/v1/models`
#: 要列的**每一行及其归属后端**。None 表示「查不到」（未注入/未加载）——调用方
#: 回落到扁平名字列表（`converter.get_available_models`），展示路径不会因此报错。
_listed_rows = None


def set_listed_rows_provider(provider) -> None:
    """注入 `/v1/models` 的行来源（`admin/server.py` 启动时调用）。"""
    global _listed_rows
    _listed_rows = provider


def listed_rows_for():
    """`/v1/models` 要列出的 (model_id, backend) 行；不可用时返回 None。

    **为什么要单开一个入口**：去前缀后同名模型在两个后端各有一行（不变量 1），
    `/v1/models` 的 `owned_by` 要按**每行自己的后端**报（用户 2026-10-16 拍板）。
    扁平的名字列表（`get_available_models()`）丢掉了这一信息，只能给出「这个名字
    的归属」；而 `/admin/api/test` 的模型门要的正是那个扁平集合。两个用途不同，
    不能合并成一个返回值。

    异常吞掉返回 None（与 :func:`strategy_winner_for` 同理）：展示路径不该因为
    registry 出问题而 500。
    """
    if _listed_rows is None:
        return None
    try:
        return _listed_rows()
    except Exception:
        return None
