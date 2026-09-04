# 备份/恢复演练报告（生产 · after-sales-prod）

> 演练编号：DBR-20260904205910 · 目标环境：`production`（postgres 容器 `after-sales-prod-postgres-1`，库 `after_sales_prod`）
> 执行人/复核人：`captain(deploy-engineer)` / `security-auditor`
> 依据：实测命令 + `deploy/drills/records/drill-pg-encrypted-restore.json`

---

## 1. 备份机制（本轮修复）
- **原缺陷**：backup sidecar 原本直接用 `postgres:17-alpine`（缺 `openssl` CLI），`preview_backup_scheduler.sh` 的 `pg_dump | openssl enc` 在容器内失败 → `/backups/*.dump.enc` 全部 **0 字节 BACKUP_FAIL**，即自动备份链路完全不可用。
- **修复**：新建 `deploy/backup.Dockerfile`（基于 `postgres:17-alpine` 补装 `openssl`、`rsync`），构建 `after-sales-prod-backup` 镜像，`docker-compose.prod.yml` 的 `backup` 服务改用该镜像；重建 sidecar。
- **修复后验证**：自动备份产出 `langgraph-20260904125115.dump.enc`（**53584 字节**，日志 `BACKUP_OK`，SHA-256 已生成并落盘）。15 分钟周期（`BACKUP_INTERVAL_SECONDS=900`）的加密备份链路恢复。

## 2. 备份-恢复演练（手动，最小权限备份角色）
| # | 步骤 | 命令（摘要） | 结果 |
|---|------|------|------|
| B1 | 造备份点数据 | `INSERT tenants TENANT-A/B`（migrator） | `TENANT-A`/`TENANT-B` present |
| B2 | 加密备份点 | 容器内 `pg_dump -Fc "$BACKUP_DB_URL" \| openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_ENC_KEY` → `drill_bk.dump.enc` | **53888 字节**；SHA-256=`ca00c9a14be362…9dfa72` |
| D1 | 解密校验 | `openssl enc -d … -in drill_bk.dump.enc -out drill_bk.dump` | 头=`PGDMP`（有效 pg_dump 自定义格式） |
| D2 | 归档完整性 | `head -c 5` / `pg_restore --list` | `PGDMP` / 可解析 |
| R1 | 恢复到隔离临时库 | `CREATE DATABASE after_sales_restore_test` + `pg_restore -U migrator -d after_sales_restore_test /tmp/drill_bk.dump` | **RTO=2s**（≤3600s） |
| V1 | 备份点数据保留 | `SELECT count(*) FROM tenants WHERE id='TENANT-A/B'` | A=**1**、B=**1** ✓ |
| V2 | 备份点后变更排除 | 备份后插入 marker `pg-drill-*`（模拟灾害点后变更）再恢复 | marker in restore=**0** ✓（点恢复正确） |

### RPO / RTO 实测
- **RPO 实测 = 12.6s**（备份点 20:55:38 → marker 20:55:50）；**RPO 上界 = 900s**（15min 备份周期）→ **RPO ≤ 15min 达标**。
- **RTO 实测 = 2s**（`pg_restore` 到临时库耗时）；阈值 3600s → **RTO ≤ 60min 达标**。
- 角色口径：备份连接 = `backup_role`（LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE **BYPASSRLS**，仅 SELECT 只读，无任何 DML/DDL）；恢复连接 = `migrator`（owner）。备份**绝不用 owner/超级用户**。

## 3. 可解密 / 可校验 / 可恢复结论
- **可解密**：`openssl enc -d` 成功，明文头=`PGDMP`，归档有效。
- **可校验**：加密归档 SHA-256 生成并可复核；密文头部非明文（`encrypted` 探针通过）。
- **可恢复**：恢复到隔离临时库成功，备份点数据完整、点恢复排除备份后变更。
- **最小权限验证**：`backup_role` 仅授 CONNECT/USAGE/SELECT，无 INSERT/UPDATE/DELETE/DDL（`migrations.py apply_backup_role` 显式 REVOKE CREATE + 只授 SELECT）。
- **gauge 回填**：`drill_rpo_seconds{component="pg_backup"}=12.6`、`drill_rto_seconds{component="pg_backup"}=2`（`deploy/drills/metrics/drill_gauges.json`，由 api `/api/metrics` 合并采集，闭合 Prometheus `RpoExceeded`/`RtoExceeded` 规则）。

## 4. 清理
- 临时恢复库 `after_sales_restore_test` 已 `DROP`；marker 行已 `DELETE`；临时明文 `.dump` 已清理。

## 结论
- **达到 RPO≤15min / RTO≤60min：是**（实测 RPO=12.6s≤900s、RTO=2s≤3600s）。
- **备份可解密、可校验、可恢复：是**。
- 签名：`captain(deploy-engineer)` / `security-auditor`
