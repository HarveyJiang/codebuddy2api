"""core.backends — 上游后端抽象层。

把「上游 HTTP + SSE」封装成统一接口 `BackendSpec.astream`，其产出的字节
**已经是 OpenAI Chat SSE**（WorkBuddy＝上游原样直通；Trae＝SOLO SSE 归一化后）。
三个协议适配器（chat/responses/anthropic）因此只依赖这一个接口。

禁止在本包模块顶部 import core.converter（循环依赖）：converter 顶部 import 本包，
本包需要 converter 的 CredentialManager 时一律函数内延迟 import。
"""
