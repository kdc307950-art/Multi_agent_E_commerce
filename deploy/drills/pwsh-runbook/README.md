# Preview op-runbook（PowerShell 演练脚本集合）

> **定位**：把 `deploy/drills/*.sh` 的 bash 演练重实现为**可在本机（Windows）执行的 PowerShell + Python 等价脚本**。
> 本仓库运行环境没有可用的 bash（仅有损坏的 WSL shim），因此这些 `.ps1`/`.py` 是本机实跑 `after-sales-preview`
> 栈时的权威入口。它们**不依赖 bash**，也**不打印任何密钥**（口令只经 `--env-file deploy/.env.preview` 注入）。
>
> **重要**：本集合只负责"**就位的脚本**"；**执行在 t3**（栈 `after-sales-preview` 由 t1 建好并注入密钥后）。
> 任何脚本在栈未就绪时都不会产出"伪装 PASS"——D1/D2 依赖 `Assert-EnvFile`/`Assert-Docker`，D3-D6 缺
> `PREVIEW_TOKEN` 时返回 BLOCKED（退出码 2/3），绝不伪造结果。

---

## 1. 脚本清单与用途

| 文件 | 对应 bash | 演练 | 说明 |
|------|-----------|------|------|
| `common.ps1` | `deploy/scripts/common.sh` | — | 公共助手：路径/环境解析、`docker compose` 封装（二进制安全 stdin/stdout 重定向）、`Invoke-WebRequest -SkipCertificateCheck` 健康探测 + `curl.exe` 兜底、.env 非敏感键读取、记录写入。 |
| `run_backup_restore.ps1` | `drill_pg_backup_restore.sh` | D2 | 备份→写标记→恢复临时库→校验标记排除且 `TENANT-A` 保留→算 RPO/RTO→写 JSON + Markdown。 |
| `run_restart_services.ps1` | `drill_restart_services.sh` | D1 | 依次重启 api/worker/redis→校验 `/api/healthz` 200、worker 运行、redis 重启后 `pg_isready` 就绪→写记录。 |
| `run_rollback.ps1` | `deploy/scripts/rollback.sh` | 回滚 | 回滚前快照(`pre-rollback-<ts>.dump`)→`pg_restore --clean --if-exists`→（可选）`compose down`+`git checkout`+`build`+`up` 回退应用→健康复验→写 rollback 记录。 |
| `run_api_drills.py` | `drill_api.sh` | D3/D4/D5/D6 | 走真实 HTTPS + 认证 + PostgreSQL 的 API 驱动演练（审批/SSE/并发/对账），仅用标准库；缺事件时返回 BLOCKED。 |
| `run_api_drills.ps1` | — | 封装 | 检查 `PREVIEW_TOKEN`，缺则标注前置（退出码 3），否则转发到 `run_api_drills.py`。 |
| `run_pwsh_runbook.py` | — | **python 封装** | 可读的统一驱动入口：`d1 / d2 / rollback / api <scenario> / all`，把「跑哪个脚本、传什么参数」封装成一行命令，透传退出码。 |
| `run_all.ps1` | `run_preview_drills.sh` | 编排 | 以独立 `pwsh -File` 子进程依次跑 D1/D2（及 PREVIEW_TOKEN 就绪时的 D3-D6），汇总 PASS/FAIL/BLOCKED。回滚为故障驱动，**不**纳入例行编排。 |

**目录**：`deploy/drills/pwsh-runbook/`。**记录输出**：`deploy/drills/records/`（JSON + Markdown）。

---

## 2. 用法（在已部署 preview 栈的服务器上）

```powershell
# 前置校验（可选）：确认 docker、env 文件就绪
pwsh deploy/drills/pwsh-runbook/run_backup_restore.ps1        # 仅会因前置缺失而报错退出

# D1 / D2
pwsh deploy/drills/pwsh-runbook/run_restart_services.ps1
pwsh deploy/drills/pwsh-runbook/run_backup_restore.ps1        # -RpoSeconds 900 可覆盖 RPO 基线

# 回滚（故障驱动，独立执行；务必核对备份 + git 引用）
pwsh deploy/drills/pwsh-runbook/run_rollback.ps1 -BackupFile deploy/backups/langgraph-<ts>.dump
pwsh deploy/drills/pwsh-runbook/run_rollback.ps1 -BackupFile ... -GitRef <commit>

# D3-D6（需要真实 JWT）
$env:PREVIEW_TOKEN = '<有效JWT：上线白名单租户 admin/approver>'
pwsh deploy/drills/pwsh-runbook/run_api_drills.ps1 approval     # 或 sse / concurrent / reconcile

# 一次性编排（D1/D2 + 就绪时的 D3-D6）
pwsh deploy/drills/pwsh-runbook/run_all.ps1
```

**统一 python 封装**（可读入口，等价驱动全部）：

```bash
python deploy/drills/pwsh-runbook/run_pwsh_runbook.py d1
python deploy/drills/pwsh-runbook/run_pwsh_runbook.py d2 --rpo-seconds 900
python deploy/drills/pwsh-runbook/run_pwsh_runbook.py rollback --backup deploy/backups/langgraph-<ts>.dump --git-ref <commit>
python deploy/drills/pwsh-runbook/run_pwsh_runbook.py api approval   # or sse / concurrent / reconcile
python deploy/drills/pwsh-runbook/run_pwsh_runbook.py all
```

可覆盖环境变量（与 bash `common.sh` 语义一致）：`DOCKER`（默认
`C:\Users\孔德草\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe`）、`COMPOSE_FILE`、
`ENV_FILE`、`PREVIEW_BASE`（默认 `https://127.0.0.1`）、`PREVIEW_TOKEN`。

---

## 3. RPO / RTO 测量口径

- **RTO（恢复耗时）**：`run_backup_restore.ps1` 用 `[Stopwatch]` 计时 `pg_restore` 命令行自开始到退出，
  写入 `rto_restore_seconds`。这是**实测值**。
- **RPO（数据丢失窗口）**：脚本默认取**备份周期基线 15 分钟**（`rpo_seconds=900`，与 `drill_pg_backup_restore.sh`
  一致，`rpo_basis="baseline_15min"`）。**每次实拍**备份周期时，可用 `-RpoSeconds <实测秒数>` 覆盖，此时
  `rpo_basis="measured_override"`。若要真正测量实际备份间隔，应由每日 cron（`backup_db.sh`）在每次备份时
  记录「上次备份时间」，再由本演练按差值计算——脚本已预留 `-RpoSeconds` 接口，未硬编码造假。
- **验收基线**：RPO ≤ 900s（15min）、RTO ≤ 3600s（60min），见 `deploy/OPS_RUNBOOK.md` §2。

`drill-pg-backup-restore.json` 字段与 bash 版对齐：`scenario` / `rpo_seconds` / `rto_restore_seconds` /
`restore_ok` / `marker_excluded` / `tenants_preserved`，另附加 `backup_file` / `backup_bytes` / `db_name` /
`restore_db` / `rpo_basis` / `timestamp`。

---

## 4. 依赖 t1（栈 / LLM）结果的判定

这些脚本**可写成并校验语法**，但**能否实跑出可信结果**取决于 t1 是否成功：

| 步骤 | 依赖 t1 的什么 | 不满足时的表现 |
|------|----------------|----------------|
| D1 重启 | `deploy/.env.preview`（t1 生成）+ `after-sales-preview` 栈已启动 + HTTPS 证书 | `Assert-EnvFile` / `Assert-Docker` 报错退出；健康探测 `Wait-Healthz` 非 200 → FAIL |
| D2 备份恢复 | 同上 + `postgres` 容器运行、`tenants` 表结构存在 | 备份/恢复命令非 0 → FAIL |
| 回滚 | 同上 + 一个真实备份 dump 与 `git` 可用 | 缺备份文件 / 恢复非 0 → FAIL；未给 `-GitRef` 只恢复数据 |
| D3 审批 | 栈 + **可达 LLM 端点**（或 `EXECUTION_PROVIDER=mock` 能驱动 chat 流）+ 白名单租户 JWT | 未得到 `approval_required` 事件 → BLOCKED(2)，非伪造 PASS |
| D4 SSE | 同上（需 `accepted` 事件 + resume 返回 `done`） | 未得 `accepted` → BLOCKED(2) |
| D5 并发 | 同上（需两次 `accepted` 且 `stream_id` 相同） | 未得 `accepted` → BLOCKED(2) |
| D6 对账 | 栈 + worker 调度 `reconcile_tenant` 任务 | 入口不可达 → BLOCKED(2) |

**结论**：
- **D1 / D2 / 回滚**：只要 **t1 成功**（生成 `deploy/.env.preview` + 栈 `after-sales-preview` 起来），即可直接实跑。
  它们不依赖外部 LLM，只依赖 Docker + PostgreSQL 数据面。
- **D3-D6（API 驱动）**：**除 t1 栈成功外，还需 LLM/mock 能驱动 chat 流 + 一个白名单租户 JWT**。
  t1 需明确「`EXECUTION_PROVIDER=mock` 是否足以驱动 `approval_required`/`accepted`/`done` 事件」；
  若 t1 结论是 **mock 可驱动** → 本 runbook 的 `run_api_drills.py` 即可实跑；若结论是 **需要真实 LLM 端点** →
  需确认 LLM 端点对该租户可达后，才能判定 D3-D5 可跑。**在 t1 给出结论前，D3-D6 保持 BLOCKED 归属，不宣称达标。**

---

## 5. 诚实边界

- 本 runbook 只交付**脚本 + 用法 + 依赖判定**；**未在真实栈上执行**（栈/密钥由 t1 提供，执行在 t3）。
- `run_api_drills.py` 的 D6 仅校验入口可达 + 审计存在作最小验证；完整 `reconcile_tenant` 对账结果由 worker
  侧任务产出（与 bash 版一致的诚实边界）。
- 所有记录写入 `deploy/drills/records/`，JSON 供机器复核，Markdown 对齐 `record_template.md` 供人工签字。
