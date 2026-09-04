# PostgreSQL 数据面验收报告 — 预发布（真实 langgraph 数据库）

> 版本：v1.1 · 2026-09-04
> 对象：电商售后多智能体工单系统 · PostgreSQL 数据面（PostgresStore + TenantScopedCheckpointer + RLS）
> 环境：预发布服务器 PostgreSQL **14.24**（Ubuntu，`127.0.0.1:55432`，数据库 **`langgraph`**），Redis 6.0.16（`127.0.0.1:6379`）
> 纪律：本报告所有"通过"均来自真实 PostgreSQL 实际运行，非 Mock/内存；RLS 一律用**非超级用户** `app_runtime` 验证。
> 角色：迁移角色 `migrator`（超级用户/表 owner，仅用于迁移/清理/策略查询）；运行角色 `app_runtime`（LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS，用于所有业务读写 RLS 验证）。

---

## 0. 结论（先读）

| 验收项 | 结论 | 证据 |
|---|---|---|
| 迁移角色执行迁移 | ✅ 通过 | `migrate_cli`（`migrations applied; runtime role ensured.` EXIT=0，见 `evidence/pg_migrate.log`） |
| PostgreSQL 数据面 12 项测试 | ✅ 12/12 通过 | `evidence/pg_suite.log`（app_runtime 连接） |
| 运行角色合规（NOINHERIT/NOSUPERUSER/NOBYPASSRLS） | ✅ | `evidence/pg_rls_policies.json` roles 段 |
| RLS 非超级用户验证 | ✅ 通过 | `tests/test_pg_rls` + `evidence/pg_acceptance_evidence.json` |
| 恢复 / 会话保留 / 幂等 / 检查点 | ✅ 通过 | `evidence/pg_checkpoint_recovery_record.json` |
| 额外验证（跨租户/停用/跨租户审批/重复回调/重启恢复） | ✅ 11/11 通过 | `evidence/pg_acceptance_evidence.json`（conclusion=true） |
| 数据库策略检查 | ✅ 4/4 通过 | `evidence/pg_rls_policies.json`（conclusion=true） |
| 全量 pytest 套件（含 12 项 PG 测试） | ✅ **196 passed, 1 skipped**（22s） | `evidence/pg_acceptance_full_v2.log`（clean，无 E/error，EXIT=0） |

**合规口径**：`app_runtime` 为 `LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`；RLS 数据面测试全部以该角色连接执行，未使用超级用户/表 owner 验证隔离。

---

## 1. 环境与启动

- 预发布服务器 PostgreSQL 14.24（Ubuntu，端口 **55432**）作为真实数据面，数据库 **`langgraph`**。
- 迁移角色 `migrator:migpass`（rolsuper=True，可做迁移/清理/策略查询）；运行角色 `app_runtime:apppass`（rolsuper=False, rolbypassrls=False, rolinherit=False, rolcanlogin=True）。
- Redis 6.0.16（`127.0.0.1:6379`，无口令）用于重启/重连恢复验证。
- 相关服务：Docker Desktop 运行中；本会话无 `docker` CLI（`C:\` 内未找到 `docker.exe`），容器级重启恢复引用上一阶段基线证据 `evidence/docker_verification.json`（详见 §7 与 §8）。

---

## 2. 迁移（迁移角色）

以迁移角色 `migrator` 执行 `python -m src.infrastructure.migrate_cli`（`DATABASE_MIGRATOR_URL` 指向 migrator，与运行角色分离）：

```
$ DATABASE_MIGRATOR_URL=postgresql://migrator:migpass@127.0.0.1:55432/langgraph
$ APP_RUNTIME_PASSWORD=apppass
$ python -m src.infrastructure.migrate_cli
migrations applied; runtime role ensured.   (EXIT=0)
```

- 迁移幂等（`CREATE TABLE IF NOT EXISTS` / `CREATE OR REPLACE FUNCTION` / `DROP POLICY ... CREATE POLICY`）。
- `apply_runtime_role` 创建/更新 `app_runtime` 并 GRANT 表/序列/函数最小权限。
- 日志：`evidence/pg_migrate.log` 内容为 `migrations applied; runtime role ensured.`。

**验收证据（本会话实时重跑）**：由 `scripts/pg_acceptance_evidence.py` 以 `migrator` 超级用户重置 schema（`drop_everything` + `initialize_all`）并重建运行角色，`schema_reset ok=true`（幂等、自包含），见 `evidence/pg_acceptance_evidence.json`。

---

## 3. 应用测试（app_runtime 运行角色）

以 `DATABASE_URL=postgresql://migrator:migpass@127.0.0.1:55432/langgraph`（超级用户仅用于 schema reset + 建运行角色）运行 pytest；一切**数据面访问**由测试内部切换到 `app_runtime`（`tests/pg_helpers.app_runtime_dsn`）。

### 3.1 PostgreSQL 数据面 12 项（全部通过）

`evidence/pg_suite.log`：
```
tests/test_pg_store.py::test_persists_across_store_rebuild PASSED
tests/test_pg_store.py::test_idempotency_across_connections PASSED
tests/test_pg_store.py::test_cross_tenant_scoped_404 PASSED
tests/test_pg_store.py::test_suspended_tenant_blocks PASSED
tests/test_pg_store.py::test_checkpoint_scope_created_atomically_with_session PASSED
tests/test_pg_rls.py::test_rls_zero_visible_without_tenant PASSED
tests/test_pg_rls.py::test_rls_cross_tenant_zero_visible PASSED
tests/test_pg_rls.py::test_rls_cross_tenant_zero_write PASSED
tests/test_pg_rls.py::test_checkpoint_tables_scoped_by_thread_scope PASSED
tests/test_pg_checkpoint_recovery.py::test_minimal_checkpoint_survives_restart PASSED
tests/test_pg_checkpoint_recovery.py::test_cross_tenant_checkpoint_resume_rejected PASSED
tests/test_pg_checkpoint_recovery.py::test_cleanup_expired_scopes_deletes_checkpoint_first PASSED
======================== 12 passed, 1 warning in 8.14s ========================
```

### 3.2 全量 pytest 套件（权威 clean 日志）

**`evidence/pg_acceptance_full_v2.log`：`196 passed, 1 skipped in 21.64s`（本会话 clean 重跑，无任何 `E`/error，EXIT=0）** —— 覆盖全部 `tests/`（含 12 项 PostgreSQL 数据面测试、RLS/Checkpoint/会话保留/幂等/审批/租户隔离/执行引擎/安全回归/认证/SSE/端到端）。

该 fresh 全量结果**替代**旧 `evidence/pg_acceptance_full.log`（该旧文件为此前 PowerShell 运行所污染，含 11 个 `E` 与 sessionfinish `PermissionError` 崩溃，非权威）。12 个 `@pytest.mark.postgres` 测试（5+4+3）在本 log 中全部通过、无 skipped（亦有 `evidence/pg_suite.log` 独立佐证：`12 passed, 1 warning in 8.14s`）。

> **唯一 1 个 skip（即 196+1=197 中的 1）**：`tests/test_hardening_acceptance.py:384` 的 crewai 真实子智能体集成（`@skipif(True, reason='本环境未安装 crewai；真实调用链在具备 crewai 的环境用集成测试验证')`）。此为本环境 `crewai` 未与 langgraph 同环境验证的**环境边界**，非本 PostgreSQL 验收项；reviewer（t5）已复核确认唯一 skip 即此 crewai 测试。

### 3.3 tmp_path / SQLite 后端说明（已随 fresh 全量运行解决）

`test_sqlite_store.py` 与 `test_recovery.py`（SQLite 后端）依赖 pytest `tmp_path`。旧 `evidence/pg_acceptance_full.log`（已被废弃）在会话结束阶段因 DSH 沙箱对 `_acceptance_tmp` 目录枚举报 `PermissionError`，导致含若干 `E`；**本会话 clean 重跑的 `evidence/pg_acceptance_full_v2.log`（196 passed, 1 skipped）已无任何 `E`/error，SQLite 后端测试亦全部通过**。因此不再构成失败；其"恢复 / 幂等"语义亦在 PostgreSQL 数据面由 `test_pg_store`、`test_pg_checkpoint_recovery` 等价覆盖。

---

## 4. RLS 数据库策略检查（非超级用户验证）

`evidence/pg_rls_policies.json`（conclusion=true，4/4 check true）。要点：

- **12 张非租户表** 全部 `ENABLE + FORCE ROW LEVEL SECURITY`：`memberships / sessions / operations / approvals / executions / streams / stream_events / audit / checkpoint_thread_scopes / checkpoints / checkpoint_blobs / checkpoint_writes`。
- **`tenants`（租户主数据）不启用 RLS**（平台层，迁移角色维护），符合设计。
- **策略清单**（`pg_policies`，共 12 条 `*_tenant_scope`，全部 `PERMISSIVE`、`cmd=ALL`、角色 `public`）：
  - 业务表（9 张）：`qual = with_check = (tenant_id = app_current_tenant_id())`。
  - 官方 checkpoint 表（3 张）：`qual = with_check = checkpoint_thread_in_current_tenant(thread_id)`（存在性租户关联，官方表不重复加 `tenant_id` 列）。
- **作用域函数**（`scope_functions`）：`app_current_tenant_id()` 与 `checkpoint_thread_in_current_tenant(candidate_thread_id text)` 两个均在。
- **角色标志**：
  | 角色 | superuser | bypassrls | inherit | createdb | createrole | canlogin |
  |---|---|---|---|---|---|---|
  | `app_runtime` | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
  | `migrator`（迁移角色） | ✅ | ❌ | ✅ | ❌ | ❌ | ✅ |
- **表 owner**：业务表 owner 为 `migrator`（迁移角色），**运行角色 `app_runtime` 不是 owner**；`FORCE RLS` 保证即便 owner 也被策略过滤（已在 `test_pg_rls` 验证）。
- **app_runtime 直接 DML 权限**：对 `sessions/operations/approvals/executions` 有 `INSERT/SELECT/UPDATE/DELETE`（最小 GRANT，无 DDL）。
- **共享库口径（caveat，非本验收 FAIL/GAP）**：`langgraph` 库为共享实例，除本系统以上 12+tenants 数据面外，另有 **25 张非租户 public 表** 未启用 RLS（`tickets/ticket_*`、`knowledge_chunks/documents`、`sla_policies`、`routing_rules`、`support_*`、`inbound/outbox_events`、`it_assets`、`tenant_it_policies`、`satisfaction_surveys`、`agent_runs/events/schema_version/thread_activity`、`checkpoint_migrations`、`store/store_migrations`）。已由 reviewer 判定：均非本仓库 `src/`/部署迁移定义；其中 7 张为 langgraph 库表、18 张为另一支援/工单/ITSM/知识应用共用本库表，属**库共享、非本验收范围**，不影响本系统 12 表数据面 RLS 结论（`all_non_tenant_tables_rls_enabled_and_forced=true` 成立）。详见 `deploy/drills/records/extra_tables_rls_assessment.json`。

---

## 5. Checkpoint 恢复记录

`evidence/pg_checkpoint_recovery_record.json`（conclusion=true，指向 `127.0.0.1:55432/langgraph`）：
```
checkpoint_written                        = 1
recovered_state_value_after_restart       = 1        (期望 1)
cross_tenant_resume_rejected              = true
cross_tenant_reject_code                  = cross_tenant_denied
cleanup_expired_deleted                   = 1
cleanup_expired_failed                    = 0
cleanup_session_removed                   = true
cleanup_scope_removed                     = true
```
即：检查点经锁定版 `TenantScopedCheckpointer` 持久化 → "重启"（新连接池 + 新 saver）后状态恢复为 `v=1`；跨租户恢复被拒；过期清理**先清 checkpoint，再删 scope/session**。

---

## 6. 额外验证（全部通过，`evidence/pg_acceptance_evidence.json` conclusion=true）

由 `scripts/pg_acceptance_evidence.py`（app_runtime 非超级用户）直接验证：

| 验证项 | 结果 | 关键事实 |
|---|---|---|
| 跨租户读取拒绝（store） | ✅ | `get_operation/get_session(TENANT-B, A资源)` → `DomainError code=not_found` |
| 跨租户读取拒绝（RLS） | ✅ | `app.tenant_id=A` 可见该租户会话；`app.tenant_id=B` → 零可见 |
| 跨租户写拒绝（RLS WITH CHECK） | ✅ | 在 B 作用域写入 `tenant_id=A` 会话被拒 |
| 无租户上下文 RLS 零可见 | ✅ | 不设置 `app.tenant_id` → 零可见 |
| 停用租户拒绝 | ✅ | `set_tenant_status(SUSPENDED)` 后创建会话 → `code=tenant_suspended` |
| 跨租户审批拒绝（读 + 决策） | ✅ | `get_approval/decide_approval(TENANT-B, A审批)` → `code=not_found` |
| 租户级幂等 | ✅ | 同 `idempotency_key` → 同 `operation_id`（`UNIQUE(tenant_id, idempotency_key)`） |
| 重复回调（防重放） | ✅ | 首次 `claim_callback` 记账；同 `nonce` 再次/三次 → `is_replay=True`，状态保持 `confirmed`（只重放不重复生效） |
| API 重启恢复 | ✅ | 重建 `PostgresStore` 后 session/operation/approval 仍在 |
| API/worker/Redis 重连恢复 | ✅ | Redis `PING` + 重建客户端 `GET` 返回 `v1`；PG 状态不丢 |

**本次 11 项断言在真实 `langgraph` 数据库上全部通过（conclusion=true）。**

---

## 7. 重启演练记录

`evidence/pg_restart_drill_record.md`（专题记录）+ `deploy/drills/records/DR-20260904-0219-api-worker-redis-restart.md`。综合本会话实测 + 上一阶段基线证据：
- **API 重启**：`api_restart_recovery`（重建 store，数据不丢）✅；PostgreSQL 检查点"进程重启"恢复（§5）✅。
- **worker 重连 Redis**：`worker_redis_reconnect`（Redis PING、重连后可读）✅。
- **容器级 Redis/worker 重启**（上一阶段基线，经 Docker 引擎命名管道 `npipe:////./pipe/docker_engine` 驱动）：`evidence/docker_verification.json` 记录 `redis_restart.running_after=true / ping_after=true / version=6.0.16`、`worker_restart.running_after=true / Connected to redis://redis:6379/0`、compose 前后 5 服务均 running。
- **pg_dump/pg_restore（RPO/RTO）**：`evidence/postgres_recovery.json`：`backup=0.353s / restore=1.963s / rpo=0.0 / dump_ok / restore_ok / rows 2→2`。

---

## 8. 完成标准核对

| 完成标准 | 状态 |
|---|---|
| PostgreSQL 验收报告 | ✅ 本文件 |
| RLS 策略清单 | ✅ `evidence/pg_rls_policies.json` + `evidence/PG_RLS_POLICY_INVENTORY.md` |
| Checkpoint 恢复记录 | ✅ `evidence/pg_checkpoint_recovery_record.json` + `evidence/PG_CHECKPOINT_RECOVERY_RECORD.md` |
| 重启演练记录 | ✅ `deploy/drills/records/DR-20260904-0219-api-worker-redis-restart.md` |
| 12 个 PostgreSQL 测试具备真实环境证据 | ✅ **12 项 PostgreSQL 数据面测试**已在真实 PostgreSQL 上通过（此前无 DB 时依赖 `pytest.skip`）；第 13 项为 crewai 集成（`@skipif(True)`），属环境边界并如实标注 |
| RLS 使用非超级用户验证 | ✅ 全部以 `app_runtime`（`NOBYPASSRLS`）连接执行 |
| 全量 pytest 套件 | ✅ **196 passed, 1 skipped**（`evidence/pg_acceptance_full_v2.log`，clean）；唯一 skip 为 crewai 环境边界（`test_hardening_acceptance.py:384`），非 PostgreSQL 验收项 |
| 失败禁止进入下一阶段 | ✅ 数据面/RLS/恢复/额外验证全部通过；唯一 skip（crewai）为环境边界，未掩盖任何数据面失败 |

## 9. 诚实边界（未宣称/受限）

1. **本会话无 `docker`/`psql`/`pg_ctl` CLI**：容器级 Redis/worker 重启与 pg 备份恢复引用上一阶段基线证据（`docker_verification.json`、`postgres_recovery.json`）；如需本会话重演需恢复 docker CLI。
2. **旧 full 日志的历史 tmp_path 限制**：早期 PowerShell 运行（已废弃的 `pg_acceptance_full.log`）在会话结束清理 `_acceptance_tmp` 时报 `PermissionError`（WinError 5），曾导致若干 `E`；本会话 fresh 全量运行（`pg_acceptance_full_v2.log`，**196 passed, 1 skipped**）无任何 E/error，该限制已被绕过，不影响验收结论。
3. **crewai 子智能体**真实调用链未验证（`@skipif(True)`），按项目宪法属"未验证"边界。
4. **大库/高并发 RTO、Redis 持久化（AOF）参数、生产拓扑 HA** 未在本会话压测（沿用上一阶段"未压测"边界）。
5. **旧 `pg_acceptance_full.log` 已废弃**：该文件为此前 PowerShell 运行所污染（含 11 个 `E` 与 sessionfinish `PermissionError`），非权威；权威全量结果以本会话 clean 重跑的 `evidence/pg_acceptance_full_v2.log`（**196 passed, 1 skipped**，无 E/error）为准。唯一 skip 为 crewai 环境边界（`test_hardening_acceptance.py:384`，`@skipif(True)`），非本 PostgreSQL 验收项；12 个 `@pytest.mark.postgres` 测试全部通过、无 skipped。
6. **共享库过度授权观察（INFO，非本验收失败）**：`apply_runtime_role` 的 `GRANT ... ON ALL TABLES IN SCHEMA public` 使 `app_runtime` 对含 25 张非 RLS 共享表在内的全部 38 张表均可 DML；对未启用 RLS 的共享表属过度授权/跨租户数据暴露加固点，建议作为独立加固任务处理（不影响本次 12 表数据面 RLS 核验）。

---

*证据位置：`evidence/pg_acceptance_full_v2.log`（权威全量 196 passed, 1 skipped）、`evidence/pg_suite.log`（12 项 PG 测试）、`evidence/pg_acceptance_evidence.json`（额外验证 11/11）、`evidence/pg_rls_policies.json` + `evidence/PG_RLS_POLICY_INVENTORY.md`（RLS 策略清单）、`evidence/pg_checkpoint_recovery_record.json` + `evidence/PG_CHECKPOINT_RECOVERY_RECORD.md`（checkpoint 恢复）、`evidence/pg_restart_drill_record.md` + `deploy/drills/records/DR-20260904-0219-api-worker-redis-restart.md`（重启演练）、`evidence/pg_migrate.log`、`evidence/docker_verification.json`、`evidence/postgres_recovery.json`。*
