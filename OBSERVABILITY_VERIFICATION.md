# 自托管可观测核对与补强（Observability Verification & Reinforcement）—— 团队工作区指引

> **单源说明**：本文件为团队工作区根目录指引；**完整核对/补强结论见** `deploy/observability/OBSERVABILITY_VERIFICATION.md`（唯一权威）。

## 结论速览

| 维度 | 结论 |
|------|------|
| Langfuse trace 带 `tenant_id`/`session_id`/`environment` | ✅ 已实现（`src/observability/tracing.py`：metadata 写 environment/tenant_id/session_id，input/output 经脱敏；未配置 Langfuse 时 no-op 不阻塞业务） |
| `/api/metrics` 内网化 | ✅ 已实现（`src/api/routes.py::_metrics_request_allowed` + `metrics_expose_internal_only`/`metrics_allowed_sources`，非白名单来源 403 fail-closed） |
| Loki 日志采集 | ✅ 已实现（`deploy/observability/loki-config.yml` + `promtail-config.yml` + `observability.sh health`） |
| 告警规则 evaluation / `alert_rules.py` | ✅ 已实现（`deploy/observability/alert-rules.yml` 单一权威 + `prometheus.yml::rule_files` 引用 + `evidence/eval_alert_rules.py` 评估） |
| 指标标签红线（禁高基数 `tenant_id`） | ✅ 已实现（`metrics.py::_FORBIDDEN_LABELS` 抛 `ValueError`） |
| 审计明细走受控查询（不落 Prometheus 标签） | ✅ 已实现（`GET /api/audit` 四维追溯） |

## 补强项（🟡 未接入/建议，详见权威文档 §二）

1. 模型拒答率无专用计数器 → 建议新增 `capability_gate_total{result}` / `model_refusal_total{kind}`。
2. HTTP/图执行延迟无直方图 → 建议新增 `api_request_latency_seconds`（histogram，有界 `route`/`method`）。
3. `component_restart_total` 未接入（规则已标注，不误报）。
4. 重复 operation 缺专用指标 → 建议新增 `idempotency_replay_total{result}`。
5. 对账批处理健康强化 → `reconcile_batch_total`。
6. 持续核对新增指标不得引入高基数/敏感标签。

> 所有阈值以真实演练/实测校准，未经实测不得宣称达标（《代理宪法 第一层 §4.2 / 第二层 §7.4》）。
