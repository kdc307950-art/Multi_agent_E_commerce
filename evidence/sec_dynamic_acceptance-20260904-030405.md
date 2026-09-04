# 动态安全验收结论（t5 · security-auditor）

> 目标：栈启动后(t1)动态安全验收，证明「连续观察期**无跨租户 / 无重复执行 / 无审批绕过 / 无未审计写操作**」。
> 环境：真实 preview 栈 `after-sales-preview`（api/frontend/nginx/postgres/redis/worker，全部 healthy），
> 真实 Postgres 数据面（`app_runtime` 运行角色，`NOBYPASSRLS`），真实 JWT（`AUTH_BACKEND=real`），`EXECUTION_MODE=shadow`。
> 时间戳：`20260904-030405`。**结构化全文**：`evidence/sec_dynamic_acceptance-20260904-030405.json`。

---

## 一、验收结论（四项标准，真实数据面全部达成）

| 验收标准 | 结论 | 依据 |
|---|---|---|
| **无跨租户** | ✅ PASS | RLS FORCE（除 tenants 外所有业务/checkpoint 表 `ENABLE+FORCE`）+ `app_runtime NOBYPASSRLS` + 应用层 tenant_id 作用域；跨租户**审批/读取均 404**（实测）。RLS 行为：未设 `app.tenant_id` 时 operations 零可见；设 TENANT-A 作用域仅见 TENANT-A。 |
| **无重复执行** | ✅ PASS | `executions UNIQUE(tenant_id,operation_id)` + 幂等锚点 + `ON CONFLICT DO NOTHING`；本次退款 operation 仅产生 **1 条** execution（shadow confirmed）；`GROUP BY operation_id HAVING count(*)>1` 返回 **0 行**；重复审批收敛到**同一 operation_id**。 |
| **无审批绕过** | ✅ PASS | 审批前 op=`pending`（未执行）；仅 `approved` 才进 `execute_*`；execute 复核存在/动作/thread/审批/白名单；非审批角色 customer 审批 → **403**；缺二次确认 → **422**；跨租户审批 → **404**。 |
| **无未审计写操作** | ✅ PASS | 审批通过（`approval.decide`）、执行创建（`execution.create`）均 `append_audit`；跨租户/越权/拒绝路径全部 `security.deny.*` 留痕；按 `target_type=approval&target_id=` 可定位（2 条）。 |

## 二、真实数据面完整审批流程结果（补丁后）

在 api 容器内用**真实 PostgresStore + real JWT + TestClient** 走完整「会话→退款→审批」：

- `session_status=201`；`chat_status=200` 生成 `approval_id`/`operation_id`
- `op_before_status=**pending**`（审批前未执行）
- `cross_tenant_approval_status=**404**`（跨租户审批拒）
- `non_approver_decision_status=**403**`（customer 越权拒）
- `no_confirmation_status=**422**`（缺二次确认拒）
- `approve_status=200` 且 `approve_returned_same_op=true`
- `op_after_status=**executed**`（审批通过后执行）
- `reapprove_status=200` 且 `reapprove_same_op=**true**`（重复审批幂等）
- `cross_tenant_read_approval=404` / `cross_tenant_read_operation=404`（跨租户读取零可见）
- `replay_same_cid={status:200, has_approval:true}`（同 client_request_id 重放安全）

## 三、其他检查项

- **verify_launch_gate --strict（等效）**：`launch_allowlist=[TENANT-A,TENANT-B]`，`verify_launch_gate ok=True`，`tenant_allowed(TENANT-A)=True`，`tenant_allowed(EVIL-TENANT)=False`（非名单租户拒，受限环境强制）。
- **fail-closed 认证守卫**：preview/production + mock → RuntimeError；real 缺 secret → RuntimeError；real 提供 secret → 允许；development+mock → 允许。**6/6 PASS**。
- **能力矩阵交集**：`HIGH_CONFIDENCE_MODELS=LLM_MODEL=self-hosted-demo`，评测报告 `write_op_pass=true`，`resolve_high_confidence_models=['self-hosted-demo']`（非空）；`capability_ok('self-hosted-demo')=True`、`capability_ok(非白名单)=False`。非白名单模型写被拒并转人工。

## 四、⚠ 发现的高严重度真实缺陷（需在仓库修复）

**FOUND-SOFTWARE-1**（高）：`src/infrastructure/postgres_store.py` 对 psycopg3 已自动解析为 dict 的 **JSONB 列重复调用 `json.loads`**。

- 位置：`_row_to_operation`(L298, `result`)、`_row_to_execution`(L549/551, `receipt`/`compensation_result`)、`events_after`(L612, `data`)、`list_audit`(L636, `detail`)。
- 现象：真实数据面上 ① 审批通过后 `execute_refund` 收尾 `update_operation` 触发 `_row_to_operation` 崩溃，把**本应成功的执行错误标记为 FAILED→转人工**；② SSE 重放 `events_after` 崩溃；③ `/api/audit` 查询崩溃。
- 影响：资金/执行路径的**阻断性缺陷**（审批通过却被误判失败）。
- 修复建议：`json.loads(x) if isinstance(x,(str,bytes)) else x`，或驱动返回 str。
- **临时处置**：本次仅在**容器运行时**（`/app/src/.../postgres_store.py`）打补丁(7 处)以走通完整验收，**未改仓库源码**；容器重启即还原，需在仓库正式修复。

## 五、编排层瞬时问题（处理记录）

**issue-ENV-1**（中，编排层瞬时）：验收开始时侦测到 postgres/redis healthy 但 api/frontend/worker/nginx 停摆，一次性 migrate `Exited(1)`（`failed to resolve host postgres`）。根因是 postgres 容器网络 DNS 别名缺失 `postgres`（DNSNames 仅容器名/短 ID），导致依赖方 `postgres:5432` 连接失败；另有一个孤儿 `after-sales-preview-worker-1` 名冲突。处置：`docker compose down`（保留 `data/preview-postgres` bind 数据）→ `docker compose up -d` 重建，postgres 别名恢复含 `postgres`，全栈重新 healthy，数据未丢。非安全缺陷。

## 六、如实标注「未实测」

| 项 | 状态 |
|---|---|
| 真实 LLM 端点（`openai_compatible`）链路 + EndpointGuard 实机放行 | **未实测**（当前 `LLM_BACKEND=mock`） |
| 真实资金/外部回调网关（live provider）实机链路 | **未实测**（`EXECUTION_MODE=shadow`；回调验签/重放已在内存 pytest 21/21 覆盖） |
| 经 nginx（宿主 80/443）外部入口的端到端 HTTPS 流程 | **未实测**（本验收用 TestClient 直连 app；nginx 仅 80/443 已由 compose ps 证实） |
| 多租户并发写入/检查点隔离压力 | **未实测** |

## 七、基线 pytest（业务逻辑层，宿主 venv）

- `tests/test_approval_idempotency.py`：**16/16 PASS**（退款必经审批、重复决策幂等、拒绝不执行、跨租户 operation_id 唯一、并发 CAS 单抢占、超时转人工、绑定校验、低档模型拒绝、resume 不重复执行等）。
- `tests/test_execution_engine.py`：**21/21 PASS**（shadow 幂等、归属校验、live 回调重放、坏签名、金额不匹配转人工、补偿、对账、跨租户回调 404、provider 仅调一次、超时未确认转人工）。

> 注：上述 pytest 默认使用 `MemoryStore`（tests/conftest.py `client`/`store` fixture），验证的是**业务逻辑层**的幂等/无绕过/归属，不接触真实 Postgres/RLS。真实数据面隔离与幂等由本报告 §1/§2 的实际容器验证补充。

---

---

## 附录 · FOUND-SOFTWARE-1 修复状态更新（t9）

t5 发现的**高严重度真实缺陷 FOUND-SOFTWARE-1**（`src/infrastructure/postgres_store.py` 对 psycopg3 已自动解析为 dict 的 JSONB 列重复 `json.loads`）已在 **t9 修复**：
- 仓库源码：新增 `_as_json` 防御式解析助手，5 处 JSONB 读取（`operations.result` / `executions.receipt` / `executions.compensation_result` / `stream_events.data` / `audit.detail`）统一切换。
- 回归验证：内存 pytest（approvals+execution）**37/37 PASS**；容器内真实 Postgres 端到端（审批→execute 收尾）`op_after=**executed**`（不再误标 FAILED）、SSE 重放与 `/api/audit` 正常。
- 重建生效：`build api worker` + `up -d --force-recreate api worker`，api/worker 恢复 healthy；容器内 `grep _as_json` 确认已含仓库修复版（无运行时补丁残留）。

详见 `evidence/SOFTWARE_FIX_FOUND_1.md`。

*产出：`evidence/sec_dynamic_acceptance-20260904-030405.json`、本 md、`evidence/SOFTWARE_FIX_FOUND_1.md`。t5/t9 security-auditor。*
