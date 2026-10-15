"""日志初始化：统一输出到 stderr，可选额外落文件并轮转。

设计要点：
- **默认即有输出**。此前 ``_log()`` 只在 ``--log PATH`` 设了才写、否则静默丢弃，
  容器里不传参数就等于没有任何日志。现在恒有 stderr handler，由容器运行时收集。
- **幂等**。重复调用不叠加 handler（先移除本模块自己加过的），避免每次 create_app
  都多一行输出。
- **第三方降噪**。本项目开 DEBUG 时不该把 httpx 的连接细节刷满屏。
- 时间戳统一北京时间，与项目其它展示口径一致。
"""
import logging
import logging.handlers
import sys
from datetime import datetime

from .models import CN

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 本模块加过 handler 的 logger，用于幂等清理。
_managed = []


class _BeijingFormatter(logging.Formatter):
    """用北京时间渲染 asctime。

    logging 默认用本地时区，容器通常是 UTC，日志时间会与后台展示差 8 小时。
    """

    def formatTime(self, record, datefmt=None):
        moment = datetime.fromtimestamp(record.created, CN)
        return moment.strftime(datefmt or _DATEFMT)


def setup_logging(level="INFO", path=None):
    """配置根 logger。level 是级别名，path 非空时额外写文件（10MB × 3 轮转）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_workbuddy", False):
            root.removeHandler(handler)
            handler.close()

    formatter = _BeijingFormatter(_FORMAT, _DATEFMT)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream._workbuddy = True
    root.addHandler(stream)
    handlers = [stream]

    if path:
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler._workbuddy = True
            root.addHandler(file_handler)
            handlers.append(file_handler)
        except OSError as exc:  # 路径不可写不该拦住启动
            root.warning("日志文件 %s 不可写，仅输出到 stderr：%s", path, exc)

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # 第三方降噪：本项目的 DEBUG 不该带出 httpx / httpcore 的连接细节。
    for name in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)

    global _managed
    _managed = handlers
    return root


def uvicorn_level():
    """uvicorn 的 log_level 参数：跟随本项目级别。

    此前两个入口都硬编码 ``log_level="warning"``，把 uvicorn 的启动错误、
    端口占用、异常堆栈全压掉了。这里让 uvicorn 的错误至少可见。
    """
    root = logging.getLogger()
    if root.level <= logging.DEBUG:
        return "debug"
    if root.level <= logging.INFO:
        return "info"
    return "warning"


def configure_uvicorn_access(enable=False):
    """uvicorn 的访问日志默认关闭——请求已经落在 request_logs 表里，重复记无意义。

    仅在需要逐请求排查时打开（``enable=True``）。
    """
    logger = logging.getLogger("uvicorn.access")
    logger.disabled = not enable
    if not enable:
        logger.setLevel(logging.CRITICAL)
    return logger
