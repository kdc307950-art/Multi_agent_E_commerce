#!/usr/bin/env bash
# backup_encrypted.sh —— 加密 + 最小权限备份角色 + 可选异机副本 的 PostgreSQL 加密备份。
#
# 取代"同机 pg_dump 即完整灾备"的旧结论：本脚本产出【加密归档 + SHA-256 校验和】，
# 使用**最小权限备份角色 backup_role**（仅 CONNECT/SELECT，绝不用 owner/超级用户），并可把加密归档
# 推送到代码库之外的受控目录/异机（REMOTE_BACKUP_DIR / scp / rsync），按 BACKUP_KEEP 保留。
#
# 安全红线：
#   * 对称加密密钥只经环境变量 BACKUP_ENC_KEY 注入，绝不硬编码 / 打印 / 写日志；
#   * 备份连接绝不用 owner/超级用户（只用 backup_role）；
#   * 落盘为 .dump.enc（密文）+ .dump.enc.sha256（校验和），明文归档不在磁盘停留。
#
# 连接模式：
#   1) 直接 DSN 模式（宿主可直连数据库；用于备份主机 / 本地验证 / WSL）：
#        BACKUP_DB_URL='postgresql://backup_role:...@host:5432/db' BACKUP_ENC_KEY='<...>' \
#          bash deploy/scripts/backup_encrypted.sh [out-base]
#   2) compose 模式（部署机经 compose exec 进入 postgres 容器执行 pg_dump，密钥仍走宿主 openssl）：
#        BACKUP_ENC_KEY='<...>' bash deploy/scripts/backup_encrypted.sh [out-base]
#
# 异机 / 保留（可选，全部受控）：
#   REMOTE_BACKUP_DIR   — 代码库之外的受控归档目录（绝对路径；cp 本地归档 + 保留）
#   REMOTE_BACKUP_HOST  — 异机 SSH 主机；REMOTE_BACKUP_PATH 目标目录（配 scp/rsync 推送）
#   REMOTE_BACKUP_USER / REMOTE_BACKUP_SSH_KEY — scp/rsync 的登录用户与私钥（私钥不必注入明文）
#   BACKUP_KEEP         — 保留份数（缺省 8）；本地与远端目录都执行保留裁剪
#
# 用法示例：BACKUP_ENC_KEY="$(openssl rand -base64 32)" bash deploy/scripts/backup_encrypted.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# ---- 对称加密密钥（只经环境变量注入，绝不硬编码/打印/写日志）----
BACKUP_ENC_KEY="${BACKUP_ENC_KEY:-}"
[ -n "$BACKUP_ENC_KEY" ] || die "缺少 BACKUP_ENC_KEY（对称加密密钥，须由环境变量注入，绝不硬编码/入日志）。"
CIPHER="${BACKUP_CIPHER:-aes-256-cbc}"

# ---- 最小权限备份角色（仅 SELECT，绝不用 owner/超级用户）----
BACKUP_USER="${BACKUP_ROLE_USER:-backup_role}"
DB_NAME="${POSTGRES_DB:-$(env_get POSTGRES_DB || true)}"; [ -n "$DB_NAME" ] || DB_NAME="langgraph"

mkdir -p "$DEPLOY_DIR/backups"
OUT_BASE="${1:-$DEPLOY_DIR/backups/langgraph-$(date +%Y%m%d%H%M%S)}"
ENC="$OUT_BASE.dump.enc"
SHA="$OUT_BASE.dump.enc.sha256"
PARTS_CSV="$OUT_BASE.manifest.csv"   # 元数据：文件名,大小,sha256（归档说明用）

# ---- 判定 pg_dump 来源（直接 DSN 模式 vs compose 模式）----
if [ -n "${BACKUP_DB_URL:-}" ]; then
  log "连接模式：直接 DSN（${BACKUP_DB_URL%%@*}@***）"   # 直接 DSN 无需 Docker
  dump_cmd() { pg_dump "$BACKUP_DB_URL" -Fc; }
else
  maybe_docker
  log "连接模式：compose（postgres 容器，备份用户 $BACKUP_USER）"
  BPW="${BACKUP_ROLE_PASSWORD:-$(env_get BACKUP_ROLE_PASSWORD || true)}"
  [ -n "$BPW" ] || die "缺少 BACKUP_ROLE_PASSWORD（备份角色口令；compose 模式必需，由环境变量注入）。"
  # 经 compose exec 注入 PGPASSWORD 到 postgres 容器进程环境，用 backup_role（只读）连接。
  dump_cmd() {
    compose exec -T -e "PGPASSWORD=$BPW" postgres pg_dump -U "$BACKUP_USER" -Fc "$DB_NAME"
  }
fi

log "用最小权限备份角色 $BACKUP_USER 做 pg_dump -Fc，并加密（$CIPHER / PBKDF2，salt 随机）→ $ENC"
T0="$(date +%s.%N)"
# pg_dump 明文经管道直接进入 openssl，明文归档不落盘。
dump_cmd | openssl enc -"$CIPHER" -pbkdf2 -salt -pass env:BACKUP_ENC_KEY > "$ENC"
T1="$(date +%s.%N)"
chmod 600 "$ENC"

BSIZE="$(wc -c < "$ENC" | tr -d ' ')"
SUM="$(sha256sum "$ENC" | cut -d' ' -f1)"
printf '%s  %s\n' "$SUM" "$(basename "$ENC")" > "$SHA"
chmod 600 "$SHA"
printf 'file,bytes,sha256\n%s,%s,%s\n' "$(basename "$ENC")" "$BSIZE" "$SUM" > "$PARTS_CSV"
BK_TIME="$(echo "$T1 - $T0" | bc -l 2>/dev/null || echo 0)"

log "加密备份完成：$ENC（bytes=$BSIZE）耗时 ${BK_TIME}s"
log "SHA-256：$SUM（已写入 $SHA）"

# ---- 校验：密文可解（解密后能读到 pg_dump 头），且密文不等于明文 ----
if openssl enc -d -"$CIPHER" -pbkdf2 -salt -pass env:BACKUP_ENC_KEY < "$ENC" 2>/dev/null \
     | head -c 5 | grep -q 'PGDMP'; then
  ok_enc=1
else
  ok_enc=0
fi
# 明文探针：密文不应含明文 pg_dump 头（说明确实加密）。
case "$(head -c 5 "$ENC" 2>/dev/null)" in
  PGDMP) probe=0 ;;
  *) probe=1 ;;
esac
[ "$probe" -eq 1 ] || warn "警告：密文头部仍为 PGDMP，疑似未加密（请检查 BACKUP_CIPHER）。"
log "密文可解验证：ok_enc=$ok_enc；密文非明文校验：encrypted=$probe"

# ---- 异机副本（受控路径）----
KEEP="${BACKUP_KEEP:-8}"
push_offsite() {
  local d="$1"
  [ -d "$d" ] || mkdir -p "$d"
  cp -p "$ENC" "$SHA" "$PARTS_CSV" "$d/"
  # 多行 cp 的 chmod
  chmod 600 "$d/$(basename "$ENC")" "$d/$(basename "$SHA")" "$d/$(basename "$PARTS_CSV")" 2>/dev/null || true
  # 保留最近 KEEP 份（按 .enc 排序）
  ls -1t "$d"/*.dump.enc 2>/dev/null | tail -n +$((KEEP+1)) | while read -r f; do
    rm -f "$f" "${f%.dump.enc}.dump.enc.sha256" "${f%.dump.enc}.manifest.csv"
    log "远端保留裁剪：删除 $f"
  done
  log "异机副本已推送到受控目录：$d（保留 $KEEP 份）"
}

if [ -n "${REMOTE_BACKUP_DIR:-}" ]; then
  push_offsite "$REMOTE_BACKUP_DIR"
fi

if [ -n "${REMOTE_BACKUP_HOST:-}" ] && [ -n "${REMOTE_BACKUP_PATH:-}" ]; then
  RU="${REMOTE_BACKUP_USER:-root}"
  log "异机副本：scp 到 $RU@$REMOTE_BACKUP_HOST:$REMOTE_BACKUP_PATH"
  SCP_ID="${REMOTE_BACKUP_SSH_KEY:+-i $REMOTE_BACKUP_SSH_KEY}"
  scp $SCP_ID "$ENC" "$SHA" "$PARTS_CSV" "$RU@$REMOTE_BACKUP_HOST:$REMOTE_BACKUP_PATH/"
  if [ -n "${REMOTE_BACKUP_KEEP:-}" ]; then
    ssh $SCP_ID "$RU@$REMOTE_BACKUP_HOST" "
      cd '$REMOTE_BACKUP_PATH' || exit 1
      ls -1t ./*.dump.enc | tail -n +$((REMOTE_BACKUP_KEEP+1)) | while read -r f; do rm -f \"\$f\" \"\${f%.dump.enc}.dump.enc.sha256\"; done
    "
  fi
  log "异机副本（scp）完成。"
fi

if [ -n "${REMOTE_RSYNC_DEST:-}" ]; then
  rlog="rsync 到 $REMOTE_RSYNC_DEST"
  # rsync 只做增量同步，校验和由附带的 .sha256 保证；--copy-dest 可选。
  RSYNC_KEY=(); [ -n "${REMOTE_BACKUP_SSH_KEY:-}" ] && RSYNC_KEY=(-e "ssh -i $REMOTE_BACKUP_SSH_KEY")
  rsync -a "${RSYNC_KEY[@]}" "$ENC" "$SHA" "$PARTS_CSV" "$REMOTE_RSYNC_DEST/" || warn "$rlog失败（继续，本地归档已保留）。"
  log "$rlog 完成（本地归档已保留）。"
fi

# ---- 本地保留裁剪 ----
ls -1t "$DEPLOY_DIR/backups"/*.dump.enc 2>/dev/null | tail -n +$((KEEP+1)) | while read -r f; do
  rm -f "$f" "${f%.dump.enc}.dump.enc.sha256" "${f%.dump.enc}.manifest.csv"
  log "本地保留裁剪：删除 $f"
done

# ---- t8：回填 RPO gauge（备份周期上界；RTO 由恢复演练回填，此处不覆盖）----
# 闭合 alert-rules.yml 的 RpoExceeded（drill_rpo_seconds{component="pg_backup"}>900）。
# 若 BACKUP_INTERVAL_SECONDS 配置 > 900，该规则将触发，表达"备份周期超标"。
dr_backfill_gauges "${BACKUP_INTERVAL_SECONDS:-900}" "" "$(basename "$ENC")"

log "加密备份完成。结论：加密=$([ "$ok_enc" -eq 1 ] && echo 已加密 || echo 待复核)，校验和已生成，异机=$([ -n "${REMOTE_BACKUP_DIR:-}${REMOTE_BACKUP_HOST:-}${REMOTE_RSYNC_DEST:-}" ] && echo 已推送 || echo 未配置)。"
log "归档：$ENC"
log "校验和：$SHA"
