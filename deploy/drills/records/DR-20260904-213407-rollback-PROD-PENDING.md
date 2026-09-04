# 回滚记录 — 生产（待部署后执行）

> **状态：`[PENDING_DEPLOY]`** —— 本记录为**生产栈回滚演练的模板/计划**，**尚未在生产栈实机执行**。
> 原因（如实，BOUNDARY-4 外部依赖）：生产栈未拉起 + 真实租户未确认（见 t7 备份恢复记录 §0）。
> 生产回滚演练必须在**真实租户就绪 + 生产栈拉起**后执行；本文件提供**可直接复用的脚本 + 记录骨架**，并在 §5 引用
> 已实测通过的 **preview** 回滚证据作为"回滚就绪 + 可分步回滚"的口径证明（非生产值）。
> 生成时间戳：`20260904-213407`。执行人/复核人：`infra（t2）/ security（t3）/ ops（t6）`。

---

## 0. 记录标识
- 回滚编号（实跑后填）：`RB-<YYYYMMDD-HHMM>`
- 环境：`production`（`after-sales-prod`；PostgreSQL 17 / Redis 7 / api / worker / frontend / nginx）
- 执行人 / 复核人：`<执行人> / <复核人>`
- 触发时间 / 完成时间：`<...> / <...>`
- 前置部署 git 引用：`<deploy ref>`；回滚目标 git 引用：`<target ref>`

---

## 1. 触发条件（命中哪条，才回滚）
> 《OPS_RUNBOOK §3》：满足任一即触发回滚。
| # | 触发信号 | 是否命中 | 证据/说明 |
|---|----------|---------|----------|
| 1 | `healthcheck.sh` 任一项失败 | `<是/否>` | `<...>` |
| 2 | PG 备份恢复 RPO/RTO 超基线，或恢复后数据不完整 | `<是/否>` | `<-pg-backup-restore.md>` |
| 3 | 审批/执行/对账出现**重复副作用**（同 `operation_id` 两次） | `<是/否>` | `<audit id>` |
| 4 | 任一租户"仅审批后执行"被绕过（未审批即执行） | `<是/否>` | `<audit id>` |
| 5 | 指标异常：错误率/审批失败率/对账 mismatch/转人工率显著上升 | `<是/否>` | `<alert>` |
| 6 | 审计追溯缺失（无法四维定位） | `<是/否>` | `<...>` |
- 本次回滚理由（一句话因果）：`<...>`

## 2. 回滚前快照（可逆性）
- 快照命令：`BACKUP_ENC_KEY=<注入> bash deploy/scripts/backup_encrypted.sh`（加密备份，可长期存档）
- 快照文件：`deploy/backups/pre-rollback-<ts>.dump.enc`（密文）+ `.sha256`
- 快照验证（可解密 + `pg_restore --list`）：`<通过 / 未验证>`
- 快照耗时：`<...> s`

## 3. 回滚动作（`bash deploy/scripts/rollback.sh <backup-file> <git-ref>`）
| 步骤 | 动作 | 命令/参数 | 结果 |
|------|------|-----------|------|
| 0 | 回滚前快照（可逆性） | `backup_encrypted.sh` | `<成功/失败>` |
| 1 | 恢复数据库（`pg_restore --clean --if-exists`） | `<backup-file>` | `<成功/失败>` |
| 2 | 应用版本回退（`git archive <ref>` + `compose build` + `compose up -d`） | `<git-ref>` | `<成功/失败>` |
| 3 | （可选）仅数据回滚不传 git-ref | `<—>` | `<—>` |

> 说明：`rollback.sh` 内联"回滚前快照"用**明文** pg_dump 直连 owner（重放恢复用）；独立、可长期存档的 **DR 加密归档**走 `backup_encrypted.sh`/`restore_drill.sh`（见 `OPS_RUNBOOK §3 注释`）。

## 4. 复验（回滚后）
| # | 复验项 | 结果 | 备注 |
|---|--------|------|------|
| 1 | `bash deploy/scripts/healthcheck.sh` | `<PASS/FAIL>` | 仅 80/443 / HTTPS 同源 / 无 CORS / fail-closed |
| 2 | 数据一致性（读取业务/checkpoint/RLS） | `<一致/不一致>` | `<-pg-backup-restore.md>` |
| 3 | 关键审计仍可四维追溯 | `<是/否>` | `GET /api/audit` |
| 4 | 无重复业务副作用（幂等收敛） | `<是/否>` | `operation_id` 单值 |
| 5 | 租户白名单/审批归属仍正确 | `<是/否>` | 跨租户拒绝 |

## 5. 生产 vs 已有 preview 实证（口径，勿混用）
- 生产本记录当前**未执行**。
- 已实测的 **preview** 回滚证据（"回滚就绪 + 可分步回滚"口径证明，**不是生产值**）：
  - `deploy/drills/records/DR-20260904030354-rollback.md`：步骤 0 快照 True / 1 数据恢复 True / 2 应用重建 False（target==deployed ref=251f430，无版本差异 => no-op）/ 3 健康复验 True；判定 True。
  - 结构化：`deploy/drills/records/20260904030652-drill-summary.json` + `rollback-record.json`。
  - 回滚入口与边界：`deploy/scripts/rollback.sh`（数据库恢复 + 应用版本回退；**不提供部分回滚到中间 schema**，与"先备份再变更、恢复靠快照"基线一致）。

## 6. 结论
- 本次回滚是否成功：`生产未实测`（preview 实跑通过）
- 回滚后是否恢复可用且无净副作用：`生产待执行`
- 是否需二次审批才能恢复变更：`<是/否>`（OPS_RUNBOOK §4 步骤 7）
- 复盘要点 / 阈值调整 / 演练记录更新：`<...>`
- 签名：`<执行人>/<复核人>`

> **差异说明**：本记录为生产预案/模板。生产实跑后须替换全部 `<...>` 并把回滚目标 ref、复验项逐项真实验证后才可宣称"通过"。
