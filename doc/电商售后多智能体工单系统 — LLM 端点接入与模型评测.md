# 电商售后多智能体工单系统 — 自托管 LLM 端点接入与模型评测

> **定位**：本文档是「错误处理与回退机制」中**能力矩阵**与「生产基线与验收测试」§五「LLM 端点」的落地专项，
> 覆盖**自托管 LLM 端点接入**、网络白名单 / 超时 / 有界重试 / 脱敏日志，以及**评测驱动的写操作能力矩阵**。
> **核心结论**：写风险工具只放行**经过写操作专项评测**（`write_op_pass=true`）的明确 model id；未通过的模型
> 只能处理查询类任务，敏感写操作带完整状态转人工。本阶段**不依赖** Graphiti / Neo4j / Milvus / CrewAI。

---

## 0. 目标与验收口径

| 目标 | 验收方式 |
|---|---|
| 真实模型链路可用 | `LLM_BACKEND=openai_compatible` 指向自托管端点，真实 SSE 链路可分流/回复（`verify_llm_chain.py [A]`；`test_*.py::test_*_via_api`） |
| 异常输出与超时不会触发执行 | 端点 5xx/超时/非法输出 → `LLMUnavailableError`/`LLMOutputError` → 节点 fail-closed 转人工，不产生 approval/执行（`test_endpoint_5xx_*` / `test_timeout_*`） |
| 未白名单模型不达退款/退货/改址审批执行链 | 非白名单模型写申请 → SSE 仅 `error`（`model_not_in_whitelist`），`operation_id=null`（`test_non_whitelist_model_write_fails_closed_via_api`） |

---

## 1. 自托管 LLM 端点接入

**完全自托管红线**：LLM 端点只能落在项目方控制的服务器/网络边界内（自管机房或项目账户下的租赁 IaaS），
数据只在这些边界内流转；绝不把 prompt/响应转发到未批准的公有 SaaS。

- `LLM_BACKEND=openai_compatible`：调用本地/内网 OpenAI 兼容 `/chat/completions`（vLLM/Ollama/自管服务）。
- 本项目提供 `src/llm/self_hosted_server.py`：一个**项目方自管的 OpenAI 兼容端点服务**（复用 `MockLLM` 规则引擎，
  暴露 `GET /v1/models` 与 `POST /v1/chat/completions`），用于在无真实权重的环境里做本地/内网链路验收。真实部署
  可替换为任意自托管 OpenAI 兼容端点。
- 端点绑定内网（默认 `127.0.0.1:8001`），不得暴露公网。

```bash
# 启动自托管端点（模型名 self-hosted-model，默认不在写白名单）
DSH_LLM_ENDPOINT_MODEL=self-hosted-model uvicorn src.llm.self_hosted_server:app \
    --host 127.0.0.1 --port 8001
```

---

## 2. 网络白名单 / 超时 / 有界重试 / 脱敏日志（`OpenAICompatibleLLM`）

| 能力 | 机制 | 配置 |
|---|---|---|
| 端点网络白名单 | `EndpointGuard`：按 host/IP/CIDR 校验 `LLM_BASE_URL`；非受限环境默认仅放行 loopback + RFC1918 私网；受限环境留空即 fail-closed | `LLM_ALLOWED_HOSTS` |
| 单调用超时（deadline） | `httpx.Timeout(timeout)`，不叠加多份超时 | `LLM_TIMEOUT_SECONDS` |
| 有界重试 + 退避 | 可重试状态码 `{408,409,429,500,502,503,504}` 指数退避 + jitter，`max_retries` 上限 | `LLM_MAX_RETRIES` |
| 脱敏日志 | `security.redact()` 掩码手机号/地址/订单号/密钥/授权头；`OpenAICompatibleLLM` 日志只打印脱敏内容 | `LLM_LOG_REDACT` |

```python
# 端点不在白名单 → 构造即失败（fail-closed，绝不访问未批准端点）
guard = EndpointGuard(["model-endpoint", "10.0.0.0/8"], restricted=True)
assert guard.allowed("http://model-endpoint:8001/v1") is True   # 私网端点放行
assert guard.allowed("https://api.openai.com/v1") is False      # 公网 SaaS 拒绝
```

---

## 3. 评测驱动的写操作能力矩阵（核心）

**红线**：写风险工具（`process_refund` / `process_return` / `update_return_address`）只允许 `HIGH_CONFIDENCE_MODELS`
白名单内模型调用；判断依据是**模型名显式白名单**，不是分数阈值。只有**通过写操作专项评测**的明确 model id 才能进入白名单。

### 3.1 唯一权威解析口：`src/llm/capability.py`

```python
def resolve_high_confidence_models(settings) -> frozenset[str]:
    # base = settings.high_confidence_models（显式白名单）
    # report = 读 llm_eval_report_path 的《写操作专项评测报告》
    # passed = {模型 : write_op_pass == true}
    # 生效 = base ∩ passed
    # 受限环境：base 为空 或 report 缺失 → frozenset()（fail-closed，无模型可写）
```

- `BaseLLM.capability_ok` / `nodes.py` 的写、执行复核一律走此策略，不再读 `mock.py` 模块常量。
- `HIGH_CONFIDENCE_MODELS` 的**默认开发演示值**仅供本地/测试让 mock 走通审批路径；生产/受限环境必须由
  配置注入 + 评测报告门控。

### 3.2 候选模型评测（六类通用 + 写操作专项）

`src/llm/eval/`：
- 测试集（`cases.py`）：**意图识别、参数提取、政策问答忠实度、低置信度、端点不可达、恶意输入** 六类 +
  **写操作专项**（refund / return_request / return_address 三条敏感写路径）。
- 评测 runner（`runner.py`）：`evaluate_model(llm)` → `ModelEvaluation`，逐用例断言 + 汇总各维度 + `write_op_pass`。

```bash
# 对候选模型（真实自托管端点）跑评测，产出写操作专项报告
python scripts/evaluate_models.py --llm-backend openai_compatible \
    --model-name <model-id> --base-url http://<endpoint>/v1 \
    --output evidence/llm_candidate_eval.json
```

只有报告里 `write_op_pass=true` 的 model id 才有资格写入 `HIGH_CONFIDENCE_MODELS`（见 §3.1）。

---

## 4. 降级 / 安全回退（写操作绝不降级执行）

| 触发条件 | 行为 |
|---|---|
| 模型不在白名单 / 能力校验失败 | 不调用写工具；保留 `operation_id` 与已验证订单信息；转人工 |
| LLM 超时 / 429 / 5xx / 结构化输出失败 | 不执行写入；有界重试超限 → 转人工；记录错误分类与 deadline |
| 金额 / 资格 / 地址 / 归属异常 | 立即停止；冻结本次 `operation_id` 自动执行；转人工 |
| 审批恢复失败 / 检查点作用域失败 | 保持 pending，禁止自动放行或重建操作 ID；转人工 |

**硬规则**：写路径任何回退都不得切换到低档模型执行，不得跳过 `human_approval`，不得清除/重建原 `operation_id`。
人工接手拿到最小必要状态：`tenant_id`、`thread_id`、`operation_id`、`approval_id`、业务意图、已验证事实、失败分类、下一步动作。

---

## 5. 前置排除项（本阶段不做）

**Graphiti、Neo4j、Milvus、CrewAI 暂不作为本阶段上线前置条件。** 本阶段政策问答走确定性 keyword 检索 +
RAG 忠实度校验（`LLM_MODEL` 经评测后可启用）；Milvus（向量）/ CrewAI（子智能体）/ Graphiti+Neo4j（长期记忆）均属后续迭代项，
不阻塞本阶段上线；接入前须按《记忆架构设计》《生产基线与验收测试》验证租户隔离与自托管。

---

## 6. 证据与脚本

| 证据 | 位置 |
|---|---|
| 写操作专项评测报告 | `evidence/llm_candidate_eval.json`（`scripts/evaluate_models.py` 产出） |
| 三验收项脚本 | `scripts/verify_llm_chain.py`（真实链路 / 异常不执行 / 非白名单不达审批链） |
| 自动化测试 | `tests/test_llm_endpoint_gate.py`（16 项：能力矩阵/网络白名单/脱敏/验收） |

---

*文档版本 1.0 · 2026 · LLM 端点接入与模型评测专项 · 与「错误处理与回退机制 / 工具调用与集成 / 生产基线与验收测试 / Agent 宪法」配套。*
