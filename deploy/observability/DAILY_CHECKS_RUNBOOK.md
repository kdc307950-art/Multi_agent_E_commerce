# 每日检查 Runbook（DAILY CHECKS RUNBOOK）—— 运维侧操作副本

> **单源说明**：本文件是根目录 `DAILY_CHECKS_RUNBOOK.md` 的**运维侧操作副本**，内容与其保持一致（改一处必改两处）。
> 它落在 `deploy/observability/` 与其配套文档（`alert-rules.yml`、`alert-rules-README.md`、`prometheus.yml`、`loki-config.yml`、
> `promtail-config.yml`）同处，供值班/运维在观测栈内直接引用。
> 8 项检查的完整「检测手段 / 阈值 / 处置」及三大核心（重复 operation / 对账 mismatch / 失败补偿）告警表达式，
> 见**根目录权威版本** `DAILY_CHECKS_RUNBOOK.md`——此处不再重复整表，仅保留**执行序**与**权威版本指引**。

---

## §0. 每日检查执行序（建议日切后 08:30）

| 顺序 | 检查项 | 检查对象 | 耗时 |
|-----|--------|---------|------|
| ① | 数据面与隔离（租户越权 / 重复 operation） | PostgreSQL RLS 数据 + 审计 | 10min |
| ② | 资金/审批链路（审批异常 / 失败补偿） | approvals / executions | 10min |
| ③ | 回调与对账（回调 mismatch / 重复/对账） | gateway_txns / executions | 10min |
| ④ | 观测链路（延迟 / 人工介入 / 模型拒答） | Langfuse / Prometheus / Loki | 10min |
| ⑤ | 汇总留痕 | 写 `DAILY_OBSERVATION-<date>.md` + 指标报告 | 5min |

## §1. 立即生效的核心告警表达式（复制进 `alert-rules.yml` 前先校准）

| 目标 | 表达式 | 级别 |
|------|--------|------|
| 重复 operation / 审批重放 | `increase(approval_decisions_total{status="replay"}[24h]) > 0` | critical |
| 对账 mismatch | `increase(reconcile_mismatch_total[24h]) > 0`；`sum(execution_outcome_total{status="mismatched"}) > 0` | critical |
| 对账任务失联 | `time() - reconcile_last_run_timestamp_seconds > 86400` | warning |
| 失败补偿 | `increase(execution_outcome_total{status=~"compensation_failed|failed_uncertain|failed_dispatch"}[24h]) > 0` | critical |
| 回调 mismatch | `increase(execution_callback_total{reason=~"signature_invalid|amount_mismatch|missing_nonce|terminal_locked|illegal_transition"}[24h]) > 0` | critical |
| 租户越权 | `increase(security_denials_total{kind=~"cross_tenant|cross_user_|forbidden|role_mismatch|access_denied"}[24h]) > 0` | critical |

> 口径：**禁止用 `tenant_id` 等高基数/敏感维度作 Prometheus 标签**；计数用 `increase(...[窗口])` + 阈值；
> 这些为**初稿占位**，须实测校准后合入**唯一权威文件** `deploy/observability/alert-rules.yml`，**不新增第二份规则文件**。

## §2. 观测链路每日快检

```bash
bash deploy/scripts/observability.sh health
curl -s http://127.0.0.1:9090/-/healthy   # Prometheus
curl -s http://127.0.0.1:3002/health      # Langfuse（自托管）
curl -s http://127.0.0.1:3100/ready       # Loki
```

**核对/补强结论**见 `deploy/observability/OBSERVABILITY_VERIFICATION.md`（Langfuse trace 已带
`tenant_id`/`session_id`/`environment` 元数据、`/api/metrics` 内网化、Loki+Promtail 采集、告警规则评估 `eval_alert_rules.py`）。

---

*权威内容以根目录 `DAILY_CHECKS_RUNBOOK.md` 为准。*
