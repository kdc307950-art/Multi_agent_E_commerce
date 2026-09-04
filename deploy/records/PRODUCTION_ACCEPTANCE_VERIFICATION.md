# 生产运行能力综合验收报告（不触达真实资金）

> 目标：在不触达真实资金的前提下，完成"生产运行能力"验证并达到用户验收标准。
> 范围：资金/业务执行面（FundsProvider 真实网关沙箱）、幂等与审批、灾备与恢复、有界指标与告警、多租户并发压测 / RLS 绕过 / 故障注入 / 恢复演练。
> 方法：团队分域实施（资金网关 / 灾备 / 可观测 / 压测·RLS·故障注入），全部在本地+自部署沙箱验证，未触真实资金；对无法在本机跑通的环境依赖如实标注为"部署期执行项"，不伪造证据。
> 评估基准：AGENTS.md「电商售后多智能体工单系统 — Agent 宪法」安全红线与可验证要求；《生产基线与验收测试》《生产环境架构设计》等设计文档口径。

- 生成时间：2026-09-04（团队多智能体协同，任务 t1–t8 完成后汇总）
- 全量回归基线：非 PG 测试 **390 passed** / 1 skipped（需 PG）；沙箱相关 32–73 项全通过。

---

## 一、结论摘要

| # | 验收项 | 结论 | 主要证据 |
|---|--------|------|---------|
| 1 | 真实沙箱不发生重复执行 | ✅ 达成 | 引擎原子单执行守卫 + 租户级幂等键 + 终态封闭 + 沙箱服务端幂等；128/256 并发 `provider.submit` 恰 1 次 |
| 2 | 回调、补偿、对账均可追溯 | ✅ 达成 | 8 个审计 action（含补偿/对账 token 级 `append_audit`）+ 数据面字段回溯 |
| 3 | 恢复目标与备份目标有实测证据 | ✅ 达成（compose 容器实跑为部署期执行项） | 加密备份 + 最小权限 backup_role + 异机副本 + 恢复演练；实测 RTO=0.093s / RPO=0.877s |
| 4 | 告警可触发、可定位、可关闭 | ✅ 达成（完整运行时 Prometheus 复验为部署期执行项） | 15 条规则：14 条可触发+可关闭；RPO/RTO gauge 写路径已闭合 |
| 5 | 多租户并发压测 / RLS 绕过 / 故障注入 / 恢复演练 | ✅（真实 PostgreSQL 动态 RLS B1–B5 已通过） | 并发矩阵 + 真实 PG RLS 动态 B1–B5 + 故障注入 8 例 + 恢复一致性 PASS |

---

## 二、逐项验收证据与缺口

### 1. 真实沙箱不发生重复执行（达成）
- **引擎层（根治并发缺口）**：
  - `allocate_execution`/`create_execution_record` 原子化：MemoryStore 在 `_decision_lock` 内原子 check-then-insert；SQLite/PG 靠 `UNIQUE(tenant_id, operation_id)` 兜底 —— 同一 operation 只落一条 execution record（修复前 MemoryStore 无锁 check-then-insert 可产生多条）。
  - `claim_execution_submit`（attempts 0→1）单执行守卫：只放行一个线程成为"本次提交外部"者，其余线程读最新记录走 `_replay_outcome` 幂等重放，**绝不二次调用 `provider.submit`**（不依赖 provider 幂等兜底）。
  - `update_execution_record` 三后端均加 `expected_status` CAS（`WHERE status=expected`，落空返回 None）；`_transition` 透传 expected_status，CAS 落空即读回当前记录幂等重放，**不再抛 `submitted -> submitted`**。
  - 证据：`tests/test_sandbox_concurrency_guard.py`（5 用例，MemoryStore 最弱后端 + 延迟注入放大窗口：并发只落 1 条 record、`submit` 恰好=1、无 submitted→submitted 抛错、CAS 落空重放、回调同 nonce 并发恰 1 confirmed、并发补偿幂等不重复）；`scripts/concurrency_stress.py --backend sqlite --concurrency 128`（`provider_submit_calls=1`，`I1_ok`/`I3_ok`/`cross_tenant_no_overwrite` 全 true，`errors={}`）。
- **沙箱层**：自部署 FastAPI 网关沙箱 `src/execution/sandbox_gateway.py`，服务端 SQLite `UNIQUE(tenant_id, idempotency_key)` + 事务内 `ON CONFLICT DO NOTHING` + 读回；同租户同键重复 submit 返回同一 `external_txn_id`，进程重启仍单行（持久化幂等，非内存 dict）。
- **端到端**：`tests/test_sandbox_e2e_flow.py`（14 用例，真实 HTTP 沙箱链路）——16 并发同键同一 `external_txn_id`；同 operation 二次 execute 同 execution_id、`provider.submit` 仅 1 次；同 nonce 重投→`replay`；终态后新 nonce→`terminal_locked`（防重复扣款/退款）。
- **真实网关沙箱高并发压测（红线级确证）**：`scripts/sandbox_concurrency_stress.py`（真实 HTTP sandbox_gateway + 引擎单执行守卫，sqlite/memory 后端，N=64/128/256 全绿），`evidence/sandbox_concurrency_stress.json`（sqlite N=256）。I1 同 operation N=256 并发 `execute` → `provider.submit` 恰 **1 次**（修复前达 20 次）、同一 external_txn_id、execution record=1、`errors={}`；FAIL 失败路径（沙箱 default_status=failed 真实 HTTP 注入）N=256 → `provider.submit=1`、`provider.compensate=1`、`distinct_reversal_id=1`（单一反转、无重复补偿）、收敛单一终态 compensated；I2 不同租户同幂等键并发 → 各租户各自 1 条、互不覆盖。真实网关即使不严格幂等，引擎也只对外发起一次。
- **缺口**：无（红线由引擎守卫 + 租户级幂等键 + 终态封闭 + 服务端幂等共同保证）。

### 2. 回调、补偿、对账均可追溯（达成）
- 审计 action（用执行记录的可信 `tenant_id` 写入，绝不用请求体不可信 tenant_id）共 8 个：
  `session.create` / `execution.create` / `approval.decide` / `execution.callback.confirmed` / `execution.compensated` / `execution.compensation_failed` / `execution.reconcile.confirmed` / `execution.reconcile.mismatch`。
- 补偿/对账分支在 `engine.py` 方法体内惰性 `store.append_audit` 补齐（无顶层 src import、不引循环；行为增量，不改状态机/幂等/回调）。经 `store.search_audit` 断言验证。
- 证据：`evidence/sandbox_e2e_evidence.json`（`audit_actions_verifiable` 列出全部 8 个 action，`audit_note` 说明写入方式与验证）；`store.search_audit` / `get_execution_record` / `get_operation` 查询断言。
- 缺口：无。

### 3. 恢复目标与备份目标有实测证据（达成，compose 容器实跑为部署期执行项）
- **加密备份**：`deploy/scripts/backup_encrypted.sh` + `preview_backup_scheduler.sh`，最小权限 `backup_role` 只读 `pg_dump -Fc` → `openssl aes-256-cbc -pbkdf2 -salt`（明文归档不落盘）→ `.dump.enc` + `.sha256`；密钥仅环境变量注入（`BACKUP_ENC_KEY`）。
- **最小权限备份账号**：`migrations.apply_backup_role()` 建 `backup_role`（`LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS`，仅 CONNECT/USAGE/SELECT，回收 public/DATABASE CREATE）；实测**写/DDL 均 Permission denied**，读回租户数据成功。
- **异机副本**：`REMOTE_BACKUP_DIR` / `scp` / `rsync` 推送代码库外受控目录 + `BACKUP_KEEP` 保留。
- **恢复演练**：`deploy/scripts/restore_drill.sh`，解密→SHA-256 复核→恢复独立临时库→采样校验（TENANT-A/B 保留、快照点后 marker 排除、RLS 策略数）→实测 RPO/RTO。
- **实测**：`evidence/dr_encrypted_backup.json`（`cipher_non_plain=1`、`decrypt_ok=1`、`sha256_ok=1`、`offsite_copied=1`）；`deploy/drills/records/drill-pg-encrypted-restore.json`（`rto_restore_seconds=0.09284`、`rpo_measured_seconds=0.876558`、`restore_ok=true`、`tenant_a/b_preserved=1`、`marker_excluded=0`）；`evidence/dr_backup_role_least_privilege.json`。
- **结论变更**：原"同机 pg_dump 即完整灾备"已被"加密归档 + SHA-256 + 最小权限备份角色 + 异机副本 + 实测 RPO/RTO"取代。
- **缺口/边界**：本机无 Docker 时用 WSL PG14 等价复刻 `apply_backup_role` 并行演练；compose 容器侧（`verify_dr_compose.sh`）仅为静态校验（bash -n/YAML），**完整容器实跑属部署期执行项**（见 §四）。

### 4. 告警可触发、可定位、可关闭（达成，完整运行时 Prometheus 复验为部署期执行项）
- **有界指标打点**（全部有界标签，杜绝 `tenant_id/user_id/order_id/operation_id/thread_id`，`_FORBIDDEN_LABELS` 仍生效）：`api_requests_total{route,method,status}`、`approval_decisions_total{route,status}` + `approval_decision_latency_seconds` 直方图、`reconcile_mismatch_total{kind}` + `reconcile_last_run_timestamp_seconds` gauge、`human_intervention_total{kind}`、`security_denials_total{kind}`、`execution_submit_total{mode}`/`execution_outcome_total{mode,status}`、`drill_rpo_seconds`/`drill_rto_seconds` gauge。修复了 metrics 渲染同指标名多系列重复 `# TYPE`（否则 Prometheus 抓取报错）。
- **规则 ↔ 指标映射**：`deploy/observability/alert-rules.yml` 对齐真实指标/标签；`ApiHighErrorRate`/`ApprovalFailureRateHigh`/`ApprovalLatencyHigh`/`ReconciliationMismatch`/`NoReconciliation`/`HighHumanInterventionRate`/`TenantDenialSpike`/`CrossTenantAccessDetected`/`RpoExceeded`/`RtoExceeded` 等表达式均能命中真实打点。
- **可触发/可关闭验证（t7）**：自建 PromQL 子集求值器 `deploy/observability/evidence/eval_alert_rules.py` + 命中阈值/事件消逝两套合成样本；结构校验（表达式可解析、`for` 合法、annotations 齐全）→ **issues=0**。**14/15 条可触发✅ + 可关闭✅**；`ComponentRestartDetected` 依赖 `component_restart_total` 未打点 → 恒不触发（合法，不误报）。
- **RPO/RTO gauge 写路径闭合（t8）**：灾备脚本实测后 `dr_backfill_gauges()` 回填 `drill_rpo/rto_seconds{component="pg_backup"}` 状态文件 → api `/api/metrics` 渲染前 `_load_drill_gauges()` 合并 → Prometheus scrape `job_name=api` → 规则 `>900`/`>3600` 触发。实测 WSL 恢复演练 `RTO=0.094207s`、`RPO=0.815516s`（未超阈值故不触发——正确，>900/3600 才告警）。
- **证据**：`deploy/observability/evidence/ALERT_RULES_VERIFICATION.md`（汇总表 + 逐条触发/定位/关闭 + 规则↔指标映射 + 缺口）、`deploy/observability/evidence/DR_GAUGE_BACKFILL.md`、`deploy/drills/metrics/drill_gauges.json`。
- **缺口/边界**：t7 用求值器等价证据（环境无 promtool）；t3/t7/t8 为合成样本 + 静态/逻辑等价验证，**完整运行时（容器读挂载 → /api/metrics → Prometheus → 告警）属部署期 `docker compose up` 后执行 `verify_dr_compose.sh` 复验**（见 §四）。

### 5. 多租户并发压测 / RLS 绕过 / 故障注入 / 恢复演练
- **多租户并发压测** ✅：`scripts/concurrency_stress.py` + `tests/test_concurrency_stress.py`，`evidence/concurrency_stress.json`（N=64/128/256）。`I1` 同租户同 operation 并发 → `provider_submit_calls=1`、`execution_records_for_op=1`、`distinct_execution_id=1`（幂等不重复）；`I2` 跨租户同幂等键 → 各租户各 1 条互不覆盖；`I3` 终态封闭（同/异 nonce 回调并发 → CAS 只一个 confirmed，其余 terminal_locked）。
- **RLS 绕过测试** ✅：静态定义层实测（`FORCE ROW LEVEL SECURITY` + `NOBYPASSRLS`，`tests/test_rls_static_definitions.py` 6 例过） + **真实 PostgreSQL 容器动态绕过 B1–B5 全部通过**（`tests/test_rls_bypass.py -m postgres` = 5 passed；`evidence/pg_rls_bypass_probe.json` conclusion=true）。方式：复用 preview `api` 容器（已含 sqlalchemy/psycopg/pytest）；在预览 postgres 集群以 migrator 建一次性测试库 `langgraph_t4_rls` 并给 app_runtime 授 CONNECT，`DATABASE_URL` 指向它，跑完 drop 测试库 + REVOKE CONNECT，**live `langgraph` 库未受影响**（确认 14 张业务表仍在、数据库列表仅 langgraph/postgres）。逐项：B1 跨租户直连 SQL 查询零可见；B2 跨租户直连写被 `WITH CHECK` 拒绝；B3 绕过应用层直接改 tenant_id 被拒（原行零污染、跨租户仍零可见）；B4 运行角色 app_runtime=NOBYPASSRLS 且非超管（无法越权；备份/超管连接天然绕过 RLS 属 PostgreSQL 语义，由角色分离规避）；B5 `FORCE ROW LEVEL SECURITY` 生效 + 未设作用域零可见 + 跨租户零可见。B3 读回改为新事务（证非 RLS 漏洞，属测试事务性问题，与 test_pg_rls.py 一致）。
- **故障注入** ✅：`tests/test_fault_injection.py`（8 例）—— timeout/5xx → `FAILED_UNCERTAIN`、明确失败 → `COMPENSATED`、补偿失败 → `human_handoff`、回调重放 → `replay`、签名错 → `signature_invalid`、网关 down → `mismatch` 转人工、补偿幂等/终态封闭；复用 t1 沙箱故障注入原语（`src/execution/sandbox_faults.py`：`X-Sandbox-Fault` 注入 timeout/http_500/http_503 等）。
- **恢复演练** ✅：`scripts/recovery_consistency_drill.py` + `tests/test_recovery_consistency.py` → `evidence/recovery_consistency.json`（PASS）：幂等键唯一、执行锚点唯一、审批唯一、租户边界、审批终态保留、审计可追溯。
- **补充**：探测发现 MemoryStore 后端本无 DB 唯一约束兜底（同 operation 并发可产生多条 record），已根治（见 §1），这是"真实网关不严格幂等即重复执行"的真实风险源。

---

## 三、安全合规对照（宪法红线）

| 宪法要求 | 结论 |
|---|---|
| 完全自托管（无第三方托管 SaaS） | ✅ 网关沙箱/LLM 端点/检索/可观测均自部署或受控内网；沙箱网关为本地服务 |
| 不触真实资金 | ✅ 全程沙箱（SQLite 服务端幂等、无真实扣款/退款副作用）；live 须显式 provider 且 fail-closed |
| 租户级幂等键（含 tenant_id，绝不含 attempt） | ✅ `UNIQUE(tenant_id, idempotency_key)` + `ON CONFLICT DO NOTHING`；幂等键沿 `oprefund:{tenant_id}:{order}:{request_id}` |
| 唯一 `human_approval`，无 direct 绕过 | ✅ 退款/退货/改址均走唯一审批 interrupt；审批拒绝→REJECTED 且无执行记录（无 direct 绕过） |
| 数据面租户作用域（RLS / NOBYPASSRLS） | ✅ app_runtime（NOBYPASSRLS）+ RLS；备份角色仅只读（BYPASSRLS 以读出全量，写/DDL 仍被拒） |
| 不引入高基数标签 | ✅ `_FORBIDDEN_LABELS` 拒绝 tenant_id/user_id/order_id/operation_id/thread_id；全部指标为有界维度 |
| 关键写路径 fail-closed 转人工 | ✅ 超时未确认/外部未知/补偿失败/审批超时 → mismatch/human_handoff 转人工，绝不静默当成功 |

---

## 四、边界与部署期执行项（诚实标注，不伪造）

1. **灾备 compose 容器实跑**：本机无 Docker 运行时（不含 docker CLI PATH），`backup_encrypted.sh`/`restore_drill.sh`/`verify_dr_compose.sh` 的 compose 模式仅做静态校验（bash -n / YAML safe_load）；实测以 WSL PG14 直接 DSN 复刻 `apply_backup_role` 完成（RPO/RTO 实测有效）。**需在预发布/生产服务器 `docker compose up` 后跑 `deploy/drills/verify_dr_compose.sh` 做容器实跑复验**。
2. **告警完整运行时**：t3/t7/t8 为合成样本 + 求值器等价验证；**需在 preview 起 Prometheus 抓取运行中 api `/api/metrics` 并对 `RpoExceeded/RtoExceeded` 注入超阈值样本（或人工置大 RPO/RTO）做一次端到端触发复验**。
3. **RLS 动态真实 PG 补验**（已在本机完成）：在预览 postgres 集群以独立一次性测试库 `langgraph_t4_rls` 完成 B1–B5 动态绕过（`tests/test_rls_bypass.py -m postgres` = 5 passed），跑完 drop 测试库 + REVOKE CONNECT，**live `langgraph` 库未受影响**（14 张业务表仍在）。证据 `evidence/pg_rls_bypass_probe.json`（conclusion=true）。预发布/生产可按相同方式（`--database-url` 指向独立测试库）复跑确认。
4. **`execution.provider=sandbox_http` 需配置 `gateway_base_url`/`gateway_api_key`** 且受限环境缺 base_url 即 fail-closed（`gateway_unconfigured`）；生产/预发布启用 live 前须核对 `EXECUTION_MODE=live` + 显式 provider。

---

## 五、建议（生产落地前）

1. **替换当前旧明文备份**：运行中的 preview `backup` 容器目前仍是"明文 owner `pg_dump`"旧脚本（`.env.preview` 无 `BACKUP_ROLE_PASSWORD`/`BACKUP_ENC_KEY`）。部署时先注入两个密钥、重建 migrate 创建 `backup_role`，再恢复加密备份侧车（`docker compose up -d`），消除"owner 明文备份"隐患。
2. 按 §四 在预发布/生产服务器完成容器实跑复验（灾备、告警端到端、RLS 动态），并把复验 RPO/RTO 与告警触发记录归档。
3. `ComponentRestartDetected` 如需生效，需编排层上报 `component_restart_total`（当前恒不触发，属合法不误报；按需补打点）。
4. 告警阈值（错误率 5%、审批失败率 5%、人工介入率 20%、RPO 900s/RTO 3600s）为基线建议值，生产正式基线须与业务/SLA 对齐后再固化。
5. **同一 operation 双写口**：确认所有写路径（超级用户/平台运维/后台任务）均走引擎单执行守卫与租户级幂等键，避免绕过。

---

## 六、关键证据文件清单

**资金/执行面**：`src/execution/sandbox_gateway.py`、`src/execution/sandbox_faults.py`、`src/execution/provider.py`、`tests/test_sandbox_http_provider.py`、`tests/test_sandbox_e2e_flow.py`、`tests/test_sandbox_concurrency_guard.py`、`scripts/sandbox_concurrency_stress.py`、`evidence/sandbox_e2e_evidence.json`、`evidence/sandbox_concurrency_stress.json`
**灾备**：`deploy/scripts/backup_encrypted.sh`、`deploy/scripts/restore_drill.sh`、`deploy/scripts/preview_backup_scheduler.sh`、`deploy/drills/verify_dr_compose.sh`、`src/infrastructure/migrations.py`、`src/infrastructure/migrate_cli.py`、`deploy/DR_KEY_MANAGEMENT.md`、`evidence/dr_backup_role_least_privilege.json`、`evidence/dr_encrypted_backup.json`、`deploy/drills/records/drill-pg-encrypted-restore.json`
**指标/告警**：`src/observability/metrics.py`、`src/api/routes.py`、`deploy/observability/alert-rules.yml`、`deploy/observability/evidence/ALERT_RULES_VERIFICATION.md`、`deploy/observability/evidence/DR_GAUGE_BACKFILL.md`、`deploy/observability/evidence/eval_alert_rules.py`、`deploy/scripts/drill_metric_exporter.py`
**压测/RLS/故障/恢复**：`scripts/concurrency_stress.py`、`tests/test_concurrency_stress.py`、`tests/test_rls_static_definitions.py`、`tests/test_rls_bypass.py`、`scripts/pg_rls_bypass_probe.py`、`tests/test_fault_injection.py`、`scripts/recovery_consistency_drill.py`、`evidence/concurrency_stress.json`、`evidence/pg_rls_bypass_probe.json`、`evidence/recovery_consistency.json`、`deploy/drills/records/T4_TEST_VERIFICATION_REPORT.md`、`deploy/drills/records/T4_RACE_REVIEW_submitted_to_submitted.md`

---

*报告由多智能体团队（资金网关/灾备/可观测/测试压测）协同产出，队长（captain）汇总核对；所有涉及"真实库级/容器级/告警运行时"的复验项已在 §四 诚实标注为部署期执行项。*
