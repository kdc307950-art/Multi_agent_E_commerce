#!/usr/bin/env bash
# migrate.sh —— 仅执行数据库迁移（一次性服务），幂等可重复。
# 以迁移角色（owner）建业务表 + 官方 checkpoint 表 + RLS + 函数，并创建/更新运行角色 app_runtime。
# 用法：bash deploy/scripts/migrate.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

maybe_docker
bash "$SCRIPT_DIR/check_secrets.sh"

log "执行数据库迁移（迁移角色：$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | head -n1 | cut -d= -f2-)）"
compose run --rm migrate

log "迁移完成。可重复执行（幂等）。"
log "校验：运行角色可连接且 RLS 就绪 -> bash deploy/scripts/healthcheck.sh"
