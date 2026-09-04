# deploy/backup.Dockerfile —— 备份 sidecar 专用镜像。
#
# 背景：原 backup 服务直接用 postgres:17-alpine，但该镜像只有 pg_dump/sha256sum(需 busybox)、
# 没有 openssl CLI；而 preview_backup_scheduler.sh 用 `openssl enc` 做加密（pg_dump 明文管道），
# 导致 /backups 下全部 .dump.enc 都是 0 字节 BACKUP_FAIL。
#
# 本镜像在 postgres:17-alpine 基础上补充 openssl（加密）与 rsync（可选的异机推送），
# 使备份 sidecar 能产出「加密 + 附 SHA-256 校验和 + 可选异机副本」的有效归档。
# 运行时无需再访问任何外网（openssl/rsync 已打入镜像），满足完全自托管/无未经批准出网。
# 挂载与命令不变：由 compose 挂载 preview_backup_scheduler.sh 为 /app/backup.sh 并执行。
FROM docker.1ms.run/library/postgres:17-alpine
RUN apk add --no-cache openssl rsync
CMD ["sh"]
