# 交付总览（DEPLOYMENT_SUMMARY · 【草稿 · 待最终化】）

> **状态：草稿（intermediate draft），非最终定稿。** 当前已纳入 **t1/t2/t3/t5/t6/t9/t10 的真实已定结果**；
> 仍依赖 **t8（in_progress：langfuse 修复 + 宿主仅 80/443 干净快照 + 15min 周期备份使 RPO 可实测）** 的字段
> 一律标注为 `<待 t8 回填>` 并附「由队长协调完成后二次确认」。
> 完成标准核对表中，凡涉 t8 的验收项在 t8 落地前标 **「进行中 / 待确认」**，**不宣称达标**。
> 本文件为中间产物，最终定稿将在 t8 落地、队长发出「可最终化」信号后由 t11 刷新。
> 生成时间戳：`20260904-030903`（草稿）。环境：`preview`（after-sales-preview），git **HEAD=`7941246`**（FOUND-SOFTWARE-1 修复已 commit；`251f430` 为其前一提交=缺陷版）。

---

## 一、交付物路径清单

| # | 交付物 | 路径 | 状态 |
|---|--------|------|------|
| 1 | 生产部署记录 | `deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-030903.md` | 已回填（t1）；**t8 相关字段待补** |
| 2 | 灰度租户清单 | `deploy/records/CANARY_TENANTS-20260904-030903.md` | 已回填（t1/t2/t5）；D3-D6 结论待 t10 更新 |
| 3 | 备份恢复记录 | `deploy/drills/records/DR-20260904030211-pg-backup-restore.md`（+`drill-pg-backup-restore.json`） | 已回填（t6）；**RPO≤15min 待 t8** |
| 4 | 回滚记录 | `deploy/drills/records/DR-20260904030354-rollback.md`（+`rollback-record.json`） | 已回填（t6） |
| 5 | 运营告警规则 | `deploy/observability/alert-rules.yml`（权威）+ `alert-rules/README.md` + `prometheus.yml`(rule_files) + compose 挂载 | 已生成；**指标增强后生效** |
| 6 | 放量审批单 | `deploy/records/SCALEUP_APPROVAL-20260904-030903.md` | 已生成；**结论待 t8 + 最终化** |
| 7 | 指标汇总表 | `deploy/records/METRICS_SUMMARY-20260904-030903.md` | 已更新（含 t9/t10） |
| 8 | 本交付总览（草稿） | `evidence/DEPLOYMENT_SUMMARY-draft.md` | 本文件（草稿） |
| 9 | 静态安全评审 | `evidence/sec_static_review.json/.md`（t2） | ✅ |
| 10 | 动态安全验收 | `evidence/sec_dynamic_acceptance-20260904-030405.json/.md`（t5，已补 t9 附录） | ✅ |
| 11 | FOUND-SOFTWARE-1 修复 | `evidence/SOFTWARE_FIX_FOUND_1.md`（t9） | ✅（**已 commit**，HEAD=`7941246`） |
| 12 | DR 演练记录 | `deploy/drills/records/`：`DR-*`、`drill-api-{approval,sse,concurrent,reconcile}.json`、`20260904030652-drill-summary.json/.md` | ✅（7/7 PASS） |

---

## 二、完成标准达成情况（逐项核对 · t8 相关标「进行中/待确认」）

| 完成标准 | 要求 | 当前状态 | 依据 | 是否达成 |
|---------|------|---------|------|---------|
| **RTO ≤ 60min** | 数据恢复 ≤60min | ✅ **实测达标**：`rto_restore_seconds=1.668s`（Stopwatch） | `drill-pg-backup-restore.json` | ✅ 已达成 |
| **RPO ≤ 15min** | 备份恢复点 ≤15min | ⚠️ **进行中/待确认**：当前 `rpo=900s` 为**基线**（`baseline_15min`），**未实测**；需 t8 落地 15min 周期备份后实测 | `drill-pg-backup-restore.json`（`measured=false`） | ⚠️ **待 t8 回填** |
| **仅 80/443** | 宿主仅开放 80/443 | ⚠️ **进行中/待确认**：preview 拓扑层 ✓（nginx 仅 80/443）；**宿主运行时**仍有本地 dev 栈（5432/6379/8000/3000）+ 遗留进程监听 → **t8 在途** | t1（compose PortBindings）/ t8 | ⚠️ **待 t8 回填** |
| **无跨租户** | 连续观察期无跨租户访问 | ✅ 实测（RLS FORCE + NOBYPASSRLS；跨租户 404），t9 源码修复+重建后复核 | t5（+t9 附录） | ✅ 已达成 |
| **无重复执行** | 同一资金操作不重复执行 | ✅ 实测（幂等锚点；`GROUP BY op HAVING count>1=0`；重复审批收敛同一 op） | t5/t9（`executions` 落库核实） | ✅ 已达成 |
| **无审批绕过** | 敏感写必经唯一 human_approval，无 direct 绕过 | ✅ 实测（pending→approved→executed；越权 403；缺二次确认 422；跨租户 404） | t5/t9 | ✅ 已达成 |
| **无未审计写操作** | 所有写操作留痕 | ✅ 实测（decision/execution 均 append_audit；拒绝路径 security.deny.*） | t5/t9 | ✅ 已达成 |
| **D3 审批门控（HITL）** | 审批前 pending→审批后 executed | ✅ 实测（t9 修复后）：`approval` → `executed`；approve_latency=0.145s | `drill-api-approval.json`（t10） | ✅ 已达成 |
| **D4 SSE 断线恢复** | resume 只重放不重执行 | ✅ 实测（t9 修复后）：resume 含 done，无重复执行 | `drill-api-sse.json`（t10） | ✅ 已达成 |
| **D5 并发重复提交** | 同 cid 单 stream | ✅ 实测（t9 修复后）：stream_id_1==stream_id_2 | `drill-api-concurrent.json`（t10） | ✅ 已达成 |
| **D6 沙箱对账** | 对账入口可达/审计存在 | ✅ 部分（入口可达 200 + 审计存在）；**mismatch 计数未实测**（后台 reconcile_tenant 分支，与 t5 交叉） | `drill-api-reconcile.json`（t10） | ⚠️ mismatch 待后台任务 |

---

## 三、指标（实测/基线 / 待回填）

| 指标 | 值 | 实测/基线 | 来源 | 备注 |
|------|----|----------|------|------|
| **RPO** | 900s（=15min） | **基线**（`baseline_15min`，未实测） | `drill-pg-backup-restore.json` | 一次性演练备份；需 t8 周期 cron 备份实测 |
| **RTO** | 1.668s | **实测**（Stopwatch） | `drill-pg-backup-restore.json` | ≤3600s 达标 |
| **错误率** | 0.0（0/7） | **实测（本演练）** | `20260904030652-drill-summary.json/.md` | 7 项全 PASS，0 失败 |
| **错误率（生产 API）** | 无值 | **无法计量** | `metrics.py` / `routes.py` | `api_requests_total` 无 `status` 标签 |
| **人工介入率** | 0.0（0/7） | **实测（本演练）** | `drill-summary` | D3 审批为设计 HITL 控制（approver 批复），非失败升级 |
| **审批耗时** | **0.145s** | **实测**（`approvals.created_at→decided_at`） | `drill-api-approval.json`（t10） | 达标（无阈值定义） |
| **对账 mismatch 计数** | 未实测 | **待后台 reconcile_tenant** | `drill-api-reconcile.json`（t10） | D6 仅校验入口+审计 |
| **无跨租户/无重复/无绕过/无未审计写** | PASS | **实测**（t5 + t9 源码修复重建后） | `sec_dynamic_acceptance.md` | ✅ |

---

## 四、t8 待落地项（落地后由 t11 回填并最终化）

| 项 | 当前 | 落地后处理 |
|---|------|-----------|
| **宿主仅 80/443** | dev 栈 + 遗留进程仍监听 5432/6379/8000/3000 | t8 停 dev 栈/停遗留进程 + `ss`/`netstat` 复核仅 {80,443} → 回填 `PRODUCTION_DEPLOYMENT_record` §6 |
| **观测栈 langfuse** | 容器退出（`CLICKHOUSE_URL`）→ langfuse 非健康，trace 未生效 | t8 加 ClickHouse+v3 migrate 或降 v2 → langfuse health 200 → 回填观测说明 |
| **RPO 实测** | 一次性演练备份，`rpo=900s` 基线未实测 | t8 加 15min 周期备份（cron）+ 记录上次备份时间 → 实测 RPO → 回填备份恢复记录/指标表 |

> 以上三项均附「由队长协调完成后二次确认」。

---

## 五、已定关键结论（t9/t10 落地后）

1. **FOUND-SOFTWARE-1**：真实缺陷（前一提交 `251f430` 为缺陷版），**t9 已修复并 commit（HEAD=`7941246`）**——`src/infrastructure/postgres_store.py` 新增 `_as_json` 防御式解析（5 处 JSONB 读取切换，`+20/-5`），未夹带 migrations.py。内存 pytest 37/37、容器内真实 Postgres E2E 通过（审批→`executed`、SSE 重放、`/api/audit` 正常、无重复执行），并**重建 api/worker + 重启生效**（运行栈即 `7941246`）。**注：`src/infrastructure/migrations.py` 为先前会话改动，仍未提交**（需与本次修复区分）。
2. **D3-D6**：**t10 已复跑全部 PASS**（基于 t9 修复后源码 + 重建，真实 JWT + mock LLM，非运行时临时补丁）。纠正 t6 中「D3-D6 BLOCKED」的旧结论——该 BLOCKED 系因未使用 `deploy/secrets/preview_login_credentials.txt` 明文凭据 + 未驱动 mock LLM 所致，属**误判**。
3. **RTO / 无跨租户 / 无重复执行 / 无审批绕过 / 无未审计写**：**实测达成**（t5 + t9 复核）。
4. **指标缺口（告警生效前提）**：`api_requests_total` 无 `status`；无 `approval_decision_latency_*`/`reconcile_mismatch_total`/`human_intervention_total`/`drill_{rpo,rto}_seconds`/`security_denials_total`。→ 需在 `metrics.py`+`routes.py` 补**有界标签**指标，`alert-rules.yml` 相关告警才会真正生效（当前表达式为空、不误报）。

---

## 六、放量建议（与 SCALEUP_APPROVAL 一致 · 待 t8 + 最终化）
- **当前不建议放量（继续等待）**。剩余阻塞：① **RPO≤15min 未实测**（待 t8）；② **宿主仅 80/443 未达成**（待 t8）；③ **观测栈 langfuse 未生效 + 告警指标未增强**（错误率/审批/对账/人工介入/RPO-RTO/安全拒绝无法计量）。D3-D6 已 PASS、FOUND-SOFTWARE-1 已修复、RTO/四类安全标准已达成。
- 满足后可依次进入 **Tier 0（TENANT-A）→ Tier 1（+TENANT-B）**，需同租户 `admin`/`approver` 二次确认审批并留痕。

---

*交付：deploy-prod-ready 团队 · t7 report-compiler 生成的【草稿】，待 t8 落地 + 队长「可最终化」信号后由 t11 定稿。*
