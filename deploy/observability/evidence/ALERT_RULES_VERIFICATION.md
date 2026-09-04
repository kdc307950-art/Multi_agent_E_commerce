# 告警规则「可触发 / 可定位 / 可关闭」验证记录（t7）

> **验证方法**：环境无 `promtool` / Prometheus 容器，故用自建 PromQL 子集求值器 `deploy/observability/evidence/eval_alert_rules.py` 对**合成样本**求值（覆盖本文件用到的 `rate`/`increase`/`sum by`/`clamp_min`/`histogram_quantile`/`time()`/标签正则匹配等算子），并做**引用规则文件的语法/结构校验**（表达式可解析、`for` 为合法时长、缺 annotations）。每次运行会重新计算 `deploy/observability/alert-rules.yml` 的每条规则，落到本文件。
> **权威判定**：以真实 Prometheus（docker-compose.observability.yml 把 `deploy/observability/alert-rules.yml` 挂载为 `/etc/prometheus/alert-rules.yml`，抓取 `/api/metrics`）为准；本次**未起容器**，故用求值器等价证据。

## 0. 语法/结构校验（promtool 等价）

✅ 表达式均可被求值器解析、`for` 均为合法时长、每条规则均含 `annotations.summary`/`annotations.description`。

## 0. 汇总表

| 规则 | 可触发(命中阈值样本) | 可关闭(事件消逝后恢复) | 备注/缺口 |
|---|:---:|:---:|---|
| ApiJobDown | 是 | 是 |  |
| PrometheusJobDown | 是 | 是 |  |
| LokiJobDown | 是 | 是 |  |
| LoginBruteForceAttempts | 是 | 是 |  |
| ComponentRestartDetected | 否 | 是 | 指标未接入（编排层需上报 component_restart_total） |
| ApiHighErrorRate | 是 | 是 |  |
| ApprovalFailureRateHigh | 是 | 是 |  |
| ApprovalLatencyHigh | 是 | 是 |  |
| ReconciliationMismatch | 是 | 是 |  |
| NoReconciliation | 是 | 是 | gauge 由 engine.reconcile()→celery 对账任务写入 |
| HighHumanInterventionRate | 是 | 是 |  |
| RpoExceeded | 是 | 是 | ⚠️ 表达式可命中，但写路径未回填（restore_drill.sh 只写 records，需 t2 回填 gauge） |
| RtoExceeded | 是 | 是 | ⚠️ 表达式可命中，但写路径未回填（restore_drill.sh 只写 records，需 t2 回填 gauge） |
| TenantDenialSpike | 是 | 是 |  |
| CrossTenantAccessDetected | 是 | 是 |  |

## 1. 逐条验证

### `ApiJobDown`（group：`after-sales-component-health`）

- **表达式**：```up{job="api"} == 0```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`up{job="api"} == 0` 且持续 `for: 1m` —— api 目标抓取失败/进程 down。
- **可定位**：summary/description 指向 OPS_RUNBOOK §4，并说明 api:8000 抓取失败。
  - 真实 annotations.summary：`API 抓取失败（job=api）`
  - 真实 annotations.description：`Prometheus 抓不到 /api/metrics（api:8000）。API down 或不可达；值班按 OPS_RUNBOOK §4 处理。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。`up` 是瞬时值，api 恢复 `up=1` 即恢复；`for: 1m` 抑制抖动。

### `PrometheusJobDown`（group：`after-sales-component-health`）

- **表达式**：```up{job="prometheus"} == 0```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`up{job="prometheus"} == 0` 且 `for: 2m`。
- **可定位**：summary 提到“观测链路中断”，description 指向检查 prometheus 容器。
  - 真实 annotations.summary：`Prometheus 自身 down`
  - 真实 annotations.description：`观测链路中断。检查 prometheus 容器。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。prometheus down 恢复后 `up=1` 即恢复。

### `LokiJobDown`（group：`after-sales-component-health`）

- **表达式**：```up{job="loki"} == 0```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`up{job="loki"} == 0` 且 `for: 2m`。
- **可定位**：summary “Loki 抓取失败”，description 指向核对 promtail/loki。
  - 真实 annotations.summary：`Loki 抓取失败`
  - 真实 annotations.description：`日志存储链路可能中断；核对 promtail/loki。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。loki 恢复后 `up=1` 即恢复。

### `LoginBruteForceAttempts`（group：`after-sales-login-security`）

- **表达式**：```sum(rate(login_rate_limited_total[5m])) > 0.5```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`sum(rate(login_rate_limited_total[5m])) > 0.5` —— 5 分钟内登录限流速率 > 0.5 次/秒（合成样本 200 次/300s→0.667 命中）。
- **可定位**：description 指向 OPS_RUNBOOK §4（疑似爆破，检查来源 IP/账号），并指到 GET /api/audit 或 Loki（含 PII 脱敏）。
  - 真实 annotations.summary：`登录被限流次数上升（疑似暴力破解）`
  - 真实 annotations.description：`5 分钟内登录被限流（login_rate_limited_total）速率 > 0.5 次/秒。按 OPS_RUNBOOK §4：疑似爆破，检查来源 IP / 账号尝试，必要时临时收紧限流或锁定来源；IP/账号明细走 GET /api/audit 或 Loki（含 PII 脱敏）。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。`rate[5m]` 窗口内无新限流即回到 0；`for: 1m` 抑制短路。

### `ComponentRestartDetected`（group：`after-sales-component-restart`）

- **表达式**：```increase(component_restart_total[5m]) > 0```
- **可触发**：❌ 不可触发。表达式 `increase(component_restart_total[5m]) > 0`，但该指标**未在任何**打点/编排层上报（store 无此 metric）→ 恒为 False，不会发声。
- **可定位**：summary 含 `{{ $labels.component }}`，description 说明需编排层接入 component_restart_total。
  - 真实 annotations.summary：`组件发生重启：{{ $labels.component }}`
  - 真实 annotations.description：`检测到 api/worker/redis/postgres/nginx 重启事件；核对重启韧性（数据面不依赖 Redis、读接口恢复）。需编排层接入 component_restart_total。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。n/a（当前不触发）。

### `ApiHighErrorRate`（group：`after-sales-api-error-rate`）

- **表达式**：```sum(rate(api_requests_total{status=~"5.."}[5m])) by (route, method)   / clamp_min(sum(rate(api_requests_total[5m])) by (route, method), 0.001) > 0.05```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`sum(rate(api{status=~"5.."}[5m])) by (route,method) / clamp_min(sum(rate(api[5m])) by (route,method),0.001) > 0.05`（合成样本 40/500→8%>5% 命中）。
- **可定位**：summary 带 route/method，description 指向 Langfuse trace/Loki/审计。
  - 真实 annotations.summary：`API 5xx 错误率上升：route={{ $labels.route }} method={{ $labels.method }}`
  - 真实 annotations.description：`5xx 占 5 分钟请求比例 > 5%。核对 Langfuse trace/Loki/审计。指标已由 HTTP 中间件按 status 记录。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。`rate` 窗口内 5xx 占比回落即恢复；5xx 计数不增长即回到阈值下。

### `ApprovalFailureRateHigh`（group：`after-sales-approval`）

- **表达式**：```sum(rate(approval_decisions_total{status=~"rejected|error|timeout"}[5m]))   / clamp_min(sum(rate(approval_decisions_total[5m])), 0.001) > 0.05```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`sum(rate(approval{status=~"rejected|error|timeout"}[5m])) / clamp_min(sum(rate(approval[5m])),0.001) > 0.05`（合成样本 30/530→5.66%>5% 命中）。
- **可定位**：description 指向 GET /api/audit?target_type=approval。
  - 真实 annotations.summary：`审批决策失败率上升`
  - 真实 annotations.description：`审批拒绝/异常/超时占比 > 5%。核对 GET /api/audit?target_type=approval。指标已按 status 记录。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。`rate` 窗口内失败占比回落即恢复。

### `ApprovalLatencyHigh`（group：`after-sales-approval`）

- **表达式**：```histogram_quantile(0.95, sum(rate(approval_decision_latency_seconds_bucket[10m])) by (le)) > 30```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`histogram_quantile(0.95, sum(rate(approval_decision_latency_seconds_bucket[10m])) by (le)) > 30`（合成样本 95% 落在 +Inf（>30s）→ p95 远超 30）。
- **可定位**：description 指向对外网关 / Langfuse 审批 span。
  - 真实 annotations.summary：`审批耗时偏高（p95 > 30s）`
  - 真实 annotations.description：`审批决策延迟 p95 超阈值。核对对外网关/Langfuse 审批 span。指标已接入。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。p95 回落至 ≤30s 即恢复；直方图依赖真实审批延迟打点。

### `ReconciliationMismatch`（group：`after-sales-reconcile`）

- **表达式**：```increase(reconcile_mismatch_total[5m]) > 0```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`increase(reconcile_mismatch_total[5m]) > 0` —— 5 分钟内出现新 mismatch 即触发（合成样本 inc=1 命中）。
- **可定位**：description 指向 OPS_RUNBOOK（不一致→转人工），明细走 GET /api/audit?target_type=operation。
  - 真实 annotations.summary：`对账出现 mismatch`
  - 真实 annotations.description：`5 分钟内出现对账不一致（reconcile_mismatch_total>0）。按 OPS_RUNBOOK：不一致/不可核实→转人工，绝不静默；明细走 GET /api/audit?target_type=operation。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。用 `increase[5m]>0` 而非 `>0`：窗口内无新 mismatch 即回到 0，可关闭（计数器单调递增本身不会回落）。

### `NoReconciliation`（group：`after-sales-reconcile`）

- **表达式**：```time() - reconcile_last_run_timestamp_seconds > 86400```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`time() - reconcile_last_run_timestamp_seconds > 86400`（对账任务超过 24h 未运行）。gauge 由 `engine.reconcile()`→Celery 对账任务在每次对账末尾写入。
- **可定位**：description 指向检查 celery 对账任务/worker。
  - 真实 annotations.summary：`对账任务超 24h 未运行`
  - 真实 annotations.description：`无对账执行痕迹（最后一次对账运行已超 24h）。检查 celery 对账任务/worker 是否故障。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。对账任务恢复运行会刷新 gauge（`_now()`），差值回落即恢复。

### `HighHumanInterventionRate`（group：`after-sales-human-intervention`）

- **表达式**：```sum(rate(human_intervention_total[5m]))   / clamp_min(sum(rate(api_requests_total[5m])), 0.001) > 0.20```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`sum(rate(human_intervention_total[5m])) / clamp_min(sum(rate(api_requests_total[5m])),0.001) > 0.20`（合成样本 human 200/总 api 800 → 0.25>20% 命中）。
- **可定位**：description 指向 capability/RAG/写操作门控，明细走 GET /api/audit?target_type=operation。
  - 真实 annotations.summary：`人工介入率上升`
  - 真实 annotations.description：`转人工/升级占比 > 20%。可能为模型档位不足/检索不过/金额或资格异常。检查 capability/RAG/写操作门控；明细走 GET /api/audit?target_type=operation。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。`rate[5m]` 窗口内人工介入占比回落即恢复。

### `RpoExceeded`（group：`after-sales-rpo-rto`）

- **表达式**：```drill_rpo_seconds{component="pg_backup"} > 900```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`drill_rpo_seconds{component="pg_backup"} > 900`（RPO > 15min）。**写路径缺口**：`restore_drill.sh` 只把 RPO/RTO 写入 `deploy/drills/records/*.json|*.md`，**未回填此 gauge** → 当前不会触发。需 dr-engineer(t2) 在演练脚本内把实测 RPO 写入该 gauge（或经受控指标端点回填）。
- **可定位**：description 指向 OPS_RUNBOOK §3（超基线→评估回滚）。
  - 真实 annotations.summary：`RPO 超阈值（>15min）`
  - 真实 annotations.description：`实测 RPO 超 900s。按 OPS_RUNBOOK §3 视为超基线，评估回滚。gauge 由备份恢复演练脚本写入。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。gauge 一旦（由脚本）写入 ≤900 的实测值即恢复；当前因未回填而不触发。

### `RtoExceeded`（group：`after-sales-rpo-rto`）

- **表达式**：```drill_rto_seconds{component="pg_backup"} > 3600```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`drill_rto_seconds{component="pg_backup"} > 3600`（RTO > 60min）。同 RpoExceeded：**写路径缺口**（restore_drill.sh 仅写 records，不写 gauge）。
- **可定位**：description 指向 OPS_RUNBOOK §3。
  - 真实 annotations.summary：`RTO 超阈值（>60min）`
  - 真实 annotations.description：`实测 RTO 超 3600s。按 OPS_RUNBOOK §3 视为超基线。gauge 由备份恢复演练脚本写入。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。同 RpoExceeded（需脚本回填后才有意义）。

### `TenantDenialSpike`（group：`after-sales-tenant-security`）

- **表达式**：```increase(security_denials_total[5m]) > 5```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`increase(security_denials_total[5m]) > 5` —— 5 分钟内安全拒绝 > 5 次（合成样本 inc=8 命中）。
- **可定位**：description 指向 GET /api/audit 四维追溯。
  - 真实 annotations.summary：`租户/安全拒绝事件激增`
  - 真实 annotations.description：`5 分钟内安全拒绝 > 5 次。核对审计：GET /api/audit?target_type=... 四维追溯。指标已按 kind（reason）记录。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。`increase[5m]>5`：窗口内无新拒绝即回到 0，可关闭。

### `CrossTenantAccessDetected`（group：`after-sales-tenant-security`）

- **表达式**：```increase(security_denials_total{kind=~"cross_tenant|cross_user_|access_denied|forbidden|role_mismatch"}[5m]) > 0```
- **可触发**：✅ 可触发（合成样本命中，求值器返回 True）。`increase(security_denials_total{kind=~"cross_tenant|cross_user_|access_denied|forbidden|role_mismatch"}[5m]) > 0`（合成样本 kind=cross_tenant inc=1 命中）。
- **可定位**：description 指向 OPS_RUNBOOK §4（platform_admin 独立流程 + 二次确认 + 不可抵赖审计）与 GET /api/audit。
  - 真实 annotations.summary：`检测到跨租户/越权访问拒绝`
  - 真实 annotations.description：`出现跨租户/越权访问拒绝事件。按 OPS_RUNBOOK §4：越权/跨租户走 platform_admin 独立流程 + 二次确认 + 不可抵赖审计；核对 GET /api/audit。`
- **可关闭**：✅ 可关闭（事件消逝样本返回未触发）。`increase[5m]>0`：窗口内无该类拒绝即恢复，可关闭。

## 2. 规则 ↔ 指标映射（与 src/observability/metrics.py + 打点处一致，全部有界标签）

| 规则 | 依赖指标 | 打点位置 | 标签（有界） |
|---|------|----------|------|
| ApiJobDown/PrometheusJobDown/LokiJobDown | `up{job=...}` | Prometheus 自动生成 | job |
| LoginBruteForceAttempts | `login_rate_limited_total` | src/api/routes.py::login | reason |
| ApiHighErrorRate | `api_requests_total` | src/main.py HTTP 中间件 | route/method/status |
| ApprovalFailureRateHigh | `approval_decisions_total` | src/api/routes.py::decide_approval | route/status |
| ApprovalLatencyHigh | `approval_decision_latency_seconds` | src/api/routes.py::decide_approval | route |
| ReconciliationMismatch | `reconcile_mismatch_total` | src/execution/engine.py::_reconcile_mismatch | kind |
| NoReconciliation | `reconcile_last_run_timestamp_seconds` | src/execution/engine.py::reconcile | 无标签 |
| HighHumanInterventionRate | `human_intervention_total` | routes.py order_deny/approval_timeout + engine.py execution_handoff/reconcile_mismatch | kind |
| RpoExceeded | `drill_rpo_seconds` | ⚠️ 未回填（restore_drill.sh 只写 records/*.md） | component |
| RtoExceeded | `drill_rto_seconds` | ⚠️ 未回填（restore_drill.sh 只写 records/*.md） | component |
| TenantDenialSpike/CrossTenantAccessDetected | `security_denials_total` | src/auth/security.py::audit_security_denial + routes.py 不可信回调 | kind |

## 3. 缺口与建议（如实标注，勿臆造达标）

1. **RPO/RTO 规则暂不可真正触发**：`restore_drill.sh`/`backup_encrypted.sh` 仅把实测 RPO/RTO 写入 `deploy/drills/records/drill-pg-encrypted-restore.json` 与 `DR-...-pg-encrypted-restore.md`，**未回填** `drill_rpo_seconds`/`drill_rto_seconds` gauge（该 gauge 只被测试与规则引用）。要使 `RpoExceeded`/`RtoExceeded` 触发，需 dr-engineer(t2) 在演练脚本/受控任务里 `get_metrics().set("drill_rpo_seconds",...，{"component":"pg_backup"})` 回填。
2. **ComponentRestartDetected** 依赖的 `component_restart_total` 未在编排层打点，当前恒不触发（表达式合法、不误报）。
3. **阈值均为占位初稿**：RPO≤900s/RTO≤3600s、错误率>5%、人工>20%、审批>5% 等需以真实演练/观测校准。

> 口径红线：所有规则标签均为有界维度（route/status/kind/component/job），绝不含 `tenant_id`/`user_id`/`order_id`/`operation_id`/`thread_id`/`approval_id`。
