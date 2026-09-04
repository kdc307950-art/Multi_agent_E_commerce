# t4 — 交付文档模板/骨架 + 回填字段清单（字段来源映射）

> **本文件是 t4 汇总**：说明 6 份交付文档的模板骨架，并逐字段列出需从 **t1（部署基线）/ t2（安全验收）/ t3（演练指标）**
> （以及后续 t5 动态安全验收、t6 DR 实跑）回填的具体字段。**当前各文档的值字段全部为 `<...>` 占位（空缺），
> 本阶段只产出骨架与字段清单，未编造任何实值。** 所有结论类字段在实测前一律不填"通过/是/达标"。

---

## t7 状态更新（20260904-030903 · 草稿）

> 本矩阵为**中间产物（草稿）**。下方按【已定】【RPO 周期界定】标注字段当前可回填状态，权威汇总见
> `evidence/DEPLOYMENT_SUMMARY-20260904-030903.md`（终稿·t13）。**RPO 已达标（t12 加 15min 周期 pg_dump 备份界定：marker_in_restore=0、TENANT_A_in_restore=1、restore_RTO=1.9s）；t8/t9/t10 已落地，相关字段已回填为真实 PASS/实测值。**

### 已定（t1/t2/t5/t6/t9/t10 落地，可回填）
- 生产部署记录：拓扑/组件版本/git(**7941246**)/环境门控/健康检查/三道闸门/构建方式/TLS(CN=preview.local 自签名) → **已回填**（t1）。
- 灰度租户清单：TENANT-A/B + 10 成员角色 + 白名单（EVIL-TENANT 拒） → **已回填**（t1/t2/t5）。
- 备份恢复记录：RTO=1.668s 实测、marker=0、TENANT-A/B 保留、备份文件/字节 → **已回填**（t6/t7）；**RPO≤15min 达标（t12 15min 周期备份界定）**。
- 回滚记录：pre-rollback 快照、恢复、healthcheck 复验、app 重建 no-op(target==deployed) → **已回填**（t6/t7）。
- 运营告警规则：`deploy/observability/alert-rules.yml`（8 组 14 规则，基于真实 metric 名 `api_requests_total`/`approval_decisions_total`/`audit_queries_total`/`up`）→ **可回填**；**需指标增强方可生效**（见 §5 缺口）。
- 指标表：RTO / 错误率(0/7) / 人工介入率(0) / 审批耗时(0.145s 数据面 + 0.204s API 往返，实测) / 无跨租户·无重复·无绕过·无未审计写(PASS，t5/t9) / D3-D6(PASS，t10) → **已回填**。
- FOUND-SOFTWARE-1：t9 已修复并 **commit（HEAD=`7941246`）**（`postgres_store.py` 新增 `_as_json`，5 处 JSONB 读取切换，+20/-5，未夹带 migrations.py）+ 重建 api/worker 生效 → **已回填（结论：发现→已修复 commit；`migrations.py` 为先前会话改动仍未提交）**。
- 宿主仅 80/443：**t8 已达成**（停 dev 栈+遗留进程，本项目宿主监听仅 {80,443}；注明 3100 为无关遗留宿主进程）→ **已回填**。
- 观测栈 langfuse：**t8 已修复（降级 v2.95.11 健康，7 组件 healthy）** → **已回填**。

### RPO 周期界定（已达标 · t12）
- **RPO ≤ 15min**：t12 已加 **15min 周期 pg_dump 备份**（`docker-compose.preview.yml` `backup` 服务每 900s，保留 8，宿主持久 `./data/preview-backups`，首份 `langgraph-20260903193257.dump`），校验 **marker_in_restore=0、TENANT_A_in_restore=1、restore_RTO=1.9s** → RPO 上界=15min 周期界定，**≤15min 达标**。相关字段在 `DEPLOYMENT_SUMMARY` 完成标准表、`METRICS_SUMMARY`、`SCALEUP_APPROVAL` #2、`DR-...pg-backup-restore` 均已按「周期界定/达标」回填。**边界**：pg_dump 周期而非 PITR/WAL 归档（更紧 RPO 的后续增强项）。

## 0. 交付物清单与文件

| # | 交付物 | 模板文件（骨架） | 最终文件名约定 | 放在 |
|---|--------|----------------|----------------|------|
| 1 | 生产部署记录 | `deploy/records/PRODUCTION_DEPLOYMENT_record-TEMPLATE.md` | `PRODUCTION_DEPLOYMENT_record-<ts>.md` | `deploy/records/` |
| 2 | 灰度租户清单 | `deploy/records/CANARY_TENANTS-TEMPLATE.md` | `CANARY_TENANTS-<ts>.md` | `deploy/records/` |
| 3 | 备份恢复记录 | `deploy/drills/records/TEMPLATE-pg-backup-restore.md` | `<ts>-pg-backup-restore.md` | `deploy/drills/records/` |
| 4 | 回滚记录 | `deploy/drills/records/TEMPLATE-rollback.md` | `<ts>-rollback.md` | `deploy/drills/records/` |
| 5 | 运营告警规则 | `deploy/observability/alert-rules/alert-rules.yml`（+`README.md` 说明） | `alert-rules.yml`（生效需接线） | `deploy/observability/alert-rules/` |
| 6 | 放量审批单 | `deploy/records/SCALEUP_APPROVAL-TEMPLATE.md` | `SCALEUP_APPROVAL-<ts>.md` | `deploy/records/` |

> 说明：t4 阶段已创建 `deploy/records/` 与 `deploy/observability/alert-rules/` 两个新目录（原先不存在）。
> 所有模板均以 `-TEMPLATE`、`TEMPLATE-` 前缀命名，避免被后续真实记录覆盖；最终 t7 汇总时应复制为带 `ts` 的文件名并回填。

---

## 1. 生产部署记录（deploy/records/PRODUCTION_DEPLOYMENT_record-<ts>.md）
| 字段 | 模板章节 | 回填来源 | 当前状态 |
|------|---------|---------|---------|
| 记录 ID / 时间戳 / 执行人复核人 | §0 | t1 | `<空缺>` |
| 拓扑与暴露面（compose 项目、网络、nginx 规则） | §1 | t1 | `<空缺>` |
| 组件版本（postgres/redis/api/worker/frontend/nginx/migrate/运行角色） | §2 | t1 | `<空缺>` |
| git 引用 / 构建方式 / build tag / rollback.log | §3 | t1 | `<空缺>`（当前可观测 HEAD=`7941246`，为参考非实测部署引用） |
| 环境与门控变量（ENV/STORAGE/AUTH/LAUNCH_*/EXECUTION_*/LLM_*/HIGH_CONFIDENCE_MODELS 等） | §4 | t1（+t2 合规） | `<空缺>` |
| 健康检查结果（[1]-[4]，监听端口、HTTP code、compose port、fail-closed） | §5 | t1 | `<空缺>` |
| 仅 80/443 验证 | §6 | t1 | `<空缺>` |
| TLS 证书 CN/SAN/有效期/签发者/受信 CA | §7 | t1（+t2 安全） | `<空缺>`（当前自签名 preview.local，非受信 CA） |
| 上线门控三道闸门结果（check_secrets/verify_launch_gate/compose config/verify_fail_closed） | §8 | t1 | `<空缺>` |
| 回滚就绪（备份/rollback.log） | §9 | t1 | `<空缺>` |
| 部署结论（compose ps 全 running、healthcheck 退出 0） | 结论 | t1 | `<空缺>` |

## 2. 灰度租户清单（deploy/records/CANARY_TENANTS-<ts>.md）
| 字段 | 模板章节 | 回填来源 | 当前状态 |
|------|---------|---------|---------|
| 首批租户白名单 + 数据面预置 + 状态 | §1 | t1（确认）+ t2（合规） | `<空缺>` |
| 成员角色与归属（customer/agent/admin/approver）、是否可审批 | §2 | t1/t2 | `<空缺>` |
| 登录凭据是否注入（AUTH_LOGIN_CREDENTIALS） | §2 | t1 | `<空缺>` |
| 放量阶梯（Tier0-3 范围/动作/通过条件/审批单引用） | §3 | t1/t2/t3 | `<空缺>`（阶梯为设计占位） |
| 放量审批单引用 + 放量前条件 | §4 | t7 | `<未创建>` |
| 风险（跨租户/停用租户/记忆隔离） | §5 | t2 | `<空缺>` |
| 结论（租户就绪/是否允许进 Tier0/1） | 结论 | t1/t2 | `<空缺>` |

## 3. 备份恢复记录（deploy/drills/records/<ts>-pg-backup-restore.md）
| 字段 | 模板章节 | 回填来源 | 当前状态 |
|------|---------|---------|---------|
| 演练编号/环境/执行人复核人/时间/git 引用 | §0 | t3（+t1 git） | `<空缺>` |
| 备份文件/备份耗时/**RPO(≤15min)** | §1 | t3/t6 | `<空缺>`（旧基线 rpo=0.0 为上一阶段，**不沿用为本阶段达标证据**） |
| 恢复耗时/**RTO(≤60min)** | §2 | t3/t6 | `<空缺>` |
| 恢复后校验（行数/checkpoint/RLS/运行角色/租户隔离/幂等） | §3 | t3/t6 | `<空缺>` |
| 关键证据（postgres_recovery.json / 四维追溯） | §4 | t3/t6 | `<空缺>` |
| 异常与处理 / 是否回滚 | §5 | t6 | `<空缺>` |
| 结论（RPO/RTO 达标、数据完整、验收） | 结论 | t3/t6 | `<空缺>` |

## 4. 回滚记录（deploy/drills/records/<ts>-rollback.md）
| 字段 | 模板章节 | 回填来源 | 当前状态 |
|------|---------|---------|---------|
| 回滚编号/环境/执行人复核人/时间/git 引用（front-ref/target-ref） | §0 | t6（+t1 git） | `<空缺>` |
| 触发条件命中（OPS_RUNBOOK §3 的 6 条信号） | §1 | t6 | `<空缺>` |
| 回滚前快照（pre-rollback dump + 验证 + 耗时） | §2 | t6 | `<空缺>` |
| 回滚动作（pg_restore / git checkout / build / up）结果 | §3 | t6 | `<空缺>` |
| 复验（healthcheck/数据一致性/审计/幂等/租户白名单） | §4 | t6（+t2 审计） | `<空缺>` |
| 结论（成功/无净副作用/二次审批/复盘） | §5 | t6 | `<空缺>` |

## 5. 运营告警规则（deploy/observability/alert-rules/）
| 字段 | 模板章节 | 回填来源 | 当前状态 |
|------|---------|---------|---------|
| 组件存活规则（ApiJobDown/PrometheusJobDown/LokiJobDown） | alert-rules.yml | t1/t2 | **可立即生效**（基于 `up`），阈值已占位 |
| 错误率规则（ApiHighErrorRate，5xx>5%） | alert-rules.yml | t2（指标增强）+ t3 | ⚠️ **为空**（`api_requests_total` 缺 `status` 标签） |
| 审批失败率（ApprovalFailureRateHigh） | alert-rules.yml | t2（指标增强） | ⚠️ **为空**（`approval_decisions_total` 缺 `status`） |
| 对账 mismatch（ReconciliationMismatch） | alert-rules.yml | t3/t6（新指标） | ⚠️ **为空**（需新增 `reconcile_mismatch_total`） |
| 人工介入率（HighHumanInterventionRate） | alert-rules.yml | t3/t6（新指标） | ⚠️ **为空**（需新增 `human_intervention_total`） |
| RPO/RTO 突破（RpoExceeded/RtoExceeded） | alert-rules.yml | t3/t6（push/记录） | ⚠️ **为空**（需 `drill_rpo_seconds`/`drill_rto_seconds`） |
| 接线（`prometheus.yml` `rule_files` + compose 卷 + 抓取目标） | README §4 | t1/t5/t7 | ⚠️ **未接线** |
| Grafana 看板/通知/权限 | README §3 | t1/t2 | ⚠️ 待配置 |
| **标签纪律：禁用 tenant_id 等** | 贯穿 | t2 评审 | ✅ 已在 `metrics.py` 强制（`_FORBIDDEN_LABELS`） |

## 6. 放量审批单（deploy/records/SCALEUP_APPROVAL-<ts>.md）
| 字段 | 模板章节 | 回填来源 | 当前状态 |
|------|---------|---------|---------|
| 审批单 ID/阶梯/申请人审批人复核人 | §0 | t1/t3/t2 | `<空缺>` |
| 放量前条件（11 项阈值 + 演练可复核） | §1 | t1/t3/t6/t5 | `<空缺>`（未经实测一律"否"） |
| 目标租户范围 | §2 | t2 | `<空缺>` |
| 风险评估（跨租户/幂等/绕过审批/模型档位/出网/RPO-RTO） | §3 | t2 | `<空缺>` |
| 审批意见 / 二次确认 / 结论 | §4 | t7 | `<空缺>`（须同租户 admin/approver） |
| 结论（放量批准 + 复验要求） | §5 | t7 | `<空缺>` |

---

## 7. 当前数据面/证据（可引用的既有基线——仅作来源，非本阶段达标凭证）
- `evidence/postgres_recovery.json`（上一阶段 pg 备份恢复：backup=0.353s/restore=1.963s/rpo=0.0）
- `evidence/docker_verification.json`（容器级 Redis/worker 重启基线）
- `evidence/pg_acceptance_evidence.json` / `pg_checkpoint_recovery_record.json`（重启/审批/幂等/租户隔离）
- `evidence/COMPENSATION_RECONCILIATION_REPORT.md` / `E2E_APPROVAL_RECORD.md`（对账/审批）
- `evidence/llm_candidate_eval.json`（写操作能力矩阵：`write_op_pass` 白名单模型）
- `evidence/PREVIEW_CONFIG_ACCEPTANCE.md` / `VERIFICATION_REPORT.md` / `live_acceptance_summary.md`
- `deploy/drills/records/DR-20260904-0219-api-worker-redis-restart.md`（已实跑的 D1/D2/D3/D5 基线记录）

> **诚实声明**：上述既有证据多来自本仓库**前期阶段/其它会话**（如 `pg-acceptance-continue`），
> 可作为"上阶段曾验证"的参考，**不能**替代本轮 t1（部署实测）/t2（动态安全验收）/t3+t6（DR 演练实测）的
> 真实回填。凡结论字段，实测前一律不填"通过/达标/是"。

---

## 8. t7 汇总时的核对清单（完成标准）
1. 6 份记录均已由 t1/t2/t3/t5/t6 回填真实值并存为带 `<ts>` 的文件。
2. 运营告警规则已接线（prometheus `rule_files` + compose 卷），且**不含 tenant_id 标签**。
3. RPO≤15min、RTO≤60min、错误率、人工介入率、审批耗时、对账结果,均已按真实口径记录。
4. 灰度租户清单与放量审批单引用一致；审批归属同租户；跨租户拒绝留痕。
5. 无任何"未实测即宣称达标"的字段；空缺字段如实标注。
