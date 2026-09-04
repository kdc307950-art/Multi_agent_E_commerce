# Alertmanager 通知通道 · 验证证据（t4）

> 结论：**告警能触发 → 路由 → 通知** 闭环成立。Prometheus 规则 → Alertmanager(自托管) → 自托管
> webhook 接收端（sink）全链路验证通过，且捕获到**真实** Prometheus 告警（非仅合成注入）。

---

## 1. 组件与镜像（全部自托管，obs 内网，不发布宿主端口）

| 服务 | 容器名 | 镜像 | 说明 |
|---|---|---|---|
| Alertmanager | `after-sales-observability-alertmanager-1` | `prom/alertmanager:v0.27.0`（docker.io；镜像源 docker.1ms.run 未收录） | 路由/分组/通知；`--config.file=/etc/alertmanager/alertmanager.yml --storage.path=/alertmanager` |
| webhook 接收端 | `after-sales-observability-alert-webhook-sink-1` | `python:3.12-alpine`（docker.io；docker.1ms.run 该 layer 不可用） | 运行 `deploy/observability/evidence/mock_webhook_receiver.py`（stdlib，POST 落盘 JSONL + 200） |

- 配置：`deploy/observability/alertmanager.yml`、`deploy/observability/evidence/mock_webhook_receiver.py`
- 挂载数据：`./data/alertmanager:/alertmanager`、`./data/alertmanager-webhooks:/data`（容器内 `/data/inbox.jsonl`）

## 2. 接线（prometheus → alertmanager）

`deploy/observability/prometheus.yml` 的 `alerting` 段：
```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager:9093"]
```
运行态验证：`GET /api/v1/alertmanagers` →
```json
{"activeAlertmanagers":[{"url":"http://alertmanager:9093/api/v2/alerts"}],"droppedAlertmanagers":[]}
```

## 3. 配置校验

```text
$ promtool check config /etc/prometheus/prometheus.yml
  SUCCESS: 1 rule files found
  SUCCESS: /etc/prometheus/prometheus.yml is valid prometheus config file syntax
  Checking /etc/prometheus/alert-rules.yml -> SUCCESS: 15 rules found

$ promtool check rules /etc/prometheus/alert-rules.yml
  SUCCESS: 15 rules found

$ amtool check-config /etc/alertmanager/alertmanager.yml
  Checking '/etc/alertmanager/alertmanager.yml'  SUCCESS
  Found: global config / route / 0 inhibit rules / 1 receivers / 0 templates

$ GET alertmanager /-/healthy
  OK
```

## 4. 健康状态

`docker ps`：`after-sales-observability-alertmanager-1`、`after-sales-observability-alert-webhook-sink-1`
均 `Up (healthy)`。

## 5. 告警到 webhook sink 的端到端证据（sink 容器日志 `docker logs ...`）

> 关键：以下 **RpoExceeded / RtoExceeded** 为 **Prometheus 真实触发**的告警（来源
> `instance=after-sales-prod-api-1:8000, job=api_prod`，即**生产** API），经 Alertmanager 路由后
> 到达自托管 sink——证明完整链路（规则→Alertmanager→通知）可用。

```log
[sink] listening on 0.0.0.0:8080, out=/data/inbox.jsonl
[sink] received webhook: {"ts":..., "path": "/webhook", "body": {"receiver": "alert-webhook", "status": "firing",
  "alerts": [{"labels": {"alertname": "RpoExceeded", "component": "backup", "env": "preview",
    "instance": "after-sales-prod-api-1:8000", "job": "api_prod", "severity": "critical"}, ...}]}}
[sink] received webhook: {"ts":..., "path": "/webhook", "body": {"receiver": "alert-webhook", "status": "firing",
  "alerts": [{"labels": {"alertname": "RtoExceeded", "component": "backup", "env": "preview",
    "instance": "after-sales-prod-api-1:8000", "job": "api_prod", "severity": "critical"}, ...}]}}
```

另含一处**合成**验证告警（幂等/路由自测）：`T4WebhookChannelCheck2`（severity=critical, env=preview），
同样命中 `receiver=alert-webhook` 并落盘到 `/data/inbox.jsonl`。

## 6. alertmanager.yml 要点（deploy/observability/alertmanager.yml）

```yaml
global:
  resolve_timeout: 5m
route:
  group_by: ['alertname', 'severity', 'env']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  receiver: alert-webhook
  routes:
    - matchers: [severity="critical"]
      receiver: alert-webhook
      repeat_interval: 2h
      continue: false
receivers:
  - name: alert-webhook
    webhook_configs:
      - url: 'http://alert-webhook-sink:8080/webhook'
        send_resolved: true
        max_alerts: 0
```

## 7. 口径提醒（t11 演练建议）

- `ApiHighErrorRate` 已改为按 `job+route+method` 分维（避免 preview+prod 混算稀释误报；见
  `deploy/observability/alert-rules.yml` 顶部"双 job 口径提示"）。
- 其余以 `api_requests_total` 为分母（如 `HighHumanInterventionRate`）及 `approval_decisions_total`
  相关规则**暂未**按 `job` 拆分，t11 演练时请按需复核（保持单一权威文件 `alert-rules.yml`）。

*配套：《OPS_RUNBOOK §4》《deploy/EGRESS_POLICY.md》《src/observability/metrics.py》。*
