#!/usr/bin/env bash
# verify_dr_local.sh —— 本机（WSL 本地 preview PostgreSQL 集群，端口 PGPORT=55432）灾备能力小规模验证。
# 仅用于本地验证/证据采集，不触真实生产。验证项：
#   V1 最小权限备份角色 backup_role（仅 CONNECT/SELECT，无写/DDL）
#   V2 加密备份（backup_encrypted.sh，aes-256-cbc + PBKDF2 + SHA-256）
#   V3 加密恢复演练（restore_drill.sh，解密恢复 + 采样校验 + 实测 RPO/RTO）
#   V4 异机副本（受控目录 REMOTE_BACKUP_DIR 推送 + 保留）
# 证据写入 deploy/drills/records/ 与 deploy/backups/（gitignore 已覆盖）。
#
# 用法（在 WSL 内、仓库根执行）：
#   PGPORT=55432 DR_MIGRATOR_PASSWORD=migpass BACKUP_ENC_KEY='<...>' \
#     bash deploy/drills/verify_dr_local.sh
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"        # deploy/
REPO="$(dirname "$BASE")"
PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-55432}"
MIG_PW="${DR_MIGRATOR_PASSWORD:-migpass}"
BACKUP_PW="$(openssl rand -hex 24)"
VERIFY_DB="langgraph_dr_verify"
MIG_URL="postgresql://migrator:${MIG_PW}@${PGHOST}:${PGPORT}/${VERIFY_DB}"
BACKUP_URL="postgresql://backup_role:${BACKUP_PW}@${PGHOST}:${PGPORT}/${VERIFY_DB}"
BACKUP_ENC_KEY="${BACKUP_ENC_KEY:-$(openssl rand -base64 32)}"

ADMIN="sudo -u postgres psql -p ${PGPORT}"
A="$ADMIN -qX -v ON_ERROR_STOP=1"
ok()  { printf '\n\033[1;32m  [OK]\033[0m %s\n' "$*"; }
bad() { printf '\033[1;31m  [FAIL]\033[0m %s\n' "$*"; }

# ---- 0. 集群就绪 & 准备独立的验证库 ----
pg_isready -h "$PGHOST" -p "$PGPORT" >/dev/null || { bad "PostgreSQL 未就绪（${PGHOST}:${PGPORT}）"; exit 1; }
ok "PostgreSQL 就绪（${PGHOST}:${PGPORT}）"
$A -c "DROP DATABASE IF EXISTS ${VERIFY_DB};"
$A -c "CREATE DATABASE ${VERIFY_DB};"
ok "准备独立验证库 ${VERIFY_DB}"

# ---- V1a. 复刻迁移 SQL：创建最小权限备份角色 backup_role + 授只读 ----
# 与 src/infrastructure/migrations.py apply_backup_role 一致（BYPASSRLS 以读出全量 RLS 数据 + 回收 public CREATE 兜底最小权限）。
SQ="
DROP ROLE IF EXISTS backup_role;
CREATE ROLE backup_role LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
ALTER ROLE backup_role PASSWORD '${BACKUP_PW}';
GRANT CONNECT ON DATABASE ${VERIFY_DB} TO backup_role;
GRANT USAGE ON SCHEMA public TO backup_role;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM backup_role;
REVOKE CREATE ON DATABASE ${VERIFY_DB} FROM backup_role;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_role;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO backup_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO backup_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO backup_role;
"
PGPASSWORD="$MIG_PW" psql -h "$PGHOST" -p "$PGPORT" -U migrator -d "$VERIFY_DB" -qX -v ON_ERROR_STOP=1 -c "$SQ"
ok "backup_role 已创建并授只读（复刻 apply_backup_role SQL）"

# ---- V1b. 建最小编业务 schema（owner=migrator）+ 种子 TENANT-A/B + RLS ----
PGPASSWORD="$MIG_PW" psql -h "$PGHOST" -p "$PGPORT" -U migrator -d "$VERIFY_DB" -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE tenants (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at DOUBLE PRECISION NOT NULL DEFAULT extract(epoch from now())
);
INSERT INTO tenants(id,name,status) VALUES ('TENANT-A','Alpha','active');
INSERT INTO tenants(id,name,status) VALUES ('TENANT-B','Beta','active');
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenants_scope ON tenants USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE, DELETE ON tenants TO app_runtime;
SQL
ok "业务表 tenants 已建（owner=migrator），种子 TENANT-A/B，RLS 就绪"

# ---- V1c. 最小权限核验：backup_role 无 DML/DDL；只读可用；写必须被拒 ----
PGPASSWORD="$BACKUP_PW" psql -h "$PGHOST" -p "$PGPORT" -U backup_role -d "$VERIFY_DB" -qX -tAc \
  "SELECT count(*) FROM tenants WHERE id IN ('TENANT-A','TENANT-B');" >/tmp/v1_read.txt
READ_OK=$([ "$(cat /tmp/v1_read.txt)" = "2" ] && echo 1 || echo 0)
FLAGS=$(PGPASSWORD="$BACKUP_PW" psql -h "$PGHOST" -p "$PGPORT" -U backup_role -d "$VERIFY_DB" -qX -tAc \
  "SELECT rolsuper||','||rolcreatedb||','||rolcreaterole||','||rolbypassrls FROM pg_roles WHERE rolname='backup_role';")
# 写（INSERT）必须 permission denied
if PGPASSWORD="$BACKUP_PW" psql -h "$PGHOST" -p "$PGPORT" -U backup_role -d "$VERIFY_DB" -qX \
     -c "INSERT INTO tenants(id,name,status) VALUES ('SHOULD_FAIL','x','active');" >/tmp/v1_write.txt 2>&1; then
  WRITE_DENIED=0
else
  WRITE_DENIED=1
fi
# DDL（CREATE TABLE）必须 permission denied
if PGPASSWORD="$BACKUP_PW" psql -h "$PGHOST" -p "$PGPORT" -U backup_role -d "$VERIFY_DB" -qX \
     -c "CREATE TABLE forbidden(t int);" >/tmp/v1_ddl.txt 2>&1; then
  DDL_DENIED=0
else
  DDL_DENIED=1
fi
printf '{"scenario":"V1_backup_role_least_privilege","read_ok":%s,"write_denied":%s,"ddl_denied":%s,"role_flags":"%s"}\n' \
  "$READ_OK" "$WRITE_DENIED" "$DDL_DENIED" "$FLAGS" > "$BASE/../evidence/dr_backup_role_least_privilege.json"
[ "$READ_OK" = 1 ] && ok "V1c backup_role 只读可用（读回 TENANT-A/B=2）" || bad "V1c 只读异常"
[ "$WRITE_DENIED" = 1 ] && ok "V1c backup_role 写被拒（INSERT permission denied）" || bad "V1c 写未被拒！"
[ "$DDL_DENIED" = 1 ] && ok "V1c backup_role DDL 被拒（CREATE TABLE permission denied）" || bad "V1c DDL 未被拒！"

# ---- V2. 加密备份（最小权限 backup_role + aes-256-cbc + SHA-256 + 异机） ----
OK_OFFSITE=0
REMOTE_BACKUP_DIR="$BASE/backups/offsite-test"
export BACKUP_DB_URL="$BACKUP_URL"
export BACKUP_ENC_KEY
export BACKUP_ROLE_PASSWORD="$BACKUP_PW"
export BACKUP_ROLE_USER=backup_role
export REMOTE_BACKUP_DIR
export BACKUP_KEEP=3
bash "$BASE/scripts/backup_encrypted.sh" "$BASE/backups/langgraph-dr-verify"
LAST_ENC="$(ls -1t "$BASE/backups/langgraph-dr-verify"*.dump.enc 2>/dev/null | head -n1)"
[ -n "$LAST_ENC" ] || { bad "未生成加密归档"; ls -la "$BASE/backups"; exit 1; }
ENC_BYTES="$(wc -c < "$LAST_ENC" | tr -d ' ')"
SHA_FILE="$LAST_ENC.sha256"
SUM="$(awk '{print $1}' "$SHA_FILE" 2>/dev/null || echo '')"
ACT="$(sha256sum "$LAST_ENC" | awk '{print $1}')"
SHA_OK=$([ "$SUM" = "$ACT" ] && echo 1 || echo 0)
# 密文不应以明文 pg_dump 头 PGDMP 开头
HEAD="$(head -c 5 "$LAST_ENC")"
ENC_NONPLAIN=$([ "$HEAD" != "PGDMP" ] && echo 1 || echo 0)
# 解密后应能得到 pg_dump 头（密钥正确）
DEC_OK=0
openssl enc -d -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_ENC_KEY < "$LAST_ENC" 2>/dev/null | head -c 5 | grep -q 'PGDMP' && DEC_OK=1
[ -f "$REMOTE_BACKUP_DIR/$(basename "$LAST_ENC")" ] && OK_OFFSITE=1
printf '{"scenario":"V2_encrypted_backup","backup_file":"%s","backup_bytes":%s,"sha256_ok":%s,"cipher_non_plain":%s,"decrypt_ok":%s,"offsite_copied":%s}\n' \
  "$(basename "$LAST_ENC")" "$ENC_BYTES" "$SHA_OK" "$ENC_NONPLAIN" "$DEC_OK" "$OK_OFFSITE" > "$BASE/../evidence/dr_encrypted_backup.json"
ok "V2 加密备份完成：$(basename "$LAST_ENC") bytes=$ENC_BYTES sha256_ok=$SHA_OK cipher_non_plain=$ENC_NONPLAIN decrypt_ok=$DEC_OK offsite=$OK_OFFSITE"

# ---- V3. 加密恢复演练（解密恢复 + 采样校验 + 实测 RPO/RTO） ----
export DR_RESTORE_DSN="$MIG_URL"
export DR_MARKER_TABLE=tenants
bash "$BASE/scripts/restore_drill.sh" "$LAST_ENC"
RESTORE_RC=$?
printf 'restore_drill_rc=%s\n' "$RESTORE_RC"

echo ""
echo "=========================================================="
echo "本地灾备能力验证完成。证据："
echo "  V1 最小权限备份角色: $BASE/../evidence/dr_backup_role_least_privilege.json"
echo "  V2 加密备份       : $BASE/../evidence/dr_encrypted_backup.json"
echo "  V3 恢复演练       : $BASE/drills/records/drill-pg-encrypted-restore.json"
echo "  归档+校验和        : $LAST_ENC (+ .sha256)"
echo "  异机副本          : $REMOTE_BACKUP_DIR/"
echo "=========================================================="
[ "$RESTORE_RC" -eq 0 ]
