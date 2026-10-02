#!/usr/bin/env bash
# 一键部署 Agent Gateway：拉取代码 → 构建镜像 → 启动服务 → 健康检查。
# 可重复执行；凭据与配置只在服务器本地，不写入仓库。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

case "${1:-}" in
    -h|--help)
        cat <<'USAGE'
用法：./deploy.sh

前置条件：
  - 已安装 Docker 与 compose 插件。
  - 本目录已有 .env：可运行 python setup_gateway.py 生成，或参考 config.live.example.json / .env.example 手写。
  - 本目录已有 config.json，需为 mode: live 并填好路由与上游真实模型 ID。
  - 主应用栈已创建外部网络 vibe-craft_web。

脚本会依次：环境检查 → 校验配置 → 拉取代码 → 构建镜像 → 启动服务 → 轮询健康检查。
USAGE
        exit 0
        ;;
    "") ;;
    *) echo "未知参数：$1（可用 --help 查看用法）" >&2; exit 1 ;;
esac

log() { printf '\033[32m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 1. 环境检查 ----------

command -v docker >/dev/null 2>&1 ||
    die '未检测到 docker，请先安装 Docker Engine 与 compose 插件'
docker compose version >/dev/null 2>&1 ||
    die 'docker 已安装但缺少 compose 插件（docker compose version 执行失败）'
docker info >/dev/null 2>&1 ||
    die '当前用户无权访问 Docker：请用 sudo 运行，或把用户加入 docker 组后重新登录'

# ---------- 2. 校验应用配置 ----------

if [ ! -f .env ]; then
    die '缺少 .env：请先运行 python setup_gateway.py 生成凭据，或参考 config.live.example.json 与 .env.example 手写。注意 config.json 需为 mode: live 并填好路由/模型 ID。'
fi
if [ ! -f config.json ]; then
    die '缺少 config.json：请以 config.live.example.json 为模板创建，需为 mode: live 并填好路由/上游真实模型 ID。'
fi
grep -qE '"mode"[[:space:]]*:[[:space:]]*"live"' config.json ||
    warn 'config.json 的 mode 不是 live：当前仍为演示模式，不会调用真实模型'

# ---------- 3. 更新代码 ----------

if git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    log '拉取最新代码'
    git -C "$REPO_ROOT" pull --ff-only
else
    warn '当前目录不是 git 仓库，跳过代码更新'
fi

# ---------- 4. 构建并启动 ----------

log '构建网关镜像'
docker compose build --pull gateway

log '启动服务'
docker compose up -d --remove-orphans

log '等待网关就绪'
HEALTHY=0
for _ in $(seq 1 30); do
    if docker compose exec -T gateway python -c \
        "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8020/health', timeout=3).status == 200 else 1)" \
        >/dev/null 2>&1; then
        HEALTHY=1
        break
    fi
    sleep 2
done
if [ "$HEALTHY" != 1 ]; then
    docker compose logs --tail 50 gateway
    die '网关未在 60 秒内通过健康检查，已输出最近日志'
fi

# ---------- 5. 结果与运维提示 ----------

cat <<EOF

部署完成。网关不对宿主机暴露端口，仅加入内网 vibe-craft_web。

常用命令（在本目录 $REPO_ROOT 下执行）：
  查看日志       docker compose logs -f gateway
  重启服务       docker compose restart gateway
  停止服务       docker compose down
  备份数据       tar czf ~/agent-gateway-data-\$(date +%F).tar.gz data

注意：
  - 本栈依赖主栈创建的外部网络 vibe-craft_web：请先在主应用目录执行 docker compose up -d，否则 deploy 会因找不到网络而失败。
  - gateway 服务名即内网 DNS 名，主应用通过 http://gateway:8020 访问。
  - config.json 以只读方式挂载，修改后需 docker compose restart gateway 生效。
  - data/ 保存 gateway.db，是唯一持久化位置，升级时不要删除。
EOF
