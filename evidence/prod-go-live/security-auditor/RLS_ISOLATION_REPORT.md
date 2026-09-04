# T2 安全审计 — RLS / 跨租户 / 审批绕过 / 幂等 / 隔离取证报告

- 角色：security-auditor（prod-go-live 团队）
- 目标栈：**生产栈 `after-sales-prod`**。**经核实 `docker ps` 无任何 `after-sales-prod-*` 容器**（仅 `after-sales-preview-*` 与 `after-sales-observability-*` 运行），T1 生产栈**尚未落地**。按任务约定**回退用 preview 栈 + 独立一次性测试库取证**，并如实标注。
- 取证方式：真实 PostgreSQL（preview 集群 `after-sales-preview_postgres-1`，内网 `after-sales-preview_internal`，172.30.0.0/16）+ 一次性测试库（`langgraph_rls_audit__1788502351541`，已 drop + REVOKE 并确认 live `langgraph` 库未污染）。复用仓库脚本/测试；全部证据不伪造，失败路径如实记录。
- 报告时间：2026-09-04。证据目录：`evidence/prod-go-live/security-auditor/`

---

## 〇、结论摘要（先看这里）

| # | 审计项 | 结论 | 说明 |
|---|--------|------|------|
| 1 | RLS 动态验证（B1–B5） | ✅ PASS | 真实 PG 动态绕过全拦截；`app_runtime`=NOBYPASSRLS 非 owner；业务表 + checkpoint 表 RLS ENABLE+FORCE |
| 2 | 跨租户拒绝 | ✅ PASS | 跨租户读/操作/审批 → 404，无状态泄露，越权留痕；联合唯一键隔离同名 order/request id |
| 3 | 审批绕过检查 | ✅ PASS | 唯一 `human_approval` interrupt；图无 `direct→execute_*` 边；执行节点 5 重复核；拒绝→REJECTED 无执行；需同租户；`platform_admin` 非租户审批角色 |
| 4 | 租户级幂等 | ✅ PASS | 键含 tenant_id+request_id 无 attempt；UNIQUE + ON CONFLICT；并发只落 1 条、跨租户不覆盖 |
| 5 | 数据面隔离 | ✅ PASS（+1 警示） | store/checkpointer 同连接同事务 `set_config`；审计/队列/轨迹均带租户；⚠️ 见 §七 迁移缺陷 |
| 6 | 审计留痕 | ✅ PASS | decision/execution/approve+deny/越权拒绝均 `append_audit`，可回溯租户/会话/工单 |

> **安全红线（跨租户 / 审批绕过 / 幂等）均未违反 → Go/No-Go 为 GO（安全侧）。**
> **但存在一个必须修复的「迁移/schema 完整性」缺陷（见 §七），属于生产上线前的发布硬化阻断项**，虽不构成安全隔离红线违反，但若不修复，全新 prod 库将无法完成迁移、且 live 数据面缺失 3 张当前迁移定义的表。

---

## 一、RLS 动态验证（B1–B5）—— ✅ PASS

**方法**：复用 `tests/pg_helpers.py`（`setup_runtime_role` 建 NOBYPASSRLS 运行角色）+ 自写运行器，对一次性测试库做动态注入绕过断言，覆盖 t4 B1–B5。因当前 `migrations.py` 的 `shipping_events` 存在外键缺陷（见 §七），标准 `reset_pg_schema` 无法在全新库完成，故运行器对 `shipping_events` 采用**符合预期语义的复合外键修正**后初始化 schema 再验证 RLS 策略行为（该缺陷不影响 RLS 策略本身，已在 §七 单列）。

**证据**：`evidence/prod-go-live/security-auditor/rls_dynamic_probe.json`（conclusion=true）

| 子项 | 断言 | 结果 |
|---|---|---|
| B1 跨租户直连查询零可见 | 显式 `WHERE tenant_id='TENANT-B'` 仍 0 行；本租户可见 1 行 | `0` / `1` ✅ |
| B2 跨租户直连写 | B 作用域写 `tenant_id='TENANT-A'` → WITH CHECK 拒绝 | `true` ✅ |
| B3 绕过应用层直接改 tenant_id | `UPDATE sessions SET tenant_id='TENANT-B'` → 拒绝；原行未变、跨租户仍 0 | `true` / `1` / `0` ✅ |
| B4 运行角色无法越权 | `app_runtime`：`rolsuper=false`、`rolbypassrls=false` | ✅ |
| B5 FORCE RLS 生效 | `sessions` RLS `enabled+forced=true`；未设作用域 0 可见；跨租户 B 0、本租户 A 1 | ✅ |

**补充（checkpoint 数据面隔离静态确认）**：`checkpoints`/`checkpoint_blobs`/`checkpoint_writes`/`checkpoint_thread_scopes` 四表均 `relrowsecurity=true, relforcerowsecurity=true`（同一 json 证据 `checkpoint_rls_static`）。

---

## 二、跨租户拒绝 —— ✅ PASS

**静态（schema）**：
- `get_operation`/`get_approval` 一律 `WHERE ... AND tenant_id=:t` → 跨租户 NOT_FOUND(404)；RLS 在数据面同样过滤（见 §一）。
- 联合唯一键隔离同名 order/request id（live 库实测，见 `pg_rls_inventory_live.json` 与下方 SQL）：`operations UNIQUE(tenant_id,idempotency_key)`、`approvals UNIQUE(tenant_id,operation_id)`、`executions UNIQUE(tenant_id,operation_id)`。
- `api/deps.py`：`tenant_id/user_id/role` 一律来自 `resolve_tenant_context`（JWT 认证 + `require_active_membership` 校验），**绝不信客户端 body/query/Header/thread_id**；`platform_admin` 非租户成员角色。

**动态（HTTP，真实运行 preview API 经 nginx 443）**：
- 同租户（TENANT-A ADMIN 访问本租户审批）→ **HTTP 200**。
- 跨租户（TENANT-B ADMIN 访问 TENANT-A 的审批）→ **HTTP 404** `{"detail":"审批单不存在","code":"not_found"}`（不泄露存在性）。
- 跨租户审批决策（TENANT-B ADMIN POST 到 TENANT-A 审批）→ **HTTP 404**，未执行。
- 越权拒绝留痕：`audit` 表出现 `security.deny.approval_access_denied | TENANT-B | ADMIN-B | <A的approval_id>`（多行，含 APPROVER-B 尝试），可回溯请求方租户/用户/目标资源。

**证据**：本报告 §二 内嵌的 HTTP 结果 + `pg_rls_inventory_live.json`（联合唯一键）。应用层跨租户/会话越权拒绝已由 `tests/test_tenant_isolation.py`、`tests/test_approval_idempotency.py`、`tests/test_execution_engine.py` 等 60 例本地测试覆盖（见 §八）。

---

## 三、审批绕过检查 —— ✅ PASS

**图结构**（`src/graph/builder.py`）：`process_refund`/`process_return`/`update_return_address` 均经 `write_approval_condition` → `{"approve":"human_approval", "handle_error":"handle_error"}`；**无任何 `direct → execute_*` 边**。`execute_refund/return/address_update` 的唯一入口是 `human_approval`（审批结果路由 `approval_result_condition`）。资格/参数/能力校验失败一律 fail-closed 转 `handle_error`（人工），绝不直接执行。

**唯一审批入口**（`src/graph/approval.py`）：`interrupt(payload)` 是唯一 `human_approval`；`validate_resume_params` 要求 `approved` 为 bool，非法即 fail-closed 置 REJECTED。

**执行节点 5 重复核**（`src/graph/nodes.py::_execute`）：①租户作用域（`get_operation` 跨租户 404）②动作类型必须匹配当前执行节点 ③操作绑定当前 thread ④**审批状态必须为 APPROVED** 且 approval→operation/thread/action 绑定一致 ⑤模型白名单。另幂等重放（EXECUTED→重放）、REJECTED/HUMAN_HANDOFF→拒绝执行。**即使图被异常走到 execute，无 approved 审批也拒绝写**（防御纵深）。

**API 审批决策**（`src/api/routes.py::decide_approval`）：`require_role(ctx,{admin,approver})`；`get_approval` 跨租户→404+留痕；绑定复核（operation/action/thread）；**批准须显式 `confirmation`**（二次确认）；**CAS `claim_approval_decision`** 抢占 `pending→终态`，`claimed=False` 幂等重放不双执行；拒绝→operation 置 `rejected`/`human_handoff`，**绝不执行**；返回"审批已拒绝，未执行"。

**同租户**：`claim_approval_decision`/`get_approval`/`get_operation` 均按 `ctx.tenant_id` 过滤；跨租户审批请求→404 + 越权审计。**`platform_admin` 不是租户审批角色**（`memberships.role` CHECK 仅四类租户角色，见 `migrations.py`），无法以平台级权限绕过租户审批归属。

**证据**：代码审查（§三 行号）+ `tests/test_approval_idempotency.py`/`tests/test_execution_engine.py` 全过（拒绝→REJECTED 且无执行记录、已决策重放不重放执行）。

---

## 四、租户级幂等 —— ✅ PASS

**键格式**（`src/core/types.py::generate_operation_key`）：`f"{prefix}:{tenant_id}:{order_id}:{request_id}"`，prefix∈`oprefund/opreturn/opaddr`，用 `client_request_id`。**绝不含重试次数/attempt**。

**DB 唯一约束 + 原子写**（`src/infrastructure/migrations.py` + `postgres_store.py`）：
- `operations UNIQUE(tenant_id,idempotency_key)` + `INSERT ... ON CONFLICT (tenant_id,idempotency_key) DO NOTHING`（读回重放）。
- `approvals UNIQUE(tenant_id,operation_id)` + `create_approval ... ON CONFLICT DO NOTHING`。
- `executions UNIQUE(tenant_id,operation_id)`（执行锚点=operation_id）。

**动态并发**（`scripts/concurrency_stress.py --backend sqlite --concurrency 128`）→ `concurrency_stress.json`：
- I1 同租户同 op 128 并发：`provider_submit_calls=1`、`distinct_external_txn_id=1`、`execution_records_for_op=1`、`distinct_execution_id=1`（**只落 1 条 execution record**；单执行守卫只一个线程成为提交者）。
- I2 跨租户同幂等键：各租户各自 1 条、`cross_tenant_no_overwrite=true`（**互不覆盖**）。
- I3 同 nonce 回调并发：CAS 只 1 个 confirmed、其余 127 个 `replay`、终态封闭。
- `conclusion=true`，`errors={}`。

**live 库唯一约束实测**（migrator 查询）：
```
approvals :: UNIQUE (tenant_id, operation_id)
executions :: UNIQUE (tenant_id, operation_id)
operations :: UNIQUE (tenant_id, idempotency_key)
checkpoint_thread_scopes :: UNIQUE (thread_id)
```

---

## 五、数据面隔离 —— ✅ PASS（+§七警示）

- **PostgreSQL/Store**（`postgres_store.py::_tx`）：`with self._engine.begin()` 同一连接/同一事务内先 `set_config('app.tenant_id', :t, true)`（事务本地）再执行业务 SQL；`create_operation/get_operation/create_approval/decide_approval/claim_approval_decision/append_audit` 全部走租户作用域连接。未设作用域（tenant_id=None）时 RLS 使业务表零可见（见 §一 B4/B5）。
- **Checkpointer**（`src/infrastructure/checkpointer.py::TenantScopedCheckpointer`）：`_cursor` 覆写为**在 saver 实际执行 SQL 的同一连接与同一事务内**先 `set_config('app.tenant_id', tenant, true)`，再执行 checkpoint SQL（**同连接同事务 RLS 闭环**）；`_scope_for_thread` 校验 thread 一致性（不一致→CROSS_TENANT_DENIED 403）；`setup()` 被显式禁止（必须走迁移建表，防运行时绕过 RLS 的两段式初始化）；raw `AsyncPostgresSaver` 不暴露给业务。graph 只以 `checkpointer=TenantScopedCheckpointer` 编译。
- **队列（Celery）**（`src/tasks/execution.py`）：任务 payload 带 `tenant_id`；执行前 `require_tenant_active` 复核（停用拒绝）。
- **可观测（Langfuse）**（`src/observability/tracing.py`）：trace metadata 写入 `tenant_id`/`session_id`/`environment`；未配置则 no-op 不阻断主链路。
- **检索**：keyword 按 `tenant_id` 过滤；milvus 文档实体带 `tenant_id`。（告警/指标标签有界，`_FORBIDDEN_LABELS` 拒绝高基数 `tenant_id` 直接作标签，明细走审计。）
- **缓存**：未发现业务数据租户无条件的缓存键；Redis 仅用于登录限流（账号/IP 键，fail-closed）。

---

## 六、审计留痕 —— ✅ PASS

- `append_audit`（tenant-scoped 事务）：`execution.create`、`execution.callback.confirmed/compensated/compensation_failed/reconcile.confirmed/mismatch`（`src/execution/engine.py`）、`approval.decide`（`src/api/routes.py`）、`auth.login`、`session.create`、`security.deny.*`（`src/auth/security.py::audit_security_denial`，含 reason/租户/用户/目标 + 脱敏）。
- 越权拒绝**留痕**：跨租户审批/操作访问均写入 `security.deny.approval_access_denied` / `security.deny.operation_access_denied`（本报告 §二 已实测多条）。拒绝路径全量入审计、明细走审计查询（指标仅记有界 `security_denials_total{kind}`）。
- 溯源到租户/会话/工单：audit 表带 `tenant_id`、`user_id`、`target_type`、`target_id`、`detail`；业务记录（operation/approval/execution）同样带 `tenant_id`。
- 本地测试 `tests/test_callback_security_log.py`、`tests/test_launch_gate_audit.py`、`tests/test_approval_idempotency.py` 全过。

---

## 七、⚠️ 迁移 / schema 完整性缺陷（发布硬化阻断项，非安全隔离红线）

1. **`migrations.py` `shipping_events` 外键缺陷**：`order_id TEXT NOT NULL REFERENCES orders(tenant_id, order_id) ON DELETE CASCADE` 为**单列引用复合主键**，PostgreSQL 报 `InvalidForeignKey: number of referencing and referenced columns for foreign key disagree`，导致 `initialize_all` 在**全新库无法完成**（实测）。正确应为复合引用 `FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id)`。
   - **影响**：当前代码无法在全新 prod 库干净建表；`migrate_cli` 会 fail。**部署前必须修复**（deploy-engineer）。
2. **live preview `langgraph` 库 schema 漂移**：实有 14 张 public 表，**缺少**当前 `migrations.py` 定义的 `orders`、`shipping_events`、`policy_documents` 三表（`src/tools/postgres_data_source.py` 依赖它们）。说明 live 库由更早版本迁移，未被后续迁移补齐；若生产启用 postgres 数据源，将以 `orders/shipping_events/policy_documents` 缺失或迁移失败造成影响。**建议**修复 FK 后重新迁移并核对数据源所需表齐全。

> 该缺陷**不构成跨租户 / 审批绕过 / 幂等 FAIL**，因此不触发"红线阻断 NO"；但属**发布硬化阻断项**，放行前应由 deploy-engineer 修复并复跑 `migrate_cli` 与数据源验收。

---

## 八、可用测试证据（复用）

本地 `.venv`（无 DB，postgres 标记自动 skip）运行，全部通过：

```
tests/test_approval_idempotency.py  tests/test_tenant_isolation.py
tests/test_execution_engine.py       tests/test_callback_security_log.py
tests/test_launch_gate_audit.py
=> 60 passed in 2.85s
```

相关既有证据（供对照，非本次伪造）：`evidence/pg_rls_bypass_probe.json`（conclusion=true）、`evidence/concurrency_stress.json`、`deploy/drills/records/approval-security-extra.json`、`evidence/audit_trace.json`。

---

## 九、Go / No-Go 判定

- **安全红线达成**：跨租户拒绝 ✅、唯一 `human_approval` 且无 direct 绕过 ✅、租户级幂等（唯一键含租户+请求ID、并发只落 1 条、跨租户不覆盖）✅、数据面隔离（RLS/同连接 set_config/队列/观测带租户）✅、审计留痕 ✅。
- **Go/No-Go（安全侧）**：**GO**。
- **前置条件（放行前必须处理）**：修复 `migrations.py` `shipping_events` 外键缺陷，并核对 live/prod 数据面是否补齐 `orders/shipping_events/policy_documents`（否则全新 prod 库无法迁移、postgres 数据源缺表）。此为非安全红线的发布硬化项，建议 deploy-engineer 修复后复验。

*本报告所有 SQL/HTTP 结果均在真实 Docker 容器环境采集；一次性测试库已清理（drop + REVOKE，live `langgraph` 库未污染，无 `langgraph_rls_audit%` 残留；测试审批单仍为 pending 未受影响）。*
