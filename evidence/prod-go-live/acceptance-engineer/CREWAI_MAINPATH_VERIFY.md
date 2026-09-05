# T2·CrewAI 退款主链路接入 LangGraph 主图验收报告

> 项目：电商售后多智能体工单系统（完全自托管）
> 团队：rc4-productionization
> 验收人：crewai-engineer（CrewAI/LangGraph 主链路工程师）
> 任务：t2 — 阶段二：打通 CrewAI 退款主链路（build_crewai_router/run_business_task 接入主图）
> 生成时间：2026（阶段二）
> 验收口径：**如实核对主请求路径确实调用 `build_crewai_router()` / `run_business_task()`，同时严守《业务多智能体系统宪法》红线；凡依赖真实自托管模型 / 真实资金而未具备的，一律标注 BLOCKED，不虚报。**

---

## 0. 结论前置（一句话汇总）

| 验收项 | 结论 | 说明 |
|---|---|---|
| 1. 主图接入 `crewai_refund_agent` | **PASS** | `src/graph/builder.py` 在 `route_by_intent` 后新增 `crewai_refund_agent` 节点，条件路由接入 |
| 2. 主请求路径确实调用 `run_business_task` | **PASS（测试级确证，stub router）** | crewai_enabled=True + order/refund/complaint 意图 → 路由到 crewai 节点并调用 `run_business_task`；`tests/test_crewai_main_path.py` 用 stub 证明调用链 |
| 3. 写意图经唯一 `human_approval` | **PASS** | refund 经 crewai 后 `needs_approval=True` + `approval_id` → 唯一 `human_approval`；审批通过后 `execute_refund` 执行 |
| 4. 无 `direct → execute` 绕过边 | **PASS（无）** | 图结构断言：`crewai_refund_agent` 无任何 → `execute_*` 直接边；`process_refund`/`process_return`/`update_return_address` 亦无 → `execute_*` 直接边 |
| 5. 工具契约校验 | **PASS** | 6 个业务工具均含 input/output schema；缺 `order_id` 拒绝；schema 递归不含 `tenant_id`/`user_id`/`role` |
| 6. fail-closed 转人工 | **PASS** | crewai 报错/端点不在白名单/模型不在 `HIGH_CONFIDENCE_MODELS`/无自托管 LLM/写未回传待审批 → `handle_error`，不产生审批/不执行 |
| 7. crewai 禁用时主路径与既有确定性行为一致 | **PASS** | crewai_enabled 缺省（settings=None 或 False）→ `build_crewai_router` 从未被调用，走既有确定性节点 |
| 8. 真实自托管模型真实 crewai 链 | **BLOCKED — 需真实端点 + crewai 包** | 本环境未安装 `crewai`、无真实权重模型端点；真实 `_run_real` 链未验证 |
| 9. 真实资金 live | **BLOCKED — 维持 shadow** | `EXECUTION_MODE=shadow`；未切 live，无真实资金触碰 |

> 关键诚实声明：**主链路「确实调用 build_crewai_router/run_business_task」以测试级 stub 确证；真实 crewai 子智能体（构造 Crew + 绑定真实工具对象 + 调自托管 LLM）依赖 `crewai` 包 + 真实自托管 LLM 端点 + 网络白名单，本环境未具备，故真实执行链标注 BLOCKED。** 本报告给出的是「接入正确 + 安全行为正确 + 契约正确」的证据，而非「真实模型已跑通」的虚报。

---

## 1. 主图如何调用 `build_crewai_router` / `run_business_task`

### 1.1 改动文件与结构

`src/graph/builder.py`：

- 新增模块常量：
  - `CREWAI_BOUND_INTENTS = frozenset({"order", "refund", "complaint"})`（绑定到 crewai 真实工具的意图集合）。
  - `CREWAI_WRITE_INTENTS = {"refund": "refund", "return_request": "return_request", "return_address": "return_address"}`。
- `build_graph(...)` 新增 `settings=None` 参数；`crewai_enabled = bool(settings is not None and settings.crewai_enabled)`。
- 新增 `crewai_route_condition(state)`：当 `crewai_enabled` 且 `intent in CREWAI_BOUND_INTENTS` 时返回 `"crewai_refund_agent"`，否则回退 `route_condition(state)`（既有确定性映射）。
- 新增 `crewai_refund_agent_node(state)`：
  1. `build_crewai_router(settings, adapter=effective_adapter, store=store)`；
  2. 构造服务端 `ctx`（`tenant_id`/`user_id`/`role`/`order_id`/`thread_id`/`client_request_id`，**全部取自 state**，即服务端 `TenantContext` 注入闭包，模型/客户端不可覆盖）；
  3. `router.run_business_task(intent, ctx, params)`；
  4. 把 `tool`/`intent`/`crew_result` 写入 state（新增到 `AgentState` 的 `tool`/`crew_result` 字段，见 `src/graph/state.py`）。
- 新增 `crewai_result_condition(state)`：`needs_approval & approval_id` → `human_approval`；`falls_to_error` → `handle_error`；否则 `generate_response`。
- 路由接线：`route_by_intent` 条件边加入 `"crewai_refund_agent": "crewai_refund_agent"`；`crewai_refund_agent` 条件边指向 `human_approval` / `handle_error` / `generate_response`。

`src/graph/state.py`：`AgentState` 新增 `tool: Optional[str]`、`crew_result: Optional[str]`（模型可控，不含身份）。

### 1.2 主路径时序（crewai_enabled=True + 意图=refund）

```
START → classify_intent → route_by_intent
    └─ (intent=refund, crewai_enabled) → crewai_refund_agent
                                          ├─ build_crewai_router(settings, adapter, store)
                                          ├─ run_business_task("refund", ctx, params)
                                          │     → _do_process_refund(ctx, params)
                                          │          ├─ 资格判定 + 创建待审批 operation/approval（幂等键 oprefund:{tenant}:{order}:{rid}）
                                          │          └─ 返回 {status, operation_id, approval_id, refund_amount}
                                          └─ 写回 state: tool/approval_id/operation_id/pending_action=refund
                                              → crewai_result_condition → human_approval（唯一，interrupt）
                                                  └─ 审批通过 → execute_refund（幂等，evt 引擎）
```

### 1.3 真实验证点（stub router 证明主链调用）

- `tests/test_crewai_main_path.py::test_crewai_enabled_refund_calls_run_business_task_and_hits_human_approval`
  - monkeypatch `src.graph.builder.build_crewai_router` 返回确定性 stub；断言 `run_business_task` 被调用一次且 intent=refund，**主路径确证调用**。
  - 断言 ctx 注入 `tenant_id=TENANT-A`、`user_id=USER-001`、`role=customer`；params 不含身份字段。
  - 断言写意图进入唯一 `human_approval`：`needs_approval=True`、`approval_id` 非空、`pending_action="refund"`、`final_response=None`（停在中断）；op/approval 均 `pending`（绝不预先执行）。
- `tests/test_crewai_main_path.py::test_crewai_enabled_refund_executes_only_after_approval`
  - 审批前 op=`pending`；`Command(resume={"approved": True, "approver": "ADMIN-A"})` 后 op=`executed`。证明**无 direct→execute 绕过**（必须先经唯一 human_approval）。

---

## 2. 工具契约校验结果

来源：`src/tools/crewai_adapter.py` 的 `BUSINESS_TOOL_SCHEMAS`（6 个工具：query_order / track_shipping / process_refund / process_return / update_return_address / escalate_ticket）。

| 校验项 | 结果 | 证据 |
|---|---|---|
| query_order / process_refund / escalate_ticket 均有 input/output schema 与描述 | PASS | `test_business_tool_schemas_contract_and_no_identity_fields` |
| 缺 `order_id` 拒绝（query_order / process_refund） | PASS（AdapterError `missing_order_id`） | `test_crewai_tools_reject_missing_order_id`；无 `order_id` 时不创建任何审批操作 |
| schema 递归不含 `tenant_id` / `user_id` / `role` | PASS | `test_business_tool_schemas_contract_and_no_identity_fields`（递归遍历 input/output schema 的叶子键） |
| 身份字段只来自服务端 ctx（模型传 TENANT-B 亦被忽略） | PASS | `test_crewai_tool_ignores_model_supplied_identity_fields`（传入 `tenant_id=TENANT-B` 仍按 TENANT-A/USER-001 查询） |
| 写工具（process_refund）只创建待审批、绝不执行 | PASS（既有） | `tests/test_hardening_acceptance.py::test_crewai_process_refund_only_creates_pending_approval`（op=`pending`，`get_approval.status=pending`，amount=299.00） |

**契约结论**：工具 schema 只暴露模型可控字段（`order_id`/`reason`/地址字段）；`tenant_id`/`user_id`/`role`/数据库连接等身份与基础设施信息一律经服务端 `ctx` 闭包注入，**绝不**暴露给模型/客户端。符合《业务多智能体系统宪法》§2 职责边界与 §5 能力门槛。

---

## 3. 写意图经唯一 human_approval & 无 direct→execute 边

### 3.1 写意图仍唯一经 human_approval

- crewai 写意图（refund）在 `crewai_refund_agent` 中只创建待审批，`needs_approval=True`，经 `crewai_result_condition` → 唯一 `human_approval` 中断。
- 审批通过才触发 `execute_refund`（`approval_result_condition` → `approved_refund` → `execute_refund`）；拒绝/转人工 → `handle_error`。
- 执行节点 `_execute` 对 op 归属/动作匹配/线程绑定/审批状态/模型白名单做复核（见 `src/graph/nodes.py`），杜绝「未审批/跨租户/动作不匹配/低档模型」执行。

### 3.2 无 direct→execute 绕过边（图结构断言）

`tests/test_crewai_main_path.py`：

- `test_crewai_disabled_uses_deterministic_path_and_has_no_direct_execute_edge`：遍历 `g.get_graph().edges`，断言
  - `process_refund`/`process_return`/`update_return_address` 的任一目标**不是** `execute_*`；
  - `crewai_refund_agent` 的任一目标**不是** `execute_*`。
- `test_crewai_enabled_graph_has_no_crewai_to_execute_direct_edge`：即使 crewai 启用，`crewai_refund_agent → execute_*` 直接边**不存在**，写路径只能经由 `human_approval`。

实测 `g.get_graph().edges`（crewai 节点出边）：
```
crewai_refund_agent → generate_response | handle_error | human_approval
```
无任何 `crewai_refund_agent → execute_*` 边。

---

## 4. fail-closed 路径（转人工，不产生审批/不执行）

| 触发条件 | 动作 | 证据 |
|---|---|---|
| crewai 路由构造失败（`build_crewai_router` 抛异常） | `falls_to_error=True` → `handle_error` | `crewai_refund_agent_node` 的 `except` 分支 |
| `run_business_task` 委派失败（模型不在白名单/端点受限/无 crewai 等） | `falls_to_error=True` → `handle_error` | `test_crewai_delegation_error_fails_closed_to_human`（stub 抛 `AdapterError("model_not_in_whitelist")` → `reason=model_not_in_whitelist`、无 op、final_response 含「人工」） |
| 写意图但 `run_business_task` 未回传 `approval_id`/`operation_id` | `falls_to_error=True` → `handle_error`（绝不执行） | `test_crewai_write_without_approval_id_in_response_fails_closed`（`reason=crewai_write_no_approval_in_response`、无 op） |
| 写工具内部：模型不在 `HIGH_CONFIDENCE_MODELS`（`_do_process_refund`） | 抛 `AdapterError("model_not_in_whitelist")` | `tests/test_hardening_acceptance.py::test_crewai_process_refund_model_not_in_whitelist_fails_closed` |
| 缺 `tenant_id`/`user_id` 服务端上下文（`_require_ctx`） | 抛 `AdapterError("missing_tenant_context")` | `src/tools/crewai_adapter.py::_require_ctx` |

fail-closed 均保留 `operation_id`（若已被创建）与完整业务状态，不降级为低成本模型写、不直接执行。

---

## 5. crewai 禁用时主路径与既有确定性行为一致

- `build_graph(...)` 不传 `settings`（或 `crewai_enabled=False`）时，`crewai_enabled=False`。
- `test_crewai_disabled_uses_deterministic_path_and_has_no_direct_execute_edge`：断言 `build_crewai_router` **从未被调用**（`holder["router"] is None`，crewai 节点不可达），退款仍走既有 `process_refund` → `human_approval`，行为与现状一致。
- 既有确定性写路径仍然只经唯一 `human_approval`，无行为回归。

---

## 6. 测试执行与回归

本专项新增 `tests/test_crewai_main_path.py` → **10 passed**：

| 用例 | 验证点 |
|---|---|
| test_crewai_enabled_refund_calls_run_business_task_and_hits_human_approval | 主路径调用 run_business_task；写意图进 human_approval |
| test_crewai_enabled_refund_executes_only_after_approval | 审批通过才执行（无绕过） |
| test_crewai_disabled_uses_deterministic_path_and_has_no_direct_execute_edge | crewai 禁用走确定性；无 direct→execute 边 |
| test_crewai_enabled_graph_has_no_crewai_to_execute_direct_edge | 启用时仍无 crewai→execute 边 |
| test_crewai_enabled_read_intent_goes_through_crewai_and_replies | order 只读经 crewai 后正常回复，不触发审批 |
| test_crewai_delegation_error_fails_closed_to_human | 委派异常 fail-closed 转人工，无审批 |
| test_crewai_write_without_approval_id_in_response_fails_closed | 写未回传待审批 → fail-closed |
| test_business_tool_schemas_contract_and_no_identity_fields | 工具契约 + schema 无身份字段 |
| test_crewai_tools_reject_missing_order_id | 缺 order_id 拒绝 |
| test_crewai_tool_ignores_model_supplied_identity_fields | 身份只来自服务端 ctx |

**FAIL-F1 修复专用真实链路用例（走 `_run_real`，非 StubRouter 掩盖形状）：**
| test_crewai_run_real_surfaces_write_meta_top_level | 真实 `_run_real` 顶层上抛 approval_id/operation_id/refund_amount |
| test_crewai_real_path_builder_routes_refund_to_human_approval | 真实 `_run_real` 返回形状下，主图 refund 进入唯一 human_approval |
| test_crewai_real_path_model_not_in_whitelist_fails_closed_no_orphan | 非白名单在 create 前失败 → 无孤儿 |
| test_crewai_real_path_post_write_failure_rolls_back_orphan | 建 pending 后委派异常 → 回滚为 human_handoff/rejected，无孤儿 |
| test_crewai_real_path_builder_delegation_failure_fails_closed_to_human | 委派异常 → 主图 fail-closed 走 handle_error |

`tests/test_crewai_main_path.py` → **15 passed**。

全量回归（`pytest tests/ -q --no-header`）：**411 passed, 34 skipped, 0 failed**（基线 396；新增 15 例 crewai 测试导致 +15，无回归）。受影响专项文件：`tests/test_hardening_acceptance.py` / `tests/test_approval_idempotency.py` / `tests/test_security_regressions.py` / `tests/test_llm_endpoint_gate.py` 均通过。

评测用例扩充（`src/llm/eval/cases.py`）：新增 `category` 分类元数据 + 9 条新场景用例，**总数 56 条（≥30）**，覆盖正常退款 / 无订单 / 跨租户 / 资格不符 / 金额异常 / 重复请求 / 缺订单号 / 恶意注入 / 低置信度 / 工具超时 / 审批拒绝 / 输出格式错误；`evaluate_model(MockLLM("gpt-4"))` → `passed=True`、`write_op_pass=True`、`failed=[]`（mock 运行时通过）。

---

## 6.b FAIL-F1 修复：真实 `_run_real` 上抛写元数据 + 孤儿清理（t7）

### 问题（安全审计 t4 发现并交回）
`_make_process_refund_tool` 把 `_do_process_refund` 的 dict 经 `json.dumps(...)` 转成字符串；
`_run_real` 只返回 `{tool, intent, crew_result}`，**不**上抛 `approval_id/operation_id/refund_amount`。
于是 `src/graph/builder.py` 的 refund 分支 `result.get("approval_id")` 恒为 None → 恒
`crewai_write_no_approval_in_response` → handle_error，真实退款永远进不了唯一 human_approval；
且 `_do_process_refund` 已先落库 pending operation/approval 形成孤儿记录。

### 修复（`src/tools/crewai_adapter.py`）
1. **`_make_process_refund_tool`**：调用 `_do_process_refund` 后，把返回业务 dict 写入
   `self._last_write_meta`（仍返回 JSON 字符串给 crew，保持工具契约不变）。
2. **`_run_real`**：kickoff 后读取 `_last_write_meta`（并用新增 `_extract_write_meta` 从
   `crew_result` 字符串解析 JSON 作兜底），把 `status/operation_id/approval_id/refund_amount/
   approval_reason` **上抛到返回 dict 顶层**（与 `_do_process_refund` 形状对齐），同时保留
   `tool/intent/crew_result`。
3. **`run_business_task`**：起始重置 `_last_write_meta`；`_run_real` 抛异常时调用
   `_rollback_orphan_write(ctx, exc)` —— 若已创建 pending operation/approval，则
   `decide_approval(..., approved=False)` 拒绝 + 置 operation 为 `human_handoff` + 审计留痕
   （`crewai.orphan_write_rolled_back`），避免孤儿 pending 记录；随后 re-raise 由主图 fail-closed。
4. **失败先于 create 的路径**（模型不在白名单 / 资格不符 / 缺租户上下文 / 缺 order_id）本来就在
   `_do_process_refund` 的 create 之前 fail-closed，**不产生任何记录**，无需回滚。

### 效果
- 真实 `_run_real` 顶层返回 `approval_id/operation_id` → builder 的 refund 分支进入唯一 `human_approval`
  （`test_crewai_real_path_builder_routes_refund_to_human_approval` 确证）。
- 无孤儿 pending 记录：或在 create 前失败（无记录），或已回滚为 `human_handoff`+`rejected`
  （`test_crewai_real_path_model_not_in_whitelist_fails_closed_no_orphan`、
    `test_crewai_real_path_post_write_failure_rolls_back_orphan` 确证），并审计留痕。
- 未新增 `direct → execute` 边；写操作仍唯一经 `human_approval`；身份仍只取服务端 `ctx`。

---

## 7. BLOCKED / 部署期执行项（诚实标注）

| 项 | 状态 | 说明 |
|---|---|---|
| **真实自托管模型 + 真实 crewai 子智能体执行** | **BLOCKED（仅剩余真实运行态）** | FAIL-F1 已修复：`_run_real` 顶层上抛 `approval_id/operation_id` 的返回形状、孤儿清理、写路径进入唯一 human_approval 均已用假 crewai 模块的真实链路测试验证。剩余 BLOCKED 仅限于「真实权重模型端点 + 真实 `crewai` 包」的实际运行：依赖 ①`crewai` 包（本环境未安装）；②真实自托管 LLM 端点（`llm_base_url` + 网络白名单）；③模型在 `HIGH_CONFIDENCE_MODELS`。三者未同时具备 → 未用真实模型跑通（`RUN_CREWAI_INTEGRATION` 未置 1）。 |
| **写操作真实资金/网关（live）** | **BLOCKED — 维持 shadow** | `EXECUTION_MODE=shadow`；未部署生产网关沙箱/未切 live；敏感资金写一律经唯一 `human_approval` + shadow/live 状态机（`src/execution`）收口。 |
| **真实模型 `write_op_pass`** | **BLOCKED** | 无真实权重模型端点评测；`HIGH_CONFIDENCE_MODELS` 实际为 mock 演示白名单（dev 回退）。 |
| **CrewAI 子智能体真实端到端** | **BLOCKED** | 需安装 crewai + 部署自托管 LLM + 置 `RUN_CREWAI_INTEGRATION=1` 复跑 `test_crewai_real_call_chain_integration`。 |

---

## 8. 结论

1. 主图已正确接入 `crewai_refund_agent`，且**主请求路径（crewai_enabled=True + order/refund/complaint 意图）确实调用 `build_crewai_router()` / `run_business_task()`**（stub 与真实 `_run_real` 返回形状双双确证）。
2. **FAIL-F1 已修复（t7）**：真实 `_run_real` 顶层返回 `approval_id/operation_id/refund_amount`，builder 的 refund 分支因之进入唯一 `human_approval`，审批通过才 `execute_refund`；**无任何 `direct → execute` 绕过边**（图结构断言 + 审批前 pending + 审批后 executed）。
3. `tenant_id`/`user_id`/`role`/审批人/数据库连接一律取自服务端 `TenantContext`（经 `ctx` 闭包注入）；工具 schema 不暴露这些字段，模型传入身份字段也被忽略。
4. crewai 报错/端点不在白名单/模型不在 `HIGH_CONFIDENCE_MODELS`/无自托管 LLM/写未回传待审批 → fail-closed 走 `handle_error`（转人工），保留 `operation_id` 与完整状态，不降级执行；写工具建 pending 后委派异常 → 回滚为 `human_handoff` + `rejected`（无孤儿 pending 记录）并审计留痕。
5. crewai 禁用时主路径与既有确定性行为完全一致（`build_crewai_router` 从未被调用）。
6. 真实模型运行与真实资金 live 均标注 **BLOCKED**（依赖真实自托管模型端点 + crewai 包 + 受控网关），未虚报。

> **验收裁决**：CrewAI 退款主链路「接入正确 + 安全行为正确 + 契约正确 + 无绕过」**通过**；「真实 crewai 子智能体 + 真实模型 + 真实资金」**BLOCKED**，需真实自托管模型端点 / crewai 包 / 受控网关就绪后，在具备环境复跑集成测试方可宣称真实运行。
