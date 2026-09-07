# 项目冻结记录

冻结日期：2026-09-07

## 冻结目标

本版本定位为个人开发者的低成本项目原型：单机、低并发、人工审批、Shadow、完全自托管。

## 冻结主链路

`售后请求 → LangGraph 路由 → CrewAI 选择受限工具 → 服务端租户校验 → human_approval → Shadow 执行 → 审计与幂等`

## 已验证

- 代表性端点上的 CrewAI 工具绑定与安全链路（测试环境）
- Ollama `qwen3:8b` 原生 `tool_calls` 与真实 CrewAI 三条敏感写工具探针均通过 3/3：退款、退货、改址均先创建 pending operation/approval，审批前执行数为 0，审批后 Shadow 执行一次，重复 `client_request_id` 不重复创建 operation
- 退款创建 operation/approval，审批前保持 pending
- 审批后才进入 Shadow 执行
- 重复请求幂等、跨租户拒绝、模型身份字段隔离
- 端点异常、非法工具结果和非白名单模型 fail-closed 转人工
- CrewAI/安全专项回归：以当前环境实际运行结果为准；跳过项不计入通过
- 全量回归：`432 passed, 35 skipped`，EXIT=0（本机 `.venv` 实测，2026-09-07）

## 明确不宣称

- 真实 Qwen 写探针证明的是本机端点到 CrewAI 工具与审批/幂等安全边界的闭环，不是生产可靠性或资金渠道证明；LangGraph `Command(resume=...)` 写路径由专项自动化测试验证，但该图级测试使用 Mock 路由，不冒充 Qwen 图级实测
- qwen3:8b 不进入 `HIGH_CONFIDENCE_MODELS`；生产写白名单仍要求独立 `write_op_pass=true` 评测报告
- 不宣称真实资金生产、不宣称真实租户上线
- 不宣称高并发、多机高可用、Graphiti/Neo4j 已落地；Milvus 仍为可选实验后端

## 验收与演示命令

```powershell
$env:RUN_CREWAI_INTEGRATION='1'
.accept-crewai-venv\Scripts\python.exe -m pytest -q tests/test_crewai_main_path.py tests/test_hardening_acceptance.py tests/test_llm_endpoint_gate.py
.venv\Scripts\python.exe scripts/run_acceptance.py
python scripts/live_e2e.py
```

## 冻结后变更规则

只接受安全修复、测试修复、证据口径修复和演示可复现性修复；不新增 Agent、向量库、图数据库、真实支付渠道或高并发基础设施。

备注：全量测试结束时 LiteLLM 可能输出一次异步客户端清理日志 traceback；该日志发生在测试已通过、进程退出阶段，不改变退出码或业务断言结果。

