# 生产语境 · 备份恢复 + 回滚演练记录（DRILL_RECORDS_PROD）

> **状态声明（如实，勿臆造）**：本文件给出**生产语境下**两项必要演练的具体执行步骤、断言、应产出文件与 RPO/RTO 核对点。
> **当前生产未实跑**，因两项前置未满足：
> 1. **真实租户未注入** —— `production` 首批真实租户未确认（`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`AUTH_LOGIN_CREDENTIALS={}`，见 t2，BLOCKED_EXTERNAL）；
> 2. **生产栈未拉起** —— 本会话 Docker CLI 不可用、无 `after-sales-prod` 栈运行态证据。
>
> 因此以下 **演练结果一律标记为"待部署后执行"**，仅复用 **preview 已实跑验证** 的脚本与阈值口径（见 §3 引用），
> **不虚构通过**。生产实跑须以**真实租户**为采样 marker（**不可沿用演示 TENANT-A/B**）。
> 生成时间戳：`20260904-213407`。执行人/复核人：`infra（t2 基础设施） / security（t3 安全合规） / ops（t6 运维可观测）`。

---

## 0. 复用脚本与前置（全部已存在于仓库，未改动）

| 用途 | 脚本 | 说明 |
|------|------|------|
| 加密备份（RPO 路径） | `deploy/scripts/backup_encrypted.sh` | `pg_dump -Fc` 经管道 → `openssl enc -aes-256-cbc -pbkdf2 -salt`；连接用最小权限 `backup_role`；产出 `.dump.enc` + `.sha256` + `.manifest.csv` |
| 加密恢复（RTO 路径） | `deploy/scripts/restore_drill.sh` | `openssl enc -d` 解密 → 恢复临时库 `langgraph_restore_test` → 采样校验 + 实测 RPO/RTO → 写 JSON+MD 记录 |
| 容器化灾备补验（编排） | `deploy/drills/verify_dr_compose.sh` | 前置检查 + `backup_role` 下钻 + 加密备份 + 加密恢复 + RPO/RTO gauge 回填 |
| 回滚 | `deploy/scripts/rollback.sh` | 回滚前快照 → `pg_restore --clean --if-exists` 恢复数据 → 应用版本回退（`git archive <ref>` + `compose build` + `compose up -d`） |
| 日常备份调度 | `deploy/scripts/preview_backup_scheduler.sh` / `backup_db.sh` | 周期加密备份；`deploy/drills/run_preview_drills.sh` 六项演练编排 |
| 阈值 / 回滚条件 | `deploy/OPS_RUNBOOK.md §2 / §3` | RPO≤15min、RTO≤60min；回滚触发 6 信号 |
| 密钥管理 | `deploy/DR_KEY_MANAGEMENT.md` | `BACKUP_ENC_KEY`/`BACKUP_ROLE_PASSWORD` 仅环境变量注入，绝不硬编码/打印/入日志 |

> **生产差异提示**：生产库名/角色为 `after_sales_prod`（owner `migrator`、运行 `app_runtime`、备份 `backup_role`），
> 与 preview 的 `langgraph` 不同；但脚本用 `$POSTGRES_DB` / `$POSTGRES_USER` 环境变量取库，**可直接复用**。

---

## 1. 演练 A：生产数据库加密备份恢复（D2 / D2'）

### 1.1 前置条件（全部满足才可执行）
- [ ] 生产栈已拉起且 `postgres` Up（`docker compose -f docker-compose.prod.yml ps`）
- [ ] 真实租户已注入（`LAUNCH_ALLOWED_TENANTS` 含真实租户 id）、成员已由 `create_bootstrapped_tenants.py` 创建
- [ ] `deploy/.env.production` 已注入 `BACKUP_ENC_KEY`、`BACKUP_ROLE_PASSWORD`（`check_secrets.sh` 校验非占位）
- [ ] `migrate` 已执行一次（`backup_role` 角色已建）：`docker compose -f docker-compose.prod.yml run --rm migrate`

### 1.2 执行步骤（具体命令）
```bash
# A1. 前置：栈在跑 + backup_role 已存在
docker compose -f docker-compose.prod.yml ps | grep -qE 'postgres.*Up'

# A2. V1 最小权限下钻：backup_role 无写/DDL（返回 DDL_DENIED=1、WRITE_DENIED=1）
#    证据 → deploy/drills/records/dr_backup_role_compose.json
COMMIT=1; docker compose -f docker-compose.prod.yml exec -T postgres psql -U backup_role -d after_sales_prod -v ON_ERROR_STOP=1 -c "CREATE TABLE _forbidden(t int);" && COMMIT=0
docker compose -f docker-compose.prod.yml exec -T postgres psql -U backup_role -d after_sales_prod -v ON_ERROR_STOP=1 -c "INSERT INTO tenants(id,name,status) VALUES('_x','_x','_x');" && WRITE=0

# A3. 加密备份（KEY 只经环境变量注入）
BACKUP_ENC_KEY=<注入> BACKUP_ROLE_PASSWORD=<注入> bash deploy/scripts/backup_encrypted.sh
#    → deploy/backups/after_sales_prod-<ts>.dump.enc (+ .sha256 + .manifest.csv)

# A4. 加密恢复演练（解密+采样校验+实测 RPO/RTO）
BACKUP_ENC_KEY=<注入> bash deploy/scripts/restore_drill.sh deploy/backups/after_sales_prod-<ts>.dump.enc
```

### 1.3 断言（全部为真才算 PASS）
| 断言 | 判定 | 说明 |
|------|------|------|
| 加密归档 SHA-256 校验 | 期望 `sha256_ok=true` | `*.dump.enc.sha256` 与 `sha256sum` 一致 |
| 归档可解析 | 期望 `archive_list_ok=true` | `pg_restore --list` 可解析 |
| 密文可解 | 期望 `decrypt_ok=true` | `openssl enc -d` 解出 `PGDMP` 头 |
| 恢复成功 | 期望 `restore_ok=true` | `pg_restore` 到临时库成功 |
| **快照点后变更排除** | 期望 `marker_excluded=true` ∥ `marker_in_restore=0` | 备份点后写入的 marker 不应出现在恢复库 |
| **真实租户 marker 恢复** | 期望 `tenant_*_preserved>=1` | 用**真实租户 id** 采样（不可不沿用演示 TENANT-A/B）；改为 `<real-tenant-id>::count>=1` |
| 无数据丢失 | 期望 `rows_restored==rows_seeded` | `pg_recovery_verify.py` 校验（示例 seed 2 恢复 2；真实租户按实际行数） |
| RLS 策略恢复 | `pg_policies` 计数恢复 | 生产 `app_runtime` 依赖 RLS，须恢复策略 |

### 1.4 应产出文件（证据）
- `deploy/backups/after_sales_prod-<ts>.dump.enc` + `.dump.enc.sha256` + `.manifest.csv`
- `deploy/drills/records/drill-pg-encrypted-restore.json`（结构化：`sha256_ok`/`archive_list_ok`/`restore_ok`/`rpo_measured_seconds`/`rpo_bound_seconds`/`rto_restore_seconds`/`marker_excluded`/`tenant_*_preserved`/`rl_policies_count`）
- `deploy/drills/records/DR-<ts>-pg-encrypted-restore.md`（人类可读记录）
- `evidence/dr_backup_role_compose.json` + `evidence/dr_encrypted_backup_compose.json`（最小权限/加密备份证据）
- RPO/RTO gauge 回填：`drill_metric_exporter.py render/verify`（`drill_rpo/rto_seconds{component="pg_backup"}`）

### 1.5 RPO / RTO 核对点
- **RPO ≤ 15min**：核对 `rpo_measured_seconds <= 900`（且 `rpo_bound_seconds=BACKUP_INTERVAL_SECONDS`）。实测 RPO = 标记点与备份点的时间差（数据丢失窗口）。
- **RTO ≤ 60min**：核对 `rto_restore_seconds <= 3600`。实测 RTO = `pg_restore` 到临时库耗时。
- 记录时间戳与证据：JSON 的 `timestamp` / 备份 `mtime` / 记录 MD 顶部。

---

## 2. 演练 B：生产回滚（RB）

### 2.1 前置
- [ ] 生产栈已拉起；有**变更前加密备份**（`backup_encrypted.sh` 产物）或 `backup_db.sh` 明文快照
- [ ] 明确回滚目标 git 引用（回滚=恢复到变更前快照 + 应用版本回退到目标 ref）

### 2.2 执行步骤（具体命令）
```bash
# 0. 回滚前快照（可逆性；加密备份可长期存档）
BACKUP_ENC_KEY=<注入> bash deploy/scripts/backup_encrypted.sh   # → pre-rollback-<ts>.dump.enc

# 1. 恢复数据 + 应用版本回退（backup-file 必填；git-ref 可选，缺省=上一个部署引用）
bash deploy/scripts/rollback.sh deploy/backups/pre-rollback-<ts>.dump <target-git-ref>

# 2. 健康复验
bash deploy/scripts/healthcheck.sh
```
> 说明：`rollback.sh` 内联"回滚前快照"用**明文** pg_dump 直连 owner（重放恢复用）；独立 DR 加密归档走 `backup_encrypted.sh`/`restore_drill.sh`（OPS_RUNBOOK §3 注释）。

### 2.3 断言（全部为真才算 PASS）
| 断言 | 判定 | 说明 |
|------|------|------|
| 回滚前快照成功 | `快照文件存在 + 可解密/可解析` | `pre-rollback-<ts>.dump.enc` |
| 数据恢复成功 | `pg_restore --clean --if-exists` exit 0 | 恢复到变更前数据 |
| 应用回退成功 | `compose build` + `compose up -d` 正常 | 仅当提供 git-ref 且 target≠deployed 时执行 |
| 健康复验 | `healthcheck.sh` PASS | 仅 80/443、HTTPS 同源、无 CORS、fail-closed |
| 数据一致性 | 业务/checkpoint/RLS 一致 | 与 `restore_drill` 校验一致 |
| 审计可四维追溯 | `GET /api/audit` 正常 | tenant/session/approval/operation |
| 无重复副作用 | 同 `operation_id` 单值 | 幂等收敛 |
| 租户白名单/审批归属 | 跨租户拒绝 | 真实租户边界未破坏 |

### 2.4 应产出文件（证据）
- `deploy/backups/pre-rollback-<ts>.dump.enc` + `.sha256`（回滚前快照）
- `deploy/drills/records/rollback-record.json`（结构化） + `20260904*-drill-summary.json`
- `deploy/drills/records/DR-<ts>-rollback.md`（人类可读记录，含步骤结果表）
- 健康复验输出（`healthcheck.sh` 日志）

### 2.5 RPO / RTO 核对点
- 回滚以**恢复数据**达成，RPO 口径同演练 A（≤15min，依赖最近一次变更前备份）；RTO 口径=恢复+重建耗时。
- **可分步回滚**：步骤 1（数据恢复）与步骤 2（应用回退）独立；若仅需数据回滚可不传 git-ref。
- 记录时间戳与证据：回滚触发/完成时间、快照文件、`pg_restore` 日志、`healthcheck.sh` 结果。

---

## 3. 生产 vs 已有 preview 实证（口径，勿混用）
| 项 | 生产（本文件） | preview 已实跑证据 |
|----|---------------|-------------------|
| 加密备份恢复 | **待部署后执行** | `DR-20260904142304-pg-encrypted-restore.md`：RPO 实测 43.780315s（上界 900s）、RTO 0.643223s、SHA-256 PASS、marker 排除、TENANT-A/B 保留 → **RPO≤15min / RTO≤60min 达标（preview）** |
| 回滚 | **待部署后执行** | `DR-20260904030354-rollback.md`：快照/数据恢复/健康复验 True、应用重建 no-op（target==deployed ref=251f430）、判定 True；结构化 `20260904030652-drill-summary.json` + `rollback-record.json` |
| 数据恢复一致性 | 待部署后执行 | `scripts/pg_recovery_verify.py`（`evidence/postgres_recovery.json`）：backup 0.353s / restore 1.963s / rows_seeded=2=rows_restored=2 |
| 容器化补验编排 | 待部署后执行 | `deploy/drills/verify_dr_compose.sh`（V1 最小权限 / V2 加密备份 / V3 恢复 / V4 gauge 回填），配合 `run_preview_drills.sh` 六项编排 |

> **口径声明**：preview 值**只证明工具链与阈值可达成**，**不构成生产达标证据**。生产实跑须以真实租户为 marker 重测，替换全部 `<...>`，并参考 `deploy/drills/records/TEMPLATE-pg-backup-restore.md`、`TEMPLATE-rollback.md` 修订记录。

---

## 4. 待办（解锁生产实跑）
1. **真实租户名单**（业务方/用户提供；同时解锁 t2/t4/t5/t6/t9）→ `create_bootstrapped_tenants.py` 创建 + `AUTH_LOGIN_CREDENTIALS` 注入 argon2id PHC + `LAUNCH_ALLOWED_TENANTS` 注入。
2. **生产栈拉起**（依赖真实租户 + 受信 TLS + 真实 LLM/网关沙箱；见 t1 复盘的 5 项 BOUNDARY-4 外部依赖）。
3. 拉起后按 §1/§2 执行演练，置 `[EXECUTED]` 并回填真实 RPO/RTO、marker（真实租户）、签名。

## 结论（如实）
- 生产 DR 演练：**`[PENDING_DEPLOY]`（待真实租户就绪 + 生产栈拉起后执行）**。
- 是否达到生产 RPO≤15min / RTO≤60min / 回滚就绪：**生产未实测，不宣称达标**（preview 已达标 = 口径证明）。
- 签名：`infra / security / ops`（生产实跑后由各域复核人补签）
