#!/usr/bin/env bash
# common.sh —— preview 部署公共助手（由其它脚本 source）。
# 固定 Compose 文件与密钥文件路径，并提供幂等的 compose 调用封装与配色日志。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"                       # deploy/
REPO_ROOT="$(dirname "$DEPLOY_DIR")"                       # 仓库根

# compose 文件位于仓库根（与既有 docker-compose.yml 同级），相对路径（./deploy/...）以 compose 文件目录解析。
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/docker-compose.preview.yml}"
ENV_FILE="${ENV_FILE:-$DEPLOY_DIR/.env.preview}"

# @deploy 入口：docker compose --env-file <ENV_FILE> -f <COMPOSE_FILE> <args...>
compose() {
  "$DOCKER" compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

log()  { printf '\n\033[1;34m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

# 允许覆盖 docker 二进制（测试/CI）。
DOCKER="${DOCKER:-docker}"
maybe_docker() {
  command -v "$DOCKER" >/dev/null 2>&1 || die "未找到 Docker（$DOCKER）。请先安装并启动 Docker 引擎。"
}
