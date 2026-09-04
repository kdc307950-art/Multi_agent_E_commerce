# 每日观察记录（DAILY OBSERVATION）

> **命名**：`DAILY_OBSERVATION-<date>.md`，`<date>` 用 `YYYYMMDD`（示例：`DAILY_OBSERVATION-20260907.md`）。
> **地位**：每天按 `DAILY_CHECKS_RUNBOOK.md` §0 执行 8 项检查后，把可核实结果填入本模板，留存在
> `deploy/observability/` 或 `deploy/records/`。**只写客观事实**，不写主观画像；敏感数据（地址/支付/订单号）不入明文。
> **口径**：租户明细走受控审计（`GET /api/audit`），**绝不用高基数 `tenant_id` 作 Prometheus 标签**。

---

## 0. 元信息

| 字段 | 值 |
|------|-----|
| 日期 | `<YYYY-MM-DD>` |
| 环境 | `preview` / `production` |
| 执行人 | `<name>` |
| 复核人 | `<name>` |
| 观测栈健康 | `bash deploy/scripts/observability.sh health` → ✅/❌ |
| 备注 | `<summary>` |

## 1. 租户越权 / 跨租户 / 越权访问（最高优先级 🔴）

- 检测：`increase(security_denials_total{kind=~"cross_tenant|cross_user_|forbidden|role_mismatch|access_denied"}[24h])`
- 当日值：`<0 | N>`；SQL 命中（§1.② 成员归属异常 / §1.③ 会话租户不一致）：`<0 | N>`
- 结论：✅ 无 / ❌ 有（`<处置：止损+platform_admin 独立流程+二次确认+审计>`）
- 关联审计：`<audit_id / target_type / target_id>`

## 2. 审批异常

- 审批失败率（rejected|error|timeout|forbidden）：`<x%>`
- 审批 p95 延迟：`<x s>`（阈值 30s）
- 挂起审批 > 24h：`<N>`；绑定不一致：`<0 | N>`；越权审批：`<0 | N>`
- 结论：✅ / ❌（`<处置>`）

## 3. 重复 operation（幂等破坏）

- `approval_decisions_total{status="replay"}` 24h：`<N>`
- SQL：幂等键重复 / 执行记录重复 / 已执行无已批准审批 / stream 重复：`<0 | N>`
- 结论：✅ / ❌（`<处置：立即停止并回滚>`）

## 4. 回调 mismatch

- `execution_callback_total{reason=~"signature_invalid|amount_mismatch|missing_nonce|terminal_locked|illegal_transition"}` 24h：`<N>`
- `execution_outcome_total{status="mismatched"}`：`<N>`
- SQL：金额不一致 / 孤儿交易号 / 补偿无 reversal：`<0 | N>`
- 结论：✅ / ❌（`<处置：核对 HMAC/止损/转人工>`）

## 5. 延迟

- 审批 p95：`<x s>`（>30s warning）
- `/api/chat` 端到端（Langfuse `chat.run` span）：`<x s>`（预算 <10s）
- Loki 超时/重试：`<N>`
- 结论：✅ / ❌（`<处置：降级仅限政策/查询类；写操作不降级转人工>`）

## 6. 人工介入率

- `human_intervention_total` 1h 速率 / `api_requests_total` 1h 速率：`<x%>`（>20% warning）
- 按 kind 分解（order_deny / approval_timeout / execution_handoff / reconcile_mismatch / escalate）：`<...>`
- `operations.status='human_handoff'` 24h / `executions.status='human_handoff'` 24h：`<N>`
- 结论：✅ / ❌（`<处置：按 kind 归因，校准能力矩阵/知识>`）

## 7. 模型拒答率（能力矩阵 fail-closed）

- 非白名单模型写操作转人工：`<N>`；无已批准审批的写操作（SQL §7.2）：`<0 | N>`
- 证据核对：`llm_candidate_eval.json`（`write_op_pass/self-hosted-model`）、`llm_endpoint_connectivity.json`（`failure_closed`）、`llm_fallback_to_human.json`
- 结论：✅ / ❌（`<处置：核对 HIGH_CONFIDENCE_MODELS + 评测报告>`）

## 8. 失败补偿

- `execution_outcome_total{status=~"compensation_failed|failed_uncertain|failed_dispatch"}` 24h：`<N>`
- SQL：补偿失败 / 逾期未确认 / 补偿回执缺失：`<0 | N>`
- 结论：✅ / ❌（`<处置：停止写入+保留 operation_id+完整状态+通知人工+审计>`）

## 9. 告警命中汇总（当日）

| 告警 | 命中 | 处置 | 是否恢复 |
|------|------|------|----------|
| `<AlertName>` | 是/否 | `<...>` | ✅/❌ |

## 10. 异常与处置留痕（不可抵赖审计口径）

- 任何 ❌ 项均已：`<止损动作 + 转人工/回滚 + 审计记录 + 复盘>`；绝不在审计外留痕。

---
*（模板结束；复制到 `DAILY_OBSERVATION-<date>.md` 后删除本行说明。配套《业务指标报告》模板见同目录。）*
