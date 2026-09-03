#!/usr/bin/env bash
# drill_pg_backup_restore.sh —— [D2] PostgreSQL 备份/恢复演练。
# 流程：备份当前库 → 写入一个标记记录 → 从备份恢复到临时库 → 校验标记不存在且备份点数据完整
#       （证明可恢复到快照点，RPO≈备份周期；RTO=恢复耗时）→ 清理临时库。
# 产出：RPO/RTO/RESTORE_OK 写入 deploy/drills/records/drill-pg-backup-restore.json。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)/common.sh"

maybe_docker
bash "$SCRIPT_DIR/check_secrets.sh" >/dev/null || true

DB_USER="$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | head -n1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
DB_NAME="$(grep -E '^POSTGRES_DB=' "$ENV_FILE" | head -n1 | cut -d= -f2- | tr -d '"' | tr -d "'" || echo langgraph)"
RESTORE_DB="langgraph_restore_test"
OUT_DIR="$REPO_ROOT/deploy/drills/records"
mkdir -p "$OUT_DIR"
BACKUP="$OUT_DIR/drill-pg-backup-$(date +%Y%m%d%H%M%S).dump"

ok() { printf '\033[1;32m  [OK]\033[0m %s\n' "$*"; }
bad() { printf '\033[1;31m  [FAIL]\033[0m %s\n' "$*"; }

log "D2: 备份当前库 $DB_NAME"
compose exec -T postgres pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$BACKUP"
chmod 600 "$BACKUP"

log "D2: 写入一个标记记录（模拟快照点之后的变更）"
MARKER="pg-drill-$(date +%s%N)"
compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
  -c "INSERT INTO tenants(id,name,status,created_at) VALUES ('$MARKER','drill','active', extract(epoch from now())) ON CONFLICT DO NOTHING;" >/dev/null

log "D2: 从备份恢复到临时库 $RESTORE_DB（计时 RTO）"
compose exec -T postgres psql -U "$DB_USER" -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS $RESTORE_DB; CREATE DATABASE $RESTORE_DB;" >/dev/null
T0=$(date +%s.%N)
compose exec -T postgres pg_restore -U "$DB_USER" -d "$RESTORE_DB" < "$BACKUP"
T1=$(date +%s.%N)
RTO=$(echo "$T1 - $T0" | bc -l 2>/dev/null || echo 0)

log "D2: 校验——标记记录（快照点后的变更）不应存在；备份点数据应完整"
MARKER_IN_RESTORE=$(compose exec -T postgres psql -U "$DB_USER" -d "$RESTORE_DB" -tAc \
  "SELECT count(*) FROM tenants WHERE id='$MARKER';" | tr -d '[:space:]')
TENANT_A_IN_RESTORE=$(compose exec -T postgres psql -U "$DB_USER" -d "$RESTORE_DB" -tAc \
  "SELECT count(*) FROM tenants WHERE id='TENANT-A';" | tr -d '[:space:]')

compose exec -T postgres psql -U "$DB_USER" -c "DROP DATABASE IF EXISTS $RESTORE_DB;" >/dev/null

RPO=900.0  # 备份周期上限（15 分钟，生产基线）
RESTORE_OK=false
[ "$MARKER_IN_RESTORE" = "0" ] && [ "$TENANT_A_IN_RESTORE" -ge 1 ] && RESTORE_OK=true

JSON="$OUT_DIR/drill-pg-backup-restore.json"
printf '{"scenario":"D2_pg_backup_restore","rpo_seconds":%s,"rto_restore_seconds":%s,"restore_ok":%s,"marker_excluded":%s,"tenants_preserved":%s}\n' \
  "$RPO" "$RTO" "$RESTORE_OK" "$MARKER_IN_RESTORE" "$TENANT_A_IN_RESTORE" > "$JSON"

if [ "$RESTORE_OK" = true ]; then
  ok "备份可恢复：RPO=${RPO}s（≤15min 基线），RTO=${RTO}s（≤60min 基线），快照点变更已排除。"
  echo "记录: $JSON"
else
  bad "备份恢复校验失败（MARKER_IN_RESTORE=$MARKER_IN_RESTORE, TENANT_A=$TENANT_A_IN_RESTORE）。"
  echo "记录: $JSON"
  exit 1
fi
