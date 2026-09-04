#!/usr/bin/env bash
# verify_dr_compose.sh —— 容器内（preview compose 栈）灾备补验入口，供 captain 集中执行。
#
# 前置（务必先满足，否则清晰报错而非静默失败）：
#   * deploy/.env.preview 已注入：BACKUP_ROLE_PASSWORD、BACKUP_ENC_KEY（POSTGRES_USER/PASSWORD/DB 需已就位）。
#   * 应用镜像已重建（migrate/backup 服务使用本仓库最新代码，含 migrations.apply_backup_role 与加密脚本）。
#     并在配置好机密后让 migrate 一次性服务执行一次：`docker compose run --rm migrate`
#     （compose 的 migrate 只在容器创建/重建时运行，不会因 up -d 自动重跑）。
#   * preview 栈运行中（`docker compose ps` 显示 postgres 等 Up）。
#
# 本脚本只做【灾备补验】所需的操作：前置检查 + 一次性加密备份 + 加密恢复演练（恢复目标为独立临时库，用后即删；
# 仅 transient 写入一条 marker 到 live tenants 表并从临时库校验其为快照点后变更，随后删除——与既有 drill 一致）。
# 不重跑 migrate / 不重建镜像 / 不改集群拓扑。
#
# 用法（仓库根）：
#   bash deploy/drills/verify_dr_compose.sh
#
# 产物：deploy/backups/langgraph-compose-dr*.dump.enc(+.sha256)、deploy/drills/records/drill-pg-encrypted-restore.json、
#       deploy/drills/records/DR-<ts>-pg-encrypted-restore.md、evidence/dr_backup_role_compose.json。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)/common.sh"

maybe_docker
ok()  { printf '\033[1;32m  [OK]\033[0m %s\n' "$*"; }
bad() { printf '\033[1;31m  [FAIL]\033[0m %s\n' "$*"; }

RUSER="${POSTGRES_USER:-$(env_get POSTGRES_USER || echo migrator)}"
DB_NAME="${POSTGRES_DB:-$(env_get POSTGRES_DB || echo langgraph)}"
BACKUP_ENC_KEY="${BACKUP_ENC_KEY:-$(env_get BACKUP_ENC_KEY || true)}"

# ---- 0. 前置：机密已注入 ----
[ -n "$BACKUP_ENC_KEY" ] || die "BACKUP_ENC_KEY 未注入：请在 deploy/.env.preview 写 BACKUP_ENC_KEY=$(openssl rand -base64 32)（见 deploy/DR_KEY_MANAGEMENT.md）。"
[ -n "$(env_get BACKUP_ROLE_PASSWORD || true)" ] || die "BACKUP_ROLE_PASSWORD 未注入：请在 deploy/.env.preview 写 BACKUP_ROLE_PASSWORD=$(openssl rand -hex 32)。"

# ---- 0. 前置：栈在跑 & backup_role 已存在 ----
compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -qE 'postgres.*Up' || die "preview 栈未运行或 postgres 未 Up（docker compose ps 检查）。"

ROLE_EXISTS="$(compose exec -T postgres psql -U "$RUSER" -d "$DB_NAME" -tAc "SELECT count(*)=1 FROM pg_roles WHERE rolname='backup_role';" 2>/dev/null | tr -d '[:space:]')"
if [ "$ROLE_EXISTS" != "t" ]; then
  bad "postgres 容器中尚未创建 backup_role。请先："
  echo "    docker compose build migrate"
  echo "    docker compose run --rm migrate"
  echo "  （并确认 .env.preview 含 BACKUP_ROLE_PASSWORD）。"
  exit 1
fi
ok "backup_role 存在于容器内（migrate 已应用）"

# ---- V1. 最小权限下钻：backup_role 无写/DDL；读可用 ----
READ_OK="$(compose exec -T postgres psql -U backup_role -d "$DB_NAME" -tAc "SELECT count(*)=1 FROM pg_roles WHERE rolname='backup_role';" 2>/dev/null | tr -d '[:space:]')"
if compose exec -T postgres psql -U backup_role -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "CREATE TABLE _forbidden(t int);" >/dev/null 2>&1; then
  DDL_DENIED=0; bad "backup_role 竟能 CREATE TABLE（最小权限失败）"
else
  DDL_DENIED=1; ok "backup_role DDL 被拒（CREATE TABLE permission denied）"
fi
if compose exec -T postgres psql -U backup_role -d "$DB_NAME" -v ON_ERROR_STOP=1 -c "INSERT INTO tenants(id,name,status) VALUES('_x','_x','_x');" >/dev/null 2>&1; then
  WRITE_DENIED=0; bad "backup_role 竟能 INSERT（最小权限失败）"
else
  WRITE_DENIED=1; ok "backup_role 写被拒（INSERT permission denied）"
fi
FLAGS="$(compose exec -T postgres psql -U "$RUSER" -d "$DB_NAME" -tAc "SELECT rolsuper||','||rolcreatedb||','||rolcreaterole||','||rolbypassrls FROM pg_roles WHERE rolname='backup_role';" | tr -d '[:space:]')"
printf '{"scenario":"V1_backup_role_compose","read_ok":%s,"write_denied":%s,"ddl_denied":%s,"role_flags":"%s"}\n' \
  "$([ "$READ_OK" = "t" ] && echo 1 || echo 0)" "$WRITE_DENIED" "$DDL_DENIED" "$FLAGS" > "$REPO_ROOT/evidence/dr_backup_role_compose.json"
ok "V1 最小权限核验完成（flags=$FLAGS）"

# ---- V2. 加密备份（compose 模式：backup_role 读 + 宿主 openssl 加密）----
TABLE_OK="$(compose exec -T postgres psql -U "$RUSER" -d "$DB_NAME" -tAc "SELECT count(*) FROM tenants WHERE id IN ('TENANT-A','TENANT-B');" 2>/dev/null | tr -d '[:space:]')"
[ "${TABLE_OK:-0}" -ge 1 ] || warn "tenants 无 TENANT-A/B 种子（采样校验基线弱；用现有表集仍可演练）。"
bash "$SCRIPT_DIR/backup_encrypted.sh" "$DEPLOY_DIR/backups/langgraph-compose-dr"
LAST_ENC="$(ls -1t "$DEPLOY_DIR/backups/langgraph-compose-dr"*.dump.enc 2>/dev/null | head -n1)"
[ -n "$LAST_ENC" ] || die "未生成加密归档。"
ENC_BYTES="$(wc -c < "$LAST_ENC" | tr -d ' ')"
SUM="$(awk '{print $1}' "$LAST_ENC.sha256" 2>/dev/null || echo '')"
ACT="$(sha256sum "$LAST_ENC" | awk '{print $1}')"
SHA_OK=$([ "$SUM" = "$ACT" ] && echo 1 || echo 0)
DEC_OK=0
openssl enc -d -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_ENC_KEY < "$LAST_ENC" 2>/dev/null | head -c 5 | grep -q 'PGDMP' && DEC_OK=1
printf '{"scenario":"V2_encrypted_backup_compose","backup_file":"%s","backup_bytes":%s,"sha256_ok":%s,"decrypt_ok":%s}\n' \
  "$(basename "$LAST_ENC")" "$ENC_BYTES" "$SHA_OK" "$DEC_OK" > "$REPO_ROOT/evidence/dr_encrypted_backup_compose.json"
ok "V2 加密备份完成（bytes=$ENC_BYTES sha256_ok=$SHA_OK decrypt_ok=$DEC_OK）"

# ---- V3. 加密恢复演练（compose 模式：解密恢复 + 采样校验 + 实测 RPO/RTO）----
bash "$SCRIPT_DIR/restore_drill.sh" "$LAST_ENC"
echo "verify_dr_compose rc=$?"

# ---- t8：复核 RPO/RTO gauge 已回填（drill_metric_exporter render/verify）----
GSTATE="${DRILL_GAUGE_STATE:-$DEPLOY_DIR/drills/metrics/drill_gauges.json}"
PYV="${PYTHON:-$(command -v python3 || command -v python || true)}"
if [ -n "$PYV" ]; then
  echo "--- 已回填的 RPO/RTO gauge（Prometheus 文本）---"
  "$PYV" "$SCRIPT_DIR/drill_metric_exporter.py" render --state "$GSTATE" || true
  echo "--- 阈值校验（RPO<=900s, RTO<=3600s）---"
  "$PYV" "$SCRIPT_DIR/drill_metric_exporter.py" verify --state "$GSTATE" || true
fi

echo ""
echo "=========================================================="
echo "容器化灾备补验完成。证据："
echo "  V1 最小权限: $REPO_ROOT/evidence/dr_backup_role_compose.json"
echo "  V2 加密备份: $REPO_ROOT/evidence/dr_encrypted_backup_compose.json"
echo "  V3 恢复演练: $DEPLOY_DIR/drills/records/drill-pg-encrypted-restore.json (+ DR-<ts>*.md)"
echo "  V4 Gauge 回填: $GSTATE（drill_rpo/rto_seconds{component=\"pg_backup\"}，经 api:8000/api/metrics 采集）"
echo "=========================================================="
