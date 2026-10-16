#!/usr/bin/env bash
#
# dev-deploy.sh —— 开发验证环境的「本地构建 + 直跑」一站式脚本
#
# 存在的原因：验证一个改动不需要等 GitHub Actions 编译（那个有月度时间额度）。
# 这个脚本把源码打包传到宿主机，在宿主机上 docker build，然后直接起一个
# **与生产完全隔离**的开发实例指向 codebuddy-dev.2020224.xyz。
#
# 隔离的四件事（任何一条破了都可能影响生产）：
#   1. 容器名      workbuddy-oneclick  → workbuddy-dev
#   2. 宿主机端口   8787                → 8788（都只绑 127.0.0.1）
#   3. 数据目录    data/               → data/（整目录独立，state.db 不共享）
#   4. 密钥        各自生成，永不复用生产的 ADMIN_KEY / API KEY
#
# **凭据不自动复制**：两个进程各持一份同一个 WorkBuddy 账号的凭据文件时，
# 刷新令牌是单次有效的——dev 刷新一次会让生产手里那份失效，直接打挂线上。
# 所以 dev 从零账号开始，用管理页的「浏览器授权」单独登一个账号。
#
# 用法：
#   scripts/dev-deploy.sh              # 构建 + 重启 + 等健康
#   scripts/dev-deploy.sh --logs       # 顺便看日志
#   scripts/dev-deploy.sh --rebuild    # 强制无缓存重建（依赖/base 变了才需要）
#   scripts/dev-deploy.sh --shell      # 进 dev 容器
set -euo pipefail

# --- 连接 -------------------------------------------------------------------
SSH_PORT="${DEV_SSH_PORT:-15072}"
SSH_HOST="${DEV_SSH_HOST:-172.30.0.1}"
REMOTE="root@${SSH_HOST}"

# --- 目标（须与 openresty 站点配置一致）-------------------------------------
DEV_DIR="/opt/docker/workbuddy2api-dev"
DEV_TAG="dev-local"                      # 本地构建的镜像 tag，不带时间戳
DEV_PORT=8788
DEV_URL="https://codebuddy-dev.2020224.xyz"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m✘\033[0m %s\n' "$*" >&2; exit 1; }

show_logs=0; rebuild=0; shell=0
for a in "$@"; do
  case "$a" in
    --logs)    show_logs=1 ;;
    --rebuild) rebuild=1 ;;
    --shell)   shell=1 ;;
    *) die "未知参数：$a" ;;
  esac
done

TMP_TGZ="$(mktemp -t dev-deploy-XXXXXX.tar.gz)"
trap 'rm -f "$TMP_TGZ"' EXIT

# --- 打包源码：git 跟踪 + 未跟踪（排除 gitignore 的）------------------------
# --others --exclude-standard 会带上新写的未跟踪文件但跳过被 ignore 的
# （__pycache__ / *.pyc 等）。所以改完没 commit 也能直接发上去验证。
LIST="$(mktemp)"; trap 'rm -f "$TMP_TGZ" "$LIST"' EXIT
git -C "$REPO_DIR" ls-files -z --cached --others --exclude-standard > "$LIST"
# 注意用 tr '\0' '\n'（翻译）而不是 tr -d '\0'（删除）——后者会把所有文件名
# 连成一行，计数恒为 1，看着像只打包了一个文件。
N="$(tr '\0' '\n' < "$LIST" | grep -c .)"
log "打包源码：${N} 个文件（含未跟踪）"
tar -C "$REPO_DIR" -czf "$TMP_TGZ" --null -T "$LIST"

log "传到 ${REMOTE}:${DEV_DIR}"
ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o BatchMode=yes "$REMOTE" "mkdir -p ${DEV_DIR}"
scp -P "$SSH_PORT" -o StrictHostKeyChecking=no -o BatchMode=yes \
    "$TMP_TGZ" "${REMOTE}:${DEV_DIR}/src.tar.gz" >/dev/null
ok "已传输"

# --- 远端：解包 → 构建 → 起容器 ---------------------------------------------
# 外层 heredoc **完全引用**（'REMOTE'），本地不做任何展开，三个值靠位置参数
# 传给远端。之前用不引用的 heredoc + 反斜杠转义 $ 是个坑：转义挡住了本地展开，
# 远端 set -u 又会去展开同一个变量，结果 unbound。
#
# 参数：$1=DEV_DIR  $2=DEV_PORT  $3=DEV_TAG  $4=rebuild(0/1)
ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o BatchMode=yes "$REMOTE" \
    bash -s -- "$DEV_DIR" "$DEV_PORT" "$DEV_TAG" "$rebuild" <<'REMOTE_SCRIPT'
set -euo pipefail
DEV_DIR="$1"; DEV_PORT="$2"; DEV_TAG="$3"; REBUILD="$4"

cd "$DEV_DIR"
rm -rf src && mkdir -p src
tar -xzf src.tar.gz -C src

# 1) env —— 只在首次生成，之后复用。否则每次重建都换密钥，客户端全断线。
if [ ! -f .env ]; then
  umask 077
  {
    echo "ADMIN_KEY=admin-dev-$(openssl rand -hex 24)"
    echo "CODEBUDDY2OPENAI_KEY=sk-dev-$(openssl rand -hex 24)"
    echo "PORT=${DEV_PORT}"
  } > .env
  echo "  首次生成 .env（随机密钥，见 ${DEV_DIR}/.env）"
fi
# shellcheck disable=SC1091
set -a; . ./.env; set +a

# 2) compose（与生产同构，容器名/端口/数据目录独立）
cat > compose.yml <<EOF
name: workbuddy-dev
services:
  app:
    build:
      context: ./src
      dockerfile: deploy/one-click/Dockerfile
    image: ghcr.io/harveyjiang/codebuddy2api:${DEV_TAG}
    container_name: workbuddy-dev
    restart: unless-stopped
    ports:
      - "127.0.0.1:\${PORT}:8787"
    environment:
      ADMIN_KEY: \${ADMIN_KEY:?未设置 ADMIN_KEY}
      CODEBUDDY2OPENAI_KEY: \${CODEBUDDY2OPENAI_KEY:?未设置 CODEBUDDY2OPENAI_KEY}
      CODEBUDDY_AUTH_DIR: /data/auth
      MANAGEMENT_DATA_DIR: /data/management
      WORKBUDDY_IMAGE_TAG: ${DEV_TAG}
      TZ: Asia/Shanghai
    volumes:
      - ./data/auth:/data/auth
      - ./data/management:/data/management
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=5)"]
      interval: 30s
      timeout: 10s
      retries: 3
EOF

mkdir -p data/auth data/management

# 3) 构建
if [ "$REBUILD" = "1" ]; then
  docker compose build --no-cache --pull
else
  docker compose build
fi

# 4) 起
docker compose up -d

# 5) 等健康（最多 90s）
for _ in $(seq 1 45); do
  st=$(docker inspect workbuddy-dev --format '{{.State.Health.Status}}' 2>/dev/null || echo none)
  [ "$st" = "healthy" ] && break
  sleep 2
done

echo
docker inspect workbuddy-dev --format '容器 {{.Name}}  镜像 {{.Config.Image}}  健康 {{.State.Health.Status}}'
REMOTE_SCRIPT

if [ "$shell" = "1" ]; then
  exec ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o BatchMode=yes "$REMOTE" \
    'docker exec -it workbuddy-dev sh'
fi

# --- 验证：走完整外网链路（域名 → Cloudflare → openresty → 容器）-------------
log "验证 ${DEV_URL}（首次构建后可能还在启动）"
CODE=000
for _ in $(seq 1 10); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$DEV_URL/health" || echo 000)
  [ "$CODE" = "200" ] && break
  sleep 3
done

echo
if [ "$CODE" = "200" ]; then
  ok "健康检查 200"
  curl -s --max-time 15 "$DEV_URL/health" | head -c 300; echo
else
  warn "健康检查返回 ${CODE} —— 看日志：scripts/dev-deploy.sh --logs"
fi

echo
ok "开发环境就绪：$DEV_URL"
cat <<INFO
  · 管理密钥：ssh -p ${SSH_PORT} ${REMOTE} 'cat ${DEV_DIR}/.env'
  · 看日志：  scripts/dev-deploy.sh --logs
  · 进容器：  scripts/dev-deploy.sh --shell
  · 重新部署（改了源码就再跑一次本脚本，不用提交 GitHub）
INFO

if [ "$show_logs" = "1" ]; then
  echo
  log "容器日志（最后 40 行）"
  ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o BatchMode=yes "$REMOTE" \
    'docker logs --tail 40 workbuddy-dev 2>&1'
fi