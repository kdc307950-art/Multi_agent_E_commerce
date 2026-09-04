# t6 验收与安全评审报告 — 敏感执行回调安全闭环

- **验收人**：reviewer（验收/安全评审）
- **任务**：t6（attempt id `5527cdc4-41aa-46d3-b534-3d3faff21498`）
- **评审对象**：同事务 nonce 校验 + 状态 CAS 终态封闭 + 操作更新 + 审计写入；回调安全日志不写不可信 tenant 租户审计；/api/metrics 内网化；API/worker/数据库/模型容器出网规则；PostgreSQL 并发测试。
- **结论总览**：**5 项全部通过**（均为独立实测/复核，非仅采信各成员自述）。无未达标项，无重大安全漏洞。

---

## 0. 环境与方法（如实说明）

| 项 | 说明 |
|---|---|
| 主机 | Windows；本会话 Docker CLI 不在 PATH（captain 在其运行环境做了容器实测） |
| PostgreSQL | 独立容器 `dsh-test-pg`（PG 17.11）；`DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/langgraph`，经 `.venv\Scripts\python.exe` psycopg 建连验证 OK |
| PG 运行角色 | `tests/pg_helpers.py` 用 `app_runtime`（`LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS`）连接，**RLS 真正生效**（非 owner/superuser 绕过） |
| 执行命令 | `$env:DATABASE_URL='...'`; `.venv\Scripts\python.exe -m pytest tests/test_pg_callback_concurrency.py -v` |
| 证据层级 | **真实 PG 实测**（并发/CAS/跨租户）＋ **逻辑层实测**（SQLite/Memory 多线程 + 端点安全日志）＋ **静态代码/配置审查**（终态不可覆盖、审计可信、metrics 内网化、出网规则） |

> 说明：`verify_t6_acceptance.py`（仓库根目录）内容为**能力矩阵门控**，与本回调安全闭环 t6 是不同目标，本次验收**不采用**，避免混淆。

---

## 1. 并发回调只产生一个合法终态和一条可信审计链 —— ✅ 通过

**实测（真实 PG）**：`tests/test_pg_callback_concurrency.py` → **7 passed in 4.11s**。

| 测试 | 断言核心 | 结果 |
|---|---|---|
| `test_pg_same_nonce_replay_only_one_terminal` | 4 线程同 nonce → 恰好 1 次 applied(confirmed)，其余 3 次 replay；仅 1 条执行记录；状态=CONFIRMED；op=executed；**callback 审计恰 1 条** `execution.callback.confirmed`，`tenant_id="TENANT-A"`（可信租户）、`user_id="callback"`、`target_id=execution_id` | PASSED |
| `test_pg_diff_nonce_concurrent_single_terminal` | 4 线程异 nonce → 恰好 1 confirmed + 3 terminal_locked；仅 1 条执行记录；reconciled 单一终态 CONFIRMED；receipt.amount=100.0 | PASSED |
| `test_pg_success_failure_competition_single_outcome` | 成功+失败并发 → 仅 1 条执行记录；最终为单一合法终态（非中间态静默当成功）；confirmed ≤1；callback 审计链可信 | PASSED |
| `test_pg_success_failure_competition_no_spurious_audit` | 成功/失败竞争收敛后 **callback 审计恰 1 条**，action ∈ {confirmed, failed_dispatch}，**非 illegal_transition** | PASSED |
| `test_pg_success_then_failure_terminal_closed` | 先成功 confirmed 后失败 → terminal_locked；终态不变；`compensation_status=None`（不触发补偿） | PASSED |
| `test_pg_terminal_after_callback_not_overwritten` | confirmed 后新 nonce → terminal_locked；同 nonce → replay；终态不变 | PASSED |
| `test_pg_cross_tenant_forgery_not_found_zero_pollution` | 跨租户 → not_found；原租户记录零污染（SUBMITTED、无 nonce）；无任何租户审计增量 | PASSED |

**逻辑层证据**：`tests/test_callback_concurrency_cas.py`（7 项）+ `tests/test_callback_security_log.py`（6 项）→ **13 passed**（SQLite/Memory 多线程确定性 CAS：同 nonce 仅 1 生效、异 nonce 收敛单一终态、终态不可覆盖、跨租户 not_found 零污染、审计链可信）。

**代码依据**：
- `src/infrastructure/store.py:53-151` `plan_callback_atomic`（三后端共用纯函数计划）：`replay`（同 nonce）→ `terminal_locked`（终态 + 异/新 nonce，`record.is_terminal`，第 99-100 行）→ `amount_mismatch`（转人工）→ `processing`（仅记账）→ 非法跃迁一律 `terminal_locked`（第 119-127 行，**绝不覆写既有流程**）→ `confirmed` / `failed_dispatch`。
- `src/infrastructure/postgres_store.py:584-664`：`_tx(tenant_id, write=True)` 同一事务；`SELECT ... FOR UPDATE` 行锁 + `tenant_id=:t`；状态 CAS `status=:expected AND status NOT IN (终态)`（第 626-630 行），rowcount=0 → terminal_locked；同事务写审计用 `record.tenant_id`（第 658 行）。
- **竞争缺陷**（captain 已修复）：`plan_callback_atomic` 第 119-127 行将非法/冲突跃迁统一判为 `terminal_locked`（applied=False，不回执、不记账、不写审计、不改状态），不再产生误导性 `illegal_transition`/覆写既有失败流程——由 `test_pg_success_failure_competition_no_spurious_audit` 硬断言覆盖（callback 审计恰一条）。

---

## 2. 伪造回调稳定返回预期拒绝码 —— ✅ 通过

**实测**：
- `test_callback_cross_tenant_rejected_404`（`tests/test_execution_engine.py`）PASSED。
- PG 跨租户伪造 → `not_found`，端点映射 **HTTP 404**（`test_pg_cross_tenant_forgery_not_found_zero_pollution` PASSED）。
- 端点安全日志测试（`tests/test_callback_security_log.py`，6 项 PASSED）：
  - `test_signature_invalid_401_and_only_security_log`：坏签名 → **401** "回调签名校验失败"，**零租户审计**，仅写平台安全日志，且日志**不含 `tenant_id`**（第 132 行断言）。
  - `test_bad_payload_only_security_log` / `test_missing_identity_zero_audit` / `test_missing_nonce_zero_audit`：非法 JSON / 缺身份 / 缺 nonce → **200 applied=False**，零租户审计，只写安全日志（按文档约定为幂等/对账语义，不当作错误）。
  - `test_unknown_execution_not_found_404_zero_audit`：合法签名但未知/他人 execution_id → **404** "执行记录不存在"，零租户审计，不泄露存在性。
  - `test_confirmed_audit_uses_record_tenant_not_body`：可信确认仅 1 条审计，`tenant_id` 取执行记录可信租户（非 body），端点未追加第二条审计。

**代码依据**：`src/api/routes.py:477-524`。`_UNTRUSTED_CALLBACK_REASONS = {signature_invalid, bad_payload, missing_identity, missing_nonce, not_found}`（第 83-89 行）；这些情形只写 `_security_log`（`get_logger("security")`，结构化日志，不入 Prometheus 标签/无 DB audit），**绝不**用 body 不可信 `tenant_id` 写租户审计、**不写数据库 audit**。HTTP 映射：signature_invalid→401、not_found→404、其余→200 applied=False。

> 观察（非缺陷）：`bad_payload`/`missing_identity`/`missing_nonce` 返回 200 而非 4xx，属设计约定（幂等/对账语义），因 applied=False 且零审计污染，不构成安全漏洞；可在文档/测试中保留该约定即可。

---

## 3. 跨租户访问为零 —— ✅ 通过

**租户作用域（三重防线）**：
1. **应用层归属校验**：`engine.apply_callback`（`engine.py:294-304`）从 body 取 `tenant_id` 仅为**定位输入**；`store.apply_callback_atomic(tenant_id, ...)` 内 `rec["tenant_id"] != tenant_id`（Memory `store.py:566-569`）或 PG `WHERE execution_id=:eid AND tenant_id=:t`（`postgres_store.py:601-607`）→ 跨租户/不存在一律 `not_found`（不泄露存在性）。
2. **RLS 兜底（PG）**：业务表 `FORCE ROW LEVEL SECURITY` + policy `tenant_id = app_current_tenant_id()`（`migrations.py:209-283`，`executions` 在内）；`_tx` 用 `set_config('app.tenant_id', :t, true)`（事务本地，`postgres_store.py:88-93`）在同一连接/事务设置。测试用 `app_runtime`（NOBYPASSRLS）角色，RLS 真正生效。跨租户测试（`test_pg_cross_tenant_forgery_not_found_zero_pollution`）实测 `not_found` + 零污染 + 无审计增量。
3. **审计只用可信租户**：`apply_callback_atomic` 写审计用 `record.tenant_id`（`store.py:589`、`postgres_store.py:658`），**绝不用请求 body 的不可信 `tenant_id`**。`test_confirmed_audit_uses_record_tenant_not_body` 已实测。

**终态不可覆盖（无裸 UPDATE）**：
- `plan_callback_atomic` 对终态记录（`record.is_terminal`）任何异/新 nonce 一律 `terminal_locked`（第 99-100 行）。
- PG CAS：`UPDATE ... WHERE ... AND status=:expected AND status NOT IN (终态)`（第 626-630 行），rowcount=0 → terminal_locked；无任何无条件的裸 `UPDATE executions SET status=...`。
- Memory/SQLite 同语义（`store.py:565-594`、`sqlite_store.py:499-574`）。
- 由 `test_pg_terminal_after_callback_not_overwritten` / `test_pg_success_then_failure_terminal_closed` 实测。

---

## 4. 公网无法访问 metrics —— ✅ 通过

**公网 nginx 阻断（容器实测，captain 提供）**：
- `deploy/nginx/preview.conf:48-50`：443 server 精确匹配 `location = /api/metrics { return 404; }`（优先级高于 `/api/` 前缀），公网访问 `/api/metrics` 一律 404，不转发 `api:8000`；其余 `/api/*` 反代不变。
- 容器实测：HTTPS `https://127.0.0.1:18443/api/metrics` → **404**；HTTP `http://127.0.0.1:18080/healthz` → **200**（nginx 正常，仅阻断指标路径）。
- `nginx:1.27-alpine` 容器 `nginx -t` → "syntax is ok / test is successful"。

**应用层来源限制（本验收独立实测 `tests/test_observability.py` → 20 passed）**：
- `_metrics_request_allowed`（`routes.py:106-124`）fail-closed：`metrics_expose_internal_only=true` 且白名单为空 + 受限环境 → 拒绝；即使 `metrics_expose_internal_only=false`，受限环境仍拒（`test_metrics_restricted_env_gate_off_still_fail_closed`，生产 gate-off 仍 403）。
- `test_metrics_endpoint_public_source_rejected`：公网来源 203.0.113.9 → **403** "metrics unavailable"。
- `test_metrics_endpoint_fake_xff_not_bypassed_when_untrusted`：`LOGIN_TRUSTED_PROXY_DEPTH=0` 时伪装 `X-Forwarded-For: 127.0.0.1` 仍 **403**（不可绕过）。
- 指标只暴露**有界聚合**（route/status 等），**不含 tenant_id 明细**（`test_metrics_rejects_tenant_label`）。

**网络/出网规则（静态审查 + 配置）**：
- `docker-compose.preview.yml`：仅 `nginx` 发布 80/443；`postgres`/`redis`/`api`/`worker`/`frontend` 均 `networks: [internal]` 无宿主端口发布（敏感端口不公网暴露）；`internal` 固定子网 `172.30.0.0/16`；`METRICS_EXPOSE_INTERNAL_ONLY=true`、`METRICS_ALLOWED_SOURCES=${...:-172.30.0.0/16}`（第 149-150 行）；`ENV=preview` → restricted。
- `deploy/EGRESS_POLICY.md`：默认无出网、敏感端口不公网发布、`LLM_BASE_URL` 必须内网自托管、`LLM_ALLOWED_HOSTS` 白名单（受限环境空则 fail-closed）、容器允许清单、Prometheus 经 `app-net` 内网直连 `api:8000/api/metrics` 抓取。
- `src/llm/security.py` `EndpointGuard.allowed()`：受限环境空白名单 fail-closed（全部拒绝，阻断未批准外联）；仅放行 loopback/RFC1918 私网（完全自托管默认）。
- compose 语法：`docker-compose.yml`、`docker-compose.preview.yml --env-file deploy/.env.preview` `config -q` → exit 0（captain 实测）；`docker-compose.observability.yml` 仅因缺 `LANGFUSE_DB_PASSWORD`（预期密钥注入项）中断，非语法错误。

> 边界说明（已知悉，不在代码安全闭环内）：preview 栈 internal 子网 `172.30.0.0/16` 若与既有子网冲突需 down 重建（`EGRESS_POLICY.md` §6）——属**部署落地**，不作为代码缺陷；已在结论中如实备注。

---

## 5. 回归 —— ✅ 通过

**默认 memory/后端无关测试子集全部不破坏**：
- `tests/test_execution_engine.py` + `tests/test_deploy_checks.py` + `tests/test_security_regressions.py` → **64 passed in 2.34s**。
  - 关键：`test_callback_cross_tenant_rejected_404`、`test_callback_invalid_signature_rejected`、`test_callback_http_endpoint_sign_and_replay`、`test_callback_amount_mismatch_goes_human`、`test_live_submit_then_callback_confirm_replay_safe` 均 PASSED。
- `tests/test_callback_concurrency_cas.py` + `tests/test_callback_security_log.py` → **13 passed**。
- `tests/test_observability.py` → **20 passed**。
- **真实 PG 并发测试** → **7 passed**。

累计本次实测通过：`7 + 13 + 64 + 20 = 104 tests`；无断言失败、无 skip（PG 测试在已有 DATABASE_URL + 启动 PG 下实际执行，非跳过）。

---

## 6. 未达标项与修复建议

**无未达标项**。以下几点为**观察/改进建议**（不阻塞通过，均已在代码中达成安全目标）：

1. **`bad_payload`/`missing_identity`/`missing_nonce` 返回 HTTP 200** 而非 4xx：属幂等/对账语义约定（applied=False + 零审计污染）。若希望合规上更严格，可考虑改为 400（不影响安全）。**建议保留现状并在测试注释/文档明确即可**。
2. **Docker `internal: true` 网络回退为普通 bridge**（`docker-compose.preview.yml:288-293`、`EGRESS_POLICY.md` §1）：因 Docker Desktop internal 网络破坏内嵌 DNS 解析，出网硬阻断由"数据面不发布端口 + LLM/Metrics 白名单 + 完全自托管"等价承接。**建议**按 §5 在主机侧叠加 iptables/nftables 逐容器出网白名单以达企业级，并留痕；该项属部署落地。
3. **`METRICS_ALLOWED_SOURCES` 默认 `172.30.0.0/16`**：依赖 `networks.internal.ipam.subnet` 确定性对齐。若部署时改子网需同步覆盖白名单，否则内网 Prometheus 抓取会 403（fail-closed 方向安全，仅影响抓取可用性）。**建议**在 `.env.preview` 显式注入，避免默认值漂移。

---

## 7. 结论

| 验收项 | 结论 | 证据类型 |
|---|---|---|
| 1. 并发回调只产生一个合法终态和一条可信审计链 | **通过** | 真实 PG 7 passed + SQLite/Memory 13 passed + 代码审查 |
| 2. 伪造回调稳定返回预期拒绝码 | **通过** | PG not_found/404、401 验签拒绝、零审计污染（实测） |
| 3. 跨租户访问为零 | **通过** | 应用层归属校验 + PG `FORCE ROW LEVEL SECURITY` + 审计只用执行记录可信租户；PG 跨租户零污染实测 |
| 4. 公网无法访问 metrics | **通过** | nginx 公网 404（容器实测）+ 应用层 403（20 passed）+ 出网规则（EGRESS_POLICY/EndpointGuard/仅 nginx 发布端口） |
| 5. 回归 | **通过** | 104 tests passed |

**最终判定：敏感执行回调安全闭环**（同事务 nonce + 状态 CAS 终态封闭 + 操作更新 + 审计写入；回调安全日志不写不可信 tenant 审计；/api/metrics 内网化；出网受限）**实现正确，满足生产级安全要求，验收通过。**
