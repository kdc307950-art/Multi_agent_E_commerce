# 端到端审批记录（End-to-End Approval Record）

> 交付物：退款/退货/改址敏感写操作"仅经唯一 `human_approval` interrupt、审批拒绝绝不执行"的端到端证据。
> 证据来源：`tests/test_e2e_flow.py`（2 passed，产出 `evidence/e2e_flow.json`）与 `evidence/live_acceptance.json` 的 `e2e_refund_approval_executed_live` 场景。
> 校验：所有敏感写路径仅由唯一 `human_approval` 节点承接，无 direct→execute 绕过（见 `src/graph/builder.py`、`src/graph/approval.py`、`src/graph/nodes.py`）。

## 1. 图结构无绕过证明

`src/graph/builder.py`：
- `process_refund` / `process_return` / `update_return_address` 三条敏感写节点，均通过 `write_approval_condition`：
  - 仅当 `needs_approval=True` 且 `approval_id` 存在 → 进入唯一 `human_approval`；
  - 否则（资格/参数/能力校验失败）→ `handle_error`（转人工），绝不直接进 `execute_*`。
- `human_approval` 经 `approval_result_condition`：仅 `approved` 才按 `pending_action` 路由到对应 `execute_*`；`rejected` → `handle_error`。
- 全图**无任何** `direct -> execute_*` 边；敏感写只能由审批通过触发。

`src/graph/nodes.py` `_write_action`：创建待审批前先做能力矩阵白名单门控、租户/归属/资格/金额校验，任一失败即 `needs_approval=False` + `falls_to_error`（转人工），不进入审批更不执行。

`src/graph/approval.py` `approval_node`：`interrupt(payload)` 唯一人工审批入口；恢复参数非法（resume 校验失败）→ fail-closed 置 REJECTED，绝不进入执行。

## 2. 审批链路流水（端到端证据）

### 2.1 会话与意图分类

| 步骤 | 结果 | 证据 |
|---|---|---|
| 创建会话（customer） | 201，返回 `thread_id` | e2e_flow.json `create_session` |
| 查询政策 RAG / 查询订单 | done 事件，final_response | e2e_flow.json `query_policy`（27 事件）、`query_order` |

### 2.2 敏感写申请（退款）→ 唯一审批中断

| 字段 | 值 | 说明 |
|---|---|---|
| `intent` | `refund` | `MockLLM.classify_intent`，confidence 0.92（>0.7 阈值） |
| `order_id` | `ORD-001` | 归属校验通过（USER-001 本人订单） |
| 资格 | 通过 | `check_refund_eligibility`：delivered 且在退款窗口内、金额合法、类别可退 |
| 幂等键 | `oprefund:TENANT-A:ORD-001:E2E-REFUND` | 租户级（tenant_id+order_id+client_request_id），**不含 attempt** |
| `operation_id` | `b0681446-…` | 唯一业务操作标识 |
| `approval_id` | `7d390a90-…` | 审批单标识 |
| 事件 | `approval_required`，`status=pending` | 进入唯一 human_approval |

### 2.3 唯一人工审批（admin / approver）

| 字段 | 值 | 说明 |
|---|---|---|
| `interrupt(payload)` | `{tenant_id, approval_id, operation_id, pending_action, order_id, amount(299.0), reason, question}` | 唯一审批入口，无 direct 绕过 |
| 审批角色 | admin（`ADMIN-A`） | 仅租户内 `admin`/`approver` 可审批；非审批角色返回 403 |
| 二次确认 | `confirmation=True` | 缺失二次确认 → 422 |
| 决定 | `approved=True` | 写回 `decide_approval`（CAS 幂等，防并发双执行） |
| outcome | HTTP 200，`operation_id` 匹配，`status=executed` | 审批通过后执行 |

### 2.4 执行 → operation 终态

| 字段 | 值 | 说明 |
|---|---|---|
| 执行模式 | shadow（API 默认）；live 验收场景亦覆盖 | `mode=shadow` |
| `execution_id` | `55251342-…` | 执行记录 |
| `external_txn_id` | `txn-c929ed6367bb1eeb` | 由幂等键派生（同幂等键同号） |
| `amount` | 299.0 | = 订单实付 |
| `status` | `executed` | operation 终态 |
| `result.receipt` | `simulated=true`（shadow）/ `confirmed`（live 回调确认） | 回执 |

### 2.5 会话历史

`message_count=3`（e2e_flow.json `session_history`）：对话消息已持久化，供客服/审计回溯。

## 3. 审批拒绝绝不执行（证据）

`tests/test_approval_idempotency.py`：
- `test_rejected_approval_does_not_execute`：`approved=False` → operation `status != executed`。
- `test_execute_rejects_when_not_approved`：未 approved → `execute_refund` 返回 `APPROVAL_BINDING_MISMATCH`，operation 未 EXECUTED。
- `test_execute_rejects_action_mismatch` / `test_execute_rejects_model_not_in_whitelist`：动作不匹配 / 低档模型 → 拒绝，且（强化后）`store.list_execution_records("TENANT-A") == []`（未到达执行引擎与外部资金）。
- `test_concurrent_decision_only_one_claims`：并发审批仅一个真正抢占成功（CAS），其余重放不重复生效。

全部 16 用例 PASS。

## 4. 完成标准核验

| 完成标准 | 证据 |
|---|---|
| 审批拒绝绝不执行 | `test_approval_idempotency.py` 16/16 PASS：拒绝/未审批/动作不匹配/低档模型均在执行前阻断 |
| 敏感写仅经唯一 human_approval | 图结构无 direct→execute 边；唯 `human_approval` 中断承接 |
| 端到端 admin 审批→执行 | e2e_flow.json + live e2e 场景：审批通过(二次确认)→operation=executed |

---
*记录生成：AgentTeams `adapter-acceptance` 验收闭环。端到端流程由船长用 `tests/test_e2e_flow.py` 复现（2 passed）并核对 live 验收 e2e 场景。*
