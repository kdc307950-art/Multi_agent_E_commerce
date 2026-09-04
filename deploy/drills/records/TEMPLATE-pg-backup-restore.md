# PostgreSQL 备份恢复演练记录（模板骨架）

> **填表说明**：模板/骨架，字段占位 `<...>`。RPO/RTO 测量与备份恢复结果由 **t3（演练脚本 + RPO/RTO 测量准备）**
> 及 **t6（DR 演练实跑）** 回填（`drill_pg_backup_restore.sh` + `run_preview_drills.sh`）。执行人/复核人应实名。
> 最终文件名约定：`deploy/drills/records/<ts>-pg-backup-restore.md`（`<ts>` 为演练时间戳）。
> 阈值（OPS_RUNBOOK §2）：**RPO ≤ 15 分钟，RTO ≤ 60 分钟**；任何一天恢复失败都应升级为值班事件并回滚相关变更。

---

## 0. 记录标识
- 演练编号：`DR-<YYYYMMDD-HHMM>`
- 环境：`preview`（PostgreSQL `17-alpine`，DB=`langgraph`，owner=`migrator`；运行角色 `app_runtime` 仅 DML）
- 执行人 / 复核人：`<t3 执行人 / 复核人>`（推荐 `drill-runner / security-auditor`）
- 开始时间 / 结束时间：`<...> / <...>`
- 应用 git 引用：`<t1 deploy.sh 记录的 ref>`（备份恢复针对该版本数据）

---

## 1. 备份（RPO 路径）
- 备份命令：`bash deploy/scripts/backup_db.sh deploy/backups/langgraph-<ts>.dump`
  （`compose exec -T postgres pg_dump -U <owner> -Fc <db> > <dump>`；`chmod 600`）
- 备份文件：`deploy/backups/langgraph-<ts>.dump`
- 备份耗时：`<...> s`（t3 测量）
- **RPO（数据恢复点目标）**：`<...> min` —— 阈值 ≤15min（实测；指备份点与故障点的数据丢失窗口）
- 备份是否成功：`<是/否>`（dump 完整性：`pg_restore --list` 校验，`<t3>`）

## 2. 恢复（RTO 路径）
- 恢复命令：`compose exec -T postgres pg_restore -U <owner> --clean --if-exists -d <db> < <dump>`（见 `rollback.sh`）
- 恢复耗时：`<...> s`（t3 测量）
- **RTO（恢复时间目标）**：`<...> min` —— 阈值 ≤60min
- 恢复是否成功：`<是/否>`

## 3. 校验结果（恢复后数据完整性）
| 校验项 | 结果 | 证据 | 回填来源 |
|--------|------|------|---------|
| 业务表行数对照（备份前 vs 恢复后） | `N → N`（一致/不一致） | `<...>` | t3/t6 |
| 官方检查点表（checkpoint/conversation）完整性 | `<一致/缺失>` | `<...>` | t6 |
| RLS 策略是否恢复 | `<是/否（策略数量）>` | `evidence/pg_rls_policies.json` | t2/t6 |
| 运行角色 `app_runtime` DML 权限 | `<是/否>` | `pg_acceptance_evidence.json` | t6 |
| 租户隔离（恢复后不串租户） | `<通过/不通过>` | `pg_checkpoint_recovery_record.json` | t2/t6 |
| 关键操作幂等（同 `operation_id` 不重复） | `<通过/不通过>` | `pg_acceptance_evidence.json` | t6 |

## 4. 关键证据（可追溯）
- 四维回溯：`tenant_id` / `session_id` / `approval_id` / `operation_id`
- 备份恢复原始记录：`evidence/postgres_recovery.json`（`backup/restore/rpo/dump_ok/restore_ok/rows`）
- 审计查询：`GET /api/audit?tenant_id=...&target_type=approval&target_id=...`
- 关键操作 ID：`<...>`

## 5. 异常与处理
- 出现的异常 / 回滚动作：`<无 / 描述>`（t6）
- 值班处理人：`<...>`
- 是否触发回滚：`<是/否>`（恢复失败 → 升级值班事件并回滚相关变更）

## 结论（待回填，勿臆造）
- RPO 是否 ≤ 15min：`<是/否>`（实测值 `...min`）
- RTO 是否 ≤ 60min：`<是/否>`（实测值 `...min`）
- 恢复后数据是否完整：`<是/否>`
- 是否达到预发布验收：`<通过/不通过>`
- 上线阈值是否满足（此项）：`<是/否>`
- 签名：`<执行人>/<复核人>`

> **差异说明（如实标注）**：旧记录 `deploy/records/DR-20260904-0219-api-worker-redis-restart.md` 引用的
> `postgres_recovery.json`（`backup=0.353s/restore=1.963s/rpo=0.0`）为**上一阶段基线**（容器级），
> 本阶段需由 **t3/t6 在当前环境重测并刷新 RPO/RTO**，不得直接沿用旧值作为本阶段达标证据。
