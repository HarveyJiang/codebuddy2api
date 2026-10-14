# 自定义模型（手动添加上游没返回的模型）

上游 `/v3/config` 返回的模型目录**不全**：有些 id 上游没在 cli agent 菜单里列出来，
但实际仍可调用（灰度、区域差异、上游菜单滞后）。现在这些模型在网关里等于不存在，
`/v1/models` 列不出、管理页看不到、也没法启用/禁用。

本次加两件事：

1. **手动添加**模型，保存到本地目录，且**不被每 6 小时的自动拉取冲掉**。
2. 让它们**可调用**：上游没返回 ≠ 不能用。

- 写于 `2026-10-15`，基线 commit `58bdd29`。
- 相关代码：`admin/models.py`、`admin/db.py`、`admin/server.py`、`admin/static/app.js`。

---

## 1. 语义：三个状态轴互不耦合

`models` 表已有 `source` / `available` / `enabled` 三列，**本次不加列**，只改语义。

| 字段 | 谁写 | 含义 |
| --- | --- | --- |
| `source` | 自动 | `upstream`（上游返回）/ `builtin`（上游没返回，用内置官方元数据兜底）/ `custom`（**用户手动添加**）。三者都在管理页显示为「来源」。 |
| `available` | 自动 | 上游**本次**是否返回。`0` = 上游没给。**不再影响可调用性**。 |
| `enabled` | **仅用户** | 是否放行。`0` 时 `PoolMiddleware` 直接 400。 |

`builtin` 与 `upstream` 是**不同的来源**，不要合并：`upstream` 表示上游本次真的
返回了它（元数据以上游为准），`builtin` 表示上游没返回、用的是内置官方元数据
兜底（如三档智能路由 `fast-model` 等）。两者都属于「上游侧」的模型，只有
`custom` 是用户加进来的。

**可调用性只由 `enabled` 决定。** 这是本次的核心：`source=custom` 且
`enabled=1` 的模型，哪怕 `available=0`（上游根本没返回它），也走正常请求。

为什么必须解耦：上游没返回有两种情况——**真的下线了**和**只是没列出来**。网关
分不清，只有用户知道。绑死 `available` 的话，用户手动加回来的模型下一次拉取就
失效了，功能等于没有。

---

## 2. `fetch()` 不能冲掉自定义模型（最容易写错的地方）

现在 `fetch()` 结尾：

```python
self._disabled &= set(available)
self._catalog, self._available = catalog, available   # 整体替换
```

整体替换 ⇒ 手动添加的模型在**启动时和每 6 小时**的自动拉取后全部消失，而用户
的预期是「保存住」。

改法：把「上游目录」与「自定义集合」**合并**。

- 新增 `self._custom: set[str]`，由 `load()` 从 `source == "custom"` 的行恢复，
  `persist()` 写回。
- `fetch()` 成功后：

```python
merged_catalog, merged_available = dict(catalog), list(available)
for mid in self._custom:
    if mid in merged_catalog:
        # 上游这次也返回了：以上游元数据为准，但保留 custom 标记，
        # 否则下一次拉取时 _custom 就断了，用户保存的项会「变成上游的」。
        merged_catalog[mid]["source"] = "custom"
        continue
    entry = self._catalog.get(mid) or {}
    merged_catalog[mid] = dict(entry, source="custom")
    merged_available.append(mid)   # 追加在末尾，不抢上游的排序位
with self._lock:
    self._disabled &= set(merged_available)
    self._catalog, self._available = merged_catalog, merged_available
```

要点：

- 自定义项元数据（name / description / credits / max_input_tokens）**来自本地**，
  上游没有它。
- 自定义项**追加在 `_available` 末尾**，不打乱上游的单价排序。
- 上游后来也有了同一 id 时用上游元数据，但 `source` 保持 `custom`。

---

## 3. `load()` / `persist()` 的改动

### `load(db)`

现在是 `load_models(only_available=True)`，**只加载 `available=1`** —— 自定义模型
若被标了 `available=0` 就加载不进来。改为：**同时加载 `available=1` 与
`source='custom'` 的行**。

`load_models` 加一个参数表达这个条件，例如：

```python
def load_models(self, only_available=False, include_custom=False):
    where = []
    if only_available:
        where.append("available=1")
    if include_custom:
        where.append("source='custom'")
    sql = "SELECT * FROM models"
    if where:
        sql += " WHERE " + " OR ".join(where)   # 注意是 OR：两种行都要
```

`load()` 里对 `source == "custom"` 的行执行 `self._custom.add(mid)`。
`_available` 的顺序按 `sort_order`：自定义项 `sort_order` 存得比上游大，
自然排在后面（写入时保证）。

### `persist(db)`

现在是遍历 `self._catalog` 写 `rows`，`available` 取 `1 if mid in self._available else 0`。
改成：

- `source` 取 `info.get("source")`，自定义项必须写回 `custom`（否则重启后
  `_custom` 集合丢了，下一次 `fetch()` 就会把它冲掉）。
- `sort_order`：`mid in self._available` 时用索引，否则用
  `len(self._available) + 自定义序号`，保证自定义项排在最后且顺序稳定。

---

## 4. 接口

### 4.1 新增：`POST /admin/api/models`

添加或更新一个模型。管理员鉴权 + CSRF（非 GET 已自动校验）。

```json
{
  "id": "some-hidden-model",
  "name": "某模型",
  "description": "上游菜单没列出，但可直接调用",
  "credits": 0.5,
  "max_input_tokens": 128000,
  "enabled": true
}
```

| 字段 | 必填 | 校验 |
| --- | --- | --- |
| `id` | 是 | 1–128 字符，`^[A-Za-z0-9._:/-]+$`（要进 URL 路径，拒空格与换行） |
| `name` | 否 | ≤ 200 字符，缺省用 `id` |
| `description` | 否 | ≤ 1000 字符 |
| `credits` | 否 | `null` 或 `≥ 0` 的数；`null` = 按标准计费 |
| `max_input_tokens` | 否 | `null` 或 1–10000000 的整数 |
| `enabled` | 否 | 布尔，缺省 `true` |

行为：

- **已有的上游模型**：只更新用户可改的字段（`name` / `description` / `credits` /
  `max_input_tokens` / `enabled`），`source` **保持 `upstream`**，不因「保存过」
  就变成自定义。这样它继续跟着上游刷新。
- **不存在的 id**：新建，`source = "custom"`，`available = 1`
  （=「我认为它能用」），`_custom` 加入该 id。
- 幂等：重复提交同一 id 不报 409，视为更新。

响应 `{"ok": true, "id": "...", "source": "custom"}`。

### 4.2 新增：`DELETE /admin/api/models/{mid}`

删除。**仅允许删 `source == "custom"`**：

- 上游模型删了没用——下一次 `fetch()` 原地复活；而且会让
  `request_logs.model_id` 的 join 断掉。上游模型只能禁用。
- 自定义模型删除时从 `_custom` 移除，否则下次 `fetch()` 会把它合并回来。
- 上游模型 → 400「上游模型只能禁用，不能删除」；不存在 → 404。

### 4.3 扩展：`PATCH /admin/api/models/{mid}`

现在只收 `enabled`，行为不变。

### 4.4 `GET /admin/api/overview` 的 `models` 段

每个模型条目增加 `custom: bool`（= `source == "custom"`），`source` 取值
`upstream` / `builtin` / `custom`，前端据此显示来源徽标并决定是否放出删除按钮。

### 4.5 `enabled_models()` / `snapshot_ids()` 的语义

```python
def enabled_models(self):
    """可调用列表：只看 enabled。available 与 source 都不参与。"""
    return [mid for mid in self._available if mid not in self._disabled]

def snapshot_ids(self):
    """全部已知 id：用于区分「已禁用」与「未知」。"""
    return set(self._available)
```

`_available` 现在含自定义项（见第 2 节合并逻辑），所以两者自动覆盖到。

**必须确认**：`PoolMiddleware` 的判断

```python
if model not in registry.enabled_models() and model in registry.snapshot_ids():
    raise HTTPException(400, f"模型 {model} 已在管理页禁用")
```

- 自定义模型必须在 `snapshot_ids()` 里，否则它会走「未知模型 → 放行」，
  禁用标记失效。
- `available` 不参与：上游没返回的自定义模型照样能调。

`db.enabled_models()`（`available=1 AND enabled=1`）与 `db.known_model_ids()`
（`available=1`）如果还有调用点，需同步放开；否则新语义在 DB 层不成立。

---

## 5. 前端

### 5.1 列表

「模型管理」表加「来源」标记与行内操作：

| 模型 | 官方描述 | 倍率 | 上下文 | 状态 | 操作 |
| --- | --- | --- | --- | --- | --- |

- `source == "custom"` 的行加「自定义」徽标，说明它由用户手动添加。
- 操作列：`启用/禁用` 照旧；`custom` 行额外给「删除」。
- 工具栏加「添加模型」按钮。

### 5.2 添加弹窗

复用现有 dialog 样式（参考 `confirm-action`）：

- ID（必填）、显示名、描述、倍率（留空 = `null`）、上下文（留空 = `null`）、
  启用（勾选，默认勾）。
- 前端做一份 ID 字符集校验，**后端才是权威**。
- 提交后 `refresh()` + toast「已保存」。

### 5.3 文案

`models-note` 补一句：

> 「上游菜单不一定列全；若某模型实际可用但列表里没有，可手动添加。」

---

## 6. 测试

补到 `tests/test_db.py` 与 `tests/test_admin_server.py`：

1. **自定义模型不被 `fetch()` 冲掉** —— `ModelRegistry` 上先 `add`，再走一次
   合并逻辑（可直接调用 `fetch` 的合并分支或注入假目录），断言 id 仍在
   `_available` 且 `source == "custom"`。
2. **重启后仍在** —— `persist` → 新 `Database` → `load`，断言自定义项还在
   （`available=0` 也要能加载进来）。
3. **上游没返回的模型可调用** —— `source=custom, available=0, enabled=1` 时
   `enabled_models()` **含**它，且 `PoolMiddleware` 不拒绝。
4. **它被禁用时要拒绝** —— `enabled=0` 时应 400「已在管理页禁用」，
   而不是当未知模型放行。
5. **上游模型不可删** —— `DELETE` 一个 `source != custom` 的 id → 400。
6. **非法 id 被拒** —— 空、含空格、超 128 字符 → 400。
7. **`DELETE` 自定义项后 `fetch()` 不复活它**（`_custom` 要同步移除）。

---

## 7. 验收

- `pytest tests/ -q` 全绿（当前 187）。
- `node --check admin/static/app.js` 通过。
- 手动验证：添加一个上游列表里没有的 id → 它出现在管理页且标「自定义」→
  **等一次自动拉取**（或点「立即拉取」）后它**仍在** → `/v1/models` 里有它
  → 用它发一次请求，**不应**得到「模型不存在」。把它禁用后发请求，应 400。
