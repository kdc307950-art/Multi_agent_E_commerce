# 指标 / 告警增强 · 收口核验（t3）

> 责任角色：`engineer-obs`（可观测与发布工程师） · 团队 `rc3-ga-closeout`
> 对应待办：`DELIVERABLES_SUMMARY.md` 第 4.2 节「工程/观测补强」——指标增强 + 告警修复（本项目为**发布前必办、非外部依赖**）。
> 日期：2026-09-04
> 结论：**核心指标打点、`METRICS_ALLOWED_SOURCES` 网段、alert-rules 单一权威源均已就位并本地实测通过**；容器镜像重建（需 Docker 引擎）为外部硬件依赖，已如实标注为**未执行**。

---

## 0. 范围声明（红线：只动观测，不动业务治理）

本收口**只**涉及 Prometheus 指标打点与告警/抓取口径，采用**有界标签**维度（route / method / status / kind / node / job / component），
**未改动**以下任何一处（对照 `AGENTS.md` 宪法与《生产环境架构设计》）：

- **敏感写路径**：`process_refund` / `process_return` / `update_return_address` 三条资金/隐私写路径的判定、金额、资格逻辑**未动**；
- **人工审批（HITL）**：唯一 `human_approval` interrupt 节点的触发、审批归属、二次确认、拒绝即终止**未动**，未添加任何 `direct → execute_*` 绕过边；
- **幂等**：`oprefund:{tenant_id}:{order_id}:{refund_request_id}` 幂等键、`UNIQUE`+`ON CONFLICT DO NOTHING`、`executed(operation_id)` 检查**未动**；
- **租户隔离 / RLS**：数据面租户作用域守卫、跨租户默认拒绝、`tenant_id` 生命周期**未动**；
- **RBAC / 能力矩阵**：租户角色（`customer`/`agent`/`admin`/`approver`）、`HIGH_CONFIDENCE_MODELS` 白名单门控、降级边界**未动**。

所有新增/校验的指标均**拒绝**把 `tenant_id`/`user_id`/`thread_id`/`order_id`/`operation_id`/`approval_id` 等用作 Prometheus 标签
（`src/observability/metrics.py::_FORBIDDEN_LABELS` 强制抛 `ValueError`；租户明细一律走受控审计查询 `GET /api/audit`，不入监控标签）。

---

## 1. 有界标签指标打点（全部就位）

| 指标 | 类型 | 标签（有界） | 打点位置 |
|------|------|------------|---------|
| `api_requests_total` | counter | `route,method,status` | `src/main.py` HTTP 中间件（统一记录；`src/api/routes.py` 提供 `_api_route_label` 有界 route） |
| `approval_decisions_total` | counter | `route,status` | `src/api/routes.py::decide_approval`（`status ∈ approved|rejected|timeout|replay|error|forbidden`） |
| `approval_decision_latency_seconds` | histogram | `route` | `src/api/routes.py::decide_approval`（仅对真正执行恢复的决策度量） |
| `reconcile_mismatch_total` | counter | `kind` | `src/execution/engine.py::_reconcile_mismatch`（`kind`=归一化 mismatch reason，有界） |
| `reconcile_last_run_timestamp_seconds` | gauge | `-` | `src/execution/engine.py` 对账任务 |
| `human_intervention_total` | counter | `kind` | `src/api/routes.py`（`order_deny`/`approval_timeout`）+ `src/execution/engine.py`（`execution_handoff`/`reconcile_mismatch`） |
| `security_denials_total` | counter | `kind` | `src/auth/security.py::audit_security_denial`（单点）+ `src/api/routes.py` 不可信回调（`kind`=审计拒绝 reason，固定集合） |
| `execution_submit_total` | counter | `mode` | `src/execution/engine.py`（`mode=shadow|live`） |
| `execution_outcome_total` | counter | `mode,status` | `src/execution/engine.py` |
| `execution_callback_total` | counter | `reason` | `src/api/routes.py::provider_callback`（`reason` 来自固定集合） |
| `drill_rpo_seconds` / `drill_rto_seconds` | gauge | `component` | `src/api/routes.py::_load_drill_gauges`（合并 DR 脚本实测 RPO/RTO，闭合 `RpoExceeded`/`RtoExceeded` 写路径） |
| `login_attempts_total` / `login_rate_limited_total` | counter | `result`/`reason` | `src/api/routes.py::login` |
| `audit_queries_total` | counter | `route` | `src/api/routes.py::get_audit` |

**`_FORBIDDEN_LABELS`（`src/observability/metrics.py`）** = `tenant_id, user_id, thread_id, order_id, operation_id, client_request_id, approval_id, execution_id, request_id` —— 全部拒绝用作标签，注册时抛 `ValueError`。

**渲染去重**：`MetricsRegistry.render()` 用 `seen_families` 保证同一指标名只输出一次 `# TYPE`，避免 Prometheus 文本抓取报错（修复了同指标名多系列重复 `# TYPE`）。

---

## 2. `METRICS_ALLOWED_SOURCES` 网段对齐（已修正）

| 环境 | internal 子网（`networks.internal.ipam.subnet`） | `METRICS_ALLOWED_SOURCES` 默认 | 状态 |
|------|------|------|------|
| preview（`docker-compose.preview.yml`） | `172.30.0.0/16`（L313-317） | `${METRICS_ALLOWED_SOURCES:-172.30.0.0/16}`（L151） | ✅ 对齐 |
| prod（`docker-compose.prod.yml`） | `172.31.0.0/16`（L270-271） | `${METRICS_ALLOWED_SOURCES:-172.31.0.0/16}`（L127） | ✅ 对齐 |

- 两端**网段与白名单确定性一致**，Prometheus 抓取源 IP 命中白名单（`METRICS_EXPOSE_INTERNAL_ONLY=true`），`_metrics_request_allowed` 不 403。
- 相关历史观察（`OBSERVABILITY_REPORT.md` §6.2 曾记录“实际 preview 内网 = 172.22.0.0/16”）已被**当前 compose 的 ipam 固定子网覆盖**：部署/校验时另以 `EGRESS_POLICY.md` §6 保证——若 `internal` 网已以其它子网创建，需先 `down` 重建，否则白名单/抓取会 403（fail-closed 方向安全，仅影响抓取可用性，不违反红线）。
- `rules` 依据：`_metrics_request_allowed` 仅在来源命中 `metrics_allowed_sources`（或受限环境未配白名单时 fail-closed 拒绝）时放行，**绝不默认放公网**。

---

## 3. alert-rules 权威文件与运行态对齐（已修正）

- **单一权威文件**：`deploy/observability/alert-rules.yml`（**9 组 / 15 条**）。
- 挂载与引用均指向同一文件：`docker-compose.observability.yml` L138 把根文件挂载为 `/etc/prometheus/alert-rules.yml`，`deploy/observability/prometheus.yml` L13-14 `rule_files` 引用它 → **Prometheus 实际加载的规则 == 仓库内 catalog**。
- **历史漂移已消除**：早期 t4 在子目录 `deploy/observability/alert-rules/alert-rules.yml` 放的**模板骨架**已被权威文件上位并**删除该重复副本**（当前文件系统无该子目录；`alert-rules-README.md` L8-9 已记录此漂移与“不要再新增第二份”）。
- 规则组（`groups`）：`after-sales-component-health`、`after-sales-login-security`、`after-sales-component-restart`、`after-sales-api-error-rate`、`after-sales-approval`、`after-sales-reconcile`、`after-sales-human-intervention`、`after-sales-rpo-rto`、`after-sales-tenant-security`。
- 关键规则均引用**已接入**的有界指标（`api_requests_total{status=~"5.."}`、`approval_decision_latency_seconds_bucket`、`reconcile_mismatch_total`、`human_intervention_total`、`security_denials_total{kind=~cross_tenant|...}`、`drill_rpo_seconds`/`drill_rto_seconds`），表达式不再为空，不误报。

## 4. Alertmanager 配置（已补齐）

- `docker-compose.observability.yml`：`alertmanager` 服务（`prom/alertmanager:v0.27.0`，挂载 `deploy/observability/alertmanager.yml`）+ 自托管 webhook sink `alert-webhook-sink`（`evidence/mock_webhook_receiver.py`，捕获到 JSONL 并回 200）。
- `deploy/observability/prometheus.yml` `alerting.alertmanagers` → `alertmanager:9093`，形成「规则触发 → Alertmanager 路由/分组 → webhook 通知」闭环。

---

## 5. 本地验证（已实测）

```
$ .venv\Scripts\python.exe -m pytest tests/test_observability.py tests/test_hardening_acceptance.py -q
.....................................................s.
54 passed, 1 skipped in 1.54s
```

核验要点（测试断言，`tests/test_observability.py`）：
- `test_one_second_bucket_supported_metric_names_render`：`approval_decision_latency_seconds`（histogram）、`reconcile_mismatch_total`、`human_intervention_total`、`security_denials_total`、`execution_*`、`drill_*` 渲染正确，且文本**不含** `tenant_id`/`user_id`/`order_id`。
- `test_new_bounded_metrics_still_forbid_cardinality_labels`：以上新增指标带 `tenant_id`/`user_id`/`operation_id` 标签时一律抛 `ValueError`。
- `test_api_requests_metric_records_status_via_middleware`：中间件真实请求出栈落 `api_requests_total{method="GET",route="healthz",status="200"}`，无租户明细。
- `test_metrics_gauge_set_and_render`：`drill_rpo_seconds`/`drill_rto_seconds`/`reconcile_last_run_timestamp_seconds` gauge 渲染正确，`tenant_id` 拒绝。
- `test_alert_rules_reference_wired_metrics`：`alert-rules.yml` 引用的核心有界指标均已接入，且规则不出现 `tenant_id=`/`user_id=` 等敏感/高基数标签。
- `test_metrics_*`：`/api/metrics` 来源限制（内网命中放行 200、公网 403、受限无白名单 fail-closed、伪造 XFF 不绕过）均通过。

**`render()` 无 `tenant_id` 标签**：由上述指标渲染/路由级测试直接断言，`_FORBIDDEN_LABELS` 兜底强制。

---

## 6. 未执行 / 外部依赖（如实标注）

- **容器镜像重建**（`git archive` clean-context 字节级重建 + 刷新 `IMAGE_DIGESTS.json`）：需 Docker 引擎可连接，**当前不可连接**（与 `DELIVERABLES_SUMMARY` §4.2 一致）→ **未执行**。因此运行中旧构建 api/worker 的“新标签生效”属**部署期复验项**，非代码侧可验证。
- **对账 mismatch 计数后台实测** / **生产栈运行时 RLS/告警端到端复验** / **Langfuse trace 落地**：属运行态依赖，标记为放量前/放量后观察项，本收口不宣称已达标。

---

*本文件为 t3 收口核验记录：仅涉及观测链路，未触碰资金/隐私写路径、审批、幂等、租户隔离与 RBAC 逻辑。*
