# 回滚记录（模板骨架）

> **填表说明**：模板/骨架，字段占位 `<...>`。触发条件来自《OPS_RUNBOOK §3 回滚条件》；快照与回滚动作由
> **t6（DR 演练实跑 / 回滚演练）** 回填，git ref 由 **t1** 提供。复验走 `healthcheck.sh`。
> 最终文件名约定：`deploy/drills/records/<ts>-rollback.md`。
> **安全基线**：回滚 = ① 恢复到变更前快照（RPO）② 应用版本回退（git ref）。不提供"部分回滚到中间 schema"。

---

## 0. 记录标识
- 回滚编号：`RB-<YYYYMMDD-HHMM>`
- 环境：`preview`（`after-sales-preview`；PostgreSQL 17 / Redis 7 / api / worker / frontend / nginx）
- 执行人 / 复核人：`<t6 执行人 / 复核人>`
- 触发时间 / 完成时间：`<...> / <...>`
- 前置部署 git 引用：`<t1 front-ref>`；回滚目标 git 引用：`<t1 target-ref>`

---

## 1. 触发条件（命中哪条，才回滚）
> 《OPS_RUNBOOK §3》：满足任一即触发回滚。
| # | 触发信号 | 是否命中 | 证据/说明 |
|---|----------|---------|----------|
| 1 | `healthcheck.sh` 任一项失败（暴露内网端口/HTTPS 异常/CORS 泄漏/fail-closed 失效） | `<是/否>` | `<...>` |
| 2 | PG 备份恢复 RPO/RTO 超基线，或恢复后数据不完整 | `<是/否>` | `<-pg-backup-restore.md>` |
| 3 | 审批/执行/对账出现**重复业务副作用**（同 `operation_id` 被执行两次） | `<是/否>` | `<audit id>` |
| 4 | 任一租户"仅审批后执行"被绕过（出现未审批即执行） | `<是/否>` | `<audit id>` |
| 5 | 指标异常：错误率/审批失败率/对账 mismatch/转人工率显著上升（告警触发） | `<是/否>` | `<alert>` |
| 6 | 审计追溯缺失：关键审计无法四维（tenant/session/approval/operation）定位 | `<是/否>` | `<...>` |
- 本次回滚理由（一句话因果）：`<...>`

## 2. 回滚前快照（可逆性）
- 快照命令：`bash deploy/scripts/backup_db.sh deploy/backups/pre-rollback-<ts>.dump`
- 快照文件：`deploy/backups/pre-rollback-<ts>.dump`
- 快照验证（dump 完整）：`<pg_restore --list 通过 / 未验证>`
- 快照耗时：`<...> s`

## 3. 回滚动作（`bash deploy/scripts/rollback.sh <backup-file> <git-ref>`）
| 步骤 | 动作 | 命令/参数 | 结果 |
|------|------|-----------|------|
| 1 | 恢复数据库（`pg_restore --clean --if-exists`） | `<backup-file>` | `<成功/失败>` |
| 2 | 应用版本回退（`git checkout <ref>` + `compose build --pull` + `compose up -d`） | `<git-ref>` | `<成功/失败>` |
| 3 | （可选）仅数据回滚不传 git-ref | `<—>` | `<—>` |

## 4. 复验（回滚后）
| # | 复验项 | 结果 | 备注 |
|---|--------|------|------|
| 1 | `bash deploy/scripts/healthcheck.sh` | `<PASS/FAIL>` | 仅 80/443 / HTTPS 同源 / 无 CORS / fail-closed |
| 2 | 数据一致性（读取业务/checkpoint/RLS） | `<一致/不一致>` | `<-pg-backup-restore.md>` |
| 3 | 关键审计仍可四维追溯 | `<是/否>` | `GET /api/audit` |
| 4 | 无重复业务副作用（幂等收敛） | `<是/否>` | `operation_id` 单值 |
| 5 | 租户白名单/审批归属仍正确 | `<是/否>` | 跨租户拒绝 |

## 5. 结论
- 本次回滚是否成功：`<是/否>`
- 回滚后是否恢复可用且无净副作用：`<是/否>`
- 是否需二次审批才能恢复变更：`<是/否>`（OPS_RUNBOOK §4 步骤 7）
- 复盘要点 / 阈值调整 / 演练记录更新：`<...>`
- 签名：`<执行人>/<复核人>`

> **差异说明**：本记录为骨架。若 t6 未实机执行回滚演练，仅**记录触发条件+快照+动作设计**，
> 不得把"设计好的回滚步骤"当作"已回滚"的实证；复验项须逐项真实验证后才填"通过"。
