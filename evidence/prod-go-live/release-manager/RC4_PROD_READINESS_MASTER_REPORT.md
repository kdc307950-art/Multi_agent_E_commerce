# RC4_PROD_READINESS_MASTER_REPORT — 生产化四阶段聚合就绪评估（最终收口）

> **产出国角色**：release-manager（发布经理）· rc4-productionization 团队 · 任务 t6（最终收口）
> **范围**：阶段0~四的全部工程与证据（rc4-candidate 阶段一收口 + 阶段二 CrewAI 主链路 + 阶段三 Docker 运行态验证 + 阶段四 预发布硬化/安全审计）。
> **诚实铁律**：凡**真实自托管模型端点、真实租户书面确认、受信 CA/域名、目标服务器、7 天 shadow、真实资金 live、真实渠道**等外部输入未到位，一律 `BLOCKED-需外部`，**绝不虚报达标/放量**。凡"完整 prod 栈带起健康 + 运行时 RLS/告警复验 + 对账/指标重建"未实际执行的，标注为部署期执行项。
> **权威测试口径**：阶段一（rc4-candidate）= **396 passed / 34 skipped / EXIT=0**；阶段二~四综合（rc5-candidate）= **411 passed / 34 skipped / EXIT=0**。凡有出入以本报告为准；阶段一 396/34 为历史基线、已被综合 411/34 取代。

---

## 0. 一句结论

**阶段0~四的工程与证据已在 `codex/prod-readiness` 分支落地并验证**（RC4-candidate 基线冻结、CrewAI 主链路接入、Docker 沙箱网关运行态实测、目标服务器预发布硬化评估、跨切面安全审计）；**但正式生产放量当前为 `NO-GO`**——受**真实资金/gateway、真实租户书面确认、真实自托管模型端点、受信 CA/域名、目标服务器、7 天 shadow** 等外部依赖阻断（BLOCKED-需外部）。系统处于 **S1 shadow + fail-closed** 安全默认态，符合"先能力后放量"门控；"放量/切 live" 红线（未审批即执行、同操作重复执行、跨租户成功、对账差异、回调验签失效、PG 恢复失败）为零且不可触发（阶段二~三已证）。

---

## 1. 阶段0~四 状态与证据路径

| 阶段 | 主题 | 状态 | 关键证据路径 |
|---|---|---|---|
| **阶段0** | git 基线确立 / 发布锚点 | ✅ | `evidence/prod-go-live/release-manager/BASELINE_LOCK.md`（rc3 锚定实测）、`RC4_PROD_READINESS_MASTER_REPORT.md`（本报告）；`release/v1.0.0-rc4-candidate`=e62d7fe（阶段一基线）、`release/v1.0.0-rc5-candidate`=本次综合提交 |
| **阶段一** | RC4-candidate 基线冻结 + 权威测试（阶段一收口） | ✅ | `evidence/prod-go-live/release-manager/FINAL_ACCEPTANCE.md`、`GO_NO_GO.md`；测试 `evidence/prod-go-live/test-runner/TEST_EVIDENCE_RC4.md`（396/34，历史） |
| **阶段二** | CrewAI 退款主链路接入 LangGraph 主图 | ✅ 接入/契约/安全行为；🔒 真实 crewai 执行链 BLOCKED | `evidence/prod-go-live/acceptance-engineer/CREWAI_MAINPATH_VERIFY.md`、`tests/test_crewai_main_path.py`（10 用例）、`src/graph/builder.py`、`src/graph/state.py`、`src/tools/crewai_adapter.py` |
| **阶段三** | Docker 沙箱网关运行态验证（幂等/回调/故障/对账） | ✅ 沙箱网关运行态全部 PASS；🔒 生产网关沙箱+实时对账部署期 | `evidence/prod-go-live/deploy-engineer/SANDBOX_GATEWAY_RUNTIME_VERIFY.md`、`IDEMPOTENCY_RUNTIME.md`、`CALLBACK_SECURITY_RUNTIME.md`、`FAULT_INJECTION_RUNTIME.md`、`RECONCILIATION_RUNTIME.md`、`runtime/runtime_verify_report.json` |
| **阶段四** | 预发布硬化 + 安全审计 | ✅ 就绪评估/红色红线；⚠️ 4/5 轴 BLOCKED-需外部；❌ F1 功能缺陷（当前树已修） | `evidence/prod-go-live/deploy-engineer/TARGET_SERVER_PRE_RELEASE_HARDENING.md`、`HARDENING_VERIFY_EVIDENCE.md`、`INFRA_READINESS_GAP.md`；`evidence/prod-go-live/security-auditor/CREWAI_WIRING_SECURITY_AUDIT.md` |

---

## 2. 权威测试数字

> 单一权威口径：**rc5-candidate 综合 = 411 passed / 0 failed / 34 skipped / EXIT=0**（37.19s）。阶段一 rc4-candidate = 396/34（历史基线，已被取代）。

| 项 | rc4-candidate（阶段一） | rc5-candidate（阶段二~四综合，权威） |
|---|---|---|
| collected | 430 | **445** |
| passed | 396 | **411** |
| failed | 0 | **0** |
| skipped | 34 | **34** |
| EXIT | 0 | **0** |
| 时长 | 41.92s | **37.19s** |
| 权威日志 | `test-runner/pytest_captain_baseline.log` | `test-runner/pytest_rc5_final.log` |
| 证据 | `TEST_EVIDENCE_RC4.md` | `TEST_EVIDENCE_RC5.md` |

**skipped 构成（二者一致，可追溯）**：34 = **33 项 PostgreSQL 数据面**（无 `DATABASE_URL`）+ **1 项 CrewAI 真实调用链**（rc4: `test_hardening_acceptance.py:384`；rc5: `:394`，需 crewai + 自托管 LLM 端点，置 `RUN_CREWAI_INTEGRATION=1`）。逐项 node 位置见 `TEST_EVIDENCE_RC5.md` §2。

**差异说明**：综合态相对阶段一**净增 15 个通过用例**（+15 passed、0 新增失败/跳过）——含 `tests/test_crewai_main_path.py` 10 例（阶段二 CrewAI 主链路验收）+ 阶段二~四其它新增/修复通过的用例。**阶段二~四未破坏阶段一 396/34 基线。**

**过程口径说明（诚实）**：t4/t5 期间 security-auditor 记录 **406 passed, 34 skipped**、crewai-engineer 记录 **406 passed, 1 skipped, 33 deselected**（非 PG 口径）。这些是**工作过程中间观测**，被本次**最终权威运行 411/34** 取代；本报告以 411/34 为唯一权威数字。

---

## 3. CrewAI 主链路验证

> **证据**：`evidence/prod-go-live/acceptance-engineer/CREWAI_MAINPATH_VERIFY.md` + `tests/test_crewai_main_path.py`（10 用例，→ 10 passed）+ `src/graph/builder.py` / `src/graph/state.py` / `src/tools/crewai_adapter.py`。

| 验证点 | 结论 | 依据 |
|---|---|---|
| `build_crewai_router` / `run_business_task` 被主请求路径调用 | ✅ PASS | `crewai_enabled=True` + order/refund/complaint 意图 → 路由到 `crewai_refund_agent` 并调用 `run_business_task`（stub router 测试级确证：`test_crewai_enabled_refund_calls_run_business_task_and_hits_human_approval`） |
| 写意图经**唯一 `human_approval`** | ✅ PASS | refund 经 crewai 后 `needs_approval=True` + `approval_id` → 唯一 `human_approval` 中断；审批通过才 `execute_refund`（`test_crewai_enabled_refund_executes_only_after_approval`：审批前 pending、审批后 executed） |
| **无 `direct → execute` 绕过边** | ✅ PASS | 图结构断言：`crewai_refund_agent` / `process_refund` / `process_return` / `update_return_address` 均**无**任何以 `execute` 开头的目标边；写路径只能经 `human_approval`（`test_crewai_*_graph_has_no_crewai_to_execute_direct_edge`） |
| 身份只来自服务端 `TenantContext` | ✅ PASS | 工具 schema/签名不含 `tenant_id/user_id/role`；模型传入身份字段被忽略（`test_business_tool_schemas_contract_and_no_identity_fields`、`test_crewai_tool_ignores_model_supplied_identity_fields`）；身份经服务端 ctx 闭包注入 |
| 工具契约 | ✅ PASS | 6 个业务工具均有 input/output schema；缺 `order_id` 拒绝且不创建审批（`test_crewai_tools_reject_missing_order_id`） |
| fail-closed 转人工 | ✅ PASS | 委派异常/端点不在白名单/模型不在 `HIGH_CONFIDENCE_MODELS`/无自托管 LLM/写未回传待审批 → `handle_error`，保留 operation_id 与完整状态，不降级执行（`test_crewai_delegation_error_fails_closed_to_human`、`test_crewai_write_without_approval_id_in_response_fails_closed`） |
| crewai 禁用时主路径与既有确定性行为一致 | ✅ PASS | crewai_enabled 缺省 → `build_crewai_router` 从未被调用，走既有确定性节点（`test_crewai_disabled_uses_deterministic_path_and_has_no_direct_execute_edge`） |
| **真实 crewai 子智能体执行链**（`_run_real`+真实 Crew+真实 LLM） | 🔒 **BLOCKED** | 本环境未安装 `crewai`、无真实自托管 LLM 端点；真实 `_run_real` 链未验证（`RUN_CREWAI_INTEGRATION` 未置 1） |

### 3.1 安全审计发现（F1）与当前树状态
> `evidence/prod-go-live/security-auditor/CREWAI_WIRING_SECURITY_AUDIT.md`

- **红线 1~6 全部 PASS**：审批不绕过、租户边界不可绕过、完全自托管、模型白名单门控、数据面租户隔离与租户级幂等、fail-closed——全部满足《编码/业务多智能体宪法》。
- **一项 FAIL（F1，功能缺陷）**：真实（非 stub）CrewAI 写路径 `_run_real` 曾在返回顶层**未上抛** `approval_id/operation_id`，导致写意图恒走 `crewai_write_no_approval_in_response` → `handle_error`（fail-closed 转人工），**未能进入唯一 `human_approval`**；且已落库的 pending 审批/操作成为孤儿记录。**安全上 fail-closed 不构成绕过**（红线不受影响），但使 CrewAI 写链路**功能断裂**。
- **当前树已修复**：`src/tools/crewai_adapter.py` 新增 `_extract_write_meta(crew_result)`，`_run_real` 从 crew 输出串解析并**上抛 `status/operation_id/approval_id/refund_amount` 到顶层**；`builder.py` 对缺失 id 做**孤儿清理**（approval 标记 system/False 拒绝，operation 转 `HUMAN_HANDOFF`），维持"待审批记录与审批链路一致"。
- **⚠️ 待办/剩余**：① 真实链路的集成测试覆盖仍**以 stub 代替真实 `_run_real` 返回形状**，请在具备 crewai + 真实端点的环境补一条真实链路集成测试；② `EndpointGuard` 通配符白名单在受限环境可能放行公网端点（硬化待办）；③ `_write_capability_ok` 与 `_crewai_llm` 的模型取值来源建议统一。
- **红线确认**：F1 不是安全绕过、未暴露 tenant 身份、未新增 `direct→execute` 边、未放宽模型门控/端点白名单；**不影响**红线 1/2/3/4/5/6 的 PASS 判定，但**必须修复**才能让 CrewAI 写链路真正可用（当前树已修复，真实链路仍 BLOCKED）。

---

## 4. Docker 运行态实测结论

> **证据**：`evidence/prod-go-live/deploy-engineer/*RUNTIME*.md` + `runtime/runtime_verify_report.json` + `SANDBOX_GATEWAY_RUNTIME_VERIFY.md`。

- **沙箱网关 `sandbox-gateway` 已在真实 Docker 运行态拉起并全链路通过**：容器 `Up (healthy)`，healthcheck 通过；同内网 verify-runner 容器经 `http://sandbox-gateway:8010` 提交/查询全部 200；沙箱 SQLite 持久化到挂载卷（不丢状态）；网关只挂 `internal` 私有网、**不发布宿主端口**。**全部不触真实资金。**
- **执行层红线运行态实测（`runtime_verify_report.json` 全部 `true`）**：
  - **幂等**：同一 `tenant_id + operation_id/idempotency_key` 绝不重复执行、绝不跨租户覆盖（`same_tenant_same_key_external_txn_same/cross_tenant_same_key_external_txn_differ/concurrent_16_same_key_unique_txn/server_side_single_row*` 均 true；服务端 SQLite `UNIQUE(tenant_id,idempotency_key)` + `ON CONFLICT DO NOTHING`）。
  - **回调安全**：坏签名/重放/跨租户/篡改金额**绝不误确认或重复扣款**（`correct_hmac_confirmed/wrong_hmac_signature_invalid/replay_same_nonce_replay/terminal_locked_new_nonce/unknown_execution_not_found/cross_tenant_callback_not_found/tampered_amount_amount_mismatch` 均 true）。
  - **故障注入**：网关 5xx/503/超时/down → 收敛为明确的 `ProviderError.code`（`upstream_5xx/timeout/network`），fail-closed，不静默放行（4/4 PASS）。
  - **对账**：系统已执行/渠道未执行 → `MISMATCHED` + operation 转 `HUMAN_HANDOFF`；审计事件 + 关联键（`tenant_id/operation_id/external_operation_id/execution_status`）留痕；不静默当成功、不重复执行（全部 true）。
- **诚实边界（关键）**：
  - 上述为**沙箱网关（boundary-2 合成钻取 + SQLite 服务端）**运行态验证，**非真实资金/gateway live**；`EXECUTION_MODE=shadow`、`EXECUTION_PROVIDER=mock`/`sandbox_http` → **写操作仅模拟回执，不触真实资金/渠道**。
  - **生产栈 `docker compose config` 目前 FAIL（exit 1）**：`deploy/.env.production` 未注入 `GATEWAY_API_KEY` / `SANDBOX_GATEWAY_API_KEY` → 含 `sandbox-gateway` 的生产栈在 config 阶段 fail-closed（符合"缺配置即拒绝"）。这是**待部署项配置缺口**（BLOCKED-需注入网关密钥 + 切 `sandbox_http`），**非代码缺失**。
  - **目标服务器 = 外部资产，未接入**；阶段二（多节点 K8s/LB/PG 主备拓扑落地）= **0%（BLOCKED）**；阶段三（生产专栈数据面运行时复验）= 部署期执行项；阶段七（切 live）= **0%（多重 BLOCKED）**。
  - 生产栈容器级健康为 **T7 一次 bring-up 观测时点结果**（`PROD_STACK_HEALTH.md`），**非当前运行态证明**（未经本次复核）。

---

## 5. 预发布硬化评估（目标服务器）

> **证据**：`TARGET_SERVER_PRE_RELEASE_HARDENING.md` + `HARDENING_VERIFY_EVIDENCE.md` + `INFRA_READINESS_GAP.md`。

**本机可执行核验项（✅）**：网络暴露面——仅 `nginx` 发布宿主端口（prod `8080/8843`），`postgres/redis/migrate/api/worker/frontend/backup/sandbox-gateway` 均 `internal-only`（数据面不发布端口）；`check_secrets.sh` fail-closed 门控存在；生产 compose 密钥缺失计数可量化。

**BLOCKED-需外部（4/5 轴）**：
| 轴 | 状态 | 说明 |
|---|---|---|
| 受信 CA / 生产域名 | ❌ BLOCKED | 证书自签（`CN=preview.local`，subject==issuer），非受信 CA（`certutil` dump 复核） |
| 生产网关密钥 | ❌ BLOCKED | `deploy/.env.production` 缺 `GATEWAY_API_KEY`/`SANDBOX_GATEWAY_API_KEY` → prod compose `config` FAIL |
| 生产观测密钥 + trace | ❌ BLOCKED | observability compose（production env）`config` FAIL（缺 9 项必需密钥）；应用侧 Langfuse pub/secret 为空 → trace NO-OP |
| 业务方书面确认真实租户 | ❌ BLOCKED | `tenants.json` 为示例（ACME-RETAIL/GLOBEX-ECOM，明示非真实）；`LAUNCH_ALLOWED_TENANTS=__NONE_APPROVED_YET__`、`AUTH_LOGIN_CREDENTIALS={}` |
| 主机防火墙/网络出口策略 | ⚠️ 部署期 | 成文清单；目标服务器主机防火墙=部署期执行项（BLOCKED-需外部服务器） |

---

## 6. 诚实 BLOCKED 清单（放量门控 · 外部输入未到位）

| 项 | 状态 | 所需真实输入 | 谁提供 |
|---|---|---|---|
| 真实自托管 LLM 权重端点 + 写操作评测 | 🔒 BLOCKED | 自托管端点 + `write_op_pass=true` 评测报告 | 部署/评测侧 |
| 真实租户书面确认（首批） | 🔒 BLOCKED | 业务方签署确认函 + 白名单/凭据/成员角色 | 业务/发布侧 |
| 受信 CA / 生产域名 | 🔒 BLOCKED | 真实域名 + 受信 CA fullchain + key | 部署/运维侧 |
| 目标服务器（多节点/HA 拓扑落地） | 🔒 BLOCKED | 外部资产接入 + 具备 Docker engine 的部署环境 | 运维侧 |
| 7 天 shadow 观察 | 🔒 BLOCKED | S1 shadow 稳定运行 7 天观测记录 | 发布侧 |
| 真实资金 live / 渠道 | 🔒 BLOCKED | `EXECUTION_MODE=live` + 真实 providers/回调验签/对账 | 资金/执行侧 |
| 生产网关沙箱部署 + 接线 | 🔒 BLOCKED | 注入 `GATEWAY_API_KEY`/`SANDBOX_GATEWAY_API_KEY` + 切 `sandbox_http` | 部署侧 |
| API 认证层"服务端认证→注入 TenantContext"证据 | 🔒 属范围外 | 认证/成员/角色校验证据 | 安全/API 侧 |
| 生产观测栈密钥 + Langfuse trace | 🔒 BLOCKED | 注入生产观测密钥 + 开放 trace | 部署/运维侧 |

**以上项本报告一律不作可用/达标/放量断言。**

---

## 7. 放量红线核验（阶段二~三已证 · 现为零且不可触发）

| 红线 | 判定 | 证据 |
|---|---|---|
| 未审批即执行 | ✅ 0 & 不可触发 | 唯一 `human_approval` 中断 + `interrupt(payload)` + 恢复参数严格校验；图无 `direct→execute` 边 |
| 同操作重复执行 | ✅ 0 & 不可触发 | 幂等键含 `tenant_id` + `UNIQUE(tenant_id,idempotency_key)` + `ON CONFLICT DO NOTHING`；运行态 `concurrent_16_same_key_unique_txn=true` |
| 跨租户成功 | ✅ 0 & 不可触发 | RLS FORCE/NOBYPASSRLS + 订单归属校验；运行态 `cross_tenant_callback_not_found=true` |
| 对账差异 | ✅ 0 & 不可触发 | 对账 `MISMATCHED` + `HUMAN_HANDOFF`，不静默当成功；审计+关联键留痕 |
| 回调验签失效 | ✅ 0 & 不可触发 | 坏 HMAC → invalid；运行态 `wrong_hmac_signature_invalid=true` |
| PG 恢复失败 | ✅ 0 & 不可触发 | 全新库 clean migrate exit 0；DR 加密恢复 RTO/RPO 上界（`deploy/drills`）；`test_recovery_consistency` |

---

## 8. 结论

1. **工程与证据已在分支落地并验证**（阶段0~四）：RC4-candidate 基线冻结（396/34）、CrewAI 主链路接入与契约/安全行为正确（10 用例 PASS）、Docker 沙箱网关运行态幂等/回调/故障/对账全部 PASS、预发布硬化评估与跨切面安全审计（红色红线 1~6 PASS）。
2. **权威测试数字（唯一）**：**rc5-candidate 综合 = 411 passed / 0 failed / 34 skipped / EXIT=0**（37.19s）；阶段一 rc4-candidate = 396/34（历史，已被取代）。均已绑定对应 commit、skipped 原因可追溯。
3. **正式放量 = `NO-GO`**：真实资金/gateway live、真实租户书面确认、真实自托管模型端点、受信 CA/域名、目标服务器、7 天 shadow 均未就绪（BLOCKED-需外部，§6）。
4. **系统处于 S1 shadow + fail-closed 安全默认态**：`EXECUTION_MODE=shadow`、写操作仅模拟回执、`LAUNCH_GATE_STRICT=true`；放量红线为零且不可触发（§7）。
5. **已知未闭合项（诚实）**：真实 crewai 执行链 BLOCKED（无 crewai/真实端点）；安全保障审计 F1（真实写路径 `_run_real` 上抛 id）**当前树已修复**，但真实链路集成测试覆盖仍以 stub 代替；生产 compose 因缺网关/观测密钥 `config` FAIL（配置缺口）；目标服务器硬化 4/5 轴 BLOCKED-需外部。

> **最终判定：阶段0~四的工程与证据已就绪并在分支验证；正式生产放量仍为 NO-GO（真实资金/租户未就绪）。** 待 §6 外部输入到位 + 生产栈运行时 RLS/告警复验 + `git archive` clean-context 字节级重建后，可转**有条件 GO**。

*release-manager · t6 最终收口 · 与 `GO_NO_GO.md` / `FINAL_ACCEPTANCE.md` / `TEST_EVIDENCE_RC5.md` / `RC4_PROD_READINESS_MASTER_REPORT.md` 配套。*
