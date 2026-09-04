#!/usr/bin/env bash
# backup_db.sh —— 备份 PostgreSQL（RPO 路径）。**DR 基线已升级为加密备份**。
#
# 本脚本是 `backup_encrypted.sh` 的薄封装：默认产出【加密归档(.dump.enc) + SHA-256 校验和】，
# 使用**最小权限备份角色 backup_role**（仅只读，绝不用 owner/超级用户），并按需推送异机/保留。
# 保留旧签名 `backup_db.sh [out-base]` 以兼容 rollback.sh 与 OPS_RUNBOOK 的调用。
#
# 安全红线：加密密钥只经环境变量 BACKUP_ENC_KEY 注入，绝不硬编码/打印/写日志。
# 用法：
#   BACKUP_ENC_KEY='<...>' bash deploy/scripts/backup_db.sh [out-base]        # 直接 DSN/compose
#   BACKUP_DB_URL='postgresql://backup_role:...@host:5432/db' BACKUP_ENC_KEY='<...>' \
#     bash deploy/scripts/backup_db.sh [out-base]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

maybe_docker
bash "$SCRIPT_DIR/check_secrets.sh" >/dev/null 2>&1 || true   # 加密备份所需机密由 backup_encrypted 校验

# 若提供了加密备份必要条件，则走加密基线；否则（本地/测试无密钥/无直连）回退明文 pg_dump 并显式警告。
if [ -n "${BACKUP_ENC_KEY:-}" ] || [ -n "${BACKUP_DB_URL:-}" ]; then
  exec bash "$SCRIPT_DIR/backup_encrypted.sh" "${1:-}"
else
  warn "未提供 BACKUP_ENC_KEY / BACKUP_DB_URL：退化为**明文** pg_dump（仅本地开发/测试，**不满足 DR 加密备份基线**）。"
  DB_USER="$(env_get POSTGRES_USER || echo migrator)"
  DB_NAME="$(env_get POSTGRES_DB || echo langgraph)"
  mkdir -p "$DEPLOY_DIR/backups"
  OUT="${1:-$DEPLOY_DIR/backups/langgraph-$(date +%Y%m%d%H%M%S).dump}"
  log "备份数据库 $DB_NAME（用户 $DB_USER，明文，非 DR 基线）到 $OUT"
  compose exec -T postgres pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$OUT"
  chmod 600 "$OUT"
  log "备份完成（明文，仅限本地开发/测试）：$OUT"
fi
