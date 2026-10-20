"""WorkBuddyBackend — 腾讯 CodeBuddy/WorkBuddy 后端（原有生产路径的封装）。

行为保持约束（字节级）：
- 200 → `aiter_bytes()` 原样直通（含工具调用 SSE，一个字节都不改）；
- 非 2xx → raise BackendHTTPError(status, body)（消费方补 `_err_event`，
  与旧 `_stream_upstream` 的非 200 分支输出完全一致）；
- httpx.HTTPError → raise BackendHTTPError(502, transport=True)；
- timeout 语义逐调用点透传（chat 流式 None / responses 流式 300 /
  非流式 120 / count_tokens 30），与旧代码各 call-site 一致。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from .base import BackendHTTPError, BackendSpec, BackendUsageError

#: 北京时间（上游 requestTime 是北京时间且无时区标记）。
CN = timezone(timedelta(hours=8))

#: 上游用量明细接口在另一个域、且没有 /v2 前缀，不能复用积分/签到的那个域。
#: 注意参数名是驼峰 pageNum/pageSize（与同域 get-user-resource 的 PageNumber/PageSize 不同）。
USAGE_BILLING = "https://www.workbuddy.cn/billing/meter/"
USAGE_PAGE_SIZE = 100
USAGE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 积分 / 签到接口（billing 直连）。与 USAGE_BILLING 不同域、不同鉴权面，
#: 所以各有一套 URL，互不复用。
BILLING = "https://www.codebuddy.cn/v2/billing/meter/"
BILLING_PAGE_SIZE = 100
#: 积分包分页上限（防御性：上游 TotalCount 异常时不无限翻页）。
BILLING_MAX_PAGES = 100

#: 「7 天内过期」窗口（天），与 trae.EXPIRY_SOON_DAYS 同口径。core 不 import admin，
#: 两边各自持一份相同数值 —— 改任一处都要同步改另一处（trae.py:93 有互指注释）。
EXPIRY_SOON_DAYS = 7


def _site_headers(headers: dict) -> dict:
    """给 chat 请求补上「像官方客户端」的站点头。

    官方桌面端除了 X-CodeBuddy-Request，还会带 Origin / Referer /
    Accept-Language —— 上游据此判断「这是客户端直连，不是裸脚本」。
    这三个头与请求目标 host 无关，但与**账号所属站点**有关，所以按
    X-Domain 派生（海外站 .ai 用 en-US，国内站用 zh-CN）。

    只在 chat 路径加：账单与运维接口打的是 www.workbuddy.cn，
    让它们带上 codebuddy.cn 的 Origin 会被 WAF 当成跨站请求拦掉。
    """
    out = dict(headers)
    domain = str(out.get("X-Domain") or "").strip()
    # X-Domain 正常是裸域名（"www.codebuddy.cn"），但防御性剥掉 scheme
    # 与尾部斜杠，免得多拼出 "https://https://…" 这种一眼假的来源。
    domain = domain.split("://", 1)[-1].strip("/")
    if not domain:
        return out
    origin = f"https://{domain}"
    out["Origin"] = origin
    out["Referer"] = f"{origin}/"
    out["Accept-Language"] = "en-US" if domain.endswith(".ai") else "zh-CN"
    return out


def _usage_time(value):
    """上游 requestTime 是北京时间且无时区标记，必须按 CN 解析，否则跨天统计差 8 小时。"""
    try:
        return int(datetime.strptime(value, USAGE_TIME_FORMAT).replace(tzinfo=CN).timestamp())
    except (TypeError, ValueError):
        return None


def _billing(manager, client, path, body=None):
    """打一次 billing 直连接口。非 200 / 非 JSON / code≠0 → BackendUsageError。

    ``code != 0`` 的 message 不透出：上游把内部细节（如「签名错误」）放在
    message 里，透给前端等于把上游内部信息暴露给调用方。
    """
    headers = manager.get_headers()
    response = client.post(BILLING + path, headers=headers, json=body or {})
    if response.status_code != 200:
        raise BackendUsageError("积分服务请求失败", response.status_code)
    try:
        d = response.json()
    except ValueError:
        raise BackendUsageError("积分服务返回格式异常")
    if not isinstance(d, dict) or d.get("code") != 0:
        raise BackendUsageError("上游未接受该操作，请检查账号或稍后重试",
                                code=d.get("code") if isinstance(d, dict) else None)
    return d.get("data")


def _credits_of(usage):
    """从 usage 里取实测 credit；取不到返回 0.0（护栏按「没花钱」计）。

    字段名逐个试、试不到就当 0：护栏的 credit 计数是**辅助**维度，
    拿到 0 不会让护栏放行本该拦下的请求（token 计数仍然可靠）。
    """
    if not isinstance(usage, dict):
        return 0.0
    for field in ("credit", "credits", "total_credit", "credit_used", "cost"):
        if field in usage:
            try:
                return float(usage[field])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def package_expiry(p):
    """Parse CycleEndTime (Beijing time) into a unix timestamp; None if absent/invalid."""
    value = p.get("CycleEndTime")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp()
    except ValueError:
        return None


def summarize_packages(packages, now=None):
    """Aggregate credits. `expiring_soon` sums the spendable balance of every package whose
    CycleEndTime falls within the next `EXPIRY_SOON_DAYS` days."""
    now = time.time() if now is None else now
    cutoff = now + EXPIRY_SOON_DAYS * 86400
    remain = Decimal(0)
    size = Decimal(0)
    earliest = None
    earliest_remaining = None
    expiring_soon = Decimal(0)
    for p in packages:
        # Precise values carry fractional credit balances; cycle values are spendable now.
        prefix = "Cycle" if any(k in p for k in ["CycleCapacityRemainPrecise", "CycleCapacityRemain"]) else ""
        def field(name, default=None):
            return p.get(prefix + name + "Precise", p.get(prefix + name, default))
        r = number(field("CapacityRemain"))
        capacity = number(field("CapacitySize", r))
        spendable = min(r, capacity) if capacity else r
        remain += spendable
        size += capacity
        if spendable > 0:
            expiry = package_expiry(p)
            if expiry is not None:
                if earliest is None or expiry < earliest:
                    earliest, earliest_remaining = expiry, float(spendable)
                if now < expiry <= cutoff:
                    expiring_soon += spendable
    return {"remaining": float(remain), "packages": len(packages),
            "earliest_expiry": earliest, "earliest_expiry_remaining": earliest_remaining,
            "expiring_soon": float(expiring_soon), "expiring_soon_updated": int(now)}


def number(value):
    """上游金额字段 → Decimal（非法值抛 ValueError，pool 侧兜底转成 0）。

    放在本模块：包里只有积分/签到链路用得到它，core 不依赖 admin，
    而 admin 已不再持有计费细节。
    """
    if value is None:
        raise ValueError("empty")
    return Decimal(str(value))


def fetch_credits(manager, client, headers=None) -> dict:
    """拉本后端的积分余额与过期信息（BackendSpec.credits_fetcher）。

    签名 ``f(manager, client, headers)`` —— headers 由 pool 统一取好传进来
    （pool 还要用同一份头判 X-Domain），本函数内部自行再取一次也行。
    """
    packages = []
    now = datetime.now(CN)
    for page in range(1, BILLING_MAX_PAGES + 1):
        d = _billing(manager, client, "get-user-resource", {
            "PageNumber": page, "PageSize": BILLING_PAGE_SIZE, "ProductCode": "p_tcaca",
            "Status": [0, 3], "PackageEndTimeRangeBegin": now.strftime(USAGE_TIME_FORMAT),
            "PackageEndTimeRangeEnd": "2126-01-01 00:00:00"})
        try:
            data = d["Response"]["Data"]
            current = data["Accounts"]
            total = int(data["TotalCount"])
            if not isinstance(current, list) or total < 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise BackendUsageError("积分数据格式异常，上次余额已保留")
        packages.extend(current)
        if len(packages) >= total:
            return summarize_packages(packages, now=now.timestamp())
        if not current:
            break
    raise BackendUsageError("积分包分页不完整，上次余额已保留")


def fetch_checkin_status(manager, client, headers=None) -> dict:
    """签到状态查询（BackendSpec.checkin_status_fetcher）。

    先问活动状态接口，404/405 回落旧接口；字段名双形态（snake/camel）都试。
    返回 ``{active, checked}`` —— 与 Trae 的 ``{checked, enable, credits}``
    只共用 ``checked`` 一个键，其余各取各的（pool 按需读）。
    """
    try:
        d = _billing(manager, client, "checkin-activity-status")
    except BackendUsageError as e:
        if e.status not in (404, 405):
            raise
        d = _billing(manager, client, "checkin-status")
    if not isinstance(d, dict):
        raise BackendUsageError("签到状态格式异常")
    def flag(snake, camel):
        v = d.get(snake, d.get(camel))
        if v not in (True, False, 0, 1, "true", "false", "1", "0"):
            raise BackendUsageError("签到状态缺少必要字段")
        return v in (True, 1, "true", "1")
    return {"active": flag("active", "active"), "checked": flag("today_checked_in", "todayCheckedIn")}


def fetch_checkin_claim(manager, client, headers=None) -> dict:
    """执行签到（BackendSpec.checkin_claim_fetcher）。

    本后端的签到是「POST daily-checkin 后回查状态」两步：上游不返回可判定的
    业务码，成败只能看回查结果。所以这里**做完两步**再返回一个合成报文，
    交给同模块的 interpret_checkin_claim 解读 —— pool 因此不需要为 WorkBuddy
    单独留一条「先 POST 再回查」的分支。
    """
    _billing(manager, client, "daily-checkin")
    return {"checked": fetch_checkin_status(manager, client, headers)["checked"]}


def interpret_checkin_claim(data: dict) -> tuple[bool, str, bool]:
    """签到结果解读（BackendSpec.interpret_checkin_claim）。

    本后端没有业务码（见 fetch_checkin_claim），只看回查的 checked；
    不可重试 —— 上游要么已接受、要么当场报错，重试同一个签到没有意义。
    """
    if data.get("checked"):
        return True, "签到成功", False
    return False, "签到已提交，但上游尚未确认；请刷新状态", False


def fetch_upstream_usage(manager, client, start, end) -> list[dict]:
    """拉本后端的上游用量明细，返回归一化行（BackendSpec.usage_fetcher）。

    签名 ``f(manager, client, start, end)``：manager 用来取本后端的凭据头，
    client 由调用方给（测试用 MockTransport 注入），start/end 是北京时间
    带时区的 datetime。归一化行字段与 upstream_request_usage 表一一对应：
    request_id / credit / model / client / request_time / agent_purpose / input_trunc。
    """
    headers = manager.get_headers()
    rows, page, total = [], 1, None
    while page <= 200:
        response = client.post(USAGE_BILLING + "get-user-request-usage", headers=headers,
                               json={"startTime": start.strftime(USAGE_TIME_FORMAT),
                                     "endTime": end.strftime(USAGE_TIME_FORMAT),
                                     "pageNum": page, "pageSize": USAGE_PAGE_SIZE})
        if response.status_code != 200:
            raise BackendUsageError("上游用量接口请求失败", response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise BackendUsageError("上游用量接口返回格式异常")
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise BackendUsageError("上游未返回用量明细")
        data = payload.get("data") or {}
        batch = data.get("data") or []
        if not isinstance(batch, list):
            raise BackendUsageError("上游用量明细格式异常")
        total = int(data.get("total") or 0)
        rows.extend(item for item in batch if isinstance(item, dict))
        if not batch or len(rows) >= total:
            break
        page += 1
    normalized = []
    for row in rows:
        request_id = row.get("requestId")
        when = _usage_time(row.get("requestTime"))
        if not request_id or when is None:
            continue
        normalized.append({
            "request_id": request_id,
            "credit": row.get("credit"),
            "model": row.get("model"),
            "client": row.get("client"),
            "request_time": when,
            "agent_purpose": row.get("agentPurpose"),
            # 上游截好的请求内容摘要。只存摘要、不存 input 全文——input 是
            # 完整正文，隐私面差一个量级（见 specs/db-redesign.md）。
            "input_trunc": row.get("inputTrunc") or None,
        })
    return normalized


class WorkBuddyBackend(BackendSpec):
    name = "workbuddy"
    owned_by = "codebuddy"
    tencent_policies = True

    # 默认后端：裸名（以及 auto / 空）在没有候选时都落到本后端。
    # `model_prefix` 留空 —— 本后端**从未**用过前缀（见 BackendSpec 的说明）。
    default_for_missing_backend = True

    # 管理页元数据（需求 2/5/7）
    label = "WorkBuddy"
    short = "WB"
    icon = "◈"
    # 图形场景用 SVG：外菱形描边 + 内实心小菱形（对应字符 ◈ 的「菱形套小菱形」）。
    # viewBox 24×24、fill=currentColor，尺寸全交给 CSS（见 icon_svg 的约定）。
    icon_svg = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M12 2.6 21.4 12 12 21.4 2.6 12z"/>'
        '<path d="M12 9.2 14.8 12 12 14.8 9.2 12z" fill="currentColor" stroke="none"/>'
        "</svg>"
    )
    color = "#2f6fed"
    order = 0
    login_kind = "browser"

    # 凭据：毫秒级 expiresAt，本身的 CredentialManager 就带 backend_name="workbuddy"。
    credential_backend = "workbuddy"
    expires_at_to_ms = 1

    # 积分/签到走 pool 的默认实现（billing 接口），故不声明 account_ops_backend。
    #
    # 上游用量明细（get-user-request-usage）只认本后端凭据：实现放在本模块，
    # 由 usage_fetcher 声明（pool 只读声明，不认后端名）。
    supports_upstream_usage = True
    usage_fetcher = "fetch_upstream_usage"
    #
    # 积分 / 签到同样只认本后端凭据（billing 域），实现也在本模块 —— 由这组
    # fetcher 指名，pool 的 operate() 一条路径分派，不再有「默认后端特权代码」。
    # checkin_claim 留空：本后端的签到是「POST daily-checkin 后回查状态」，
    # 没有独立 claim 接口，也不需要 interpret_checkin_claim（上游直接给结果）。
    credits_fetcher = "fetch_credits"
    checkin_status_fetcher = "fetch_checkin_status"
    checkin_claim_fetcher = "fetch_checkin_claim"
    billing_fetcher = "_billing"
    #
    # 模型目录来自 /v3/config（见 admin/models.py:fetch）。它只认**本后端**账号的
    # 凭据，所以拉目录时必须挑一个默认后端的账号 —— 由本声明告诉 admin，
    # 而不是在 admin 里写死「用 workbuddy 账号」。
    needs_own_credential_for_catalog = True

    # 三档智能路由（fast / balanced / deep）是**本后端私有**的虚拟模型：
    # 无前缀，且不参与裸名自动选择（那种请求只会命中默认后端＝本后端）。
    # 声明出来是防将来路由策略改动把它们选到别的后端去 —— 这不是「无前缀
    # 所以恰好归我」，而是「本来就只可能归我」。
    pinned_models = ("fast-model", "balanced-model", "deep-model")

    # 跨后端规范名（specs/model-routing-by-policy.md §4）：**只声明非恒等映射**。
    #
    # 为什么必须显式声明：腾讯的 Kimi K3 上游 id 叫 `kimi-k3-1`，字节的叫
    # `trae/kimi-k3`。剥掉前缀后是 `kimi-k3-1` vs `kimi-k3` —— 天然不等，
    # 靠字符串规则永远合不起来。用户要的是「一个 Kimi K3」，所以要把腾讯这个
    # 偏了一截的 id 显式指向规范名。
    #
    # 为什么不写 `{"kimi-k2.6": "kimi-k2.6"}` 这种恒等映射：它没有信息量，
    # 却让「加模型」变成「记得也来这里抄一遍 id」，抄漏了不报错、只是静默不合组。
    # 默认规则（`canonical_name`）已经把「剥前缀即规范名」覆盖掉了。
    canonical_for = {
        "kimi-k3-1": "kimi-k3",
    }

    def chat_url(self) -> str:
        from .. import converter  # 延迟 import 防循环

        return f"{converter.BACKEND}/v2/chat/completions"

    def make_credential(self, path):
        from .. import converter  # 延迟 import 防循环

        return converter.CredentialManager(path)

    def interpret_checkin_claim(self, data: dict):
        """签到结果解读。实现见模块级同名函数。"""
        return interpret_checkin_claim(data)

    async def astream(self, headers, body, *, timeout=None, raise_on_error=False):
        url = self.chat_url()
        # 站点头只加在 chat 路径（见 _site_headers 的说明）。
        headers = _site_headers(headers)
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        raw = await r.aread()
                        raise BackendHTTPError(r.status_code, raw)
                    async for chunk in r.aiter_bytes():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as e:
            raise BackendHTTPError(502, str(e), transport=True) from e
