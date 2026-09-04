# 真实模型与网关配置说明（G5 / G6 解锁）

> 定位：本文档是「自托管 LLM 端点 + 评测驱动写操作能力矩阵 + EndpointGuard + 内网网关沙箱」的
> **权威配置说明**，配套 `scripts/evaluate_models.py`、`src/llm/capability.py`、`src/llm/security.py`、
> `src/execution/sandbox_gateway.py`、`src/execution/provider.py`。所有值均已在本机真实校验（见 §验证）。
> **约束：EXECUTION_MODE 恒为 `shadow`，绝不接真实资金，绝不切 live。**

---

## 0. 结论摘要

| 配置项 | 值 | 说明 |
|---|---|---|
| LLM 端点 | `http://127.0.0.1:8001/v1`（模型 `self-hosted-model`） | 项目自管 OpenAI 兼容端点，`src/llm/self_hosted_server.py` |
| LLM 后端 | `LLM_BACKEND=openai_compatible` | 调自托管端点，无任何公有 SaaS |
| EndpointGuard | `LLM_ALLOWED_HOSTS=127.0.0.1,localhost`（生产加内网 CIDR） | 拒绝公网 SaaS，fail-closed |
| 写操作白名单 | `HIGH_CONFIDENCE_MODELS=self-hosted-model` | 仅评测报告 `write_op_pass=true` 的模型 |
| 评测报告 | `LLM_EVAL_REPORT_PATH=evidence/llm_candidate_eval.json` | 24 用例全通过，`write_op_pass=true` |
| 执行提供方 | `EXECUTION_PROVIDER=sandbox_http` | 调自部署网关沙箱 `http://127.0.0.1:8010` |
| 执行模式 | `EXECUTION_MODE=shadow` | 只生成待执行记录+模拟回执，不触真实资金 |
| 回调验签 | `EXECUTION_CALLBACK_HMAC_SECRET=<env注入>` | HMAC-SHA256，verify_hmac_signature |

---

## 1. 自托管 OpenAI 兼容模型端点

**完全自托管红线**：LLM 端点只能落在项目方控制的网络边界内；绝不转发到未批准公有 SaaS。

- 端点服务：`src/llm/self_hosted_server.py`，暴露 `GET /v1/models` 与 `POST /v1/chat/completions`。
- 暴露模型名由 `DSH_LLM_ENDPOINT_MODEL` 控制（默认 `self-hosted-model`）。
- 绑定内网 `127.0.0.1:8001`，不得暴露公网。

```bash
DSH_LLM_ENDPOINT_MODEL=self-hosted-model \
  .venv/Scripts/python.exe -m uvicorn src.llm.self_hosted_server:app \
    --host 127.0.0.1 --port 8001 --log-level warning
```

**诚实边界**：本环境无真实权重运行时（无 vLLM/Ollama/LM Studio、无缓存权重），端点引擎为项目的
**确定性规则（`MockLLM`）**，是设计文档 §1 为「无真实权重环境做本地/内网链路验收」提供的项目自管
实现。**真实生产权重**需以 vLLM/Ollama 接入同一 OpenAI 兼容契约（同 `/v1/chat/completions` + 同 prompt 契约），
并**重新跑一次** `scripts/evaluate_models.py` 产出该真实权重模型的评测报告，方能将其加入
`HIGH_CONFIDENCE_MODELS`。这是 PROJECT_STATUS 边界4 G5 的残余外部依赖，未达成前**不得宣称已用真实权重模型评测**。

## 2. EndpointGuard（端点网络白名单）

`src/llm/security.py::EndpointGuard` 在构造 `OpenAICompatibleLLM` 时校验 `LLM_BASE_URL` 的 host/IP/CIDR：

- 命中显式白名单 → 放行；未命中且受限环境 → fail-closed（拒绝访问，构造即抛 `LLMUnavailableError`）。
- 受限环境（preview/production）`LLM_ALLOWED_HOSTS` 为空 → 一律拒绝（阻断未批准外联）。
- 非受限环境（development/test）白名单为空 → 默认仅放行 loopback + RFC1918 私网。

```python
g = EndpointGuard(["model-endpoint", "10.0.0.0/8"], restricted=True)
g.allowed("http://model-endpoint:8001/v1") is True     # 私网端点放行
g.allowed("https://api.openai.com/v1") is False        # 公网 SaaS 拒绝
```

本机验证：`127.0.0.1,localhost`（受限）+ `http://127.0.0.1:8001/v1` → 放行 ✓；`https://api.openai.com/v1` → 拒 ✓；
受限+空白名单 → loopback 与私网 10.x 均拒 ✓（fail-closed）。

## 3. 评测驱动的写操作能力矩阵

`src/llm/capability.py::resolve_high_confidence_models(settings)` 是**唯一权威解析口**：

```
生效白名单 = HIGH_CONFIDENCE_MODELS（显式） ∩ { 评测报告中 write_op_pass==true 的模型 }
受限环境且 显式白名单为空 或 评测报告缺失 → frozenset()（fail-closed，无模型可写）
```

- 只有**通过写操作专项评测**（`write_op_pass=true`）的明确 model id 才能进入白名单；
- 判断依据是**模型名显式白名单**，不是分数阈值；未评测模型一律拒绝写并转人工。

本机验证（`evidence/llm_candidate_eval.json`，`self-hosted-model` 写操作专项通过）：

| 环境 | HIGH_CONFIDENCE_MODELS | llm_eval_report_path | 生效白名单 | capability_ok(self-hosted-model) |
|---|---|---|---|---|
| preview | `self-hosted-model` | 有报告且 write_op_pass=true | `{self-hosted-model}` | True |
| preview | 空 | 任意 | `{}` | False（fail-closed） |
| preview | `self-hosted-model` | 空/缺失 | `{}` | False（fail-closed） |
| preview | `self-hosted-model` | 报告 write_op_pass=false | `{}` | False（被排除） |

未评测的 `qwen2.5-max` 在报告存在且白名单含它时仍被排除（`capability_ok('qwen2.5-max')=False`）——白名单**只含真实评测通过的模型**。

## 4. 内网资金网关沙箱

`src/execution/sandbox_gateway.py` = 独立 HTTP 服务，SQLite **服务端持久化幂等**（不依赖内存 dict），
完全自托管、不触真实资金、提供 fagault 注入接缝。

| 端点 | 行为 |
|---|---|
| `POST /api/sandbox/submit` | 以 `(tenant_id, idempotency_key)` 唯一约束 + `ON CONFLICT DO NOTHING` 幂等；同键重复返回同一 `external_txn_id`/结果，绝不重复扣款/退款 |
| `GET /api/sandbox/query` | 按 `external_txn_id` 查最终状态（对账） |
| `POST /api/sandbox/compensate` | 以 `(tenant_id, idempotency_key, execution_id)` 派生**稳定 reversal_id**（`uuid5(NAMESPACE_URL,...)`），重发返回同一回执 |

```bash
SANDBOX_GATEWAY_PORT=8010 \
SANDBOX_GATEWAY_DB=data/sandbox_gateway.db \
SANDBOX_GATEWAY_API_KEY=<env注入> \
  .venv/Scripts/python.exe -m src.execution.sandbox_gateway
```

**调用方**：`src/execution/provider.py::SandboxHttpFundsProvider`（`EXECUTION_PROVIDER=sandbox_http`）。
配置 `gateway_base_url`；缺失即 `build_provider` 抛 `gateway_unconfigured`（fail-closed，绝不回退 mock）。

**客户端回调验签**：`src/execution/verification.py::verify_hmac_signature` 常量时间比对；`engine.apply_callback`
以 `(execution_id, nonce)` 做 CAS 非重放：签名错误→`signature_invalid` 拒绝；同 nonce 重投→`replay`（只重放不重复生效）；
陌生/冲突 nonce 或终态→`terminal_locked`/按状态机；金额/状态 unknown 或对账冲突→`mismatch`→`HUMAN_HANDOFF`。

## 5. 权威配置（部署用）

```dotenv
# --- LLM（完全自托管内网端点）---
LLM_BACKEND=openai_compatible
LLM_BASE_URL=http://127.0.0.1:8001/v1      # 生产：内网 host，如 http://model-endpoint:8001/v1
LLM_API_KEY=<server-env-inject-or-local>
LLM_MODEL=self-hosted-model
LLM_ALLOWED_HOSTS=127.0.0.1,localhost      # 生产：model-endpoint,10.0.0.0/8 等内网 CIDR
LLM_LOG_REDACT=true
LLM_TIMEOUT_SECONDS=10
LLM_MAX_RETRIES=2

# --- 写操作能力矩阵（评测驱动，白名单只含真实评测通过模型）---
HIGH_CONFIDENCE_MODELS=self-hosted-model
LLM_EVAL_REPORT_PATH=evidence/llm_candidate_eval.json

# --- 执行面 ---
EXECUTION_MODE=shadow                      # 恒为 shadow，绝不接真实资金
EXECUTION_PROVIDER=sandbox_http
GATEWAY_BASE_URL=http://127.0.0.1:8010
GATEWAY_API_KEY=<server-env-inject>
GATEWAY_TIMEOUT_SECONDS=10
EXECUTION_CALLBACK_HMAC_SECRET=<server-env-inject-random-64hex>
```

## 6. 已验证（本机）

- `scripts/evaluate_models.py` → `evidence/llm_candidate_eval.json`：`self-hosted-model` 六类 + 写操作专项全通过，
  24 用例 `passed=true`，`write_op_pass=true`，`any_write_failure=false`。
- 能力矩阵/EndpointGuard 受限环境解析（§3 表、§2 验证）全部符合 fail-closed 语义。
- 网关沙箱 `http://127.0.0.1:8010` 端到端可用；并发/回调验收见
  `evidence/sandbox_concurrency_stress.json`、`evidence/GATEWAY_SANDBOX_ACCEPTANCE_REPORT.md`、
  `evidence/CALLBACK_CONCURRENCY_REPORT.md`。
- `EXECUTION_MODE=shadow`：只生成待执行记录 + 模拟回执，未触真实资金、未切 live。

## 7. 边界与待办

1. **真实权重模型**：本环境端点引擎为项目确定性规则；真实 vLLM/Ollama 权重接入同一契约后需**重新评测**并更新白名单。
2. **真实资金通道**：本环境仅沙箱网关，未接真实资金渠道；`EXECUTION_MODE=shadow` 保持，切 live 需另行批准。
3. **公网/生产参数**：`LLM_ALLOWED_HOSTS`、`GATEWAY_BASE_URL`、`METRICS_ALLOWED_SOURCES` 应按实际内网 CIDR 替换，
   密钥一律环境变量注入。
