# 幂等运行态验证（deploy-engineer · 阶段三）

> 位置：运行态（**真实 Docker 沙箱网关 `http://sandbox-gateway:8010`**，SQLite 服务端持久化幂等）。
> 脚本：`_verify_runtime/run_sandbox_runtime_verify.py`（容器内验证，输出见 `runtime/runtime_verify_report.json`）。
> 结论：**全部 `PASS`（运行态实测）**。执行层同一 `tenant_id + operation_id/idempotency_key` 绝不重复执行、绝不跨租户覆盖。

---

## 判定口径（均由真实 HTTP 调用沙箱网关 + 服务端单行复核）

| # | 断言 | 期望 | 实测 |
|---|---|---|---|
| 1 | 同租户同 `idempotency_key` 重复 submit | 同一 external_txn_id | ✅ `same_tenant_same_key_external_txn_same: true` → `txn-b6f6f7ed4f7c4ae8` |
| 2 | 重复提交返回同状态 | 均 `succeeded` | ✅ `status_both_succeeded: true` |
| 3 | 回执金额 = 订单实付（299.00，非请求方任意值） | `receipt.amount == 299.0` | ✅ `amount_is_order_paid_299: true` |
| 4 | **跨租户同 `idempotency_key`** 生成不同外部队列号 | 不同 txn_id | ✅ `cross_tenant_same_key_external_txn_differ: true` |
| 5 | 跨租户 query 各自隔离（金额独立） | A=10 / B=20 | ✅ `cross_tenant_query_amount_a_10 / _b_20: true` |
| 6 | 跨租户查不到对方外部队列号 | `not_found` | ✅ `cross_tenant_query_not_found: true` |
| 7 | **并发 16 线程同键** submit | 全部同一 external_txn_id | ✅ `concurrent_16_same_key_unique_txn: true` |
| 8 | **服务端只落一行**（同租户同键） | `COUNT==1` | ✅ `server_side_single_row: true` |
| 9 | 服务端跨租户各落一行 | A=1 / B=1 | ✅ `server_side_single_row_cross_tenant_A/B: true` |

---

## 服务端 SQLite 唯一约束强证据（host 卷读取，`data/verify-sandbox/sandbox_gateway.db`）

```
gateway_txns total = 4
  ('TENANT-A', 'rt-key-aaa', 'txn-b6f6f7ed4f7c4ae8', 'succeeded', 299.0)
  ('TENANT-A', 'rt-key-reconcile', 'txn-9a2ae75bc20e4341', 'succeeded', 299.0)
  ('TENANT-A', 'rt-key-shared', 'txn-4e8892e9db834e82', 'succeeded', 10.0)
  ('TENANT-B', 'rt-key-shared', 'txn-bd5ce2c4a2e24cbf', 'succeeded', 20.0)
A/rt-key-aaa count = 1   # 同租户同键只落一行（绝不重复扣款/退款）
A/rt-key-shared count = 1
B/rt-key-shared count = 1
```

要点：`(tenant_id, idempotency_key)` 为唯一键（`PRIMARY KEY (tenant_id, idempotency_key)`），`INSERT ... ON CONFLICT DO NOTHING` + 事务内先查后插实现原子幂等。跨租户同 `idempotency_key`（`rt-key-shared`）在 A/B 各落一行且金额不同（10 / 20）→ **互不覆盖**。

---

## 对账级幂等（同 operation 不重复提交外部）

运行态之外，`tests/test_sandbox_e2e_flow.py::test_adapter_execute_idempotent_reexecute_no_resubmit` 与 `tests/test_fault_injection.py::test_single_flight_no_duplicate_submit_or_transition_race` 通过 `CountingProvider` 断言：审批后重复执行同一 operation → 幂等重放（`provider.submit` 只调用 1 次、执行记录仅 1 条、execution_id 不变）。这些属于**引擎层并发守卫 + 幂等**，已由既有测试覆盖（`390 passed` 基线），我在运行态以真实网关复核了第 8/9 项（服务端单行）作为强背书。

---

## 状态：✅ 运行态实测（网关服务端单行）+ ✅ 引擎层并发守卫（既有测试，非本任务伪造）
