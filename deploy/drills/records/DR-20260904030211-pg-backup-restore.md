# 预发布演练记录 — PostgreSQL 备份恢复（DR-20260904030211）

- 演练编号：DR-20260904030211
- 环境：preview（after-sales-preview，PostgreSQL 17-alpine，DB=langgraph，owner=migrator；运行角色 app_runtime 仅 DML）
- 应用 git 引用：251f430（branch main）
- 执行人 / 复核人：drill-runner / security-auditor
- 开始时间 / 结束时间：2026-09-04 03:02:11 / 2026-09-04 03:02:21（drill-pg-backup-restore.json timestamp）

| # | 演练项 | 操作号/证据ID | 结果 | 时长 | 备注 |
|---|--------|--------------|------|------|------|
| D2 | PostgreSQL 备份恢复 | backup=drill-pg-backup-20260904030211.dump | True | RTO=1.668s | RPO=900s=**15min 周期备份界定**（t12 已加 `backup` 服务每 900s `pg_dump -Fc`，保留 8，宿主持久 `./data/preview-backups`）；RTO 实测（Stopwatch）≤60min 达标 |

## 关键证据
- marker_excluded=0（应为 0）
- tenants_preserved(TENANT-A)=1（应 >=1）；tenants_preserved(TENANT-B)=1
- backup_file=drill-pg-backup-20260904030211.dump（backup_bytes=51908）；restore_db=langgraph_restore_test
- RPO_basis=**15min 定时备份周期界定**（t12）；RTO_restore_seconds=1.668；t12 恢复校验：marker_in_restore=0、TENANT_A_in_restore=1、restore_RTO=1.9s
- 周期备份服务（t12）：`docker-compose.preview.yml` `backup` 服务（`image=postgres:17-alpine`，`command=sh /app/backup.sh`，`BACKUP_INTERVAL_SECONDS=900`、`BACKUP_KEEP=8`，挂载 `deploy/scripts/preview_backup_scheduler.sh` + `./data/preview-backups:/backups`）；首份 `langgraph-20260903193257.dump`
- 结构化：deploy/drills/records/drill-pg-backup-restore.json + 20260904030652-drill-summary.json

## 结论（如实）
- 是否达到预发布验收：**是**（RTO 实测 1.668s ≤60min；RPO 上界=15min 周期备份界定（marker_in_restore=0、TENANT-A 保留、restore_RTO=1.9s）→ RPO≤15min 达标；数据完整）。**边界**：pg_dump 周期而非 PITR/WAL 归档（PITR/WAL 为更紧 RPO 的后续增强项）。
- 签名：drill-runner / security-auditor
