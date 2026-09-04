# 网关沙箱端到端验收报告（G6 网关沙箱）

> 验证人：gw-verify（test-runner） ｜ 日期：2026-09-04 ｜ 执行模式：shadow（不触真实资金）
> 目标：内网资金网关沙箱（自部署 `sandbox_gateway`，SQLite 持久化幂等，`X-API-Key=gw-key`）端到端、幂等、并发与补偿路径验收。

## 0. 结论（TL;DR）

**通过。** 网关沙箱的三个关键不变量全部成立：

| 验收项 | 结果 | 关键证据 |
|---|---|---|
| submit 服务端幂等（同租户同键只落一行） | ✅ PASS | 重复 POST 返回同一 `external_txn_id`；`count_txns=1` |
| query 按 external_txn_id 查询状态 | ✅ PASS | `txn-...` → `succeeded`（对账可用）；未知 → `not_found` |
| compensate 稳定 reversal_id（重放一致） | ✅ PASS | 重复 POST 返回同一 `rev-...` |
| 跨租户同 idempotency_key 互不覆盖 | ✅ PASS | 各租户 `count_txns=1`，`external_txn_id` 不同 |
| N=256 并发：provider submit 恰好 1 次 | ✅ PASS | `provider_submit_calls=1`、`distinct_external_txn_id=1` |
| N=256 并发：失败收敛单一终态（compensated） | ✅ PASS | `distinct_reversal_id=1`、`provider_submit_calls=1`、`errors={}` |
| N=256 跨租户并发：each 租户各 1 条 | ✅ PASS | `cross_tenant_no_overwrite=true` |

## 1. 被测对象

- **服务**：`src/execution/sandbox_gateway.py` 的 `create_sandbox_gateway_app`，以独立 uvicorn 进程运行于 `http://127.0.0.1:8010`（自托管、内网、SQLite 持久化）。
- **数据面**：`data/sandbox_gateway.db`（WAL），`gateway_txns` / `gateway_reversals` 两表；业务唯一键：
  - `gateway_txns`：`PRIMARY KEY (tenant_id, idempotency_key)`；
  - `gateway_reversals`：`PRIMARY KEY (tenant_id, idempotency_key, execution_id)`。
- **鉴权**：`X-API-Key`（测试值 `gw-key`）；不匹配返回 401。
- **回调/HTTP 细节**：PowerShell `Invoke-WebRequest` 必须带 `-ContentType application/json`，否则 422（FastAPI 需要 JSON 类型载荷）。

## 2. 端到端 HTTP 验证（真实运行中的 8010 实例）

验证脚本 `_verify_tmp/verify_gateway_e2e.py`（httpx 客户端），结果落盘 `evidence/gateway_sandbox_e2e_verify.json`。

### 2.1 submit 幂等（服务端只落一行）

同 `(tenant_id=TENANT-E2E, idempotency_key=idem-e2e-<ts>)` 连续两次 POST `/api/sandbox/submit`：

- 第一次 `external_txn_id` = `txn-ccd7896ce617458e`
- 第二次 `external_txn_id` = `txn-ccd7896ce617458e`（**完全相同**）
- `store.count_txns(tenant, idem)` = **1**（服务端仅落一行）
- `same_external_txn_id=true`、`single_row=true`、`idempotent_ok=true`
- 状态 `succeeded`

> 机制：`INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING` + 事务内读回既有行，即便并发重复提交也只落一行并返回同一结果。

### 2.2 query 对账查询

- `GET /api/sandbox/query?tenant_id=TENANT-E2E&external_txn_id=txn-ccd7896ce617458e` → `status=succeeded`、`amount=88.5`、`exists=true`。
- 未知 `external_txn_id` → `status=not_found`（不泄露、不抛错）。

### 2.3 compensate 稳定 reversal_id

同 `(tenant_id, execution_id=exe-<ts>-abc, idempotency_key)` 连续两次 POST `/api/sandbox/compensate`：

- 第一次 `reversal_id` = `rev-4de35b2cdbf05f5a`
- 第二次 `reversal_id` = `rev-4de35b2cdbf05f5a`（**完全相同**）
- `stable_reversal_ok=true`

> 机制：`reversal_id = "rev-" + uuid5(NAMESPACE_URL, sandbox-gateway://{tenant}:{idem}:{execution})`，确定性派生，重放一致。

### 2.4 跨租户同 idempotency_key 不覆盖

- 同 `idempotency_key` 分别以 `TENANT-E2E` 与 `TENANT-E2E-B` 提交。
- `tenant_A_rows=1`、`tenant_B_rows=1`、`tenant_B_external_txn_id=txn-2e017285deeb4f84` ≠ `txn-ccd7896ce617458e`。
- `cross_tenant_no_overwrite=true`（唯一键含 `tenant_id`）。

## 3. N=256 并发压力测试

命令：

```
.venv\Scripts\python.exe scripts/sandbox_concurrency_stress.py --backend sqlite --concurrency 256 --out evidence/sandbox_concurrency_stress.json
```

结果：`evidence/sandbox_concurrency_stress.json`（`conclusion=true`）。

### 3.1 I1：同 operation 高并发 execute（真实沙箱 HTTP）

- `concurrency=256`、`elapsed_seconds=0.3064`
- `provider_submit_calls=1`（**恰好 1 次**）
- `distinct_external_txn_id=1`、`execution_records_for_op=1`、`distinct_execution_id=1`
- `errors={}`（无 `submitted->submitted` 跃迁抛错）
- 终态 `submitted`
- **`I1_ok=true`**

### 3.2 FAIL：显式失败（default_status=failed 走真实 HTTP）

- `provider_submit_calls=1`、`provider_compensate_calls=1`
- `distinct_reversal_id=1`（**单一 reversal_id**）
- `execution_records_for_op=1`、`distinct_execution_id=1`、`errors={}`
- 终态 `compensated`（单终态收敛）
- **`FAIL_ok=true`**

### 3.3 I2：跨租户同 idempotency_key 并发

- `concurrency_per_tenant`（每租户 256）
- `provider_a_submit_calls=1`、`provider_b_submit_calls=1`
- `op_a_execution_records=1`、`op_b_execution_records=1`、`distinct_execution_id_a=1`、`distinct_execution_id_b=1`
- `errors={}`
- **`cross_tenant_no_overwrite=true`**

## 4. 结论与合规备注

- 端到端通过：同租户重复提交返回同一 `external_txn_id` 且服务端只落一行（`count_txns=1`）。
- 幂等通过：`compensate` 返回稳定 `reversal_id`（重放一致）。
- 跨租户通过：同 `idempotency_key` 各租户各 1 条，互不覆盖。
- N=256 并发通过：高并发下 `submit` 恰好 1 次、`errors={}`、失败路径收敛单一终态。
- **未触真实资金**：全程使用自部署沙箱网关 + `ExecutionMode` 受控路径。
- 诚实标注：本报告验收的是**引擎层调用已修复的 `claim_execution_submit` 单执行守卫 + 真实网关（进程外 HTTP）** 的组合，证明真实沙箱不发生重复执行；SQLite 并发写由单实例共享锁串行化（多实例/独立连接会因 SQLite 写串行化限制抛 `database is locked`），真实多进程行锁并发已由 PostgreSQL 的 `FOR UPDATE` + RLS 路径另行验证（见 `test_pg_callback_concurrency.py`）。

## 5. 复现命令

```powershell
.\.venv\Scripts\python.exe _verify_tmp\verify_gateway_e2e.py
.\.venv\Scripts\python.exe scripts/sandbox_concurrency_stress.py --backend sqlite --concurrency 256 --out evidence/sandbox_concurrency_stress.json
```
