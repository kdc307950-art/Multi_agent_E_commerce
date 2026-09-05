# 对账运行态验证（deploy-engineer · 阶段三）

> 位置：**运行态**（真实沙箱网关 `http://sandbox-gateway:8010`；`ExecutionEngine.reconcile` 对 SUBMITTED/异常记录执行外部 `query` 核实并收口）。
> 结论：**全部 `PASS`（运行态实测）**。对账不静默当成功、不重复执行，并把「关联键」与「审计事件」留痕到可追踪。

---

## 1. 判定口径与结果（`runtime/runtime_verify_report.json` → `reconcile`）

| # | 场景 | 期望 | 实测 |
|---|---|---|---|
| 1 | **渠道已执行/系统未收尾**：已提交到网关的真实 external_txn_id + 超过确认窗口（overdue）未回调 | 对账经 `query` 网关确认为 succeeded → `CONFIRMED`（绝不再提交） | ✅ `overdue_reconcile_confirmed: true`、`reconcile_confirmed_count_ge_1: true` |
| 2 | **系统已执行/渠道未执行**：external_txn_id 在网关不存在（`not_found`） | 对账 `unknown_provider_status:not_found` → `MISMATCHED` + operation 转 `HUMAN_HANDOFF` | ✅ `missing_external_reconcile_mismatch: true`、`missing_external_operation_human_handoff: true`、`reconcile_mismatch_count_ge_1: true` |
| 3 | **金额不一致** | 回调 `amount_mismatch` 转人工（见 CALLBACK_SECURITY） | ✅ `tampered_amount_amount_mismatch: true` |
| 4 | **租户归属不一致** | 跨租户回调/查询 → `not_found`（隔离，不泄露） | ✅ `cross_tenant_callback_not_found: true`（见 CALLBACK_SECURITY） |
| 5 | **审批与执行不一致** | 审批拒绝→不执行；审批通过→提交；对账/补偿失败→人工 | 见既有测试（`test_sandbox_e2e_flow` 拒绝不执行 / `test_fault_injection` 补偿失败转人工）；已在发布基线 `390 passed` 覆盖 |

---

## 2. 审计事件 + 关联键留痕（对账口径的可追踪字段）

对账确认 / 冲突各落一条 **token 级审计**，且携带 `operation_id` / `reason`（运行态断言）：
```
audit_confirmed_action_written: true          # execution.reconcile.confirmed
audit_confirmed_carries_operation_id: true    # 审计 detail 含 operation_id
audit_mismatch_action_written: true           # execution.reconcile.mismatch
audit_mismatch_carries_reason: true           # 含 unknown_provider_status:* 原因
```

**执行记录/操作的关联键（对账口径所要求）在收敛后齐全**：
```json
traceability_fields_confirmed_record:  {"tenant_id":"TENANT-A","operation_id":"46fa01b1-...",
  "external_operation_id":"txn-c556a89d3dd84c56","execution_status":"confirmed"}
traceability_fields_mismatched_record: {"tenant_id":"TENANT-A","operation_id":"e49dd1a9-...",
  "external_operation_id":"txn-does-not-exist","execution_status":"mismatched"}
```
> 说明：`external_operation_id` 即对账用的 `external_txn_id`（渠道侧外部单号）；`execution_status` 即 `execution_status`；`operation_id`、`tenant_id` 均为可关联键。`approval_id`（审批单）与 `thread_id`（会话）在操作/执行记录上关联（`store` 层维护），并随 `execution.*` 审计事件回溯到租户/会话/工单——这一关联链由既有验收测试（`test_sandbox_e2e_flow.py` 全程可追溯）与 `test_approval_idempotency.py` 覆盖。

---

## 3. 对账语义要点（代码证据，非本任务新增）

- **overdue 强制并入**：`list_reconciliation_targets` 先返回 overdue（超过 `confirm_timeout_seconds` 仍未确认）记录，且 `reconcile` 再额外把 overdue 并入（受 `limit` 截断也不会漏掉），确保超时未确认一定进入外部核实或转人工。
- **外部核实**：`provider.query(tenant_id, external_txn_id)`；查询失败 / 状态未知 → `mismatch` 转人工；外部明确失败 → 补偿（回滚），补偿失败 → 人工；绝不静默当成功。
- **失败补偿幂等**：以 `(tenant_id, idempotency_key, execution_id)` 派生稳定 reversal_id，重复补偿只收敛单一终态（见 `test_fault_injection.py::test_compensation_idempotent_single_terminal`）。
- **对账最后运行时间 gauge**：`reconcile_last_run_timestamp_seconds`，供「对账超时未运行」告警判定。

---

## 状态：✅ 运行态实测（overdue 收敛 / 缺失转人工 + 审计/关联键留痕）
