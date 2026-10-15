"""配置的唯一读取入口。

设计要点：
- **导入即解析**：`.env` 在模块导入时读取，而不是等某个 ``load()`` 被调用。这样任何
  ``from admin import config`` 拿到的都是已加载的值，没有「必须先调用初始化」的时序陷阱——
  ``core.converter`` 在模块级就要求值（``_PRODUCT_NAME`` 等常量）。
- **优先级**：命令行参数 > 真实环境变量 > `.env` 文件 > 代码默认值。
  `.env` 用 ``setdefault`` 语义写入 ``os.environ``，因此 docker/compose 显式传的变量
  始终赢过文件，符合直觉。
- **旧名继续生效**：``CODEBUDDY_*`` / ``CLIENT_INFO_*`` 这些名字用户已经写在 compose 里了，
  改名会静默失效。这里保留别名映射，命中旧名时告警一次。
- 平台标准变量（``LOCALAPPDATA``/``XDG_DATA_HOME``）不属于本项目配置，不在此收敛。
"""
import os

# .env 路径：默认取当前工作目录，可用 WORKBUDDY_ENV_FILE 覆盖（测试用 /dev/null 规避）。
_ENV_FILE = os.environ.get("WORKBUDDY_ENV_FILE", ".env")

# (规范名, (旧名字, ...))。取值时按「规范名 → 旧名依次」找第一个有值的。
_ALIASES = {
    "WORKBUDDY_DATA_DIR": ("MANAGEMENT_DATA_DIR",),
    "WORKBUDDY_AUTH_DIR": ("CODEBUDDY_AUTH_DIR",),
    "WORKBUDDY_HOST": (),
    "WORKBUDDY_PORT": ("PORT",),
    "WORKBUDDY_ADMIN_KEY": ("ADMIN_KEY",),
    "WORKBUDDY_API_KEY": ("CODEBUDDY2OPENAI_KEY",),
    "WORKBUDDY_LOG": ("CODEBUDDY2OPENAI_LOG",),
    "WORKBUDDY_LOG_LEVEL": (),
    "WORKBUDDY_STREAM_IDLE_TIMEOUT": (),
    "WORKBUDDY_DESENSITIZE": ("CODEBUDDY_RESPONSES_DESENSITIZE",),
    "WORKBUDDY_NO_COMPACT": (),
    "WORKBUDDY_LOSSY_PROJECTION": ("CODEBUDDY_LOSSY_PROJECTION",),
    # 伪装头也走统一入口，但保持原有名字（README 已公开文档化，改名收益低、破坏面大）。
    "WORKBUDDY_PRODUCT_NAME": ("CLIENT_INFO_PRODUCT_NAME",),
    "WORKBUDDY_PRODUCT_VERSION": ("CLIENT_INFO_PRODUCT_VERSION",),
    "WORKBUDDY_UA_EXTENSION": ("CLIENT_INFO_USER_AGENT_EXTENSION",),
}

_warned = set()


def _parse_env_file(path):
    """最小 .env 解析：KEY=VALUE、# 注释、空行、值两侧引号。

    不覆盖已存在的真实环境变量（setdefault），环境变量优先级更高。
    文件不存在或不可读时静默返回——没有 .env 是正常情况，不是错误。
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_parse_env_file(_ENV_FILE)


def raw(name, default=None):
    """按「规范名 → 旧名」取第一个有值的环境变量。

    命中旧名时告警一次（每个变量只提醒一次，不刷屏）。
    """
    canonical = _ALIASES.get(name, ())
    if os.environ.get(name):
        return os.environ[name]
    for alias in canonical:
        value = os.environ.get(alias)
        if value:
            if alias not in _warned:
                _warned.add(alias)
                import logging

                logging.getLogger("admin.config").warning(
                    "配置 %s 已弃用，请改用 %s", alias, name
                )
            return value
    return default


def _to_bool(value, default=False):
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _to_int(value, default, name):
    """解析整数；非法值直接抛错，避免把坏配置带到运行期才炸。"""
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        raise ValueError(f"配置 {name} 必须是整数，当前为 {value!r}")


class Config:
    """启动时配置快照。

    属性在构造时求值一次；运行期改环境变量不会生效（容器部署下改配置即重启，语义一致）。
    """

    def __init__(self):
        self.data_dir = raw("WORKBUDDY_DATA_DIR", "/data/management")
        self.auth_dir = raw("WORKBUDDY_AUTH_DIR", "/data/auth")
        # host 默认值由调用方决定：admin.server 是容器入口，必须监听全网卡
        # （否则 compose 的端口映射进不来）；converter 本地直跑时用 127.0.0.1。
        self.host = raw("WORKBUDDY_HOST") or None
        self.port = _to_int(raw("WORKBUDDY_PORT"), 8787, "WORKBUDDY_PORT/PORT")
        if not 1 <= self.port <= 65535:
            raise ValueError(f"端口必须在 1-65535 之间，当前为 {self.port}")
        self.admin_key = raw("WORKBUDDY_ADMIN_KEY", "")
        self.api_key = raw("WORKBUDDY_API_KEY", "")
        self.log_path = raw("WORKBUDDY_LOG") or None
        self.log_level = self._level(raw("WORKBUDDY_LOG_LEVEL", "INFO"))
        self.stream_idle_timeout = _to_int(
            raw("WORKBUDDY_STREAM_IDLE_TIMEOUT"), 120, "WORKBUDDY_STREAM_IDLE_TIMEOUT"
        )
        self.desensitize = _to_bool(raw("WORKBUDDY_DESENSITIZE"), False)
        self.no_compact = _to_bool(raw("WORKBUDDY_NO_COMPACT"), False)
        self.lossy_projection = _to_bool(raw("WORKBUDDY_LOSSY_PROJECTION"), False)
        self.product_name = raw("WORKBUDDY_PRODUCT_NAME", "WorkBuddy")
        self.product_version = raw("WORKBUDDY_PRODUCT_VERSION", "5.8.2")
        self.ua_extension = raw("WORKBUDDY_UA_EXTENSION", "CLI/2.161.0")

    @staticmethod
    def _level(value):
        import logging

        name = str(value).strip().upper()
        if hasattr(logging, name) and isinstance(getattr(logging, name), int):
            return name
        import logging as _logging

        _logging.getLogger("admin.config").warning(
            "未知日志级别 %r，回退 INFO", value
        )
        return "INFO"


_config = None


def get():
    """取全局配置（首次调用时构造并缓存）。"""
    global _config
    if _config is None:
        _config = Config()
    return _config


def reset():
    """丢弃缓存，重新读环境变量。仅供测试使用。"""
    global _config
    _config = None
