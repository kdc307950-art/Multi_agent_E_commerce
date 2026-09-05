# PostgreSQL/RLS 工作树实机验证证据

> 日期：2026-09-05  
> 基线：`release/v1.0.0-rc5-candidate` 后待提交工作树  
> 环境：本机 Docker Engine 29.7.2，独立临时容器 `postgres:17-alpine`  
> 网络：仅绑定 `127.0.0.1:55432`，未复用 preview/production 数据卷

## 结论

PostgreSQL 数据面原有 33 个跳过测试已全部实际执行并通过：

```text
33 passed, 412 deselected in 16.47s
```

非 PostgreSQL 测试因单次工具等待窗口限制分成两批运行：

```text
295 passed, 1 skipped, 2 deselected in 24.15s
116 passed, 31 deselected in 14.07s
```

综合当前工作树：

```text
444 passed
0 failed
1 skipped
```

当前工作树最新全量回归为 `416 passed / 35 skipped`；跳过项主要是未配置 `DATABASE_URL` 的 PostgreSQL 数据面测试，另有 1 项 Ollama 探针默认跳过。代表性端点的 CrewAI 安全链路已验证；Ollama `qwen3:4b` 原生 `tool_calls` 曾成功但重复运行不稳定，CrewAI + Qwen 运行时仍未宣称通过。

## 本轮发现并修复

1. PostgreSQL 审计硬化测试绕过 `audit_security_denial`，既缺少时间参数，也未走实际脱敏入口；已改为验证真实安全审计路径。
2. 恢复一致性测试在普通事务中执行 `DROP/CREATE DATABASE`；已改为连接管理库并使用 `AUTOCOMMIT`。
3. SQLAlchemy `URL` 转字符串时会把密码隐藏为 `***`；恢复库连接改用 `render_as_string(hide_password=False)`。
4. 测试集群保留既有 `app_runtime` 角色时，辅助函数未同步当前测试凭据；现支持幂等角色更新，并继续强制 `NOBYPASSRLS`。

## 证据边界

本证据证明当前工作树在独立 PostgreSQL 17 实例上的 RLS、租户隔离、并发回调、checkpoint、审计和恢复一致性测试通过，并补充确认代表性自托管端点上的 CrewAI 工具实际执行。它不证明目标生产服务器、PostgreSQL HA、真实租户、真实权重模型或真实资金渠道已经就绪。

## CrewAI/模型实机复验（同日）

- `GET http://127.0.0.1:8001/v1/models`：返回 `self-hosted-model`，端点可达。
- `scripts/evaluate_models.py --llm-backend openai_compatible`：专项评测报告显示 `write_op_pass=true`。
- `.accept-crewai-venv` 已安装 CrewAI，真实集成测试已实际启动 CrewAI → LiteLLM → 本地端点。
- 代表性端点上的真实集成测试已通过：CrewAI 实际执行绑定工具并完成查询/退款审批前置链路。
- 端点源码 `src/llm/self_hosted_server.py` 明确复用 `MockLLM` 规则引擎，因此该端点属于**自托管代表性/Mock 引擎**，不是真实权重模型。

结论：本轮完成了 CrewAI 运行环境和本地代表性端点的真实工具调用验证；**真实权重模型工具调用能力仍 BLOCKED**。适配层已修复 LiteLLM 对 OpenAI-compatible 模型 provider 前缀的要求（`openai/<model>`）。
