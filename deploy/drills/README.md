# 预发布演练（Preview Drills）

> 上线前与每日例行演练。目标是证明：**完整链路可恢复、故障可回滚、关键审计可按租户/会话/审批/operation_id 追溯**。
> 演练必须在**预发布服务器**（具备 Docker + 已部署 `after-sales-preview` 栈）上执行；本仓库提供一个
> 可在**本地 dev 环境（无 Docker/无 PG）** 运行的等价子集脚本，用于持续回归。

---

## 1. 六项演练与证明目标

| # | 演练 | 证明目标 | 脚本 |
|---|------|----------|------|
| D1 | API/worker/Redis 重启 | 重启后读接口恢复；Redis 重启不影响 PostgreSQL 数据面；在途敏感写不重复 | `drill_restart_services.sh` |
| D2 | PostgreSQL 备份恢复 | RPO≤15min、RTO≤60min；恢复后数据完整且快照点后变更被正确排除 | `drill_pg_backup_restore.sh` |
| D3 | 审批中断恢复 | 敏感写**仅审批后执行**；同一审批重复决策幂等收敛到单一操作 | `drill_api.sh approval` |
| D4 | SSE 断线恢复 | resume 从 Last-Event-ID 重放既有事件，**不重复执行** | `drill_api.sh sse` |
| D5 | 并发重复提交 | 同一 `client_request_id` 并发两次 → **单一操作/单一流**（幂等） | `drill_api.sh concurrent` |
| D6 | 真实业务沙箱对账 | 非终态执行→对账收口；不一致/不可核实→**fail-closed 转人工**（不静默） | `drill_api.sh reconcile` |

## 2. 在预发布服务器上执行（完整链路）

前置：`bash deploy/scripts/deploy.sh` 已成功；`after-sales-preview` 栈运行中；`deploy/.env.preview` 已注入密钥；
对 D3-D6 需一个"上线白名单租户"的 admin/approver 的**真实 JWT**（`PREVIEW_TOKEN`）。

```bash
git -C <repo> rev-parse --short HEAD          # 记录应用版本
# D1/D2 独立（服务器操作）
bash deploy/drills/drill_restart_services.sh
bash deploy/drills/drill_pg_backup_restore.sh

# D3-D6（API 驱动；需 PREVIEW_TOKEN）
export PREVIEW_BASE=https://<host>
export PREVIEW_TOKEN=$(< 有效 JWT >)   # 白名单租户 admin/approver
bash deploy/drills/drill_api.sh approval
bash deploy/drills/drill_api.sh sse
bash deploy/drills/drill_api.sh concurrent
bash deploy/drills/drill_api.sh reconcile

# 或一次性编排（生成记录）
bash deploy/drills/run_preview_drills.sh
```

各演练脚本在通过时非零==全部 PASS；且以 `deploy/drills/records/` 下的 JSON/Markdown 记录留痕。

## 3. 本环境（dev）可运行的等价验证

本仓库环境无 Docker / 无可达预发布服务器，故提供 `verify_dev_drills.py`，用 SqliteStore + 内存图
模拟"进程重启"，复现 D1-D6 的逻辑断言并生成结构化记录：

```bash
python deploy/drills/verify_dev_drills.py --record-dir deploy/drills/records
```

它实际运行这六类场景（见脚本内注释），并在本机生成 `dev-drills-<ts>.json`。这是本会话可验证的证据。

## 4. 边界与诚实说明

- **进程间审批中断续跑**（跨重启恢复图 interrupt 状态）依赖 `PostgresSaver` checkpointer 的
  `TenantScopedCheckpointer`（同连接/同事务设置 RLS），属 PostgreSQL 数据面能力。dev 内存/sqlite 后端
  使用 `InMemorySaver`，不保留跨进程图状态，因此该场景由 `drill_api.sh approval` 在预发布服务器复验，
  以及 `tests/test_pg_checkpoint_recovery.py`（postgres 标记）覆盖。
- 本会话无 Docker、无 SSH 主机，**服务器实机部分**以上述可直接执行的脚本 + 记录模板交付；`verify_dev_drills.py`
  在本机实际运行通过（见 `deploy/drills/records/dev-drills-*.json`）。

## 5. 记录规范

每项演练结果写入 `deploy/drills/records/`；每日例行备份恢复演练按 `record_template.md` 填写，且经
`OPS_RUNBOOK.md` §5 的"每日备份恢复演练记录"入库留痕。
