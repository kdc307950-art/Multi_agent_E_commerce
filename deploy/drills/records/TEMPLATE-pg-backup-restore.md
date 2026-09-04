# PostgreSQL 加密备份恢复演练记录（模板骨架）

> **填表说明**：模板/骨架，字段占位 `<...>`。RPO/RTO 实测与备份恢复结果由 **t2（灾备能力建设）** 提供
> `restore_drill.sh`（**加密**备份 + 最小权限备份角色 `backup_role` + SHA-256 校验 + 实测 RPO/RTO）。
> 执行人/复核人应实名。最终文件名约定：`deploy/drills/records/<ts>-pg-encrypted-restore.md`。
> 阈值（OPS_RUNBOOK §2）：**RPO ≤ 15 分钟，RTO ≤ 60 分钟**；任何一天恢复失败都应升级为值班事件并回滚相关变更。

---

## 0. 记录标识
- 演练编号：`DR-<YYYYMMDD-HHMM>`
- 环境：`preview`（PostgreSQL `17-alpine`，DB=`langgraph`，owner=`migrator`；运行角色 `app_runtime` 仅 DML；**备份角色 `backup_role` 仅 CONNECT/SELECT**）
- 执行人 / 复核人：`<t2 执行人 / 复核人>`（推荐 `drill-runner / security-auditor`）
- 开始时间 / 结束时间：`<...> / <...>`
- 应用 git 引用：`<t1 deploy.sh 记录的 ref>`（备份恢复针对该版本数据）

---

## 1. 加密备份（RPO 路径）
- 备份命令：`BACKUP_ENC_KEY=<注入> bash deploy/scripts/backup_encrypted.sh`
  （`pg_dump -Fc` 经管道 `openssl enc -aes-256-cbc -pbkdf2 -salt` → `.dump.enc`；连接用最小权限 `backup_role`）
- 加密归档：`deploy/backups/langgraph-<ts>.dump.enc`
- SHA-256 校验和：`langgraph-<ts>.dump.enc.sha256`（`sha256sum`）
- 备份耗时：`<...> s`（实测）
- **RPO（数据恢复点目标）**：`<...> min` —— 阈值 ≤15min（实测；备份点与故障点的数据丢失窗口；同时记录备份周期上界）
- 备份是否成功：`<是/否>`（密文可解 + 归档 `pg_restore --list` 校验）

## 2. 恢复（RTO 路径）
- 恢复命令：`BACKUP_ENC_KEY=<注入> bash deploy/scripts/restore_drill.sh <backup.enc>`
  （`openssl enc -d` 解密 → 恢复到独立临时库 `langgraph_restore_test` → 采样校验 + RPO/RTO 实测）
- 恢复耗时(RTO)：`<...> s`（实测）
- 恢复是否成功：`<是/否>`

## 3. 校验结果（恢复后数据完整性）
| 校验项 | 结果 | 证据 | 回填来源 |
|--------|------|------|---------|
| 加密归档 SHA-256 校验 | `<通过/失败>` | `*.dump.enc.sha256` | t2（restore_drill E2） |
| 归档完整性（`pg_restore --list`） | `<通过/失败>` | `drill-pg-encrypted-restore.json` | t2 |
| 快照点后变更排除（marker） | `<排除/未排除>` | `marker_in_restore=0` | t2（E5） |
| 采样数据保留（TENANT-A/B） | `<N/N>` | `tenant_a/b_preserved` | t2（E6） |
| RLS 策略是否恢复 | `<是/否（策略数）>` | `pg_policies` 计数 | t2 |
| 备份角色最小权限（无写/DDL） | `<是/否>` | 下钻 `backup_role` 无 DML/DDL 授权 | t2 |
| 租户隔离（恢复后不串租户） | `<通过/不通过>` | `pg_checkpoint_recovery_record.json` | t2/t6 |
| 关键操作幂等（同 `operation_id` 不重复） | `<通过/不通过>` | `pg_acceptance_evidence.json` | t6 |

## 4. 关键证据（可追溯）
- 结构化记录：`deploy/drills/records/drill-pg-encrypted-restore.json`
  （`sha256_ok` / `archive_list_ok` / `restore_ok` / `rto_restore_seconds` / `rpo_measured_seconds`
   / `rpo_bound_seconds` / `marker_excluded` / `tenant_a_preserved` / `tenant_b_preserved` / `rl_policies_count`）
- 备份加密密钥仅环境变量注入，未打印/写日志（见 `deploy/DR_KEY_MANAGEMENT.md`）。
- 审计查询：`GET /api/audit?tenant_id=...&target_type=approval&target_id=...`

## 5. 异常与处理
- 出现的异常 / 回滚动作：`<无 / 描述>`
- 值班处理人：`<...>`
- 是否触发回滚：`<是/否>`（恢复失败 → 升级值班事件并回滚相关变更）

## 结论（待回填，勿臆造）
- RPO 是否 ≤ 15min：`<是/否>`（实测值 `...min`；上界 `...min`）
- RTO 是否 ≤ 60min：`<是/否>`（实测值 `...min`）
- 加密归档 / 最小权限备份角色是否达标：`<是/否>`
- 恢复后数据是否完整：`<是/否>`
- 是否达到预发布验收：`<通过/不通过>`
- 签名：`<执行人>/<复核人>`

> **差异说明（如实标注）**：旧记录 `deploy/records/DR-20260904-0219-api-worker-redis-restart.md` 引用的
> `postgres_recovery.json`（backup/restore/rpo）为**上一阶段基线（同机明文 pg_dump，容器级）**，
> 本阶段由 **t2 的 `restore_drill.sh`** 在当前环境重测并刷新为**加密备份 + 最小权限 backup_role + 实测 RPO/RTO**，
> 不得沿用旧值作为本阶段达标证据。
