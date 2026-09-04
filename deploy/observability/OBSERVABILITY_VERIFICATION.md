# 自托管可观测核对与补强（Observability Verification & Reinforcement）

> 目的：核对「电商售后多智能体工单系统」自托管可观测链路是否按《代理宪法》第一层 §1（完全自托管/无未审计出网）
> 与第二层 §7（全链路可追踪/租户维度可追踪/只写客观指标）落地，并列出**补强项**。
> 核对基准 = 仓库实际代码/配置（只读核对，不改证据）。

---

## 一、核对结果（已就绪 ✅）

| 链路 | 需求 | 现状 | 依据 |
|------|------|------|------|
| **Langfuse trace 带租户/会话/环境** | trace 必须写 `tenant_id`/`session_id`/`environment` 到 metadata | ✅ 已实现 | `src/observability/tracing.py`：`span()` 构造 `meta={"environment":...}`，`tenant_id`、`session_id` 写入 metadata；`user_id` 作为 Langfuse 用户维度，`session_id` 同时用于 trace session。`chat.run`、`approval.decision`、`execution.*` 均以 `trace_span(...)` 包裹。未配置 `langfuse_public_key` 时 no-op，绝不阻塞业务。 |
| **输入输出脱敏** | 明文 trace 不得含地址/支付/订单号 | ✅ 已实现 | `tracing.py` 对 input/output 经 `_redact_payload`/`redact()` 脱敏。 |
| **/api/metrics 内网化** | 只允许白名单内网抓取，否则 fail-closed | ✅ 已实现 | `src/api/routes.py::_metrics_request_allowed` + `/metrics` 路由（来源 IP 命中 `metrics_allowed_sources` 才返回，否则 403）；`src/config.py`：`metrics_expose_internal_only`（受限环境恒为仅内网）+ `metrics_allowed_sources`（如 prod=`172.31.0.0/16`）。 |
| **指标标签红线** | 禁止高基数/敏感 `tenant_id` 等作标签 | ✅ 已实现 | `src/observability/metrics.py::_FORBIDDEN_LABELS` 抛 `ValueError`；标签仅用有界 `route/status/kind/mode/job/component`。 |
| **Loki 日志采集** | 应用日志（脱敏后）采集到自托管 Loki | ✅ 已实现 | `deploy/observability/loki-config.yml`（单二进制）+ `promtail-config.yml`（docker_sd + relabel service/container）；`deploy/scripts/observability.sh`。 |
| **结构化日志带租户上下文** | 日志可追踪 tenant/session/operation | ✅ 已实现 | `src/observability/logging.py`：`JsonFormatter` 把 `tenant_id/session_id/operation_id/request_id/environment` 写入 `extra`，emit 前经 `redact()`。 |
| **告警规则（权威单源）** | Prometheus 加载 == 仓库内 catalog | ✅ 已实现 | `deploy/observability/alert-rules.yml`（8 组/15 条）被 `prometheus.yml::rule_files` 引用，并经 `docker-compose.observability.yml` 挂载为 `/etc/prometheus/alert-rules.yml`。 |
| **告警规则评估** | 有可判定/校验规则的手段 | ✅ 已实现 | `deploy/observability/evidence/eval_alert_rules.py`（对合成样本求值 + 规则语法/结构校验），输出 `ALERT_RULES_VERIFICATION.md`；另有 `promtool check rules`。 |
| **告警通知** | 告警推送自托管 Alertmanager | ✅ 已实现 | `deploy/observability/alertmanager.yml` + `prometheus.yml::alerting.alertmanagers -> alertmanager:9093`。 |
| **观测自身存活** | 观测栈不失效（盲区） | ✅ 已实现 | `observability.sh health`（Prometheus/Grafana/Loki/Langfuse 运行 + `curl /api/metrics` 抓取验证）。 |
| **核心业务指标** | 审批/执行/对账/安全/人工介入有界计数 | ✅ 已实现 | `api_requests_total`、`approval_decisions_total`、`approval_decision_latency_seconds`、`execution_submit_total`、`execution_outcome_total`、`execution_callback_total`、`reconcile_mismatch_total`、`reconcile_last_run_timestamp_seconds`、`human_intervention_total`、`security_denials_total`、`drill_{rpo,rto}_seconds`、`audit_queries_total`、`login_attempts_total`、`login_rate_limited_total`。 |

---

## 二、补强项（Gap & Recommendation）🟡

以下为**未接入/建议补强**，均遵循「有界标签 + 只写客观指标」红线；落地前须实测校准阈值。

| # | 缺口 | 现状 | 建议补强 |
|---|------|------|----------|
| 1 | **模型拒答率无专用计数器** | 当前仅能通过 `human_intervention_total{kind=...}`、`execution_outcome_total{status="human_handoff"}` 与「无已批准审批的写操作」间接推断 | 新增有界计数器 `capability_gate_total{result}`（`result=blocked|allowed`）与 `model_refusal_total{kind}`（`kind=write_not_whitelist|hallucination_fail|low_confidence`），在 `src/graph/nodes.py` 写操作门控失败处与 `BaseLLM.capability_ok=False` 处上报。落地后替换 `DAILY_CHECKS_RUNBOOK §7` 的间接表达式。 |
| 2 | **HTTP/图执行延迟无直方图** | `api_requests_total` 是 counter（无延迟直方图）；延迟明细走 Langfuse/Loki | 新增 `api_request_latency_seconds`（histogram，有界 `route`/`method`）与 `chat_execution_latency_seconds`，使 `p95` 可直接在 Prometheus 算，避免仅靠 Langfuse 人工检索。 |
| 3 | **`component_restart_total` 未接入** | `alert-rules.yml`/`alert-rules-README` 已标注「未接入」（引用缺失指标→不误报） | 编排层在重启事件处上报 `component_restart_total{component=~"api|worker|redis|postgres|nginx"}`，落地后 `ComponentRestartDetected` 自动生效。 |
| 4 | **「重复 operation」缺专用指标** | 由 `approval_decisions_total{status="replay"}` 与 SQL 幂等键重复检测兜底 | 新增 `idempotency_replay_total{result}`（`result=replay|duplicate_detected`），在 `store` 幂等键冲突路径 inc，让重复 operation 有直接告警序列。 |
| 5 | **对账批处理健康** | 有 `reconcile_last_run_timestamp_seconds`（>24h 告警 `NoReconciliation`） | 增加 `reconcile_batch_total{tenant_id_present=false}`（有界维度，示 batch 级成功/失败），并保证对账任务异常进入死信转人工。 |
| 6 | **审计明细 vs 监控分流** | 租户明细走 `GET /api/audit`（不入 Prometheus 标签）——已合规 | 持续核对：任何新增指标不得引入 `tenant_id/user_id/thread_id/order_id/operation_id/approval_id/client_request_id` 作标签。 |

---

## 三、每日快检（观测健康 + 路由闭环）

```bash
# 1) 观测栈健康（4 服务运行 + 可抓取 /api/metrics）
bash deploy/scripts/observability.sh health

# 2) 应用 /api/metrics 内网化复验（应命中白名单才 200，否则 403）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/metrics
#    非白名单来源（模拟外网）应 403 —— fail-closed

# 3) 告警规则合法性
promtool check rules /etc/prometheus/alert-rules.yml   # 或等价 eval_alert_rules.py

# 4) Langfuse 元数据抽检（内部仪表/API）
#    确认任一 trace 的 metadata 含 environment / tenant_id / session_id
```

---

## 四、结论

- **核对通过项**：Langfuse trace 元数据（`tenant_id`/`session_id`/`environment` + 输入输出脱敏）、
  `/api/metrics` 内网化（来源白名单 fail-closed）、Loki+Promtail 日志采集、告警规则单一权威 +
  `eval_alert_rules.py` 评估、指标标签红线、审计明细走受控查询（不落 Prometheus 高基数标签）、完全自托管。
- **补强项**：以 `## 二` 六个缺口的落地为主，落地后同步更新 `alert-rules.yml`（**勿新增第二份规则文件**）与
  `DAILY_CHECKS_RUNBOOK.md` 的检测表达式。所有阈值以真实演练/实测校准，不得未经实测宣称达标。

---

*与《OPS_RUNBOOK §6 观测链路运维》《DAILY_CHECKS_RUNBOOK》《代理宪法 第一层 §1 / 第二层 §7》配套。*
