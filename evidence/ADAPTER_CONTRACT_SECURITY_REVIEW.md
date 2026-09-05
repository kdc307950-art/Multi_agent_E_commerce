# Adapter 契约改造安全评审（本轮 · 五要点）

> **范围**：按任务五大要点评审本轮 Adapter 契约改造（读路由审计、归属校验、数据源契约、launch 门控、
> LLM 熔断 fail-closed），**只评审与报告，不改代码**。环境无 Docker/PostgreSQL/模型端点，真实活体验证
> 需基础设施；以下为静态核对 + 定向测试佐证，未做活体（如实标注）。

---

## 结论

| # | 要点 | 结论 |
|---|---|---|
| 1 | `GET /orders` 重构后审计动作一致性 + 不泄露存在性 | ✅ 通过 |
| 2 | `adapter.py` + `data_source.py` 归属校验 + fail-closed | ✅ 通过 |
| 3 | 数据源契约杜绝业务节点/读路由直接读 `mock_data` | ⚠️ 基本通过，1 处遗留风险（RAG 默认回退） |
| 4 | launch 门控"受限环境必须 postgres" | ✅ 合理且不误伤开发/测试 |
| 5 | LLM 熔断 fail-closed 转人工是否可被绕过 | ✅ 主路径不可绕过；1 处配置级关注项 |

---

## 1. `GET /api/orders/{order_id}` 审计动作一致性 + 不泄露存在性

- `src/api/routes.py::get_order` 已改为经 `request.app.state.adapter`（EcommerceAdapter）读取，
  捕获 `AdapterError` 并区分：
  - `exc.code == "order_not_owned"`（同租户 customer 读他人订单）→ 审计 `security.deny.cross_user_order`，
    detail 带 `owner=exc.owner`；
  - 其余（`order_not_found` / `cross_tenant_denied`）→ 审计 `security.deny.order_access_denied`。
  两分支均 `raise DomainError(NOT_FOUND, "订单不存在", 404)`，**对外统一 404**，不区分"不存在/不属于本租户/
  无权"，不泄露订单存在性。✅
- 配套：`adapter._assert_ownership` 在 `order_not_owned` 时通过 `AdapterError(..., owner=raw["user_id"])`
  传递订单归属者，供审计 detail 使用；`AdapterError.owner` 为可选参数，向后兼容。
- 佐证：`tests/test_security_regressions.py::test_same_tenant_customer_cannot_read_other_order`（同租户 B 读
  A 订单 → 404 且审计含 `security.deny.cross_user_order`；staff 可读；200/404 正确）。本套件 24/24 过。

## 2. `adapter.py` + `data_source.py` 归属校验 + fail-closed

- **归属**（`_assert_ownership`）：`raw["tenant_id"] != tenant_id` → `cross_tenant_denied`；
  `raw["user_id"] != user_id and role not in STAFF_ROLES` → `order_not_owned`。`STAFF_ROLES`
  （agent/admin/approver）与 `routes._staff_role` 一致。✅
- **读路径**：`get_order`/`get_shipping` 均先做归属/存在校验；`get_shipping` 对"不存在/无权"返回 None
  （规避信息泄露）。
- **写路径 fail-closed**（`nodes._write_action`）：先 `capability_ok` 模型白名单门控 →
  `check_refund/return_eligibility`、`validate_address_change` 资格/金额/地址校验 → 不通过一律
  `falls_to_error=True` 转人工；通过后仅 `create_operation + create_approval`（`needs_approval=True`），
  **绝不直接 execute**（唯一 `human_approval` 入口）。`AdapterError / LLMError` 均落入 `falls_to_error`。✅
- **数据源契约**：`BusinessDataSource` 方法签名强制带 `tenant_id`；`PostgresBusinessDataSource` 在事务内
  `set_config('app.tenant_id', :t, true)` + `WHERE tenant_id=:t`，跨租户返回 None，RLS 兜底。✅

## 3. 是否杜绝直接读 `mock_data`（grep src）

- **订单/物流读路径**：`routes.get_order`、`graph.nodes.query_order_node/track_shipping_node` 全部经
  `EcommerceAdapter` → `BusinessDataSource`，不直接读 `mock_data`。`MockBusinessDataSource` 读 mock_data
  是"受控 mock 数据源"的**合规**实现，非绕过。✅
- **政策检索（served app 路径）**：`main.py → build_retriever(settings)`（默认 keyword 返回
  `DataSourceRetriever`，委托 `search_policy(tenant_id,q)`，租户隔离）→ `ChatRunner → build_graph →
  make_rag_graph(llm, retriever=...)`。运行时注入的是数据源后端检索器。✅
- **遗留风险（⚠️ 需关注）**：`src/graph/rag.py::make_rag_graph` 的默认回退 `retriever = retriever or
  KeywordRetriever()`，而 `KeywordRetriever` 直接读 `mock_data.KNOWLEDGE_BASE / POLICY_KEYWORDS`
  （`src/retrieval/keyword.py`），且**非租户隔离**。当 `build_graph(...)` 未注入 retriever（如测试直接构造，
  `build_graph` 签名 `retriever=None`）即落入此默认。served app 总是注入 `build_retriever` 结果，故线上
  不受影响，但该默认回退与"业务路径不直接读 mock_data"的目标不一致。**建议**：`make_rag_graph`/
  `build_graph` 改为"必须注入 retriever，否则 fail-closed"，或让 `KeywordRetriever` 改读数据源/去除直接
  mock 依赖。

## 4. launch 门控"受限环境 business_data_backend 必须 postgres"是否合理/误伤

- `src/core/launch_gate.py::verify_launch_gate` 新增：`if settings.is_restricted_env and
  business_data_backend != "postgres" → violation`。`is_restricted_env = env in {"preview","production"}`
  （config.py:240）。
- **不影响开发/测试**：仅在 `is_restricted_env`（preview/production）触发；`env=development/test` 零命中。
  合理——受限环境业务读路径必须接受控真实系统（订单/物流/政策读 PG + RLS + 服务端注入 tenant_id），
  Mock 仅限测试环境。✅
- 佐证：`test_launch_gate_audit.py`（含受限环境 Mock 数据源标记用例）+ `test_llm_endpoint_gate.py` 合计 32 passed。

## 5. LLM 熔断 fail-closed 转人工是否可能被绕过

- `src/llm/openai_compatible.py::_chat`：先 `if not self._cb.allow(): raise LLMUnavailableError(...)`
  （OPEN 期快速失败），节点层捕获 `LLMError` 后 `falls_to_error=True` 转人工。`LLMUnavailableError`
  计入熔断失败；`LLMOutputError` 不计入（模型行为异常，仍由节点 fail-closed）。✅
- **主路径不可绕过**：即便熔断关闭，`LLMUnavailableError` 仍会抛出 → 节点 fail-closed 转人工；
  熔断只是**额外的快速失败/隔离**层，不是唯一防御。✅
- **配置级关注项（⚠️）**：`CircuitBreaker.enabled = failure_threshold > 0`，且阈值来自
  `settings.llm_cb_failure_threshold`（默认 5），代码**未强制**受限环境的阈值必须 >0。若误配
  `llm_cb_failure_threshold=0`，熔断被禁用（`allow()` 恒 True），快速失败层失效（退化为超时/重试兜底）。
  **建议**：在 launch 门控或 config 校验中，对受限环境强制 `llm_cb_failure_threshold > 0`（fail-closed）。

---

## 通过 / 需修复清单

**通过（无需改动）**
- 读路由审计动作区分 + 带 owner + 统一 404 不泄露存在性。
- Adapter 归属（customer 仅本人 / staff 本租户 / 跨租户/跨用户拒绝）与写路径 fail-closed（仅审批，不直接执行）。
- 数据源契约强制 tenant_id；Postgres 注入 app.tenant_id + RLS 兜底。
- launch 门控受限环境必须 postgres（不误伤开发/测试）。
- 熔断 fail-closed 主链路（端点持续故障 → LLMUnavailableError → 转人工）。

**需修复 / 建议（不阻断默认路径，按优先级）**
1. [中·契约一致] RAG `make_rag_graph` 默认回退 `KeywordRetriever()` 直接读 `mock_data`、非租户隔离。
   建议改为"必须注入 retriever，否则 fail-closed"，或让 KeywordRetriever 改走数据源。
2. [中·可用性] `adapter.get_shipping` 的 `except (AdapterError, Exception)` 过宽，会吞系统性
   `DataSourceError`（如 DB 断连）并返回 None → 客户端误报"暂无物流轨迹"。建议仅捕获 AdapterError，
   DataSourceError 上抛走 fail-closed 转人工。
3. [低·配置] 受限环境未强制 `llm_cb_failure_threshold > 0`；需在 launch 门控/配置校验中强制，防误配关闭熔断。
4. [低·信息]（已修复 · 闭环注入，t1）`crewai_adapter._run_real` 现经 `_task_description` 仅向模型暴露
   用途与可用工具，**不再**把 `tenant_id/user_id/role/order_id/thread_id` 拼进 Task description（不泄露身份）；
   租户身份仅由服务端 `ctx`（调用方注入的 TenantContext）经**闭包**注入各工具对象，模型不可覆盖。
   `crewai_enabled=False`（默认关闭）仍为安全前提。
5. [低·信息] `postgres_data_source.policy_documents()` 未设 `app.tenant_id`，FORCE RLS 下全量返回空；
   仅 milvus 后端用作语料预载，需以可绕过 RLS 的迁移/owner 角色或设作用域，否则会空。

---

## 测试佐证与诚实说明

- 本轮相关套件实跑：`test_launch_gate_audit.py + test_llm_endpoint_gate.py` = **32 passed**；
  早前 `test_pg_data_source_acceptance + test_pg_rls + test_data_source_contract +
  test_security_regressions + test_hardening_acceptance` = **70 passed / 10 skipped**（10 skipped 为
  skip-if-no-PG）。
- 本会话环境无 Docker/PostgreSQL/模型端点，故真实 PG/RLS 隔离、真实模型写白名单、真实端点熔断为
  "代码就绪 + 静态核对 + 定向测试通过"，**未做活体验收**；需基础设施后执行
  `evidence/PG_RLS_ACCEPTANCE_PROCEDURE.md` 与 `verify_llm_chain.py` 方能得出生产可用结论。
