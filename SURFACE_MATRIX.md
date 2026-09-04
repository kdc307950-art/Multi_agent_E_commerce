# SURFACE_MATRIX — 生产仅开放 5 项功能面门控验证

> **任务**：t5 · 只开放 5 项功能面门控验证（安全合规评审员 / security）
> **对照标准**：`AGENTS.md` 宪法 + 团队上线目标"功能裁剪：仅查询 / 工单创建 / 政策问答 / 退款建议 / shadow 记录"
> **验证对象**：`after-sales-prod`（生产栈）**路由面 + 工具面 + 配置 + 图结构**四级
> **日期**：2026-09-04
> **总判定**：**生产仅开放 5 项能力，资金敏感动作只进人工审批 + shadow，无任何真实写；live 执行 / 真实外部网关 / 模型写直通 一律关闭（PASS）。**

---

## 0. 验证范围与结论摘要

| # | 验证项 | 结论 |
|---|--------|------|
| 1 | API 路由面仅开放 5 项（查询/工单创建/政策问答/退款建议/shadow 操作记录） | ✅ PASS |
| 2 | 工具面仅开放只读 + 资格判定（写路径被 gate 到审批 + shadow） | ✅ PASS |
| 3 | `EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`、callbacks 仅 shadow、`launch_require_approval=true` | ✅ PASS |
| 4 | 无任何路径可绕过 `human_approval` 触达 `execute` | ✅ PASS |
| 5 | 关闭面：live 执行 / 真实外部网关 / 模型写直通 / CrewAI 委派 | ✅ PASS（已关闭） |

---

## 1. API 路由面矩阵（`src/api/routes.py`）

生产暴露的 HTTP 面为 **15 个路由**（含健康/指标/观测），其中 **业务功能面仅开放 5 项**，其余为受控治理 / 观测入口（非面向客户的功能，且按 RBAC 严格授权）。

| # | 路由 | 方法 | 能力归类 | 开放/关闭 | 说明 |
|---|------|------|---------|----------|------|
| 1 | `/api/orders/{order_id}` | GET | **① 查询** | ✅ 开放 | 只读订单+物流；服务端 `tenant_id` + 归属校验；跨租户/他人 → 404（`routes.py:602–630`） |
| 2 | `/api/sessions` | POST | **② 工单创建** | ✅ 开放 | 生成不透明 `thread_id` 工单/会话（`routes.py:289–298`）；RBAC `session.create` 留痕 |
| 3 | `/api/sessions` | GET | **② 工单创建** | ✅ 开放 | 会话列表（customer 仅本人 / staff 全租户）（`routes.py:301–309`） |
| 4 | `/api/sessions/{thread_id}` | GET | **② 工单创建** | ✅ 开放 | 会话/工单详情（`routes.py:312–317`） |
| 5 | `/api/sessions/{thread_id}/messages` | GET | **② 工单创建** | ✅ 开放 | 会话消息/工单内容（`routes.py:320–328`） |
| 6 | `/api/chat` | POST | **①/③/④ 统一入口** | ✅ 开放 | SSE；按意图路由 query_order/track_shipping（查询）、agentic_rag（政策问答）、process_*（退款建议/触发审批）（`routes.py:332–360`、`src/service/chat.py:72–189`） |
| 7 | `/api/approvals` | GET | **④ 退款建议** | ✅ 开放（读） | 查看审批建议列表（`routes.py:364–375`） |
| 8 | `/api/approvals/{approval_id}` | GET | **④ 退款建议** | ✅ 开放（读） | 单个审批建议（跨租户 → 404 + 审计 `approval_access_denied`）（`routes.py:378–391`） |
| 9 | `/api/approvals/{approval_id}/decision` | POST | **④ 人工审批** | ✅ 开放（治理） | **唯一 HITL 决策入口**：仅 `admin/approver`；跨租户 404；需显式 `confirmation` 二次确认；CAS 抢占幂等；拒绝→不执行（`routes.py:394–506`） |
| 10 | `/api/operations/{operation_id}` | GET | **⑤ shadow 操作记录** | ✅ 开放（读） | 操作状态/幂等键/结果（跨租户 404 + 审计）（`routes.py:510–521`） |
| 11 | `/api/executions/{execution_id}` | GET | **⑤ shadow 操作记录** | ✅ 开放（读） | 执行记录（mode/status/receipt）只读（跨租户 404 + 审计）（`routes.py:525–542`） |
| 12 | `/api/callbacks/{channel}` | POST | **网关回调（shadow 沙箱）** | ⚠️ 存在但仅 shadow 语义 | HMAC 验签 + nonce 防重放；生产 `provider=mock` 无外部网关触发，回调通路处于静默；不可信回调只写平台安全日志（`routes.py:546–598`） |
| 13 | `/api/members` | GET | 治理（成员列表） | ✅ 开放（仅 admin） | `require_role(ctx,{"admin"})`（`routes.py:634–643`） |
| 14 | `/api/audit` | GET | 治理（审计查询） | ✅ 开放（staff） | 租户作用域审计 + detail 脱敏（`routes.py:647–673`） |
| 15 | `/api/auth/login` | POST | 认证 | ✅ 开放（必须） | 仅 `real` 后端 + 已配置凭据；限流/退避/脱敏；否则 fail-closed（`routes.py:242–285`） |
| 16 | `/api/healthz` | GET | 健康 | ✅ 开放（免认证） | 仅报告存活，无租户数据（`routes.py:678–680`） |
| 17 | `/api/metrics` | GET | 观测 | ✅ 开放（内网） | Prometheus 聚合指标；来源 IP 白名单否则 403；无租户/高基数标签（`routes.py:715–721`） |

### 1.1 关闭的能力（无对应开放路由）
- **无 `/escalate` 公共入口**：人工升级由后端领域服务创建并通过上述资源呈现，不暴露第二个入口（`routes.py:9`）。
- **无任何 `execute_*` / `live` / `funds` 写路由**：执行只经 `human_approval` 审批后在图内由 `execute_*` 触发，且为 shadow。
- **无真实外部网关路由**：`/api/callbacks` 仅做验签回调（shadow 沙箱语义），不提供调用真实支付网关的写入口。

---

## 2. 工具面矩阵（`src/tools/*`）

| 工具 | 定义 | 能力归类 | 开放/关闭 | 说明 |
|------|------|---------|----------|------|
| `query_order` | `BUSINESS_TOOL_SCHEMAS` (`crewai_adapter.py:30–33`) | **① 查询** | ✅ 开放 | 只读；租户/归属校验（`adapter.py:94–100`） |
| `track_shipping` | `crewai_adapter.py:33–34` | **① 查询** | ✅ 开放 | 只读物流（`adapter.py:102–112`） |
| `get_order`/`get_shipping`/`search_policy` | `BusinessDataSource` (`data_source.py:33–57`) | **①/③ 读** | ✅ 开放 | 只读；强制 `tenant_id` 作用域；mock\|postgres（生产 postgres） |
| `process_refund` | `crewai_adapter.py:34–35`；`nodes.py:322` | **④ 退款建议** | ✅ 开放（写→审批） | 仅在 `nodes.py::_write_action` 内创建 operation+approval 置 `needs_approval=True`，**不进 execute**；`capability_ok` 门控（白名单） |
| `process_return` / `update_return_address` | `crewai_adapter.py:36–39`；`nodes.py:323–324` | **④ 退款建议** | ✅ 开放（写→审批） | 同上，资格判定 + 触发审批；不直接写库 |
| `escalate_ticket` | `crewai_adapter.py:40–42`；`nodes.py:199–200` | 转人工 | ✅ 开放 | 升级/转人工（无自决敏感写） |
| `execute_operation` | `adapter.py:183–203` | **仅供审批后执行** | ⚠️ 关闭（仅 shadow） | 唯一执行面；由 `human_approval` 触发；`_execution_mode=shadow` → 只产模拟回执 |
| `make_execution_engine` | `adapter.py:173–181` | 执行引擎 | ⚠️ 关闭（仅 shadow） | `mode=shadow`、`provider=None` |

### 2.1 关闭的工具/委派路径
- **`build_provider`**（`provider.py:222–241`）：生产 `execution_provider=mock` → `MockFundsProvider`；`sandbox_http` 需 `gateway_base_url`，缺失 fail-closed；生产未配置 `GATEWAY_BASE_URL`。
- **CrewAI / MCP 子智能体委派**：`crewai_enabled=False`（`config.py:226` 默认），业务节点走内置确定性路径（`crewai_adapter.py:4,137–145`）。**模型写直通在能力门控层被关闭**（见 §3.4）。
- **`.env.production` 无 `GATEWAY_BASE_URL` / `GATEWAY_API_KEY`** → 无真实外部网关。

---

## 3. 关键配置 / 执行面验证

### 3.1 `EXECUTION_MODE=shadow`
- **源码**：`config.py:203–209`（`execution_mode: str = "shadow"`）、`config.py:157–158` `src/execution/adapter.py::live_execution_enabled`（`execution_mode is LIVE and provider is not None`）。
- **生产配置**：`deploy/.env.production:80` `EXECUTION_MODE=shadow`；`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:57`。
- **执行引擎**：`src/execution/engine.py:76–80` `mode` 缺省 `SHADOW`；`:131–158` `_run_shadow` 生成确定性模拟回执（`receipt.simulated=true`、`mode=shadow`、立即收敛 `confirmed`），**不触真实资金**；`:160–170` `_run_live` 必须先有 provider，否则 `503`（fail-closed）。
- **证据**：`evidence/shadow_acceptance.json`（`execution_mode: shadow`；refund/return/address 各 37 检查全过，`receipt.simulated=true`、`mode==SHADOW`、`operation.status==EXECUTED`）。

### 3.2 `EXECUTION_PROVIDER=mock`
- **源码**：`provider.py:222–241` `build_provider`：`mock` → `MockFundsProvider`（确定性模拟，`provider.py:67–122`）。
- **生产配置**：`deploy/.env.production:81` `EXECUTION_PROVIDER=mock`；`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:57`。
- **适配器**：`adapter.py:230–232` `provider = _build_provider(settings) if mode is LIVE else None` —— shadow 下 `provider=None`，`live_execution_enabled=False`。

### 3.3 `execution_callbacks` 仅 shadow
- **源码**：`routes.py:546–598` `/api/callbacks/{channel}`（HMAC 验签 + nonce 防重放）；`callbacks` 只在执行记录处于可回调状态时被 `engine.apply_callback` 处理；executions.mode `CHECK (mode IN ('shadow','live'))`（`migrations.py:114`）。
- **生产**：`EXECUTION_PROVIDER=mock` 且未配置 `GATEWAY_BASE_URL` → **无外部网关可发回调**，回调通路静默；即便收到伪造回调，`signature_invalid`/`not_found` 等只写平台安全日志、不入租户审计（`routes.py:123–129,573–589`）。
- **证据**：`tests/test_callback_security_log.py`（回调安全日志/不可信回调不留租户审计）；`deploy/.env.production:82` `EXECUTION_CALLBACK_HMAC_SECRET`（随机密钥，非默认值，`deploy/scripts/check_secrets.sh:154–157` 强制非 `shadow-callback-secret`）。

### 3.4 `launch_require_approval=true` + 严格门控
- **源码**：`config.py:91` `launch_require_approval: bool = True`；`config.py:93–95` `launch_full_audit` / `launch_manual_review` 亦默认 true；`launch_gate.py:25–69` `verify_launch_gate`（`LAUNCH_REQUIRE_APPROVAL=false` → 违规；`EXECUTION_MODE=live + provider=mock` → 违规；受限环境缺白名单 → 违规；`storage_backend != postgres` → 违规）；`launch_gate.py:72–78` `enforce_strict` 违规即抛。
- **生产配置**：`deploy/.env.production:57–60` `LAUNCH_REQUIRE_APPROVAL=true`、`LAUNCH_FULL_AUDIT=true`、`LAUNCH_MANUAL_REVIEW=true`、`LAUNCH_GATE_STRICT=true`；`deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:54`。
- **批量/管理端点**：`deploy/scripts/check_secrets.sh:68–90,116–123,126–138`（`LAUNCH_GATE_STRICT` 非 true 即拒；`EXECUTION_MODE=live`+`mock` 即拒；`AUTH_CREDENTIAL_HASH` 非 argon2id\|bcrypt 即拒；`DEMO_SEED_ENABLED=true` 即拒）。

---

## 4. 无任何路径绕过 `human_approval` 触达 `execute`

| 层面 | 证据 | 结论 |
|------|------|------|
| **图结构** | `src/graph/builder.py:66–119`：`process_refund/process_return/update_return_address` 经 `write_approval_condition` → `{"approve":"human_approval", "handle_error":"handle_error"}`；`human_approval` 经 `approval_result_condition` → 仅 `execute_refund/execute_return/execute_address_update`；**无任何 `direct → execute_*` 边** | ✅ 无绕过 |
| **唯一审批入口** | `src/graph/approval.py:20–55`：唯一 `interrupt(payload)`；`validate_resume_params` 要求 `approved` 为 bool，非法 fail-closed 置 `REJECTED` | ✅ 唯一入口 |
| **写节点前置** | `src/graph/nodes.py:115–197` `_write_action`（能力门控→租户/订单校验→资格/金额/地址校验→失败转人工→创建 operation+approval 置 `needs_approval=True`） | ✅ 仅建审批单 |
| **执行节点 5 重复核** | `src/graph/nodes.py:202–289` `_execute`：①租户作用域 ②动作类型匹配 ③绑定 thread ④**审批状态必须 APPROVED** 且 approval→operation/thread/action 绑定一致 ⑤模型白名单；另有幂等重放、`REJECTED/HUMAN_HANDOFF` 拒绝执行 | ✅ 防御纵深 |
| **API 决策** | `src/api/routes.py:394–506` `decide_approval`：仅 `admin/approver`；跨租户 404；绑定复核；批准须显式 `confirmation`；CAS `claim_approval_decision` 幂等；拒绝→不执行 | ✅ 唯一执行触发点 |
| **工具层/子智能体** | `adapter.py:183–203` `execute_operation`（仅在审批后调用）；`crewai_adapter.py:14` "即使 CrewAI 被打开，写操作仍须经唯一 human_approval" | ✅ 工具层不绕过 |

> **结论**：无论是图结构、API 决策层、执行节点还是工具/子智能体层，**触达 `execute_*` 的唯一路径是 `human_approval` 的 `approved` 结果**；且执行节点自身又做 5 重复核 + 幂等，即使图被异常走到 `execute`，无 approved 审批也拒绝写（防御纵深）。

---

## 5. 验证脚本 / 测试证据

### 5.1 验证脚本（可复跑）
- **`scripts/verify_launch_gate.py`**：上线门控核验（`--strict`），覆盖少量租户 / 仅审批后执行 / 全量审计 / 人工复核 / postgres 数据面；`EXECUTION_MODE=live+mock` → 违规（`launch_gate.py:41–43`）。
- **`scripts/live_acceptance.py`** / **`evidence/run_shadow_acceptance.py`**：shadow 沙箱验收（模拟回执、幂等重放、归属拒绝）。
- **`deploy/scripts/check_secrets.sh`**：密钥/门控检查（`LAUNCH_GATE_STRICT`、`AUTH_CREDENTIAL_HASH`、`DEMO_SEED_ENABLED`、回调 HMAC 非默认）。

### 5.2 测试用例（关键）
| 测试 | 覆盖 |
|------|------|
| `tests/test_launch_gate_audit.py` | 门控：`test_verify_launch_gate_flags_live_with_mock_provider`、`test_launch_gate_ok_when_satisfied_and_strict_passes`、`test_enforce_strict_raises_on_violation`、`test_launch_allowlist_denies_non_allowlisted_tenant`（403 + 审计） |
| `tests/test_execution_engine.py` | shadow 模式 / live 缺 provider fail-closed / 幂等 / 状态机 / 补偿 |
| `tests/test_approval_idempotency.py` | 拒绝→REJECTED 无执行、已决策重放不重放执行 |
| `tests/test_e2e_flow.py` / `tests/test_sandbox_e2e_flow.py` | 端到端：查询/政策问答/退款建议→审批→shadow 执行 |
| `tests/test_callback_security_log.py` | 回调验签/不可信回调不写租户审计 |
| `tests/test_data_source_contract.py` | 只读数据源契约（租户作用域） |
| `tests/test_rls_bypass.py` / `tests/test_tenant_isolation.py` | 跨租户拒绝、RLS 隔离 |
| `tests/test_deploy_checks.py:136–158` | `check_secrets` 门控 `LAUNCH_GATE_STRICT` / 回调 HMAC 非默认 |

### 5.3 记录（真实运行产物）
| 记录 | 要点 |
|------|------|
| `evidence/shadow_acceptance.json` | `execution_mode=shadow`，37 检查全过；refund/return/address 各场景 `receipt.simulated=true`、`mode==SHADOW`、幂等重放同一 `execution_id`、归属/跨租户拒绝 |
| `deploy/.env.production:57–60,67,76,80–82` | `LAUNCH_REQUIRE_APPROVAL=true`、`LAUNCH_FULL_AUDIT=true`、`LAUNCH_MANUAL_REVIEW=true`、`LAUNCH_GATE_STRICT=true`、`LLM_BACKEND=mock`、`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__`、`EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock` |
| `deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-2055.md:54–59` | 三开关 + `LAUNCH_GATE_STRICT=true`；`EXECUTION_MODE=shadow`/`provider=mock`（沙箱不上真实资金）；`LLM_BACKEND=mock`；`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__` |
| `evidence/prod-go-live/security-auditor/RLS_ISOLATION_REPORT.md` §三 | 审批绕过检查 PASS（无 direct 边、5 重复核、`platform_admin` 非审批角色） |
| `evidence/E2E_APPROVAL_RECORD.md` / `evidence/E2E_ACCEPTANCE_REPORT-20260904-1310.md` | 端到端审批记录 |

---

## 6. 结论

生产**仅开放 5 项能力**（查询 / 工单创建 / 政策问答 / 退款建议 / shadow 操作记录），资金敏感动作（退款/退货/改址）**只进入唯一人工审批 + shadow**，不产生真实写。逐项确认：

1. ✅ API 路由面（`src/api/routes.py`）与工具面（`src/tools/*`）仅开放上述 5 项业务能力；
2. ✅ `EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`、callbacks 仅 shadow 语义、`launch_require_approval=true`（+ `launch_gate_strict`/`full_audit`/`manual_review` 全 true）；
3. ✅ 无任何路径可绕过 `human_approval` 触达 `execute_*`（图结构 / API 决策 / 执行节点 5 重复核 / 工具层均不绕过）；
4. ✅ 关闭面确认关闭：**live 执行**（`EXECUTION_MODE=shadow`、`provider=None`、`live_execution_enabled=False`、live 缺 provider fail-closed）、**真实外部网关**（`EXECUTION_PROVIDER=mock`、未配置 `GATEWAY_BASE_URL`）、**模型写直通**（`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__` → 能力矩阵空集 → 写操作 fail-closed 转人工；且即便白名单模型仍需人工审批）、**CrewAI/MCP 子智能体委派**（`crewai_enabled=False`）。

### 已知限制（如实，非本矩阵达标项）
- **`/api/callbacks/{channel}` 端点存在**：它在 `EXECUTION_PROVIDER=mock` 下无外部网关注入回调，处于静默；生产如需接 `sandbox_http`/`live` 网关须另行评审并满足 `GATEWAY_BASE_URL`、`launch_require_approval`、写模型评测等前置。放行前应与 `SECURITY_ATTESTATION.md` §8（受信 CA / 真实租户注入 / 写模型评测 / 迁移复核）一并闭环。
- **`HIGH_CONFIDENCE_MODELS=__NO_VERIFIED_WRITE_MODEL__`**：当前无模型通过写操作评测（`write_op_pass=true`），故**所有写操作 fail-closed 转人工**，即"退款建议"只能触发审批，无人能自动执行——属设计上的安全默认态。

— *security（安全合规评审员）* · 2026-09-04
