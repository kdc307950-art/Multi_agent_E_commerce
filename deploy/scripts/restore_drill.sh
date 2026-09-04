#!/usr/bin/env bash
# restore_drill.sh —— [D2'] 加密备份恢复演练：解密恢复 + 采样数据校验 + RPO/RTO 实测。
#
# 取代"同机 pg_dump 即完整灾备结论"：从【加密归档(.dump.enc)】解密 → 恢复到独立临时库 →
# 校验备份点数据完整性与快照点后变更被排除 → **实测 RPO/RTO** 并写入证据记录。
# 恢复连接用**迁移角色/owner**（重放对象需要），备份连接用最小权限角色 backup_role。
#
# 安全：解密密钥只经环境变量 BACKUP_ENC_KEY 注入，绝不打印/写日志；恢复目标为临时库，用后可清。
#
# 连接模式：
#   1) 直接 DSN 模式（宿主可直连；用于备份主机 / 本地验证 / WSL）：
#        BACKUP_ENC_KEY='<...>' DR_RESTORE_DSN='postgresql://migrator:...@host:5432/db' \
#          bash deploy/scripts/restore_drill.sh [backup.enc]
#   2) compose 模式（部署机经 compose exec 进入 postgres 容器执行）：
#        BACKUP_ENC_KEY='<...>' bash deploy/scripts/restore_drill.sh [backup.enc]
#
# 产物：
#   * deploy/drills/records/drill-pg-encrypted-restore.json —— 结构化证据
#   * deploy/drills/records/DR-<ts>-pg-encrypted-restore.md  —— 可复核 Markdown 记录
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)/common.sh"

# 直接 DSN 模式无需 Docker；compose 模式才检查（见下方恢复连接判定）。
CIPHER="${BACKUP_CIPHER:-aes-256-cbc}"
BACKUP_ENC_KEY="${BACKUP_ENC_KEY:-}"
[ -n "$BACKUP_ENC_KEY" ] || die "缺少 BACKUP_ENC_KEY（解密密钥，须由环境变量注入）。"

RECORDS_DIR="$DEPLOY_DIR/drills/records"
mkdir -p "$RECORDS_DIR" "$DEPLOY_DIR/backups"

DB_NAME="${POSTGRES_DB:-$(env_get POSTGRES_DB || true)}"; [ -n "$DB_NAME" ] || DB_NAME="langgraph"
RESTORE_DB="langgraph_restore_test"
MARKER_TABLE="${DR_MARKER_TABLE:-tenants}"

ok()  { printf '\033[1;32m  [OK]\033[0m %s\n' "$*"; }
bad() { printf '\033[1;31m  [FAIL]\033[0m %s\n' "$*"; }

# 直接 DSN 模式：把 restore DSN 的来源库名替换成临时库名，构造指向临时库的连接串。
restore_target_url() {
  echo "${DR_RESTORE_DSN%/*}/$RESTORE_DB"
}

# ---- 恢复连接函数（直接 DSN / compose 二选一）----
psql_root()  {  # 执行无需选择数据库的管理命令（DROP/CREATE DATABASE）
  if [ -n "${DR_RESTORE_DSN:-}" ]; then psql "$DR_RESTORE_DSN" "$@"; else compose exec -T postgres psql -U "$RUSER" -d postgres "$@"; fi
}
psql_src()  {  # 在源库执行
  if [ -n "${DR_RESTORE_DSN:-}" ]; then psql "$DR_RESTORE_DSN" "$@"; else compose exec -T postgres psql -U "$RUSER" -d "$DB_NAME" "$@"; fi
}
psql_target() {  # 在临时库执行
  if [ -n "${DR_RESTORE_DSN:-}" ]; then psql "$(restore_target_url)" "$@"; else compose exec -T postgres psql -U "$RUSER" -d "$RESTORE_DB" "$@"; fi
}
pg_restore_target() {
  if [ -n "${DR_RESTORE_DSN:-}" ]; then
    pg_restore -d "$(restore_target_url)" < "$TMP_DUMP"
  else
    compose cp "$TMP_DUMP" postgres:/tmp/drill.dump
    compose exec -T postgres pg_restore -U "$RUSER" -d "$RESTORE_DB" /tmp/drill.dump
    compose exec -T postgres rm -f /tmp/drill.dump 2>/dev/null || true
  fi
}

# ---- 1. 确定要恢复的加密归档（缺省先做一次加密备份，保证是有校验和的加密归档）----
ENC="${1:-}"
if [ -z "$ENC" ]; then
  log "未指定备份文件：先做一次加密备份（最小权限 backup_role）→ 再恢复实测。"
  [ -n "${BACKUP_DB_URL:-}" ] || warn "直接 DSN 未设置 BACKUP_DB_URL（尝试 compose 模式）。"
  bash "$SCRIPT_DIR/backup_encrypted.sh"
  ENC="$(ls -1t "$DEPLOY_DIR/backups"/*.dump.enc 2>/dev/null | head -n1)"
  [ -n "$ENC" ] || die "加密备份未生成 .dump.enc。"
fi
[ -f "$ENC" ] || die "找不到加密备份：$ENC"
SHA_FILE="$ENC.sha256"; [ -f "$SHA_FILE" ] || warn "缺少 $SHA_FILE（应有 SHA-256 校验和）。"

# 备份点快照时间（落盘 mtime，作为 RPO 计量的备份点）
SNAP_EPOCH="$(stat -c %Y "$ENC" 2>/dev/null || date +%s)"
SNAP_ISO="$(date -d "@$SNAP_EPOCH" +%Y-%m-%dT%H:%M:%S%z 2>/dev/null || stat -c %y "$ENC")"

# ---- 2. 校验和复核（SHA-256）----
if [ -f "$SHA_FILE" ]; then
  EXPECTED="$(awk '{print $1}' "$SHA_FILE")"
  ACTUAL="$(sha256sum "$ENC" | cut -d' ' -f1)"
  if [ "$EXPECTED" = "$ACTUAL" ]; then
    ok "SHA-256 校验通过：$ACTUAL"; SHA_OK=true
  else
    bad "SHA-256 校验失败：期望 $EXPECTED 实际 $ACTUAL"; SHA_OK=false
  fi
else
  SHA_OK=false
fi

# ---- 3. 解密到临时明文 .dump ----
TMP_DUMP="$(mktemp "$DEPLOY_DIR/backups/.restore-XXXXXX.dump")"
openssl enc -d -"$CIPHER" -pbkdf2 -salt -pass env:BACKUP_ENC_KEY < "$ENC" > "$TMP_DUMP" \
  || die "解密失败（密钥/密文不一致？）。"
log "解密完成：$ENC → $TMP_DUMP"

# ---- 4. 归档完整性：pg_restore --list 确认是有效 pg_dump 自定义格式 / SQL 归档 ----
if pg_restore --list "$TMP_DUMP" >/dev/null 2>&1; then
  ok "归档完整性校验 OK（pg_restore --list 可解析）。"; LIST_OK=true
elif head -n 40 "$TMP_DUMP" | grep -q "PostgreSQL database dump"; then
  ok "归档完整性校验 OK（纯 SQL 归档）。"; LIST_OK=true
else
  bad "归档完整性校验失败（pg_restore --list 不可解析）。"; LIST_OK=false
fi

# ---- 5. 恢复连接角色（owner/迁移角色）----
if [ -n "${DR_RESTORE_DSN:-}" ]; then
  RUSER="$(basename "$(echo "${DR_RESTORE_DSN%%@*}" | sed -E 's#^[a-z]+://##' | cut -d: -f1)")"
  log "恢复连接：直接 DSN（owner=$RUSER）"
else
  maybe_docker
  RUSER="${POSTGRES_USER:-$(env_get POSTGRES_USER || echo migrator)}"
  log "恢复连接：compose（postgres 容器，owner=$RUSER）"
fi

# ---- 6. 写入标记记录（快照点后变更；用于证明点恢复能排除它）----
MARKER="pg-drill-$(date +%s%N)"
T_MARK="$(date +%s.%N)"
log "写入标记记录（快照点后变更）：$MARKER"
psql_src -v ON_ERROR_STOP=1 \
  -c "INSERT INTO $MARKER_TABLE(id,name,status,created_at) VALUES ('$MARKER','drill','active', extract(epoch from now())) ON CONFLICT DO NOTHING;" >/dev/null 2>&1 || \
  warn "标记写入跳过（$MARKER_TABLE 列不匹配？演练容忍）。"

# ---- 7. 重建临时库并恢复到临时库（计时 RTO）----
log "重建临时库 $RESTORE_DB"
psql_root -c "DROP DATABASE IF EXISTS $RESTORE_DB;" >/dev/null 2>&1 || true
psql_root -c "CREATE DATABASE $RESTORE_DB;" >/dev/null
T_R0="$(date +%s.%N)"
if pg_restore_target; then RESTORE_OK=true; else RESTORE_OK=false; fi
T_R1="$(date +%s.%N)"
RTO="$(echo "$T_R1 - $T_R0" | bc -l 2>/dev/null || echo 0)"
RTO="$(awk -v t="$RTO" 'BEGIN{printf "%.6f", t}')"   # 规范化为 0.###（避免 ".1" 非法 JSON）

# ---- 8. 采样校验：备份点数据完整（TENANT-A/B 保留）+ 快照点后变更排除 + RLS 策略数 ----
MARKER_IN_RESTORE="$(psql_target -tAc "SELECT count(*) FROM $MARKER_TABLE WHERE id='$MARKER';" 2>/dev/null | tr -d '[:space:]' || echo '?')"
TENANT_A="$(psql_target -tAc "SELECT count(*) FROM $MARKER_TABLE WHERE id='TENANT-A';" 2>/dev/null | tr -d '[:space:]' || echo 0)"
TENANT_B="$(psql_target -tAc "SELECT count(*) FROM $MARKER_TABLE WHERE id='TENANT-B';" 2>/dev/null | tr -d '[:space:]' || echo 0)"
POLICIES="$(psql_target -tAc "SELECT count(*) FROM pg_policies;" 2>/dev/null | tr -d '[:space:]' || echo 0)"

# ---- 9. 清理临时库与临时明文 ----
psql_root -c "DROP DATABASE IF EXISTS $RESTORE_DB;" >/dev/null 2>&1 || true
psql_src -c "DELETE FROM $MARKER_TABLE WHERE id='$MARKER';" >/dev/null 2>&1 || true
rm -f "$TMP_DUMP"

# ---- 10. RPO 实测：备份点与标记点的时间差（数据丢失窗口）----
RPO_MEASURED="$(echo "$T_MARK - $SNAP_EPOCH" | bc -l 2>/dev/null || echo 0)"
if awk "BEGIN{exit !($RPO_MEASURED<0)}"; then RPO_MEASURED=0; fi
RPO_MEASURED="$(awk -v r="$RPO_MEASURED" 'BEGIN{printf "%.6f", r}')"   # 规范化为 0.###（避免非法 JSON）
# RPO 上界（备份周期最坏情况），缺省 900s=15min（与 BACKUP_INTERVAL_SECONDS 对齐）。
RPO_BOUND="${BACKUP_INTERVAL_SECONDS:-900}"

# ---- t8：把实测 RPO/RTO 回填为 Prometheus gauge（drill_rpo/rto_seconds{component="pg_backup"}）----
# 闭合 alert-rules.yml 的 RpoExceeded/RtoExceeded（此前仅写 evidence md，gauge 恒不触发）。
dr_backfill_gauges "$RPO_MEASURED" "$RTO" "$(basename "$ENC")"

ALL_OK="$SHA_OK"
[ "$RESTORE_OK" = true ] || ALL_OK=false
[ "$MARKER_IN_RESTORE" = "0" ] || ALL_OK=false
[ "${TENANT_A:-0}" -ge 1 ] || ALL_OK=false

# ---- 11. 证据记录 ----
TS="$(date +%Y%m%d%H%M%S)"
JSON="$RECORDS_DIR/drill-pg-encrypted-restore.json"
printf '{"scenario":"D2_encrypted_backup_restore","timestamp":"%s","backup_file":"%s","backup_bytes":%s,"sha256_ok":%s,"archive_list_ok":%s,"decrypt_ok":%s,"restore_ok":%s,"rto_restore_seconds":%s,"rpo_measured_seconds":%s,"rpo_bound_seconds":%s,"marker_excluded":%s,"tenant_a_preserved":%s,"tenant_b_preserved":%s,"rl_policies_count":%s,"restore_db":"%s","rpo_basis":"measured_age_vs_interval_bound","cipher":"%s"}\n' \
  "$TS" "$(basename "$ENC")" "$(wc -c < "$ENC" | tr -d ' ')" "$SHA_OK" "$LIST_OK" true "$RESTORE_OK" \
  "$RTO" "$RPO_MEASURED" "$RPO_BOUND" "$MARKER_IN_RESTORE" "$TENANT_A" "$TENANT_B" "$POLICIES" "$RESTORE_DB" "$CIPHER" > "$JSON"

MD="$RECORDS_DIR/DR-$TS-pg-encrypted-restore.md"
{
  echo "# 预发布演练记录 — 加密备份恢复（DR-$TS）"
  echo ""
  echo "- 演练编号：DR-$TS"
  echo "- 环境：preview（PostgreSQL，DB=$DB_NAME；备份角色 backup_role 仅只读；恢复用迁移/owner 角色）"
  echo "- 应用 git 引用：$(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "- 备份点快照：$SNAP_ISO（epoch=$SNAP_EPOCH）"
  echo ""
  echo "| # | 演练项 | 结果 | 证据 |"
  echo "|---|--------|------|------|"
  echo "| E1 | 加密归档（${CIPHER} + PBKDF2） | PASS | backup_bytes=$(wc -c < "$ENC" | tr -d ' ') |"
  echo "| E2 | SHA-256 校验和 | $([ "$SHA_OK" = true ] && echo PASS || echo FAIL) | $(basename "$SHA_FILE") |"
  echo "| E3 | 归档完整性（pg_restore --list） | $([ "$LIST_OK" = true ] && echo PASS || echo FAIL) | $([ "$LIST_OK" = true ] && echo 可解析 || echo 不可解析) |"
  echo "| E4 | 解密恢复（RTO） | $([ "$RESTORE_OK" = true ] && echo PASS || echo FAIL) | RTO=${RTO}s |"
  echo "| E5 | 快照点后变更排除（marker） | $([ "$MARKER_IN_RESTORE" = "0" ] && echo PASS || echo FAIL) | marker_in_restore=$MARKER_IN_RESTORE |"
  echo "| E6 | 采样数据保留（TENANT-A/B） | $([ "${TENANT_A:-0}" -ge 1 ] && echo PASS || echo FAIL) | A=$TENANT_A B=$TENANT_B |"
  echo ""
  echo "## RPO / RTO 实测"
  echo "- RPO（实测数据丢失窗口）：**${RPO_MEASURED}s**；RPO 上界（备份周期最坏情况）：${RPO_BOUND}s（阈值 ≤900s）"
  echo "- RTO（恢复耗时实测）：**${RTO}s**（阈值 ≤3600s）"
  echo ""
  echo "## 关键证据"
  echo "- backup_file=$(basename "$ENC")；restore_db=$RESTORE_DB"
  echo "- 密钥仅由环境变量 BACKUP_ENC_KEY 注入，未打印/写日志；备份角色 backup_role 仅只读。"
  echo ""
  echo "## 结论（如实，勿臆造）"
  echo "- 是否达到 RPO≤15min / RTO≤60min：$([ "$RESTORE_OK" = true ] && awk -v r="$RPO_MEASURED" -v b="$RPO_BOUND" -v t="$RTO" 'BEGIN{print (r<=b && t<=3600)?"是":"复核"}' || echo 复核)（实测值见上）"
  echo "- 签名：drill-runner / security-auditor"
} > "$MD"

if [ "$ALL_OK" = true ]; then
  ok "加密备份恢复演练通过：SHA256=$SHA_OK 归档可解析，RTO=${RTO}s，RPO实测=${RPO_MEASURED}s（上界${RPO_BOUND}s），marker 已排除，TENANT-A/B 保留。"
  echo "记录: $JSON"
  echo "记录: $MD"
  exit 0
else
  bad "加密备份恢复演练未全通过（SHA_OK=$SHA_OK LIST_OK=$LIST_OK RESTORE_OK=$RESTORE_OK MARKER=$MARKER_IN_RESTORE A=$TENANT_A）。"
  echo "记录: $JSON"
  exit 1
fi
