# CREWAI_WIRING_SECURITY_AUDIT — CrewAI 主链路跨切面安全/合规审计

> **审计者**：security-auditor（角色：安全合规审计（跨切面））
> **对象**：crewai-engineer 于任务 t2 接入的 CrewAI 主链路
> **范围文件**：`src/graph/builder.py`、`src/tools/crewai_adapter.py`、`src/llm/eval/cases.py`、`tests/test_crewai_main_path.py`
> **关联红线**：《编码 Agent 工作宪法》+《业务多智能体系统宪法》（安全红线 1-8、职责边界、HITL、能力矩阵、数据完整性与幂等、fail-closed）
> **测试实跑**：`.venv\Scripts\python.exe -m pytest tests/test_crewai_main_path.py -q` → **10 passed in 0.33s**（EXIT=0）✅
> **对照基线**：`release/v1.0.0-rc4-candidate` 权威捕获 `396 passed, 34 skipped`（430 collected，见 `evidence/prod-go-live/test-runner/pytest_captain_baseline.log`）
>
> **标注约定**：✅ PASS · ❌ FAIL · ⚠️ 待办/风险 · 🔒 BLOCKED-需外部（真实自托管模型/真实资金/真实租户/目标服务器，禁止虚构）

---

## TL;DR（逐项汇总）

| # | 审计项 | 结论 | 关键依据 |
|---|---|---|---|
| 1 | 人工审批不被绕过 | ✅ PASS | 图无 `crewai_refund_agent→execute_*` 直接边；写路径唯一进 `human_approval`；approval 用 `interrupt()`；**但真实链路 fail-closed（见漏点 F1）** |
| 2 | 租户边界不可绕过 | ✅ PASS（API 认证路径 🔒 需外部） | tools schema/工具签名不暴露 tenant/user/role；身份仅服务端 ctx 注入；写工具做归属校验 |
| 3 | 完全自托管红线 | ✅ PASS（⚠️ 通配符白名单待办） | `_crewai_llm` 强制本地端点 + `EndpointGuard` 网络白名单，无端点/不在白名单 fail-closed；不接公有 SaaS |
| 4 | 模型能力门控 | ✅ PASS | `capability_ok` = 模型名显式白名单（非分数阈值）；生产空白名单 → 全写转人工 |
| 5 | 数据面租户隔离与租户级幂等 | ✅ PASS | 幂等键含 tenant_id；`UNIQUE(tenant_id,idempotency_key)`；`set_config` 同事务；RLS + FORCE/NOBYPASSRLS |
| 6 | fail-closed | ✅ PASS | 构造/委派/无待审批/模型不在白名单/资格/金额/无文档/端点失败 → `handle_error` 转人工 |
| 7 | **真实 CrewAI 写路径接线缺陷** | ❌ **FAIL**（安全上 fail-closed 不构成绕过，但功能断裂 + 产生孤儿审批/操作记录） | `_run_real` 未把 `approval_id/operation_id` 上抛到顶层，builder 恒 `crewai_write_no_approval_in_response` → 转人工；测试用 stub 掩盖 |
| 8 | 破坏既有红线 / 396-34 基线 | ✅ 未破坏红线（F1 为 fail-closed 安全）；⚠️ 测试数与基线口径见 §8 | 无新增绕过边；无 tenant 字段暴露；图结构/幂等/RLS 未变更 |
| 9 | 真实依赖（BLOCKED） | 🔒 真实自托管模型端点+评测、真实资金链路、真实租户确认、目标服务器验证 → 需外部 | 见 §9 |

---

## 1. 人工审批不被绕过 —— ✅ PASS（附安全分析）

**结论：未发现任何 `direct → execute_*` 绕过边；写路径唯一进入 `human_approval`。**

证据：

- **图结构（builder.py）**：
  - `crewai_refund_agent` 的后置条件为 `crewai_result_condition`，其可到目标仅为 `human_approval`（approve）/`handle_error`/`generate_response`（`builder.py:216-219`），**不含任何 `execute_*`**。
  - 确定性写节点 `process_refund`/`process_return`/`update_return_address` 的后置 `write_approval_condition` 仅为 `human_approval`（approve）/`handle_error`（`builder.py:223-234`），同样无 `execute_*`。
  - `human_approval` 的后置 `approval_result_condition` 只有 `approved_*→execute_*` 或 `rejected→handle_error`（`builder.py:235-243`）。
  - 图中 `execute_refund/execute_return/execute_address_update` 的**唯一入边**来自 `human_approval`（`builder.py:238-241`）。
- **审批节点是真实中断**：`make_approval_node` 用 LangGraph 的 `interrupt(payload)`（`approval.py:32`），恢复参数经 `validate_resume_params` 严格校验，非法即 `decide_approval(..., False, "resume_params_invalid")` 拒绝（`approval.py:34-41`）。审批通过后才由 `approval_result_condition` 路由到 `execute_*`。**无代码捷径。**
- **CrewAI 写工具只创建待审批**：`_do_process_refund` 只 `store.create_operation(...)` + `store.create_approval(...)`，返回 `status=pending_approval`，**绝不执行**（`crewai_adapter.py:237-272`）。
- **图结构测试**：`test_crewai_disabled_uses_deterministic_path_and_has_no_direct_execute_edge`（`test_crewai_main_path.py:166-172`）与 `test_crewai_enabled_graph_has_no_crewai_to_execute_direct_edge`（`:175-183`）断言 `crewai_refund_agent`/写节点**没有任何以 `execute` 开头的目标边**，且 `human_approval` 在写入边中。

> **安全定性（重要）**：结论 1 是 PASS。**但**真实 CrewAI 写链路目前**并未真正走到** `human_approval`——而是恒 fail-closed（见 **漏点 F1，§7**）。这不构成绕过/fail-open，而是"功能断裂但安全"（fail-closed）。顶层图安全模型未被破坏。

---

## 2. 租户边界不可绕过 —— ✅ PASS

**结论：tenant_id / user_id / role 只来自服务端上下文；CrewAI 工具 schema 与调用不暴露也不接受这些字段；写工具做订单归属校验（跨租户拒绝）。**

证据：

- **工具 schema 不含身份字段**：`BUSINESS_TOOL_SCHEMAS`（`crewai_adapter.py:36-145`）中每个工具的 `input_schema`/`output_schema` 只含业务参数（`order_id/reason/receiver_name/phone/region/detail`），**无 `tenant_id`/`user_id`/`role`**。测试 `test_business_tool_schemas_contract_and_no_identity_fields` 递归收集 schema 叶子并断言不含身份键（`test_crewai_main_path.py:275-290`）。
- **工具函数签名同样只暴露业务参数**：`query_order(order_id)`、`process_refund(order_id, reason="")`、`escalate_ticket(reason="")`（`crewai_adapter.py:282-303`）。模型无法通过签名传入身份字段。
- **身份仅服务端 ctx 注入**：工具对象经由闭包捕获调用方注入的 `ctx`，内部用 `_require_ctx(ctx,"tenant_id"/"user_id")` 读取（`crewai_adapter.py:160-165, 226-245`）；`ctx` 由构建期 `crewai_refund_agent_node` 从**图状态**（即服务端认证后的 `TenantContext`）组装（`builder.py:131-139`），模型/客户端不可覆盖。测试 `test_crewai_tool_ignores_model_supplied_identity_fields` 断言模型传入的 `tenant_id="TENANT-B"` 被忽略、以服务端 ctx 为准（`test_crewai_main_path.py:310-321`）。
- **写工具做订单归属校验**：`check_refund_eligibility → get_order → _assert_ownership`：`raw["tenant_id"] != tenant_id → cross_tenant_denied`；非本人且非 staff → `order_not_owned`（`adapter.py:206-210`）。`execute_operation` 亦复核 `op.tenant_id != tenant_id → cross_tenant_denied`（`adapter.py:191-193`）。`get_shipping` 对"不存在/无权"返回 None 规避信息泄露（`adapter.py:105-112`）。
- **state 仅新增模型可控、非身份字段**：`tool`/`crew_result` 为 CrewAI 主链路新增字段，注释明确"不含 tenant/user/role 身份"（`state.py:65-67`）。它们不会影响审批路由（路由由 `intent` + `approval_id/operation_id` 决定）。
- **参数层 strict/extra-forbid 兜底**：写参经严格 schema（`tool_require_strict_schema=True`，`config.py:200`）；评测集 `params-address-extra-tenant`（`cases.py:128-132`）覆盖"携带非法 tenant_id → extra=forbid 拒绝"。

> 🔒 **BLOCKED-需外部**：本审计**未验证 API 层**如何把认证后的 `TenantContext` 写入图状态 `tenant_id/user_id/role`（即"绝不从 `thread_id` 解析身份、绝不信任前端传参"）。t2 的接线本身不信任客户端身份（schema/工具不暴露、`_require_ctx` 缺失即拒绝），该"服务端认证 → 注入 state"环节属于 API/鉴权层，已由既有验收覆盖；如需在本证据中闭环此点，须由安全/认证侧提供 API 层证据（超出本 4 文件范围）。

---

## 3. 完全自托管红线 —— ✅ PASS（附 ⚠️ 待办）

**结论：CrewAI 只允许本地/内网 OpenAI 兼容端点，且端点须在网络白名单内；无端点/不在白名单 → fail-closed；不接公有 SaaS；无未经批准遥测。**

证据：

- **强制本地端点**：`_crewai_llm`（`crewai_adapter.py:324-349`）：
  - `settings is None` → 抛 `CrewAIIntegrationError("...拒绝使用公有 SaaS 默认端点")`；
  - `base_url` 为空 → 同样拒绝；
  - 构造 `EndpointGuard(llm_allowed_host_list, restricted=is_restricted_env)`，`guard.allowed(base_url)` 不通过 → 抛 `CrewAIIntegrationError("...不在自托管网络白名单内")`。
  - **绝不回退到任何公有 SaaS 默认端点**。
- **EndpointGuard**（`security.py:53-113`）：`base_url` 空 → False；无显式白名单时：受限环境 → False（fail-closed），非受限 → 仅 loopback/RFC1918 私网（`_is_private`，`security.py:92-104`）；命中白名单规则才放行。
- **配置默认安全**：`llm_base_url=http://localhost:8001/v1`、`llm_allowed_hosts=""`（`config.py:150-157`）；`crewai_enabled=False`（`config.py:226`，默认关闭，走确定性路径）。受限环境（preview/production，`config.py:254-256`）空白名单 → 端点拒绝 → fail-closed。
- **遥测**：本模块不引入任何外部遥测；CrewAI/LLM 端点外联由 `EndpointGuard` + 网络出口策略约束。

> ⚠️ **待办（非当前绕过，属硬化风险）**：`EndpointGuard._matches` 允许白名单通配规则 `"*"`、`"0.0.0.0/0"`、`"::/0"`（`security.py:80-81`）。若部署配置把 `llm_allowed_hosts` 设为任一通配符，`restricted` 环境下**仍会放行任意公网端点**，从而击穿完全自托管红线（`restricted` 仅在无白名单时生效，见 `security.py:69-73`）。**建议**：`is_restricted_env=True` 时禁用/拒绝通配符规则（或在配置加载期校验），并补充一个"受限环境通配符白名单必须失败"的用例。此点不变更现状（默认空白名单即 fail-closed），故记为待办而非 FAIL。

> 🔒 **BLOCKED-需外部**：真实自托管模型权重端点是否可达/已评测，属"真实 LLM 权重端点+评测"（G5/BL边界4），须由部署/评测侧提供。本审计仅验证代码在"无端点/不在白名单"时 fail-closed，**不虚构真实端点可用性**。

---

## 4. 模型能力门控 —— ✅ PASS

**结论：写工具只允许 `HIGH_CONFIDENCE_MODELS` 白名单模型；名单外 → 拒绝写转人工；门控依据是模型名显式白名单，而非分数阈值。**

证据：

- **唯一权威解析口**：`src/llm/capability.py`。
  - `capability_ok(model, settings) = model in resolve_high_confidence_models(settings)`（`capability.py:84-86`）——**集合成员判断（白名单），非分数阈值**。
  - `resolve_high_confidence_models`：显式白名单 `high_confidence_models`（CSV）；为空且受限 → `frozenset()`（fail-closed），为空且开发 → 演示默认（`capability.py:53-81`）；受限环境且无评测报告 → `frozenset()`（`capability.py:74-76`）；有效 = 显式白名单 ∩ `write_op_pass=true` 评测集合（`capability.py:79-81`）。
- **CrewAI 写门控**：`_write_capability_ok`（`crewai_adapter.py:213-221`）在 `_do_process_refund` 最前调用（`crewai_adapter.py:239-240`），不在白名单 → `raise AdapterError("model_not_in_whitelist")` → 上游 fail-closed 转人工，**绝不写**。
  - `build_crewai_router` 传 `llm=None`（`crewai_adapter.py:421`），故真实链路走 `capability_ok(settings.effective_llm_model, settings)`。`effective_llm_model = crewai_model or llm_model`（`config.py:263-265`）；`_crewai_llm` 同样取 `effective_llm_model`（`crewai_adapter.py:337`），**门控模型与执行模型一致**，无"用 X 模型写、Y 模型门控"的错位。
- **生产 fail-closed**：`high_confidence_models=""`（`config.py:170`）→ 受限环境 `resolve_high_confidence_models` 返回空集 → 任何模型写都失败转人工。符合"名单外一律拒绝写并转人工"。
- **评测集覆盖**：`WRITE_OP_CASES` 含"非白名单：写必须转人工"与"白名单：写进入审批"（`cases.py:245-263`），并由 runner 以模型名白名单判定。

> ⚠️ 小待办（非安全问题，保守 fail-closed）：`_write_capability_ok` 在 `settings` 存在时用 `settings.effective_llm_model`；若该值恰为空串而 `llm_model` 非空，则门控用空串（不在白名单 → 拒写），而 `_crewai_llm` 用 `llm_model`。该不一致**只导致更保守的拒写**（fail-closed，安全），但可能让"已配置模型却无法写"。建议部署侧保证 `crewai_model==llm_model` 或在此用同一取值来源。

> 🔒 **BLOCKED-需外部**：真实模型是否通过**写操作专项评测**（`write_op_pass=true`，即 `llm_eval_report_path` 报告），属"真实权重模型评测"（G5），须由评测侧提供。本审计不虚构评测结论。

---

## 5. 数据面租户隔离与租户级幂等 —— ✅ PASS

**结论：写操作唯一键含 tenant_id；数据库带唯一约束/原子写入防跨租户同名覆盖；PostgreSQL/RLS 作用域守卫在 SQL 同事务生效。**

证据：

- **幂等键含租户**：`generate_operation_key(action, tenant_id, order_id, request_id)` 返回 `{prefix}:{tenant_id}:{order_id}:{request_id}`（`types.py:234-237`），key 来自 `_do_process_refund`（`crewai_adapter.py:259`）。**绝不含重试次数/attempt**。
- **唯一约束 + 原子写入**：`create_operation` 用 `INSERT ... ON CONFLICT (tenant_id,idempotency_key) DO NOTHING`（`postgres_store.py:269-276`）；`create_approval` 用 `ON CONFLICT (tenant_id,operation_id) DO NOTHING`（`postgres_store.py:337-344`）。迁移建 `UNIQUE (tenant_id, idempotency_key)`（`migrations.py:78`）与 `UNIQUE (tenant_id, operation_id)`（`migrations.py:100/132`）。**同名跨租户不互相覆盖**。
- **RLS + 同事务租户作用域**：`_tx` 用 `set_config('app.tenant_id', :t, true)`（`postgres_store.py:88-93`，事务本地）与 SQL 在**同一连接/事务**内设置——满足"RLS 租户变量必须在 saver 实际执行 SQL 的同一事务设置"。业务表 + checkpoint 表均 `ENABLE/FORCE ROW LEVEL SECURITY` 且建 `*_tenant_scope` 策略（`migrations.py:255-365`）；运行角色 `app_runtime` 为 `NOINHERIT ... NOBYPASSRLS`（`migrations.py:396`），备份角色用 `BYPASSRLS` 仅读全量（`migrations.py:410-420`）。
- **CrewAI 写工具复用店 store 的租户作用域**：`build_crewai_router(settings, adapter, store=store)`（`builder.py:127`, 签名 `crewai_adapter.py:410-411`）把 `store` 传入，`_do_process_refund` 的 `store.create_operation(tenant_id,...)` 全走带 RLS 的租户路径（`crewai_adapter.py:260-264`）。

---

## 6. fail-closed —— ✅ PASS

**结论：资格/金额异常、模型档位不足、无文档、重试耗尽、幻觉检查失败、检查点恢复失败、委派/配置/端点失败 → 一律带状态转人工（`handle_error`），不降级执行敏感写。**

证据（t2 接线相关路径）：

- `crewai_refund_agent_node`：
  - 构造 router 异常 → `_fail_closed(state, code="crewai_router_build_failed", ...)`（`builder.py:127-130`）；
  - `run_business_task` 异常 → `_fail_closed(..., "crewai_delegation_failed")`（`builder.py:142-145`）；
  - **写意图但返回无 `approval_id/operation_id`** → `_fail_closed(..., "crewai_write_no_approval_in_response")`（`builder.py:167-170`）；
  - `crewai_result_condition`：`falls_to_error` → `handle_error`（`builder.py:106-107`）。
- `write_approval_condition`：`needs_approval and approval_id` → `approve`，否则 `handle_error`（`builder.py:69-74`）。
- `test_crewai_delegation_error_fails_closed_to_human`（`test_crewai_main_path.py:207-223`）：委派抛 `model_not_in_whitelist` → `falls_to_error=True`、`reason` 匹配、**不创建审批操作**、`final_response` 含"人工"。
- `test_crewai_write_without_approval_id_in_response_fails_closed`（`test_crewai_main_path.py:226-256`）：写意图缺 `approval_id/operation_id` → `crewai_write_no_approval_in_response`，**绝不创建审批操作**。
- 工具层 fail-closed：无 `store` → `missing_store`（`crewai_adapter.py:247-249`）；模型不在白名单 → `model_not_in_whitelist`（`:239-240`）；资格不符/金额非法 → `refund_ineligible`（`:253-255`）；缺 `order_id` → `missing_order_id`（`:231-232`）。
- RAG/幻觉检测终点 `rag_result_condition`：`error` → `handle_error`（`builder.py:252-255`）；RAG-only 仅政策/查询类，写操作始终带完整状态转人工。
- 审批恢复非法参数 → `resume_params_invalid` 拒绝（`approval.py:34-41`）。

> **重要限定**：见 **漏点 F1（§7）**——真实 CrewAI 写路径**恒触发** fail-closed（`crewai_write_no_approval_in_response`），即 fail-closed 判定本身可靠、无 fail-open；但这也意味着 CrewAI 写链路在其当前接线下**无法完成"进入 human_approval"**。

---

## 7. ❌ FAIL —— 真实 CrewAI 写路径接线缺陷（安全上 fail-closed 不构成绕过，但功能断裂 + 产生孤儿审批/操作记录）

**这是本次审计最值得交回 crewai-engineer 的发现。**

### 7.1 现象
真实（非 stub）CrewAI 写路径(`intent=refund`)经 `_run_real` → `crew.kickoff()` 后返回的**顶层字典**为：

```python
# crewai_adapter.py:401-402
return {"tool": tool_name, "intent": intent,
        "crew_result": str(result) if result is not None else ""}
```

这里**没有 `approval_id` / `operation_id` / `refund_amount`** 字段。

而 `builder.py` 的 `crewai_refund_agent_node` 对写意图这样取：

```python
# builder.py:151-170
action = CREWAI_WRITE_INTENTS.get(intent)   # "refund"
if action:
    approval_id = result.get("approval_id")       # None（顶层无此键）
    operation_id = result.get("operation_id")     # None
    if approval_id and operation_id:              # False
        ...
    else:
        out.update(_fail_closed(state, "crewai_write_no_approval_in_response", ...))
```

→ 真实 CrewAI 写路径**恒走 `crewai_write_no_approval_in_response` → `handle_error`（转人工）**，**永远不会**进入 `human_approval` 中断。

### 7.2 为什么是缺陷（而非"只是安全"）
- **与设计意图不一致**：`builder.py:8-10` 与 `crewai_adapter.py:17-18` 明确"写意图经 crewai 后仍必须唯一进入 human_approval"。当前实现使其恒 fail-closed，未达成该意图。
- **产生孤儿记录**：`_do_process_refund` **在返回前**已执行 `store.create_operation(...)` + `store.create_approval(...)`（`crewai_adapter.py:259-264`），即操作/审批单**已落库为 pending**；但其 `approval_id/operation_id` 只存在于 `crew_result` 的文本中，未被 `human_approval` 消费。结果是 store 中存在**永不被审批中断承接/确认的 pending 审批/操作记录**——若人工在 `handle_error` 侧另行处理，存在**重复处理/对账分歧**风险；若后续有路径按 pending 审批单"补审批"，也易造成二次执行（尽管 `execute_*` 幂等）。
- **测试掩盖**：`tests/test_crewai_main_path.py` 用 `_StubRouter`（`:41-77`）直接 `return real._do_process_refund(...)` 把 `approval_id/operation_id` 放到**顶层**（`:57-61`），从而"证明"写路径进入 `human_approval`。**该 stub 绕开了真实 `_run_real` 的字符串封装**，故现有测试**并未覆盖真实 CrewAI 写路径**——`test_crewai_enabled_refund_*` 成立的前提（顶层有 id）在真实链路上不成立。

### 7.3 修复建议（**不代改，交回 crewai-engineer**）
1. 让 `run_business_task`/`_run_real` 在写意图时，把 `_do_process_refund` 结果中的 `approval_id/operation_id/refund_amount/status` **解析并上抛到顶层 dict**（而不是只放到 `crew_result` 文本），使 `builder` 能正确路由到唯一 `human_approval`。
2. 或让 CrewAI `process_refund` 工具返回结构化输出并在 `_run_real` 中 `json.loads` 回填顶层；务必保证写路径仍返回 `status=pending_approval`、仍只创建待审批、仍由唯一 `human_approval` 承接。
3. **清理/幂等考虑**：避免"已落库但未被审批承接"的孤儿记录——在 `handle_error`/`crewai_write_no_approval_in_response` 分支，对已创建的 pending operation/approval 做显式拒绝/标记（或让 `_do_process_refund` 在未真正进入审批前**延迟落库**），以维持"待审批记录与审批链路一致"。
4. **补测**：新增一条**真实链路的集成测试**（mock `_run_real` 或包含 crewai 的环境），断言 `intent=refund` 时 `run_business_task` 返回顶层含 `approval_id/operation_id`，且主图随后经 `human_approval` 中断；不要再用"顶层直接返回业务 dict"的 stub 代替真实返回形状。

> **安全定性（再次明确）**：F1 **不是**安全绕过，也**没有**把 `tenant` 身份暴露给模型、**没有**新增 `direct→execute` 边、**没有**放宽模型门控或端点白名单。它是"fail-closed 但功能断裂"的接线缺陷。因此它**不影响**红线 1/2/3/4/5/6 的 PASS 判定，但必须修复才能让 CrewAI 写链路真正可用。

---

## 8. 是否破坏既有红线 / 396-34 基线 —— （✅ 未破坏红线；⚠️ 基线口径需说明）

- **绕过边**：无新增。图结构经测试断言无 `crewai_refund_agent→execute_*` 直接边（`test_crewai_main_path.py:166-183`）。
- **tenant 字段暴露**：未新增。schema、工具签名、state 新字段均不含身份（§2）。
- **幂等/RLS/审批归属**：未变更，仍满足（§1/§5）。
- **396/34 基线（已实跑核对）**：本审计用 `.venv\Scripts\python.exe -m pytest tests/ -q` 实跑（EXIT=0，43.91s）得 **406 passed, 34 skipped**。`release/v1.0.0-rc4-candidate` 权威捕获为 **396 passed, 34 skipped**（430 collected）。**+10 passed 恰为本次新增的 `tests/test_crewai_main_path.py`（10 用例）**；34 skipped 不变（=33 项 Postgres 数据面无 `DATABASE_URL` + 1 项 CrewAI 真实调用链无 crewai）。**结论：t2 未破坏 396/34 基线，仅新增 10 个通过用例（406/34）**，无失败/无真跳过缺失。（本机可跑全量说明此环境未触发历史 `tmp_path` `PermissionError [WinError 5]` 问题；如未来环境出现 320/70，仍应首选判定为环境问题而非 t2 引入。）

---

## 9. 真实依赖/证据边界（BLOCKED-需外部）

以下项**不可在本机闭环**，须由外部/部署/评测/业务方提供**真实**输入，禁止虚构：

| 项 | 期望证据 | 谁提供 | 状态 |
|---|---|---|---|
| 真实自托管 LLM 权重端点 + 调用 | LLM 端点可达、网络白名单命中、`capability_ok` 返回 True | 部署/评测侧 | 🔒 BLOCKED |
| 模型写操作专项评测 `write_op_pass=true` 报告 | `llm_eval_report_path` JSON，白名单模型均在报告中且 pass | 评测侧 | 🔒 BLOCKED |
| 真实资金/沙箱网关链路 | `execution_mode=live` + 真实 providers/回调验签/对账 | 资金/执行侧 | 🔒 BLOCKED |
| 首批书面确认真实租户 / 7 天观察 | 真实租户确认函、`LAUNCH_ALLOWED_TENANTS`、7 天 shadow 观测 | 业务/发布侧 | 🔒 BLOCKED |
| API/认证层「服务端认证 → 注入 TenantContext」 | 认证、成员/角色校验、不信任 `thread_id`/前端传参 | 安全/API 侧 | 🔒 BLOCKED（属审计范围外） |

**本审计对以上项一律不作出可用/达标的断言。**

---

## 10. 结论

- **红线 1-6 均 PASS**：审批不绕过、租户边界不可绕过、完全自托管、模型白名单门控、数据面租户隔离与租户级幂等、fail-closed，全部满足《编码/业务多智能体宪法》。
- **一项 FAIL（F1）**：真实 CrewAI 写路径因 `_run_real` 未上抛 `approval_id/operation_id` 而恒 fail-closed 转人工，且已落库的 pending 审批/操作成为孤儿记录；现有测试以 stub 掩盖该形状。**安全上 fail-closed 无绕过**，但**必须修复**才能让 CrewAI 写链路真正进入唯一 `human_approval`。
- **待办**：`EndpointGuard` 通配符白名单在受限环境可放行公网端点（硬化风险）；`_write_capability_ok` 与 `_crewai_llm` 的模型取值来源建议统一。
- **BLOCKED-需外部**：真实自托管模型端点/评测、真实资金链路、真实租户确认、7 天观察、API 认证层证据 —— 均未在本审计闭环，不作现实断言。

---

*审计版本 1.0 · security-auditor · 与《生产基线与验收测试》《Agent 宪法》配套。*
