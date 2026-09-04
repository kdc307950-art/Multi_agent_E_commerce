#!/bin/sh
# preview_backup_scheduler.sh —— 每 BACKUP_INTERVAL_SECONDS(默认 900s/15min) 对 preview langgraph 库做 pg_dump -Fc
# 到 /backups（持久卷）。保留最近 BACKUP_KEEP(默认 8) 份。日志走 stdout 供 docker logs 查询。
# 作用：把 RPO 上界真实绑定到 <=15min（备份周期），供 D2 备份恢复演练界定"备份点后变更被排除"。
# 由 docker-compose.preview.yml 的 backup 服务挂载为 /app/backup.sh 并执行（镜像使用 postgres:17-alpine 自带 pg_dump）。
set -u
mkdir -p /backups
INTERVAL="${BACKUP_INTERVAL_SECONDS:-900}"
KEEP="${BACKUP_KEEP:-8}"
[ -z "${BACKUP_DB_URL:-}" ] && { echo "$(date -Is) BACKUP_FAIL no BACKUP_DB_URL"; exit 1; }

echo "$(date -Is) backup scheduler starting: interval=${INTERVAL}s keep=${KEEP} url_host=$(echo "$BACKUP_DB_URL" | sed -E 's#(://[^:]+:)[^@]+@#\1***@#')"
while true; do
  ts=$(date +%Y%m%d%H%M%S)
  out="/backups/langgraph-${ts}.dump"
  if pg_dump -Fc "$BACKUP_DB_URL" -f "$out" 2>/tmp/pgbk.err; then
    echo "$(date -Is) BACKUP_OK $out ($(du -h "$out" 2>/dev/null | cut -f1))"
    # 保留最近 KEEP 份，删除更旧的
    ls -1t /backups/*.dump 2>/dev/null | tail -n +$((KEEP+1)) | while read -r f; do rm -f "$f"; done
  else
    echo "$(date -Is) BACKUP_FAIL"
    sed 's/^/    /' /tmp/pgbk.err | tail -20
  fi
  sleep "$INTERVAL"
done
