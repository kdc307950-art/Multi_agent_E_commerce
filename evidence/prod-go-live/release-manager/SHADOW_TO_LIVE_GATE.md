# shadow → live 切换门控（受控上线流程）

> **产出国角色**：release-manager（发布经理）· prod-go-live 团队 · 任务 t6
> **目的**：把"只读 + shadow 观察 → 人工复核 → 对账 → 业务负责人确认 → 指定租户切 live"的**分步门控**落成
> **可执行、可证据化、可回退**的流程。**每步有表单/证据、显式门槛、操作卡（`EXECUTION_MODE`/`EXECUTION_PROVIDER`/
> `LAUNCH_ALLOWED_TENANTS`/白名单模型调整）。**
> **重要边界**：**本项目任务不实际执行 live 切换**，仅产出**受控门控流程**；实际切换须由部署/运维在满足门控后执行，
> 且**切 live 前必须取得业务负责人书面确认**（`CANARY_TENANT_CONFIRMATION_TEMPLATE.md`）。
>
> 依据：`deploy/OPS_RUNBOOK.md` §1（首次上线门控）、§2（上线阈值）、§3（回滚条件）；`deploy/records/PRODUCTION_ACCEPTANCE_VERIFICATION.md`；
> `deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-030903.md` §4/§8；`deploy/records/CANARY_TENANTS-TEMPLATE.md`；
> `evidence/prod-go-live/release-manager/GO_NO_GO.md`；`deploy/DR_KEY_MANAGEMENT.md`；`deploy/EGRESS_POLICY.md`。

---

## 0. 总原则

1. **只读 / shadow 严格先于 live**：正式生产**永不跳过只读 + shadow 观察**直接上 live 真实资金。
2. **每步有权重与回退**：任何一步不达标即**停滞在本步**，或**回退到 shadow/只读**，**绝不带病放量**。
3. **一切写操作带完整状态**：切 live 涉及真实资金，任何不确定即**转人工**（fail-closed），绝不降级用低档模型续跑。
4. **证据化**：每一步产出表单/记录（本文件每步的"表单与证据"列），异常或未达标者**如实标注 BLOCKED**。
5. **审计留痕**：操作者、审批依据、时间、结果四维可追溯（tenant/session/approval/operation）。

---

## 1. 分步门控总览

| 阶段 | 门控名 | 输入 → 输出 | 门槛（全部满足才过） | 责任人 |
|:---:|--------|------------|---------------------|--------|
| S0 | 只读观察 | 生产栈部署 + 只读接入 → 只读观测记录 | 只读无任何写副作用；不产生 approval/operation | deploy-engineer / observability-engineer |
| S1 | shadow 观察 | `EXECUTION_MODE=shadow` → shadow 观测记录 | shadow 无真实资金副作用；无绕过/无重复/可对账 | deploy-engineer |
| S2 | 人工复核 | 复核 shadow 期间的审批/审计/指标 → 复核表 | 审批/审计/指标达标；无 BLOCKED | release-manager / security-auditor |
| S3 | 对账 | shadow 对账运行 → 对账报告 | 非终态收口；mismatch 可核实 or 转人工 | test-runner |
| S4 | 业务负责人确认 | 书面确认函 + 切 live 审批单 → 批准 | 同租户 admin/approver 二次确认；书面确认 | release-manager / 业务方 |
| S5 | 指定租户切 live | `EXECUTION_MODE=live` + 白名单调整 → 上线记录 | 全部前置满足；仅白名单租户；可回退 | deploy-engineer |
| S6 | 7 天连续观察 | live 运行 + 每日观测 → 7 天观察报告 | 观测指标全部达标；无告警红线 | observability-engineer |

---

## 2. 逐步详表（表单 / 证据 / 操作卡）

### S0 只读观察
- **目标**：确认生产栈在**只读**下正常工作，无任何写副作用。
- **表单与证据**：
  - `PRODUCTION_DEPLOYMENT_record-<ts>.md`（生产栈部署 + `healthcheck.sh` 全过、仅 80/443、无 CORS）。
  - 只读观测记录：`GET /api/healthz`、`/api/metrics`、前端 `/`；确认**无任何** `approval`/`operation`/`execution.create` 产生。
- **门槛**：
  1. 生产栈健康（healthcheck 4 项全过）。
  2. 仅 80/443 暴露；无 CORS 头。
  3. 只读接入后**零写副作用**（审计无 `execution.create`/`approval.decide`）。
- **操作卡**：`EXECUTION_MODE=readonly`（或保持 shadow 但关闭一切写触发）——**由配置层保证**，禁止运行时绕过。
- **不达标/回退**：任一失败 → 停在此步，检查安全基线，不进入 S1。

### S1 shadow 观察
- **目标**：在 shadow 模式（**不上真实资金**）下运行完整业务流，观察行为与指标。
- **表单与证据**：
  - 配置快照：`EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`（`deploy/.env.<env>`）。
  - shadow 观测：`deploy/records/METRICS_SUMMARY-<ts>.md`、`evidence/sec_dynamic_acceptance-<ts>.md`（无绕过/无重复/可对账）。
  - `healthcheck.sh` 复测；`compose ps` 全部 healthy。
- **门槛**：
  1. 无真实资金副作用（shadow 仅沙箱）。
  2. 无审批绕过（唯一 human_approval）。
  3. 无重复执行（同一 operation 恰 1 次）。
  4. 可对账（对账入口可复核）。
- **操作卡**：`EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`；`HIGH_CONFIDENCE_MODELS` 指向评测通过模型（如需写则必须为白名单）。
- **不达标/回退**：出现绕过/重复/不可对账 → 立即排查并**回退**，不进入 S2。

### S2 人工复核
- **目标**：由发布经理/安全审计对 shadow 期间的审批、审计、指标做独立复核。
- **表单与证据**：
  - `sec_dynamic_acceptance-<ts>.md`（无审批绕过 / 无跨租户 / 无重复 / 无未审计写）。
  - 审计四维抽查：`GET /api/audit?target_type=approval|execution...`；`approval.decide`/`execution.create`/`security.deny.*` 留痕。
  - 指标复核：错误率/审批失败率/审批耗时/对账/人工介入/RPO-RTO/安全拒绝。
- **门槛**：
  1. 审批归属正确（同租户、仅 admin/approver、二次确认）。
  2. 跨租户访问零放行。
  3. 无未审计写。
  4. 关键指标可用（无空表达式/无高基数标签）。
- **操作卡**：无（复核动作，冻结 shadow 配置以保持一致性）。
- **不达标/回退**：任一异常 → 记录并回退 shadow 复查；修复后可重跑本步。

### S3 对账
- **目标**：证明后台对账可将非终态收口、不一致/不可核实转人工。
- **表单与证据**：
  - `COMPENSATION_RECONCILIATION_REPORT-<ts>.md`、`deploy/drills/records/drill-api-reconcile.json`。
  - 对账运行一次，采样 mismatch 计数（避免"入口可达但计数为空"）。
- **门槛**：
  1. 非终态收口为终态。
  2. mismatch 可核实 → 转人工；不可核实 → 转人工（绝不静默）。
  3. `reconcile_mismatch_total` 有界打点可观测。
- **操作卡**：无（后台 Celery 对账任务；确保 worker 运行）。
- **不达标/回退**：mismatch 计数无法观测或收口失败 → 回退 shadow 排查，不入 S4。

### S4 业务负责人确认（切 live 关键闸门）
- **目标**：取得**业务负责人/租户方书面确认** + **同租户 admin/approver 二次确认审批单**。
- **表单与证据**：
  - `CANARY_CONF-<租户ID>-<ts>.md`（`CANARY_TENANT_CONFIRMATION_TEMPLATE.md` 签署回传）。
  - `deploy/records/SCALEUP_APPROVAL-<ts>.md`（放量审批单，由目标租户 `admin`/`approver` 二次确认签署）。
- **门槛**：
  1. 书面确认函**由真人/业务方签署**（本系统不代签）。
  2. 放量审批单：同租户 `admin`/`approver` + 二次确认 + 审计留痕（`approval_id`/`operation_id`）。
  3. 审批人与目标资源**同租户**；跨租户审批一律拒绝并留痕。
  4. `platform_admin` 不得替代租户审批（仅平台级合规流程独立受控）。
- **操作卡**：无（审批/签署动作）。
- **不达标/回退**：**未经书面确认或审批单批准 → 不切 live**。

### S5 指定租户切 live（受控执行）
- **目标**：对**已书面确认 + 已审批**的**指定租户**，将 `EXECUTION_MODE` 从 shadow 切到 live 并用真实 provider，**仅放行白名单租户**。
- **操作卡（配置变更，须 `deploy/.env.<env>` 内显式、不落仓）**：
  1. `EXECUTION_MODE=live`、`EXECUTION_PROVIDER=<真实受控 provider>`（如网关直连渠道，**必须带 fail-closed**）。
  2. `LAUNCH_ALLOWED_TENANTS=<已书面确认租户，逗号分隔>`（**仅放行本批**；非名单租户 403 并审计）。
  3. `HIGH_CONFIDENCE_MODELS=<评测 write_op_pass=true 模型>`（限定写操作模型；未评测/非白名单被拒转人工）。
  4. `LAUNCH_GATE_STRICT=true`、`LAUNCH_REQUIRE_APPROVAL=true`、`LAUNCH_FULL_AUDIT=true`、`LAUNCH_MANUAL_REVIEW=true`。
  5. 重新跑 `scripts/verify_launch_gate.py --strict`（0 违规）＋ `deploy/scripts/healthcheck.sh`。
- **表单与证据**：
  - 变更记录（改了什么变量、谁、何时）、`verify_launch_gate --strict` 输出、`healthcheck.sh` 结果、`compose ps`。
  - 切 live 后对**指定租户**做冒烟：会话→退款→审批→执行（真实 provider 沙箱/受控渠道），确认无重复/无绕过/可对账。
- **门槛**：
  1. 所有前置（S0–S4）达标。
  2. 仅白名单租户放行。
  3. 切 live 冒烟通过（无重复/无绕过/可对账）。
  4. 具备回滚（`rollback.sh` + 变更前备份）。
- **不达标/回退**：冒烟失败或发现异常 → **立即关 live 转 shadow/人工** + 按 `ROLLBACK_PAUSE_TAKEOVER.md` 触发回滚。

### S6 7 天连续观察
- **目标**：切换 live 后**连续观察 ≥7 天**，验证无异常并留存证据。
- **表单与证据**：
  - 每日观测记录（`deploy/records/METRICS_SUMMARY-<ts>.md` + 每日备份恢复演练记录 `deploy/drills/record_template.md`）。
  - 观察维度：跨租户拒绝、重复执行、回调失败、人工介入率、对账差异、错误率、备份结果。
  - 告警记录（`deploy/observability/evidence/` 告警触发/定位/关闭）。
- **门槛**：
  1. ≥7 天连续无"跨租户/重复执行/审批绕过"事件。
  2. 错误率/人工介入率/对账 mismatch 在告警阈值内。
  3. 每日恢复演练成功（RPO/RTO 达标）。
  4. 无告警红线命中（`CrossTenantAccessDetected`/`RpoExceeded`/`RtoExceeded`/`ApprovalFailureRateHigh`/`HighHumanInterventionRate` 等）。
- **不达标/回退**：任一红线命中 → **立即关 live 转人工**；连续异常 → 按回滚条件回滚。

---

## 3. 关键配置变量改动表（切 live 操作卡摘要）

| 变量 | shadow（切 live 前） | live（切 live 后） | 说明 / 风险 |
|------|--------------------|-------------------|------------|
| `EXECUTION_MODE` | `shadow` | `live` | **关键**：shadow 无真实资金；live 触真实资金，必须 fail-closed |
| `EXECUTION_PROVIDER` | `mock` | `<真实受控 provider>` | 未配置/live 无 provider → fail-closed 拒（`live_without_provider_fail_closed`） |
| `LAUNCH_ALLOWED_TENANTS` | `<内部验收/首批>` | `<已书面确认租户>` | 仅放行白名单；非名单 403 + 审计 |
| `HIGH_CONFIDENCE_MODELS` | `<评测通过>` | `<评测 write_op_pass=true>` | 写风险工具仅白名单模型可写；否则转人工 |
| `LAUNCH_GATE_STRICT`/`LAUNCH_REQUIRE_APPROVAL`/`LAUNCH_FULL_AUDIT`/`LAUNCH_MANUAL_REVIEW` | `true` | `true`（保持不变） | 严禁为放量而降级 |
| `LLM_BACKEND`/`LLM_BASE_URL`/`LLM_MODEL` | `mock`（preview 现状）/`openai_compatible` | `openai_compatible` + 内网端点 + 评测通过模型 | 真实权重模型评测通过后才启用 |
| `METRICS_ALLOWED_SOURCES` | 内网 `172.30.0.0/16` | 内网 | `METRICS_EXPOSE_INTERNAL_ONLY=true`，不外网暴露 |
| `BACKUP_ENC_KEY`/`BACKUP_ROLE_PASSWORD` | 需注入（preview 现行明文→需迁移） | 注入独立生产密钥 | 生产独立密钥/库/备份，不复用 preview |

---

## 4. 放量红线（任一条 → 立即停 + 关 live 转人工 + 评估回滚）

1. **未审批即执行**（审批绕过）。
2. **同一 operation_id 二次执行**（重复资金副作用）。
3. **跨租户访问被放行**。
4. **对账 mismatch 且不可核实**。
5. 告警命中资金/安全红线（`RpoExceeded`/`RtoExceeded`/`CrossTenantAccessDetected`/`ApiHighErrorRate`/`ApprovalFailureRateHigh`/`HighHumanInterventionRate`）。
6. 每日恢复演练失败。

---

## 5. 结论 / 状态

- 当前系统状态：**处于 S1 shadow（`EXECUTION_MODE=shadow`/`EXECUTION_PROVIDER=mock`）**，符合"只读+shadow 先于 live"。
- **本任务未执行 live 切换**；实际切换须由部署/运维在满足 S0–S5 门控 + 业务负责人书面确认后执行，且全程可回退。
- 对应门禁（`GO_NO_GO.md`：G12 = `待 T1/T5 证据，勿假定`、G13 = `待外部输入（业务方签署）`、G14 = `待外部输入（需先切 live）`，见 GO_NO_GO.md §二）：本流程**已定义** shadow→live 受控门控；但**真实租户书面确认（G13）与 7 天观察（G14）**尚未完成（外部输入未到，按顺序硬约束不下定稿结论），故**当前不得切 live**。

## 6. 签名
- 产出行：`release-manager`（t6）
