# 交付总览（DEPLOYMENT_SUMMARY · 【终稿·RPO 达标】）

> **状态：终稿（t13 最终化）**，已纳入 **t1/t2/t3/t5/t6/t8/t9/t10/t12 全部已定结果**。
> **RPO 达标**：t12 已为 preview 栈加入**每 15min 的 pg_dump 周期备份**（sidecar `backup` 容器，cron 每 900s，
> `BACKUP_KEEP=8`，宿主持久 `./data/preview-backups`），并跑「备份→写标记→恢复临时库→校验」：
> **marker_in_restore=0、TENANT_A_in_restore=1、restore_RTO=1.9s**。故 **RPO 上界=真实 15min(900s) 备份周期界定**（非硬编码基线），满足 **RPO≤15min**；
> **RTO=1.668s≤60min 达标**。边界/后续增强：**pg_dump 周期备份，非 PITR/WAL 归档**（更紧 RPO 的后续项）。
> 环境：`preview`（`after-sales-preview`），git **HEAD=`7941246`**（FOUND-SOFTWARE-1 修复已 commit）。
> 生成时间戳：`20260904-030903`（t13 最终化）。

---

## 一、交付物路径清单

| # | 交付物 | 路径 | 状态 |
|---|--------|------|------|
| 1 | 生产部署记录 | `deploy/records/PRODUCTION_DEPLOYMENT_record-20260904-030903.md` | ✅ 已定稿（t1/t8/t9/t12） |
| 2 | 灰度租户清单 | `deploy/records/CANARY_TENANTS-20260904-030903.md` | ✅ 已定稿（t1/t2/t5） |
| 3 | 备份恢复记录 | `deploy/drills/records/DR-20260904030211-pg-backup-restore.md`（+`drill-pg-backup-restore.json`） | ✅ 已回填（t6/t7 + t12 RPO 周期界定） |
| 4 | 回滚记录 | `deploy/drills/records/DR-20260904030354-rollback.md`（+`rollback-record.json`） | ✅ 已回填（t6/t7） |
| 5 | 运营告警规则 | `deploy/observability/alert-rules.yml`（权威）+ `alert-rules/README.md` + `prometheus.yml`(rule_files) + compose 挂载 | ✅ 已生成；**指标增强后生效**（待办） |
| 6 | 放量审批单 | `deploy/records/SCALEUP_APPROVAL-20260904-030903.md` | ✅ 已定稿（结论：暂缓放量，附待办清单） |
| 7 | 指标汇总表 | `deploy/records/METRICS_SUMMARY-20260904-030903.md` | ✅ 已定稿（RPO=15min 周期界定） |
| 8 | 本交付总览（终稿） | `evidence/DEPLOYMENT_SUMMARY-20260904-030903.md` | ✅ 本文件（RPO 达标） |
| 9 | 静态安全评审 | `evidence/sec_static_review.json/.md`（t2） | ✅ |
| 10 | 动态安全验收 | `evidence/sec_dynamic_acceptance-20260904-030405.json/.md`（t5，已补 t9 附录） | ✅ |
| 11 | FOUND-SOFTWARE-1 修复 | `evidence/SOFTWARE_FIX_FOUND_1.md`（t9） | ✅（已 commit，HEAD=7941246） |
| 12 | DR 演练记录 | `deploy/drills/records/`：`DR-*`、`drill-api-{approval,sse,concurrent,reconcile}.json`、`20260904030652-drill-summary.json/.md` | ✅（7/7 PASS） |
| 13 | 观测栈 | `docker-compose.observability.yml`（langfuse v2 健康）+ `deploy/observability/*.yml` | ✅（t8：langfuse v2.95.11 健康，7 组件 healthy） |
| 14 | 周期备份 | `docker-compose.preview.yml` `backup` 服务 + `deploy/scripts/preview_backup_scheduler.sh` + `./data/preview-backups/` | ✅（t12：每 900s，保留 8，首份 `langgraph-20260903193257.dump`） |

---

## 二、完成标准达成情况（最终核对表 · 三档标注：✅ 实测 / 由周期界定 / ⚠ 未实测·基线）

| 完成标准 | 要求 | 状态 | 档位 | 依据 |
|---------|------|------|------|------|
| **RPO ≤ 15min** | 备份恢复点 ≤15min | ✅ **达标**：RPO 上界=**15min 周期备份**界定（实测 marker 排除=0、TENANT-A 保留、恢复 RTO=1.9s；首份 `langgraph-20260903193257.dump`） | **由周期界定** | t12（backup 服务每 900s + 恢复校验） |
| **RTO ≤ 60min** | 数据恢复 ≤60min | ✅ **达标**：`rto_restore_seconds=1.668s`（Stopwatch，D2 实测） | **实测** | `drill-pg-backup-restore.json` |
| **仅 80/443** | 宿主仅开放本项目业务端口 | ✅ **达标**：本项目宿主监听=nginx **{80,443}** | **实测** | t8；**注明**：3100 为**无关遗留宿主进程**（非本项目服务，观察期留意） |
| **无跨租户** | 连续观察期无跨租户访问 | ✅ **达标**：RLS FORCE + NOBYPASSRLS；跨租户 404 | **实测** | t5（+t9 复核） |
| **无重复执行** | 同一资金操作不重复执行 | ✅ **达标**：幂等锚点；`GROUP BY op HAVING count>1=0`；重复审批收敛同一 op | **实测** | t5/t9 |
| **无审批绕过** | 敏感写必经唯一 human_approval，无 direct 绕过 | ✅ **达标**：pending→approved→executed；越权 403；缺二次确认 422；跨租户 404 | **实测** | t5/t9 |
| **无未审计写操作** | 所有写操作留痕 | ✅ **达标**：decision/execution 均 append_audit；拒绝路径 security.deny.* | **实测** | t5/t9 |
| **D3 审批门控（HITL）** | 审批前 pending→审批后 executed | ✅ **达标**：approval→executed；approval latency=0.145s | **实测** | t10 |
| **D4 SSE 断线恢复** | resume 只重放不重执行 | ✅ **达标**：resume 含 done，无重复执行 | **实测** | t10 |
| **D5 并发重复提交** | 同 cid 单 stream | ✅ **达标**：stream_id_1==stream_id_2 | **实测** | t10 |
| **D6 沙箱对账** | 对账入口可达/审计存在 | ✅ 入口可达 200 + 审计存在；**mismatch 计数未实测** | ⚠️ 部分未实测 | t10（后台 reconcile_tenant 与 t5 交叉） |
| **观测链路可用** | langfuse/trace/指标/日志 | ✅ **达标**：langfuse v2.95.11 健康，7 组件 healthy | **实测** | t8 |
| **应用侧 Langfuse trace** | 应用上报 trace（metadata=租户/会话/环境） | ⚠️ **未实测**（应用侧 `LANGFUSE_*` 已配置；真实 trace 上报未在本栈验证） | **未实测** | t5 §六 |

> **未实测 / 待办清单**（不构成当前达标，留作放量前补强）：对账 mismatch 计数（后台）、多租户并发/检查点隔离压力、应用侧 Langfuse trace 真实上报、告警指标增强（错误率/审批失败率/审批耗时/对账/人工介入/RPO-RTO/安全拒绝对应指标与标签）、真实 LLM（openai_compatible）链路、真实外部网关（live）链路。

---

## 三、指标（实测 / 周期界定 / 未实测）

| 指标 | 值 | 档位 | 来源 | 备注 |
|------|----|------|------|------|
| **RPO** | 900s（=15min，**周期界定**） | **由周期界定** | t12（backup 每 900s + 恢复校验 marker_in_restore=0、TENANT_A_in_restore=1、restore_RTO=1.9s） | 上界=15min 周期备份；**PITR/WAL 归档为更紧 RPO 的后续增强** |
| **RTO** | 1.668s | **实测** | `drill-pg-backup-restore.json`（D2，Stopwatch）；另 t12 恢复校验 restore_RTO=1.9s | ≤3600s 达标 |
| **错误率** | 0.0（0/7） | **实测（本演练）** | `20260904030652-drill-summary.json/.md` | 7 项全 PASS，0 失败 |
| **错误率（生产 API）** | 无值 | **无法计量** | `metrics.py`/`routes.py` | `api_requests_total` 无 `status` 标签 |
| **人工介入率** | 0.0（0/7） | **实测（本演练）** | `drill-summary` | D3 审批为设计 HITL 控制，非失败升级 |
| **审批耗时** | **0.145s**（数据面）/ **0.204s**（API 往返） | **实测** | t10 | 达标（无阈值定义，放量后建议设 p95） |
| **对账 mismatch 计数** | 未实测 | **未实测（后台）** | `drill-api-reconcile.json` | D6 仅校验入口+审计 |
| **无跨租户/无重复/无绕过/无未审计写** | PASS | **实测** | t5（+t9 复核） | ✅ |
| **组件 down** | `up{job}` 类可告警 | **可立即生效** | `alert-rules.yml` | 组件存活告警 |
| **错误率/审批失败率/审批耗时/对账/人工介入/RPO-RTO/安全拒绝** | 表达式为空 | **待指标增强后生效** | `alert-rules.yml` | 需补有界标签指标 |

---

## 四、已定关键结论

1. **FOUND-SOFTWARE-1**：真实缺陷（前一提交 `251f430` 为缺陷版），**t9 已修复并 commit（HEAD=`7941246`）**——`src/infrastructure/postgres_store.py` 新增 `_as_json`（5 处 JSONB 读取切换，`+20/-5`，未夹带 migrations.py）；内存 pytest 37/37、容器内真实 Postgres E2E 通过（审批→`executed`、SSE 重放、`/api/audit` 正常、无重复执行），重建 api/worker 生效。**注：`src/infrastructure/migrations.py` 为先前会话改动仍未提交。**
2. **D3-D6**：**t10 已基于 t9 修复后源码复跑全部 PASS**（真实 JWT 明文凭据 + mock LLM，非运行时临时补丁）。纠正 t6 的「BLOCKED」误判。
3. **Langfuse**：t8 采用 A2（降级 v2.95.11）修复，`/api/public/health` OK，观测栈 7 组件 healthy；**应用侧 trace 真实上报未实测**。
4. **宿主仅 80/443**：t8 停 dev 栈 + 停遗留进程，干净快照；**3100 为无关遗留宿主进程**（注明）。
5. **RPO**：**t12 落地 15min 周期 pg_dump 备份（backup 服务每 900s，保留 8，宿主持久 `./data/preview-backups`），RPO 上界=15min 周期备份界定；校验 marker_in_restore=0、TENANT_A_in_restore=1、restore_RTO=1.9s → RPO≤15min 达标**。边界：**pg_dump 周期而非 PITR/WAL**（更紧 RPO 的后续增强项）。
6. **RTO / 无跨租户 / 无重复 / 无绕过 / 无未审计写 / D3-D6**：实测达标。
7. **指标增强缺口（告警前提，待办/后续）**：`api_requests_total` 无 `status`；无 `approval_decision_latency_*`/`reconcile_mismatch_total`/`human_intervention_total`/`drill_{rpo,rto}_seconds`/`security_denials_total`。→ 需在 `metrics.py`+`routes.py` 补**有界标签**指标，`alert-rules.yml` 相关告警才真正生效（当前表达式为空，不误报）。

---

## 五、放量建议（与 SCALEUP_APPROVAL 一致 · 附待办清单）
- **建议谨慎放量 / 仍暂缓**。**已达成的放量前置**：RPO≤15min（周期界定）、RTO≤60min、仅 80/443（本项目）、无跨租户/重复/绕过/未审计写、D3-D6 全部 PASS、FOUND-SOFTWARE-1 已修复、langfuse 观测可用、Tier 0 租户（TENANT-A）就绪。
- **仍未实测的关键项**（建议放量前补强或作为观察项）：对账 mismatch 计数（后台）、多租户并发/检查点隔离压力、应用侧 Langfuse trace 真实上报、告警指标增强（错误率/审批/对账/人工介入/RPO-RTO/安全拒绝无法计量）、真实 LLM（openai_compatible）+ 真实外部网关（live）链路。
- 若队长决定放量，应**从 Tier 0（TENANT-A）开始**，需同租户 `admin`/`approver` 二次确认审批并留痕；上表未实测项作为**观察期监控项**。

---

*交付：deploy-prod-ready 团队 · t13 report-compiler 最终化（RPO 达标，完成标准表三档标注）。依据：t1/t8/t12 deploy-engineer、t2/t5/t9 security-auditor、t3/t6/t10 drill-runner、t4/t7/t11 report-compiler。*
