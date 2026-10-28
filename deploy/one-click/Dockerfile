# syntax=docker/dockerfile:1
# 一键式部署镜像：自包含，无需预先构建基础镜像。
# 同时包含协议转换核心与中文管理后台，以管理后台模式启动。
FROM python:3.14-slim

# 构建期网络：pypi.org 在本机实测 ~22KB/s（单包 10-30s），阿里云镜像 ~260KB/s
# 需要换源时：docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

WORKDIR /app

COPY requirements.lock.txt .
# 依赖单独成层：lock 不变 == 整层命中缓存，不会重装
# 缓存挂载：lock 变了也复用已下载的 wheel，不重复走网络
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" \
                --timeout 60 --retries 5 \
                -r requirements.lock.txt

COPY core ./core
COPY admin ./admin

EXPOSE 8787

CMD ["python3", "-m", "admin.server"]
