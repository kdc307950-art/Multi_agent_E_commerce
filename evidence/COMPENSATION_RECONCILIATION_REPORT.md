# 补偿与对账报告（Compensation & Reconciliation Report）

> 交付物：真实业务 Adapter + 沙箱 Provider 的补偿与对账闭环证据。
> 生成依据：`evidence/shadow_acceptance.json`（37/37）、`evidence/live_acceptance.json` + `live_acceptance_summary.md`（41/41）、`src/execution/engine.py` 的补偿/对账实现，以及 `tests/test_execution_engine.py` 的 overdue 用例。
> 范围：业务沙箱（MemoryStore + MockFundsProvider），执行模式 shadow → live 两阶段验收。

## 1. 验收口径

| 执行模式 | 是否触真实资金 | Provider | 结果 |
|---|---|---|---|
| SHADOW | 否（模拟回执 `receipt.simulated=true`） | 无需 | `shadow_acceptance.json` 37/37 通过 |
| LIVE | 是（受控执行开关，调用 `MockFundsProvider`） | `MockFundsProvider` | `live_acceptance.json` 41/41 通过 |

- 口令：先跑 shadow（第一轮只生成待执行记录 + 模拟回执，不触真实资金），再运行业务沙箱 live（受控执行开关）。
- 完成标准之一"同一业务操作重复提交只产生一次外部执行"：shadow/live 均验证同一 `operation_id` → 同一 `execution_id` → 重复 `execute_operation` 与重复回调均为幂等重放，不新增执行记录。

## 2. 对账与补偿闭环总结

### 2.1 对账（Reconciliation）目标识别

`ExecutionEngine.list_reconciliation_targets(tenant_id)` 返回非终态/异常记录（`submitted` / `failed_uncertain` / `reconciling` / `failed_dispatch`），并额外优先返回 **overdue** 记录（`submitted` 且 `now - submitted_at > confirm_timeout_seconds`），`limit` 截断只丢弃未超时记录，**绝不遗漏超时未确认项**。

`reconcile(tenant_id)`：对每目标执行 `provider.query(external_txn_id)` 外部核实：

| 外部查询结果 | 处理 | 终态 |
|---|---|---|
| `succeeded/success` | 迁移到 `CONFIRMED` | `CONFIRMED`（operation=EXECUTED） |
| `failed/rejected` | 走 `_reconcile_failure` → 补偿 | `COMPENSATED`（补偿成功）或 `HUMAN_HANDOFF`（补偿失败） |
| 查询失败（网络/超时 `ProviderError`） | `_reconcile_mismatch` | `MISMATCHED`（operation=HUMAN_HANDOFF） |
| 外部状态未知 | `_reconcile_mismatch` | `MISMATCHED`（operation=HUMAN_HANDOFF） |
| 缺 `external_txn_id` / 无 provider | `_reconcile_mismatch`（fail-closed） | `MISMATCHED`（operation=HUMAN_HANDOFF） |

> 关键：任何"外部不可达、状态未知、金额不符、缺外部队列号"都不会被当作成功；一律 fail-closed 转人工（`HUMAN_HANDOFF` / `MISMATCHED`），保留完整执行记录与审计。

### 2.2 补偿（Compensation）

`_compensate(record)` 调用 `provider.compensate(execution_id, idempotency_key, amount)` 发起反向（回滚）。补偿以同一 `idempotency_key` + `execution_id` 发起，重放一致；结果收敛：

| 补偿结果 | 终态 | operation |
|---|---|---|
| `succeeded` | `COMPENSATED`（`compensation_status=compensated`，含 reversal 回执） | EXECUTED |
| `failed` | `COMPENSATION_FAILED` | HUMAN_HANDOFF（转人工对账） |

### 2.3 超时未确认（overdue）→ 强制对账/转人工

由本此验收补充实现（`execution_confirm_timeout_seconds` 从死参数真正生效）：
- `_is_overdue`：仅 `submitted` 且 `now - submitted_at > confirm_timeout_seconds` 判定 overdue（`submitted_at` 缺失不误判，交通用对账兜底）。
- `list_overdue_reconciliation_targets` / `reconcile`：overdue 记录**强制并入对账**并 `query` 外部核实。
- `_reconcile_one`：overdue 记录在 detail 加 `overdue_` 标记，强化可追踪性。

live 验收 L5/L6 实证：
- 外部可达且成功 → `reconcile` 收口为 `CONFIRMED`（operation=EXECUTED）。
- 外部不可达 → `MISMATCHED` + operation=HUMAN_HANDOFF（未静默当成功）。

## 3. 验收证据汇总

| 证据文件 | 断言 | 结果 |
|---|---|---|
| `evidence/shadow_acceptance.json` | 37 | 37/37 PASS |
| `evidence/live_acceptance.json` | 41 | 41/41 PASS |
| `tests/test_execution_engine.py`（overdue/幂等/回调/金额异常/补偿） | 20 用例 | 20/20 PASS |
| `tests/test_approval_idempotency.py`（拒绝不执行/幂等决策/绑定/超时） | 16 用例 | 16/16 PASS |

## 4. 场景计数：补偿与对账人工接管

| 场景 | 触发 | 收敛 | 转人工 |
|---|---|---|---|
| 外部明确失败（live L4） | provider result=failure | `COMPENSATED`（含 reversal 回执） | 否（已补偿回滚） |
| 回调金额异常（live L3） | callback amount ≠ record.amount | `MISMATCHED` | 是（HUMAN_HANDOFF） |
| 回调超时未确认 + 外部成功（live L5） | submitted 超过 confirm_timeout，query 成功 | `CONFIRMED` | 否（对账确认） |
| 回调超时未确认 + 外部不可达（live L6） | submitted 超过 confirm_timeout，query 失败 | `MISMATCHED` | 是（HUMAN_HANDOFF） |
| 回调重放（live L2） | 同 nonce 重投 | 保持 `CONFIRMED`，`applied=False reason=replay` | 否（幂等重放） |
| live 无 provider（live L7） | 无受控执行开关 | fail-closed 拒绝 | 是（拒绝执行，operation 保持 PENDING） |

## 5. 完成标准核验

| 完成标准 | 支撑 |
|---|---|
| 同一业务操作重复提交只产生一次外部执行 | shadow/live 幂等重放：同一 execution_id、`provider.submit` 仅调 1 次、执行记录不增 |
| 审批拒绝绝不执行 | `test_approval_idempotency.py`：拒绝→operation=REJECTED/HUMAN_HANDOFF，`list_execution_records` 为空（未达引擎/外部） |
| 回调异常、超时、金额异常全部转人工 | live L3（金额异常→MISMATCHED+HUMAN_HANDOFF）/ L5/L6（超时→对账收口或转人工）/ 回调验签失败→signature_invalid |
| 禁止低档模型/异常降级绕过审批 | 能力矩阵白名单门控 + 审批绑定一致性校验，未达写白名单一律拒绝转人工 |

---
*报告生成：AgentTeams `adapter-acceptance` 验收闭环。证据来源于成员产出（t1 审计 / t2 超时实现 / t3 测试 / t4 shadow / t5 live）并经船长复核。*
