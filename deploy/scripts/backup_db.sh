#!/usr/bin/env bash
# backup_db.sh —— 备份 PostgreSQL（RPO 路径）。默认输出到 deploy/backups/。
# 用法：bash deploy/scripts/backup_db.sh [backup-file]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

maybe_docker
bash "$SCRIPT_DIR/check_secrets.sh"

DB_USER="$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | head -n1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
DB_NAME="$(grep -E '^POSTGRES_DB=' "$ENV_FILE" | head -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || echo langgraph)"
mkdir -p "$DEPLOY_DIR/backups"
OUT="${1:-$DEPLOY_DIR/backups/langgraph-$(date +%Y%m%d%H%M%S).dump}"

log "备份数据库 $DB_NAME（用户 $DB_USER）到 $OUT"
compose exec -T postgres pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$OUT"
chmod 600 "$OUT"
log "备份完成：$OUT"
