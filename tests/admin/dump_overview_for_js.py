"""把 WB_TEST_DB 指向的库跑一遍管理页 overview，输出 JSON 给前端测试用。

存在的理由：前端测试要跑在 `/admin/api/overview` 的**真实形状**上（字段名、source、
credits 都由后端组装决定）。手写 fixture 会漂，测的就成了「我以为的接口」。

走 `create_app` + ASGITransport，与 rendermodels.test.mjs 里的内联脚本同路 ——
只有走完整启动路径，注册表才会被铺上内置/静态目录（直接 ModelRegistry() 拿到的是
空列表，见 dump 出来 models: [] 那个坑）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根


async def _dump(root: Path) -> dict:
    import httpx

    from admin import server as s

    app = s.create_app(root / "management", root / "auth",
                       "sk-test-client-key", "admin-key-long-enough-1234")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.post("/admin/api/login", json={"key": "admin-key-long-enough-1234"})
        c.headers["Cookie"] = r.headers.get("set-cookie", "").split(";")[0]
        overview = (await c.get("/admin/api/overview")).json()
        backends = (await c.get("/admin/api/backends")).json()
    return {"models": overview["models"], "backends": backends["backends"]}


def main() -> int:
    root = Path(os.environ.get("WB_TEST_DB", "/tmp/opencode/v7fresh"))
    if not (root / "management" / "state.db").exists():
        print(f"未找到 {root}/management/state.db", file=sys.stderr)
        return 1
    json.dump(asyncio.run(_dump(root)), sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
