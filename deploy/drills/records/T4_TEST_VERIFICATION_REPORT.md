# t4 多租户并发压测 / RLS 绕过 / 故障注入 / 恢复演练 —— 验证结论与证据

- 执行者：test-engineer（t4）
- 工作区：`D:\software\PythonProject1\PythonProject\Multi_agent_E_commerce`
- 时间：2026-09-04（本轮验证；RLS 静态定义层 + 真实 PG 动态 B1–B5 + SQLite 并发/故障/恢复已实测）
- **环境界限（如实）**：本机有完整 Docker preview 集群（真实 PG 容器 `after-sales-preview-postgres-1`，
  postgres:17，Up；数据库服务只在内部网络 172.30.0.0/16，宿主未发布端口）。`tests/pg_helpers.pg_available()`
  读的是宿主机 shell 的 `DATABASE_URL` 环境变量（未设）→ 本机 shell 直连不可达；但经容器网络（docker
  exec / 容器内连接）可访问真实 PG。据此，**RLS 静态定义层已实测**（本地可跑，6 例过）；**RLS 动态
  B1–B5 已在真实 PG 上以独立一次性测试库完成并通过**（见 RLS 一节）。并发压测/故障注入/恢复后一致性
  用 SQLite 后端（本机确定性可复现），其中"幂等锚点/租户级幂等/终态封闭"是不依赖调度顺序的安全不变式。

## 1. 多租户并发压测（SQLite，本机已运行）
产物：`scripts/concurrency_stress.py`（可复用脚本）、`tests/test_concurrency_stress.py`（4 例，全过）。
证据：`evidence/concurrency_stress.json`（N=64 样本，conclusion=true）。

| 场景 | 观察指标（N=64） | 结论 |
|---|---|---|
| 同租户同 idempotency_key 高并发 execute（I1） | 执行记录=1、distinct external_txn_id=1、distinct execution_id=1 | **只产生一次资金执行（幂等不重复）** ✓ |
| 不同租户同 idempotency_key 并发（I2） | 各租户各 1 条、operation 归属各租户、execution_id 不同 | **互不覆盖（租户级幂等）** ✓ |
| 同 nonce 回调并发（I3） | applied=1、replay=N-1、终态 confirmed | **CAS 只允许一个合法终态** ✓ |
| 异 nonce 回调并发（I3b） | confirmed=1、terminal_locked=N-1 | **终态封闭，不重复扣款** ✓ |

**诚实记录的一个红黄点（capitan 复核后定位为引擎并发缺陷，**已修复**）**：
- 原缺陷：`ExecutionEngine.execute` 在 live 模式的幂等检查**非原子**。同一 operation 高并发
  execute 时，多个线程都越过 `get_execution_by_operation()==None` 检查，继而各自
  `create_execution_record`（幂等返回同一记录）并**全部调用 `provider.submit`**（对外部重复提交），
  且输线程在 `_transition(record, SUBMITTED)` 读到已被推进的 SUBMITTED 状态 → 抛
  `DomainError: 执行状态跃迁非法: submitted -> submitted`。复现：N=128 时 provider.submit 达 20 次、
  1 次 submitted->submitted 异常。
- **判定**：属**引擎并发缺陷**（不是测试误报/断言过宽）——"真实沙箱不发生重复执行"这条红线此前
  实际只由 **provider 按 idempotency_key 幂等**兜底，引擎自身并未保证单次提交；这对后续接真实
  （未必严格幂等）网关构成重复扣款/重复补偿隐患，属宪法 §第四 4"并发安全"。
- **修复**：新增**单执行守卫** `store.claim_execution_submit`（Memory/Sqlite/Postgres 三后端一致），
  以 `attempts` 0→1 作为"本次 live 提交外部"的单次认领标记（仅对 status=pending_submit 的非终态
  记录认领；终态返回 False，保留 terminal_locked）。`_run_live` 先认领，非提交者直接幂等重放，
  **绝不重复提交外部**。认领在 Sqlite 单连接锁 / PG「FOR UPDATE + RLS + CAS」内原子完成，
  与 `apply_callback_atomic` 同模式。改动文件：`src/execution/engine.py`、`src/infrastructure/
  store.py`、`sqlite_store.py`、`postgres_store.py`。
- **修复后实测**：N=128/256 下 `provider.submit` **恰好 1 次**、`errors={}`（不再有 submitted->submitted）、
  执行记录=1、distinct external_txn_id=1、distinct execution_id=1；I2/I3 租户级幂等与终态封闭不变。
- **新增回归测试**：`tests/test_fault_injection.py::test_single_flight_no_duplicate_submit_or_transition_race`
  （提交恰一次、无跃迁竞态、幂等锚点不变）、`::test_single_flight_failure_no_duplicate_compensation`
  （失败路径下也收敛到单一终态、单一 reversal，无重复补偿）；并将
  `test_concurrency_stress.py::test_same_tenant_same_op_concurrent_single_execution` 断言**从严**
  （assert provider.submit==1 且 errors==[]，不再允许 submitted->submitted 例外）。

## 2. RLS 绕过测试
产物：`tests/test_rls_bypass.py`（PG-marked 动态绕过 B1–B5，可部署期跑）、
`tests/test_rls_static_definitions.py`（**已实测，6 例全过**）、`scripts/pg_rls_bypass_probe.py`（探测脚本）。

RLS 绕过测试：静态定义层实测（FORCE ROW LEVEL SECURITY + NOBYPASSRLS，6 例过）；**并在本项目 preview
集群的真实 PostgreSQL 上以独立一次性测试库 `langgraph_t4_rls` 完成动态 B1–B5 绕过
（`tests/test_rls_bypass.py -m postgres` = 5 passed，`evidence/pg_rls_bypass_probe.json` conclusion=true）**，
live `langgraph` 库未受影响。预发布/生产可按相同方式（`--database-url` 指向独立测试库）复跑确认。

- 静态检查（已实测通过）：业务表（tenants 除外）+ 官方 checkpoint 表全部 `ENABLE + FORCE ROW LEVEL SECURITY`；
  每表 policy 用 `app_current_tenant_id()` 做 USING+WITH CHECK；checkpoint 表用
  `checkpoint_thread_in_current_tenant(thread_id)` 关联；运行角色 app_runtime 定义为
  `LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`。
- 动态 B1–B5（真实 PostgreSQL 已通过）：B1 跨租户直连 SQL 查询零可见；
  B2 跨租户直连写被 WITH CHECK 拒绝；B3 绕过应用层直接改 tenant_id 被拒（原行零污染）；B4 运行角色
  app_runtime = NOBYPASSRLS 且非超管（无法越权，备份/超管连接天然绕过 RLS 属 PostgreSQL 语义，由角色分离
  规避）；B5 FORCE RLS 生效 + 未设作用域零可见 + 跨租户零可见。
- 可复跑：`scripts/pg_rls_bypass_probe.py --database-url postgresql://migrator:<POSTGRES_PASSWORD>@postgres:5432/langgraph_t4`
  （指向独立一次性测试库）；`tests/test_rls_bypass.py -m postgres` 保持 PG-marked 可部署期跑。运行角色凭据
  由 `APP_RUNTIME_USER` / `APP_RUNTIME_PASSWORD` 注入（从 deploy/.env.preview 读取，勿写日志）。

### RLS 真实 PG 动态探测记录
- 方式：复用 `after-sales-preview-api-1`（含 sqlalchemy 2.0.52 / psycopg 3.3.5 / pytest 9.1.1），
  在其内部以 `-m postgres` 运行 `tests/test_rls_bypass.py`，**仅指向临时独立测试库**。
- 隔离约束：由于 B1–B5 的 fixture 会调用 `reset_pg_schema`（破坏性 DDL），先在预览 postgres 集群上**单独建**
  一次性测试库 `langgraph_t4_rls`（以 migrator 建成 + 授予 app_runtime CONNECT），并把 `DATABASE_URL` 指向它；
  跑完后已 **drop 该测试库并 REVOKE CONNECT**，live `langgraph` 库未受影响（14 张表仍在）。使用独立隔离
  测试库只是避免污染 live 数据的标准做法，**不降低验证有效性**。
- 连接注入：`DATABASE_URL=postgresql://migrator:<POSTGRES_PASSWORD>@postgres:5432/langgraph_t4_rls`，
  `APP_RUNTIME_USER=app_runtime`、`APP_RUNTIME_PASSWORD=<APP_RUNTIME_PASSWORD>`（从 deploy/.env.preview
  读取，未打印/未写日志）；容器网络内主机名 `postgres:5432`。
- 结果：`tests/test_rls_bypass.py -m postgres` = 5 passed；`scripts/pg_rls_bypass_probe.py` conclusion=true，
  证据 `evidence/pg_rls_bypass_probe.json`。

## 3. 故障注入
产物：`tests/test_fault_injection.py`（8 例，全过）。清单：F1 provider 提交超时→FAILED_UNCERTAIN、
F2 提交 5xx 不确定→FAILED_UNCERTAIN、F3 外部明确失败→补偿 COMPENSATED、F4 补偿失败→COMPENSATION_FAILED 转人工、
F5 回调重放/重复通知→replay 不重复生效、F6 回调签名错误→signature_invalid 拒绝、F7 网关 down→提交不确定+
对账 mismatch 转人工、F8 补偿幂等（多次扫→单一稳定 reversal_id，终态封闭）。
- 结论：执行链路对上述异常**统一收敛到 submitted→reconcile→confirmed/mismatch/human_handoff**，
  **不重复执行、补偿幂等、终态不可覆盖**。

## 4. 恢复演练（聚焦"恢复后一致性"）
边界：dr-engineer（灾备）已负责备份可恢复性 / RPO / RTO / 加密 / 异机副本 / 最小权限备份账号
（见 `deploy/drills/records/DR-20260904*-pg-encrypted-restore.md`、`evidence/postgres_recovery.json`）。
本 t4 在**恢复后一致性**上补充验证：
产物：`scripts/recovery_consistency_drill.py`、`tests/test_recovery_consistency.py`（2 例转 SQLite 通过，1 例 PG skip）、
`deploy/drills/records/recovery-consistency-<ts>.json`、`evidence/recovery_consistency.json`（conclusion=PASS）。
- 一致性校验（恢复后全过）：C1 幂等键唯一（同租户同幂等键仅 1 条；跨租户同幂等键字符串互不覆盖）、
  C2 执行锚点唯一（一个操作至多 1 条执行记录）、C3 审批唯一、C5 审批终态（approved）保留不漂移、
  C4 租户边界（跨租户零可见）、审计可追溯且租户归属正确、恢复后幂等重放语意保留。

## 结论
- **不触真实资金**，全部使用 mock/sandbox provider；无任何重复扣款/重复退款/重复执行。
- 并发压测、故障注入、恢复后一致性在 SQLite 后端**实测通过**并留存证据；并发压测红黄点已
  **定位为引擎并发缺陷并已修复+回归**（见上）。
- **RLS 绕过测试：✅ 通过（真实 PostgreSQL 动态验证）**。静态定义层实测（6 例过，FORCE ROW LEVEL SECURITY +
  NOBYPASSRLS）；并在预览集群的真实 PostgreSQL 上以独立一次性测试库 `langgraph_t4_rls` 完成动态 B1–B5
  绕过（`tests/test_rls_bypass.py -m postgres` = 5 passed，`evidence/pg_rls_bypass_probe.json` conclusion=true），
  live `langgraph` 库未受影响。预发布/生产可按相同方式（`--database-url` 指向独立测试库）复跑确认。
- 其余（恢复后一致性、并发压测）在本机 SQLite 已实测通过；真实 PG 并发压测建议在独立测试库/临时
  schema 上按 captain 统筹补充（已说明连接注入方式，见上报消息）。
