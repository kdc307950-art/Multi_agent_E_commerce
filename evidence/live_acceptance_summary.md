# 业务沙箱 LIVE 验收证据

- 验收口径：`live_acceptance`，`execution_mode=live`
- 断言总数：41；通过：41；失败：0；全部通过：是
- 生成时间：2026-09-04T02:09:24+0800

## 场景明细

### live_valid_callback_and_replay

| 步骤 | 断言 | 结果 | 实际 |
|---|---|---|---|
| submit | status == SUBMITTED | PASS | ExecutionStatus.SUBMITTED |
| submit | external_txn_id present | PASS | True |
| submit | execution record status == SUBMITTED | PASS | ExecutionStatus.SUBMITTED |
| submit | execution record external_txn_id present | PASS | True |
| callback_confirm | applied is True | PASS | True |
| callback_confirm | status == CONFIRMED | PASS | confirmed |
| callback_confirm | operation.status == EXECUTED | PASS | OperationStatus.EXECUTED |
| callback_confirm | execution record status == CONFIRMED | PASS | ExecutionStatus.CONFIRMED |
| replay | applied is False | PASS | False |
| replay | reason == replay | PASS | replay |
| replay | execution record remains CONFIRMED | PASS | ExecutionStatus.CONFIRMED |
| replay | no new execution record | PASS | 1 |

### live_callback_amount_mismatch_human

| 步骤 | 断言 | 结果 | 实际 |
|---|---|---|---|
| amount_mismatch | applied is False | PASS | False |
| amount_mismatch | reason == amount_mismatch | PASS | amount_mismatch |
| amount_mismatch | execution record status == MISMATCHED | PASS | ExecutionStatus.MISMATCHED |
| amount_mismatch | operation.status == HUMAN_HANDOFF | PASS | OperationStatus.HUMAN_HANDOFF |

### live_external_failure_compensated

| 步骤 | 断言 | 结果 | 实际 |
|---|---|---|---|
| compensate | status == COMPENSATED | PASS | ExecutionStatus.COMPENSATED |
| compensate | compensation_status == compensated | PASS | compensated |
| compensate | operation.status == EXECUTED | PASS | OperationStatus.EXECUTED |
| compensate | receipt provides reversal | PASS | True |

### live_timeout_unconfirmed_reconcile_confirm

| 步骤 | 断言 | 结果 | 实际 |
|---|---|---|---|
| submit | status == SUBMITTED | PASS | ExecutionStatus.SUBMITTED |
| reconcile_timeout | overdue record scanned | PASS | True |
| reconcile_timeout | reconciled >= 1 | PASS | True |
| reconcile_timeout | execution record status == CONFIRMED | PASS | ExecutionStatus.CONFIRMED |
| reconcile_timeout | operation.status == EXECUTED | PASS | OperationStatus.EXECUTED |

### live_timeout_unconfirmed_unreachable_human

| 步骤 | 断言 | 结果 | 实际 |
|---|---|---|---|
| submit | status == SUBMITTED | PASS | ExecutionStatus.SUBMITTED |
| reconcile_timeout | overdue record scanned | PASS | True |
| reconcile_timeout | mismatch >= 1 | PASS | True |
| reconcile_timeout | execution record status == MISMATCHED | PASS | ExecutionStatus.MISMATCHED |
| reconcile_timeout | operation.status == HUMAN_HANDOFF | PASS | OperationStatus.HUMAN_HANDOFF |

### live_without_provider_fail_closed

| 步骤 | 断言 | 结果 | 实际 |
|---|---|---|---|
| no_provider | live 无 provider → fail-closed 拒绝 | PASS | DomainError: live 执行模式必须配置 FundsProvider（受控执行开关未开启）。 |
| no_provider | operation NOT executed | PASS | OperationStatus.PENDING |

### e2e_refund_approval_executed_live

| 步骤 | 断言 | 结果 | 实际 |
|---|---|---|---|
| create_session | session created (201) | PASS | 201 |
| create_session | thread_id present | PASS | True |
| request_refund | approval_required event emitted | PASS | True |
| request_refund | approval status == pending | PASS | pending |
| request_refund | approval_id present | PASS | True |
| request_refund | operation_id present | PASS | True |
| approve | decision http 200 | PASS | 200 |
| approve | decision operation_id matches | PASS | 1cecae82-9fad-47dc-8abe-b334f413d08d |
| query_operation | operation.status == EXECUTED | PASS | executed |

**结论：通过** （41/41）
