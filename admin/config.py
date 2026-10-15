"""兼容垫片：配置实现已下沉到 core（standalone 镜像只 COPY core，core 不能依赖 admin）。

保留这个模块名是为了让 ``from admin import config`` / ``import admin.server`` 这类
既有导入继续可用。这里**透传** core 的实现，不用手写名单——手写名单会在 core 新增
函数时静默漏掉（曾经漏掉 legacy_used()，导致 create_app 直接 AttributeError）。
"""
from core import runtime_config as _impl

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals().setdefault(_name, getattr(_impl, _name))

__all__ = [n for n in dir(_impl) if not n.startswith("__")]
