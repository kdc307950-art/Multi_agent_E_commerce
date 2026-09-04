# 预发布演练记录 — API / worker / Redis 重启（真实 langgraph 数据库）

> 每执行一次演练各复制一份，填写真实结果；存于 `deploy/drills/records/`。
> 记录须可复核：时间、操作号、通过项、失败项、关键证据（审计/操作ID）、签发人。

## 基本信息
- 演练编号：DR-20260904-0219
- 环境：`preview`（PostgreSQL 14.24 @ `127.0.0.1:55432`，数据库 `langgraph`；Redis 6.0.16 @ `127.0.0.1:6379`）
- 执行人 / 复核人：evidence-compiler / reviewer (pg-acceptance-continue)
- 开始时间 / 结束时间：2026-09-04 02:19 / 2026-09-04 02:19（本会话实测；容器级基线引用上一阶段）
- 应用 git 引用：`pg-acceptance-continue`（见仓库 HEAD）
- 上线租户白名单：TENANT-A / TENANT-B（验收构造，非生产租户）

> 演练目标：验证「API / worker / Redis 重启后的状态恢复」，确认会话、审批、操作与租户边界不丢失、不串租户、不重复执行。所有业务读写均以运行角色 `app_runtime`（LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS）执行。

## 演练项结果

| # | 演练项 | 操作号/证据ID | 结果 | 时长 | 备注 |
|---|--------|--------------|------|------|------|
| D1 | API/worker/Redis 重启 | `pg_acceptance_evidence.json`（api_restart_recovery / worker_redis_reconnect） | PASS | — | 重建 store 后 session/operation/approval 仍在；Redis PING + 重连 GET=v1 |
| D1-a | 跨租户恢复被拒（重启路径） | `pg_checkpoint_recovery_record.json`（cross_tenant_denied） | PASS | — | 新池+新 saver 恢复不泄漏其它租户线程 |
| D1-c | Container 级 Redis/worker 重启（基线） | `docker_verification.json` | PASS | — | redis/worker restart running_after=true，connect OK；compose 5 服务 running |
| D2 | PostgreSQL 备份恢复 | `postgres_recovery.json` | PASS | 0.353s bk / 1.963s rs | RPO=0.0，rows 2→2，dump/restore OK |
| D3 | 审批中断恢复 | `pg_acceptance_evidence.json`（cross_tenant_approval_rejected） | PASS | — | 跨租户审批读+决策均 not_found；仅审批后执行 |
| D5 | 并发重复提交 / 重复回调 | `pg_acceptance_evidence.json`（tenant_idempotency / duplicate_callback_replay_safe） | PASS | — | 同 idempotency_key→同 operation_id；同 nonce 只重放不重复生效 |

## 关键证据（可追溯）
- **API 重启（重建 PostgresStore）**：`evidence/pg_acceptance_evidence.json` → `api_restart_recovery ok=true`（rebuild store -> session/operation/approval survive）。
- **Graph 检查点进程重启（新池 + 新 saver）**：`evidence/pg_checkpoint_recovery_record.json` → `checkpoint_written=1`、`recovered_state_value_after_restart=1`、跨租户 `cross_tenant_denied`。
- **worker / Redis 重连**：`evidence/pg_acceptance_evidence.json` → `worker_redis_reconnect`（`available=true, ping=true, reconnect_get=v1`）。
- **容器级 Redis/worker 重启（上一阶段基线）**：`evidence/docker_verification.json`（`redis_restart.running_after=true / ping_after=true / version_after=6.0.16`；`worker_restart.running_after=true / Connected to redis://redis:6379/0`；compose 前后 5 服务 running）。
- **pg_dump/pg_restore（RPO/RTO）**：`evidence/postgres_recovery.json`（`backup=0.353s / restore=1.963s / rpo=0.0 / dump_ok / restore_ok / rows 2→2`）。

## 异常与处理
- 出现的异常 / 回滚动作：无业务异常。全量 pytest 以 clean 重跑 `evidence/pg_acceptance_full_v2.log` 记录 **196 passed, 1 skipped**（无 E/error，EXIT=0）；唯一 skip 为 `test_hardening_acceptance.py:384` 的 crewai 环境边界（`@skipif(True) reason=本环境未安装 crewai`），非本 PostgreSQL 验收项。旧 `evidence/pg_acceptance_full.log` 为此前 PowerShell 运行污染（含 11 个 E 与 sessionfinish PermissionError），已由 v2 权威日志替代。
- 值班处理人：evidence-compiler

## 结论
- 是否达到预发布验收：**通过**
- 上线阈值是否满足：**是**（数据面状态持久化于 PostgreSQL，应用/worker 重启不丢失；Redis 断线重连可恢复；重启后租户边界保持，无跨租户读取/审批/恢复；重复回调只重放不重复生效）
- 边界（如实标注）：本会话无 `docker`/CLI，容器级重启与 pg 备份恢复引用上一阶段基线（`docker_verification.json`、`postgres_recovery.json`）；如需本会话重演容器级重启需恢复 docker CLI。
- 签名：evidence-compiler
