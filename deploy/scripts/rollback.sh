#!/usr/bin/env bash
# rollback.sh —— 回滚 preview（数据库恢复 + 应用重建）。
# 安全优先：先讲清回滚边界。数据库向前迁移本身幂等（CREATE IF NOT EXISTS / 幂等函数），
# 真正的"回滚"是：① 从变更前备份恢复数据（RPO）；② 应用重建到变更前 git 引用（版本回退）。
# 不提供"部分回滚到某个中间 schema"，与生产基线"先备份再变更、恢复靠快照"一致。
#
# 用法：bash deploy/scripts/rollback.sh <backup-file> [git-ref]
#   backup-file 必填（如 deploy/backups/langgraph-20260903.dump）；git-ref 缺省=上一个部署引用。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

maybe_docker
bash "$SCRIPT_DIR/check_secrets.sh"

BACKUP="${1:?请指定要恢复的备份文件（deploy/backups/xxx.dump）}"
[ -f "$BACKUP" ] || die "备份文件不存在：$BACKUP"
REF="${2:-}"

DB_USER="$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | head -n1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
DB_NAME="$(grep -E '^POSTGRES_DB=' "$ENV_FILE" | head -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || echo langgraph)"

log "0. 回滚前快照当前状态（可逆性）"
compose exec -T postgres pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$DEPLOY_DIR/backups/pre-rollback-$(date +%Y%m%d%H%M%S).dump"

log "1. 恢复数据库（从 $BACKUP）"
compose exec -T postgres pg_restore -U "$DB_USER" --clean --if-exists -d "$DB_NAME" < "$BACKUP"

if [ -n "$REF" ]; then
  log "2. 重建应用到 git 引用 $REF"
  compose down
  git -C "$REPO_ROOT" checkout "$REF" -- . 2>/dev/null || warn "无法检出 $REF（请手工切换并重建）。"
  compose build --pull
  compose up -d
else
  log "2. 未提供 git-ref，跳过应用重建（仅恢复数据）。如需版本回退，传 git-ref。"
fi

log "回滚完成。请执行 deploy/scripts/healthcheck.sh 复验。"
