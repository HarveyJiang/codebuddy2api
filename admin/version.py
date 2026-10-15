"""部署版本标识：让界面能一眼看出线上跑的是哪一次发布。

镜像由 docker-factory 构建，容器里**没有 .git**（只 COPY core/ 与 admin/），
所以版本不可能从 git 现场推出来——必须由部署侧注入。约定：

1. ``WORKBUDDY_IMAGE_TAG``：compose 里把 image 的 tag 原样传给容器，
   值形如 ``main-62291f0fc1a5fbdea815707117e45bf3778a0a24`` 或 ``20261015-163101``。
   与 ``docker compose ps`` 看到的镜像完全一致，是最可信的口径。
2. ``WORKBUDDY_VERSION``：想在界面显示语义版本（如 ``1.2.0``）时用它，
   优先于镜像 tag 展示。缺省不设。
3. 都取不到时回落到 ``unknown``，界面照常渲染，不因缺版本而报错。

与 ``core/runtime_config.raw`` 一致：用 ``in os.environ`` 判断「有没有设」，
显式设为空串算「设了」，好让部署方有办法主动把版本显示关掉。
"""
import os


def image_tag():
    """镜像 tag（部署侧注入）。取不到返回 None。"""
    for name in ("WORKBUDDY_IMAGE_TAG", "CODEBUDDY2OPENAI_IMAGE_TAG"):
        value = os.environ.get(name)
        if name in os.environ and value and value.strip():
            return value.strip()
    return None


def semantic():
    """人工指定的语义版本（可选）。取不到返回 None。"""
    value = os.environ.get("WORKBUDDY_VERSION")
    if "WORKBUDDY_VERSION" in os.environ and value and value.strip():
        return value.strip()
    return None


def describe():
    """给界面用的展示串，永远非空。"""
    return semantic() or image_tag() or "unknown"


def snapshot():
    """结构化版本信息，供 /health 与 overview 使用。

    ``label`` 是界面上那一行字；另两个字段保留原始来源，排查时能分辨
    「显示的是手写版本还是镜像 tag」。
    """
    return {"label": describe(), "semantic": semantic(), "image_tag": image_tag()}
