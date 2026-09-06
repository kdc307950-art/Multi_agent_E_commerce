# Ollama Qwen3 8B 复测记录

日期：2026-09-06

## 复测环境

- Ollama：本机 `http://127.0.0.1:11434`
- 模型：`qwen3:8b`
- CrewAI：`0.152.0`
- LiteLLM：`.accept-crewai-venv` 中的锁定版本
- 思考输出：`think=false`
- 端点：OpenAI-compatible `/v1/chat/completions`

## 结果

### 1. 原生工具调用协议

`scripts/verify_ollama_tool_call.py` 独立运行 3 次，结果为 **3/3 PASS**：

- 返回标准 `tool_calls`
- 工具名为 `query_order`
- 参数为 `{"order_id":"ORD-001"}`

这只证明 Ollama 端点在该固定请求下具备协议兼容性，不证明 CrewAI 运行时稳定，也不证明任何写操作能力。

### 2. 真实 CrewAI 查询链路

使用 `.accept-crewai-venv` 直接构造 `CrewAIToolRouter(crewai_enabled=True)`，绑定真实 `query_order` 工具并连续运行 3 次：

| 运行 | CrewAI 返回 | 绑定工具实际执行 | 结论 |
|---:|---|---:|---|
| 1 | `tool=query_order`，返回模型生成的订单 JSON | 0 次 | 失败：未调用工具 |
| 2 | `tool=query_order`，返回模型生成的订单 JSON | 0 次 | 失败：未调用工具 |
| 3 | `tool=query_order`，返回订单摘要 | 1 次 | 成功：实际调用工具 |

两次未执行工具的结果包含与种子数据不一致的金额/商品，说明不能把 `tool` 字段或模型文本视为工具执行证据。

## 判定

- 原生 `qwen3:8b` 工具调用协议：**通过（3/3）**。
- 真实 CrewAI + Qwen 查询工具执行：**未通过稳定性门槛（1/3 实际执行）**。
- `evidence/llm_candidate_eval.json`：保持 `whitelist_eligible: false`。
- `HIGH_CONFIDENCE_MODELS`：不加入 `qwen3:8b`。
- 退款、退货、改址：继续人工审批、Shadow、幂等和 fail-closed。

## 3. 适配器修复后的复核

已修复 `src/tools/crewai_adapter.py` 中的运行时适配缺口：

- 对真实 CrewAI LLM，优先提交带严格 `tool_choice` 的原生工具调用；
- LiteLLM 返回普通文本、超时或端点短暂异常时，只允许一次最小化原生请求重试；
- 原生调用未返回受限工具、参数不是 JSON 对象，或工具未实际执行时，立即 fail-closed，不进入会猜测参数的 ReAct 文本重试；
- 原生响应必须恰好包含一条绑定工具调用；输入 schema 禁止额外字段，避免多调用歧义和身份字段注入；
- 工具函数仍通过 CrewAI Tool 对象和服务端 `TenantContext` 闭包执行，模型不能注入 `tenant_id`、`user_id` 或 `role`。

修复后的本机全量回归为 **415 passed, 36 skipped**（2026-09-06），CrewAI/安全专项为 **51 passed, 3 skipped**。但对 Ollama `qwen3:8b` 的随后 3 次真实 CrewAI 复核，端点均返回 HTTP 503，未发生绑定工具调用。因此这次复核证明失败会被收敛为异常/人工处理，**不构成 CrewAI + Qwen 稳定通过证据**。

## 后续边界

当前最合理的演示口径是“Qwen3 8B 原生协议兼容性已验证；CrewAI 运行时已增加 fail-closed 原生工具适配，但端点稳定性仍待验证”。在 CrewAI 能稳定证明绑定工具实际执行前，不得宣称“Qwen3 8B CrewAI 查询链路已通过”，更不得据此开放敏感写操作。
