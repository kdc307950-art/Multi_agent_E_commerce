# Ollama Qwen3 复测记录

日期：2026-09-05

## 结果

- Ollama 服务可达，模型 `qwen3:4b` 已安装。
- 原生 OpenAI-compatible 请求在部分尝试中返回标准 `query_order` `tool_calls`。
- 重复 CrewAI 查询复测未稳定通过，出现 `Invalid response from LLM call - None or empty`。
- 本次复测未修改写操作白名单；`evidence/llm_candidate_eval.json` 仍保持 `whitelist_eligible: false`。

## 收尾口径

该模型可作为“本地权重端点候选/不稳定实验”展示，不作为已通过的 CrewAI 运行时依赖。面试演示使用代表性端点测试和确定性安全链路；退款、退货、改址继续人工审批、Shadow、幂等和 fail-closed。
