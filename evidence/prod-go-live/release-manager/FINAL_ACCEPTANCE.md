# 验收结论（FINAL_ACCEPTANCE · release-manager · t6 · 定稿 · rc3（rc2 期定稿，随 rc3 发布））

> **产出国角色**：release-manager（发布经理）· prod-go-live 团队 · 任务 t6
> **版本状态**：**定稿**（基于 t1–t5 各角色回填的真实证据；逐条结论与整体结论已定稿）。**随 `release/v1.0.0-rc3`（=3ccab5c）提交。**
> **git 口径（RC3 定稿）**：当前发布锚点 = `release/v1.0.0-rc3` = annotated tag 对象 `d1d2867` → commit `3ccab5c`（== HEAD）。本文为 **rc2 期定稿**，作为 rc3 发布证据随 rc3 发布；rc1/rc2 为历史锚点（rc2 = tag 对象 `290b830` → commit `fa7c9a3`；`bca4861` 为 rc2 谱系早期收口 commit 父提交，非 rc2 锚）。
>
> **rc2 锚定口径修正（✨ 定稿 · release-manager 2026-09-05 · 经队长 git 复测确认）**：`release/v1.0.0-rc2` = annotated tag 对象 **`290b830`** → commit **`fa7c9a3`**（rc2 文档收口尖端）；**`bca4861` 为 rc2 谱系早期"阶段一 rc2 基线收口" commit（祖先），不是 rc2 tag/锚**。本文档凡涉及 rc2 锚定的表述以此为准；rc1 锚保留 `cd743d3` 不变。基线链定稿：`7941246→3268a1c(可复现构建)→696444a→8ddca48→cd743d3(rc1)→cde30fb→59e37f2→bca4861(rc2 早期收口 commit, 祖先)→fa7c9a3(rc2 tag 目标)→阶段一 rc3 收口(3ccab5c=rc3 tag 目标==HEAD)`。
> **证据来源**：`evidence/prod-go-live/<role>/`；并交叉引用仓库既有 `[substrate]`（preview/沙箱级）作为能力基线。
> **证据边界**：本文件所有判定均按四证据边界（边界1 单元测试 / 边界2 Mock·沙箱 / 边界3 PostgreSQL·RLS 实测 / 边界4 真实生产外部依赖）标注，绝不把"一次性库取证"误作"生产栈实机达标"，也不把"mock 引擎"误作"真实模型能力"。定义详见 §〇。
> **诚实原则**：凡**真实租户书面确认 / 真实资金链路 / 受信 CA / 真实权重模型 / 7 天观察**未获提供，一律如实标注 `❌ BLOCKED-需外部`；凡**生产栈运行时 RLS / 告警端到端复验 / `git archive` clean-context 字节级重建 / mismatch 计数后台实测**未实际执行，一律标注 `部署期执行项`；**独立生产栈容器级带起健康已证实**（`PROD_STACK_HEALTH.md`，T7），绝不虚报、也不缩小。

---

## 〇、四种证据边界（本文件统一口径）

| 边界 | 定义 | 证据示例 | 边界内可信度 |
|:---:|------|---------|-------------|
| **边界1 单元测试** | `pytest tests/` 纯单测（内存/SQLite，不触真实 PG/外部） | `tests/test_approval_idempotency.py`/`test_tenant_isolation.py`/`test_llm_endpoint_gate.py` | 契约/逻辑层 |
| **边界2 Mock / 沙箱** | preview mock LLM、`sandbox_gateway` 沙箱、沙箱并发、真实 HTTP 沙箱回执、合成钻取告警 | `evidence/sandbox_concurrency_stress.json`、`acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md`、`tests/test_sandbox_e2e_flow.py` | 系统层行为（非真实资金/权重） |
| **边界3 PostgreSQL / RLS 实测** | 真实 PostgreSQL 数据面（动态 B1–B5、真实 PG 并发、DR 加密恢复、全新库 clean migrate） | `security-auditor/rls_dynamic_probe.json`、`test-runner/concurrency_stress_pg.json`、`DR_encrypted_restore.md`、`deploy-engineer/MIGRATE_VERIFY.md` | 数据面真实性（对象多为一**次性独立测试库**，非 `after-sales-prod` 专栈实机） |
| **边界4 真实生产外部依赖** | 真实受信 CA/域名、真实权重模型端点、真实资金渠道、书面确认真实租户、7 天观察 | 无（外部输入未提供）——均 **BLOCKED-需外部** | 仅外部输入到位后成立 |

> **诚实声明**：边界3 的"真实 PG"取证对象多为 **preview 集群 + 一次性独立测试库**（`langgraph_rls_audit__*`/`langgraph_drill`/`after-sales-drill-pg`/`after-sales-prodtest-pg`），属真实 PostgreSQL 数据面技术真实性，但**非 `after-sales-prod` 专栈实机**；生产栈容器级健康已证实（PROD_STACK_HEALTH），但生产栈数据面 RLS/并发/恢复复验仍为**部署期执行项**。边界2 的"真实运行态告警"用受控合成钻取源（`drill-probe`）注入，非真实流量；"可写模型"跑在 mock 引擎，**不构成真实模型能力证明**。RPO 为 pg_dump 周期上界（≤900s），非 PITR/WAL 秒级。

---

## 一、验收结论摘要（定稿）

| # | 用户验收标准 | 结论 | 本机闭环？ | 证据边界 | 主要证据 |
|---|-------------|:---:|:---:|:---:|---------|
| A1 | **首批真实租户受控闭环** | ❌ **未闭环（BLOCKED-需业务方）** | 否 | 边界4 | `deploy-engineer/DEPLOY_BASELINE.md` §3.1（`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`AUTH_LOGIN_CREDENTIALS={}`）；`CANARY_TENANT_CONFIRMATION_TEMPLATE.md` |
| A2 | **无审批绕过** | ✅ **达成（本机闭环）** | 是 | 边界1 | `security-auditor/RLS_ISOLATION_REPORT.md` §三；`tests/test_approval_idempotency.py`/`test_execution_engine.py`/`test_tenant_isolation.py`/`test_launch_gate_audit.py`（60 passed） |
| A3 | **无跨租户** | ✅ **达成（本机闭环）** | 是 | 边界1/3 | `security-auditor/rls_dynamic_probe.json`（B1–B5 conclusion=true）；`RLS_ISOLATION_REPORT.md` §一/§二（跨租户读/写/审批 404+越权留痕） |
| A4 | **无重复执行** | ✅ **达成（本机闭环，真实 PG+沙箱）** | 是 | 边界1/2/3 | `test-runner/concurrency_stress_pg.json`（N=256 submit=1）、`concurrency_stress.json`、`sandbox_concurrency_stress.json`；`test_pg_callback_concurrency.py` 7 passed |
| A5 | **真实资金链路可对账** | ⚠️ **部分**（沙箱对账 ✅；真实资金链路＝待外部输入/未接） | 否 | 边界2/3（沙箱）＋边界4（真实资金） | `acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §3/§4；`test-runner/reconcile_evidence.json`；`[substrate]` `evidence/live_acceptance_summary.md` |
| A6 | **具备回滚/暂停/人工接管机制** | ✅ **达成（机制层本机验证）** | 是 | 边界1/2/3 | `test-runner/DR_encrypted_restore.md`（RPO/RTO）；`LLM_GATEWAY_ACCEPTANCE.md` §3（reconcile/compensate→HUMAN_HANDOFF）；`ROLLBACK_PAUSE_TAKEOVER.md`；`OPS_RUNBOOK.md` §3/§4 |
| A7 | **只读 + shadow 先于 live** | ✅ **达成（有序）** | 是 | 边界2 | `observability-engineer/OBSERVABILITY_REPORT.md` §4（`EXECUTION_MODE=shadow`+mock，未切 live）；`deploy-engineer/DEPLOY_BASELINE.md` §6.1；`SHADOW_TO_LIVE_GATE.md` |
| A8 | **生产隔离（密钥/库/备份不复用 preview）** | ✅ **配置级**（`independent=true`）+ ✅ **完整 prod 栈带起健康已证实**（T7）+ ⚠️ 生产栈运行时 RLS 复验＝部署期执行项 | 是（配置级）/ 部署期（运行时复验） | 边界3 | `deploy-engineer/INDEPENDENCE.json`（independent=true）；`DEPLOY_BASELINE.md` §3/§5/§6.2/§7#7；`PROD_STACK_HEALTH.md`；`MIGRATE_VERIFY.md` |
| A9 | **受信 TLS** | ❌ **未闭环（BLOCKED-需外部）** | 否 | 边界4 | `deploy-engineer/DEPLOY_BASELINE.md` §4（自签 CN=preview.local；无受信 CA） |
| A10 | **真实模型评测 + 能力矩阵执行门控** | ❌ **BLOCKED**（真实模型评测）＋能力矩阵/写门控 ✅ PASS | 部分 | 边界1/2（能力矩阵）+ 边界4（真实模型） | `acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §1/§2（preview mock、端点不可达、self-hosted-model=MockLLM）；`evidence/llm_candidate_eval.json`（stub） |
| A11 | **7 天连续观察** | ❌ **未闭环（待切 live 后）** | 否 | 边界4 | `observability-engineer/OBSERVABILITY_REPORT.md` §5（7 天监控方案）；`SHADOW_TO_LIVE_GATE.md` S6 |

---

## 二、逐项判定（定稿）

### A1 首批真实租户受控闭环 —— ❌ 未闭环（BLOCKED-需业务方）
- **判定**：`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`（fail-closed）、`AUTH_LOGIN_CREDENTIALS={}`（无真实租户哈希）；**无任何真实租户书面确认函**；当前 `TENANT-A/B` 为历史演示租户（新基线已禁用 `seed_default`）。**不可本机闭环**，需真人/业务方签署。
- **证据**：`evidence/prod-go-live/deploy-engineer/DEPLOY_BASELINE.md` §3.1；`CANARY_TENANT_CONFIRMATION_TEMPLATE.md`。
- **所需输入**：真实租户（业务方）签署确认函 + `hash_login_credentials.py` 产 argon2id PHC 注入 + `LAUNCH_ALLOWED_TENANTS` 注入 + 成员/角色授予 + 审计。

### A2 无审批绕过 —— ✅ 达成（本机闭环）
- **判定**：退款/退货/改址必经唯一 `human_approval`；无任何 `direct → execute_*` 边；执行节点 5 重复核（含审批状态必须 APPROVED）；二次确认 + CAS 单抢占；拒绝→`REJECTED`/`HUMAN_HANDOFF` 绝不执行；跨租户审批/读取→404+越权留痕。
- **证据**：`evidence/prod-go-live/security-auditor/RLS_ISOLATION_REPORT.md` §三；`tests/test_approval_idempotency.py`/`test_execution_engine.py`/`test_tenant_isolation.py`/`test_launch_gate_audit.py`（60 passed）。

### A3 无跨租户 —— ✅ 达成（本机闭环）
- **判定**：真实 PG 动态 B1–B5 全拦；应用层 `resolve_tenant_context`（JWT+成员校验）拒绝客户端租户；跨租户读/写/审批 404 + 越权留痕；联合唯一键隔离同名 order/request id。
- **证据**：`evidence/prod-go-live/security-auditor/rls_dynamic_probe.json`（conclusion=true）、`RLS_ISOLATION_REPORT.md` §一/§二、`pg_rls_inventory_live.json`。

### A4 无重复执行 —— ✅ 达成（本机闭环，真实 PG+沙箱）
- **判定**：同 operation 并发 N=256 → `provider.submit=1`、execution record=1；跨租户同幂等键互不覆盖；同 nonce CAS 仅 1 confirmed、其余 replay、终态封闭；FAIL 路径收敛单一 `compensated`。
- **证据**：`evidence/prod-go-live/test-runner/concurrency_stress_pg.json`（真实 PG N=256）、`concurrency_stress.json`（SQLite N=256）、`sandbox_concurrency_stress.json`（真 HTTP 沙箱）；`test_pg_callback_concurrency.py` 7 passed。

### A5 真实资金链路可对账 —— ⚠️ 部分
- **判定**：**沙箱级对账 ✅**——入口可达 + 外部未知/无 provider → `MISMATCHED`→`HUMAN_HANDOFF` + 审计留痕（`execution.reconcile.mismatch`、`execution.compensated`）；网关沙箱代码/测试级 PASS（服务端幂等/验签/补偿/对账/gateway_unconfigured fail-closed/N=256 单次提交）；**但真实资金/真实网关渠道未接**，`EXECUTION_MODE=shadow`（不触真实资金），**生产网关沙箱可用性＝部署期执行项/未部署**；mismatch 计数后台未实测。
- **证据**：`evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §3/§4、`test-runner/reconcile_evidence.json`、`CONCURRENCY_RECOVERY_REPORT.md` §5。
- **所需输入**：真实资金/网关渠道 + 生产网关沙箱部署 + 对账重跑。

### A6 具备回滚/暂停/人工接管机制 —— ✅ 达成（机制层本机验证）
- **判定**：① **回滚**：加密备份（RTO=0.643s）+ `rollback.sh`/`restore_drill.sh` + DR 演练记录；② **暂停**：`OPS_RUNBOOK` §3/§4（nginx 摘除/503 + `LAUNCH_ALLOWED_TENANTS` 收紧 + `EXECUTION_MODE=readonly` + worker 停 + 暂停前备份）；③ **人工接管**：`mismatch/human_handoff`→`HUMAN_HANDOFF`、`gateway_unconfigured` fail-closed、补偿失败→转人工、`LLMUnavailableError`→转人工；终态封闭防重复扣款/退款；④ **资金异常立即关 live 转人工**三步动作。机制层已定义且具备实测/记录；生产实机演练为部署期执行项。
- **证据**：`evidence/prod-go-live/test-runner/DR_encrypted_restore.md`、`deploy/scripts/rollback.sh`、`OPS_RUNBOOK.md` §3/§4、`LLM_GATEWAY_ACCEPTANCE.md` §3、`ROLLBACK_PAUSE_TAKEOVER.md`。

### A7 只读 + shadow 先于 live —— ✅ 达成（有序）
- **判定**：当前 `EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`、`LAUNCH_GATE_STRICT=true`；`_run_shadow` 只生成待执行记录+模拟回执（`simulated=true`），不触真实资金；未切 live。
- **证据**：`evidence/prod-go-live/observability-engineer/OBSERVABILITY_REPORT.md` §4、`deploy-engineer/DEPLOY_BASELINE.md` §6.1、`LLM_GATEWAY_ACCEPTANCE.md` §4；`SHADOW_TO_LIVE_GATE.md`（S0–S6）。

### A8 生产隔离（密钥/库/备份不复用 preview）—— ✅ 配置级 independent=true + ✅ 完整 prod 栈带起健康已证实（T7）
- **判定**：`INDEPENDENCE.json` `independent=true`（项目/库名/密码指纹/卷/备份目录/子网/宿主端口/nginx/密钥全部与 preview 不同且不相交、不触碰 `./data/preview-*`）；全新 prod-like 库 clean migrate exit 0（17 表、复合 FK 正确、RLS 生效）。**完整 `after-sales-prod` 栈带起健康：✅ 已证实**（`PROD_STACK_HEALTH.md`，T7）——compose `migrate` exit 0、api/worker/frontend/nginx/postgres/redis 全容器 **healthy**、nginx 8080/8843 暴露、api `/api/healthz`(8843)=200、frontend `/`=200、`/api/metrics`(公网)=404（内网化正确阻断）。**⚠️ 生产栈运行时 RLS / 告警端到端复验**（DEPLOY_BASELINE §7#8）为**部署期执行项**；受信 TLS（A9）就绪前不可对外暴露 8080/8843（当前 443 用自签）。
- **证据**：`evidence/prod-go-live/deploy-engineer/INDEPENDENCE.json`、`DEPLOY_BASELINE.md` §3/§5/§6.2/§7#7、`PROD_STACK_HEALTH.md`、`MIGRATE_VERIFY.md`。

### A9 受信 TLS —— ❌ 未闭环（BLOCKED-需外部）
- **判定**：现有证书自签（Subject=Issuer=CN=preview.local）；无受信 CA 链。
- **证据**：`evidence/prod-go-live/deploy-engineer/DEPLOY_BASELINE.md` §4。
- **所需输入**：真实域名 + 受信 CA 证书链 + `nginx -t`/端到端复验。

### A10 真实模型评测 + 能力矩阵执行门控 —— ❌ BLOCKED（真实模型评测）＋能力矩阵/写门控 ✅ PASS
- **判定**：① 真实模型评测 `❌ BLOCKED-需外部`：preview `LLM_BACKEND=mock`、`model-endpoint:8001` 无容器、host 探测 `127.0.0.1:8001/v1/models` 连接拒绝、`self-hosted-model`=MockLLM 规则引擎代表（非真实权重）；`evidence/llm_candidate_eval.json` 现仅 `self-hosted-demo` stub（write_op_pass=true,cases=1）。② 能力矩阵/写门控 `✅ PASS`：受限环境空白名单/缺报告→`frozenset()` fail-closed；`capability_ok(self-hosted-demo)=True`、`self-hosted-model=False`；非白名单拒写转人工；`test_llm_endpoint_gate.py` 21 passed。⚠️ 可写模型为 mock 引擎，不构成真实模型能力证明；`verify_capability_matrix_t2.py` 因报告缺 `self-hosted-model` 有 4 项 FAIL（报告完整性）。
- **差异根因（诚实标注 · 阶段一只标注、不重生成）**：同 G5——`evidence/llm_candidate_eval.json` 的 `self-hosted-demo`/1‑用例 stub **非真实评测产物**，系 `tests/test_llm_endpoint_gate.py::test_whitelist_model_still_requires_approval` **覆写共享证据文件**所致（运行 pytest 即清空真评测报告）。权威目标口径 = 模型 `self-hosted-model` + **全量 24 用例**；真实重生成推迟到阶段三（需真实权重端点先起 + 先用 mock 对 `self-hosted-model` 产口径一致报告 + **先修该测试**，否则再跑 pytest 会再次清空）。当前只做诚实标注，**不宣称已验证**。
- **证据**：`evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §1/§2 + `LLM_GATEWAY_EVIDENCE.json`。

### A11 7 天连续观察 —— ❌ 未闭环（待切 live 后）
- **判定**：当前未切 live，观察期未开始。
- **证据**：`evidence/prod-go-live/observability-engineer/OBSERVABILITY_REPORT.md` §5（7 天监控指标集/看板/任一资金异常关 live 转人工动作）；`SHADOW_TO_LIVE_GATE.md` S6。
- **所需输入**：切 live 后 ≥7 天连续观测 + 每日恢复演练 + 告警无异常。

---

## 三、整体结论（定稿）

### `NO-GO`（正式生产上线当前不可放行）／**能力/合规/幂等/可回滚已闭环（GO 的能力基础），受外部输入阻断转有条件 GO**

- **已在本机闭环并验证（✅）**：A2 无审批绕过、A3 无跨租户、A4 无重复执行（真实 PG+沙箱）、A6 具备回滚/暂停/人工接管、A7 只读+shadow 先于 live、A8 生产隔离（配置级 `independent=true` + 完整 prod 栈带起健康**已证实**，PROD_STACK_HEALTH T7）。
- **不可在本机闭环（❌/⚠️，需外部/部署期输入）**：
  | 项 | 状态 | 边界 | 原因（所需输入） |
  |----|:---:|:---:|------|
  | A1 首批真实租户受控闭环 | ❌ BLOCKED | 边界4 | 需业务方签署书面确认 + 白名单/凭据注入 |
  | A5 真实资金链路可对账 | ⚠️ 部分 | 边界2/3 + 边界4(真实资金) | 真实资金/网关渠道未接（生产网关沙箱＝部署期执行项） |
  | A9 受信 TLS | ❌ BLOCKED | 边界4 | 需真实域名 + 受信 CA 证书链 |
  | A10 真实权重模型评测 | ❌ BLOCKED | 边界1/2 + 边界4(真实模型) | 需真实自托管权重端点 + 评测报告 |
  | A11 7 天连续观察 | ❌ 待切 live 后 | 边界4 | 观察期未开始 |
- **诚实标注（本机不可闭环项）**：A1（真实租户书面确认）/A9（受信 TLS）/A10（真实权重模型）/A11（7 天观察）均因**真实外部输入缺失**不可闭环，属**边界4**；A5（真实资金）/A10 相关部分为**边界4 部署期执行项**；A8 的**生产栈运行时 RLS/告警复验**、A6 的**生产栈实机演练**为**部署期执行项**（需在 `after-sales-prod` 部署后实跑复验）。**A8 完整栈带起健康已证实**，不属"未闭环"。
- **结论**：系统已满足**无审批绕过、无跨租户、无重复执行、可对账（沙箱级）、具备回滚/暂停/人工接管、只读+shadow 先于 live、生产独立栈健康已证实**，构成 **GO 的能力基础**；但正式生产上线（切 live/放量）受 **A1 + A5 + A9 + A10 + A11** 外部输入（边界4）阻断，**当前 NO-GO**。外部输入到位 + 生产栈运行时 RLS/告警复验 + `git archive` clean-context 字节级重建（部署期执行项）通过后转**有条件 GO**（触发清单见 `GO_NO_GO.md` §三）。

---

## 四、签名
- 验收产出行：`release-manager`（t6）· **定稿**
- 说明：所有 ✅ 均引用可溯源真实证据；所有 ❌/⚠️ 均给出原因与所需输入，不虚报达标；凡部署期执行项（完整 prod 栈带起健康、运行时告警复验、对账指标重建）均如实标注。与 `GO_NO_GO.md` 整体裁决一致。
