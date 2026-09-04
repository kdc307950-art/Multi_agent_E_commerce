# 回滚 / 暂停 / 人工接管机制（正式文档）

> **产出国角色**：release-manager（发布经理）· prod-go-live 团队 · 任务 t6
> **目标**：把生产上线的三类降级/接管机制——**回滚（Rollback）**、**暂停（Pause）**、**人工接管（Human Takeover）**——
> 落成**正式、可执行、可证据化**的运行文档；含**前条件、具体操作、证据化、审计留痕**，并落实"**资金异常立即关 live 转人工**"标准动作。
> **边界**：本任务**只产出机制文档**；实际回滚/暂停/接管动作须由运维在线上按本手册执行。
>
> 依据：`deploy/OPS_RUNBOOK.md` §3（回滚条件）/ §4（值班流程）；`deploy/scripts/rollback.sh`、`restore_drill.sh`、
> `backup_db.sh`、`backup_encrypted.sh`、`preview_backup_scheduler.sh`；`deploy/drills/records/TEMPLATE-rollback.md`、
> `TEMPLATE-pg-backup-restore.md`；`deploy/DR_KEY_MANAGEMENT.md`；`deploy/backups/rollback.log`；
> `deploy/records/PRODUCTION_ACCEPTANCE_VERIFICATION.md`；`deploy/EGRESS_POLICY.md`；`AGENTS.md`（Agent 宪法）。

---

## 0. 三类机制定位

| 机制 | 定义 | 触发场景 | 目标 |
|------|------|---------|------|
| **回滚（Rollback）** | 恢复到变更前快照（RPO）+ 应用版本回退（git ref），重建/重启 | 部署/迁移/资金副作用异常，需**回到已知良好状态** | 消除异常，恢复可用 |
| **暂停（Pause）** | 停止接收新请求/关闭上线闸门，**保留现场**，不销毁状态 | 观察期发现风险、需停流量处置 | 冻结流量、避免副作用扩大，为排查/接管留空间 |
| **人工接管（Human Takeover）** | 带**完整状态**转人工，人工决策/处置，系统只重放不重执行 | 金额异常/资格存疑/模型档位不足/检索不过/审批超时/补偿失败 | 把不确定交给人工，绝不静默当成功 |

---

## 1. 回滚（Rollback）

### 1.1 触发条件（`deploy/OPS_RUNBOOK.md` §3，满足任一即回滚）
1. `healthcheck.sh` 任一项失败（暴露内网端口 / HTTPS 异常 / CORS 泄漏 / fail-closed 失效）。
2. `drill_pg_backup_restore.sh` 的 RPO/RTO 超基线，或恢复后数据不完整。
3. 审批/执行/对账出现**重复业务副作用**（同一 `operation_id` 被执行两次）。
4. 任一租户"仅审批后执行"被绕过（未审批即执行）。
5. 指标异常（错误率/审批失败率/对账 mismatch/转人工率显著上升，告警触发）。
6. 审计追溯缺失（关键审计无法四维定位）。

### 1.2 前条件（回滚前必须满足）
- **回滚前快照（可逆性）**：先做加密备份（需要 `BACKUP_ENC_KEY`，见 `deploy/DR_KEY_MANAGEMENT.md`）。
  ```bash
  BACKUP_ENC_KEY=<注入> bash deploy/scripts/backup_db.sh deploy/backups/pre-rollback-$(date +%Y%m%d%H%M%S)
  ```
- 确认 `deploy/backups/rollback.log` 已记录本次部署引用：`deploy ref=<ref> ts=<ts> commit=<full> config_version=<cfg> build_ts=<ts> digests=<svc=digest,...>`。
- 确认目标**回滚 git ref**（可回退到的上一良好版本）。

### 1.3 操作
```bash
# 1) 恢复到变更前快照 + 应用版本回退
bash deploy/scripts/rollback.sh deploy/backups/langgraph-<ts>.dump <git-ref>
# 2) 健康复验
bash deploy/scripts/healthcheck.sh
# （可选，数据仅回滚不传 git-ref）
```
- 数据库向前迁移幂等（`CREATE IF NOT EXISTS`/`CREATE OR REPLACE`），故回滚 = 恢复数据（RPO）+ 应用版本回退；
  **不提供"部分回滚到中间 schema"**。
- 独立、可长期存档的 **DR 加密归档**走 `deploy/scripts/backup_encrypted.sh` / `restore_drill.sh`（`backup_role` 最小权限 + aes-256-cbc + SHA-256）。

### 1.4 证据化（复用）且复验项
- 回滚记录：`deploy/drills/records/DR-20260904030354-rollback.md`（+ `rollback-record.json`）——**t6 实跑通过**。
- 复验项（`TEMPLATE-rollback.md` §4）：`healthcheck.sh` PASS；数据一致性；审计四维可追溯；无重复副作用（幂等收敛）；租户白名单/审批归属正确。
- **注意**：回滚**必须实机执行并逐项复验**，不得把"设计好的回滚步骤"当"已回滚"实证。

### 1.5 密钥轮换（回滚伴随的安全动作）
- 见 `deploy/DR_KEY_MANAGEMENT.md` §3：任一字面量泄露/回滚异常 → **立即轮换** `BACKUP_ENC_KEY`（并存旧密钥于受控 vault），重打加密备份；`BACKUP_ROLE_PASSWORD` 同步轮换。建议最长 90 天轮换一次，每次轮换触发一次恢复演练。

---

## 2. 暂停（Pause）

### 2.1 目标与语义
- **停止接收新请求 / 关闭上线闸门**，但**保留现场与状态**（不做销毁、不回退版本），为处置留空间。
- 区别于回滚：暂停是**冻结**，回滚是**回到过去**。

### 2.2 操作（按层，从上游到下游）
1. **API 层（停止新请求）**：
   - 在反向代理/网关（nginx `/api/*`）或负载均衡侧**摘除后端**（`upstream` 置为不可用 / 返回 503），阻断新请求进入 `api:8000`。
   - 或把 `nginx` `listen 80/443` 指向维护页（`return 503` / 维护页），对外表现"暂停服务"。
2. **上线门控层（关闸）**：
   - 收紧 `LAUNCH_ALLOWED_TENANTS`（去掉目标租户）→ 非名单租户请求 403 + 审计。
   - 或置 `LAUNCH_GATE_STRICT=true` 并在受限环境校验 `verify_launch_gate.py --strict`。
3. **执行层（停止新执行）**：
   - 置 `EXECUTION_MODE=readonly`（或保持 shadow，阻断任意 live 写）、`EXECUTION_PROVIDER=mock`。
   - 停/隔离 Celery worker 以停止后台对账/补偿（`docker compose stop worker` 或 scale=0），避免后台继续执行。
   - 若涉及敏感写，确保 `human_approval` 未解锁新的执行。
4. **数据面（可读不可写/只读）**：需要时把运行角色 `app_runtime` 只保留 SELECT 或断网 `DEFERRABLE`/连接数限流（一般不做，除非要强一致冻结）。
5. **观测面**：保留 Prometheus/Loki/Langfuse 抓取，用于暂停期间取证。

### 2.3 前条件与证据
- **前条件**：暂停前**先做一次加密备份**（保证暂停期间的现场可恢复）。
- **证据**：
  - 暂停动作记录（改了哪些配置、谁、何时、为何）、`compose ps`/`nginx -t`、`verify_launch_gate --strict`。
  - 暂停后确认：新请求被拒（503/403）+ 审计 `security.deny.*` 或网关 503 留痕。
  - 现场保留：完整 `deploy/backups/pre-pause-<ts>.dump.enc` + 观测快照。

### 2.4 恢复（解除暂停）
- 反向恢复上述变更；确认 `verify_launch_gate --strict` 0 违规 + `healthcheck.sh` 全过 + 告警无异常后，按 `SHADOW_TO_LIVE_GATE.md` 重启受控流程（如需恢复 live 须重新走人工复核/对账/业务负责人确认）。

---

## 3. 人工接管（Human Takeover）——带完整状态转人工

### 3.1 目标与语义
- 把**不确定/异常**的请求**带完整状态转人工**，系统**只重放、不重执行**；人工做出决策/处置，全过程**审计留痕**。
- **绝不静默当成功**；**绝不用低可靠模型补齐**。

### 3.2 哪些场景触发转人工（fail-closed）
1. **模型档位不足/非白名单**：写风险工具触发但模型不在 `HIGH_CONFIDENCE_MODELS` → `model_not_in_whitelist`，`operation_id=null` 转人工。
2. **端点不可达/超时/5xx/非法输出/幻觉**：→ `LLMUnavailableError` / `LLMOutputError` / `faithfulness` 失败 → 转人工。
3. **金额/资格异常**：回调金额不匹配 → `amount_mismatch` → `HUMAN_HANDOFF`；资格存疑 → 转人工。
4. **外部未知/超时未确认**：→ `mismatch` 转人工。
5. **补偿失败**：`compensation_failed` → `human_handoff`。
6. **审批超时**：未确认 → 转人工。
7. **无文档/检索不过/幻觉检查失败**：→ `handle_error` 转人工。
8. **数据面租户作用域缺失/检查点恢复失败**：→ 停止写入、保留 `operation_id` + 完整状态、转人工并审计。

> 依据：`evidence/llm_fallback_to_human.json`（11 条路径，均 `operation_id_is_none=true`、`approval_none=true`）；
> `evidence/live_acceptance_summary.md`（`amount_mismatch→HUMAN_HANDOFF`、`timeout_unreachable→HUMAN_HANDOFF`、`no_provider→fail-closed`）；
> `tests/test_fault_injection.py`（8 例：timeout/5xx→FAILED_UNCERTAIN、明确失败→COMPENSATED、补偿失败→human_handoff、网关 down→mismatch 转人工）。
> 消费/触发端：`src/execution/engine.py`（execution_handoff / reconcile_mismatch）、`src/api/routes.py`（order_deny / approval_timeout）、`src/llm/*`。

### 3.3 人工接管的标准动作（管线）
| 步骤 | 动作 | 产出/证据 |
|------|------|-----------|
| 1 识别 | 命中触发场景（状态=HUMAN_HANDOFF / HUMAN_INTERVENTION / PENDING_UNCONFIRMED） | `operation_id` 完整状态 |
| 2 状态封存 | 保留完整状态（`operation_id`、审批、执行记录、审计、Trace） | 状态快照 + 审计 |
| 3 转人工 | 生成人工工单/通知（含最小必要租户上下文） | 工单 + `human_intervention_total{kind}` 打点 |
| 4 人工处置 | 人工（admin/approver/agent）核对、决策 | 处置记录 + 审计 |
| 5 恢复/收口 | 人工确认后系统**只重放既有动作**（resume/replay），**不重执行**；或人工直接收口 | 幂等重放/收口记录 |
| 6 留痕 | 全链路 `append_audit`（操作者、依据、时间、结果） | 四维审计（tenant/session/approval/operation） |

### 3.4 人工接管的红线
- **只重放不重执行**：同 `operation_id` 绝不二次写资金（幂等键 + 终态封闭 + `UNIQUE(tenant_id,operation_id)`）。
- **带完整状态转人工**：宁可慢、宁可人工，绝不给可能错误的账户操作。
- **同租户**：人工工单/处置必须限定当前 `tenant_id`；跨租户一律拒绝并留痕。
- **审计**：`platform_admin` 仅用于平台级合规（显式目标租户 + 最小范围 + 二次确认 + 不可抵赖审计），不替代租户审批。

---

## 4. "资金异常立即关 live 转人工"标准动作（上线观测期红线）

> 目的：观测期一旦出现任何资金异常，**立即**停止 live 资金副作用，转人工，不拖延。

**触发信号**（任一）：
- 出现**未审批即执行**（审批绕过）。
- 出现**同一 operation_id 重复执行**（重复扣款/退款）。
- 出现**跨租户资金访问**（跨租户读/写/审批被放行）。
- 出现**对账 mismatch 且不可核实**。
- 告警命中：`CrossTenantAccessDetected` / `ApiHighErrorRate` / `ApprovalFailureRateHigh` / `HighHumanInterventionRate` / `RpoExceeded` / `RtoExceeded` / `ReconciliationMismatch`。

**标准动作（三步，按序执行）**：
1. **关 live（立即止损）**：置 `EXECUTION_MODE=readonly` 或回 shadow（`EXECUTION_PROVIDER=mock`）；停/隔离 worker（`docker compose stop worker`）；nginx 侧对新请求返回 503 或摘除后端；收紧 `LAUNCH_ALLOWED_TENANTS`。**不允许**用低档模型续跑。
2. **转人工（带完整状态）**：保留 `operation_id` / 审批 / 执行记录 / 审计 / Trace；生成人工工单，通知值班/admin/approver；**只重放不重执行**。
3. **记录 + 评估回滚**：在 `deploy/drills/records/<ts>-rollback.md` 记录触发信号与动作；按 `deploy/OPS_RUNBOOK.md` §3 判断是否回滚（恢复到变更前快照 + 应用版本回退）；回滚前先做加密备份。

**证据**：
- `compose ps`/配置变更记录；`verify_launch_gate --strict`；`GET /api/audit`（四维）；`human_intervention_total`/`security_denials_total` 打点；告警记录（`deploy/observability/evidence/`）。

---

## 5. 值班（On-Call）与处置（`deploy/OPS_RUNBOOK.md` §4）

| 步骤 | 动作 | 产出 |
|------|------|------|
| 1 | 告警接收（Prometheus/Grafana/Langfuse/Loki）→ 5 分钟响应 | 值班记录 |
| 2 | 定性（数据面/应用面/观测面/安全） | 事故分类 |
| 3 | 止损（若涉资金/审批/越权→先止损：回滚或停租户白名单；**不**用低档模型续跑） | 止损动作 |
| 4 | 排查（Langfuse trace、Loki 脱敏日志、`GET /api/audit` 四维） | 根因假设 |
| 5 | 恢复/回滚（按 §3 回滚条件） | 恢复结果 |
| 6 | 复核（备份恢复演练 + `healthcheck.sh` + 对账） | 复核记录 |
| 7 | 复盘（更新 OPS_RUNBOOK/告警阈值/演练记录；**需二次审批才恢复变更**） | 复盘记录 |

**值班红线**：
- 绝不把 PII（地址/支付/卡号）写入工单/日志（日志已全局脱敏）。
- 越权/跨租户一律走 `platform_admin` 独立流程 + 二次确认 + 不可抵赖审计。
- 敏感写异常一律转人工，绝不自行用低可靠模型补齐。

---

## 6. 结论 / 状态

- **回滚**：机制与实跑记录已具备（`deploy/scripts/rollback.sh` + `DR-20260904030354-rollback.md`，t6 实跑通过）；回滚=恢复快照（RPO）+ 应用版本回退。
- **暂停**：机制已定义（nginx 摘除/503 + `LAUNCH_ALLOWED_TENANTS` 收紧 + `EXECUTION_MODE=readonly` + worker 停/隔离 + 暂停前备份），保留现场。
- **人工接管**：机制已定义（带完整状态转人工、只重放不重执行、全流程审计）；触发场景与红线均有证据（fail-closed 矩阵 + 故障注入 + live 沙箱）。
- **资金异常立即关 live**：标准三步动作（关 live → 转人工 → 记录+评估回滚）已定义。
- **对应门禁（`GO_NO_GO.md`，结论待回填定稿）**：G11 人工审批 `待 T2 证据`、G7 恢复备份 `待 T3 证据`、G6 沙箱网关 `待 T5 证据` → **回滚/暂停/人工接管机制已在机制层定义，且具备 `[substrate]` 实证**（`deploy/scripts/rollback.sh`+`DR-20260904030354-rollback.md`、`deploy/OPS_RUNBOOK.md`§3/§4、`evidence/llm_fallback_to_human.json`(11 条转人工)、`tests/test_fault_injection.py`(8 例)）；但**生产实机演练**仍需在独立生产栈上按 `OPS_RUNBOOK` §5 每日恢复演练 + `verify_dr_compose.sh` 容器实跑复验后归档，方可由 t3 证据定稿。

## 7. 签名
- 产出行：`release-manager`（t6）
