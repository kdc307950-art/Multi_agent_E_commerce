# 放量审批单（正式交付 · 基于 t1/t5/t6 真实结果回填）

> t4 模板回填为正式交付物。放量前条件逐项基于 **OPS_RUNBOOK §2 上线阈值**，用 **t1/t5/t6 真实结果**核对。
> **审批角色**：仅当前租户内 `admin`/`approver`；**同租户审批**，跨租户一律拒绝并留痕；`platform_admin` 不作租户审批角色。
> 生成时间戳：`20260904-030903`。

---

## 0. 审批单标识
- 审批单 ID：`SCALEUP-20260904-030903`
- 目标阶梯：`Tier 0 → Tier 1`（首批 `TENANT-A` → `TENANT-A,B`）
- 申请人：`deploy-engineer`（部署）/ `drill-runner`（演练）；审批人：`TENANT-A admin/approver`；复核人：`security-auditor`
- 审批环境：`preview`（`after-sales-preview`），git **HEAD=`7941246`**（FOUND-SOFTWARE-1 修复已 commit；`251f430` 为其前一提交=缺陷版）

## 1. 放量前条件核对（**所有项须实测/可复核**）
| # | 条件 | 是否满足 | 证据/记录 | 来源 |
|---|------|---------|----------|------|
| 1 | 部署健康：4 项全过；仅 80/443；无 CORS | ✅（t1/t8：4 项全过、无 CORS；**宿主本项目监听=仅 {80,443}**（t8 干净快照；**注明 3100 为无关遗留宿主进程**）） | `PRODUCTION_DEPLOYMENT_record-20260904-030903.md` | t1/t8 |
| 2 | RPO ≤ 15min，RTO ≤ 60min | ✅ **RTO=1.668s 实测达标**；**RPO=900s 达标**——t12 已加 **15min 周期 pg_dump 备份**（backup 服务每 900s，保留 8，宿主持久 `./data/preview-backups`），校验 **marker_in_restore=0、TENANT_A_in_restore=1、restore_RTO=1.9s** → RPO 上界=15min 周期界定 | `DR-20260904030211-pg-backup-restore.md` + t12（`data/preview-backups/langgraph-20260903193257.dump`） | t6/t12 |
| 3 | 重启韧性：API/worker/Redis 重启后读接口恢复；Redis 重启不影响 PG | ✅ PASS（pass=3） | `DR-20260904-0302-api-worker-redis-restart.md` | t6 |
| 4 | 审批幂等：同审批重复决策收敛单 `operation_id`，不重复执行 | ✅ PASS（真实数据面；`reapprove_same_op=true`） | `sec_dynamic_acceptance-20260904-030405.md` | t5 |
| 5 | SSE 恢复：断线 resume 只重放不重执行 | ✅ PASS（t9 修复后 t10：resume 含 done，无重复执行） | `drill-api-sse.json` | t10 |
| 6 | 并发幂等：同 `client_request_id` 单一 stream/操作 | ✅ PASS（t9 修复后 t10：stream_id_1==stream_id_2）+ 数据面 UNIQUE(tenant_id,idempotency_key) | `drill-api-concurrent.json` | t10 |
| 7 | 对账：非终态收口；不一致→转人工 | ✅ 入口可达 200 + 审计存在（t10）；**mismatch 计数由后台 reconcile_tenant 未实测**（与 t5 交叉） | `drill-api-reconcile.json` | t10 |
| 8 | 审计四维追溯（tenant/session/approval/operation） | ✅ PASS（approvals 含四维字段；`/api/audit` 可定位） | `sec_dynamic_acceptance-20260904-030405.md` | t5/t6 |
| 9 | 写操作模型：仅 `HIGH_CONFIDENCE_MODELS` 内可写 | ✅ PASS（`self-hosted-demo` write_op_pass=true，交集非空；非白名单拒写转人工） | `sec_dynamic_acceptance.md` §三 | t5 |
| 10 | 完全自托管：LLM 白名单；Graphiti 遥测关；无未经批准出网 | ⚠️ **部分**（自托管栈 ✓、langfuse `TELEMETRY_ENABLED=false` ✓；真实 LLM 端点链路未实测；GAP-02/03 风险待处置） | `sec_static_review.md` §一/§二 | t2 |
| 11 | 安全合规：无跨租户/无重复执行/无审批绕过/无未审计写 | ✅ PASS（t5 真实数据面 + **t9 修复后（HEAD=7941246）无该缺陷**，E2E：审批→executed、无重复执行） | `sec_dynamic_acceptance.md` §一/§四 + `SOFTWARE_FIX_FOUND_1.md` | t5/t9 |

- **阈值达成结论**：**RPO 已达标**（#2：15min 周期备份界定），#1/#3/#4/#5/#6/#8/#9/#11 已达成；**仍部分未达成**——#10（部分，真实 LLM 链路未实测）、#7（mismatch 待后台）、以及放量前需关注的**未实测项**（多租户并发/检查点隔离压力、应用侧 Langfuse trace 真实上报、告警指标增强）。
- **可放量前提（引用 7941246 修复后版本）**：FOUND-SOFTWARE-1 已修复（HEAD=7941246）+ 修复后审批/SSE/并发/对账（#4/#5/#6/#7）全部 PASS + **RPO≤15min（15min 周期备份界定）+ RTO 达标** + 宿主仅 {80,443}（t8）作为放量条件；**未实测项（对账 mismatch/多租户并发/应用侧 trace/告警指标）作放量前补强或观察项。**
- **演练记录可复核性**：**可复核**（`deploy/drills/records/20260904030652-drill-summary.json/.md`(7/7 PASS) + `drill-api-{approval,sse,concurrent,reconcile}.json` + 各 `DR-*.md` + t12 `data/preview-backups/`）。

## 2. 目标租户范围（本次申请放量）
- 本次放量：`TENANT-A`（Tier 0）→ 拟扩 `TENANT-A,TENANT-B`（Tier 1）
- 放量后白名单（`LAUNCH_ALLOWED_TENANTS`）：`TENANT-A,TENANT-B`
- 放量步进 / 观察期：`1 → 2 租户`；建议观察期 ≥ 1 个稳定窗口（未定义）

## 3. 风险评估
| 风险项 | 等级 | 说明 | 缓解/预案 | 来源 |
|--------|------|------|-----------|------|
| 跨租户访问 | 低 | RLS FORCE + NOBYPASSRLS；跨租户 404 | 数据面隔离 + 同租户审批 | t2/t5 |
| 重复资金副作用（同 operation_id 执行两次） | 低 | UNIQUE(tenant_id,operation_id) + 幂等锚点 + ON CONFLICT | 幂等 + 审计；t5 实测 0 重复 | t2/t5 |
| 敏感写绕过人工审批 | 低 | 唯一 `human_approval`，无 direct；execute 复核 | `launch_require_approval=true` + 门控 | t2 |
| **执行路径崩溃（FOUND-SOFTWARE-1）** | **已解除** | 审批通过后 execute 收尾/SSE 重放/`/api/audit` 崩溃 → 误标 FAILED→转人工 | **已修复（HEAD=`7941246`，`_as_json` 5 处 + 重建 api/worker + E2E 通过）** | t5 §4 / t9 / `SOFTWARE_FIX_FOUND_1.md` |
| 模型档位/能力矩阵不匹配 | 中 | GAP-01：`LLM_MODEL` vs `HIGH_CONFIDENCE_MODELS` 名需一致 | 部署注入一致且 `write_op_pass=true` | t2 GAP-01 |
| 出网泄漏（GAP-02/03） | 中-高 | LLM_ALLOWED_HOSTS 通配符放行一切；internal 网未 internal:true | restricted 拒绝通配符 + 设 internal:true 并挂 LLM 网关 | t2 GAP-02/03 |
| RPO/RTO 超基线 | 中 | RPO 上界=15min 周期备份界定（达标）；RTO 1.668s 达标 | 超基线→回滚；PITR/WAL 为更紧 RPO 的后续增强 | t6/t12 |
| 实际资金/外部网关（live） | 不适用 | `EXECUTION_MODE=shadow`（沙箱），不上真实资金 | 仅 mock provider；live 需另验收 | t1/t5 |

## 4. 审批意见（同租户 `admin`/`approver` 填写）
- 审批人（tenant/user/role）：`<TENANT-A admin 或 approver，需实名>` —— **须属于目标租户**；`platform_admin` 不适用。
- 审批意见：`<有条件放量（仅 Tier 0）或暂缓>` —— **RPO 已达标（t12 15min 周期备份界定，marker=0、TENANT-A 保留、restore_RTO=1.9s）**，RTO 达标、宿主仅 {80,443} 达成（t8）、langfuse 观测可用（t8）、D3-D6 全部 PASS、FOUND-SOFTWARE-1 已修复（HEAD=7941246）、四类安全标准达标。**仍未实测项**：对账 mismatch 计数（后台）、多租户并发/检查点隔离压力、应用侧 Langfuse trace 真实上报、告警指标增强（错误率/审批/对账/人工介入/RPO-RTO/安全拒绝无法计量）、真实 LLM（openai_compatible）链路。
- 二次确认：`<是/否>`（OPS_RUNBOOK §3 要求）；审批记录：`approval_id=<...>; operation_id=<...>`（四维可追溯）
- 结论：`<暂缓放量（附待办清单：对账 mismatch、多租户并发、应用侧 trace、告警指标增强、真实 LLM）；若队长接受将上述未实测项列为放量后观察项，可由 Tier 0（TENANT-A）开始，需同租户 admin/approver 二次确认并留痕>`
- 审批签名 / 时间：`<...>`

## 5. 结论（如实，未实测不宣称达标）
- 放量前条件是否全部达成且可复核：**核心达成**（RPO 周期界定、RTO、仅 80/443、安全四项、D3-D6、FOUND-SOFTWARE-1 修复、观察链路）；**部分未实测**（对账 mismatch、多租户并发、应用侧 trace、告警指标、真实 LLM）。
- 是否批准本次放量：**暂缓放量（附待办清单）**；若队长接受将未实测项列为放量后观察项，可从 **Tier 0（TENANT-A）** 开始，仍须同租户 `admin`/`approver` 二次确认并留痕。
- 放量前/后须满足：① GAP-02/03 处置（LLM_ALLOWED_HOSTS 无通配符 + internal 网收紧）；② 告警指标增强落地（错误率/审批/对账/人工介入/RPO-RTO/安全拒绝可计量）；③ 观察期复验 `healthcheck.sh` + 告警无异常 + RPO/RTO 复测。
- 签发：`<审批人> / <复核人 security-auditor>`

> **跨租户红线**：审批人须与目标租户同 `tenant_id`；跨租户审批一律拒绝并留痕（t5 实测跨租户审批→404）。
