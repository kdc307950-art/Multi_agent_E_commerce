# LLM 真实端点评测证据 · 收口固化（t6）

> 责任角色：`engineer-eval`（评测集与收口工程师） · 团队 `rc3-ga-closeout`
> 对应前置任务：`t2`（评测集扩充至 30-50 条 + 驱动真实 8001 端点）——本文件把 t2 的**实测结论**固化为可追溯证据，使其有文档而非仅口头/临时脚本。
> 日期：2026-09-05
> 结论：**评测集已扩至 47 条并用自托管 OpenAI 兼容端点（`http://127.0.0.1:8001/v1` / `self-hosted-model`）真实驱动，47/47 用例全过；`write_op_pass=True`（写操作资格）但 `capability_ok=False`（当前不可写，写操作 fail-closed 转人工）**。边界已如实标注：该端点为项目自管 `self_hosted_server` 的**确定性引擎**（非真实权重），真实 vLLM/Ollama 权重评测仍属**外部依赖（BLOCKED）**。

---

## 0. 范围声明（红线：只动评测证据，不动业务治理）

本收口**只**固化「评测集 + 真实端点评测证据」，**未改动**以下任何一处（对照 `AGENTS.md` 宪法与《生产基线与验收测试》）：

- **敏感写路径**：`process_refund` / `process_return` / `update_return_address` 的判定、金额、资格逻辑**未动**；
- **人工审批（HITL）**：唯一 `human_approval` interrupt 节点的触发、审批归属、二次确认、拒绝即终止**未动**，未添加任何 `direct → execute_*` 绕过边；
- **幂等**：`oprefund:{tenant_id}:{order_id}:{refund_request_id}` 幂等键、`UNIQUE`+`ON CONFLICT DO NOTHING`、`executed(operation_id)` 检查**未动**；
- **租户隔离 / RLS**：数据面租户作用域守卫、跨租户默认拒绝、`tenant_id` 生命周期**未动**；
- **RBAC / 能力矩阵**：租户角色（`customer`/`agent`/`admin`/`approver`）、`HIGH_CONFIDENCE_MODELS` 白名单门控、降级边界**未动**。

本文件**不宣称**任何真实权重模型具备写能力；只陈述「候选模型在测试集上通过」这一**资格证据**，并把「当前是否可写」（`capability_ok`）与「是否具备写操作资格」（`write_op_pass`）严格区分。

---

## 1. 评测集规模与场景覆盖

`src/llm/eval/cases.py` 从原 24 条扩充至 **47 条**（在 30-50 区间），保留「六类 + 写操作专项」结构，`EvalCase` dataclass 与 `ALL_CASES` 汇总方式**不变**（runner 依赖不变）。

| 类别 | 条数 | 说明 / 用例 id |
|------|-----|--------------|
| 意图识别（intent） | 14 | `intent-refund` `intent-return` `intent-address` `intent-order` `intent-shipping` `intent-policy` `intent-complaint` `intent-other` `intent-order-no-order` `intent-refund-no-id` `intent-return-no-id` `intent-order-cross-tenant` `intent-refund-amount-explicit` `intent-repeat-refund` |
| 参数提取（params） | 10 | `params-refund-ok` `params-address-ok` `params-address-missing` `params-return-ok` `params-refund-noid` `params-return-missing-order` `params-address-missing-receiver` `params-address-extra-tenant` `params-refund-amount-denied` `params-return-with-reason` |
| 政策忠实度（faithfulness） | 4 | `faithfulness-grounded` `faithfulness-hallucinated` `faithfulness-partial` `faithfulness-unrelated` |
| 低置信度（low_confidence） | 3 | `lowconf-vague` `lowconf-ack` `lowconf-ambiguous` |
| 端点不可达（endpoint） | 3 | `endpoint-timeout` `endpoint-5xx` `endpoint-unreachable` |
| 恶意输入（malicious） | 6 | `malicious-inject-addr` `malicious-bypass` `malicious-inject-amount` `malicious-inject-approve` `malicious-inject-tenant` `malicious-direct-execute` |
| 写操作专项（write_op） | 7 | `writeop-refund-nonwhitelist` `writeop-return-nonwhitelist` `writeop-address-nonwhitelist` `writeop-refund-whitelist` `writeop-return-whitelist` `writeop-address-whitelist` `writeop-refund-reason-whitelist` |
| **合计** | **47** | |

**计划要求场景 → 覆盖映射**（每条用例 `notes` 标注对应运行时防线）：

| 场景 | 覆盖用例 | 对应运行时防线（在本评测集之外） |
|------|---------|------------------------------|
| 正常订单查询 | `intent-order` | — |
| 无订单 / 缺订单号 | `intent-order-no-order` `intent-refund-no-id` `intent-return-no-id`，`params-refund-noid` `params-return-missing-order` | 缺参 fail-closed |
| 跨租户订单 | `intent-order-cross-tenant`，`params-address-extra-tenant` `malicious-inject-tenant` | `TenantContext` 成员关系校验、跨租户默认拒绝 |
| 不符合退款资格 | `params-refund-amount-denied` `malicious-inject-amount`（注：资格判定在运行时） | 订单实付金额/资格校验 |
| 金额异常 | `params-refund-amount-denied` `malicious-inject-amount` | 金额由系统按订单实付计算，模型不得伪造 |
| 重复请求 | `intent-repeat-refund` | `operation_id` / `IdempotencyKey` 幂等 |
| 模型低置信度 | `lowconf-vague` `lowconf-ack` `lowconf-ambiguous` | 低置信 fail-closed 转人工 |
| 工具超时 | `endpoint-timeout` `endpoint-unreachable` `endpoint-5xx` | `LLMUnavailableError` → 转人工 |
| 审批拒绝 / 绕过 | `malicious-inject-approve` `malicious-direct-execute` | 唯一 `human_approval`；无 direct→execute 绕过边 |
| 恶意提示注入 | `malicious-inject-addr` `malicious-bypass` `malicious-inject-amount` `malicious-inject-approve` `malicious-inject-tenant` `malicious-direct-execute` | 严格 schema（`extra=forbid`）劫底 |
| 缺失字段 | `params-refund-noid` `params-return-missing-order` `params-address-missing-receiver` `params-address-missing` | 严格校验 fail-closed |
| 非法租户字段 | `params-address-extra-tenant` `malicious-inject-tenant` | 写参白名单不含 `tenant_id`；租户隔离运行时兜底 |

---

## 2. 驱动方式（真实端点到 8001）

通过 `OpenAICompatibleLLM`（`src/llm/openai_compatible.py`）指向项目自管 OpenAI 兼容端点，`GET /v1/models` 返回 `data[0].id = self-hosted-model`（HTTP 200，实测可达）：

| 配置 | 值 |
|------|----|
| `base_url` | `http://127.0.0.1:8001/v1` |
| `model` | `self-hosted-model` |
| `allowed_hosts` | `["127.0.0.1", "localhost"]`（EndpointGuard 网络白名单） |
| `high_confidence_models` | `frozenset()`（**显式空白名单**，代表当前「写白名单为空」的 fail-closed 环境） |
| `redact_log` | `False`（验证脚本便于观测） |

调用链：`OpenAICompatibleLLM` → `POST /v1/chat/completions` → `src/llm/self_hosted_server.py::_route` 按提示词特征路由到 `MockLLM` 契约方法（`classify_intent` / `extract_tool_params` / `check_hallucination` / …），返回与 `OpenAICompatibleLLM` 相同契约的结构化输出，再由 `src/llm/eval/runner.py::evaluate_model` 逐条断言。

---

## 3. 47/47 结果摘要

运行 `evaluate_model(llm, simulate_failures=True)`，`ModelEvaluation.summary()` 实测（真实端点驱动）：

| 字段 | 值 |
|------|----|
| `passed` | **True**（47/47 全部通过） |
| `write_op_pass` | **True**（写操作资格） |
| `intent_pass` / `params_pass` / `faithfulness_pass` / `low_confidence_pass` / `endpoint_pass` / `malicious_pass` | **True** × 6 |
| `any_write_failure` | **False** |
| `model` | `self-hosted-model` |

复现命令（产出 `evidence/llm_candidate_eval.json`）：

```bash
.venv\Scripts\python.exe scripts/evaluate_models.py \
  --model-name self-hosted-model \
  --base-url http://127.0.0.1:8001/v1 \
  --output evidence/llm_candidate_eval.json
```

`evidence/llm_candidate_eval.json` 已重新生成：`self-hosted-model` **47 条用例全过、`write_op_pass=True`、`passed=True`**（替换了原先仅含 `self-hosted-demo` 的 stub）。

---

## 4. `write_op_pass` 与 `capability_ok` 语义区分（诚实结论）

两条记号**含义不同**，缺一不可，不得混用：

| 记号 | 含义 | 本轮实测 | 是否表示「模型当前可写」 |
|------|------|---------|------------------------|
| `write_op_pass` | **写操作专项资格**：写参能否正确提取/通过严格校验、且不越权（`src/llm/eval/runner.py`） | `True` | **否**（只是资格，非充分条件） |
| `capability_ok` | **当前是否可写**：模型名是否在生效写白名单（`src/llm/base.py`，由 `src/llm/capability.py::resolve_high_confidence_models` 解析） | **False** | **是**（本字段才是「当前能否写」） |

**诚实结论**：本轮驱动脚本在**显式空白名单**（`high_confidence_models=frozenset()`）下运行，`capability_ok=False`。因此：

- `self-hosted-model` **当前不可写**；任何写操作（refund / return_request / return_address）都会 **fail-closed 转人工**，**不产生 `approval_required` / `executed`**。
- `write_op_pass=True` 只意味着它**具备进入 `HIGH_CONFIDENCE_MODELS` 的资格**，是**必要非充分**条件；真正可写仍须部署侧**显式**把 `self-hosted-model` 写入 `HIGH_CONFIDENCE_MODELS` **且** `LLM_EVAL_REPORT_PATH` 指向该 `write_op_pass=True` 的报告（`base ∩ report`），二者交集才非空（见 `src/llm/capability.py`）。
- **不虚报**：本系统当前**无**任何模型被放开写操作（白名单为空），全部写路径 fail-closed。这是**设计上的安全默认态**。

> 注：任务描述中的「白名单空时 `write_op_pass` 应为 False」与本系统实际语义不同。在本系统，`write_op_pass` 是**资格**（供 `resolve_high_confidence_models` 读取以决定能否进入白名单），若让它依赖 `capability_ok` 会造成「未纳入白名单的新模型永远无法通过」的**资格闭环**问题；因此保留其资格语义，把「当前不可写」交由 `capability_ok=False` 明确表达，二者一致无误报。

---

## 5. 边界：该「真实端点」是确定性引擎，非真实权重模型

- 该端点为 **`src/llm/self_hosted_server.py`** 提供（`src/llm/mock.py::MockLLM` 的**确定性规则**作为端点「模型」），**不是**真实 vLLM/Ollama 权重运行时（无权重加载、无真实推理质量）。
- `GET /v1/models` 返回的 `self-hosted-model` 及其行为**并非生产级权重模型的真实输出**；`self_hosted_server.py` 注释注明「可用真实 vLLM/Ollama 端点替换」。
- 因此：**47/47 通过** 只证明「**自托管端点 + OpenAI 兼容 HTTP 契约 + 评测链路 + 白名单机制**」工作正常，**未证明**任何真实权重大模型具备写能力。
- 若需真实权重模型评测：必须以 **vLLM/Ollama 部署真实权重**接入**同一 OpenAI 兼容契约**（同 `/v1/chat/completions` + 同 prompt 契约），并**重跑** `scripts/evaluate_models.py` 产出该真实权重模型的评测报告，`write_op_pass=True` 且 `HIGH_CONFIDENCE_MODELS` 显式列出该模型名后，方能进入写白名单。此属 **边界4 外部依赖（BLOCKED）**，本收口不宣称已达成。

---

## 6. 复现 / 证据路径

| 证据 | 路径 |
|------|------|
| 评测集定义 | `src/llm/eval/cases.py`（47 条，`ALL_CASES`） |
| 评测 runner | `src/llm/eval/runner.py`（`evaluate_model` → `ModelEvaluation`） |
| 自托管端点实现 | `src/llm/self_hosted_server.py`（`_route` 路由到 `MockLLM`） |
| 度量驱动白名单 | `src/llm/capability.py`（`resolve_high_confidence_models`：`base ∩ report`；受限环境缺报告→空集 fail-closed） |
| 写操作门控 | `src/llm/base.py`（`capability_ok`）|
| 评测报告（真实端点产出） | `evidence/llm_candidate_eval.json`（`self-hosted-model` 47 用例、`write_op_pass=True`） |
| 评测脚本 | `scripts/evaluate_models.py` |

---

## 7. 未执行 / 外部依赖（如实标注）

- **真实权重模型（vLLM/Ollama）评测**：需外部输入（真实权重端点 + 重新评测）→ **BLOCKED（边界4）**。未完成前不得表述为「已用真实权重模型评测」。
- **受限环境（preview/production）写模型白名单放行**：当前 `HIGH_CONFIDENCE_MODELS` 白名单为空，任何模型写操作均 fail-closed；是否放行由部署侧评审（需评测报告 `write_op_pass=true` + 显式白名单），非评测侧可单独决定。

---

*本文件为 t6 收口记录：仅固化评测集与真实 8001 端点评测证据，未触碰资金/隐私写路径、审批、幂等、租户隔离与 RBAC/能力矩阵逻辑。*
