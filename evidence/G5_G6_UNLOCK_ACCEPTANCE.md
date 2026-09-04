# G5 / G6 解锁总验收报告（汇总交付）

> **编制**：release-documenter（docs） ｜ **日期**：2026-09-04 ｜ **工作目录**：`D:\software\PythonProject1\PythonProject\Multi_agent_E_commerce`
> **汇总依据**：`t1`（LLM 评测 / 能力矩阵，acceptance-engineer）、`t2`（网关沙箱验收 / 回调并发，test-runner）、`deploy/LLM_GATEWAY_CONFIG.md`（真实模型与网关配置说明，Captain）。
> **执行模式**：`EXECUTION_MODE=shadow`（只生成待执行记录 + 模拟回执，**不触真实资金、未切 live**）。

---

## 0. 结论摘要（TL;DR）

| # | 验收标准（G5 / G6 解锁基线） | 结论 | 关键证据 |
|---|---|---|---|
| 1 | 真实权重模型评测报告产生 | ⚠️ **机制达成 / 权重未达成**（详见 §3 诚实边界） | `evidence/llm_candidate_eval.json`、`evidence/LLM_CAPABILITY_MATRIX.md` |
| 2 | `write_op_pass=true` | ✅ **通过** | `evidence/llm_candidate_eval.json`（顶层 `write_op_pass=true`） |
| 3 | 白名单只含真实评测通过模型 | ✅ **通过**（受限环境必检） | `src/llm/capability.py`、`deploy/.env.preview.example` §HIGH_CONFIDENCE_MODELS |
| 4 | 网关 HTTP 沙箱端到端通过 | ✅ **通过** | `evidence/GATEWAY_SANDBOX_ACCEPTANCE_REPORT.md`、`gateway_sandbox_e2e_verify.json`、`gateway_http_e2e_captain.json` |
| 5 | N=256 并发 `provider submit` 仍为 1 | ✅ **通过** | `evidence/sandbox_concurrency_stress.json`（`provider_submit_calls=1`） |
| 6 | 所有异常路径 fail-closed | ✅ **通过** | `evidence/llm_endpoint_connectivity.json`、`llm_fallback_to_human.json`、`callback_g6_verify.json`、`deploy/LLM_GATEWAY_CONFIG.md` |

**一句话裁决**：G5 的「评测驱动写操作能力矩阵 + 白名单 + EndpointGuard」与 G6 的「自部署内网资金网关沙箱 + 幂等/回调/补偿/对账」**已在当前 shadow 环境内端到端闭环并通过验收**；**唯一残余外部依赖**是「真实生产权重模型」（边界4 G5）——当前端点引擎为项目**确定性规则（MockLLM）**，真实权重需以 vLLM/Ollama 接入同一 OpenAI 兼容契约并**重新评测**后方可进入白名单。**不得谎称已用真实权重模型评测。**

---

## 1. 验收标准逐条对照

### 1.1 真实权重模型评测报告产生 — ⚠️ 机制达成，权重未达成

- **已产出**：`scripts/evaluate_models.py --model-name self-hosted-model --llm-backend openai_compatible --base-url http://127.0.0.1:8001/v1` → `evidence/llm_candidate_eval.json`（24 用例全量，**已替代此前仅含 `self-hosted-demo` stub 的旧报告**）。
- **已证实**：端点 `http://127.0.0.1:8001/v1` 真实可用（`GET /v1/models` → `data[0].id=self-hosted-model`），六类评测 + 写操作专项全部 `passed=true`。
- **诚实边界**：端点引擎是项目**自管确定性规则（`self_hosted_server._route` / MockLLM）**，**不是真实权重运行时**（无 vLLM/Ollama/LM Studio、无缓存权重）。`Write_op_pass=true` 是「评分/发牌机制」而非「真实模型智商证明」。**真实生产权重**须以 vLLM/Ollama 接入同一 OpenAI 兼容契约（同 `/v1/chat/completions` + 同 prompt 契约），并**重跑** `evaluate_models.py` 产出该真实权重模型的评测报告后，方可写入 `HIGH_CONFIDENCE_MODELS`。
- **判定**：G5 的「评测链路 + 白名单机制」**达成**；「真实权重模型」这一**边界4 外部依赖****未达成**。以下所有「通过」均以「当前确定性端点」为对象，**不可推广到真实权重**。

### 1.2 `write_op_pass=true` — ✅ 通过

- `evidence/llm_candidate_eval.json` 顶层：`"write_op_pass": true`、`"any_write_failure": false`、`"passed": true`。
- 写操作专项 5 用例全部 `passed=true`：`writeop-refund/return/address-nonwhitelist` + `writeop-refund/address-whitelist`。
- 六类评测（`intent/params/faithfulness/low_confidence/endpoint/malicious`）共 19 用例全部 `passed=true`；总 24/24。
- **意涵**：`writeop-*-nonwhitelist` 与 `writeop-*-whitelist` 两组都会走通参数校验，说明「能否写」最终由**能力矩阵白名单门控**裁决，而非参数是否合法——这正是宪法「门控依据是模型名显式白名单，不是分数阈值」的体现。

### 1.3 白名单只含真实评测通过模型 — ✅ 通过（受限环境）

- **唯一权威解析口**：`src/llm/capability.py::resolve_high_confidence_models(settings)`：
  ```
  生效白名单 = HIGH_CONFIDENCE_MODELS（显式） ∩ { 评测报告中 write_op_pass==true 的模型 }
  受限环境且 显式白名单为空 或 评测报告缺失 → frozenset()（fail-closed，无可写模型）
  ```
- 本机验证（`LLM_CAPABILITY_MATRIX.md` §4 场景表 + `verify_capability_matrix_t2.py` 27/27 PASS）：
  - preview + 显式 `self-hosted-model` + 报告 `write_op_pass=true` → 生效 `{self-hosted-model}`，`capability_ok=True`；
  - preview + 空白名单 / 报告缺失 / 报告 `write_op_pass=false` → 一律空集 / 排除，`capability_ok=False`（fail-closed）；
  - **未评测的 `qwen2.5-max` 即使列入白名单也被排除**（`capability_ok('qwen2.5-max')=False`）→ 白名单**只含真实评测通过模型**。
- 配置落地：`deploy/.env.preview.example` §HIGH_CONFIDENCE_MODELS=`self-hosted-model`，`LLM_EVAL_REPORT_PATH=/app/evidence/llm_candidate_eval.json`（容器只读挂载）。
- **诚实标注**：`development/test` 环境在「显式白名单为空」时回落 `DEFAULT_DEV_WRITE_MODELS`（`gpt-4/gpt-4-turbo/claude-3-opus/qwen2.5-max`），这是**仅限 dev/test 的演示默认集合**，是该报告未覆盖的模型，但**不具受限环境写能力**——受限环境（preview/production）严格走「显式白名单 ∩ 报告」，保证「白名单只含评测通过模型」不变量成立。

### 1.4 网关 HTTP 沙箱端到端通过 — ✅ 通过

- 服务：`src/execution/sandbox_gateway.py`，独立 uvicorn 运行于 `http://127.0.0.1:8010`（自托管、内网、SQLite **服务端持久化幂等**，`X-API-Key=gw-key`）。
- 端到端 HTTP 验证（`evidence/gateway_sandbox_e2e_verify.json`，独立脚本 `_verify_tmp/verify_gateway_e2e.py`）：
  - **submit 幂等**：同 `(tenant_id, idempotency_key)` 两次 POST → 同一 `external_txn_id`（`txn-5208bb17686543f3`），`server_rows_for_key=1`，`idempotent_ok=true`；
  - **query 对账**：`GET /api/sandbox/query` → `succeeded`、`amount=88.5`；未知 → `not_found`；
  - **compensate 稳定 reversal_id**：两次 POST → 同一 `rev-165acef0541d5dfd`（`uuid5` 确定性派生，重放一致）；
  - **跨租户不覆盖**：同 `idempotency_key` 分属 `TENANT-A`/`TENANT-B`，各 1 行、`external_txn_id` 不同，`cross_tenant_no_overwrite=true`。
- Captain 独立交叉验证：`evidence/gateway_http_e2e_captain.json` → 同租户幂等、query、稳定 reversal、跨租户无碰撞，`all_ok=true`。
- 汇总报告：`evidence/GATEWAY_SANDBOX_ACCEPTANCE_REPORT.md`（结论：**通过**）。

### 1.5 N=256 并发 `provider submit` 仍为 1 — ✅ 通过

- `evidence/sandbox_concurrency_stress.json`（`scripts/sandbox_concurrency_stress.py --backend sqlite --concurrency 256`）：
  - **I1 同 operation 高并发 execute**：`concurrency=256`、`provider_submit_calls=1`（**恰好 1 次**）、`distinct_external_txn_id=1`、`distinct_execution_id=1`、`errors={}`，`I1_ok=true`；
  - **FAIL 显式失败收敛**：`provider_submit_calls=1`、`provider_compensate_calls=1`、`distinct_reversal_id=1`（单一 reversal）、终态 `compensated`（单终态），`FAIL_ok=true`；
  - **I2 跨租户同 idempotency_key 并发**：每租户 256，两个 provider 各 `submit_calls=1`、各 1 条执行记录，`cross_tenant_no_overwrite=true`。
- 结论：高并发下 `submit` 精确 1 次、无重复扣款/退款、失败收敛单一终态；`conclusion=true`。
- 机制：`INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING` + 事务内读回既有行（并发重复提交也只落一行并返回同一结果）。
- 诚实标注：本报告验收的是「引擎层调用已修复的 `claim_execution_submit` 单执行守卫 + 真实网关（进程外 HTTP）」的组合；SQLite 并发写由单实例共享锁串行化（多实例/独立连接会抛 `database is locked`），真实多进程行锁已由 PostgreSQL `FOR UPDATE` + RLS 路径另行验证（`test_pg_callback_concurrency.py`）。

### 1.6 所有异常路径 fail-closed — ✅ 通过

**LLM 端点异常**（`evidence/llm_endpoint_connectivity.json`，12 条探测）：
| 探测 | 结果 | failure_closed |
|---|---|---|
| `GET /v1/models` / classify / extract / hallucination / rewrite / rag_answer | 真实内容 | 否（正常路径） |
| `low_confidence/vague` | confidence=0.5<0.7 → 节点层转人工 | 否（主动 fail-closed） |
| `timeout/ReadTimeout` | 抛 `LLMUnavailableError` | **true** |
| `retry/503` | 有界重试 3 次后抛 `LLMUnavailableError` | **true** |
| `invalid_json/_safe_json` | 抛 `LLMOutputError` | **true** |
| `invalid_json/validate_intent_output` | 抛 `LLMOutputError` | **true** |
| `unreachable/ConnectError` | 抛 `LLMUnavailableError` | **true** |

**回退转人工**（`evidence/llm_fallback_to_human.json`，11 条路径）：非白名单模型 refund/return/address、端点不可达、超时、5xx、非法 JSON/意图值域非法、低置信度、答案不忠实、恶意注入、可重试错误重试耗尽——**全部转入工 / 拒绝写**，且**每条 `operation_id_is_none=true`、`approval_none=true`**（转人工时未创建操作 ID、未生成审批，写路径被彻底阻断）。

**写操作门控**（`LLM_CAPABILITY_MATRIX.md` §4B）：对非白名单模型发起 refund → `capability_ok=False` → graph `falls_to_error=True`、`reason=model_not_in_whitelist`、`operation_id=None`、**无 `approval_required` 事件、未创建 approval/operation 记录**。

**网关/回调 fail-closed**（`evidence/callback_g6_verify.json` + `CALLBACK_CONCURRENCY_REPORT.md`）：
| 场景 | 引擎结果 | 判定 |
|---|---|---|
| 错误签名 | `signature_invalid, applied=False` | ✅ 拒绝 |
| 同 nonce 重投 | `replay, applied=False`（终态保持不变） | ✅ 只重放不重复生效 |
| 终态后新 nonce | `terminal_locked, applied=False` | ✅ 终态封闭 |
| 金额不一致 |执行 `mismatched`、操作 `human_handoff` | ✅ 转人工 |
| 未知 status |归一化中间态（保持 `submitted`、仅记账 nonce） | ✅ 等待最终回调 |
| 跨租户伪造 | `not_found, applied=False`，原记录零污染 | ✅ 拒绝、不泄露 |

**配置层 fail-closed**（`deploy/LLM_GATEWAY_CONFIG.md`）：EndpointGuard 非白名单 host → 构造即抛 `LLMUnavailableError`（拒绝未批准外联）；受限环境白名单为空 → 一律拒绝；`build_provider` 缺 `gateway_base_url` → 抛 `gateway_unconfigured`（**绝不回退 mock**）。

> **结论**：超时 / 5xx / 非法 JSON / 不可达 / 非白名单 / 验签失败 / 重放 / 终态覆盖 / 金额冲突 / 跨租户伪造 —— 所有异常路径均 **fail-closed**（拒绝或转人工），**无一落入「可能执行错误资金操作」的开放路径**。

---

## 2. 交付物清单核对（齐全性）

参考 ✅ / ⚠️ 标注状态，均已存在于磁盘（`D:\software\PythonProject1\PythonProject\Multi_agent_E_commerce`）。

### G5（LLM 评测 / 能力矩阵 / 白名单）
| 文件 | 来源 | 状态 |
|---|---|---|
| `evidence/llm_candidate_eval.json` | t1 `evaluate_models.py` 实测 | ✅ 24 用例，`write_op_pass=true` |
| `evidence/LLM_CAPABILITY_MATRIX.md` | t1 能力矩阵 / 写门控结论 | ✅ 27/27 + 28/28 断言 PASS |
| `evidence/llm_endpoint_connectivity.json` | t1 端点探测 | ✅ 12 条，异常路径全 `failure_closed=true` |
| `evidence/llm_fallback_to_human.json` | t1 回退转人工 | ✅ 11 条路径全阻断 |
| `evidence/LLM_ENDPOINT_EVAL_REPORT.md` | t1 LLM 端点评测 | ✅ |
| `evidence/rag_probe_evidence.json` | t1 RAG 占位符缺陷观测/修复 | ✅（诚实记录，已修复复验） |
| `verify_capability_matrix_t2.py` | t1 验证脚本 | ✅ 27/27 断言 |
| `verify_t6_acceptance.py` | t1 交叉验证脚本 | ✅ 28/28 断言 |

### G6（网关沙箱 / 回调并发 / 补偿）
| 文件 | 来源 | 状态 |
|---|---|---|
| `evidence/GATEWAY_SANDBOX_ACCEPTANCE_REPORT.md` | t2 网关端到端验收 | ✅ |
| `evidence/gateway_sandbox_e2e_verify.json` | t2 HTTP 端到端实测 | ✅ `conclusion=true` |
| `evidence/gateway_http_e2e_captain.json` | Captain 交叉验证 | ✅ `all_ok=true` |
| `evidence/sandbox_concurrency_stress.json` | t2 N=256 并发 | ✅ `conclusion=true` |
| `evidence/CALLBACK_CONCURRENCY_REPORT.md` | t2 回调验签/重放/冲突 | ✅ |
| `evidence/callback_g6_verify.json` | t2 引擎回调探针 | ✅ `conclusion=true` |
| `src/execution/sandbox_gateway.py` + `src/execution/provider.py` + `src/execution/verification.py` | 被测实现 | ✅ 存在 |

### 配置说明
| 文件 | 来源 | 状态 |
|---|---|---|
| `deploy/LLM_GATEWAY_CONFIG.md` | Captain 真实模型与网关配置说明 | ✅ |
| `deploy/.env.preview.example` | Captain 配置模板 | ✅ `EXECUTION_MODE=shadow`、`HIGH_CONFIDENCE_MODELS=self-hosted-model`、`LLM_ALLOWED_HOSTS=model-endpoint,10.0.0.0/8` |
| `deploy/.env.production.example` | Captain 配置模板 | ✅ |

> **核对结论**：t1 / t2 / Captain 三方的交付物**均已落盘且相互引用一致**，无缺失。

---

## 3. 诚实边界声明（关键，必读）

### 3.1 端点引擎为项目确定性规则（MockLLM），**并非真实权重** —— 不得谎称已用真实权重模型评测

- 本环境**无真实权重运行时**（无 vLLM/Ollama/LM Studio、无缓存权重）。端点服务 `src/llm/self_hosted_server.py` 的引擎为项目的**确定性规则（`_route` / MockLLM）**，是设计文档为「无真实权重环境做本地/内网链路验收」提供的项目自管实现。
- 因此 `evidence/llm_candidate_eval.json` / `LLM_CAPABILITY_MATRIX.md` 中的「24 用例通过、`write_op_pass=true`」**仅证明该确定性端点 + 评测链路 + 白名单机制工作正常**，**未证明任何真实权重大模型具备写能力**。
- **解锁路径**：以 vLLM/Ollama 部署真实权重模型，接入**同一 OpenAI 兼容契约**（同 `/v1/chat/completions` + 同 prompt 契约）后，**重新跑一次** `scripts/evaluate_models.py` 产出该真实权重模型的评测报告；`write_op_pass=true` 且 `HIGH_CONFIDENCE_MODELS` 显式列出该模型名后，方能进入写白名单。
- **结论**：这是 **边界4 G5 的残余外部依赖**。未完成前，任何「已用真实权重模型评测」的表述均为**不实**，必须表述为「评测链路/白名单机制已就绪，真实权重模型待接入并重新评测」。

### 3.2 EXECUTION_MODE=shadow 确认（未触真实资金、未切 live）

- `deploy/LLM_GATEWAY_CONFIG.md` §配置表：`EXECUTION_MODE=shadow`，「只生成待执行记录+模拟回执，不触真实资金」，「绝不接真实资金，绝不切 live」。
- `deploy/.env.preview.example` / `.env.production.example`：`EXECUTION_MODE=shadow`。
- `PROJECT_STATUS.md` §四：**「只读+shadow 先于 live（`EXECUTION_MODE=shadow`，未切 live）」**，且「G6/A5 真实资金链路」仍为边界4 外部依赖（生产网关沙箱外部部署 + shadow-only 提交）。
- **结论**：全程在 shadow / 受控沙箱路径内验证，未触碰真实资金渠道、未切换至 live。

### 3.3 对「G5 / G6 是否已解锁」的精确表述

| 维度 | 状态 | 说明 |
|---|---|---|
| G5 评测驱动能力矩阵 + 白名单 + EndpointGuard | ✅ **已解锁（本环境）** | 确定性端点验证闭环；白名单只含评测通过模型；写门控 fail-closed |
| G5 真实权重模型评测 | ⚠️ **未解锁（边界4 外部依赖）** | 需 vLLM/Ollama 接入 + 重新评测 |
| G6 内网沙箱网关（幂等/回调/补偿/对账/并发） | ✅ **已解锁（本环境）** | 真机 8010 HTTP 端到端 + N=256 并发 + 回调验签/重放/终态封闭 |
| G6 真实资金链路 | ⚠️ **未解锁（边界4 外部依赖）** | 生产网关沙箱外部部署 + 真实资金渠道另行批准 |
| `EXECUTION_MODE=shadow` | ✅ 保持 | 未触真实资金、未切 live |

---

## 4. 结论与放行建议

- **G5 / G6 在本 shadow 环境内的能力/安全/幂等侧均已端到端闭环并通过验收**，为「有条件 GO」提供能力基础（对齐 `PROJECT_STATUS.md` 边界1/2/3 已充分验证）。
- **不能对外宣称「G5 真实权重模型 / G6 真实资金链路已解锁」**；这两项仍属 **边界4 外部依赖**，需外部输入到位（真实权重端点 + 重新评测；生产网关沙箱部署 + shadow-only 提交 + 另行批准）后，方可由 Captain / release-manager 按既定口径转「有条件 GO / 放行」。
- **进入 live 前建议**：部署侧显式配置写白名单后，用真实权重模型 + 真实沙箱网关，走通「白名单门控 → 归属/资格/金额校验 → 唯一 `human_approval` 审批 → 幂等执行」全链复验；同时保持 `EXECUTION_MODE=shadow`，切 live 需另行批准并记录审计。

---

*本报告为交付物汇总与验收对照，未重新运行测试；所引用的原始证据文件与脚本均可在上述路径复现。*
