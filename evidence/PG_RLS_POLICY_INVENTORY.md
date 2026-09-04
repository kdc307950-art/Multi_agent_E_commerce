# RLS 策略清单 — PostgreSQL 数据面（真实 langgraph 数据库）

> 版本：v1.1 · 2026-09-04
> 环境：PostgreSQL 14.24（Ubuntu，`127.0.0.1:55432`），数据库 `langgraph`
> 数据来源：`evidence/pg_rls_policies.json`（`scripts/pg_rls_inventory.py`，以 `migrator` 只读元数据查询产出，conclusion=true）
> 合规口径：所有业务读写使用运行角色 `app_runtime`（LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS），未使用超级用户/表 owner 验证隔离。

---

## 1. 结论

| 检查项 | 结果 |
|---|---|
| 所有非租户表 RLS 已启用且强制 | ✅ true |
| `tenants`（租户主数据）不启用 RLS | ✅ true |
| `app_runtime` 非超级用户且非 bypassrls | ✅ true |
| 作用域函数齐全（2 个） | ✅ true |
| **conclusion** | ✅ **true** |

> **全量套件佐证**：权威日志 `evidence/pg_acceptance_full_v2.log` 记录 **全量 pytest 196 passed, 1 skipped**（本会话 clean 重跑，无 E/error，EXIT=0）。12 个 `@pytest.mark.postgres` 测试全部通过、无 skipped；唯一 skip 为 `test_hardening_acceptance.py:384` 的 crewai 环境边界（`@skipif(True)`）。RLS 一律以**非超级用户** `app_runtime`（NOINHERIT NOSUPERUSER NOBYPASSRLS）连接验证，未用超级用户/表 owner 验证隔离。

---

## 2. RLS 启用/强制 状态（按表）

| 表 | rls_enabled | rls_forced |
|---|---|---|
| approvals | ✅ | ✅ |
| audit | ✅ | ✅ |
| checkpoint_blobs | ✅ | ✅ |
| checkpoint_thread_scopes | ✅ | ✅ |
| checkpoint_writes | ✅ | ✅ |
| checkpoints | ✅ | ✅ |
| executions | ✅ | ✅ |
| memberships | ✅ | ✅ |
| operations | ✅ | ✅ |
| sessions | ✅ | ✅ |
| stream_events | ✅ | ✅ |
| streams | ✅ | ✅ |
| **tenants** | ❌ | ❌ |

**12 张非租户表全部 `ENABLE + FORCE ROW LEVEL SECURITY`**；`tenants`（租户主数据，平台层由迁移角色维护）不启用 RLS，符合设计。

---

## 3. 策略清单（`pg_policies`，共 12 条 `*_tenant_scope`）

全部为 `PERMISSIVE`、`cmd=ALL`、角色 `public`。

### 3.1 业务表（9 张）：`qual = with_check = (tenant_id = app_current_tenant_id())`

| 表 | 策略名 | qual / with_check |
|---|---|---|
| approvals | approvals_tenant_scope | `(tenant_id = app_current_tenant_id())` |
| audit | audit_tenant_scope | `(tenant_id = app_current_tenant_id())` |
| checkpoint_thread_scopes | checkpoint_thread_scopes_tenant_scope | `(tenant_id = app_current_tenant_id())` |
| executions | executions_tenant_scope | `(tenant_id = app_current_tenant_id())` |
| memberships | memberships_tenant_scope | `(tenant_id = app_current_tenant_id())` |
| operations | operations_tenant_scope | `(tenant_id = app_current_tenant_id())` |
| sessions | sessions_tenant_scope | `(tenant_id = app_current_tenant_id())` |
| stream_events | stream_events_tenant_scope | `(tenant_id = app_current_tenant_id())` |
| streams | streams_tenant_scope | `(tenant_id = app_current_tenant_id())` |

### 3.2 官方 LangGraph checkpoint 表（3 张）：`qual = with_check = checkpoint_thread_in_current_tenant(thread_id)`

| 表 | 策略名 | qual / with_check |
|---|---|---|
| checkpoints | checkpoints_tenant_scope | `checkpoint_thread_in_current_tenant(thread_id)` |
| checkpoint_blobs | checkpoint_blobs_tenant_scope | `checkpoint_thread_in_current_tenant(thread_id)` |
| checkpoint_writes | checkpoint_writes_tenant_scope | `checkpoint_thread_in_current_tenant(thread_id)` |

> 官方 checkpoint 三表不重复加 `tenant_id` 列，而用 `checkpoint_thread_in_current_tenant(thread_id)` 做"存在性租户关联"（经 `checkpoint_thread_scopes` 关联到 `thread_id` 所属租户）。

---

## 4. 作用域函数（`scope_functions`）

| 函数 | 签名 |
|---|---|
| `app_current_tenant_id` | 无参（返回 `current_setting('app.tenant_id', true)` 的 `NULLIF` 结果） |
| `checkpoint_thread_in_current_tenant` | `candidate_thread_id text`（经 `checkpoint_thread_scopes` 存在性关联） |

---

## 5. 角色标志（`pg_roles`）

| 角色 | superuser | bypassrls | inherit | createdb | createrole | canlogin |
|---|---|---|---|---|---|---|
| `app_runtime` | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| `migrator`（迁移角色） | ✅ | ❌ | ✅ | ❌ | ❌ | ✅ |

`app_runtime` 满足 **LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE**。

---

## 6. 表 owner（`pg_tables`）

| 表 | tableowner |
|---|---|
| approvals | migrator |
| checkpoints | migrator |
| checkpoint_blobs | migrator |
| checkpoint_thread_scopes | migrator |
| checkpoint_writes | migrator |
| executions | migrator |
| operations | migrator |
| sessions | migrator |

**运行角色 `app_runtime` 不是 owner**；`FORCE RLS` 保证即便 owner 也被策略过滤（已在 `tests/test_pg_rls` 验证 owner 绕过被拒）。

---

## 7. app_runtime 直接 DML 权限（最/小 GRANT）

对 `sessions` / `operations` / `approvals` / `executions` 均有 `INSERT / SELECT / UPDATE / DELETE`（无 DDL）。

| 表 | 权限 |
|---|---|
| approvals | INSERT / DELETE / UPDATE / SELECT |
| executions | DELETE / INSERT / SELECT / UPDATE |
| operations | INSERT / SELECT / UPDATE / DELETE |
| sessions | INSERT / DELETE / UPDATE / SELECT |

---

## 8. 语义要点（非超级用户隔离的成立前提）

1. **RLS 隔离靠"非 owner + NOBYPASSRLS"**：超级用户/owner 天然绕过 RLS，故隔离验证必须用运行角色 `app_runtime`；实测跨租户读取（store + SQL RLS）、跨租户写（WITH CHECK）、无租户上下文零可见、跨租户审批读写全部被拒（见 `evidence/pg_acceptance_evidence.json`）。
2. **FORCE RLS**：即便表 owner（migrator）以运行角色之外的方式访问，也强制走策略；`tenants` 例外（平台层）。
3. **checkpoint 三表用存在性关联**：官方 LangGraph 表结构不含 `tenant_id`，借用 `checkpoint_thread_in_current_tenant(thread_id)` 保证线程属于当前租户作用域，跨租户 checkpoint 恢复被拒（`cross_tenant_denied`，见 `evidence/pg_checkpoint_recovery_record.json`）。

---

## 9. 共享库 caveat 与过度授权观察（非本验收 FAIL/GAP）

- **共享库口径**：`langgraph` 库为共享实例，除本系统以上 12 张数据面表 + `tenants` 外，另存在 **25 张非租户 public 表未启用 RLS**（`tickets/ticket_*`、`knowledge_chunks/documents`、`sla_policies`、`routing_rules`、`support_*`、`inbound/outbox_events`、`it_assets`、`tenant_it_policies`、`satisfaction_surveys`、`agent_runs/events/schema_version/thread_activity`、`checkpoint_migrations`、`store/store_migrations`）。已由 reviewer 判定：均非本仓库 `src/`/部署迁移定义；其中 7 张为 langgraph 库表、18 张为另一支援/工单/ITSM/知识应用共用本库表。属**库共享、非本验收范围**，不影响本系统 12 表数据面 RLS 结论（`all_non_tenant_tables_rls_enabled_and_forced=true` 成立）。详见 `deploy/drills/records/extra_tables_rls_assessment.json`。
- **过度授权观察（INFO）**：`apply_runtime_role` 的 `GRANT ... ON ALL TABLES IN SCHEMA public` 使 `app_runtime` 对含 25 张非 RLS 共享表在内的全部 38 张表均可 DML（`evidence/pg_rls_policies.json` 的 `app_runtime_table_grants` 已逐表确认）。对未启用 RLS 的共享表属过度授权/跨租户数据暴露加固点，建议作为独立加固任务处理（不影响本次 12 表 RLS 核验）。

---

*源数据：`evidence/pg_rls_policies.json`（conclusion=true，timestamp 见文件内）。*
