"""兼容垫片：日志实现在 core（standalone 镜像只 COPY core，core 不能依赖 admin）。"""
from core.runtime_logging import (  # noqa: F401
    configured,
    setup_logging,
    uvicorn_level,
)

__all__ = ["setup_logging", "uvicorn_level", "configured"]
