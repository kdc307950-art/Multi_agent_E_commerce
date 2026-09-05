# 面试版冻结记录

冻结日期：2026-09-05

## 冻结目标

本版本定位为个人开发者的低成本面试作品：单机、低并发、人工审批、Shadow、完全自托管。

## 冻结主链路

`售后请求 → LangGraph 路由 → CrewAI 选择受限工具 → 服务端租户校验 → human_approval → Shadow 执行 → 审计与幂等`

## 已验证

- 代表性自托管 OpenAI-compatible 端点上的真实 CrewAI 工具调用
- 查询订单工具实际执行
- 退款创建 operation/approval，审批前保持 pending
- 审批后才进入 Shadow 执行
- 重复请求幂等、跨租户拒绝、模型身份字段隔离
- 端点异常、非法工具结果和非白名单模型 fail-closed 转人工
- CrewAI 专项回归：`74 passed, 1 skipped`
- 全量回归（`RUN_CREWAI_INTEGRATION=1`）：`414 passed, 34 skipped`，EXIT=0

## 明确不宣称

- 当前端点不是 7B/8B 真实权重模型
- 不宣称真实资金生产、不宣称真实租户上线
- 不宣称高并发、多机高可用、Graphiti/Milvus 已落地

## 面试演示命令

```powershell
$env:RUN_CREWAI_INTEGRATION='1'
.accept-crewai-venv\Scripts\python.exe -m pytest -q tests/test_crewai_main_path.py tests/test_hardening_acceptance.py tests/test_llm_endpoint_gate.py
python scripts/live_e2e.py
```

## 冻结后变更规则

只接受安全修复、测试修复、证据口径修复和演示可复现性修复；不新增 Agent、向量库、图数据库、真实支付渠道或高并发基础设施。

备注：全量测试结束时 LiteLLM 可能输出一次异步客户端清理日志 traceback；该日志发生在测试已通过、进程退出阶段，不改变退出码或业务断言结果。
