# 运营告警规则 · 说明与接线（单一权威来源）

> **权威规则文件（单一事实源）**：`deploy/observability/alert-rules.yml`（t7 最终交付，8 组 / 15 条）。
> 该文件经 `docker-compose.observability.yml` 的 `prometheus` 服务挂载为容器路径
> `/etc/prometheus/alert-rules.yml`，并由 `deploy/observability/prometheus.yml` 的 `rule_files` 引用。
> **Prometheus 实际加载的规则 == 仓库内 catalog**（挂载与 `rule_files` 均指向同一文件，唯一权威）。
>
> ⚠️ 历史漂移说明：早期 t4 阶段曾在子目录 `deploy/observability/alert-rules/alert-rules.yml` 放**模板骨架**，
> 后已被权威文件上位并删除该重复副本（目录已移除）。**仓库内只有一份告警规则**，不要再新增第二份。
> 相关《OPS_RUNBOOK §2 上线阈值》《§3 回滚条件》与「只写客观指标、绝不用高基数 tenant_id 作标签」纪律。
> 数值均为**初稿占位**，须以真实演练/实测校准。

---

## 1. 告警规则清单（`alert-rules.yml`，8 组 / 15 条）

| 规则 | 面向维度 | 表达式是否可立即生效 | 阈值（占位，须校准） |
|------|---------|--------------------|--------------------|
| `ApiJobDown` | 组件存活 | ✅ 立即可用（基于 `up{job="api"}`） | `up==0` 持续 1m |
| `PrometheusJobDown` / `LokiJobDown` | 观测组件存活 | ✅ 立即可用 | `up==0` 持续 2m |
| `ApiHighErrorRate` | 错误率 | ✅ 已接入（`api_requests_total` 带 `status`） | 5xx 占比 > 5%（`for: 5m`） |
| `ApprovalFailureRateHigh` | 审批失败率 | ✅ 已接入（`approval_decisions_total` 带 `status`） | 失败占比 > 5%（`for: 10m`） |
| `ApprovalLatencyHigh` | 审批耗时 | ✅ 已接入（`approval_decision_latency_seconds`） | p95 > 30s（`for: 10m`） |
| `ReconciliationMismatch` | 对账 mismatch | ✅ 已接入（`reconcile_mismatch_total`） | `increase(...[5m]) > 0`（`for: 5m`） |
| `NoReconciliation` | 对账运行 | ✅ 已接入（`reconcile_last_run_timestamp_seconds`） | 超 24h 未运行（`for: 30m`） |
| `HighHumanInterventionRate` | 人工介入率 | ✅ 已接入（`human_intervention_total`） | > 20%（`for: 15m`） |
| `RpoExceeded` / `RtoExceeded` | RPO/RTO 突破 | ✅ 已接入（`drill_{rpo,rto}_seconds`） | RPO>900s / RTO>3600s |
| `TenantDenialSpike` | 租户/安全拒绝 | ✅ 已接入（`security_denials_total{kind}`） | 5 分钟 > 5 次 |
| `CrossTenantAccessDetected` | 跨租户/越权 | ✅ 已接入（`security_denials_total{kind=~"cross_tenant|..."}`） | `increase(...[5m]) > 0`（`for: 1m`） |
| `ComponentRestartDetected` | 组件重启 | ⚠️ **未接入**（`component_restart_total` 指标未实现；表达式引用缺失指标→不误报） | 需编排层接入 `component_restart_total` |

> **不误报约束**：`ComponentRestartDetected` 引用的 `component_restart_total` 尚未由编排层上报，Prometheus 对缺失序列
> 求值为空向量（`vector(0)`），**不会产生误报警**；待指标落地后自动生效，无需改规则。

---

## 2. 口径红线

- **绝不用高基数/敏感维度做 Prometheus 标签**：`tenant_id`、`user_id`、`thread_id`、`order_id`、`operation_id`、
  `approval_id`、`client_request_id` 一律禁止（`metrics.py::_FORBIDDEN_LABELS` 会抛 `ValueError`）。标签只用有界维度
  （`route`/`status`/`kind`/`node`/`job`/`component`）。租户明细一律走受控审计查询（`GET /api/audit`）。
- **只写客观指标**：阈值须以真实演练/实测校准，禁止未实测即宣称达标。

---

## 3. 接线（已就绪，缺一不可）

1. **挂载规则**：`docker-compose.observability.yml` 的 `prometheus` 服务已挂载
   `- ./deploy/observability/alert-rules.yml:/etc/prometheus/alert-rules.yml:ro`（唯一权威文件）。✅
2. **引用规则文件**：`deploy/observability/prometheus.yml` 已设置
   ```yaml
   rule_files:
     - /etc/prometheus/alert-rules.yml
   ```
   ✅（指向被挂载的权威文件，语法即目录无漂移）
3. **抓取目标**：`/api/metrics`（api）＋ 观测自身（prometheus/loki）。组件 down 目前基于 api 抓取健康间接推断。
4. **告警控制器**：Grafana Alerting 需与 `alert-rules.yml` 保持单一来源，避免 UI 双写漂移。

---

## 4. 校验方法

- **权威判定**：以真实 Prometheus 为准。`docker-compose.observability.yml` 已将权威文件挂载为
  `/etc/prometheus/alert-rules.yml`，可用 `promtool check rules /etc/prometheus/alert-rules.yml`（或容器内）校验。
- **等价证据**（环境无 promtool/Prometheus 容器时）：`deploy/observability/evidence/eval_alert_rules.py`
  对**合成样本**求值并做规则语法/结构校验，输出见 `deploy/observability/evidence/ALERT_RULES_VERIFICATION.md`。

---

*配套：《OPS_RUNBOOK §2/§3/§4》《PREVIEW_DEPLOYMENT_CHECKLIST》《src/observability/metrics.py》。*
