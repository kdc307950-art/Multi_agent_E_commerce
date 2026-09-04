# 放量审批单（模板骨架）

> **填表说明**：模板/骨架，字段占位 `<...>`。放量前条件（阈值达成 + 演练记录可复核）来自《OPS_RUNBOOK §2 上线阈值》
> 与各演练记录（t3 演练指标 / t6 DR 实跑）；目标租户范围来自灰度租户清单；风险与合规来自 **t2 安全评审**。
> **审批角色**：仅当前租户内的 `admin` / `approver` 可审批；**同租户审批**，跨租户审批一律拒绝并留痕；
> `platform_admin` 不是租户审批角色，不得绕过租户审批归属。
> 最终文件名约定：`deploy/records/SCALEUP_APPROVAL-<ts>.md`。

---

## 0. 审批单标识
- 审批单 ID：`SCALEUP-<ts>`
- 申请时间 / 审批时间：`<...> / <...>`
- 放量阶梯：`Tier 0 / Tier 1 / Tier 2 / Tier 3`（见 `deploy/records/CANARY_TENANTS-<ts>.md`）
- 申请人（放量执行方）：`<t1 / t3>`；审批人（租户 admin/approver）：`<t2 确认>`；复核人：`<...>`

---

## 1. 放量前条件（**全满足才可放量**，逐项勾选并给出证据）
> 《OPS_RUNBOOK §2》：各项须**实测/演练可复核**，未经实测不得宣称达标。

| # | 条件 | 是否满足 | 证据/记录 | 回填来源 |
|---|------|---------|----------|---------|
| 1 | 部署健康：`healthcheck.sh` 4 项全过；仅 80/443 开放；无 CORS 头 | `<是/否>` | `PRODUCTION_DEPLOYMENT_record-<ts>.md` | t1 |
| 2 | 数据恢复：RPO ≤ 15min，RTO ≤ 60min | `<是/否>` | `<-pg-backup-restore.md>` | t3/t6 |
| 3 | 重启韧性：API/worker/Redis 重启后读接口恢复；Redis 重启不影响 PG 数据面 | `<是/否>` | `DR-...-restart.md` | t3/t6 |
| 4 | 审批幂等：同审批重复决策收敛到单一 `operation_id` | `<是/否>` | `E2E_APPROVAL_RECORD.md` / 审计 | t2/t3 |
| 5 | SSE 恢复：断线 resume 只重放不重执行 | `<是/否>` | `drill_api.sh sse` | t3 |
| 6 | 并发幂等：同 `client_request_id` 单一 stream/操作 | `<是/否>` | `drill_api.sh concurrent` | t3 |
| 7 | 对账：非终态收口；不一致/不可核实 → 转人工 | `<是/否>` | `COMPENSATION_RECONCILIATION_REPORT.md` | t3/t6 |
| 8 | 审计追溯：关键审计可四维（tenant/session/approval/operation）定位 | `<是/否>` | `audit_trace.py` | t2/t3 |
| 9 | 写操作模型：写风险工具仅 `HIGH_CONFIDENCE_MODELS` 内可写 | `<是/否>` | `evidence/llm_candidate_eval.json` | t1/t2 |
| 10 | 完全自托管：LLM 端点白名单；Graphiti 遥测关闭；无未经批准出网 | `<是/否>` | `LLM_ALLOWED_HOSTS` / 网络策略 | t2 |
| 11 | 安全合规：租户隔离/审批归属/幂等/审计/无未审计写 全过 | `<是/否>` | t2/t5 动态验收 | t2/t5 |

- **阈值达成结论**：`<全部达成 / 部分未达成>`（未达成不得放量）
- **演练记录可复核性**：`<全部可复核 / 存在缺失>`（记录缺失 → 不得放量）

## 2. 目标租户范围（本次放量）
- 本次放量租户：`<TENANT-A / +TENANT-B / +...>`（引用 `CANARY_TENANTS-<ts>.md` 阶梯）
- 放量后白名单（`LAUNCH_ALLOWED_TENANTS`）：`<...>`
- 放量倍率 / 步进：`<...>`；观察期：`<...>`

## 3. 风险评估
| 风险项 | 等级 | 说明 | 缓解/预案 | 回填来源 |
|--------|------|------|-----------|---------|
| 跨租户访问 | `<低/中/高>` | 跨租户默认拒绝 | 数据面 RLS + `TenantScopedCheckpointer` + 同租户审批 | t2 |
| 重复资金副作用（同 operation_id 执行两次） | `<...>` | 幂等键 + 唯一约束 | 幂等 + 审计 | t2/t3 |
| 敏感写绕过人工审批 | `<...>` | 唯一 `human_approval`，无 direct | `launch_require_approval=true` + 门控 | t2 |
| 模型档位不足导致转人工率上升 | `<...>` | `HIGH_CONFIDENCE_MODELS` 白名单 | 转人工 + 运营监控 | t1/t2 |
| 完全自托管出网泄漏 | `<...>` | 遥测关闭 + 端点白名单 | 网络出口策略 | t2 |
| RPO/RTO 超基线 | `<...>` | 备份恢复演练 | 触发回滚（OPS_RUNBOOK §3） | t3/t6 |

## 4. 审批意见（**同租户 admin/approver 填写**）
- 审批人（tenant_id / user_id / 角色）：`<...>` —— 须属于目标租户的 `admin`/`approver`
- 审批意见：`<同意 / 不同意 / 同意但需...>`
- 二次确认：`<是/否>`（OPS_RUNBOOK §3：审批需二次确认）
- 结论：`<批准放量 / 拒绝放量 / 有条件放量>`
- 审批记录（审计留痕）：`approval_id=<...>; operation_id=<...>`（可四维追溯）
- 审批签名 / 时间：`<...>`

## 5. 结论（模板，勿臆造）
- 放量前条件是否全部达成且可复核：`<是/否>`（未实测/记录缺失一律为"否"）
- 是否批准本次放量：`<是/否>`
- 放量后复验要求：`<healthcheck.sh` + 告警无异常 + 观察期指标达标>`
- 签发：`<审批人> / <复核人>`

> **跨租户红线**：若审批人/目标租户不在同一 `tenant_id`，本轮放量**拒绝并留痕**；`platform_admin`
> 仅用于平台级合规流程（显式目标租户 + 最小范围 + 二次确认 + 不可抵赖审计），不能替代租户审批。
