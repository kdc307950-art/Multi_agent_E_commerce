# 业务指标报告（BUSINESS METRICS REPORT）

> **命名**：`BUSINESS_METRICS_REPORT-<period>.md`，`<period>` 用周/日标识（示例：`BUSINESS_METRICS_REPORT-WEEK-20260907.md`）。
> **地位**：周期（建议每周一次，可每日）汇总**客观业务指标**，与《DAILY_OBSERVATION》配套。写入 `deploy/observability/` 或 `deploy/records/`。
> **口径纪律**：只写客观指标（解决率/人工介入率/AHT/忠实度/相关性/延迟/token），按真实口径统计，不虚报（《代理宪法 第二层 §7.4》）。
> 租户明细走受控审计（`GET /api/audit`），**绝不用高基数 `tenant_id` 作 Prometheus 标签**；指标入口 `GET /api/metrics`。

---

## 0. 元信息

| 字段 | 值 |
|------|-----|
| 周期 | `<YYYY-MM-DD ~ YYYY-MM-DD>` |
| 环境 | `preview` / `production` |
| 统计口径 | `<metric 采集窗口：1h / 24h / 7d>` |
| 制表人 / 复核人 | `<name>` / `<name>` |

## 1. 核心业务指标（客观口径）

| 指标 | 定义（公式） | 本期值 | 上期值 | 阈值 | 趋势 |
|------|-------------|--------|--------|------|------|
| 解决率 Resolution Rate | 成功处理工单 / 总工单 | `<x%>` | `<x%>` | `<基线>` | ↑/→/↓ |
| 人工介入率 Human Intervention Rate | `sum(rate(human_intervention_total[1h])) / clamp_min(sum(rate(api_requests_total[1h])),0.001)` | `<x%>` | `<x%>` | ≤20% | ↑/→/↓ |
| 平均处理时长 AHT | 工单结束时-开始时（可分类型） | `<x s>` | `<x s>` | `<基线>` | ↑/→/↓ |
| 端到端延迟 p95 | Langfuse `chat.run` span 时长 p95 | `<x s>` | `<x s>` | <10s | ↑/→/↓ |
| 审批决策 p95 延迟 | `histogram_quantile(0.95, sum(rate(approval_decision_latency_seconds_bucket[1h])) by (le))` | `<x s>` | `<x s>` | ≤30s | ↑/→/↓ |
| 忠实度 Faithfulness | 答案必须基于检索文档（RAG 忠实度评测） | `<x>` | `<x>` | 全通过 | — |
| 相关性 Relevance | 检索文档与问题相关性评分 | `<x>` | `<x>` | `<基线>` | — |
| Token 消耗 | 单次对话平均 / 总计（token） | `<x>` | `<x>` | `<预算>` | ↑/→/↓ |

## 2. 资金/执行链路指标（客观口径）

| 指标 | 定义（公式） | 本期值 | 阈值 |
|------|-------------|--------|------|
| 审批决策 | `sum(approval_decisions_total{status=...})` 分布（approved/rejected/timeout/replay/error/forbidden） | `<...>` | — |
| 审批失败率 | `sum(rate(approval_decisions_total{status=~"rejected|error|timeout|forbidden"}[24h]))/clamp_min(sum(rate(approval_decisions_total[24h])),0.001)` | `<x%>` | ≤5% |
| 执行模式分布 | `sum(execution_outcome_total{mode,status})`（shadow=live） | `<...>` | shadow 默认 |
| 执行结果分布 | `execution_outcome_total{status=...}`（confirmed/compensated/mismatched/human_handoff/...） | `<...>` | — |
| 回调异常 | `execution_callback_total{reason=~"signature_invalid|amount_mismatch|missing_nonce|terminal_locked|illegal_transition"}` | `<N>` | 0 |
| 对账结果 | reconcile scanned/reconciled/mismatched | `<...>` | mismatch=0 |
| 对账任务健康 | `time() - reconcile_last_run_timestamp_seconds` | `<x s>` | ≤86400 |
| 失败补偿 | `execution_outcome_total{status=~"compensation_failed|failed_uncertain|failed_dispatch"}` | `<N>` | 0 |

## 3. 安全/合规指标（客观口径）

| 指标 | 本期值 | 阈值 |
|------|--------|------|
| 安全拒绝总数 `security_denials_total{kind=...}`（cross_tenant/forbidden/role_mismatch/...） | `<N>` | 越权=0 |
| 跨租户/越权拒绝 | `<N>` | 0（🔴 红线） |
| 登录被限流 `login_rate_limited_total{reason}` | `<N>` | — |
| 审计查询 `audit_queries_total{route}` | `<N>` | — |

## 4. 观测/质量指标（客观口径）

- RAG 无文档/相关性失败/幻觉失败 → fail-closed 次数（证据 `llm_fallback_to_human.json` / `llm_endpoint_connectivity.json`）：`<N>`
- 能力矩阵门控：非白名单模型写操作转人工数 `capability_gate`（补强后）/ 无批准审批的写操作：`<N>`

## 5. 异常与结论

- 本期触发告警：`<list>`；处置与恢复：`<...>`。
- 超阈值项：`<...>`；根因与改进建议（**只写客观事实，不写主观画像**）。
- 是否达到放量/继续观察条件：`<是/否>`（依据《OPS_RUNBOOK §2 上线阈值》）。

---
*（模板结束；复制为 `BUSINESS_METRICS_REPORT-<period>.md` 后删除本行说明。配套《DAILY_OBSERVATION》模板见同目录。）*
