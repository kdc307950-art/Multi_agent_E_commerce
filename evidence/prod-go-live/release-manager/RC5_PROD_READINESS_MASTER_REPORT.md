# RC5_PROD_READINESS_MASTER_REPORT — 生产候选版本聚合报告

> 审查日期：2026-09-05  
> 当前分支：`codex/prod-readiness`  
> 历史生产候选锚点：`release/v1.0.0-rc5-candidate` → `8d44e80`
> 当前面试交付锚点：`interview-freeze-2026-09-05` → `682ecdd`
> 审查基线时工作树：`git status --porcelain` clean；本次发布收口优化会产生待提交文档变更

## 结论

当前版本是**生产候选版本**，不是正式生产版本。系统处于 `S1 shadow + fail-closed`，正式生产放量判定为 **NO-GO**。

已完成的工程能力包括：多租户认证与隔离、统一 SSE 对话入口、退款/退货人工审批、租户级幂等、CrewAI 主链路契约、沙箱网关运行态验证、回滚和灾备演练设计。

以下事项仍未闭环：真实权重模型能力、生产 PostgreSQL/RLS 专栈复验、真实租户书面确认、受信 TLS、目标生产服务器、真实资金渠道、生产观测密钥与 7 天 Shadow。代表性自托管端点上的 CrewAI 工具调用已在独立环境完成验证。

## 阶段状态

| 阶段 | 状态 | 证据边界 |
|---|---|---|
| 0 版本冻结 | 完成 | Git/tag/发布规则 |
| 1 基础业务与租户隔离 | 基本完成 | 单元/契约测试 |
| 2 CrewAI 主链路 | 接入完成；真实执行未验证 | stub/fake CrewAI 契约 |
| 3 沙箱执行 | 已验证 | Docker 沙箱/合成回执 |
| 4 预发布硬化 | 评估完成；外部条件阻塞 | 配置与静态/演练证据 |
| 5 真实租户 Shadow | 未开始 | 缺真实租户和目标服务器 |
| 6 小流量 Live | 未开始 | 缺真实模型和资金渠道 |
| 7 正式放量 | NO-GO | 未满足放量门控 |

## 测试口径

当前工作树最新回归为 `416 passed / 0 failed / 35 skipped / EXIT=0`（2026-09-05）。35 个跳过项主要是未配置 `DATABASE_URL` 的 PostgreSQL 数据面测试，另有 1 项 Ollama 探针默认跳过；该数字不能等同于 CrewAI + Qwen 运行时或生产验证通过。

2026-09-05 的补充验证确认：CrewAI/LLM 专项 `41 passed / 1 skipped`，全量回归 `416 passed / 35 skipped`。Ollama `qwen3:4b` 原生 `tool_calls` 探针曾成功但重复运行不稳定；CrewAI + Qwen 运行时仍待适配，写白名单保持关闭。该结果不覆盖历史 RC5 发布标签。

## 生产阻塞项

1. 真实自托管权重模型端点和写操作专项评测。
2. 真实权重模型 + LLM 集成测试。
3. 目标生产 PostgreSQL、RLS、备份恢复和告警复验。
4. 生产域名和受信 CA TLS。
5. 真实租户确认函、PHC 登录凭据和审批角色。
6. 资金网关/渠道联测、回调验签和对账。
7. 生产观测密钥、Langfuse trace、Alertmanager 和连续 7 天 Shadow。

## 放量前强制顺序

真实模型评测 → 真实 CrewAI 链路 → 生产 PG/RLS → 目标服务器硬化 → 真实租户 Shadow 7 天 → 小流量 Live → 分租户扩容。

任何一步失败，都必须保持 `shadow` 或转人工，不得通过低档模型、客户端字段或 direct execution 绕过审批。
