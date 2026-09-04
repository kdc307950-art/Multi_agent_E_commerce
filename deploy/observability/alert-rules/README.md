# 运营告警规则 · 说明与接线

> **权威规则文件**：`deploy/observability/alert-rules.yml`（t7 最终交付，挂载到 `/etc/prometheus/alert-rules.yml`）。
> 本目录下的 `alert-rules/alert-rules.yml`（t4 模板骨架）已被**上位**，以仓库根 `deploy/observability/alert-rules.yml` 为准。
> 对应《OPS_RUNBOOK §2 上线阈值》《§3 回滚条件》与「只写客观指标、绝不用高基数 tenant_id 作标签」的纪律。
> 数值均为**初稿占位**，须以真实演练/实测校准。

---

## 1. 告警规则清单（`alert-rules.yml`）

| 规则 | 面向维度 | 表达式是否可立即生效 | 阈值（占位，须校准） |
|------|---------|--------------------|--------------------|
| `ApiJobDown` | 组件存活 | ✅ 立即可用（基于 `up{job="api"}`） | `up==0` 持续 1m |
| `PrometheusJobDown` / `LokiJobDown` | 观测组件存活 | ✅ 立即可用 | `up==0` 持续 2m |
| `ApiHighErrorRate` | 错误率 | ⚠️ 当前**为空**（需 status 标签） | 5xx 占比 > 5% |
| `ApprovalFailureRateHigh` | 审批失败率 | ⚠️ 当前**为空**（需 status 标签） | 失败占比 > 5% |
| `ReconciliationMismatch` | 对账 mismatch | ⚠️ 当前**为空**（需新指标） | `> 0` |
| `HighHumanInterventionRate` | 人工介入率 | ⚠️ 当前**为空**（需新指标） | > 20% |
| `RpoExceeded` / `RtoExceeded` | RPO/RTO 突破 | ⚠️ 当前**为空**（需 push/记录外链） | RPO>900s / RTO>3600s |

> **明确不误报**：那些"当前为空"的规则，其 PromQL 表达式引用了尚未存在的标签/指标，Prometheus 对缺失的
> 序列会求值为空向量（`vector(0)`），**不会产生误报警**；代价是**也不会真正告警**，直到指标增强落地。

---

## 2. 指标缺口与前置（**必须回填/增强，否则对应告警不生效**）

| 告警 | 现状 | 需要的前置（来源） |
|------|------|-------------------|
| ApiHighErrorRate | `src/api/routes.py` 的 `api_requests_total` 只有 `(route,method)` 标签 | 给 `get_metrics().counter("api_requests_total", ("route","method","status"), {...})` 增加 `status` 维度（**回填来源：t2 安全评审 / 指标增强**） |
| ApprovalFailureRateHigh | `approval_decisions_total` 只有 `(route)` | `approval_decisions_total` 增加 `status/reason` 维度（回填：t2） |
| ReconciliationMismatch | **无此指标** | 新增 `counter("reconcile_mismatch_total")`，在对账收口时 inc；或从日志/审计聚合（回填：t3/t6） |
| HighHumanInterventionRate | **无此指标** | 新增 `counter("human_intervention_total")`，在 `escalate_ticket`/转人工处 inc（回填：t3/t6） |
| RpoExceeded / RtoExceeded | **无此指标** | 由备份恢复演练或受控脚本写入 `drill_rpo_seconds`/`drill_rto_seconds` gauge（`component="pg_backup"`），或外链到 `<-pg-backup-restore.md>`（回填：t3/t6） |

> **口径红线**：任何新增指标**仍不得**把 `tenant_id`（及 user_id/thread_id/order_id/operation_id/approval_id/
> client_request_id）用作标签——这是 `metrics.py::_FORBIDDEN_LABELS` 强制的，违反会抛 `ValueError`。
> 租户明细一律走受控审计查询（`GET /api/audit`）。

---

## 3. Grafana 说明（展示建议）

- **数据源**：Grafana 对接自托管 Prometheus（`http://prometheus:9090`）；标签有界，无高基数维度。
- **建议看板（Dashboard）**：
  1. **API 健康**：`rate(api_requests_total[5m])`（按 `route` 拆分）、5xx 占比、`up{job="api"}`。
  2. **审批**：`rate(approval_decisions_total[5m])`、审批失败占比（需增强 status 后可用）。
  3. **告警状态**：`ALERTS` 面板，展示上述规则的 firing/pending 状态。
  4. **日志**：Loki 面板（Loki 数据源），按 `env`/`job` 过滤；应用日志已全局 PII 脱敏。
- **告警通知**：Grafana Alerting → 联系点（邮件/Webhook）→ 通知策略，路由到值班人；告警处理见《OPS_RUNBOOK §4》。
- **权限**：`GF_USERS_ALLOW_SIGN_UP=false`；管理员账号强口令由环境注入（`GRAFANA_ADMIN_PASSWORD`）。

---

## 4. 接线（让本规则真正生效，缺一不可）

1. **挂载规则**：`docker-compose.observability.yml` 的 `prometheus` 服务增加卷
   `- ./deploy/observability/alert-rules/alert-rules.yml:/etc/prometheus/alert-rules.yml:ro`，
   并在 `command` 里加 `--web.enable-lifecycle`（或重启 prometheus）。
2. **引用规则文件**：`deploy/observability/prometheus.yml` 增加：
   ```yaml
   rule_files:
     - /etc/prometheus/alert-rules.yml
   ```
3. **抓取目标**：若需对 postgres/redis/worker 做组件-down 直接告警，需为其增加 scrape 目标
   （当前仅 `api`、`prometheus`、`loki`）；否则组件 down 只能靠 api 抓取健康间接推断。
4. **告警状态**：Grafana Alerting 规则也可在 UI 里配置（等价于本 yml），任选其一保持单一来源，避免双写漂移。

---

## 5. 本轮状态（**如实标注：多数告警尚未接线/指标未增强**）

- ✅ `alert-rules.yml` 骨架已建；组件存活类（基于 `up`）规则可立即生效。
- ⚠️ 需增强指标的规则（错误率/审批失败率/对账/人工介入/RPO-RTO）当前**表达式为空，不在告警**。
- ⚠️ `prometheus.yml` 尚未加 `rule_files`；compose 尚未挂载本规则文件——**接线路由 t1/t5/t7 完成**。
- ⚠️ 阈值均为**占位初稿**，须以真实演练/实测（t3/t6）校准后替换，**禁止未实测即宣称达标**。

*配套：《OPS_RUNBOOK §2/§3/§4》《PREVIEW_DEPLOYMENT_CHECKLIST》《src/observability/metrics.py》。*
