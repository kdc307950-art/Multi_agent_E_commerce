# CONCURRENCY_RECOVERY_REPORT.md — T3·回调并发 + 沙箱并发 + 恢复一致性 + 备份恢复演练取证

> 执行者：**test-runner**（测试/演练员） · 团队：prod-go-live · 任务：t3（attempt 1）
> 时间：2026-09-04（真实执行） · 生成：evidence/prod-go-live/test-runner/
> 原则：**只记录真实数值与真实证据；缺项、缺陷、边界一律如实标注，绝不填默认值/臆造。**

---

## 0. 环境与对象声明（诚实标注）

| 项 | 实况 |
|---|---|
| 生产栈 `after-sales-prod` | **未部署**（docker compose ls 无该栈；无 `data/prod-backups` → 本次创建） |
| 运行中的栈 | `after-sales-preview`（project=after-sales-preview，含 postgres:17-alpine、api、worker、frontend、nginx、redis、observability 等） |
| 演练对象 | **preview 栈 + 一次性独立 PostgreSQL**（`after-sales-drill-pg`，docker.1ms.run/library/postgres:**17-alpine**，与 preview 同一镜像/版本）。标注：生产栈未部署，故以 preview 同版本 + 独立临时库取证，并遵循「多成员分库隔离、不破坏共享 preview 库」约束。 |
| 一次性库连接 | `127.0.0.1:56742`（host 已发布），库 `langgraph_drill`；owner=migrator(SUPERUSER)、运行角色 app_runtime(NOBYPASSRLS)、备份角色 backup_role(BYPASSRLS 只读)。**临时性**，演练后即弃。 |
| DR 密钥 | `BACKUP_ENC_KEY` / `BACKUP_ROLE_PASSWORD` 为**一次性测试密钥**（openssl rand 生成），非生产密钥；`deploy/.env.preview` 中原本**没有**这两个键。来源已注明，用于本次演练，绝不写入仓库明文。 |

### 关键发现（生产阻断级，先行高亮）
1. **仓库迁移存在致命 FK 缺陷**：`src/infrastructure/migrations.py` 的 `shipping_events` 表 FK 写成
   `order_id TEXT ... REFERENCES orders(tenant_id, order_id)` —— 单引用列指向两列复合主键，
   PostgreSQL 报 `number of referencing and referenced columns for foreign key disagree`。
   **任何对干净库调用 `initialize_all()` 都会在 `apply_business_schema` 阶段失败**，因此
   **生产新库/恢复新库无法完成迁移**。这是本次 go-live 必须修复的**硬 NO-GO 项**。
2. **迁移与已部署 preview schema 漂移**：preview DB 实际有 12 张 RLS 表、12 条 policy，**无 `shipping_events`、无 `orders` 表**；
   仓库迁移比 preview 部署**超前**（多出 shipping_events/orders/policy_documents/RLS 等），说明 preview 是从更早的 schema 快照部署的，当前迁移版本尚未在其上应用。漂移+缺陷双重风险。
3. **备份角色最小权限验证通过**：backup_role `rolbypassrls=t`（仅绕行级读全量快照，非写/DDL）、`has_select_all=17`、`has_dml_all=0`（无 INSERT/UPDATE/DELETE/TRUNCATE）——符合《备份角色只用只读》红线。

---

## 1. 回调并发测试（引擎层，N=256）

**方法**：`scripts/concurrency_stress.py`（SQLite 后端）+ 自研 `concurrency_stress_pg_runner.py`（复用同一不变式函数，跑在**真实 PostgreSQL** 上）。衡量不变式而非调度顺序。

### 1.1 SQLite 后端（evidence/concurrency_stress.json，N=256，全通过）

| 场景 | 并发 | provider.submit 次数 | distinct external_txn_id | execution record | distinct execution_id | errors | 结论 |
|---|---|---|---|---|---|---|---|
| I1 同租户同 operation 并发 execute | 256 | **1** | **1** | **1** | **1** | {} | PASS |
| I2 同幂等键跨租户并发 | 256/租户 | A=1,B=1 | — | opA=1,opB=1 | A=1,B=1 | {} | PASS |
| I3 同 thread 同 nonce 回调 CAS | 256 | — | — | — | applied=1 | — | PASS |

- I1：`provider_submit_calls=1`、`distinct_external_txn_id=1`、`execution_records_for_op=1`、`distinct_execution_id=1`、`statuses=["submitted"]` —— **引擎只对外提交一次、只落 1 条执行记录**。
- I2：`cross_tenant_no_overwrite=true`（exec_a.tenant=A, exec_b.tenant=B, execution_id 不同）—— **同租户幂等键收敛同一结果、跨租户同键互不覆盖**。
- I3：`applied=1, replay=255, final_status=confirmed` —— **同 nonce 高并发下 CAS 只允许一个 confirmed 终态，其余 255 个 replay**（终态封闭）。

### 1.2 真实 PostgreSQL 后端（evidence/concurrency_stress_pg.json，N=256，全通过）
（复用一次性独立库 + 修正 FK 后的 schema + RLS + 行锁）

| 场景 | provider.submit | distinct txn | record | distinct eid | errors | 结论 |
|---|---|---|---|---|---|---|
| I1 同租户同 operation 并发 | **1** | **1** | **1** | **1** | {} | PASS |
| I2 跨租户同幂等键 | A=1,B=1 | — | opA=1,opB=1 | A=1,B=1 | {} | PASS |
| I3 同 nonce 回调 CAS | — | — | applied=1 | replay=255 | — | PASS |

> PG 后端在真实 `FOR UPDATE` 行锁 + RLS + `claim_execution_submit`（attempts 0→1 原子认领）下仍成立
> 「submit 恰好一次、单条执行记录、终态封闭」——这是**真实生产数据面**（非 mock）的证据。

**结论：回调并发 §1 PASS（真实并发数 N=256，实际 submit 计数=1）。**

---

## 2. 沙箱并发压测（真实网关沙箱端到端 HTTP）

**方法**：`scripts/sandbox_concurrency_stress.py` —— submit/query/compensate 全部走**真实 HTTP 沙箱网关**
（`src/execution/sandbox_gateway.py`，uvicorn 后台线程），而非引擎内 mock provider。证据 `evidence/sandbox_concurrency_stress.json`（N=256）。

| 场景 | provider.submit | compensate | distinct reversal | record | distinct eid | errors | 终态 | 结论 |
|---|---|---|---|---|---|---|---|---|
| I1 真沙箱同 operation 并发 | **1** | — | — | **1** | **1** | {} | submitted | PASS |
| FAIL 显式失败并发 | **1** | **1** | **1** | **1** | **1** | {} | **compensated** | PASS |
| I2 真沙箱跨租户同幂等键 | A=1,B=1 | — | — | opA=1,opB=1 | A=1,B=1 | {} | — | PASS |

- I1：即使真实网关（进程外）也只被提交一次（`provider.submit_calls=1`、`external_txn_id` 唯一、`record=1`、`errors={}`）。
- FAIL（`default_status=failed` 走真实 HTTP）：收敛单一终态 `compensated`，`compensate=1`、`distinct_reversal_id=1` —— **不再像修复前重复补偿/跃迁抛错**。
- I2：跨租户真实网关下各 1 条、互不覆盖。

**结论：沙箱并发 §2 PASS。**

---

## 3. 恢复一致性演练（恢复到临时库后一致性）

**方法**：`scripts/recovery_consistency_drill.py`（SQLite 冷备 = online backup API 一致性快照，等效 pg_dump），
证据 `evidence/recovery_consistency.json`（并另存 `deploy/drills/records/recovery-consistency-*.json`）。
聚焦「恢复后一致性」，不含 RPO/RTO（由 §4 负责）。

| 检查项 | 含义 | 结果 |
|---|---|---|
| C1 幂等键唯一（同租户同键 1 条） | `UNIQUE(tenant_id, idempotency_key)` 恢复后仍成立 | PASS |
| C1 跨租户同键互不覆盖 | 不同租户同幂等键字符串互不冲突 | PASS |
| C1 幂等重放恢复后仍生效 | 同幂等键创建返回既有 operation | PASS |
| C2 执行锚点唯一 | `UNIQUE(tenant_id, operation_id)` 每个 op 至多一条 execution | PASS |
| C3 审批幂等唯一 | 每个 operation 至多一个审批单 | PASS |
| C4 租户边界 / 跨租户零可见 | 恢复后 TENANT-A 记录不被 TENANT-B 可见 | PASS |
| C5 敏感审批终态保留 | APPROVED 终态恢复后不漂移 | PASS |
| C5 审计可追溯 + 租户归属 | audit 保留且 tenant_id 归属正确 | PASS |

**结论：consistency=PASS（8/8 检查项）。**

补充：真实 PG 上的回调并发/CAS/跨租户伪造安全测试（`tests/test_pg_callback_concurrency.py`，7 passed）
在**真实 PG + RLS + 行锁**上验证了同一 nonce 只一个终态、异 nonce 收敛单终态、成功/失败竞争单终态、
终态后回调 terminal_locked 不覆盖、跨租户 execution_id 伪造 → `not_found` 拒绝码 + 零污染。见 §6 测试汇总。

---

## 4. 备份恢复演练（加密备份 → 独立临时库恢复 → 校验 → 实测 RPO/RTO）

**方法**：`deploy/scripts/backup_encrypted.sh`（aes-256-cbc + PBKDF2 + SHA-256 + backup_role 最小权限只读）
+ `deploy/scripts/restore_drill.sh`（解密 → 恢复独立临时库 → 校验 → 实测 RPO/RTO），**direct-DSN 模式**在 WSL 执行。

- 备份经 **backup_role（只读、BYPASSRLS 用于读全量）** 做 `pg_dump -Fc`，明文经管道直接进入 openssl，磁盘只留密文。
- 恢复用 owner(migrator) 角色在临时库 `langgraph_restore_test` 重放对象。
- 异机副本（REMOTE_BACKUP_DIR）指向 **`data/prod-backups`**（**prod 独立目录**，非 preview-backups）。

### 4.1 加密备份证据
| item | 值 |
|---|---|
| backup 文件 | `deploy/backups/langgraph-20260904142219.dump.enc`（55648 bytes） |
| cipher | aes-256-cbc + PBKDF2 + salt |
| SHA-256 | `1a49db19...cbd3030`（配 `.sha256`，已校验） |
| **cipher_non_plain** | **1**（密文头部为 `Salted__`，非明文 `PGDMP`）→ 真实加密 |
| decrypt_ok | 1（解密后的明文头部为 `PGDMP`，pg_restore --list 解析出 143 TOC entries） |
| offsite_copied | 1（已 cp 到 `data/prod-backups/`） |
| backup 位置 | `data/prod-backups`（**prod 独立目录**，非 preview-backups）；`deploy/backups/` 为本地归档 |

> 注：`backup_encrypted.sh` 的快速 `ok_enc` 检查返回 0，是脚本内 `openssl ... | head -c5 | grep` 管道
> 缓冲导致的**误报**；我用 `verify_decrypt.sh` 独立复核：`DECRYPT_OK` + 明文头 `PGDMP` + `pg_restore --list` 152 行可解析。
> 即**解密真实成功**，加密并非失败（见 §7 说明：这是脚本校验的一个已知小瑕疵，已标注）。

### 4.2 恢复校验 + RPO/RTO 实测（evidence/drill_pg_encrypted_restore.json / DR_encrypted_restore.md）
| E# | 演练项 | 结果 | 数值/证据 |
|---|---|---|---|
| E1 | 加密归档（aes-256-cbc+PBKDF2） | PASS | backup_bytes=55648 |
| E2 | SHA-256 校验和 | PASS | 校验通过 |
| E3 | 归档完整性（pg_restore --list） | PASS | 可解析 |
| E4 | 解密恢复 | PASS | **RTO=0.643223 s** |
| E5 | 快照点后变更排除（marker） | PASS | marker_in_restore=**0**（已排除） |
| E6 | 采样数据保留（TENANT-A/B） | PASS | A=1, B=1 |
| 附带 | RLS policy 数保留 | 15 | 恢复到临时库后 `pg_policies` 计数=15 |

### 4.3 RPO / RTO 目标（实测 vs 周期界定）
| 指标 | 实测/口径 | 目标 | 达标 |
|---|---|---|---|
| **RPO** | 43.780315 s（实测数据丢失窗口）；**上界（周期最坏情况）= 900 s** | ≤900 s | **是** |
| **RTO** | 0.643223 s（恢复耗时实测） | ≤3600 s | **是** |

> **边界（不可夸大，必须注明）**：本方案 RPO 口径是 **pg_dump 周期备份**（每 15 分钟边界 ≤900s），
> **不是 PITR / WAL 连续归档恢复**。因此 RPO 上界由 `BACKUP_INTERVAL_SECONDS`（缺省 900s）界定，
> 而非 WAL 重放到某一秒；如果生产要求 PITR/秒级 RPO，需另行提供 WAL 归档方案。**本演练证明的是
> 「周期备份+加密+恢复可还原到备份点」，RPO 表示为周期上界，不含 PITR。**（`rpo_basis=measured_age_vs_interval_bound`）

**结论：备份恢复演练 §4 PASS（RPO≤900s 周期界定、RTO≤3600s 达标、密文非明文、解密 OK、SHA OK、异机副本落 prod 独立目录）。**

---

## 5. 可对账（补偿/对账分支留痕 + 对账入口可达 + mismatch 转人工）

**真实证据**：`evidence/reconcile_evidence.json`（一次性独立 PG 上实际驱动 `ExecutionEngine.reconcile`）

| item | 值 |
|---|---|
| reconcile 入口可达 | true（hasattr(engine,'reconcile') 且 scanned≥1） |
| TENANT-A 未知外部状态（provider 返回 unknown） | scanned=3, mis_matched=**3**, terminal_status=**mismatched**, operation_status=**human_handoff** |
| TENANT-B 无 provider（live 未配置，fail-closed） | scanned=2, mis_matched=**2** |
| 审计留痕 | `execution.reconcile.mismatch` ×3；`execution.compensated` ×1 |
| mismatch→转人工路径 | true（mis_matched≥1 + audit `execution.reconcile.mismatch` 存在 + 操作 human_handoff） |

代码依据：`src/execution/engine.py` 的 `reconcile()/`_reconcile_one/_reconcile_mismatch`
（`execution.reconcile.confirmed`、`execution.reconcile.mismatch`、`execution.compensated`、
`execution.compensation_failed` 审计 action）；对账入口为后台任务 `src/tasks/execution.py::reconcile_tenant`
（Celery，tenant 作用域 + `require_tenant_active` 复核）。

**结论：可对账 §5 PASS（对账入口可达、外部未知/无 provider 均 fail-closed 转人工、审计留痕、补偿分支留痕）。**

---

## 6. 补充真实测试汇总（pytest，不触真实资金）

| 测试 | 结果 |
|---|---|
| test_sandbox_concurrency_guard.py / test_fault_injection.py / test_callback_concurrency_cas.py / test_concurrency_stress.py / test_recovery_consistency.py / test_callback_security_log.py | **34 passed, 1 skipped**（skipped=PG-marked 需 reset schema，本机注解见 §7） |
| test_sandbox_e2e_flow.py / test_sandbox_http_provider.py / test_execution_engine.py | **53 passed** |
| test_pg_callback_concurrency.py（真实 PG + RLS，经 pg_patch_conftest 修正 FK 后运行） | **7 passed** |
| test_pg_rls.py / test_pg_store.py（真实 PG） | passed（部分） |

> 说明：`test_pg_hardening.py` 有 2 处 **FAIL，属既有测试代码与 API 签名漂移**（`append_audit()` 缺少 `now` 参数），与并发/恢复/备份无关；非本次范围，已标注供队长/发布经理知悉。

---

## 7. 如实标注的不足 / 边界（不隐藏）

1. **生产栈未部署**：本次以 preview + 一次性独立库取证；RPO/RTO/并发数据面为真实 PostgreSQL，但**对象不是真正的 after-sales-prod 独立栈**。prod 栈的恢复演练需在 after-sales-prod 部署后再做一次。
2. **仓库迁移 `shipping_events` FK 缺陷（生产阻断, NO-GO）**：干净库初始化必失败；需修复为复合 FK 后才能真正部署 prod / 恢复 prod 新库。我在一次性库中**仅在演练时修正**，**未改仓库源**。
3. **迁移与 preview schema 漂移**：preview 无 shipping_events/orders 等新表；生产部署前需先评估并统一迁移版本。
4. **RPO 为 pg_dump 周期界定而非 PITR/WAL**：RPO 上界 900s 来自备份周期，非秒级 PITR；如需 PITR 需另配 WAL 归档。
5. **DR 密钥为一次性测试密钥**，非生产密钥；且 `backup_encrypted.sh` 的快速 `ok_enc` 检查有 grep 管道缓冲误报（已用 `verify_decrypt.sh` 独立复核解密成功）。生产密钥应走受控 vault 且不落地。
6. `test_pg_hardening.py` 2 处 FAIL 为既有测试/API 签名漂移，非本次范围。
7. `.backup_key`（一次性测试密钥）保留在 evidence 目录用于复核，生产部署**删除**且绝不复用。`data/prod-backups` 内有一个**遗留 0 字节** `langgraph-20260904061843.dump.enc`（历史/其它成员的产物），本次真实备份为 55648 字节的 `langgraph-20260904142219.dump.enc`。

---

## 8. 结论汇总

| 项 | 结果 | 关键证据 |
|---|---|---|
| 1 回调并发（N=256，幂等=1/终态封闭/跨租户不覆盖） | **PASS** | concurrency_stress.json / concurrency_stress_pg.json |
| 2 沙箱并发（真 HTTP，FAIL 路径收敛） | **PASS** | sandbox_concurrency_stress.json |
| 3 恢复一致性（恢复后 C1–C5） | **PASS** | recovery_consistency.json |
| 4 备份恢复演练（加密+校验+RPO/RTO） | **PASS（RPO≤900s 周期界定、RTO≤3600s）** | drill_pg_encrypted_restore.json / DR_encrypted_restore.md |
| 5 可对账（入口可达 + mismatch 转人工 + 留痕） | **PASS** | reconcile_evidence.json |
| 6 真实 PG 回调/CAS/跨租户伪造安全 | **PASS** | test_pg_callback_concurrency.py (7 passed) |

**Go/No-Go 建议**：测试与演练项目（1–6）本身证据充分、均 PASS；**但存在一项生产阻断级迁移缺陷（shipping_events FK）+ preview 与迁移 schema 漂移**，在修复并完成「after-sales-prod 独立栈上的恢复演练」之前，**不应放行生产上线（NO-GO for prod migration/fresh-DB）**。安全红线与幂等/RLS/无重复执行在真实 PG 上均验证通过。

---

### 签名
- 执行：test-runner（测试/演练员）
- 时间：2026-09-04
- 证据目录：`evidence/prod-go-live/test-runner/`
