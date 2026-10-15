"""兼容垫片：日志实现在 core（standalone 镜像只 COPY core，core 不能依赖 admin）。

与 admin/config.py 同理，这里透传而不是手写名字，避免 core 新增函数时漏转发。
"""
from core import runtime_logging as _impl

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals().setdefault(_name, getattr(_impl, _name))

__all__ = [n for n in dir(_impl) if not n.startswith("__")]
