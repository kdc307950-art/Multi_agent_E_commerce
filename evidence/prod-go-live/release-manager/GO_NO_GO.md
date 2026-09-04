# 正式生产上线 Go/No-Go 清单（骨架 · 待 t1–t5 证据回填后定稿）

> **产出国角色**：release-manager（发布经理）· prod-go-live 团队 · 任务 t6
> **版本状态**：**骨架 + 列定义 + 证据引用占位**。**整体裁决与多数门禁结论未定稿**；
> 已按 **T5（acceptance-engineer）实测**定稿 **G5 / G6**（其余门禁 G1–G4、G7–G12、G15 待对应 tX 证据回填；G13/G14 为外部输入）。
> **顺序硬约束（captain 明示）**：Go/No-Go 清单与验收结论**只能在读到 t1/t2/t3/t4/t5 的真实证据产出后定稿**。
> 因此：
> - 每个门禁的**结论列（✅实测|⚠️部分|❌BLOCKED）**必须基于**真实 t1–t5 证据**（写入 `evidence/prod-go-live/<role>/`）；
>   证据未到者一律标 **`待 tX 证据，勿假定`**，**绝不因“等不及”填默认值**。
> - **整体裁决（GO/有条件 GO/NO-GO）**同样基于真实 t1–t5 证据；当前为 **“待定（需 t1–t5 回填）**”，非最终裁决。
> - 仓库既有 preview/沙箱级证据（`deploy/records/*`、`deploy/drills/records/*`、`evidence/*`、`deploy/observability/evidence/*`）
>   仅作为**系统能力基线参考**，**不作**生产 go-live 门禁的定稿依据。
> - 定稿动作：t1–t5 回填后，从 `evidence/prod-go-live/<role>/` 读取各自产出，逐门禁刷新本清单并定稿。
> - 例外说明：G13（首批真实租户书面确认）与 G14（7 天观察）**不来自 t1–t5**，而是**真实外部依赖**
>   （需业务方签署 / 需切 live 后实际观察）；两者在当前阶段即为未满足，所需输入已单独写明。

- 记录 ID：`GONOGO-PROD-<ts>`（定稿时补 ts）
- 骨架时间：2026-09-04（t6 起草）
- 目标环境：`production`（正式生产上线，**非 preview**）
- 依据模板：`deploy/records/PRODUCTION_DEPLOYMENT_record-TEMPLATE.md`、`deploy/records/CANARY_TENANTS-TEMPLATE.md`、
  `deploy/records/SCALEUP_APPROVAL-TEMPLATE.md`、`deploy/drills/records/TEMPLATE-rollback.md`、
  `deploy/drills/records/TEMPLATE-pg-backup-restore.md`、`deploy/OPS_RUNBOOK.md`、`deploy/records/PRODUCTION_ACCEPTANCE_VERIFICATION.md`

---

## 一、列定义（结论判定口径，统一）

**结论列取值**：
- `✅ 实测`：已在本机读到**该门禁对应的真实生产证据**，逐点验证通过。
- `⚠️ 部分`：已读到部分真实证据，但存在**明确未闭环/需部署期复验**的项。
- `❌ BLOCKED`：读到**明确不满足或明确缺失**的证据，需要外部/前置输入。
- `待 tX 证据，勿假定`：该门禁的**生产证据尚未回填**（t1–t5 进行中），**当前不下结论**；不得用 preview/默认值代替。
- `待外部输入`：该门禁**依赖非 t1–t5 的真实外部输入**（如业务方书面确认、真实资金渠道、7 天观察期），在本机无法产生。

**证据来源约定**：
- `T<t>`＝当前 prod-go-live 团队角色产出，目录约定 `evidence/prod-go-live/<role>/`：
  - `T1`=deploy-engineer / `T2`=security-auditor / `T3`=test-runner / `T4`=observability-engineer / `T5`=acceptance-engineer。
- `[substrate]`＝仓库既有 preview/沙箱级证据（仅作能力基线参考，**非定稿依据**），示例：`[substrate] evidence/sec_dynamic_acceptance-20260904-030405.md`。

---

## 二、门禁总表（结论列＝待回过填）

| # | 门禁 | 要求 | 结论 | 依据来源 | 责任 |
|---|------|------|:---:|:---:|------|
| G1 | **代码 tag（生产发布基线）** | 正式生产发布 tag + 干净树 + 完整 commit 哈希；配置版本可审计 | `待 T1 证据，勿假定` | `evidence/prod-go-live/deploy-engineer/` | deploy-engineer |
| G2 | **镜像 digest（可复现构建）** | 记录镜像 digest；内容/配置确定性；冷构建字节级可复现 | `待 T1 证据，勿假定` | `evidence/prod-go-live/deploy-engineer/` | deploy-engineer |
| G3 | **RLS 动态验证** | FORCE RLS + NOBYPASSRLS；跨租户读/写/改 tenant_id 零可见/被拒 | `待 T2 证据，勿假定` | `evidence/prod-go-live/security-auditor/` | security-auditor |
| G4 | **回调并发/幂等** | 同 operation/同幂等键并发回调不重复执行（provider.submit 恰 1 次） | `待 T3 证据，勿假定` | `evidence/prod-go-live/test-runner/` | test-runner |
| G5 | **真实模型评测 + 能力矩阵** | 评测驱动白名单；write_op_pass 交集；非白名单写 fail-closed | `❌ BLOCKED`（真实模型评测：无真实权重端点）+ 能力矩阵/写门控 `✅ PASS` | `evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md`(+`LLM_GATEWAY_EVIDENCE.json`) | acceptance-engineer |
| G6 | **真实网关沙箱验收** | self-hosted 网关 live 链路可跑通且不重复执行/可对账 | `⚠️ 部分`（代码/测试级 `✅ PASS`；生产网关沙箱可用性＝**部署期执行项，未部署**） | `evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §3 | acceptance-engineer |
| G7 | **恢复/备份演练** | RPO≤15min；RTO≤60min；加密备份+最小权限角色+异机副本 | `待 T3 证据，勿假定` | `evidence/prod-go-live/test-runner/` | test-runner |
| G8 | **告警演练** | 规则可触发/可定位/可关闭；关键指标有界打点 | `待 T4 证据，勿假定` | `evidence/prod-go-live/observability-engineer/` | observability-engineer |
| G9 | **生产密钥/库/备份独立性** | 生产独立密钥/数据卷/备份位置，不复用 preview | `待 T1 证据，勿假定` | `evidence/prod-go-live/deploy-engineer/` | deploy-engineer |
| G10 | **受信 TLS** | 生产使用受信 CA 证书链（非自签名） | `待 T1 证据，勿假定` | `evidence/prod-go-live/deploy-engineer/` | deploy-engineer |
| G11 | **人工审批强制** | 退款/退货/改址必经唯一 human_approval，无 direct 绕过 | `待 T2 证据，勿假定` | `evidence/prod-go-live/security-auditor/` | security-auditor |
| G12 | **只读+shadow 先于 live** | 先只读与 shadow 观察，再切换 live；切换受控 | `待 T1/T5 证据，勿假定` | `evidence/prod-go-live/deploy-engineer/`+`acceptance-engineer/` | release-manager |
| G13 | **首批真实租户书面确认** | 首批真实租户书面确认函签署 + 白名单注入 + 成员/角色授予 | `待外部输入（业务方签署）` | ➜ `CANARY_TENANT_CONFIRMATION_TEMPLATE.md` | release-manager / 业务方 |
| G14 | **7 天连续观察** | 切换 live 后连续观察 ≥7 天 | `待外部输入（需先切 live 并观察）` | ➜ `SHADOW_TO_LIVE_GATE.md` S6 | observability-engineer |
| G15 | **对账可核实** | 非终态收口；不一致/不可核实 → 转人工；mismatch 计数可观测 | `待 T3 证据，勿假定` | `evidence/prod-go-live/test-runner/` | test-runner |

---

## 三、逐项详表（骨架：要求 + 证据引用占位 + 待回填口径）

> 下列每项为**证据引用占位**（预期读取路径 + 判定口径）；**结论在 tX 回填后填写**。为避免误导，
> 每项同时标注 `[substrate]`（既有 preview 能力基线，供 tX 回填时交叉；**不作为定稿依据**）。

### G1 代码 tag（生产发布基线）
- **要求**：正式生产发布 tag（干净工作树）+ 完整/短 commit 哈希 + `DEPLOY_CONFIG_VERSION` 可审计；绝不从脏工作区构建。
- **证据读取（待回填）**：`evidence/prod-go-live/deploy-engineer/<T1 产出>`（发布 tag/commit/deploy.sh 输出）。
- **判定口径**：是否在**独立生产栈**、干净 tree、正式发布 tag，且 commit 含 FOUND-SOFTWARE-1 修复（`7941246` 及之后）。
- **[substrate]**（非定稿）：`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-030903.md` §3（tag `baseline-prod-4`，commit `03819a8`；但该 tag 早于 `7941246`，非当前生产准备 HEAD）。

### G2 镜像 digest（可复现构建）
- **要求**：记录 repo digest/image id；内容与配置确定性；冷构建字节级可复现。
- **证据读取（待回填）**：`evidence/prod-go-live/deploy-engineer/<T1 产出>`（digest 列表 / 复现性结论）。
- **判定口径**：内容/配置确定性 +（如可行）冷构建字节级一致，或如实承认构建器级非确定残余风险。
- **[substrate]**（非定稿）：`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-030903.md` §3（base 钉 digest+lock+git archive+SOURCE_DATE_EPOCH；冷构建两遍 digest 不一致→BuildKit 级非确定）。

### G3 RLS 动态验证
- **要求**：`FORCE ROW LEVEL SECURITY` + `app_runtime NOBYPASSRLS`；B1–B5（跨租户读/写、改 tenant_id、非超管、未设作用域）零可见/被拒。
- **证据读取（待回填）**：`evidence/prod-go-live/security-auditor/<T2 产出>`（生产数据面 RLS 动态结果）。
- **判定口径**：在**独立生产栈**数据面上复跑 B1–B5；确证跨租户零可见/写拒/改 tenant_id 被拒。
- **[substrate]**（非定稿）：`evidence/pg_rls_bypass_probe.json`（B1–B5 conclusion=true，preview 一次性测试库）；`tests/test_rls_bypass.py -m postgres`（5 passed）。

### G4 回调并发 / 幂等
- **要求**：同业务操作/幂等键并发回调不重复执行；`UNIQUE(tenant_id,operation_id)`；单执行守卫；终态封闭；同 nonce 重投→replay。
- **证据读取（待回填）**：`evidence/prod-go-live/test-runner/<T3 产出>`（并发压测/沙箱并发/恢复一致性）。
- **判定口径**：在**生产栈**上同 operation 并发 → provider/回调恰好一次；重复审批收敛；跨租户幂等键互不覆盖。
- **[substrate]**（非定稿）：`evidence/sandbox_concurrency_stress.json`（N=256→provider.submit=1）；`evidence/concurrency_stress.json`；`tests/test_sandbox_concurrency_guard.py`。

### G5 真实模型评测 + 能力矩阵 —— `❌ BLOCKED（真实模型评测）`＋能力矩阵/写门控 `✅ PASS`
- **要求**：评估驱动白名单（`base ∩ write_op_pass=true`）；非白名单/未评测模型写 fail-closed 转人工；受限环境缺报告即拒绝；**真实权重模型评测通过**。
- **证据（T5 实测，权威）**：
  - `evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §1：**真实模型评测＝BLOCKED**——preview 运行时 `LLM_BACKEND=mock`（`LLM_MODEL=self-hosted-demo`）、`LLM_BASE_URL=http://model-endpoint:8001/v1` 但**无对应容器/服务**、host 探测 `http://127.0.0.1:8001/v1/models` → `WinError 10061（连接被拒绝）`、`docker ps` 无任何 `model-endpoint` 容器；`self-hosted-model` 为 `src/llm/self_hosted_server.py` 复用的 **MockLLM 规则引擎（representative）**，非真实权重模型。
  - `evidence/llm_candidate_eval.json`（当前挂载到 preview 容器）**仅 `self-hosted-demo`：`write_op_pass=true`，cases=1（最小 stub）**；先前 `LLM_ENDPOINT_EVAL_REPORT.md` 所述"全量 24 用例 + `self-hosted-model`"**已不在磁盘**（被覆写为 stub）。
  - §2 能力矩阵/写门控＝**PASS（逻辑/运行时正确）**：`resolve_high_confidence_models` 在 preview+白名单 `self-hosted-demo`+报告(含) → `{self-hosted-demo}`（`capability_ok(self-hosted-demo)=True`、`self-hosted-model=False`）；受限环境空白名单/缺报告 → `frozenset()` fail-closed；非白名单拒写转人工；`tests/test_llm_endpoint_gate.py` **21 passed**。⚠️**但可写模型 `self-hosted-demo` 跑在 `LLM_BACKEND=mock`（规则引擎）上，不构成真实模型能力证明**；`verify_capability_matrix_t2.py` 因报告缺 `self-hosted-model` 有 **4 项 FAIL**（属报告完整性/一致性，非逻辑缺陷）。
- **判定**：**真实模型评测 ＝ `❌ BLOCKED`**（需自托管真实权重端点并重跑评测）；**能力矩阵/写门控 ＝ `✅ PASS`**（但非真实模型能力证明）。
- **解锁条件（t5 明示）**：部署真实自托管模型端点（vLLM/Ollama 或自管 OpenAI 兼容服务）→ 配置 `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` + 写入 `LLM_ALLOWED_HOSTS` → `LLM_BACKEND=openai_compatible` → 跑 `scripts/evaluate_models.py`（`--base-url` 显式传真实端口，默认 8021 为误）产出含该 model id 且 `write_op_pass=true` 的报告 → `HIGH_CONFIDENCE_MODELS` 显式列出该 id。

### G6 真实网关沙箱验收 —— `⚠️ 部分`（代码/测试级 `✅ PASS`；生产网关沙箱可用性＝部署期执行项，未部署）
- **要求**：self-hosted 网关（完全自托管）live 链路可跑通且**不重复执行、可对账、金额异常→转人工、无 provider→fail-closed**；**生产网关沙箱可用**。
- **证据（T5 实测，权威）**：
  - `evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §3：**代码/测试级 ✅ PASS**——服务端幂等 `UNIQUE(tenant_id,idempotency_key)`+`ON CONFLICT DO NOTHING`+读回；跨租户同幂等键孤立；回调验签（`EXECUTION_CALLBACK_HMAC_SECRET`，`tests/test_callback_security_log.py` 6 passed）；重放/终态封闭；compensate 派生稳定 `reversal_id`；reconcile → `MISMATCHED`→`HUMAN_HANDOFF`；`gateway_unconfigured` fail-closed；`EXECUTION_MODE=shadow` 不触真实资金；`scripts/sandbox_concurrency_stress.py`（sqlite N=256 全绿：`provider.submit` 恰 1 次）；`tests/test_sandbox_e2e_flow.py` 14 passed（真实 uvicorn 临时端口）。
  - **生产网关沙箱可用性＝部署期执行项/未部署**：preview 运行时 `EXECUTION_PROVIDER=mock`、`GATEWAY_BASE_URL` 未配置、无生产网关沙箱容器；`sandbox_gateway` 仅在测试 fixture 临时拉起。
- **判定**：**`⚠️ 部分`**——网关沙箱链路（代码/测试级，含 256 并发不重复执行）已确证；**但"生产网关沙箱可用"＝部署期执行项/未部署**，需部署内网 `sandbox_gateway` → 注入 `GATEWAY_BASE_URL`/`GATEWAY_API_KEY` → `EXECUTION_PROVIDER=sandbox_http` → 端到端复跑。
- **解锁条件（t5 明示）**：部署生产网关沙箱 + 注入配置 + `EXECUTION_PROVIDER=sandbox_http` + shadow-only 提交复跑。

### 附：EXECUTION_MODE 状态（t5 确认）
- `evidence/prod-go-live/acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md` §4：preview 运行时 `EXECUTION_MODE=shadow`（`_run_shadow` 只生成待执行记录+模拟回执 `simulated=true`，**不调用任何真实资金接口**）；**本次不切换 live**；live 前置清单已在 §4.2 列出（真实模型端点+评测、生产网关沙箱、业务负责人确认、首少量租户+只读+shadow+7 天观察、回滚/暂停/接管）。

### G7 恢复 / 备份演练
- **要求**：RPO≤15min、RTO≤60min；**加密备份**（aes-256-cbc+PBKDF2+SHA-256）+ **最小权限 backup_role** + **异机副本** + 实测 RPO/RTO + marker 校验。
- **证据读取（待回填）**：`evidence/prod-go-live/test-runner/<T3 产出>`（恢复一致性/备份恢复演练 RPO/RTO）。
- **判定口径**：在**生产独立备份位置**上复跑加密备份→恢复→校验，RPO/RTO 实测值达标。
- **[substrate]**（非定稿）：`deploy/drills/records/drill-pg-encrypted-restore.json`（RTO≈0.093s、RPO≈0.877s）；`deploy/drills/records/DR-20260904030211-pg-backup-restore.md`；`evidence/recovery_consistency.json`（PASS）；`evidence/dr_encrypted_backup.json`、`evidence/dr_backup_role_least_privilege.json`。

### G8 告警演练
- **要求**：规则可触发/可定位/可关闭；关键指标有界打点；RPO/RTO/错误率/审批耗时/对账/人工介入/安全拒绝可计量。
- **证据读取（待回填）**：`evidence/prod-go-live/observability-engineer/<T4 产出>`（运行时告警触发/定位/关闭 + 关键指标 + shadow/7 天监控方案）。
- **判定口径**：是否补全有界指标打点；是否在运行时 Prometheus 上做一次端到端触发/定位/关闭复验。
- **[substrate]**（非定稿）：`deploy/observability/evidence/ALERT_RULES_VERIFICATION.md`（14/15 可触发/关闭，求值器等价证据；RPO/RTO 规则写路径未回填、ComponentRestartDetected 未打点）；`deploy/observability/evidence/DR_GAUGE_BACKFILL.md`。

### G9 生产密钥 / 库 / 备份独立性
- **要求**：生产使用**独立生产密钥 / 独立数据库 / 独立备份位置 / 独立证书**，不复用 preview。
- **证据读取（待回填）**：`evidence/prod-go-live/deploy-engineer/<T1 产出>`（独立生产栈 + 独立性核验：产证/密钥/库/备份不复用 preview）。
- **判定口径**：是否有独立 Compose 项目/数据卷/密钥集/备份位置/证书，且核验未复用 preview。
- **[substrate]**（非定稿）：`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-030903.md` §0/§1/§4（**当前仅 preview；`production` 未部署**）——仅提示，不作为定稿。

### G10 受信 TLS
- **要求**：生产使用**受信 CA 证书链**（非自签名），并经 `nginx -t`/端到端 HTTPS 复验。
- **证据读取（待回填）**：`evidence/prod-go-live/deploy-engineer/<T1 产出>`（受信 CA 证书链 + 复验记录）。
- **判定口径**：生产证书链由受信 CA 签发，`https://host/` 与 `https://host/api/openapi.json` 经受信链校验 200。
- **[substrate]**（非定稿）：`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-030903.md` §7（自签名 CN=preview.local；“生产受信 CA：未配置”）——仅提示，不作为定稿。

### G11 人工审批强制
- **要求**：退款/退货/改址必经唯一 `human_approval`；无 direct 绕过；仅同租户 admin/approver 可审批；二次确认；拒绝即终止。
- **证据读取（待回填）**：`evidence/prod-go-live/security-auditor/<T2 产出>`（审批归属/绕过/幂等/隔离取证）。
- **判定口径**：在**生产数据面**上确证唯一 human_approval、越权 403、跨租户 404、缺二次确认 422、拒绝不执行、重复审批收敛。
- **[substrate]**（非定稿）：`evidence/sec_dynamic_acceptance-20260904-030405.md` §一/§二（无审批绕过 ✅）；`tests/test_approval_idempotency.py`（16/16）；`tests/test_execution_engine.py`（21/21）；`src/graph/approval.py`。

### G12 只读 + shadow 先于 live
- **要求**：先前只读与 shadow 观察，人工复核/对账/业务负责人确认后再对指定租户切 live；切换受控、可回退。
- **证据读取（待回填）**：`evidence/prod-go-live/deploy-engineer/<T1 产出>`（部署模式配置）+ `evidence/prod-go-live/acceptance-engineer/<T5 产出>`（live 前置）。
- **判定口径**：当前 `EXECUTION_MODE=shadow`（未切 live），且 `SHADOW_TO_LIVE_GATE.md` 流程已定义；正式切 live 是否已发生/是否受控。
- **[substrate]**（非定稿）：`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-030903.md` §4（`EXECUTION_MODE=shadow`/`mock`）；`evidence/prod-go-live/release-manager/SHADOW_TO_LIVE_GATE.md`（S0–S6 流程）。

### G13 首批真实租户书面确认 —— 待外部输入（业务方签署）
- **要求**：首批仅放行经书面确认的真实租户；书面确认函签署 + `LAUNCH_ALLOWED_TENANTS` 白名单注入 + 成员/角色授予 + 审计。
- **模板**：`evidence/prod-go-live/release-manager/CANARY_TENANT_CONFIRMATION_TEMPLATE.md`。
- **判定口径**：真实租户（业务方）**书面确认函未回传前**，本门禁不满足（本仓库不代签）。
- **[substrate]**（非定稿）：`deploy/records/CANARY_TENANTS-20260904-030903.md`（现 `TENANT-A/B` 为演示/seed 租户，非真实租户；`DEMO_SEED_ENABLED=false`）——仅提示。

### G14 7 天连续观察 —— 待外部输入（需先切 live 并观察）
- **要求**：切换 live 后连续观察 ≥7 天，重点看跨租户拒绝、重复执行、回调失败、人工介入率、对账差异、错误率、备份结果。
- **证据读取（待回填/待执行）**：`evidence/prod-go-live/observability-engineer/<T4 产出>`（7 天监控方案）+ 切 live 后每日观测记录（`deploy/OPS_RUNBOOK.md` §5）。
- **判定口径**：≥7 天连续观测数据 + 每日备份恢复演练记录 + 告警无异常。

### G15 对账可核实
- **要求**：非终态收口；不一致/不可核实→转人工；mismatch 计数可观测。
- **证据读取（待回填）**：`evidence/prod-go-live/test-runner/<T3 产出>`（对账演练）。
- **判定口径**：后台对账实际运行 + mismatch 计数可观测 + `reconcile_mismatch_total` 有界打点。
- **[substrate]**（非定稿）：`evidence/COMPENSATION_RECONCILIATION_REPORT.md`；`deploy/drills/records/drill-api-reconcile.json`（D6 入口 200+审计；mismatch 计数后台未实测）。

---

## 四、整体裁决 —— **待定（需 t1–t5 证据回填后定稿）**

> 按 captain 顺序硬约束，整体裁决（GO/有条件 GO/NO-GO）**只有在读到 t1–t5 真实生产证据后才能定稿**。
> 以下为**预判/草稿方向（非最终裁决）**，供队长决策参考；定稿前不得以本段作为“已定 GO/NO-GO”依据。

**草稿预判（倾向，待证据确认）**：
- **能力/安全/合规侧（A2 无审批绕过、A3 无跨租户、A4 无重复执行、A6 回滚/暂停/接管、A7 shadow 先于 live）**：`[substrate]` preview/沙箱层已验证充分，**预测**在 t1–t5 生产证据回填后大概率达 `✅/⚠️`，但**需 T2/T3 在生产栈确证**后方可定稿。
- **生产专属门禁 G9/G10（独立栈/受信 CA）**：`[substrate]` 显示当前仅 preview/自签名；**预测**在 T1 独立生产栈 + 受信 CA 到位后转 `✅`，否则 `❌`；**待 T1 证据**。
- **G13/G14（真实租户书面确认 / 7 天观察）**：真实外部依赖，**在业务方签署与切 live 观察前恒为未满足**，属确定性 BLOCKED（不因“等”改变）。
- **因此草稿整体预判**：**倾向“暂缓放量 / 有条件 GO 前置于外部输入完成”**；一旦业务方确认（G13）+ 独立生产栈（G9）+ 受信 CA（G10）+ 真实资金/模型（G5/G6）+ 7 天观察（G14）全部闭环，方可转 GO。**最终裁决待 t1–t5 证据回填后由本清单定稿。**

---

## 五、定稿流程（t1–t5 回填后执行）

1. 从 `evidence/prod-go-live/deploy-engineer/`（T1）、`security-auditor/`（T2）、`test-runner/`（T3）、`observability-engineer/`（T4）、`acceptance-engineer/`（T5）读取各自产出。
2. 逐门禁（G1–G15）把 `待 tX 证据` 替换为 `✅ 实测 / ⚠️ 部分 / ❌ BLOCKED`，并写入实际证据路径与关键数值。
3. 对 G13/G14 保持外部输入判定（若业务方已回传确认函/已观察满 7 天则转 `✅`）。
4. 依据全部门禁更新整体裁决（GO/有条件 GO/NO-GO），并同步 `FINAL_ACCEPTANCE.md` 结论。
5. 若某角色交付缺失或受阻，`send_message` 通知 Captain 协调；不自行臆断。

## 六、签名
- 起草：`release-manager`（t6）· **待定稿**
