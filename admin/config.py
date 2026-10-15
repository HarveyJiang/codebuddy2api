"""兼容垫片：配置实现已下沉到 core（standalone 镜像只 COPY core，core 不能依赖 admin）。

保留这个模块名是为了让 ``from admin import config`` 这类既有导入继续可用。
"""
from core.runtime_config import (  # noqa: F401
    Config,
    get,
    raw,
    reset,
)

__all__ = ["Config", "get", "raw", "reset"]
