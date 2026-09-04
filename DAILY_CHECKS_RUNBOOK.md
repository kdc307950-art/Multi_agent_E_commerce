# 每日检查 Runbook（DAILY CHECKS RUNBOOK）

> **地位**：本文件是「电商售后多智能体工单系统」**每日例行可观测检查的权威手册**，覆盖 8 项独立检查。
> 配套：《OPS_RUNBOOK.md》（上线阈值/回滚/值班流程）、《代理宪法》（第一层安全红线 + 第二层业务宪法）、
> `deploy/observability/alert-rules.yml`（权威告警规则）、《生产环境架构设计》§4。
> 一致性副本（运维侧）：`deploy/observability/DAILY_CHECKS_RUNBOOK.md`（与本文内容保持一致，改一处必改两处）。
>
> **使用**：每天按 §0 顺序执行；每个检查给出「检测手段 / 阈值 / 处置」。达标项记录到 `DAILY_OBSERVATION-<date>.md`；
> 周期指标汇总到《业务指标报告》。任何一项触发「处置=升级/回滚」阈值，按 `OPS_RUNBOOK §2/§3/§4` 处理。

---

## §0. 每日检查执行序（建议日切后 08:30，按一二三四五分段执行）

| 顺序 | 检查项 | 检查对象 | 耗时 |
|-----|--------|---------|------|
| ① | 数据面与隔离（§1 租户越权 / §3 重复 operation） | PostgreSQL RLS 数据 + 审计 | 10min |
| ② | 资金/审批链路（§2 审批异常 / §8 失败补偿） | approvals / executions | 10min |
| ③ | 回调与对账（§4 回调 mismatch / 重复/对账） | gateway_txns / executions | 10min |
| ④ | 观测链路（§5 延迟 / §6 人工介入 / §7 模型拒答） | Langfuse / Prometheus / Loki | 10min |
| ⑤ | 汇总留痕 | 写 `DAILY_OBSERVATION-<date>.md` + 指标报告 | 5min |

> **统一红线**（任何检查都遵守）：
> - **绝不把 `tenant_id`/`user_id`/`thread_id`/`order_id`/`operation_id` 等作为 Prometheus 标签**（`metrics.py::_FORBIDDEN_LABELS` 会抛错）；租户/会话明细一律走 `GET /api/audit` 受控查询或 Loki（已脱敏）。
> - **只写客观事实**，不写主观标签；阈值以真实演练/实测校准，未经实测不得宣称达标。
> - **敏感写异常一律转人工**，绝不自行用低可靠模型补齐；对账不一致/不可核实→转人工，绝不静默。

---

## §1. 检查一：租户越权 / 跨租户 / 越权访问（安全红线，最高优先级）

**为什么查**：`tenant_id` 只能来自服务端认证上下文；跨租户访问默认拒绝。若出现越权/跨租户，即为合规红线，
必须第一时间发现（`安全红线 7`、`业务宪法 §3.5/§3.6`）。

**检测手段（三者结合）**

1. **Prometheus 指标 / 告警表达式**
   ```promql
   # 跨租户/越权拒绝 —— 任一发生即 critical（increase 保证窗口内无新事件即恢复可关闭）
   increase(security_denials_total{kind=~"cross_tenant|cross_user_|forbidden|role_mismatch|access_denied"}[24h]) > 0
   ```
   ```promql
   # 安全拒绝短窗口激增
   increase(security_denials_total[5m]) > 5
   ```
   `security_denials_total{kind}` 已接入：`src/auth/security.py::audit_security_denial`（kind=审计拒绝 reason，有界）。

2. **PostgreSQL 数据面 SQL**（在受控审计库执行，需 RLS 上下文）
   ```sql
   -- ① 任一租户内出现的越权/跨租户拒绝审计（过去 24h）
   SELECT tenant_id, user_id, action, target_type, target_id, created_at
   FROM audit
   WHERE created_at > extract(epoch from now()) - 86400
     AND action IN ('audit.security_denial','security_denial','approval.access_denied','forbidden')
   ORDER BY created_at DESC
   LIMIT 100;

   -- ② 成员关系异常：审批主体对目标会话无合法成员归属（租户 RBAC 先于工具授权）
   SELECT a.tenant_id, a.approver, a.operation_id, a.created_at
   FROM approvals a
   LEFT JOIN memberships m
     ON m.tenant_id = a.tenant_id AND m.user_id = a.approver
   WHERE a.status = 'approved'
     AND (m.user_id IS NULL OR m.status <> 'active' OR m.role NOT IN ('approver','admin'));

   -- ③ 会话归属校验失败（应被拒）：目标会话与操作不属于同一租户
   SELECT s.tenant_id AS session_tenant, o.tenant_id AS op_tenant,
          s.thread_id, o.operation_id
   FROM operations o
   JOIN sessions s ON s.thread_id = o.thread_id
   WHERE s.tenant_id <> o.tenant_id;
   ```

3. **Loki 日志查询**（应用日志已全局脱敏）
   ```logql
   {service="api"} |~ "security_denial|cross_tenant|cross_user_|forbidden|role_mismatch|access_denied"
   ```

**阈值**
| 信号 | 阈值 | 级别 |
|------|------|------|
| `security_denials_total{kind=~"cross_tenant\|..."}` 24h 增量 | `> 0` | 🔴 critical |
| `security_denials_total[5m]` 增量 | `> 5` | 🟠 warning |
| 审批主体成员归属异常 / 会话租户不一致 SQL 命中 | `> 0` | 🔴 critical |

**处置**
- 🔴 critical：立即止损（停相关租户白名单 / `LAUNCH_ALLOWED_TENANTS`），按 `OPS_RUNBOOK §4` 值班红线处理。
- 越权/跨租户一律走 `platform_admin` **独立流程 + 二次确认 + 不可抵赖审计**，不得用租户普通角色处理。
- 核对 `GET /api/audit?target_type=...` 四维追溯（tenant/session/approval/operation）。
- 复盘更新告警阈值与审计口径；绝不在审计外留痕。

---

## §2. 检查二：审批异常（审批失败/绑定不一致/挂起/超时/跨租户审批）

**为什么查**：退款/退货/改址必须经唯一 `human_approval`；审批决定唯一锚定 approval 记录的
`operation_id/thread_id/pending_action`。审批异常直接关系资金安全与租户归属（`业务宪法 §3`）。

**检测手段**
1. **指标 / 告警表达式**
   ```promql
   # 审批失败率（拒绝/异常/超时/越权 占比）
   sum(rate(approval_decisions_total{status=~"rejected|error|timeout|forbidden"}[24h]))
     / clamp_min(sum(rate(approval_decisions_total[24h])), 0.001) > 0.05
   ```
   ```promql
   # 审批耗时 p95
   histogram_quantile(0.95, sum(rate(approval_decision_latency_seconds_bucket[1h])) by (le)) > 30
   ```
   `approval_decisions_total{route,status}`：`status ∈ approved|rejected|timeout|replay|error|forbidden`（已接入 `routes.py`）。

2. **PostgreSQL SQL**
   ```sql
   -- ① 挂起审批超阈值（应被人工/超时收口）
   SELECT tenant_id, approval_id, operation_id, status, created_at
   FROM approvals
   WHERE status = 'pending' AND created_at < extract(epoch from now()) - 86400;

   -- ② 绑定不一致（operation_id/thread_id/pending_action 与审批锚定不符）
   SELECT a.approval_id, a.tenant_id, a.operation_id, a.pending_action, o.pending_action AS op_action,
          o.thread_id AS op_thread, a.thread_id AS appr_thread
   FROM approvals a JOIN operations o ON o.operation_id = a.operation_id
   WHERE a.pending_action <> o.pending_action OR a.thread_id <> o.thread_id;

   -- ③ 审批越权：审批主体角色非法（见 §1.② 同款，此处聚焦 approver 维度）
   ```

3. **Loki**
   ```logql
   {service="api"} |~ "approval|binding_mismatch|APPROVAL_BINDING_MISMATCH|approval_timeout"
   ```

**阈值**
| 信号 | 阈值 | 级别 |
|------|------|------|
| 审批失败率 | `> 5%` | 🔴 critical |
| 审批 p95 延迟 | `> 30s` | 🟠 warning |
| 绑定不一致 / 越权审批 | `> 0` | 🔴 critical |
| 挂起审批 > 24h | `> 0` | 🟠 warning |

**处置**
- 绑定不一致：立即止损并核对审批单与操作锚定；拒绝非法绑定（`APPROVAL_BINDING_MISMATCH` 返回 422），**绝不静默恢复**。
- 审批超时 → 已转人工（`human_intervention_total{kind="approval_timeout"}`），人工复核，不自动放行。
- 跨租户审批 → 统一拒绝并留痕；只允许租户内 `admin/approver` 审批。
- 复核 `GET /api/audit?target_type=approval&target_id=...`。

---

## §3. 检查三：重复 operation（幂等破坏 / 重复业务副作用）

**为什么查**：同一资金操作永不重复执行。幂等锚定 `operation_id` 与 `UNIQUE(tenant_id, idempotency_key)`。
一旦出现重复 operation 或同一操作被执行两次，属**立即停止并回滚**信号（`OPS_RUNBOOK §3 条件 3`）。

**检测手段**
1. **指标 / 告警表达式**
   ```promql
   # 审批重放（重复决策/重复提交）—— 出现即异常
   sum(rate(approval_decisions_total{status="replay"}[24h])) > 0
   ```
   ```promql
   # 同一操作被多次执行提交（重放幂等时不应重复计 —— 计数只对新记录 inc）
   sum(rate(execution_submit_total[5m])) > 0
   ```
   > 说明：`execution_submit_total` 仅对新记录 inc，重放不重复计；若其短窗口速率异常升高，说明出现重复提交路径。

2. **PostgreSQL SQL**
   ```sql
   -- ① 幂等键重复（应被 UNIQUE(tenant_id,idempotency_key) 阻止；命中说明有绕过或脏数据）
   SELECT tenant_id, idempotency_key, count(*) AS cnt
   FROM operations
   GROUP BY tenant_id, idempotency_key
   HAVING count(*) > 1;

   -- ② 同一操作被多条执行记录引用（每操作应恰一条执行）
   SELECT tenant_id, operation_id, count(*) AS cnt
   FROM executions
   GROUP BY tenant_id, operation_id
   HAVING count(*) > 1;

   -- ③ 已执行但缺少已批准审批的 operation（绕过 human_approval 的嫌疑）
   SELECT o.tenant_id, o.operation_id, o.status
   FROM operations o
   WHERE o.status IN ('executed','pending')
     AND NOT EXISTS (
        SELECT 1 FROM approvals a
        WHERE a.operation_id = o.operation_id AND a.status = 'approved'
     );

   -- ④ 同一 client_request_id 生成多个 stream（start 原子去重应唯一）
   SELECT tenant_id, user_id, client_request_id, count(*) AS cnt
   FROM streams
   GROUP BY tenant_id, user_id, client_request_id
   HAVING count(*) > 1;
   ```

3. **Loki**
   ```logql
   {service="api"} |~ "replay|duplicate|idempot|already_exist|UNIQUE|operation_id"
   ```

**阈值**
| 信号 | 阈值 | 级别 |
|------|------|------|
| 幂等键/执行记录/stream 重复 | `> 0` | 🔴 critical |
| 已执行但无已批准审批 | `> 0` | 🔴 critical（绕过 HITL） |
| 审批重放（replay） | `> 0` | 🟠 warning（幂等重放允许，但需确认未重执行） |

**处置**
- 🔴 critical：**立即停止并回滚**（`OPS_RUNBOOK §3` 回滚动作）；重复业务副作用＝回滚触发信号。
- 幂等以 `UNIQUE` + `INSERT ... ON CONFLICT DO NOTHING` 或事务内「检查+写入」保证；确认未发生第二次执行。
- 重试/重放只**重放既有结果**，绝不重新执行。复核 `GET /api/audit?target_type=operation`。

---

## §4. 检查四：回调 mismatch（回调防伪/核验不一致）

**为什么查**：回调以 `(execution_id, nonce)` 做 CAS 原子应用；`apply_callback_atomic` 在同一事务完成，
杜绝「记账成功但状态未跃迁」。回调核验不一致（金额/签名/nonce/终态锁定）暴露资金记录与外部回执不一致或伪造回调。

**检测手段**
1. **指标 / 告警表达式**
   ```promql
   # 回调异常（签名/金额/nonce/非法跃迁/终态锁定/找不到）
   increase(execution_callback_total{reason=~"signature_invalid|amount_mismatch|missing_nonce|illegal_transition|terminal_locked|not_found|processing|bad_payload"}[24h]) > 0
   ```
   ```promql
   # 对账后仍处 mismatch 的执行（收口失败，需人工）
   sum(execution_outcome_total{status="mismatched"}) > 0
   ```
   `execution_callback_total{reason}` 已接入（`routes.py`，reason=CallBackResult.reason，有界）。

2. **PostgreSQL SQL**
   ```sql
   -- ① 执行记录与沙箱网关回执金额不一致
   SELECT e.execution_id, e.tenant_id, e.amount, g.amount AS gw_amount, e.status
   FROM executions e
   LEFT JOIN gateway_txns g ON g.tenant_id = e.tenant_id AND g.operation_id = e.operation_id
   WHERE e.amount IS NOT NULL AND g.amount IS NOT NULL
     AND abs(e.amount - g.amount) > 0.001;

   -- ② 孤儿外部交易号（有外部回执但无对应执行，或反之）
   SELECT g.tenant_id, g.external_txn_id, g.operation_id, e.external_txn_id AS exec_txn
   FROM gateway_txns g LEFT JOIN executions e ON e.external_txn_id = g.external_txn_id
   WHERE e.external_txn_id IS NULL;

   -- ③ 已确认但外部态非成功（对账冲突）
   SELECT e.execution_id, e.operation_id, e.status, e.external_txn_id
   FROM executions e WHERE e.status = 'mismatched';

   -- ④ 补偿发起但没有对应 reversal 记录
   SELECT r.execution_id, r.tenant_id FROM executions e
   LEFT JOIN gateway_reversals r
     ON r.tenant_id = e.tenant_id AND r.execution_id = e.execution_id
   WHERE e.status = 'compensated' AND r.reversal_id IS NULL;
   ```

3. **Loki**
   ```logql
   {service="api"} |~ "callback|mismatch|signature_invalid|amount_mismatch|nonce|terminal_locked"
   ```

**阈值**
| 信号 | 阈值 | 级别 |
|------|------|------|
| 回调 reason 异常（签名/金额/nonce/非法跃迁） | `> 0` | 🔴 critical（疑似伪造/篡改） |
| 执行状态 = mismatched | `> 0` | 🔴 critical（对账冲突） |
| 金额不一致 / 孤儿交易号 | `> 0` | 🔴 critical |

**处置**
- 签名/金额/nonce 异常：疑似伪造回调，立即止损、隔离外部回执来源、核对 HMAC 密钥与网关日志。
- 对账冲突（mismatched）：按 `业务宪法 §4.2` —— 无法核实→转人工；保留 `operation_id` + 完整状态 + 通知人工 + 审计，绝不静默。
- 补偿发起但无 reversal：补齐补偿/人工对账，避免资金悬空。

---

## §5. 检查五：延迟（端到端 / 审批 / 图执行）

**为什么查**：实时对话路径要求 <10s 响应预算（快速失败 + 降级）；写操作到审批链路延迟关乎资金体验与超时收口。

**检测手段**
1. **指标 / 告警表达式**
   ```promql
   # 审批决策延迟 p95
   histogram_quantile(0.95, sum(rate(approval_decision_latency_seconds_bucket[1h])) by (le)) > 30
   ```
   > HTTP/图执行延迟无独立直方图指标（`api_requests_total` 是 counter）；**延迟明细走 Langfuse trace**
   > （按 `tenant_id`/`session_id` 检索 span 时长）与 Loki。
2. **Langfuse**：按 `metadata.tenant_id` / `metadata.session_id` / `environment` 过滤 trace，
   检查 `chat.run`、`approval.decision`、`execution.*` span 的 duration，找出 p95 超预算的链路。
3. **PostgreSQL SQL（会话/流异常）**
   ```sql
   -- 长活会话或流过期滞留（可能拖慢链路）
   SELECT tenant_id, thread_id, status, created_at, expires_at
   FROM sessions WHERE status = 'active' AND expires_at < extract(epoch from now());
   ```
4. **Loki**
   ```logql
   {service="api"} |~ "ReadTimeout|timeout|deadline|slow|>5000ms"
   ```

**阈值**
| 信号 | 阈值 | 级别 |
|------|------|------|
| 审批 p95 延迟 | `> 30s` | 🟠 warning |
| `/api/chat` 端到端（Langfuse） | `> 10s`（<10s 预算） | 🟠 warning |
| 大量超时/重试（Loki） | 短窗口激增 | 🟠 warning |

**处置**
- 按 `OPS_RUNBOOK §4.4` 排查 Langfuse trace + Loki + `GET /api/audit`；定位是模型、外部网关、还是数据面。
- **降级仅用于政策/查询类**（收工具→缩短上下文→RAG-only→带完整状态转人工）；**资金/敏感写绝不降级执行**，一律带完整状态转人工。
- 短超时（deadline=10/idle=3）+ 快速失败 + 降级；后台批处理用保守重试 + 队列 + 死信转人工。

---

## §6. 检查六：人工介入率（Human Intervention Rate）

**为什么查**：人工介入是 fail-closed 的兜底，但过高说明模型档位不足 / 检索不过 / 金额/资格异常 / 写操作门控过严，
需定位并优化（`业务宪法 §1 总纲`）。

**检测手段**
1. **指标 / 告警表达式**
   ```promql
   # 人工介入率（24h）
   sum(rate(human_intervention_total[1h]))
     / clamp_min(sum(rate(api_requests_total[1h])), 0.001) > 0.20
   ```
   `human_intervention_total{kind}`：`kind ∈ order_deny|approval_timeout|execution_handoff|reconcile_mismatch|escalate|...`（已接入）。
2. **PostgreSQL SQL**
   ```sql
   -- 转人工的 operation / execution
   SELECT tenant_id, operation_id, status, created_at
   FROM operations WHERE status = 'human_handoff' AND created_at > extract(epoch from now()) - 86400;
   SELECT tenant_id, operation_id, status, last_error
   FROM executions WHERE status = 'human_handoff' AND updated_at > extract(epoch from now()) - 86400;
   ```
3. **Loki**
   ```logql
   {service="api"} |~ "human_handoff|escalate|order_deny|approval_timeout|reconcile_mismatch|execution_handoff"
   ```

**阈值**
| 信号 | 阈值 | 级别 |
|------|------|------|
| 人工介入率 | `> 20%` | 🟠 warning |
| 介入率持续上升趋势 | 日环比 > 50% | 🟠 warning |

**处置**
- 按 kind 归因：order_deny=资格/金额异常；approval_timeout=审批未及时处理；reconcile_mismatch=对账冲突；
  escalate=政策模糊/检索不过。逐类核对 `GET /api/audit?target_type=operation` 与 RAG/能力矩阵。
- **优化方向**：补足已验证知识/政策、校准模型白名单（`HIGH_CONFIDENCE_MODELS`）——但写操作仍必须经唯一审批，**不得**为了降低介入率而用不可靠路径放行。

---

## §7. 检查七：模型拒答率（能力矩阵 fail-closed / 拒绝写操作 / 幻觉失败转人工）

**为什么查**：写风险工具只允许 `HIGH_CONFIDENCE_MODELS` 白名单模型调用；名单外模型一律拒绝写并转人工。
模型拒答/降级是安全兜底，但若**异常升高**说明白名单配置错误、模型档位不足或幻觉防线过严（`代理宪法 第一层 §2.1 + 第二层 §5`）。

**检测手段**
1. **指标 / 告警表达式（当前无专用计数器 → 用人工介入 + 无审批写操作信号）**
   ```promql
   # ① 模型拒/写操作门控失败 → 通常落为 human_handoff / falls_to_error
   sum(rate(human_intervention_total{kind=~"capability_gate|execution_handoff|order_deny"}[24h])) > 0
   # ② 直接用于核对"非白名单模型触发写需求"：看 execution_outcome_total 中 human_handoff/失败
   sum(rate(execution_outcome_total{status=~"human_handoff|failed_dispatch|compensation_failed"}[24h])) > 0
   ```
   > **补强建议**：新增有界计数器 `capability_gate_total{result}`（`result=blocked|allowed`）与
   > `model_refusal_total{kind}`（`kind=write_not_whitelist|hallucination_fail|low_confidence`），
   > 在 `src/graph/nodes.py` 的写操作门控失败处与 `BaseLLM.capability_ok=False` 处上报，落地后替换上面第 ① 条。

2. **PostgreSQL SQL**
   ```sql
   -- 写操作发起但无已批准审批（能力矩阵拒写 → 本应转人工，若出现"未转人工"即为故障）
   SELECT o.tenant_id, o.operation_id, o.status, o.created_at
   FROM operations o
   WHERE o.pending_action IN ('refund','return_request','return_address')
     AND NOT EXISTS (SELECT 1 FROM approvals a
                     WHERE a.operation_id = o.operation_id AND a.status='approved');
   ```
3. **证据核对**（只读）：
   - `evidence/llm_candidate_eval.json`：`self-hosted-model.write_op_pass / passed` （24 用例全部 passed）。
   - `evidence/llm_endpoint_connectivity.json`：`timeout/retry/invalid_json/unreachable` 均 `failure_closed=true`。
   - `evidence/llm_fallback_to_human.json`：所有转人工路径 `operation_id=None` 且 `approval=None`（不误创建资金操作）。
4. **Loki**
   ```logql
   {service="api"} |~ "capability|write_op|fallback_to_human|not_in_whitelist|high_confidence|hallucination|fail_closed"
   ```

**阈值**
| 信号 | 阈值 | 级别 |
|------|------|------|
| 非白名单模型写操作未转人工（SQL 命中） | `> 0` | 🔴 critical（绕过能力矩阵） |
| 模型拒答/转人工短窗口激增 | 速率 > 基线 | 🟠 warning |
| 证据报告含 `passed=false` / `failure_closed=false` | `> 0` | 🔴 critical |

**处置**
- 🔴 critical（非白名单模型被放行写）：立即止损 + 回滚；核对 `HIGH_CONFIDENCE_MODELS` 配置与评测报告（`resolve_high_confidence_models`）。
- 模型拒答/降级升高：核对白名单与模型档位；**写操作绝不降级用便宜模型补齐**，一律带完整状态转人工。
- 幻觉/相关性失败：fail-closed 经 `handle_error` 转人工，不把未验证草稿改名为 RAG-only 继续输出。

---

## §8. 检查八：失败补偿（dispatch 失败 / 补偿失败 / 对账后补偿）

**为什么查**：执行面失败必须补偿（回滚）或转人工；补偿失败/逾期未补偿意味着资金可能悬空，属写操作安全回退矩阵触发信号（`业务宪法 §5.3`）。

**检测手段**
1. **指标 / 告警表达式**
   ```promql
   # 补偿失败 / dispatch 失败 / 结果不确定（任一短窗口 > 0 即告）
   increase(execution_outcome_total{status=~"compensation_failed|failed_dispatch|failed_uncertain"}[24h]) > 0
   ```
   ```promql
   # 长期处于 compensating（补偿中未收口）
   sum(execution_outcome_total{status=~"compensating|reconciling"}) > 0
   ```
   `execution_outcome_total{mode,status}`：`status=ExecutionStatus`（有界：pending_submit|submitted|confirmed|failed_dispatch|failed_uncertain|compensating|compensated|compensation_failed|reconciling|reconciled|mismatched|human_handoff）。

2. **PostgreSQL SQL**
   ```sql
   -- ① 补偿失败需人工处置
   SELECT execution_id, tenant_id, operation_id, status, compensation_status, last_error, updated_at
   FROM executions
   WHERE status IN ('compensation_failed','failed_uncertain') OR compensation_status = 'failed';

   -- ② 逾期未确认的执行（对账后仍未收口）
   SELECT operation_id, tenant_id, external_txn_id, submitted_at, confirmed_at, status
   FROM executions
   WHERE status IN ('submitted','pending_submit','failed_dispatch')
     AND submitted_at < extract(epoch from now()) - 86400;

   -- ③ 补偿回执缺失（compensated 但无 compensation_result / gateway_reversals 无记录）
   SELECT execution_id, operation_id, status, compensation_result
   FROM executions WHERE status='compensated' AND (compensation_result IS NULL
     OR NOT EXISTS (SELECT 1 FROM gateway_reversals r WHERE r.execution_id = executions.execution_id));
   ```
3. **Loki**
   ```logql
   {service="api"} |~ "compensat|compensation_failed|dispatch_failure|reconcile_mismatch|failed_uncertain"
   ```

**阈值**
| 信号 | 阈值 | 级别 |
|------|------|------|
| `compensation_failed` / `failed_uncertain` | `> 0` | 🔴 critical |
| 逾期未确认（> 24h） | `> 0` | 🟠 warning |
| 补偿回执缺失 | `> 0` | 🔴 critical |

**处置**
- 补偿失败：按 `业务宪法 §5.3` **停止写入**，保留 `operation_id` 与完整状态，通告人工 + 审计；不得重试无界。
- 有界重试（指数退避 + jitter）；同类型错误不重复叠加。
- 确认补偿 `reversal_id` 存在（`gateway_reversals` 派生稳定 reversal_id，重放一致）。缺回执 → 人工对账补齐。

---

## §9. 观测链路核对（每日快检：可观测本身是否健康）

自托管可观测栈自身故障会导致「盲区」——每日快检确保监控不失效。运行：
```bash
bash deploy/scripts/observability.sh health
# 或抓取三点：
curl -s http://127.0.0.1:9090/-/healthy          # Prometheus
curl -s http://127.0.0.1:3002/health             # Langfuse（自托管）
curl -s http://127.0.0.1:3100/ready               # Loki
```
告警规则本身健康：`promtool check rules /etc/prometheus/alert-rules.yml`（或等价 `eval_alert_rules.py`）。

详见 `deploy/observability/OBSERVABILITY_VERIFICATION.md`。

---

## §10. 关键告警表达式速查（重复 operation / 对账 mismatch / 失败补偿 —— 三大核心）

| 目标 | 告警表达式（可直接进 `alert-rules.yml`） | 阈值 |
|------|----------------------------------------|------|
| **重复 operation/审批重放** | `increase(approval_decisions_total{status="replay"}[24h]) > 0` | 出现即 critical |
| **幂等键重复（兜底审计）** | `sum by (tenant_id) (increase(execution_submit_total[24h])) > 0` *依赖速率核对* | 异常升高 warning |
| **对账 mismatch** | `increase(reconcile_mismatch_total[24h]) > 0`；`sum(execution_outcome_total{status="mismatched"}) > 0` | 出现即 critical |
| **对账任务失联** | `time() - reconcile_last_run_timestamp_seconds > 86400` | > 24h warning |
| **失败补偿** | `increase(execution_outcome_total{status=~"compensation_failed|failed_uncertain|failed_dispatch"}[24h]) > 0` | 出现即 critical |
| **回调 mismatch** | `increase(execution_callback_total{reason=~"signature_invalid|amount_mismatch|missing_nonce|terminal_locked|illegal_transition"}[24h]) > 0` | 出现即 critical |

> 口径：表达式标签用了**有界维度**（route/status/kind/mode/job），绝不带 `tenant_id` 等高基数标签；
> 计数用 `increase(...[窗口])` 与 `threshold`（保证窗口内无新事件即可恢复可关闭）；`for:` 需按 `alert-rules.yml` 惯例配置。
> 这些表达式为**初稿占位**，须以真实演练/实测校准后合入权威文件 `deploy/observability/alert-rules.yml`（**勿新增第二份规则文件**）。

---

*与《OPS_RUNBOOK》《代理宪法》《生产环境架构设计 §4》配套。所有阈值为初稿占位，未经实测不得对外宣称达标。*
