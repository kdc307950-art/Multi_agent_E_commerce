# 重启演练记录 — API / worker / Redis / PostgreSQL

> 版本：v1.0 · 2026-09-04
> 目标：验证「API / worker / Redis 重启后的状态恢复」，确认会话、审批、操作与租户边界不丢失、不串租户、不重复执行。
> 环境：Preview 服务器 PostgreSQL 14.24（`127.0.0.1:55432/acceptance`）+ Redis 6.0.16（`127.0.0.1:6379`）。

---

## 1. 演练矩阵

| 重启对象 | 演练方式 | 恢复验证 | 状态 |
|---|---|---|---|
| **API**（应用进程） | 重建 `PostgresStore`（新连接池 = 应用重启）后重读数据 | session / operation / approval 仍在；operation 状态不变 | ✅ |
| **API / Graph 检查点进程** | 写 checkpoint → 关闭连接池 → 新建连接池 + 新 saver 重新读取 | 状态恢复为 `v=1`（重启不丢） | ✅ |
| **worker（Celery）** | 重建异步连接池 + 重新连接 Redis broker | Redis PING + 重建客户端可读；任务注册恢复 | ✅ |
| **Redis** | 重建 Redis 客户端（模拟断线后重连） | `SET`/`GET` 可达，数据可回读 | ✅ |
| **Redis 容器 / worker 容器**（上一阶段基线） | Docker 引擎命名管道 `restart` 容器 | `running_after=true`、`PING=true`、`Connected to redis://redis:6379/0`、`memory.write_tick` 注册、compose 前后 5 服务 running | ✅ |
| **PostgreSQL 备份恢复**（上一阶段基线） | `pg_dump -Fc` → 新库 `pg_restore` | `RPO=0.0s`、`restore=1.963s`、`rows 2→2` | ✅ |

---

## 2. 本会话实测证据

### 2.1 API 重启（重建 store）
`evidence/pg_acceptance_evidence.json` →
```
api_restart_recovery: {
  "ok": true,
  "detail": "rebuild store -> session/operation/approval survive"
}
```

### 2.2 Graph 检查点进程重启（新池 + 新 saver）
`evidence/pg_checkpoint_recovery_record.json` →
```
checkpoint_written               = 1
recovered_state_value_after_restart = 1
```
写入检查点后关闭连接池，再用全新 `AsyncConnectionPool` + `TenantScopedCheckpointer` 重新取状态，`v=1` 恢复。
跨租户恢复被拒（`cross_tenant_denied`），不泄露其它租户线程。

### 2.3 worker / Redis 重连
`evidence/pg_acceptance_evidence.json` →
```
worker_redis_reconnect: {
  "ok": true,
  "detail": { "available": true, "ping": true, "reconnect_get": "v1" }
}
```

---

## 3. 上一阶段基线（容器级，Docker 引擎命名管道驱动）

`evidence/docker_verification.json`（经 `npipe:////./pipe/docker_engine` 驱动）：
- `redis_restart`: `restarted=true / running_after=true / ping_after=true / version_after=6.0.16`。
- `worker_restart`: `restarted=true / running_after=true / log_tail=Connected to redis://redis:6379/0 ... [tasks] memory.write_tick`。
- `compose_status_before` / `compose_status_after`: redis / postgres / frontend / api / worker 5 服务均 `running`。

`evidence/postgres_recovery.json`（pg_dump/pg_restore）：`backup=0.353s / restore=1.963s / rpo=0.0 / dump_ok=true / restore_ok=true / rows_seeded=2 / rows_restored=2`。

---

## 4. 结论

- 数据面状态（会话、审批、操作、检查点）持久化于 PostgreSQL，**应用/worker 重启不丢失**；Redis 仅承载 broker/锁等瞬态，**断线重连后可恢复**。
- 重启后没有出现跨租户读取/审批/恢复（`cross_tenant_denied` / `not_found`），**租户边界在重启路径上保持**。
- 重复回调（同 `nonce`）在重启/重连后仍只重放不重复生效（`claim_callback` CAS，见 `pg_acceptance_evidence.json`）。
- **边界**：本会话无 `docker` CLI，容器级 Redis/worker `restart` 演练引用上一阶段 `docker_verification.json` 基线；如需在本会话重演容器级重启，需先恢复 `docker`/`docker.exe` CLI 与镜像。

---

*证据：`evidence/pg_acceptance_evidence.json`、`evidence/pg_checkpoint_recovery_record.json`、`evidence/docker_verification.json`、`evidence/postgres_recovery.json`。*
