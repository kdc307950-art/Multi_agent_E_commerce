# PostgreSQL 加密备份恢复演练记录 — 生产（待部署后执行）

> **状态：`[PENDING_DEPLOY]`** —— 本记录为**生产栈的 DR 演练模板/计划**，**尚未在生产栈实机执行**。
> 原因（如实，BOUNDARY-4 外部依赖）：① 生产栈未拉起（本会话 Docker CLI 不可用、无 `docker-compose.prod.yml` 运行态证据）；
> ② `production` 首批**真实租户**未确认（`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`AUTH_LOGIN_CREDENTIALS={}`，t2 = BLOCKED_EXTERNAL）。
> 生产 DR 演练必须在**真实租户就绪 + 生产栈拉起**后执行；本文件提供**可直接复用的脚本 + 记录骨架**，并在 §6 引用**已实测通过的 preview 证据**作为工具链/阈值达标的口径证明（非生产值）。
> 生成时间戳：`20260904-213407`。执行人/复核人：`infra（t2 后端/基础设施） / security（t3）/ ops（t6）`。

---

## 0. 记录标识
- 演练编号（实跑后填）：`DR-<YYYYMMDD-HHMM>`
- 环境：`production`（`after-sales-prod`；PostgreSQL 17 / Redis 7 / api / worker / frontend / nginx / migrate / backup）
- 执行人 / 复核人：`<执行人> / <复核人>`
- 开始时间 / 结束时间：`<...> / <...>`
- 应用 git 引用：`<deploy.sh 记录的部署 ref>`（备份恢复针对该版本数据）

---

## 1. 加密备份（RPO 路径）
- 命令（生产，密钥只经环境变量注入，见 `deploy/DR_KEY_MANAGEMENT.md`）：
  ```bash
  BACKUP_ENC_KEY=<注入> BACKUP_ROLE_PASSWORD=<注入> bash deploy/scripts/backup_encrypted.sh
  ```
  - `pg_dump -Fc` 经管道 → `openssl enc -aes-256-cbc -pbkdf2 -salt`（`abackup_role` 最小权限）→ `deploy/backups/<name>.dump.enc`
  - 配套 `<name>.dump.enc.sha256`（SHA-256 校验和）+ `<name>.manifest.csv`
- 加密归档：`deploy/backups/after_sales_prod-<ts>.dump.enc`
- 备份耗时：`<...> s`（实测）
- **RPO**：`<...> min`，阈值 ≤ **15min**（实测；同时记录备份周期上界 `BACKUP_INTERVAL_SECONDS`）
- 备份是否成功：`<是/否>`（密文可解 + `pg_restore --list` 校验）

## 2. 恢复（RTO 路径）
- 命令：
  ```bash
  BACKUP_ENC_KEY=<注入> bash deploy/scripts/restore_drill.sh deploy/backups/<name>.dump.enc
  ```
  - `openssl enc -d` 解密 → 恢复到独立临时库 → 采样校验 + RPO/RTO 实测
- 恢复耗时（RTO）：`<...> s`，阈值 ≤ **60min**
- 恢复是否成功：`<是/否>`

## 3. 校验结果（恢复后数据完整性）
| 校验项 | 结果 | 证据 | 回填来源 |
|--------|------|------|---------|
| 加密归档 SHA-256 校验 | `<通过/失败>` | `*.dump.enc.sha256` | restore_drill E2 |
| 归档完整性（`pg_restore --list`） | `<通过/失败>` | `drill-pg-encrypted-restore.json` | E3 |
| 快照点后变更排除（marker） | `<排除/未排除>` | `marker_in_restore=0` | E5 |
| **真实租户 marker 恢复**（替换演示 TENANT-A/B） | `<保留/未保留>` | `<真实租户 id>` 在恢复库计数 | E6 |
| 采样数据保留（真实租户） | `<N/N>` | `tenant_*_preserved` | E6 |
| RLS 策略恢复 | `<是/否（策略数）>` | `pg_policies` | — |
| 备份角色最小权限（无写/DDL） | `<是/否>` | 下钻 `backup_role` 无 DML/DDL 授权 | — |
| 租户隔离（恢复后不串租户） | `<通过/不通过>` | `pg_checkpoint_recovery_record` | t3/t6 |
| 关键操作幂等（同 `operation_id` 不重复） | `<通过/不通过>` | `pg_acceptance_evidence` | t6 |

## 4. 关键证据（可追溯）
- 结构化：`deploy/drills/records/drill-pg-encrypted-restore.json`（`sha256_ok`/`archive_list_ok`/`restore_ok`/`rto_restore_seconds`/`rpo_measured_seconds`/`rpo_bound_seconds`/`marker_excluded`/`tenant_*_preserved`/`rl_policies_count`）
- 备份加密密钥仅环境变量注入，未打印/写日志（`deploy/DR_KEY_MANAGEMENT.md`）
- 审计查询：`GET /api/audit?tenant_id=...&target_type=approval&target_id=...`

## 5. 异常与处理
- 出现的异常 / 回滚动作：`<无 / 描述>`
- 值班处理人：`<...>`
- 是否触发回滚：`<是/否>`（恢复失败 → 升级值班事件并回滚相关变更）

## 6. 生产 vs 已有 preview 实证（口径，勿混用）
- 生产本记录当前**未执行**。
- 已实测的 **preview** 加密备份恢复证据（工具链/阈值达标证明，**不是生产值**）：
  - `deploy/drills/records/DR-20260904142304-pg-encrypted-restore.md`：RPO 实测 43.780315s（上界 900s）、RTO 0.643223s、SHA-256 PASS、marker 排除、TENANT-A/B 保留 —— **RPO≤15min / RTO≤60min 达标（preview）**。
  - 早期 `scripts/pg_recovery_verify.py`（`evidence/postgres_recovery.json`）：backup 0.353s / restore 1.963s / rows_seeded=2=rows_restored=2。
  - `deploy/drills/records/DR-20260904030211-pg-backup-restore.md`（旧明文容器级基线，已由加密版本 superseded）。

## 结论（如实，勿臆造）
- RPO 是否 ≤ 15min：`生产未实测`（preview 实测 43.78s ≤ 900s 上界）
- RTO 是否 ≤ 60min：`生产未实测`（preview 实测 0.643s ≤ 3600s）
- 加密归档 / 最小权限备份角色是否达标：`生产待执行`（preview PASS）
- 恢复后数据是否完整：`生产待执行`
- 是否达到生产 DR 验收：**待生产栈拉起 + 真实租户就绪后实跑**。当前**未**宣称生产达标。
- 签名：`<执行人>/<复核人>`

> **差异说明**：本记录为生产预案/模板。生产实跑后须替换全部 `<...>`，并以**真实租户**为采样 marker（不可沿用演示 TENANT-A/B）。
