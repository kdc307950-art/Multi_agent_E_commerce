# Ollama Qwen3 8B 重试记录

日期：2026-09-07

## 复测环境

- Ollama：本机 `http://127.0.0.1:11434`
- 模型：`qwen3:8b`
- CrewAI：`.accept-crewai-venv` 中的 `0.152.0`
- 端点：OpenAI-compatible `/v1/chat/completions`
- `PYTHONUTF8=1`
- `LLM_DISABLE_THINKING=true`

## 结果

### 1. 原生工具调用协议

执行：

```powershell
$env:OLLAMA_MODEL='qwen3:8b'
$env:PYTHONUTF8='1'
.\.accept-crewai-venv\Scripts\python.exe scripts\verify_ollama_tool_call.py
```

结果：

```text
[PASS] qwen3:8b returned query_order(ORD-001) via native tool_calls
[INFO] Protocol compatibility only; write whitelist remains unchanged.
```

该结果只证明固定请求下的 OpenAI-compatible `tool_calls` 协议兼容性。

### 2. 真实 CrewAI 查询工具执行

使用 `CrewAIToolRouter(enabled=True)`，绑定真实 `query_order` CrewAI 工具，连续运行 3 次。每次均出现 CrewAI 工具调用记录，且工具实际执行计数为 `1`：

| 运行 | 耗时 | 工具 | 执行计数 | 结果 |
|---:|---:|---|---:|---|
| 1 | 18.04s | `query_order` | 1 | `状态:delivered 金额:299.0 商品:无线耳机(x1)` |
| 2 | 6.49s | `query_order` | 1 | `状态:delivered 金额:299.0 商品:无线耳机(x1)` |
| 3 | 5.21s | `query_order` | 1 | `状态:delivered 金额:299.0 商品:无线耳机(x1)` |

### 3. 专项自动化测试

执行：

```powershell
$env:RUN_OLLAMA_PROBE='1'
$env:OLLAMA_MODEL='qwen3:8b'
$env:PYTHONUTF8='1'
.\.accept-crewai-venv\Scripts\python.exe -m pytest -q tests/test_crewai_main_path.py
```

结果：

```text
19 passed in 4.88s
```

## 判定与边界

- 本地 Qwen3 8B 原生 `tool_calls`：通过。
- 真实 CrewAI `query_order` 工具调用：本次 3/3 实际执行通过。
- `qwen3:8b` 仍不加入 `HIGH_CONFIDENCE_MODELS`，`evidence/llm_candidate_eval.json` 继续保持 `whitelist_eligible: false`。
- 该复测只覆盖只读查询，不证明退款、退货或改址写操作能力，也不改变人工审批、Shadow、幂等和 fail-closed 约束。
- 运行期间曾出现 Windows 控制台 GBK EventBus 编码提示；不影响工具执行。演示和证据应以工具调用记录、真实工具返回值和测试结果为准。
