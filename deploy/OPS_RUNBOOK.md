# 上线阈值 · 回滚条件 · 值班处理流程 · 每日备份恢复演练记录（OPS RUNBOOK）

> 定位：预发布 → 生产上线的**可执行运行手册**。与《PREVIEW_DEPLOYMENT_CHECKLIST》《生产环境架构设计》
> 《代理宪法》（第一层安全红线）配套。所有阈值以**真实演练/实测**为准，未经实测不得宣称达标。
> 首次上线限制：**少量租户、仅审批后执行、全量审计、人工复核**（见 §1 与 `src/core/launch_gate.py`）。

---

## 1. 首次上线门控（少量租户 / 仅审批后执行 / 全量审计 / 人工复核）

| 约束 | 落地方式 | 强制校验 |
|------|----------|----------|
| 少量租户 | `LAUNCH_ALLOWED_TENANTS` 白名单（逗号分隔）；非名单租户请求 → 403 并写审计 | `tenant_allowed_for_launch`；`verify_launch_gate --strict` |
| 仅审批后执行 | 退款/退货/改址必经唯一 `human_approval` interrupt；无 direct 绕过 | `graph/approval.py`；`launch_require_approval=true` |
| 全量审计 | 每条敏感/拒绝路径 `store.append_audit`（tenant/session/approval/operation 维度） | `launch_full_audit=true` |
| 人工复核 | 审批人/转人工/降级/越权拒绝均留痕；`platform_admin` 独立受控 | `launch_manual_review=true` |
| 数据面 | PostgreSQL（持久化 + RLS + `TenantScopedCheckpointer`）；运行角色仅 DML | `STORAGE_BACKEND=postgres` |
| 执行模式 | shadow（沙箱）；**不上真实资金** | `EXECUTION_MODE=shadow` |

`scripts/verify_launch_gate.py --strict` 对受限环境违规项非零退出（fail-closed）。

---

## 2. 上线阈值（达到才可放量）

| 区域 | 阈值 | 验收方式 |
|------|------|----------|
| 部署健康 | `healthcheck.sh` 4 项全过；仅 80/443 开放；无 CORS 头 | `bash deploy/scripts/healthcheck.sh` |
| 数据恢复 | RPO ≤ 15 分钟，RTO ≤ 60 分钟 | `restore_drill.sh`（**加密备份**解密恢复 + 实测 RPO/RTO 写入记录） |
| 加密备份 | 归档为密文（aes-256-cbc+PBKDF2）、附 SHA-256 校验和、可解密恢复 | `restore_drill.sh`（密文可解 + 校验和复核） |
| 最小权限备份账号 | 备份用 `backup_role`（仅 CONNECT/SELECT，无写/DDL） | `backup_encrypted.sh`/`preview_backup_scheduler.sh` + 下钻验证备份角色无 DML/DDL |
| 异机副本 | 加密归档推送到代码库之外的受控目录/异机，按 `BACKUP_KEEP` 保留 | `backup_encrypted.sh`（`REMOTE_BACKUP_DIR`/`scp`/`rsync`） |
| 重启韧性 | API/worker/Redis 任一重启后读接口恢复；Redis 重启不影响 PG 数据面 | `drill_restart_services.sh` |
| 审批幂等 | 同一审批重复决策收敛到单一 `operation_id`，不重复执行 | `drill_api.sh approval` |
| SSE 恢复 | 断线 resume 只重放既有事件，**不**重新执行 | `drill_api.sh sse` |
| 并发幂等 | 同一 `client_request_id` → 单一 stream/单一操作 | `drill_api.sh concurrent` |
| 对账 | 非终态收口；不一致/不可核实 → 转人工（绝不静默） | `drill_api.sh reconcile` |
| 审计追溯 | 关键审计可按 `tenant_id`/`session_id`/`approval_id`/`operation_id` 四维追溯 | `GET /api/audit?target_type=approval&target_id=...` |
| 写操作模型 | 写风险工具仅 `HIGH_CONFIDENCE_MODELS`（评测通过）白名单模型可写；否则转人工 | `capability.resolve_high_confidence_models` |
| 完全自托管 | LLM 端点白名单；Graphiti 遥测关闭；无未经批准出网 | `EndpointGuard`、`LLM_ALLOWED_HOSTS` |

**放量条件**：上述全部达到且演练记录可复核；白名单从少量租户（如 1–2 个）开始，逐步扩至全部。

---

## 3. 回滚条件（故障即回滚）

**触发回滚的信号**（满足任一）：
1. `healthcheck.sh` 任一项失败（暴露内网端口 / HTTPS 异常 / CORS 泄漏 / fail-closed 失效）。
2. `drill_pg_backup_restore.sh` 的 RPO/RTO 超基线，或恢复后数据不完整。
3. 审批/执行/对账出现**重复业务副作用**（同一 `operation_id` 被执行两次）——立即停止并回滚。
4. 任一租户"仅审批后执行"被绕过（出现未审批即执行）。
5. 指标异常：错误率、审批失败率、对账 mismatch 率、转人工率显著上升（告警触发）。
6. 审计追溯缺失：关键审计无法按四维定位到资源。

**回滚动作**（安全优先，恢复靠快照）：
```bash
# 0) 回滚前快照（可逆性）—— 加密备份（需要 BACKUP_ENC_KEY 注入；见 DR_KEY_MANAGEMENT.md）
BACKUP_ENC_KEY=<注入> bash deploy/scripts/backup_db.sh deploy/backups/pre-rollback-$(date +%Y%m%d%H%M%S)
# 1) 恢复到变更前快照 + 重建应用到变更前 git 引用
bash deploy/scripts/rollback.sh deploy/backups/langgraph-<ts>.dump <git-ref>
# 2) 健康复验
bash deploy/scripts/healthcheck.sh
```
> 数据库向前迁移幂等（`CREATE IF NOT EXISTS`/`CREATE OR REPLACE`），故回滚=恢复数据（RPO）+ 应用版本回退；
> 不提供"部分回滚到中间 schema"。生产基线"先加密备份、再变更、恢复靠快照"。
> 注：`rollback.sh` 内联的"回滚前快照"用 `pg_restore` 需要的**明文** pg_dump 直连 owner 执行（重放恢复用）；
> 独立、可长期存档的**DR 加密归档**请走 `backup_encrypted.sh`/`restore_drill.sh`。

---

## 4. 值班处理流程（On-Call）

| 步骤 | 动作 | 产出 |
|------|------|------|
| 1 告警接收 | Prometheus/Grafana/Langfuse/Loki 告警 → 值班人 5 分钟内响应 | 值班记录 |
| 2 定性 | 判断影响面：数据面 / 应用面 / 观测面 / 安全（审计/越权） | 事故分类 |
| 3 止损 | 若涉及资金/审批/越权 → 先止损（可回滚或停相关租户白名单）；**不**用低档模型续跑 | 止损动作 |
| 4 排查 | 查 Langfuse trace（按 tenant/session）、Loki 日志（已脱敏）、`GET /api/audit` 四维追溯 | 根因假设 |
| 5 恢复/回滚 | 按 §3 回滚条件决定；数据库恢复走 `rollback.sh` | 恢复结果 |
| 6 复核 | 备份恢复演练 + 变更后 `healthcheck.sh` + 对账 | 复核记录 |
| 7 复盘 | 更新 OPS_RUNBOOK / 告警阈值 / 演练记录；需二次审批才恢复变更 | 复盘记录 |

**值班红线**：
- 绝不把 PII（地址/支付/卡号）写入工单/日志；日志已在应用内全局脱敏，值班只读脱敏后的证据。
- 越权/跨租户事件一律以 `platform_admin` 独立流程 + 二次确认 + 不可抵赖审计处理。
- 敏感写异常一律转人工，绝不自行用低可靠模型补齐。

---

## 5. 每日备份恢复演练记录

每日例行：**加密备份 → 解密恢复 → 采样校验**（实测 RPO/RTO），把结果按 `deploy/drills/record_template.md` 填写，
留存在 `deploy/drills/records/`。建议 cron（每日 03:00 加密备份、每日 04:00 加密恢复演练）：

```bash
# crontab（需注入 BACKUP_ENC_KEY；密钥管理见 deploy/DR_KEY_MANAGEMENT.md）
0 3 * * * BACKUP_ENC_KEY=<注入> bash /path/deploy/scripts/backup_encrypted.sh
0 4 * * * BACKUP_ENC_KEY=<注入> bash /path/deploy/scripts/restore_drill.sh
```

每日演练记录要点：日期、加密备份文件、**实测 RPO/RTO**、恢复后数据校验、最小权限备份角色、执行人/复核人、异常与处理。
**任何一天恢复失败都应升级为值班事件并回滚相关变更。**
> 加密备份由 `backup` sidecar（`preview_backup_scheduler.sh`，最小权限 `backup_role` + aes-256-cbc + SHA-256）周期性执行，
> 备份点后变更在恢复演练中必须被排除（marker 校验），禁止以"同机 pg_dump 当结论"。

---

## 6. 观测链路运维

- **可观测栈**：`bash deploy/scripts/observability.sh up|down|health`（Langfuse + Prometheus + Grafana + Loki + Promtail；完全自托管）。
- **关键审计追踪**：`GET /api/audit?tenant_id=...&target_type=approval&target_id=...`（受控、detail 已脱敏）；
  平台级跨租户用 `platform_admin` 独立流程。
- **指标口径**：有界标签（route/status/kind），**绝不用高基数 `tenant_id` 作标签**；租户明细走审计查询。
- **脱敏保证**：所有 logger 的消息与敏感 extra 字段在 emit 前经 `redact()`；结构化日志带 tenant 上下文。

---

*与《生产环境架构设计》§4、《PREVIEW_DEPLOYMENT_CHECKLIST》、《代理宪法》配套。上线阈值以真实演练为准，未经实测不得对外宣称达标。*
