# 7 天验收追踪表（7-DAY ACCEPTANCE TRACKER）

> **地位**：本表用于**真实租户连续运行 7 天**的每日验收勾选与处置闭环（对应 `SHADOW_TO_LIVE_GATE.md` S6「7 天连续观察」、
> `/evidence/prod-go-live/release-manager/GO_NO_GO.md` G14）。
> **命名**：`7DAY_ACCEPTANCE_TRACKER-<起止日期>.md`（示例：`7DAY_ACCEPTANCE_TRACKER-20260907-20260913.md`）。
> **每日执行**：某天的勾选卡见下方「Day N 勾选卡」，逐项填 **✅ 正常(绿) / ❌ 异常(红) / ⬜ 未观测**；每项填**检测依据**（指标/SQL/Loki/审计），
> 触发处置闭环（观察 → 定性 → 止损 → 排查 → 恢复/回滚 → 复核 → 复盘，见 `OPS_RUNBOOK §4`）。
>
> **数据来源**：全部取自 `deploy/observability/DAILY_CHECKS_RUNBOOK.md`（8 项检查检测手段/阈值/处置）、
> `deploy/observability/OBSERVABILITY_VERIFICATION.md`（可观测核对）、`GET /api/metrics`（Prometheus 指标）、
> `GET /api/audit`（受控审计四维追溯）、`deploy/observability/evidence/`（告警触发/定位/关闭）、每日备份恢复演练（`deploy/drills/record_template.md`）。
> **口径红线**：只写客观事实；租户明细走受控审计，**绝不用高基数 tenant_id 作 Prometheus 标签**；阈值以实测校准。

---

## 0. 记录标识

| 字段 | 值 |
|------|-----|
| 环境 | `preview` / `production` |
| 观察租户 | `<LAUNCH_ALLOWED_TENANTS 实测，如 TENANT-A / TENANT-B>` |
| 观察起止 | `<YYYY-MM-DD ~ YYYY-MM-DD>`（连续 ≥7 天） |
| 执行/复核人 | `<observability-engineer> / <release-manager>` |
| 执行模式 | `EXECUTION_MODE=shadow`（**未切 live**） |

## 1. 验收点总览（7 天必须全部 ✅）

| 验收点 | 含义 | 达标口径（每日 ∈ {绿, 红}） |
|--------|------|---------------------------|
| 无跨租户泄露 | 跨租户/越权访问被拒 | `security_denials_total{kind=~"cross_tenant\|..."}` 24h 增量 = 0 |
| 无重复资金提交 | 同一 operation 仅执行 1 次 | 幂等键/执行记录无重复；`approval_decisions_total{status="replay"}` 未造成重执行 |
| 无未审批执行 | 写操作必经唯一 human_approval | 无「已执行但无已批准审批」operation |
| 关键告警无未处理 | 各红线告警不命中，命中即闭环 | 见 §3 告警清单，命中项均已处置且恢复 |

## 2. 8 项每日检查（正常=绿，异常=红）

| 检查 | 检测依据（runbook 章节 + 指标/SQL/Loki） | 阈值 | 状态 |
|------|------------------------------------------|------|------|
| 租户越权 | `DAILY_CHECKS_RUNBOOK §1`：`security_denials_total{kind=~"cross_tenant\|..."}`；审计 `action IN (audit.security_denial, ...)` | 越权=0 | ⬜ |
| 审批异常 | `§2`：`approval_decisions_total{status=~"rejected|error|timeout|forbidden"}` 占比；p95；挂起审批 | 失败率≤5%，p95≤30s，越权=0 | ⬜ |
| 重复 operation | `§3`：幂等键/执行记录/stream 重复 SQL；`approval_decisions_total{status="replay"}` | 重复=0 | ⬜ |
| 回调 mismatch | `§4`：`execution_callback_total{reason=~"signature_invalid|amount_mismatch\|..."}`；mismatched | 异常=0 | ⬜ |
| 延迟 | `§5`：审批 p95；`/api/chat` 端到端（Langfuse `chat.run` span） | p95≤30s；端到端<10s | ⬜ |
| 人工介入率 | `§6`：`human_intervention_total` / `api_requests_total` | ≤20% | ⬜ |
| 模型拒答率 | `§7`：非白名单写操作转人工；无批准审批写操作；证据核对 | 绕过=0；拒答率≤基线 | ⬜ |
| 失败补偿 | `§8`：`execution_outcome_total{status=~"compensation_failed\|..."}`；补偿回执缺失 | 失败=0 | ⬜ |

---

## 3. Day 1 勾选卡（复制 Day 2–Day 7 逐日填写）

**日期**：`<YYYY-MM-DD>` ｜ **环境**：`preview/production` ｜ **执行人/复核人**：`<...>`

| # | 检查/验收点 | 状态 | 检测依据（实测值） | 处置闭环 |
|---|------------|:---:|-------------------|----------|
| 1 | 租户越权（无跨租户泄露） | ⬜ | `security_denials_total{kind=~"cross_tenant\|..."}`=`<0>`；审计命中=`<0>` | `<无/观察→定性→止损→排查→恢复→复核→复盘>` |
| 2 | 审批异常 | ⬜ | 失败率=`<x%>`；p95=`<x s>`；挂起>`<N>`；越权审批=`<0>` | `<...>` |
| 3 | 重复 operation（无重复资金提交） | ⬜ | 幂等键/执行记录重复=`<0>`；replay=`<N>` | `<...>` |
| 4 | 回调 mismatch | ⬜ | `execution_callback_total` 异常=`<0>`；mismatched=`<0>` | `<...>` |
| 5 | 延迟 | ⬜ | 审批 p95=`<x s>`；`/api/chat` 端到端=`<x s>` | `<...>` |
| 6 | 人工介入率 | ⬜ | `<x%>`（≤20%） | `<...>` |
| 7 | 模型拒答率 | ⬜ | 非白名单写转人工=`<N>`；无批准审批写=`<0>` | `<...>` |
| 8 | 失败补偿 | ⬜ | compensation_failed/failed_uncertain=`<0>` | `<...>` |
| 9 | 无未审批执行 | ⬜ | 无「已执行但无已批准审批」=`<是/否>` | `<...>` |
| 10 | 关键告警无未处理 | ⬜ | 命中=`<alert list>`；处置并关闭=`<是/否>` | `<...>` |

**当日结论**：`<7/7 绿 / 有异常>`；异常项已按 OPS_RUNBOOK §3 回滚或按 §4 值班闭环处理并留痕。当日签字：`<...>`

> ↓ 复制上方「Day N 勾选卡」填 Day 2–Day 7（每日一张），并记录每日备份恢复演练结果（`deploy/drills/record_template.md`，RPO≤15min/RTO≤60min）。

---

## 4. 7 天汇总（Day 1–Day 7）

| 天 | 租户越权 | 审批异常 | 重复operation | 回调mismatch | 延迟 | 人工介入率 | 模型拒答率 | 失败补偿 | 无跨租户 | 无重复资金 | 无未审批 | 关键告警未处理 |
|----|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| D1 | — | — | — | — | — | — | — | — | — | — | — | — |
| D2 | — | — | — | — | — | — | — | — | — | — | — | — |
| D3 | — | — | — | — | — | — | — | — | — | — | — | — |
| D4 | — | — | — | — | — | — | — | — | — | — | — | — |
| D5 | — | — | — | — | — | — | — | — | — | — | — | — |
| D6 | — | — | — | — | — | — | — | — | — | — | — | — |
| D7 | — | — | — | — | — | — | — | — | — | — | — | — |

**7 天结论**：`<是否满足 ≥7 天连续无跨租户/重复执行/审批绕过；错误率/人工介入率/对账 mismatch 在阈值内；每日恢复演练成功；无告警红线命中>`。

## 5. 验收结论（G14 门禁）

- 7 天验收：`<PASS / FAIL / BLOCKED>`（PASS 须 7 天全绿 + 关键告警无未处理 + 每日演练成功）。
- 是否满足进入 live 切换 / 汇总放量：`<是/否>`；依据 `SHADOW_TO_LIVE_GATE.md` S0–S6 与 `GO_NO_GO.md` G13/G14。
- 签名：执行 `observability-engineer` ／ 复核 `release-manager`。
- 数据来源：`deploy/observability/DAILY_CHECKS_RUNBOOK.md`、`deploy/observability/OBSERVABILITY_VERIFICATION.md`、`deploy/observability/evidence/`、`GET /api/metrics`、`GET /api/audit`。

---
*（模板结束；复制为 `7DAY_ACCEPTANCE_TRACKER-<起止日期>.md` 后逐日填写并删除本行说明。）*
