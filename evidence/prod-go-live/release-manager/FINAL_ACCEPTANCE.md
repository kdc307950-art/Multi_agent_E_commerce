# 验收结论（FINAL_ACCEPTANCE · release-manager · t6 · 待定稿）

> **产出国角色**：release-manager（发布经理）· prod-go-live 团队 · 任务 t6
> **版本状态**：**骨架 + 验收标准定义 + 证据引用占位**；**整体结论未定稿**。
> 已按 **T5（acceptance-engineer）实测**定稿 **A5 / A10**（其余 A1–A4、A6–A9、A11 待对应 tX 证据/外部输入）。
> **顺序硬约束（captain 明示）**：验收结论**只能在读到 t1–t5 真实证据后定稿**。因此：
> - 每条验收标准**结论列**基于真实 t1–t5 生产证据；证据未到者标 **`待 tX 证据，勿假定`**。
> - **不可在本机闭环**的标准（依赖真实业务/资金/观察/生产栈/QoS）标 `待外部输入` 并写明所需输入；
>   但具体的 `✅ 实测 / ⚠️ 部分 / ❌ BLOCKED` 判据仍需 t1–t5 证据佐证（除纯真理性的外部依赖外）。
> - 仓库既有 preview/沙箱证据（`[substrate]`）仅作**能力基线参考**，不作生产验收定稿依据。
> - 定稿：t1–t5 回填后刷新本标准逐条结论，并同步 `GO_NO_GO.md` 整体裁决。

---

## 一、验收结论摘要（结论列＝待回填）

| # | 用户验收标准 | 结论 | 定稿依据 | 主要证据（占位） |
|---|-------------|:---:|:---:|---------|
| A1 | **首批真实租户受控闭环** | `待外部输入（业务方签署）` | 非 t1–t5 可产 | `CANARY_TENANT_CONFIRMATION_TEMPLATE.md`；`[substrate]` `deploy/records/CANARY_TENANTS-20260904-030903.md`（现为演示租户） |
| A2 | **无审批绕过** | `待 T2 证据，勿假定` | T2 | `evidence/prod-go-live/security-auditor/`；`[substrate]` `evidence/sec_dynamic_acceptance-20260904-030405.md`、`tests/test_approval_idempotency.py`(16/16)、`tests/test_execution_engine.py`(21/21) |
| A3 | **无跨租户** | `待 T2 证据，勿假定` | T2 | `evidence/prod-go-live/security-auditor/`；`[substrate]` `evidence/pg_rls_bypass_probe.json`(B1–B5)、`tests/test_rls_bypass.py -m postgres`(5 passed) |
| A4 | **无重复执行** | `待 T3 证据，勿假定` | T3 | `evidence/prod-go-live/test-runner/`；`[substrate]` `evidence/sandbox_concurrency_stress.json`(N=256→submit=1)、`evidence/concurrency_stress.json` |
| A5 | **真实资金链路可对账** | `⚠️ 部分`（沙箱对账机制 `✅ PASS`；**真实资金链路＝待外部输入/未接**） | T5 | `evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §3/§4；`[substrate]` `evidence/live_acceptance_summary.md`、`evidence/COMPENSATION_RECONCILIATION_REPORT.md` |
| A6 | **具备回滚/暂停/人工接管机制** | `待 T3/T2 证据定稿`（机制已定义，实证待回填归档） | T3/T2 | `evidence/prod-go-live/test-runner/`；`[substrate]` `deploy/scripts/rollback.sh`+`DR-20260904030354-rollback.md`、`deploy/OPS_RUNBOOK.md`§3/§4、`ROLLBACK_PAUSE_TAKEOVER.md` |
| A7 | **只读 + shadow 先于 live** | `待 T1/T5 证据，勿假定` | T1/T5 | `evidence/prod-go-live/deploy-engineer/`+`acceptance-engineer/`；`[substrate]` `PRODUCTION_DEPLOYMENT_record-20260904-030903.md`§4(EXECUTION_MODE=shadow) |
| A8 | **生产隔离（密钥/库/备份不复用 preview）** | `待 T1 证据，勿假定`（`[substrate]` 提示当前仅 preview） | T1 | `evidence/prod-go-live/deploy-engineer/`；`[substrate]` `PRODUCTION_DEPLOYMENT_record-20260904-030903.md`§0/§1/§4 |
| A9 | **受信 TLS** | `待 T1 证据，勿假定`（`[substrate]` 提示当前自签名） | T1 | `evidence/prod-go-live/deploy-engineer/`；`[substrate]` `PRODUCTION_DEPLOYMENT_record-20260904-030903.md`§7 |
| A10 | **真实模型评测 + 能力矩阵执行门控** | `❌ BLOCKED`（真实模型评测：无真实权重端点）＋能力矩阵/写门控 `✅ PASS`（但可写模型为 mock 引擎） | T5 | `evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §1/§2；`[substrate]` `evidence/llm_candidate_eval.json`（现为 self-hosted-demo stub） |
| A11 | **7 天连续观察** | `待外部输入（需先切 live 并观察）` | 非 t1–t5 可产 | `SHADOW_TO_LIVE_GATE.md` S6；`deploy/OPS_RUNBOOK.md`§5；`[substrate]` `deploy/records/DEPLOYMENT_SUMMARY-20260904-030903.md`§二（多处未实测/待观察） |

---

## 二、逐项判定（骨架：标准定义 + 证据引用占位 + 待回填口径）

> 每条为**证据引用占位 + 判定口径**；**结论在 tX 回填后填写**。`[substrate]` 仅作能力基线交叉参考，不作定稿依据。

### A1 首批真实租户受控闭环 —— `待外部输入（业务方签署）`
- **标准定义**：首批仅放行**经书面确认的真实租户**；确认函签署 + 白名单注入 + 成员/角色授予 + 审计。
- **证据占位**：`evidence/prod-go-live/release-manager/CANARY_TENANT_CONFIRMATION_TEMPLATE.md`（模板）；`[substrate]` `deploy/records/CANARY_TENANTS-20260904-030903.md`（现为 demo/seed 租户）。
- **判定口径**：真实租户（业务方）**书面确认函是否回传并签署**。未回传前恒未满足（本仓库不代签）。**非 t1–t5 可产**。

### A2 无审批绕过 —— `待 T2 证据，勿假定`
- **标准定义**：退款/退货/改址必经唯一 `human_approval`；无 direct 绕过；越权 403 / 跨租户 404 / 缺二次确认 422；拒绝不执行。
- **证据占位**：`evidence/prod-go-live/security-auditor/<T2 产出>`；`[substrate]` `evidence/sec_dynamic_acceptance-20260904-030405.md`（preview 验收：无审批绕过 ✅）、`tests/test_approval_idempotency.py`(16/16)、`tests/test_execution_engine.py`(21/21)、`src/graph/approval.py`。
- **判定口径**：在**生产数据面**确证唯一 human_approval 与归属校验；`[substrate]` 仅为 preview 层佐证。

### A3 无跨租户 —— `待 T2 证据，勿假定`
- **标准定义**：RLS FORCE + NOBYPASSRLS；跨租户读/写/改 tenant_id 零可见/被拒；同租户审批。
- **证据占位**：`evidence/prod-go-live/security-auditor/<T2 产出>`；`[substrate]` `evidence/pg_rls_bypass_probe.json`(B1–B5)、`tests/test_rls_bypass.py -m postgres`(5 passed)、`evidence/PG_RLS_POLICY_INVENTORY.md`。
- **判定口径**：在**生产栈**数据面复跑 B1–B5。

### A4 无重复执行 —— `待 T3 证据，勿假定`
- **标准定义**：同一资金操作只执行一次；`UNIQUE(tenant_id,operation_id)`；单执行守卫；终态封闭；重投放 r重放。
- **证据占位**：`evidence/prod-go-live/test-runner/<T3 产出>`；`[substrate]` `evidence/sandbox_concurrency_stress.json`(N=256→`provider.submit=1`)、`evidence/concurrency_stress.json`(I1/I2/I3 全 true)、`tests/test_sandbox_concurrency_guard.py`、`tests/test_sandbox_e2e_flow.py`(14 用例)。
- **判定口径**：在**生产栈**上同 operation 并发 → `provider.submit` 恰 1 次；重复审批收敛；跨租户幂等键互不覆盖。

### A5 真实资金链路可对账 —— `⚠️ 部分`（沙箱对账 `✅ PASS`；真实资金链路＝待外部输入/未接）
- **标准定义**：非终态收口；不一致/不可核实→转人工；mismatch 计数可观测；**真实资金链路**可对账。
- **证据（T5 实测，权威）**：
  - `evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §3：网关沙箱**代码/测试级 ✅ PASS**——服务端 SQLite 幂等、回调验签（`tests/test_callback_security_log.py` 6 passed）、reconcile → `MISMATCHED`→`HUMAN_HANDOFF`、compensate 稳定 reversal_id、`gateway_unconfigured` fail-closed、N=256 并发 `provider.submit` 恰 1 次（`evidence/sandbox_concurrency_stress.json`）、`tests/test_sandbox_e2e_flow.py` 14 passed（真实 uvicorn 临时端口）。
  - §4：preview 运行时 `EXECUTION_MODE=shadow`（不触真实资金）；**生产网关沙箱可用性＝部署期执行项，未部署**（`EXECUTION_PROVIDER=mock`、无 `GATEWAY_BASE_URL`）。
  - `[substrate]` `evidence/live_acceptance_summary.md`（live 沙箱 41/41）、`evidence/COMPENSATION_RECONCILIATION_REPORT.md`、`deploy/drills/records/drill-api-reconcile.json`。
- **判定**：**沙箱级对账机制 `✅ PASS`**（含 fail-closed 转人工）；**真实资金链路可对账＝待外部输入（真实资金/网关渠道未接）**，需接入真实渠道并重跑对账；`reconcile_mismatch_total` 计数后台观测与指标打点待补。

### A6 具备回滚/暂停/人工接管机制 —— 机制已定义（实证待回填归档）
- **标准定义**：① 回滚（恢复快照+版本回退+密钥轮换）② 暂停（停止新请求/关闸）③ 人工接管（带完整状态转人工、只重放不重执行）+ 资金异常立即关 live 转人工。
- **证据占位**：`evidence/prod-go-live/test-runner/<T3 产出>`（恢复/备份演练）+ `evidence/prod-go-live/security-auditor/<T2 产出>`（fail-closed 转人工取证）；`[substrate]` `deploy/scripts/rollback.sh`+`DR-20260904030354-rollback.md`(t6 实跑)、`deploy/scripts/restore_drill.sh`+`drill-pg-encrypted-restore.json`、`deploy/OPS_RUNBOOK.md`§3/§4、`evidence/llm_fallback_to_human.json`(11 条转人工)、`tests/test_fault_injection.py`(8 例)。
- **判定口径**：三种机制在**生产栈**上实跑并出具记录；`[substrate]` 已证明机制层可用；生产实机演练按 `OPS_RUNBOOK`§5 复验后归档。

### A7 只读 + shadow 先于 live —— `待 T1/T5 证据，勿假定`
- **标准定义**：先只读与 shadow 观察，人工复核/对账/业务负责人确认后再切 live；切换受控、可回退。
- **证据占位**：`evidence/prod-go-live/deploy-engineer/<T1 产出>`（部署模式）+ `acceptance-engineer/<T5 产出>`（live 前置）；`[substrate]` `PRODUCTION_DEPLOYMENT_record-20260904-030903.md`§4(EXECUTION_MODE=shadow/mock)；`SHADOW_TO_LIVE_GATE.md`（S0–S6 分步门控）。
- **判定口径**：当前 `EXECUTION_MODE=shadow`（未切 live），且流程受控；正式切 live 是否已发生/是否受控。

### A8 生产隔离（密钥/库/备份不复用 preview） —— `待 T1 证据，勿假定`
- **标准定义**：生产使用独立密钥/数据库/备份位置/证书，不复用 preview。
- **证据占位**：`evidence/prod-go-live/deploy-engineer/<T1 产出>`（独立生产栈 + 不复用 preview 核验）；`[substrate]` `PRODUCTION_DEPLOYMENT_record-20260904-030903.md`§0(`production 未部署`)/§1/§4(`deploy/.env.preview`)——仅提示，非定稿。
- **判定口径**：是否部署独立生产栈并核验隔离；未部署/未验证即不满足。

### A9 受信 TLS —— `待 T1 证据，勿假定`
- **标准定义**：生产使用受信 CA 证书链（非自签名），经 `nginx -t`/端到端 HTTPS 复验。
- **证据占位**：`evidence/prod-go-live/deploy-engineer/<T1 产出>`；`[substrate]` `PRODUCTION_DEPLOYMENT_record-20260904-030903.md`§7（自签名 CN=preview.local；“生产受信 CA：未配置”）——仅提示。
- **判定口径**：生产证书链由受信 CA 签发并经端到端校验。

### A10 真实模型评测 + 能力矩阵执行门控 —— `❌ BLOCKED`（真实模型评测）＋能力矩阵/写门控 `✅ PASS`
- **标准定义**：评测驱动白名单（`base ∩ write_op_pass=true`）；非白名单/未评测模型写 fail-closed；受限环境缺报告即拒绝；**真实权重模型评测通过**。
- **证据（T5 实测，权威）**：
  - `evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §1：**真实模型评测＝BLOCKED**——preview `LLM_BACKEND=mock`、`LLM_BASE_URL=http://model-endpoint:8001/v1` 但**无对应容器**、host 探测 `127.0.0.1:8001/v1/models` → `WinError 10061（连接被拒绝）`、`self-hosted-model` 为 MockLLM 规则引擎代表（非真实权重）；`evidence/llm_candidate_eval.json` **仅 `self-hosted-demo` stub（write_op_pass=true，cases=1）**，先前全量 24 用例报告**不在磁盘**。
  - §2：能力矩阵/写门控＝**PASS（逻辑/运行时正确）**——受限环境空白名单/缺报告 → `frozenset()` fail-closed；`capability_ok(self-hosted-demo)=True`、`self-hosted-model=False`；非白名单拒写转人工；`tests/test_llm_endpoint_gate.py` **21 passed**。⚠️ 但可写模型跑在 `LLM_BACKEND=mock`，**不构成真实模型能力证明**；`verify_capability_matrix_t2.py` 因报告缺 `self-hosted-model` 有 **4 项 FAIL**（属报告完整性，非逻辑缺陷）。
- **判定**：**真实模型评测＝`❌ BLOCKED`**（需自托管真实权重端点 + 重跑评测 + `write_op_pass=true` 报告）；**能力矩阵/写门控＝`✅ PASS`**（但非真实模型能力证明）。
- **解锁条件**：见 `LLM_GATEWAY_ACCEPTANCE.md` §1.3（部署真实自托管端点 → `evaluate_models.py` → 白名单 `HIGH_CONFIDENCE_MODELS` + 报告 + 端点可用）。

### A11 7 天连续观察 —— `待外部输入（需先切 live 并观察）`
- **标准定义**：切 live 后连续观察 ≥7 天，看跨租户/重复/回调/介入率/对账/错误率/备份；异常立即关 live。
- **证据占位**：`SHADOW_TO_LIVE_GATE.md` S6；`deploy/OPS_RUNBOOK.md`§5（每日备份恢复演练）；`deploy/observability/evidence/`；`[substrate]` `deploy/records/DEPLOYMENT_SUMMARY-20260904-030903.md`§二/§五（多处未实测/待观察）。
- **判定口径**：切 live 后 ≥7 天连续观测记录 + 每日恢复演练 + 告警无异常。**观察期未开始**，非 t1–t5 可产。

---

## 三、整体结论 —— **待定（需 t1–t5 证据回填后定稿）**

> 按 captain 顺序硬约束，整体结论**只有在读到 t1–t5 真实生产证据后才能定稿**。以下为**草稿预判（非最终）**。

**草稿预判（倾向，待 t1–t5 证据确认）**：
- 能力/安全/合规侧（A2、A3、A4、A6、A7）`[substrate]` preview/沙箱层已验证充分，**预测**回填后大概率 `✅/⚠️`；但须 T2/T3 在**生产栈**确证方可定稿。
- 生产专属 A8/A9（独立栈/受信 CA）`[substrate]` 当前仅 preview/自签名，**预测**在 T1 到位后转 `✅` 否则 `❌`；待 T1。
- A1（真实租户书面确认）、A11（7 天观察）为**真实外部依赖**，业务方签署/切 live 观察前恒未满足（确定性）。
- A5（真实资金）、A10（真实权重模型）依赖真实渠道/真实模型，`[substrate]` 证明的是沙箱/契约级，**非真实资金/权重**。
- **因此草稿整体预判**：**系统能力基线已达（A2/A3/A4/A6/A7），但正式生产上线（A1/A5/A8/A9/A10/A11）尚未闭环**；**倾向暂缓放量**，待外部/生产专属前置闭环后转（有条件）GO。**最终结论待 t1–t5 回填后由本文件 + `GO_NO_GO.md` 定稿。**

### 诚实标注（本机不可闭环项 + 所需输入）
| 项 | 为何不可本机闭环 | 所需输入 |
|----|------------------|----------|
| A1 真实租户书面确认 | 签署须真人/业务方 | 真实租户签署确认函 + 白名单注入 + 成员/角色授予 |
| A5 真实资金链路 | 仅沙箱网关 | 真实资金渠道联调 + 对账重跑 |
| A8 生产隔离 | 无独立 production 栈 | T1 部署独立生产栈 + 隔离核验记录（待回填） |
| A9 受信 TLS | 自签名证书 | 受信 CA 证书链 + 复验（待 T1 回填） |
| A10 真实权重模型 | 端点为规则引擎 | 真实 vLLM/Ollama 端点 + 重跑评测（待 T5 回填） |
| A11 7 天观察 | 观察期未开始 | 切 live 后 ≥7 天连续观测记录 |

---

## 四、定稿流程（t1–t5 回填后执行）
1. 读取 `evidence/prod-go-live/{deploy-engineer,security-auditor,test-runner,observability-engineer,acceptance-engineer}/` 产出。
2. 逐条把 `待 tX 证据` 替换为 `✅/⚠️/❌`，写入实际证据路径与数值。
3. 对 A1/A11（外部依赖）按是否已有真实回传/观察更新；A5/A10 视真实渠道/真实模型接入情况更新。
4. 更新整体结论，并与 `GO_NO_GO.md` 整体裁决同步定稿。
5. 若某角色交付缺失/受阻，`send_message` 通知 Captain；不自行臆断。

## 五、签名
- 起草：`release-manager`（t6）· **待定稿**
