# Shadow → Live 评审记录（SHADOW TO LIVE REVIEW）

> **地位**：本模板用于记录 **shadow 期**（`EXECUTION_MODE=shadow`，不上真实资金）取得的**可量化数据**，并据此给出
> **切 live 的前置条件判定与审批结论**。对应 `SHADOW_TO_LIVE_GATE.md` S2（人工复核）/ S4（业务负责人确认）/ S5（切 live）；
> 与 `/evidence/prod-go-live/release-manager/GO_NO_GO.md` G14（7 天观察）、`CANARY_TENANT_CONFIRMATION_TEMPLATE.md`（书面确认）衔接。
> **命名**：`SHADOW_TO_LIVE_REVIEW-<租户ID>-<ts>.md`。
> **重要**：本模板**只作评审记录**，**不实际切换 live**；实际切换须部署/运维在满足全部前置 + 业务负责人书面确认后执行，全程可回退。
> **数据来源**：shadow 期数据取自 `deploy/observability/DAILY_CHECKS_RUNBOOK.md`、`deploy/observability/BUSINESS_METRICS_REPORT-TEMPLATE.md`、
> `deploy/records/7DAY_ACCEPTANCE_TRACKER-<ts>.md`、`GET /api/metrics`、`GET /api/audit`、`evidence/llm_candidate_eval.json`、
> `evidence/llm_fallback_to_human.json`、`evidence/COMPENSATION_RECONCILIATION_REPORT-<ts>.md`。

---

## 0. 记录标识

| 字段 | 值 |
|------|-----|
| 评审记录 ID | `S2L-<租户ID>-<ts>` |
| 租户 / 环境 | `<TENANT-A>` / `preview` / `production` |
| shadow 观察窗口 | `<YYYY-MM-DD ~ YYYY-MM-DD>`（≥7 天） |
| 执行模式 | `EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`（**未切 live**） |
| 评审人 / 复核人 | `<release-manager> / <security-auditor>` |
| 数据来源 | 见上文「数据来源」——所有值必须**可追溯到观测/证据**，不得凭空填写 |

## 1. Shadow 期数据（可量化，须有来源）

| 维度 | 定义 / 公式（来源） | Shadow 期值 | 目标 | 是否达标 |
|------|--------------------|-------------|------|:---:|
| **退款建议准确率** | shadow 期抽样退款建议 vs 真实/标准答案正确率（来源：`evidence/llm_candidate_eval.json`（intent/params/faithfulness 用例）+ shadow 日采样的建议正确率） | `<x%>` | ≥`<阈值>` | ⬜ |
| **人工介入率** | `sum(rate(human_intervention_total[1h])) / clamp_min(sum(rate(api_requests_total[1h])),0.001)`（`GET /api/metrics`） | `<x%>` | ≤20% | ⬜ |
| **模型拒答率** | 非白名单写操作转人工 / 总写需求；与 `evidence/llm_fallback_to_human.json`（所有转人工 `operation_id=None`、`approval=None`） | `<x%>` | ≤`<基线>` | ⬜ |
| **对账一致** | `reconcile_mismatch_total` 增量为 0；`execution_outcome_total{status="mismatched"}`=0；对账 scanned/reconciled（`COMPENSATION_RECONCILIATION_REPORT`） | `<mismatch=0>` | mismatch=0 | ⬜ |
| **审批正确率** | 审批决策 approved/rejected 中符合政策比例（`GET /api/audit?target_type=approval`） | `<x%>` | 越权=0 | ⬜ |
| **执行结果分布** | `execution_outcome_total{mode,status}`（shadow 默认） | `<...>` | — | ⬜ |

> 补强说明：若某维度**当前无可计量指标**（如真实模型拒答数），须**如实标注「待补强/待观测」**，不得虚设（`OBSERVABILITY_VERIFICATION.md` §二 补强项）。

## 2. 关键合规/安全观察（shadow 期，全绿才可切 live）

| 观察点 | 途径 | 结果 |
|--------|------|:---:|
| 无审批绕过（唯一 human_approval） | `GET /api/audit`（approval 四维）+ shadow 无 approved 即执行 | ⬜ |
| 无跨租户访问被放行 | `security_denials_total{kind=~"cross_tenant\|..."}` + 审计 | ⬜ |
| 无重复执行/重复资金副作用 | 同一 `operation_id` 恰 1 次执行（幂等核对） | ⬜ |
| 无未审计写 | 每条敏感/拒绝路径 `append_audit` | ⬜ |
| 可对账（非终态收口、mismatch 转人工） | 对账任务运行 + mismatch 计数 | ⬜ |
| 备份恢复演练达标 | `deploy/drills/record_template.md`（RPO≤15min/RTO≤60min） | ⬜ |
| 观测链路就绪 | Langfuse trace 已带 `tenant_id/session_id/environment` + PII 脱敏；`/api/metrics` 内网化；Loki 采集 | ⬜ |

## 3. Live 切换前置条件（全部满足才可切）

按 `SHADOW_TO_LIVE_GATE.md` S0–S5 + `src/core/launch_gate.py`：

| # | 前置条件 | 判定 | 依据/证据 |
|---|----------|:---:|----------|
| 1 | 至少 7 天 shadow 连续观察无红线命中 | ⬜ | `7DAY_ACCEPTANCE_TRACKER-<ts>.md`（全绿） |
| 2 | 真实租户**书面确认**（业务方签署） | ⬜ | `CANARY_CONF-<租户ID>-<ts>.md` |
| 3 | 放量审批单：同租户 `admin`/`approver` 二次确认 + 审计留痕 | ⬜ | `SCALEUP_APPROVAL-<ts>.md` |
| 4 | `EXECUTION_MODE=live` + **真实受控 provider**（非 mock；缺 provider fail-closed） | ⬜ | `deploy/.env.<env>`、`verify_launch_gate --strict` |
| 5 | `LAUNCH_ALLOWED_TENANTS=<已确认租户>`（仅放行白名单；非名单 403 + 审计） | ⬜ | `launch_allowlist` |
| 6 | `HIGH_CONFIDENCE_MODELS=<评测 write_op_pass=true 模型>` | ⬜ | `resolve_high_confidence_models` + `llm_candidate_eval.json` |
| 7 | 写操作仍须唯一 `human_approval`；`LAUNCH_REQUIRE_APPROVAL/LAUNCH_FULL_AUDIT/LAUNCH_MANUAL_REVIEW` = true | ⬜ | `launch_gate.py` |
| 8 | `LAUNCH_GATE_STRICT=true`；`verify_launch_gate --strict` 0 违规；`healthcheck.sh` 4 项全过 | ⬜ | `scripts/verify_launch_gate.py`、`deploy/scripts/healthcheck.sh` |
| 9 | 具备回滚能力（变更前备份 + `rollback.sh`） | ⬜ | `deploy/scripts/rollback.sh`、`DR_KEY_MANAGEMENT.md` |
| 10 | 切 live 冒烟通过（会话→退款→审批→执行，无重复/无绕过/可对账） | ⬜ | 切 live 后冒烟记录 |

> **注意**：条件 2/3（真实租户书面确认 + 放量审批单）目前为**外部输入**（`GO_NO_GO.md` G13=BLOCKED-需业务方），
> 未满足则**不得切 live**（`SHADOW_TO_LIVE_GATE.md` §5）。

## 4. 审批结论

| 评审项 | 结论 |
|--------|------|
| Shadow 期数据是否达到切 live 前目标 | `<是/否>` |
| 关键合规/安全观察是否全绿 | `<是/否>` |
| Live 切换前置条件是否全部满足 | `<是/否>`（条件 2/3 未满足则「否」） |
| **评审结论** | `<APPROVE / HOLD / NO-GO>` —— APPROVE 仅当 1–10 全满足且业务方书面确认；否则 HOLD（待外部输入）或 NO-GO（有红线命中） |
| 备注 | `<若 HOLD：列出未满足项与缺失的外部输入；若 NO-GO：列出触发红线>` |

## 5. 签名

- 评审人（release-manager）：`<签名>` ／ 复核人（security-auditor）：`<签名>` ／ 业务负责人（书面确认）：`<签名>`
- 数据全部可追溯：**任何未实测/缺指标的值不得填写**，如实标注「待观测/待补强」。

---
*（模板结束；复制为 `SHADOW_TO_LIVE_REVIEW-<租户ID>-<ts>.md` 后填写并删除本行说明。不实际执行 live 切换。）*
