#!/bin/sh
# preview_backup_scheduler.sh —— 每 BACKUP_INTERVAL_SECONDS(默认 900s/15min) 对 preview langgraph 库做
# **加密** pg_dump -Fc（最小权限 backup_role）到 /backups（持久卷），附 SHA-256 校验和，可选推送异机。
# 保留最近 BACKUP_KEEP(默认 8) 份。日志走 stdout 供 docker logs 查询。
# 作用：把 RPO 上界真实绑定到 <=15min（备份周期），并满足「加密备份 + 最小权限备份角色」红线。
# 由 docker-compose.preview.yml 的 backup 服务挂载为 /app/backup.sh 并执行。
# 安全：对称加密密钥只经环境变量 BACKUP_ENC_KEY 注入，绝不打印/写日志；绝不用 owner/超级用户连接。
set -u
mkdir -p /backups
INTERVAL="${BACKUP_INTERVAL_SECONDS:-900}"
KEEP="${BACKUP_KEEP:-8}"
CIPHER="${BACKUP_CIPHER:-aes-256-cbc}"
[ -z "${BACKUP_DB_URL:-}" ] && { echo "$(date -Is) BACKUP_FAIL no BACKUP_DB_URL"; exit 1; }
[ -z "${BACKUP_ENC_KEY:-}" ] && { echo "$(date -Is) BACKUP_FAIL no BACKUP_ENC_KEY"; exit 1; }

mask() { echo "$BACKUP_DB_URL" | sed -E 's#(://[^:]+:)[^@]+@#\1***@#'; }
echo "$(date -Is) encrypted backup scheduler starting: interval=${INTERVAL}s keep=${KEEP} cipher=${CIPHER} url_host=$(mask)"

# 保留最近 KEEP 份（.enc）；联动删除同名 .sha256。作用在指定目录。
prune() {
  dir="$1"
  ls -1t "$dir"/*.dump.enc 2>/dev/null | tail -n +$((KEEP+1)) | while read -r f; do
    rm -f "$f" "${f%.dump.enc}.dump.enc.sha256" "${f%.dump.enc}.manifest.csv"
    echo "$(date -Is) prune $f"
  done
}

while true; do
  ts=$(date +%Y%m%d%H%M%S)
  enc="/backups/langgraph-${ts}.dump.enc"
  sha="${enc}.sha256"
  man="${enc%.dump.enc}.manifest.csv"
  # pg_dump 明文经管道直达 openssl，明文归档不落盘；用最小权限 backup_role 只读导出。
  if pg_dump -Fc "$BACKUP_DB_URL" 2>/tmp/pgbk.err | openssl enc -"$CIPHER" -pbkdf2 -salt -pass env:BACKUP_ENC_KEY > "$enc"; then
    if [ -s "$enc" ]; then
      sum="$(sha256sum "$enc" | awk '{print $1}')"
      printf '%s  %s\n' "$sum" "$(basename "$enc")" > "$sha"
      printf 'file,bytes,sha256\n%s,%s,%s\n' "$(basename "$enc")" "$(wc -c < "$enc" | tr -d ' ')" "$sum" > "$man"
      echo "$(date -Is) BACKUP_OK $enc sha256=$sum bytes=$(wc -c < "$enc" | tr -d ' ')"
      prune /backups
      # 异机副本：受控目录（/offsite 挂载）或 rsync 推送
      if [ -n "${BACKUP_REMOTE_DIR:-}" ]; then
        mkdir -p "$BACKUP_REMOTE_DIR"
        cp -p "$enc" "$sha" "$man" "$BACKUP_REMOTE_DIR/" && chmod 600 "$BACKUP_REMOTE_DIR"/*.dump.enc 2>/dev/null || true
        prune "$BACKUP_REMOTE_DIR"
        echo "$(date -Is) OFF_SITE_DIR_OK $BACKUP_REMOTE_DIR"
      fi
      if [ -n "${BACKUP_RSYNC_DEST:-}" ]; then
        if rsync -a "$enc" "$sha" "$man" "$BACKUP_RSYNC_DEST/" 2>/tmp/rsync.err; then
          echo "$(date -Is) OFF_SITE_RSYNC_OK $BACKUP_RSYNC_DEST"
        else
          echo "$(date -Is) OFF_SITE_RSYNC_FAIL"; sed 's/^/    /' /tmp/rsync.err | tail -5
        fi
      fi
    else
      echo "$(date -Is) BACKUP_FAIL empty_enc $enc"
    fi
  else
    echo "$(date -Is) BACKUP_FAIL"
    sed 's/^/    /' /tmp/pgbk.err | tail -20
  fi
  sleep "$INTERVAL"
done
