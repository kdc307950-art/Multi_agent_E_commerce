# 阶段4/6 就绪与缺口评估（真实模型 · 首批真实租户）

> **归属**：security-auditor · `rc3-prod-readiness`
> **口径**：基于 **T1.2 核验后的发布证据**，仅对「阶段4（真实自托管模型）」与「阶段6（首批真实租户 Shadow）」做**就绪度 + 缺口**评估。
> **铁律**：**绝不伪造评测通过、绝不伪造租户已确认**。凡外部输入未到位，一律如实标注 `BLOCKED（待外部输入）`；凡「mock/规则引擎评测」「示例租户」一律不得当作真实达标证据。
> **数据来源**：`evidence/llm_candidate_eval.json`、`acceptance-engineer/LLM_GATEWAY_ACCEPTANCE.md`、`observability-engineer/OBSERVABILITY_REPORT.md`、`release-manager/CANARY_TENANT_CONFIRMATION_TEMPLATE.md`、`deploy/records/tenants.json`、`deploy/.env.production`、`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md`。

---

## 0. 结论速览

| 阶段 | 目标 | 当前就绪度 | 状态 |
|---|---|---:|---|
| **阶段4** | 真实自托管模型（`self-hosted-model`）接入 + `write_op_pass=true` 评测报告 + 能力矩阵放行 | **0%** | 🔴 **BLOCKED（真实端点缺失）** |
| **阶段6** | 首批真实租户 Shadow（书面确认 + 白名单 + 登录凭据 + 成员/角色 + 审计） | **0%** | 🔴 **BLOCKED（业务方外部输入缺失）** |

> 两阶段均属**外部依赖未到位**，现状为**设计上的 fail-closed 安全默认态**（无租户放行、无写模型授权、登录一律拒绝）。这不是「缺陷」，而是**尚未满足前置条件的正常阻断**；但在断言「阶段4/6 就绪」**之前**，必须补齐下述外部输入，且**不可**以当前任何 mock/示例证据先行动作。

---

## 一、阶段 4：真实自托管模型接入

### 1.1 现状证据（可溯源）

| 证据 | 内容 | 指向 |
|---|---|---|
| `evidence/llm_candidate_eval.json`（整文件） | `{"self-hosted-demo": {"write_op_pass": true, "cases": [{"case_id": "writeop-refund-whitelist", "passed": true}]}}`（仅 1 条用例的最小 stub） | 报告**只含 `self-hosted-demo`**，**不含权威目标模型 `self-hosted-model`** |
| `LLM_GATEWAY_ACCEPTANCE.md` §1.1 row「LLM_BACKEND」 | **`mock`**（preview 未走 `openai_compatible`，用本地确定性规则引擎） | 非真实端点 |
| §1.1 row「LLM_MODEL」 | `self-hosted-demo`（preview 运行态） | true endpoint 未在跑 |
| §1.1 端点探测 | `http://127.0.0.1:8001/v1` → `ok=false`，`WinError 10061（连接被拒绝）`；端口 8001 **无监听** | 端点不可达 |
| §1.1 Docker ps | 无任何 `model-endpoint`/`sandbox`/`gateway` 容器 | 无真实模型容器 |
| §1.1「self-hosted-model 的本质」 | `src/llm/self_hosted_server.py` 复用 `src/llm/mock.MockLLM`（规则引擎）作「模型」，注释原文「可用真实 vLLM/Ollama 端点替换」 | 即便此前跑过，`self-hosted-model` 也**不是生产级权重模型** |
| §1.2 评测报告权威性 | 「全量 24 用例 + `self-hosted-model`」报告**不在磁盘**；已被某次 pytest `json.dump` **覆写**为上面 113 字节 stub | 权威报告被污染/丢失 |
| `deploy/.env.production` | `LLM_BACKEND=mock`、`LLM_MODEL=self-hosted-model`、`LLM_EVAL_REPORT_PATH=/app/evidence/llm_candidate_eval.json`、`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__` | 生产白名单**为空** |
| `LLM_GATEWAY_ACCEPTANCE.md` §2.2 | preview+白名单=`self-hosted-model`+报告缺该模型 → 生效白名单=`{}` → `capability_ok(self-hosted-model)=False` | 写门控 fail-closed |

**结论（阶段4）**：无真实自托管端点；「真实模型权重」不存在（`self-hosted-model` 为规则引擎代表）；权威评测报告缺失（唯一在盘的为 1 用例 stub，且权威目标 `self-hosted-model` 不在其中）。因此**当前无法产出「真实模型 `write_op_pass=true`」的可信证明**；能力矩阵交集为空 → **所有写操作 fail-closed 转人工**。= **BLOCKED**。

### 1.2 外部依赖（当前环境不具备）
- 一个**真实自托管权重模型端点**（vLLM / Ollama，或项目自管的 OpenAI 兼容服务），运行于**项目方受控内网**，非第三方托管 SaaS；能返回真实权重模型的 `/v1/models` 与 `/v1/chat/completions`。
- 该端点与 `HIGH_CONFIDENCE_MODELS`/能力门控逻辑兼容的 `write_op_pass` 评测能力（多维度、全量 24 用例）。

### 1.3 所需输入
1. 真实端点的 `LLM_BASE_URL`（内网可达、真实端口，非默认 8021 错误值）。
2. 端点真实 `LLM_API_KEY`（自托管/受控，服务端注入，不入库/镜像/日志）。
3. `LLM_MODEL`（目标模型名，权威目标=`self-hosted-model`）。
4. `LLM_ALLOWED_HOSTS` 收敛为**已批准内网站段**（如 `model-endpoint,10.0.0.0/8`）。
5. 用 `scripts/evaluate_models.py --base-url <真实端口> --model-name self-hosted-model` 产出**全量 24 用例**、`write_op_pass=true` 的评测报告，落 `evidence/llm_candidate_eval.json`（**先修会覆写该证据的测试** `tests/test_llm_endpoint_gate.py::test_whitelist_model_still_requires_approval`，否则再跑 pytest 会再次清空报告）。
6. 将真实模型 id 显式写入 `HIGH_CONFIDENCE_MODELS`；`LLM_BACKEND=openai_compatible`；`LLM_EVAL_REPORT_PATH` 指向含 `write_op_pass=true` 的报告。
7. 诚实记录：模型 id、各维评测分数、报告路径、评测日期。

### 1.4 解锁条件（全部满足后，才可宣称「真实模型评测通过」）
> 参照 `LLM_GATEWAY_ACCEPTANCE.md` §1.3：
1. 部署真实自托管权重端点（受控内网），配置 `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`，写入 `LLM_ALLOWED_HOSTS`。
2. `LLM_BACKEND=openai_compatible` → 跑 `evaluate_models.py` 产出含 `self-hosted-model` 且 `write_op_pass=true` 的**全量 24 用例**报告。
3. `HIGH_CONFIDENCE_MODELS` 显式列出该模型 id；`LLM_EVAL_REPORT_PATH` 指向该报告。
4. 修复覆写证据的测试（防止 pytest 再次清空报告）。
5. 用当前构建重建 + 真实请求（非 mock）做写操作端到端复验（切 live 前置）。

### 1.5 当前环境无法闭环的诚实结论
- 当前沙箱**无 Docker engine 可连接、无真实权重端点、无真实模型评测**；`httpx` 探测 `127.0.0.1:8001` 连接被拒；`self-hosted-model` 仅为规则引擎代表。
- 因此**本环境无法闭环阶段4**；现有 `self-hosted-demo`/1 用例 stub **不是**真实评测产物，**不可**作为「阶段4 就绪」证据。
- 任何「模型已评测通过」的表述在满足 §1.4 前都应标注 `❌ BLOCKED（待真实端点+真实评测报告）`。

---

## 二、阶段 6：首批真实租户 Shadow

### 2.1 现状证据（可溯源）

| 证据 | 内容 | 指向 |
|---|---|---|
| `deploy/.env.production` | `LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`AUTH_LOGIN_CREDENTIALS={}`、`LAUNCH_GATE_STRICT=true`、`DEMO_SEED_ENABLED=false` | 无任何放行租户；登录凭据为空表 |
| `CANARY_TENANT_CONFIRMATION_TEMPLATE.md` | 为**模板**；行 81：「在真实租户（业务方）完成签署并回传本函+附件 A/B 前，`G13 首批真实租户书面确认` 门禁判 `待外部输入（业务方签署）`＝BLOCKED 语义」；且「本仓库不代签」 | 无已签名确认函 |
| `deploy/records/tenants.json` | `_template_note`：「本文件为【示例】…**非真实**，切勿以本示例直接写入生产」；`ACME-RETAIL`/`GLOBEX-ECOM` 为示例租户 | 无真实租户 |
| `deploy/records/CANARY_TENANTS-20260904-212814.md` | 行 6：「真实租户名单系外部输入（BLOCKED_EXTERNAL）」；行 82：「示例租户一律不得放量」 | 无真实确认 |
| `deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md` | 行 55：`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`（不向外部放行）；行 92：生产未向外部放行租户 | 生产未放量 |
| `observability-engineer/OBSERVABILITY_REPORT.md` §4 | `EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`（无 live 网关）、`LAUNCH_GATE_STRICT=true`；生产栈未部署；Langfuse trace = NO-OP（`LANGFUSE_PUBLIC_KEY=`/`SECRET_KEY=` 空） | shadow+只读先于 live；观测链路就绪项缺失（§4 结论：切 live 前必须补齐 Langfuse 密钥+trace 落地验证） |
| grep「确认函」 | 全仓库未见 `CANARY-CONF-<租户ID>-<ts>.md` 实体（仅模板/引用）；仅 `SHADOW_TO_LIVE_REVIEW.md` 行 57 的 `⬜`（未勾选）占位 | **无任何已签确认函** |

**结论（阶段6）**：无真实租户书面确认函；`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`（门控拒绝所有）、`AUTH_LOGIN_CREDENTIALS={}`（登录一律拒绝）、`EXECUTION_MODE=shadow`（不触真实资金）、`EXECUTION_PROVIDER=mock`。= **BLOCKED**。同时观测链路（Langfuse trace）与生产栈运行时复验尚为部署期执行项。

### 2.2 外部依赖（当前环境不具备）
- **真实租户（业务方）**提供首批放行租户与成员/角色清单，并**书面签署确认函**（真人/业务方电子签章或手签+日期）。
- 该确认函明确：租户主体、`tenant_id`、首批成员（user_id + role，仅 `customer/agent/admin/approver`）、退款/退货/改址政策口径、审批人（本租户 admin/approver）、放量阶梯（Tier 0/1/2/3）。

### 2.3 所需输入
1. **书面确认函**（`CANARY-CONF-<租户ID>-<ts>.md`，业务方签署），含政策口径确认 + 审批人授权 + 成员/账号交接清单。
2. 真实租户 `tenant_id`（服务端认证后从 `TenantContext` 取，禁客户端指定）。
3. 每个成员的 argon2id PHC 哈希（`scripts/hash_login_credentials.py --algorithm argon2id` 生成）。
4. 受控脚本创建租户/成员并授角色（`scripts/create_bootstrapped_tenants.py --spec <tenants.json> --apply`，禁用 demo）。
5. 将真实租户 id 写入 `LAUNCH_ALLOWED_TENANTS`（`deploy/.env.production`，`check_secrets` 校验非空/非占位）。
6. 将 PHC 哈希写入 `AUTH_LOGIN_CREDENTIALS`（JSON，键 `<tenant_id>:<user_id>`）。
7. **审计留痕**：操作者、审批依据（确认函编号）、时间、结果。
8. （切 live 前）补齐观测链路就绪项：注入 `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`（自托管），用真实请求验证一次性 trace 落地，metadata 含 `tenant_id/session_id/environment`，PII 脱敏；（具备 engine 后）生产栈运行时 RLS/告警端到端复验。

### 2.4 解锁条件（全部满足后，才可进入「首批真实租户 Shadow」）
> 参照 `CANARY_TENANT_CONFIRMATION_TEMPLATE.md` §4 交接动作 + §5 观察承诺：
1. 租户方书面确认函签署（本租户 admin/approver 授权审批）。
2. 受控脚本创建租户与成员，argon2id PHC 写入 `AUTH_LOGIN_CREDENTIALS`。
3. 租户 id 写入 `LAUNCH_ALLOWED_TENANTS`（`check_secrets` 通过）。
4. 成员/角色（customer/agent/admin/approver）授予；审批人须为本租户 admin/approver。
5. 审计留痕（操作者、确认函编号、时间、结果）。
6. 进入**先只读 + shadow 观察 → 人工复核 → 对账 → 业务负责人确认 → 指定租户切 live**，切 live 后连续观察 ≥7 天；任何资金异常立即关 live 转人工（`SHADOW_TO_LIVE_GATE.md` S0–S6）。
7. 生产栈运行时 RLS/告警端到端复验 + 观测链路就绪（Langfuse trace 落地）到位。

### 2.5 当前环境无法闭环的诚实结论
- 当前**无任何真实租户书面确认函**（仅有模板/示例），`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`AUTH_LOGIN_CREDENTIALS={}`。
- 示例租户（`ACME-RETAIL`/`GLOBEX-ECOM`）与示例成员**非真实**，`tenants.json`/`CANARY_TENANTS` 明确「切勿以示例直接写入生产」。
- 因此**本环境无法闭环阶段6**；任何「真实租户已确认/已放行」表述在满足 §2.4 前都应标注 `❌ BLOCKED（待业务方书面确认函 + 凭据/成员/角色注入 + 审计）`。
- 生产仍处**设计上的 fail-closed 安全默认态**（无租户放行、无登录凭据、无写模型授权），属**安全**而非「就绪」。

---

## 三、双阶段联动依赖（阶段4 ↔ 阶段6）

| 依赖 | 说明 |
|---|---|
| 阶段6 Shadow **先于** 阶段4 可写 | `EXECUTION_MODE=shadow`（不触真实资金）可在**无真实写模型授权**时先行启动观察；`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__` → 写全转人工，Shadow 观察期不产生自动写 |
| 阶段4 是「切 live 可自动写」的**前置** | 真实租户放行后若要「指定租户切 live → 自动执行写」，必须**先**有真实模型评测授权；否则即使租户放行，写操作仍全员转人工（安全，但无自动资金路径） |
| 观测链路为两者共同前置 | 阶段6 的 7 天观察依赖可上报（Langfuse trace + Prometheus 有界指标）；当前 trace = NO-OP，为切 live 前**必须补齐**项 |
| 生产栈运行时复验 | 两阶段均依赖 `after-sales-prod` 部署后的 RLS/告警/恢复复验（当前为部署期执行项；沙箱无 engine 无法实跑） |

---

## 四、诚实结论（绝不伪造）

1. **阶段4**：`0% 就绪`。无真实自托管模型端点；`self-hosted-model` 仅为规则引擎代表；在盘评测报告为 1 用例 stub 且权威目标模型不在其中；`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__`（空）。**BLOCKED（待真实权重端点 + `write_op_pass=true` 全量 24 用例评测报告）**。当前任何「模型评测通过」表述均为**虚报**。
2. **阶段6**：`0% 就绪`。无真实租户书面确认函；`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`AUTH_LOGIN_CREDENTIALS={}`。**BLOCKED（待业务方书面确认函 + argon2id PHC 注入 + 成员/角色授予 + 审计）**。示例租户不得放量。当前任何「真实租户已确认」表述均为**虚报**。
3. **可先行、且已部分就绪的部分**（非阻断）：Shadow/只读先于 live 门控已就位（`EXECUTION_MODE=shadow`、`LAUNCH_GATE_STRICT=true`）；能力矩阵/写门控 **fail-closed 语义正确**（空名单/缺报告 → `frozenset()` → 写转人工）；日志/观测**代码已接入**（`tenant_id` 上下文、有界标签、`_FORBIDDEN_LABELS`），仅缺真实流量与密钥。
4. **审计局限**：本评估为**静态证据 + 已核验文档**比对，未实跑生产编排、未连接真实权重端点、未执行任何真实凭据/租户创建；一切「就绪」判定以**外部输入到位 + 部署期执行项完成**为前件。

---

*证据来源：`evidence/llm_candidate_eval.json`、`LLM_GATEWAY_ACCEPTANCE.md` §1/§2、`OBSERVABILITY_REPORT.md` §4/§5、`CANARY_TENANT_CONFIRMATION_TEMPLATE.md`、`deploy/records/tenants.json`、`deploy/.env.production`、`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md`、`deploy/records/CANARY_TENANTS-20260904-212814.md`、`deploy/records/SHADOW_TO_LIVE_REVIEW.md`。全程只读；未伪造评测通过/租户已确认。*
