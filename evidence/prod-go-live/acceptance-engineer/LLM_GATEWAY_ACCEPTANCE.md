# T5·LLM 网关验收报告（真实模型评测 / 能力矩阵门控 / 网关沙箱 / live 前置）

> 项目：电商售后多智能体工单系统（完全自托管）
> 团队：prod-go-live
> 验收人：acceptance-engineer（验收工程师）
> 生成时间：2026-09-04
> 验收口径：**如实核对真实外部端点可用性，绝不虚设模型 / 伪造评测通过；凡依赖真实模型 / 网关端点而未具备的，一律标注 BLOCKED 或「部署期执行项」，不填默认值。**
> 证据根目录：`evidence/prod-go-live/acceptance-engineer/`（本报告 + `LLM_GATEWAY_EVIDENCE.json`），并交叉引用 `evidence/` 下既有证据。

---

## 0. 结论前置（一句话汇总）

| 验收项 | 结论 | 说明 |
|---|---|---|
| 1. 真实模型评测 | **BLOCKED — 需自托管真实模型端点** | 当前无真实权重模型端点；preview 使用 `LLM_BACKEND=mock`；仅有的「self-hosted-model」是确定性规则引擎（MockLLM），非真实 vLLM/Ollama 权重 |
| 2. 能力矩阵 / 写门控 | **PASS（逻辑与运行时均正确）** | 评测驱动白名单、受限环境 fail-closed、非白名单拒写转人工均验证通过；⚠️但「可写」模型当前为 mock 引擎，不构成真实模型能力证明 |
| 3. 真实网关沙箱 | **PASS（代码/测试）**；**生产网关沙箱可用性 = 部署期执行项** | `sandbox_http` 服务端 SQLite 幂等 + 回调验签 + compensate/reconcile + `gateway_unconfigured` fail-closed 全部验证；preview 实际 `EXECUTION_PROVIDER=mock`，未部署生产网关沙箱 |
| 4. EXECUTION_MODE | ✅ 当前 `shadow`，**未切 live** | 真实资金未被触碰；live 前置清单见 §4 |
| 5. 自托管边界 | **PASS（无未批准第三方 SaaS）** | 端点/检索/观测均自托管或受控内网；端点网络白名单 + 日志脱敏；数据面端口未发布 |

> 关键诚实声明：**本专项范围内没有可运行的真实自托管模型端点，也没有已部署的生产网关沙箱。** 本报告给出的是「契约/逻辑/测试级」通过证据 + 真实的「缺少真实端点」标注，而不是一份「已通过真实模型评测」的虚报。

---

## 1. 真实模型评测（含端点探测）

### 1.1 当前运行时事实（探测结果，证据 `LLM_GATEWAY_EVIDENCE.json`）

**容器运行态（`after-sales-preview-api-1`，`docker inspect` 实测）：**

| 配置 | 值 | 含义 |
|---|---|---|
| `LLM_BACKEND` | **`mock`** | preview 并未走 `openai_compatible` 真实端点，而是用本地确定性规则引擎 |
| `LLM_MODEL` | `self-hosted-demo` | 模型名 |
| `LLM_BASE_URL` | `http://model-endpoint:8001/v1` | 仅配置项；无对应容器/服务在跑 |
| `LLM_ALLOWED_HOSTS` | `model-endpoint` | 端点网络白名单（评审时有效） |
| `LLM_EVAL_REPORT_PATH` | `/app/evidence/llm_candidate_eval.json` | 挂载的评测报告（只读） |
| `HIGH_CONFIDENCE_MODELS` | `self-hosted-demo` | 写操作白名单 |
| `ENV` | `preview` | 受限环境 |

**端点可用性探测（host 侧 `GET /v1/models`）：**
- `http://127.0.0.1:8001/v1` → `ok=false`，`WinError 10061（连接被拒绝）`。端口 8001 无监听。
- Docker `ps`：无任何 `model-endpoint` / `sandbox` / `gateway` 容器；无 `after-sales-prod` 栈（`docker compose ls` 仅 `after-sales-preview` + `after-sales-observability`）。

**「self-hosted-model」的本质（诚实标注）：**
`src/llm/self_hosted_server.py` 明确复用 `src/llm/mock.MockLLM`（确定性规则引擎）作为端点「模型」，注释原文「可用真实 vLLM/Ollama 端点替换」。因此即便该端点此前在 `127.0.0.1:8001` 被运行过（先前的 `llm-eval` 团队），其 `/v1/models` 返回的 `self-hosted-model` 也**不是生产级权重模型的真实输出**——它是规则引擎的代表（representative）。

### 1.2 评测报告当前状态（权威性）

| 文件 | 当前内容 | 说明 |
|---|---|---|
| `evidence/llm_candidate_eval.json`（挂载到 preview 容器） | **仅 `self-hosted-demo`：`write_op_pass=true`，cases=1**（最小 stub） | 该文件当前只有 1 条用例的 stub |
| `evidence/LLM_ENDPOINT_EVAL_REPORT.md` 所述「全量 24 用例 + `self-hosted-model`」 | **不在磁盘上** | 该全量报告已被某次 pytest 覆写为上面 113 字节的 stub（报告自身 §0/§2.3 亦有此说明） |

### 1.3 结论：真实模型评测 = **BLOCKED**

- 无真实端点（`LLM_BACKEND=mock`；端点不可达；无模型容器）。
- 不存在「真实模型权重」；「self-hosted-model」为规则引擎代表，非真实模型。
- 因此**无法**产出「真实模型 `write_op_pass=true`」的可信证明；**不得把 mock / 规则引擎评测当作真实模型评测通过**。

**解锁条件（需满足后才可宣称「真实模型评测通过」）：**
1. 部署一个**真实自托管模型端点**（如 vLLM/Ollama，或项目自管的 OpenAI 兼容服务），绑定受控内网，配置 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`，并把该主机/网段写入 `LLM_ALLOWED_HOSTS`。
2. 以 `LLM_BACKEND=openai_compatible` 指向该端点，跑 `scripts/evaluate_models.py`（注意 `--base-url` 需显式传真实端口，默认 8021 为错），产出含该 model id 且 `write_op_pass=true` 的评测报告。
3. 确认该 model id 被 `HIGH_CONFIDENCE_MODELS` 显式列出，且 `LLM_EVAL_REPORT_PATH` 指向含 `write_op_pass=true` 的报告（受限环境缺任一即 fail-closed）。
4. 诚实记录：模型 id、各维评测分数、评测报告路径、评测日期。

---

## 2. 能力矩阵 / 写门控验证

### 2.1 判定逻辑（权威解析口 `src/llm/capability.py`）

```
base   = HIGH_CONFIDENCE_MODELS 显式白名单（受限环境为空 → 空集 fail-closed；开发/测试 → DEFAULT_DEV_WRITE_MODELS）
report = 评测报告中 write_op_pass=true 的模型集合（由 llm_eval_report_path 指向）
生效白名单 = base ∩ report；受限环境无报告 → frozenset()（fail-closed）
```

### 2.2 运行时解析（preview 容器内实测，evidence JSON）

`resolve_high_confidence_models(Settings(preview 配置))`：

| 场景 | 配置 | 生效白名单 | `capability_ok` 结果 |
|---|---|---|---|
| preview + 白名单=`self-hosted-demo` + 报告(含 self-hosted-demo=true) | **实际运行态** | `{self-hosted-demo}` | `self-hosted-demo=True`，`self-hosted-model=False` |
| preview + 空白名单 | fail-closed | `{}` | 全部 False |
| preview + 白名单 + 无报告 | fail-closed | `{}` | 全部 False |
| preview + 白名单=`self-hosted-model` + 报告 | 报告缺 self-hosted-model | `{}` | `self-hosted-model=False` |
| dev + 默认 | 演示回退 | `DEFAULT_DEV_WRITE_MODELS`（gpt-4、claude-3-opus、qwen2.5-max、gpt-4-turbo） | `gpt-4=True`，`self-hosted-model=False` |

**结论**：能力矩阵逻辑与写门控语义**正确**——受限环境空白名单 / 缺报告一律 `frozenset()` 拒绝写并转人工；只有「显式白名单 ∩ 评测报告 write_op_pass=true」的模型才可写。

### 2.3 非白名单模型拒写 / 转人工（fail-closed）证据

- `tests/test_llm_endpoint_gate.py` → **21 passed**。含 `test_non_whitelist_model_write_fails_closed_via_api`（非白名单模型写操作 → `error(model_not_in_whitelist)`、`operation_id=null`、无待审批）、`test_non_whitelist_model_no_bypass_in_graph`、`test_whitelist_model_still_requires_approval`（白名单模型仍必须进入唯一 `human_approval`）、`test_capability_restricted_empty_whitelist_fails_closed`、`test_capability_restricted_requires_eval_report`、`test_built_llm_restricted_empty_whitelist_no_default_fallback`。
- `scripts/verify_llm_chain.py`：`[C] 非白名单模型退款 fail-closed`（SSE `error`、无 `approval_required`）；`[B] 端点 5xx/超时 → LLMUnavailableError`（节点层 fail-closed 转人工，不触发执行）。证据 `evidence/verify_llm_chain_evidence.json`。
- `INTENT_CONFIDENCE_THRESHOLD=0.7`：`test_low_confidence_fail_closed`（preview 运行时 `intent_threshold=0.7`）；低置信度（`conf<0.7`）→ 转人工，不自动路由/写。

### 2.4 ⚠️ 必须诚实标注的能力矩阵「名不副实」点

> preview 的「可写模型」`self-hosted-demo` 实际跑在 **`LLM_BACKEND=mock`**（规则引擎）上。也就是说：**能力矩阵门控逻辑正确且已生效，但它门控的模型当前是 mock 引擎，并非真实模型。** 这满足「先只读/shadow、再切 live」的保守姿态（`EXECUTION_MODE=shadow`，无真实资金），但**不能**据「`capability_ok=self-hosted-demo=true`」宣称「真实模型具备写能力」。

### 2.5 一处需修复/复核的项（诚实收录）

`verify_capability_matrix_t2.py`（能力矩阵独立验证脚本）复跑：**27 项断言中 4 项 FAIL**，全部集中于场景 4：
- 「证据报告包含 self-hosted-model 且 write_op_pass=true（场景4前置）」FAIL
- 「场景4：生效白名单 = {self-hosted-model}」FAIL
- 「场景4：capability_ok('self-hosted-model')=True」FAIL
- 「场景4：write_capable_models == {'self-hosted-model'}」FAIL

根因**不是**能力矩阵逻辑缺陷（场景 4b 用受控临时报告通过），而是**评估报告完整性/一致性**：磁盘上的 `evidence/llm_candidate_eval.json` 只含 `self-hosted-demo` 的 stub，不再含 `self-hosted-model` 的全量报告。因此场景 4（以 `self-hosted-model` 白名单 + 真实报告为前提）前置校验不成立。**建议**：在接入真实模型并重新评测后，用 `scripts/evaluate_models.py --base-url <真实端点>` 重新生成覆盖 `self-hosted-model`（或真实模型 id）的全量报告，再复跑该脚本确认全 PASS。

---

## 3. 真实网关沙箱验收

### 3.1 代码/测试级验证（全部通过）

| 验收点 | 证据 | 结果 |
|---|---|---|
| 服务端幂等：`UNIQUE(tenant_id, idempotency_key)` + 事务内 `INSERT ... ON CONFLICT DO NOTHING` + 读回 | `src/execution/sandbox_gateway.py`（`GatewayStore.submit`）；`tests/test_sandbox_http_provider.py::test_gateway_store_server_side_idempotent_single_row` | PASS：同租户同键重复提交返回同一 `external_txn_id`，`count_txns==1`，进程重启仍单行 |
| 跨租户同幂等键互不覆盖（租户级幂等） | `test_gateway_store_cross_tenant_same_key_isolated`、`test_cross_tenant_same_key_no_overwrite` | PASS：各租户各 1 行，amount 独立，跨租户 `query` → `not_found` |
| 签名回调验证（`EXECUTION_CALLBACK_HMAC_SECRET`） | `src/execution/engine.py::apply_callback` → `verify_hmac_signature`；`tests/test_callback_security_log.py`（6 passed）、`tests/test_sandbox_e2e_flow.py` | PASS：坏签名 → `signature_invalid`；验签通过才应用；审计 `execution.callback.confirmed` |
| 重放保护 / 终态封闭 | `test_sandbox_e2e_flow.py`（同 nonce 重投→`replay`；终态后新 nonce→`terminal_locked`） | PASS：防重复扣款/退款 |
| compensate（回滚）路径 | `src/execution/engine.py::compensate/_settle_after_dispatch_failure`；`test_compensate_derives_stable_reversal_id` | PASS：`(tenant, idempotency_key, execution_id)` 派生稳定 `reversal_id`，重放一致；补偿失败→`HUMAN_HANDOFF` |
| reconcile（对账）路径 | `engine.py::reconcile`；`tests/test_execution_engine.py`（21 passed，含 overdue）；`COMPENSATION_RECONCILIATION_REPORT.md` | PASS：外部不可达/未知/金额不符/缺外部队列号一律 `MISMATCHED` → `HUMAN_HANDOFF`，绝不静默当成功 |
| `gateway_unconfigured` fail-closed（sandbox_http 缺 base_url） | `src/execution/provider.py::build_provider`；`tests/test_sandbox_http_provider.py::test_build_provider_sandbox_http_missing_base_url_fail_closed` | PASS：抛 `ProviderError("gateway_unconfigured")`，绝不回退 mock |
| `EXECUTION_MODE=shadow` 不触真实资金 | `engine.py::_run_shadow`（`receipt.simulated=true`、`"未调用真实资金接口"`）；preview 运行时 `EXECUTION_MODE=shadow` | PASS |
| 高并发不重复执行 | `scripts/sandbox_concurrency_stress.py`、`evidence/sandbox_concurrency_stress.json`（sqlite N=256 全绿：`provider.submit` 恰 1 次、execution record=1、`errors={}`） | PASS（红线级确证） |

**真实 HTTP 沙箱链路**：`tests/test_sandbox_e2e_flow.py` → **14 passed**（真实 uvicorn 起 `create_sandbox_gateway_app` 于临时端口，Service 端 SQLite 持久化幂等，全程不触真实资金）。

### 3.2 生产网关沙箱可用性 = **部署期执行项 / 未部署**

- preview 运行时 `EXECUTION_PROVIDER=mock`、`GATEWAY_BASE_URL` 未配置（容器 env 无该变量）。
- **无**生产网关沙箱容器/服务在跑；`sandbox_gateway` 仅在测试 fixture 中临时拉起（临时端口 + 内存/临时 SQLite）。
- 因此：**「生产网关沙箱可用性」暂不能以「已部署且可连」标注**；本专项确认其代码与测试路径正确。若要 assert「生产网关沙箱可用」，需：部署 `src/execution/sandbox_gateway.py` 为内网服务 → 注入 `GATEWAY_BASE_URL` / `GATEWAY_API_KEY`（`SANDBOX_GATEWAY_API_KEY`）→ `EXECUTION_PROVIDER=sandbox_http` → 端到端复跑 `test_sandbox_e2e_flow.py` + 对真实 gateway 做一次 shadow-only 提交（不触资金）。

---

## 4. EXECUTION_MODE=shadow 与 live 切换前置清单

### 4.1 现状

- preview 运行时 `EXECUTION_MODE=shadow`（实测）。`_run_shadow` 只生成待执行记录 + 模拟回执（`simulated=true`），**不调用任何真实资金接口**。✅ 真实资金未被触碰。

### 4.2 「切 live provider」的前置清单（NOT DONE — 不实际切换）

> live 是受控执行开关（`execution_mode=live` + 显式 `FundsProvider`）；阴影→live 属 release-manager 的 shadow→live 门控，且需人工复核。本清单为验收前置，**本次不执行切换**。

1. **沙箱端到端全过**：`tests/test_sandbox_e2e_flow.py`（14）+ `tests/test_sandbox_http_provider.py`（18）+ `tests/test_execution_engine.py`（21）+ `tests/test_approval_idempotency.py`（16）全 PASS；真实 HTTP 网关 on 临时端口复跑通过。
2. **幂等 / 对账 / 回滚就绪**：`UNIQUE(tenant_id, idempotency_key)` 服务端幂等 + `operation_id` 幂等锚 + `compensate`/`reconcile` fail-closed；`evidence/sandbox_concurrency_stress.json` N=256 单次提交确证。
3. **真实自托管模型端点 + 评测**：部署真实模型端点 → `evaluate_models.py` 产 `write_op_pass=true` 报告 → 该 model id 进入 `HIGH_CONFIDENCE_MODELS`（受限环境缺报告/白名单即 fail-closed）。（当前 BLOCKED，见 §1）
4. **生产网关沙箱部署**：内网部署 `sandbox_gateway`，注入 `GATEWAY_BASE_URL`/`GATEWAY_API_KEY`；`EXECUTION_PROVIDER=sandbox_http`。
5. **业务负责人确认**：首批真实租户（`LAUNCH_ALLOWED_TENANTS`，当前 `TENANT-A,TENANT-B`）+ 业务/合规负责人对「真实资金写」的书面确认；回调密钥 `EXECUTION_CALLBACK_HMAC_SECRET` 为非默认随机密钥（`check_secrets` 校验）。
6. **切换动作**：`EXECUTION_MODE=live`（受控），首次以少量租户 + 只读 + shadow 覆盖观察，连续观察 ≥7 天，满足无审批绕过 / 无跨租户 / 无重复执行 / 可对账 / 具备回滚暂停人工接管，才全量 live。
7. **回滚/暂停/人工接管机制**：`EXECUTION_MODE=shadow` 可随时回退；`mismatch/human_handoff` 转人工；终态封闭防重复扣款/退款。

> ⚠️ **本次绝不切换 live**。一切「切换 live」结论须由 release-manager 的 shadow→live 门控 + 人工复核后作出。

---

## 5. 自托管边界（完全自托管红线）

| 红线 | 证据 | 结论 |
|---|---|---|
| LLM 端点在受控内网 | `LLM_ALLOWED_HOSTS=model-endpoint`（`EndpointGuard` 仅放行白名单 host/IP/CIDR；受限环境空白名单 fail-closed） | ✅ |
| 无第三方托管 SaaS | 端点/模型为自托管（`src/llm` + 自部署端点）；预览实际 `LLM_BACKEND=mock`（本地规则引擎，无外联） | ✅ |
| 检索自托管 | `RETRIEVAL_BACKEND=keyword`（确定性、无外部依赖） | ✅ |
| 观测自托管或关闭 | `LANGFUSE_PUBLIC_KEY=`/`LANGFUSE_SECRET_KEY=` 为空 → 追踪 no-op；Langfuse 为自托管 `http://langfuse:3000`（未配置不启用） | ✅ |
| 日志脱敏 / 端点白名单 | `src/llm/security.py::redact`（手机号/地址/订单号/密钥/卡号）与 `EndpointGuard` | ✅ |
| 数据面端口不发布 | docker ps：`api/worker/frontend/postgres/redis` 均仅容器内端口，无 `0.0.0.0`；仅 `nginx` 发布 80/443 | ✅ |
| 出网阻断边界（已记录取舍） | `docker-compose.preview.yml` 注释：`internal:true` 直连在 Docker Desktop(WSL2) 下破坏内嵌 DNS 容器名解析，故回退普通 bridge，以「数据面不发布端口 + LLM_ALLOWED_HOSTS 白名单 + 完全自托管」等价落地；见 `deploy/EGRESS_POLICY.md` | ✅（已记录边界/取舍） |

**结论**：本专项范围内未发现未批准的第三方 SaaS 接入；端点/检索/观测均自托管或受控内网，符合完全自托管红线。⚠️ 但需注意：preview 目前 `LLM_BACKEND=mock` 意味着「真实模型链」尚未接入真实权重（见 §1），这是「先 shadow/只读再切 live」的保守且安全姿态。

---

## 6. 关键证据文件清单

**本专项产出（`evidence/prod-go-live/acceptance-engineer/`）：**
- `LLM_GATEWAY_ACCEPTANCE.md`（本报告）
- `LLM_GATEWAY_EVIDENCE.json`（端点探测 + 能力矩阵解析 + 运行态，机器可读）
- `gather_llm_gateway_evidence.py`（证据采集脚本，可复跑）

**交叉引用（既有 `evidence/`）：**
- `evidence/llm_candidate_eval.json`（当前为 `self-hosted-demo` stub）
- `evidence/verify_llm_chain_evidence.json`、`evidence/llm_fallback_to_human.json`、`evidence/llm_endpoint_connectivity.json`、`evidence/LLM_ENDPOINT_EVAL_REPORT.md`（先期 llm-eval 团队，含「self-hosted-model 为规则引擎代表」的边界声明）
- `evidence/sandbox_e2e_evidence.json`、`evidence/sandbox_concurrency_stress.json`、`evidence/COMPENSATION_RECONCILIATION_REPORT.md`
- `deploy/records/PRODUCTION_ACCEPTANCE_VERIFICATION.md`（先期生产能力综合验收，已诚实标注部署期执行项）

**本专项实测（pytest，运行于宿主 `.venv\Scripts\python.exe`）：**
- `tests/test_llm_endpoint_gate.py` → 21 passed
- `tests/test_sandbox_http_provider.py` → 18 passed
- `tests/test_sandbox_e2e_flow.py` → 14 passed
- `tests/test_execution_engine.py` → 21 passed
- `tests/test_approval_idempotency.py` → 16 passed
- `tests/test_callback_security_log.py` → 6 passed

---

## 7. 结论

1. **真实模型评测：BLOCKED**——无真实自托管模型权重端点（`LLM_BACKEND=mock`、端点不可达、无模型容器）；「self-hosted-model」为规则引擎代表而非真实模型。需部署真实自托管端点 + `evaluate_models.py` 评测 + `write_op_pass=true` 报告后方可宣称通过。
2. **能力矩阵/写门控：PASS（逻辑/运行时正确）**——评测驱动白名单、受限环境 fail-closed、非白名单拒写转人工、低置信度 fail-closed 均验证。⚠️但当前「可写模型」为 mock 引擎；评审报告完整性需在接入真实模型后重建（`verify_capability_matrix_t2.py` 因报告缺 `self-hosted-model` 有 4 项 FAIL，逻辑正确、属报告一致性事项）。
3. **网关沙箱：PASS（代码/测试）**——服务端 SQLite 幂等 + 回调验签 + compensate/reconcile + `gateway_unconfigured` fail-closed + N=256 单次提交全部验证；**生产网关沙箱可用性为「部署期执行项」**（preview 为 mock，未部署生产网关）。
4. **EXECUTION_MODE=shadow 确认**，未切 live；已给出完整 live 前置清单，本次不执行切换。
5. **自托管边界 PASS**——无未批准第三方 SaaS；端点/检索/观测自托管控；端点白名单 + 日志脱敏 + 数据面不发布端口。

> **验收裁决**：在「真实自托管模型端点」与「生产网关沙箱」就绪前，本项目只能以 **shadow + mock** 姿态运行，**不得宣称「真实模型评测通过」或「生产网关沙箱可用」**；这两项是 live provider 切换的前置硬性条件。当前无虚设/伪造评测，所有「未具备即标注」已如实记录。
