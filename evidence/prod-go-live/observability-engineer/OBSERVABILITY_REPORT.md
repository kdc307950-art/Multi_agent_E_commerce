# 可观测报告 — 告警演练 / 关键指标验证 / shadow 观测 / 7 天监控方案（t4）

> 负责人：`observability-engineer`（prod-go-live）
> 运行环境：Docker Desktop（Windows + WSL2），项目根 `D:\software\PythonProject1\PythonProject\Multi_agent_E_commerce`
> 结论摘要：**RpoExceeded 已在真实运行中的 Prometheus 完成「注入→firing→定位→关闭」端到端闭环**；关键指标有界标签/`_FORBIDDEN_LABELS`/无重复 `# TYPE` 在**当前代码**验证通过；生产栈**未部署**（仅 preview + 观测栈运行），因此「生产抓取对象 + 生产异常率等真实口径」为**部署期执行项**；同时发现 **4 处配置/口径不一致**（详见 §6），其中 1 处会直接导致生产指标抓取 403，必须先修复。

---

## 0. 运行时盘点（如实）

| 栈 | 状态 | 说明 |
|---|---|---|
| `after-sales-preview` | 运行中（7 容器健康） | api/worker/frontend/nginx/postgres/redis/backup |
| `after-sales-observability` | 运行中（10 个容器，核心 7 组件健康） | prometheus/grafana/loki/promtail/langfuse+postgres+redis/clickhouse/minio |
| `after-sales-prod`（生产栈） | **未部署** | `docker compose ls` 仅见 preview + observability；`PRODUCTION_DEPLOYMENT_record-20260904-030903.md` L10 明示「production 未部署（本阶段为 preview 上线前置）」。`after-sales-prod-api:latest` 镜像已构建但**无运行容器** |

观测栈 Prometheus 当前抓取对象：`api:8000/api/metrics`（即 **preview** api，经 `app-net`=after-sales-preview_internal）。生产栈未上线前，无独立生产抓取源。

---

## 1. 告警演练（运行时端到端）✅ 真实闭环

### 1.1 触发前基线（关键事实）
- 生产/独立栈未部署；观测栈 Prometheus 健康，3 个抓取目标全部 `up`（`api`/`loki`/`prometheus`），`lastError` 为空。
- **重要**：运行中的 Prometheus 挂载的是 **`deploy/observability/alert-rules/alert-rules.yml`（子目录，6 个 group，旧草稿注释）**，而非根目录 `deploy/observability/alert-rules.yml`（当前 compose 指定、8 个 group、最终版）。两者 `RpoExceeded` 表达式一致（`drill_rpo_seconds{component="pg_backup"} > 900`），故本演练对运行态有效；但也暴露了「运行规则 ≠ 权威规则」的配置漂移（见 §6.1）。
- 关键规则 `RpoExceeded` 已加载、`health=ok`：`query: drill_rpo_seconds{component="pg_backup"} > 900`，`for: 1m`。

### 1.2 注入超阈值样本（一次性受控合成样本，已记录）
运行一个临时、可控的指标源（容器 `after-sales-drill-probe`，挂在观测内网 `obs`），只暴露一个有界维度序列：
```
# TYPE drill_rpo_seconds gauge
drill_rpo_seconds{component="pg_backup"} 1500
```
在 `deploy/observability/prometheus.yml` 临时加一个 `drill-probe` 抓取 job，`docker kill -s HUP` 让 Prometheus 重新加载配置。**演练后已完全还原（§1.6）。**

### 1.3 触发：pending → firing（真实运行态）
```
GET /api/v1/targets  → 4 目标，含 drill-probe health=up
GET /api/v1/query?query=drill_rpo_seconds
  → {drill_rpo_seconds{component="pg_backup", instance="after-sales-drill-probe:9100", job="drill-probe"} = 1500}
GET /api/v1/alerts
  → 首次 {alertname=RpoExceeded, state="pending", activeAt=...T06:17:54Z, value=1.5e+03}
  → 60s（for:1m）后 {alertname=RpoExceeded, state="firing", value=1.5e+03}
```
**真实 firing 取得**（非求值器伪造）：阈值 `>900` 被 `1500` 命中，`for: 1m` 期满后由 `pending` 转 `firing`。

### 1.4 定位（哪个序列越限 / 指向何处）
```
GET /api/v1/query?query=drill_rpo_seconds{component="pg_backup"} > 900
  → 唯一命中：{component="pg_backup", instance="after-sales-drill-probe:9100", job="drill-probe"}=1500
```
- 越限序列 = `job="drill-probe"`（本次受控注入源），`component="pg_backup"`，值 `1500`。
- 运行态规则 annotations 指向 OPS_RUNBOOK §3（超基线→评估回滚）。⚠️ 但运行态注释仍写「当前无此指标…表达式为空」（旧草稿），与实际已 firing 矛盾（见 §6.1）。

### 1.5 关闭（恢复后清告警）
```
重起 drill-probe 为 DRILL_RPO=100（<=900）
GET /api/v1/query?query=drill_rpo_seconds → {drill_rpo_seconds{...}=100}
GET /api/v1/alerts → {"data":{"alerts":[]}}   ← RpoExceeded 完全清除（inactive）
```
恢复超阈值样本 → 规则条件不再为真 → firing 立即转 inactive，报警清单清空。**恢复闭环达成。**

### 1.6 演练还原（确认无残留）
- `prometheus.yml` 已从备份还原（仅 `api`/`prometheus`/`loki` 3 个 job，无 `drill-probe`）—— 用 read 工具复核原文（UTF-8 中文注释完好）。
- `docker kill -s HUP` 重新加载后 `/api/v1/targets` 恢复 3 目标、全部 `up`。
- 删除 `after-sales-drill-probe` 容器。
- 备份文件保留于 `evidence/prod-go-live/observability-engineer/prometheus.yml.orig.bak`。

> **结论**：`RpoExceeded` 做到「真实注入 → 运行态 firing → PromQL 定位 → 恢复 clear」的端到端闭环，证据为真实 Prometheus 的 `/api/v1/alerts` / `/api/v1/query`。**基于 gauge 型规则（RPO/RTO）**。基于计数器的规则（`ApiHighErrorRate`/`ApprovalFailureRateHigh`/`HumanInterventionRate` 等）依赖 `status`/`human_intervention_total` 等有界指标，**运行中的 preview api 容器为旧构建，不产出这些标签**（见 §2.2），因此本轮无法对它们做真实运行态触发，记为**部署期（用当前构建 + 真实流量）复验项**。

---

## 2. 关键指标验证

### 2.1 有界标签 / `_FORBIDDEN_LABELS` / 无重复 `# TYPE`（对当前代码）
用 `src/observability/metrics.py` 做最小验证程序（输出见 `_probe_py/probe_metrics.py`）：
- `_FORBIDDEN_LABELS` = `{approval_id, client_request_id, execution_id, operation_id, order_id, request_id, tenant_id, thread_id, user_id}`（9 个高基数/敏感维度）。
- 对上述每一个作为标签调用 `counter(...)` → 均被 `ValueError` **拒绝**（`指标标签不得包含高基数/敏感维度…`）。✅
- `render()` 输出 `# TYPE api_requests_total counter` 仅 **1 次**（`seen_families` 去重生效），无重复 `# TYPE`；输出标签仅 `method/route/status` 有界维度。✅
- 输出扫描未出现 `tenant_id/user_id/order_id/operation_id/thread_id/approval_id/client_request_id` 等任一高基数字段。✅

### 2.2 真实口径核算与「是否可计量」——诚实标注
下列以 `deploy/records/METRICS_SUMMARY-20260904-030903.md` 为历史口径基准，并结合当前代码逐一核对「当前表达式 + 是否真实打点」：

| 指标 | 当前代码是否打点 | 运行中 preview 是否可计量 | 结论 |
|---|---|---|---|
| `api_requests_total{route,method,status}` | 是（`src/api/routes.py` HTTP 中间件按 `status` 记） | **否**——运行容器旧构建，`/api/metrics` 无 `status` 标签，且出现**重复 `# TYPE api_requests_total counter`** | 口径需「当前构建」重建后再计 |
| `approval_decisions_total{route,status}` | 是（status ∈ approved/rejected/…/forbidden） | 否（运行容器无 `status`） | 同上 |
| `approval_decision_latency_seconds`（histogram） | 是 | 否 | 部署期复验 |
| `reconcile_mismatch_total{kind}` | 是（`engine._reconcile_mismatch`） | 否 | 部署期复验 |
| `human_intervention_total{kind}` | 是（order_deny/approval_timeout/execution_handoff/reconcile_mismatch/escalate） | 否 | 部署期复验 |
| `security_denials_total{kind}` | 是（`audit_security_denial` + 不可信回调） | 否 | 部署期复验 |
| `drill_rpo_seconds`/`drill_rto_seconds{component}` | 是（`_load_drill_gauges`，gauge） | **否**——运行容器无 `/app/evidence/dr_metrics` 挂载、无 `DRILL_GAUGE_STATE` | 本次**用受控注入源**替代；正式写路径需当前构建部署后复验 |
| `up{job=...}`（组件存活） | Prometheus 自动 | **是**（3 目标全 up） | ✅ 可计量 |
| `login_rate_limited_total{reason}` | 是（routes.py::login） | 是（表达式非空） | 未真实触发，部署期用真实/受控登录压力复验 |

> **口径诚实说明**：本轮**无法**给出「生产/预览真实运行错误率、人工介入率、审批失败率」等真实观察值——因为运行中的 preview api 为旧构建，缺 `status`/`human_intervention_total` 等标签；这些不属于「虚报」范围，如实标注为**待当前构建重建 + 真实流量（或受控样本）复验**。求值器(`eval_alert_rules.py`)已证明：若上述指标按当前定义打点，则各规则表达式可命中/可关闭（见 §3）。对账 mismatch、审批失败率、安全拒绝等指标**当前代码已接入**，但运行态需部署后复验才能给出真实计数。

---

## 3. 告警规则「可触发/可关闭」求值器等价证据

运行 `deploy/observability/evidence/eval_alert_rules.py`（对根目录权威 `deploy/observability/alert-rules.yml`，8 组 15 条）：
```
VALIDATION issues=0
ApiJobDown/PrometheusJobDown/LokiJobDown/LoginBruteForceAttempts/ApiHighErrorRate/ApprovalFailureRateHigh/
ApprovalLatencyHigh/ReconciliationMismatch/NoReconciliation/HighHumanInterventionRate/RpoExceeded/RtoExceeded/
TenantDenialSpike/CrossTenantAccessDetected : trigger=YES close=YES
ComponentRestartDetected                   : trigger=NO  close=YES（依赖 component_restart_total，未接入 → 恒不触发，不误报）
```
- **求值器等价证据**：各规则在「超阈值合成样本」上为真（可触发），在「事件消逝样本」上为假（可关闭）。
- 权威判定以真实 Prometheus 为准（见 §1 对 `RpoExceeded` 的真实运行态闭环；其余规则需部署期复验）。`ComponentRestartDetected` 依赖指标未接入，如实标注。

---

## 4. shadow / 只读观测启动验证

| 项 | 状态 | 证据 |
|---|---|---|
| `EXECUTION_MODE` | **shadow** | preview api 运行态 env（`docker inspect`）+ `PRODUCTION_DEPLOYMENT_record` L73 |
| `EXECUTION_PROVIDER` | **mock**（无 live 网关） | 同上；`LAUNCH_GATE_STRICT=true` |
| 禁止 live+mock | 是 | `src/core/launch_gate.py`：live + mock 记为违规（fail-closed）；shadow 为首次上线合法模式 |
| 生产栈 | **未部署** | 生产 shadow/只读等待 deploy-engineer 上线，本阶段无法验证生产运行态 |
| Langfuse trace | **NO-OP（未上报）** | preview api `LANGFUSE_PUBLIC_KEY=` 空、`LANGFUSE_SECRET_KEY=` 空；`src/observability/tracing.py` `get_tracer()` 在 public_key 为空时返回 `_NULL`（no-op）。**观测栈 Langfuse 容器健康但未收到任何 trace** |
| JSON 日志 | **已开启** | `LOG_JSON_FORMAT=true`；`docker logs` 输出结构化 JSON（`ts/level/logger/service/message`） |
| 日志 tenant_id 上下文 | 已接入（待真流量触发） | `src/observability/logging.py` `JsonFormatter` 输出 `tenant_id/session_id/operation_id` 等 `_CONTEXT_KEYS`；近期日志为 unauthenticated healthz/metrics 探针（无 tenant_id），业务请求日志会带 tenant_id。属「代码就绪、未用真实请求验证」 |

> **结论**：当前运行栈处于 **shadow + mock**（无 live provider），符合「只读/shadow 先于 live 切换」要求；但 **Langfuse trace 未实际上报**（密钥为空）——这是切 live 前**必须补齐**的观测链路就绪项（注入密钥 + 用真实请求验证一次性 trace 落地，metadata 含 `tenant_id/session_id/environment`，PII 脱敏）。**本次未做任何 live 切换（DO NOT 切 live）。**

---

## 5. 7 天观察监控方案（观察期指标集 + 资金异常立即关 live 转人工）

### 5.1 观察期监控指标集（有界标签，一律不把 `tenant_id/user_id/...` 当 Prometheus 标签）
| 关注维度 | 指标/来源 | 标签（有界） | 阈值/口径 |
|---|---|---|---|
| 跨租户拒绝数 | `security_denials_total{kind=~"cross_tenant|cross_user_|access_denied|forbidden|role_mismatch"}` | `kind` | `increase[5m] > 0` 立即告警（合规红线） |
| 重复执行数 | `execution_outcome_total{mode,status}` + 审计表（`GET /api/audit?target_type=operation`） | `mode,status` | **目标 = 0**；任何重复计数 → 告警 |
| 回调失败数 | `execution_callback_total{reason}` | `reason`（有界） | `increase[5m]>0` → 告警 |
| 人工介入率 | `human_intervention_total{kind}` / `api_requests_total` | `kind` | ratio >20% → 告警 |
| 对账差异 | `reconcile_mismatch_total{kind}` + `reconcile_last_run_timestamp_seconds` | `kind` | mismatch>0（窗口内）→ 告警；>24h 未跑 → 告警 |
| 系统错误率 | `api_requests_total{status=~"5.."}` | `route,method,status` | >5% → 告警 |
| 备份结果 | `drill_rpo_seconds`/`drill_rto_seconds{component="pg_backup"}` + `up{job="backup"}` | `component/job` | RPO>900 / RTO>3600 → 告警；备份 `up=0` → 告警 |
| 组件存活 | `up{job=api|prometheus|loki}` | `job` | `==0` → 告警 |
| 审批链路 | `approval_decisions_total{status=~"rejected|error|timeout"}`、`approval_decision_latency_seconds` | `route,status/le` | 失败>5%、p95>30s → 告警 |

**租户维度受控聚合取数**：`tenant_id` 只作为**审计/聚合查询**（`GET /api/audit?target_type=...`、Loki 结构化日志、Langfuse metadata）的检索维度，**绝不进入 Prometheus 标签**（避免高基数标签爆炸）。按租户的计数/明细均通过自托管平台受控查询取得，并默认脱敏。

### 5.2 看板（Grafana）
基于上述指标建 4 个面板：
1. **资金安全**：跨租户拒绝数、重复执行数（目标0）、驳回/超时审批数、对账 mismatch、备份 RPO/RTO、执行结果。
2. **系统健康**：`up`、5xx 错误率、p95 延迟、token/调用量。
3. **人工介入 / 升级**：人工介入率、转人工明细（受控审计）。
4. **观测链路**：Prometheus/Loki/Langfuse 自身抓取健康（`up`）。

### 5.3 「任一资金异常立即关 live 转人工」的告警→执行动作
- **触发**：`CrossTenantAccessDetected`（最高优先级）、`RpoExceeded`/`RtoExceeded`、`ReconciliationMismatch`、`approval failure`、重复执行计数>0、对账不可核实。
- **动作（执行/值班）**：
  1. 收到以上任一 **资金/合规类** 告警 → **立即将 `EXECUTION_MODE` 由 `live` 切回 `shadow`**（或 `暂停`/`人工接管`），阻断后续自动写路径；
  2. 保留 `operation_id`/`approval_id` 与**完整状态**，`handle_error` 带完整上下文**转人工**；
  3. 走 `platform_admin` 受控流程 + 二次确认 + **不可抵赖审计**（跨租户场景）；
  4. 审计留痕：操作者、时间、依据、结果（`GET /api/audit`）。
- **回退矩阵**：超时/模型不在白名单/资格金额异常/审批超时/检查点恢复失败/数据面租户作用域缺失 → **不写**、保留 `operation_id`、通知人工、记录审计。**绝不降级用低档模型写、绝不直接执行。**

---

## 6. 发现的问题（如实，须在切 live 前处理）

### 6.1（高优先）运行态 Prometheus 加载的是**旧的 alert-rules 子目录文件**，与权威根文件不一致
- 运行容器挂载：`deploy/observability/alert-rules/alert-rules.yml`（6 组、旧草稿注释「当前无此指标/表达式为空」）。
- 当前 compose（`docker-compose.observability.yml`）与权威文件：`deploy/observability/alert-rules.yml`（8 组、最终版）。
- **后果**：运行态告警集是旧子集（缺 `login-security`/`component-restart`/`tenant-security` 组），且注释与实际 firing 矛盾（§1.4 所见 RpoExceeded 已 firing 但注释仍写「表达式为空」）。**须 `docker compose` 重建/重启 prometheus 到当前 compose，使运行态=权威规则，再复验。**

### 6.2（高优先）`METRICS_ALLOWED_SOURCES` 默认网段与实际内网不匹配 → 生产抓取会 403
- 当前 compose 默认：`METRICS_ALLOWED_SOURCES=${METRICS_ALLOWED_SOURCES:-172.30.0.0/16}`，`METRICS_EXPOSE_INTERNAL_ONLY=true`。
- 实际 preview 内网（`after-sales-preview_internal`）子网 = **172.22.0.0/16**（Prometheus 抓取源即 172.22.x）。
- **后果**：若用当前代码 + 上述默认 env 部署，`_metrics_request_allowed` 会把 172.22.x 判定为不在白名单 → **403 → 指标抓取失败（up=0）**，监控全断。**必须**：把 `METRICS_ALLOWED_SOURCES` 设为实际抓取子网（或 pin 内网 subnet 并与 allowlist 对齐）。本轮运行态因 preview api 为旧构建（无 METRICS_* env）而未暴露，但生产用当前代码时必须先修。
  （注：根目录 `deploy/observability/alert-rules/README.md`/注释也写到 172.30.0.0/16 — 需一并核对。）

### 6.3（中）运行中的 preview api 是**旧构建**
- `/api/metrics` 缺 `status` 标签、重复 `# TYPE api_requests_total counter`、无 drill gauge、无 METRICS_* env、无 `/app/evidence/dr_metrics` 挂载。
- 原因是容器创建早于当前代码/当前 compose。**当前代码（`src/observability/metrics.py`/`routes.py`）已修正**（status 标签 + seen_families 去重 + gauge 合并 + 门控）。**用当前构建重建 preview/prod api 后，运行态口径即与权威一致**。作为对照，我在 `_probe_py/probe_metrics.py` 对当前代码验证了有界标签与去重，均正确。

### 6.4（中）无 alertmanager 且 Langfuse 未上报
- 观测栈**未部署 alertmanager**：规则在 Prometheus 内求值/进入 firing（本演练已证），但无 alertmanager 把告警投递到接收端（邮件/Webhook 等）。**切 live 前置项**：加 alertmanager + receiver（或改用 Grafana alerting）。
- Langfuse 密钥为空 → trace **no-op**（见 §4）。**切 live 前置项**：注入 `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`，用真实请求验证 trace 落地（metadata 含 `tenant_id`/`session_id`/`environment`，PII 脱敏）。

### 6.5（低）生产 api 抓取目标未配置
- `deploy/observability/prometheus.yml` 仅抓 `api:8000`（preview）。生产栈上线后需新增生产 api 目标（独立内网子网），并同步校验 §6.2 的 allowlist 子网。

---

## 7. 结论

- **告警演练**：`RpoExceeded` **真实运行态端到端闭环**（注入→firing→PromQL 定位→恢复 clear），证据为真实 Prometheus `/api/v1/*` 返回。其余基于有界计数器的规则在运行态无法触发（旧构建缺标签），已用**求值器等价证据**覆盖，并明确为**部署期复验项**。**未伪造任何 firing 状态。**
- **关键指标**：当前代码有界标签/`_FORBIDDEN_LABELS`/无重复 `# TYPE` 均验证通过；真实运行口径（错误率/人工介入率/对账/审批失败/安全拒绝）在运行中**不可计量**（旧构建），代码已接入但需当前构建 + 真实流量复验；按 METRICS_SUMMARY 如实分级。
- **shadow/只读**：当前为 `shadow`+`mock`，无 live provider，符合先行要求；Langfuse 未上报为唯一硬缺口。
- **7 天监控方案**：给出指标集、看板划分、「任一资金异常立即关 live 转人工」动作与回退矩阵、租户维度受控聚合取数（tenant_id 永不作为 Prometheus 标签）。
- **前置修复项（切 live 前）**：§6.1 规则文件漂移、§6.2 allowlist 子网不匹配（会 403）、§6.4 alertmanager+Langfuse、§6.5 生产抓取目标。

---

### 附录：运行时证据留存
- 告警触发/关闭：见 `evidence/prod-go-live/observability-engineer/drill_rpo_alerts.json`（firing 与 clear 两次 `/api/v1/alerts` 原文）。
- PROMQL 定位：`/api/v1/query?query=drill_rpo_seconds{component="pg_backup"} > 900` → 唯一命中 `job="drill-probe"` 值 1500。
- 有界标签验证：`_probe_py/probe_metrics.py`（9 个 _FORBIDDEN_LABELS 均被拒；`# TYPE api_requests_total counter` 仅 1 次）。
- 原 prometheus.yml 备份：`evidence/prod-go-live/observability-engineer/prometheus.yml.orig.bak`。
