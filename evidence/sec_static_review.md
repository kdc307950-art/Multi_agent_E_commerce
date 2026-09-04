# 静态安全/合规评审结论（t2 · security-auditor）

> 对 照 `<AGENTS.md 代理宪法>`（第一层编码工作宪法 + 第二层业务宪法），基于对 `deploy/` 配置、
> `docker-compose.preview.yml`、`deploy/nginx/preview.conf`、`docker-compose.observability.yml` 与
> `src/` 源码的**真实阅读**，逐条核对安全红线，并准备动态验证脚本/命令清单。
> 结论引用具体文件/函数/行，禁止臆测；凡依赖容器实机/真实 LLM/真实资金/真实 RLS 的部分一律标记为
> **未验证（静态）**，不作已验证宣称。
> 结构化全文见 `evidence/sec_static_review.json`。

---

## 一、红线核对结论（10 条，均满足）

| # | 红线 | 结论 | 关键实现证据 |
|---|---|---|---|
| RL-01 | 仅 nginx 暴露 80/443，数据面不发布宿主端口 | ✅ 满足 | `docker-compose.preview.yml` L27-42/44-55/73-147/149-183/185-205 全部仅 `networks: [internal]` 无 `ports`；仅 nginx L209-228/L211-213 有 `"80:80"/"443:443"`，挂 `edge+internal` 双网；`preview.conf` L6-18 仅 80 回 healthz/其余 301，L21-38 仅 443 ssl |
| RL-02 | 客户端身份不信任，user_id/tenant_id 取自服务端 TenantContext | ✅ 满足 | `src/auth/security.py` L350-389 `resolve_tenant_context` 仅从 Bearer token 解析；L179-216 `JwtAuthenticator.authenticate` 验签+iss/aud/exp+`require_active_membership`；`src/service/chat.py` L99-107 `input_state` 的 tenant/user/role 全部取自 ctx；`src/core/types.py` L228-230 thread_id 为不透明 uuid，不嵌入 user_id |
| RL-03 | 所有数据面带 tenant_id；跨租户默认拒绝；platform_admin 独立受控 | ✅ 满足 | `src/infrastructure/migrations.py` L29-206 业务表全带 tenant_id；L208-296 `ENABLE+FORCE RLS`，除 tenants 外全部；L299-318 `app_current_tenant_id()`/`checkpoint_thread_in_current_tenant()`；`postgres_store.py` L58-64 `_tx` 同连接同事务 `set_config(..., true)`；`checkpointer.py` L112-125 覆写 `_cursor` 同事务 RLS、L93-98 禁 `setup()` 防绕过；`src/core/types.py` L20-29 Role 不含 platform_admin |
| RL-04 | 三条敏感写必经唯一 human_approval，无 direct→execute 绕过 | ✅ 满足 | `builder.py` L96-116 三个 process 经 `write_approval_condition` 仅 `approve`→`human_approval`，否则 `handle_error`；L108-116 `human_approval` 经 `approval_result_condition` approved 才到对应 `execute_*`；L117-119 execute 仅边到 generate_response；`nodes.py` L202-289 `_execute` 复核存在/动作/thread/审批 approved/模型白名单/未执行 |
| RL-05 | 审批角色门控 + 二次确认 + 审计留痕 | ✅ 满足 | `routes.py` L278 `require_role(ctx,{'admin','approver'})`；L283-304 绑定复核；L306-308 二次确认；L318-324 CAS 抢占 `claim_approval_decision`；`postgres_store.py` L373-403 CAS 原子 claimed=False 不重复执行；L345-347 审计 |
| RL-06 | 租户级幂等，UNIQUE(tenant_id,idempotency_key)+ON CONFLICT DO NOTHING | ✅ 满足 | `migrations.py` L64-80 operations `UNIQUE(tenant_id,idempotency_key)`；L105-133 executions `UNIQUE(tenant_id,operation_id)`；`postgres_store.py` L234-254 create_operation `ON CONFLICT DO NOTHING`；L443-451 create_execution_record 同；`engine.py` L77-81 execute 先查既有→重放；L300-312 终态封闭；`src/core/types.py` L233-236 幂等键绝不含 attempt |
| RL-07 | 完全自托管，无第三方 SaaS；LLM 端点白名单 | ✅ 满足 | `compose.preview.yml` L103-109 LLM 内网端点 + `LLM_ALLOWED_HOSTS` 强制；`openai_compatible.py` L78-81 EndpointGuard 白名单不通过即 fail-closed；`security.py` L53-117 EndpointGuard；`compose.observability.yml` 全自托管，langfuse `TELEMETRY_ENABLED=false` L32，obs 网 `internal:true` L131-133 |
| RL-08 | 能力矩阵：写风险工具仅 HIGH_CONFIDENCE_MODELS 白名单模型（模型名显式白名单） | ✅ 满足 | `capability.py` L53-81 resolve_high_confidence_models = 显式白名单 ∩ 评测 write_op_pass=true，受限环境空/缺报告→frozenset(fail-closed)；`base.py` L52-59 capability_ok/is_write_capable；`nodes.py` L124-128（写前门控）/L243-248（执行复核）；`llm/__init__.py` L29-46 注入 |
| RL-09 | 受限环境 fail-closed + 受控执行面 shadow（不触真实资金） | ✅ 满足 | `main.py` L50-69 fail_closed_auth_guard（restricted + mock 或缺 secret → RuntimeError）；`auth/security.py` L358-362；`compose.preview.yml` L114-117 EXECUTION_MODE=shadow、PROVIDER=mock、回调密钥强制；`engine.py` L100-131 shadow 只生成模拟回执 |
| RL-10 | 全链路审计留痕 + PII 脱敏 + 指标不暴露高基数租户标签 | ✅ 满足 | `auth/security.py` L67-88 audit_security_denial；L41-61 `_redact_detail` 脱敏；`routes.py` L492-515 /audit（租户作用域、detail 再脱敏）与 /metrics（有界聚合、无 tenant_id）；`postgres_store.py` L614-640 audit 表；`llm/security.py` L18-50 redact() |

---

## 二、缺口 / 风险（需部署前关注）

| # | 缺口 | 级别 | 说明 |
|---|---|---|---|
| GAP-01 | LLM 模型名 vs 能力矩阵白名单名一致性 | 中 | 能力矩阵按模型名门控。当前评测报告 `evidence/llm_candidate_eval.json` 内为 `self-hosted-demo`（write_op_pass=true），而 compose.preview.yml `LLM_MODEL` 默认 `self-hosted-model`（L107）、`.env.preview.example` `LLM_MODEL=self-hosted-model`（L68）。部署时 `HIGH_CONFIDENCE_MODELS` 必须注入与 `LLM_MODEL` **一致**、且评测报告中 `write_op_pass=true` 的模型名，二者交集才非空；否则受限环境无任何可写模型（虽然安全 fail-closed，但会阻塞写流程）。 |
| GAP-02 | `LLM_ALLOWED_HOSTS` 通配符会放行一切出网 | 高（若误配） | `src/llm/security.py` L80-81：白名单含 `*`/`0.0.0.0/0`/`::/0` 时直接放行。受限环境虽然强制非空（compose L109），但未禁止通配符。建议对 restricted 环境拒绝通配符，或在 `check_secrets.sh` 增加拒绝。 |
| GAP-03 | 出网阻断为应用层白名单+私有桥接网，未强制网络层 internal=true | 中 | `docker-compose.preview.yml` L230-238 `internal` 网络未设 `internal:true`；L235-236 注释明确「如需更严格…可将本网络设为 internal:true」。若按宪法 1.8 更严格口径，建议部署时把 internal 设 `internal:true` 并把自托管 LLM 网关挂到同一网络。 |
| GAP-04 | promtail 挂载宿主 docker.sock 与容器日志目录 | 低-中 | `docker-compose.observability.yml` L121-123。虽 obs 网 internal:true，但 docker.sock 以 root 授予容器是宿主暴露面。建议最小挂载。 |
| GAP-05 | 依赖容器实机/真实 LLM/真实资金/真实 RLS 的部分本次静态未验证 | 提示 | 需在栈起来后由动态/验收任务补全（见动态脚本）。 |
| GAP-06 | customer 也能发起退款/退货/改址申请 | 低 | `nodes.py` L115-197 `_write_action` 未对申请做角色门控（任何角色可发申请，创建 pending operation+approval）。符合宪法「退款专家只触发审批不直接退款」，最终由 admin/approver 审批决定；需在验收文档明示，避免误判为越权写库。 |

---

## 三、动态验证脚本 / 命令清单（栈起来后直接复用）

| 脚本 | 命令 | 覆盖 |
|---|---|---|
| `scripts/verify_launch_gate.py` | `python scripts/verify_launch_gate.py --strict [--allowed-tenants ...] [--execution-mode shadow] [--storage-backend postgres]` | 首批租户非空、shadow 沙箱、仅审批后执行、全量审计、人工复核、数据面=postgres |
| `scripts/verify_preview_fail_closed.py` | `python scripts/verify_preview_fail_closed.py` | 受限环境 mock/缺密钥→启动失败；real+secret→允许 |
| `deploy/scripts/verify_fail_closed.sh` | `bash deploy/scripts/verify_fail_closed.sh` | 一次性 api 容器内 fail-closed 验证（等价容器形态） |
| `verify_capability_matrix_t2.py` | `python verify_capability_matrix_t2.py` | 能力矩阵白名单解析、受限环境 fail-closed、白名单∩评测交集、write_op_pass=false 排除 |
| `scripts/pg_rls_inventory.py` | `DATABASE_URL=postgresql://<superuser>@<host>:<port>/<db> python scripts/pg_rls_inventory.py` | 除 tenants 外全表 ENABLE+FORCE RLS、app_runtime 非 superuser/非 bypassrls、scope 函数存在、表 owner=迁移角色、app_runtime DML 授权；输出 `evidence/pg_rls_policies.json` |
| `scripts/audit_trace.py` | `python scripts/audit_trace.py --db <sqlite or DATABASE_URL> --tenant <t> [--thread/--approval/--operation/--action]` | 按租户/会话/审批/operation_id 四维追溯审计，detail 脱敏（平台级受控） |
| `deploy/scripts/check_secrets.sh` | `bash deploy/scripts/check_secrets.sh`（ENV_FILE 指向 inject 后的 .env.preview） | 缺 JWT/缺模型白名单/缺回调密钥/命中默认回调密钥/STRICT 非 true/缺 LLM_ALLOWED_HOSTS/缺评测报告→拒绝 |
| `scripts/docker_verify.py` | `python scripts/docker_verify.py` | Docker 拓扑端口收紧验证 |
| `scripts/pg_recovery_verify.py` / `scripts/pg_checkpoint_recovery_evidence.py` | `python scripts/pg_recovery_verify.py` / `python scripts/pg_checkpoint_recovery_evidence.py` | 备份恢复后数据面完整/租户分片、checkpoint 清理恢复证据 |

---

## 四、对验收标准的可验证点映射

- **连续观察期无跨租户**：RL-03 + 动态 `pg_rls_inventory` + 跨租户 API 调用应 404（`get_operation/get_approval/get_execution_record` 均 `AND tenant_id=:t`）。
- **无重复执行**：RL-06 + 同 `idempotency_key`/`operation_id` 重放应返回既有结果；回调经 `claim_callback` 记账 nonce + 终态封闭。
- **无审批绕过**：RL-04/RL-05 + 图结构无 direct 边 + `require_role(admin/approver)` + 二次确认 + CAS。
- **无未审计写操作**：RL-10 + `audit_trace` 四维追溯 + 拒绝路径 `audit_security_denial` 留痕。

---

*产出：`evidence/sec_static_review.json`（结构化全文）、本 markdown 摘要。t2 security-auditor。*
