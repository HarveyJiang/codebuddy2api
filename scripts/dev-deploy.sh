#!/usr/bin/env bash
#
# dev-deploy.sh —— 开发验证环境的「本地构建 + 直跑」一站式脚本
#
# 存在的原因：验证一个改动不需要等 GitHub Actions 编译（那个有月度时间额度）。
# 这个脚本把源码打包传到宿主机，在宿主机上 docker build，然后直接起一个
# **与生产完全隔离**的开发实例指向 codebuddy-dev.2020224.xyz。
#
# 隔离的三件事（任何一条破了都可能影响生产）：
#   1. 容器名      workbuddy-oneclick  → workbuddy-dev
#   2. 宿主机端口   8787                → 8788（都只绑 127.0.0.1）
#   3. 状态库      state.db            → 各自独立（dev 要跑 schema 迁移，且两套
#                                       SQLite 并发写同一个库会真的锁坏）
#
# **密钥刻意与生产相同**（ADMIN_KEY / CODEBUDDY2OPENAI_KEY 直接取自生产 .env）：
# 客户端只改 base URL 就能在 prod / dev 之间切，不用维护两套配置。
# 这与「库要独立」不冲突——env 里的那个 API key 走 check_api 的 test_keys 兜底
# 分支，根本不查库；库里那几条 key 才是各实例私有的。
#
# **账号用生产同一份凭据**（挂载生产的 auth 目录，不是复制）：
#
# 这里刻意**不复制**。复制会得到两份会分叉的文件——生产刷新出 T2 写进自己那份、
# dev 还拿着旧的 T1，而刷新令牌是单次有效的，dev 那次刷新会让生产下次刷新失败。
# 挂载**同一个目录**则是单一真相源：谁刷新谁写回共享文件，另一个下次 mtime 检查
# 就读到新的。
#
# 残余风险只有一个：两个进程在同一瞬间并发刷新同一份过期凭据，其中一次会失败。
# 后果可控——凭据写回是原子的（tmp + os.replace，不会写坏文件），失败的进程抛
# RuntimeError 只影响那一次请求，下次请求重读到新 token 即恢复。
#
# 挂载必须读写：只读的话生产停掉超过 token 有效期，dev 就自己续不上了。
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

# dev 直接挂生产同一份凭据目录（见文件头说明：挂载共享安全，复制才危险）
SHARED_AUTH_DIR="${DEV_AUTH_DIR:-/opt/docker/workbuddy2api/data/auth}"
# 密钥与生产共用，从生产的 .env 读取（脚本只在宿主机上 cat，不回显值）
PROD_ENV="${DEV_PROD_ENV:-/opt/docker/workbuddy2api/.env}"

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
# 参数：$1=DEV_DIR $2=DEV_PORT $3=DEV_TAG $4=rebuild $5=共享 auth 目录 $6=生产 .env
ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o BatchMode=yes "$REMOTE" \
    bash -s -- "$DEV_DIR" "$DEV_PORT" "$DEV_TAG" "$rebuild" "$SHARED_AUTH_DIR" "$PROD_ENV" <<'REMOTE_SCRIPT'
set -euo pipefail
DEV_DIR="$1"; DEV_PORT="$2"; DEV_TAG="$3"; REBUILD="$4"; SHARED_AUTH="$5"; PROD_ENV="$6"

# 必须先 export：下面的 compose heredoc 是**不引用**的，写文件时远端会展开
# ${SHARED_AUTH_DIR}，那时还没定义的话 set -u 会直接报 unbound。
export SHARED_AUTH_DIR="$SHARED_AUTH"

cd "$DEV_DIR"
rm -rf src && mkdir -p src
tar -xzf src.tar.gz -C src

# 1) env —— 密钥直接取自生产的 .env（不打印到日志）。
#    每次部署重新生成：dev 侧没有任何需要保留的私有值，PORT 是常量。
if [ ! -f "$PROD_ENV" ]; then
  echo "  找不到生产 .env：$PROD_ENV" >&2
  exit 1
fi
prod_admin=$(grep -E '^ADMIN_KEY=' "$PROD_ENV" | head -1 | cut -d= -f2-)
prod_api=$(grep -E '^CODEBUDDY2OPENAI_KEY=' "$PROD_ENV" | head -1 | cut -d= -f2-)
if [ -z "$prod_admin" ] || [ -z "$prod_api" ]; then
  echo "  生产 .env 里 ADMIN_KEY / CODEBUDDY2OPENAI_KEY 缺失或为空" >&2
  exit 1
fi
umask 077
{
  echo "ADMIN_KEY=${prod_admin}"
  echo "CODEBUDDY2OPENAI_KEY=${prod_api}"
  echo "PORT=${DEV_PORT}"
} > .env

# 共享密钥变了就重置 dev 库，理由必须讲清楚：
# server.py 里「把 env 里的 initial_key 收编成一条 key」被
#     if not accounts and not keys:
# 门控 —— 那是**一次性引导**，不是持续同步。所以已经建过库的实例，
# 换一个 CODEBUDDY2OPENAI_KEY 重启后并不会自动拥有新密钥，会静默 401。
#
# 这是产品设计而非缺陷：env 是引导种子，之后 DB 是唯一真相源；若改成每次启动
# 都同步，UI 里建的 key 会被 env 覆盖，多密钥管理就废了。
#
# dev 的库本就是一次性的（要跑 schema 迁移、反复增删字段），所以这里在检测到
# 共享密钥指纹变化时重置它，好让应用自己的收编逻辑重新跑一遍 —— 而不是从脚本
# 里直接 INSERT keys 表（那会绕过应用、且列名随迁移变，脆）。
fp_file="data/.shared-key-fp"
new_fp=$(printf '%s' "$prod_api" | sha256sum | cut -c1-16)
old_fp=$(cat "$fp_file" 2>/dev/null || echo "")
# 指纹一变就必须重建容器——不只是「文件存在才重建」。env 的收编只发生在**启动时**
# 且只在空库上，所以哪怕库文件已经不在了（上一轮删过），容器也仍持有旧内存里的
# 旧密钥，不重启就等于没换。
NEED_RESTART=0
if [ "$new_fp" != "$old_fp" ]; then
  if [ -f data/management/state.db ]; then
    echo "  共享密钥已变 → 重置 dev 库"
    rm -f data/management/state.db data/management/state.db-wal data/management/state.db-shm
  else
    echo "  共享密钥已变 → 库本就不存在，仍需重建以重新走启动收编"
  fi
  NEED_RESTART=1
  printf '%s' "$new_fp" > "$fp_file"
else
  echo "  env：密钥复用生产（指纹 ${new_fp}，未变，保留 dev 库）"
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
      # 凭据：挂生产同一份目录（单一真相源，理由见脚本头）。
      # 状态：dev 自己的目录，绝不与生产共享——两套 state.db 并发写同一个
      # SQLite 会真的把库锁坏。
      - ${SHARED_AUTH_DIR}:/data/auth
      - ./data/management:/data/management
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=5)"]
      interval: 30s
      timeout: 10s
      retries: 3
EOF

mkdir -p data/management

if [ ! -d "$SHARED_AUTH" ]; then
  echo "  共享凭据目录不存在：$SHARED_AUTH" >&2
  exit 1
fi
echo "  共享凭据目录：$SHARED_AUTH（$(ls -1 "$SHARED_AUTH"/*.info 2>/dev/null | wc -l) 个凭据）"

# 3) 构建
if [ "$REBUILD" = "1" ]; then
  docker compose build --no-cache --pull
else
  docker compose build
fi

# 4) 起
#    重置过库就**必须**强制重建：env 没变时 compose 判定配置未变、不会重启，
#    容器会继续用着旧内存（旧密钥），于是删了库却仍然 401 —— 删库不重建是白删。
if [ "$NEED_RESTART" = "1" ]; then
  echo "  强制重建容器（需重新走一遍启动收编，否则仍在用旧密钥的内存）"
  docker compose up -d --force-recreate
else
  docker compose up -d
fi

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
  · 密钥与生产相同，直接用 https://codebuddy.2020224.xyz 的那套
  · dev 私有配置：ssh -p ${SSH_PORT} ${REMOTE} 'cat ${DEV_DIR}/.env'（内容=生产密钥+PORT）
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