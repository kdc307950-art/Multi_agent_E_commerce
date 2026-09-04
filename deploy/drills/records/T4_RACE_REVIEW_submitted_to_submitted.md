# t4 竞态复核记录：`submitted->submitted`

- 触发：funds-engineer（t1 交付报告）在 `test_concurrency_stress.py` 中观察到并发下
  `submitted->submitted` 线程竞态告警，要求 t4 在收尾前核实并处理（关联"真实沙箱不发生重复执行"红线）。
- 复核人：test-engineer
- 时间：2026-09-04

## 判定：**引擎并发缺陷**（非测试误报 / 非断言过宽）

依据：
1. **复现**：`scripts/concurrency_stress.py`（CountingProvider 统计 submit 次数）。修复前 N=128 时
   `provider.submit` 被并发调用 **20 次**（对外部重复提交），并出现 1 次 `DomainError:
   执行状态跃迁非法: submitted -> submitted`。
2. **机制**：`ExecutionEngine.execute` 在 live 模式的幂等检查**非原子**——多个并发线程都越过
   `get_execution_by_operation()==None` 检查 → 各自 `create_execution_record`（幂等返回同一记录）→
   **全部**进入 `_run_live` → 全部 `provider.submit`；输线程随后 `_transition(record, SUBMITTED)`
   读到已被推进的 SUBMITTED 状态，`can_transition(SUBMITTED, SUBMITTED)=False` → 抛
   `submitted->submitted`。
   （机制与 `can_transition` 的 `if current == next_status: return False` 一致，非误报。）
3. **红线关联**：宪法 §第四 4"并发安全 / 同一资金操作永不重复执行"。此前"不重复执行"实际**仅由
   provider 按 idempotency_key 幂等**兜底，引擎自身未保证单次提交；对后续接真实（未必严格幂等）
   网关构成重复扣款/重复补偿隐患。

## 修复（diff 概述）

1. `src/infrastructure/store.py`（MemoryStore）、`sqlite_store.py`、`postgres_store.py`：新增
   `claim_execution_submit(tenant_id, execution_id, *, now)` —— 单执行守卫。以 `attempts` 0→1 作为
   "本次 live 提交外部"的单次认领标记：
   - 仅对 `status='pending_submit'` 的非终态记录认领；终态记录返回 False（**保留 terminal_locked**）；
   - 成功认领 → True（本线程为提交者）；已被认领/已推进 → False。
   - 原子性：Memory 用 `_decision_lock`；SQLite 用单连接锁内 `UPDATE ... WHERE status='pending_submit'
     AND attempts=0`（rowcount==1）；PG 用同一事务内 `FOR UPDATE` + RLS + CAS `WHERE status='pending_submit'
     AND attempts=0`（与 `apply_callback_atomic` 同模式）。
2. `src/execution/engine.py::_run_live`：提交前先调用 `claim_execution_submit`；非提交者读取最新
   记录后 `_replay_outcome` 幂等重放，**绝不重复调用 provider.submit、绝不进入 submitted->submitted 跃迁**。

## 修复后实测（SQLite）
- N=128：`provider_submit_calls=1`、`errors={}`、执行记录=1、distinct external_txn_id=1、distinct execution_id=1。
- N=256：同上（`provider_submit_calls=1`、`errors={}`）。I1/I2/I3（租户级幂等 + 终态封闭）不变。

## 测试调整
- `tests/test_concurrency_stress.py::test_same_tenant_same_op_concurrent_single_execution`：断言**从严**，
  改为 `assert provider.submit == 1` 且 `assert errors == []`（此前允许 submitted->submitted 例外）。
- `tests/test_fault_injection.py` 新增回归：
  - `test_single_flight_no_duplicate_submit_or_transition_race`：同 execution 高并发 execute → submit==1、
    external_txn_id 唯一、execution_id 唯一、无任何状态跃迁异常。
  - `test_single_flight_failure_no_duplicate_compensation`：失败路径下高并发 execute → 收敛到单一终态
    （COMPENSATED/COMPENSATION_FAILED）、单一 reversal_id，无重复补偿。
- 全量回归：385 passed / 34 skipped（未破坏既有用例）。

## 结论
- 属**引擎并发缺陷**（非测试问题），已修复：执行引擎现在自身保证"同一操作并发只提交一次"，
  不再依赖 provider 幂等兜底；`submitted->submitted` 竞态已消除。
- 已向 captain 说明需要真实沙箱并发压测由 funds-engineer 的沙箱工具做真实网关验证；
  本修复使用 mock/sandbox provider，验证的是引擎状态机/单执行守卫的正确性。
- 对 t6 验收：**不应**把 `submitted->submitted` 判为缺陷（已修复）；"真实沙箱不发生重复执行"
  现在由引擎单执行守卫 + 租户级幂等键 + 终态封闭共同保证。
