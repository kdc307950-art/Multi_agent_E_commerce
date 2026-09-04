# Checkpoint 恢复记录 — TenantScopedCheckpointer（真实 langgraph 数据库）

> 版本：v1.1 · 2026-09-04
> 环境：PostgreSQL 14.24（Ubuntu，`127.0.0.1:55432`），数据库 `langgraph`
> 数据来源：`evidence/pg_checkpoint_recovery_record.json`（`scripts/pg_checkpoint_recovery_evidence.py`，以 app_runtime 运行角色执行，conclusion=true）
> 目标：复现《生产基线与验收测试》验收口径 —— checkpoint 经锁定版 `TenantScopedCheckpointer` 持久化，"重启"（新连接池 + 新 saver）后可恢复；跨租户恢复被拒；过期清理先清 checkpoint 再清 scope/session。

---

## 1. 结论

| 项 | 值 | 期望 | 结果 |
|---|---|---|---|
| checkpoint 写入 | 1 | 1 | ✅ |
| 重启后恢复状态值 | 1 | 1 | ✅ |
| 跨租户恢复被拒 | true | true | ✅ |
| 跨租户拒绝码 | cross_tenant_denied | cross_tenant_denied | ✅ |
| 过期清理删除数 | 1 | 1 | ✅ |
| 过期清理失败数 | 0 | 0 | ✅ |
| 清理后 session 移除 | true | true | ✅ |
| 清理后 scope 移除 | true | true | ✅ |
| **conclusion** | **true** | **true** | ✅ |

---

## 2. 过程与证据

### 2.1 进程 1：写 checkpoint
以 `TENANT-A` / 线程 `th-ck-recovery` 创建会话并 `graph.ainvoke({"v": 0})`，经 `checkpoint_request_scope` 设置 `app.tenant_id`，写入 1 个状态（`checkpoint_written=1`）。

### 2.2 进程 2（模拟 API 重启）：新连接池 + 新 saver 恢复
关闭第 1 个连接池，再用**全新** `AsyncConnectionPool` + `TenantScopedCheckpointer` 重新 `agen_state`，恢复出 `v=1`（`recovered_state_value_after_restart=1`）。**重启不丢状态**。

### 2.3 跨租户恢复被拒
以 `TENANT-B` / `other-th` 作用域读取 `TENANT-A` 线程的 checkpoint → `DomainError code=cross_tenant_denied`。**不泄露其它租户线程**。

### 2.4 过期清理：先清 checkpoint
写入 `v=0` 状态后以 `now=999999` 触发 `cleanup_expired_threads`：`deleted=1, failed=0`，且 session 与 scope 均被移除（`cleanup_session_removed=true`，`cleanup_scope_removed=true`）。**先清 checkpoint，再删 scope/session**。

---

## 3. 关联证据

- 跨租户 checkpoint 恢复拒绝与清理顺序在 `tests/test_pg_checkpoint_recovery.py` 中亦有断言（`test_cross_tenant_checkpoint_resume_rejected`、`test_cleanup_expired_scopes_deletes_checkpoint_first`；`evidence/pg_suite.log` 12 passed，且权威全量日志 `evidence/pg_acceptance_full_v2.log` 亦可佐证）。
- **全量套件佐证**：`evidence/pg_acceptance_full_v2.log` 记录 **全量 pytest 196 passed, 1 skipped**（clean，无 E/error，EXIT=0）；12 个 `@pytest.mark.postgres` 测试全部通过、无 skipped；唯一 skip 为 `test_hardening_acceptance.py:384` 的 crewai 环境边界（`@skipif(True)`），非本 PostgreSQL 验收项。checkpoint 恢复/清理均以 `app_runtime`（非超级用户，NOBYPASSRLS）执行。
- RLS 层对 checkpoint 三表的策略见 `evidence/PG_RLS_POLICY_INVENTORY.md` §3.2（`checkpoint_thread_in_current_tenant(thread_id)`）。
- API/worker 重启演练整体结论见 `evidence/pg_restart_drill_record.md` 与 `deploy/drills/records/DR-20260904-0219-api-worker-redis-restart.md`。

---

*源数据：`evidence/pg_checkpoint_recovery_record.json`（conclusion=true，timestamp 见文件内）。*
