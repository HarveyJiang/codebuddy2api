"""日志初始化：统一输出到 stderr，可选额外落文件并轮转。

放在 core 层（不是 admin）：deploy/standalone 只 COPY core，独立转换器镜像
必须能只用 core 起得来，所以 core 不能反向依赖 admin。

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
from datetime import datetime, timedelta, timezone

from .log_redact import RedactingFilter

# 北京时间。core 层不依赖 admin.models，这里自带一份。
CN = timezone(timedelta(hours=8))

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
    """配置根 logger。level 是级别名，path 非空时额外写文件（10MB × 3 轮转）。

    幂等：重复调用不叠加 handler。注意这会**重新配置**——调用方若已自行装好
    handler，再调这里会把它们换掉。导入式启动（uvicorn admin.server:create_app）
    由 create_app 内部按需调用，不会覆盖已有的落盘配置。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_workbuddy", False):
            root.removeHandler(handler)
            handler.close()

    formatter = _BeijingFormatter(_FORMAT, _DATEFMT)
    redactor = RedactingFilter()
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    # 脱敏挂在 handler 上（不是 logger）：这样 httpx / uvicorn 自己打的日志
    # 也会被过一遍，而不只是本项目 _log() 出来的记录。
    stream.addFilter(redactor)
    stream._workbuddy = True
    root.addHandler(stream)
    handlers = [stream]

    if path:
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redactor)
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


def configured():
    """根 logger 是否已有本项目装的 handler（判断是否需要初始化）。"""
    return any(getattr(h, "_workbuddy", False) for h in logging.getLogger().handlers)


def uvicorn_level():
    """uvicorn 的 log_level 参数：跟随本项目级别。

    此前两个入口都硬编码 ``log_level="warning"``，把 uvicorn 的启动错误、
    端口占用、异常堆栈全压掉了。这里让 uvicorn 的错误至少可见。

    访问日志请用 ``uvicorn.run(..., access_log=False)`` 关闭，不要在这里改
    ``uvicorn.access`` 的 level——``uvicorn.run()`` 内部会 dictConfig 把它重置回去。
    """
    root = logging.getLogger()
    if root.level <= logging.DEBUG:
        return "debug"
    if root.level <= logging.INFO:
        return "info"
    return "warning"
