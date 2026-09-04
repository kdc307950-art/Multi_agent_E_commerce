"""多租户并发压测（pytest 版）—— 不触真实资金，仅 mock/sandbox provider。

对应 t4 子项 1，验证并发不变式（不管调度顺序都应成立）：
- I1 同租户同 idempotency_key 高并发 execute → **只产生一次资金执行**：同一 operation 只有
  1 条 execution 记录、1 个 distinct external_txn_id、1 个 distinct execution_id（幂等不重复）；
- I2 不同租户同 idempotency_key 并发 → 各租户**各自 1 条**执行记录，互不覆盖（租户级幂等）；
- I3 同 thread/同 nonce 回调并发 → `apply_callback_atomic` CAS 只允许一个 confirmed，其余
  terminal_locked（终态封闭，绝不重复扣款/退款）。

真实性与界限（诚实标注）：本文件用 SQLite 后端（单连接 + RLock 写串行化）获得本机可复现的
确定性结果，验证"幂等锚点 + 租户级幂等 + 终态封闭"这些**不依赖调度顺序**的安全不变式；
SQLite 的写串行化不代表 PostgreSQL 行锁并发的排序特性（后者见 `test_pg_callback_concurrency.py`）。
`provider.submit` 调用次数可能 >1（引擎 execute 幂等检查非原子），但按 idempotency_key 幂等
返回同一 external_txn_id → 外部只见到一个资金执行（这是设计允许/依赖 provider 幂等的边界，
见脚本模块 docstring I4，如实记录）。
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from src.core.types import PendingAction, Role
from src.execution import ExecutionEngine, ExecutionMode, ExecutionStatus, MockFundsProvider
from src.infrastructure.sqlite_store import SqliteStore
from src.execution.verification import build_callback_signature

SECRET = "stress-secret"


def _seed(store: SqliteStore) -> None:
    for t in ("TENANT-A", "TENANT-B"):
        store.create_tenant(t, f"租户-{t}")
        store.add_membership(t, f"USER-{t}", Role.CUSTOMER)
        store.create_session(t, f"USER-{t}", f"th-{t}", time.time(), 7)


class CountingProvider:
    def __init__(self, result: str = "success") -> None:
        self.submits = 0
        self.txns: list[str] = []
        self._inner = MockFundsProvider(result=result)

    def submit(self, **kw):
        self.submits += 1
        r = self._inner.submit(**kw)
        self.txns.append(r["external_txn_id"])
        return r

    def query(self, **kw):
        return self._inner.query(**kw)

    def compensate(self, **kw):
        return self._inner.compensate(**kw)


# ---------------------------------------------------------------------------
# I1: 同租户同操作并发 → 一次资金执行（幂等不重复）
# ---------------------------------------------------------------------------
def test_same_tenant_same_op_concurrent_single_execution(tmp_path):
    store = SqliteStore(str(tmp_path / "i1.db"))
    _seed(store)
    provider = CountingProvider("success")
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE, provider=provider,
                             callback_secret=SECRET)
    op = store.create_operation("TENANT-A", "th-A", "ORD-001", PendingAction.REFUND,
                                "opkey:i1", time.time())
    order = SimpleNamespace(total_amount=100.0)
    n = 32
    barrier = threading.Barrier(n)
    exids: list[str] = []
    errors: list[str] = []

    def worker():
        barrier.wait()
        try:
            out = engine.execute(op, order)
            exids.append(out.execution_id)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    # 不变式：同一操作只有一条执行记录、单一 execution_id、单一 external_txn_id。
    rec = store.get_execution_by_operation("TENANT-A", op.operation_id)
    assert rec is not None
    assert len(set(exids)) == 1
    assert rec.idempotency_key == "opkey:i1"
    assert rec.tenant_id == "TENANT-A"
    # 单执行守卫（t4 修复）：并发 execute 只应有一线程成为提交者，`provider.submit` 恰好一次
    # （不再依赖"provider 幂等"兜底），且**绝无** submitted->submitted 状态跃迁竞态异常。
    assert provider.submits == 1, f"provider.submit 被并发调用 {provider.submits} 次（应=1）"
    assert errors == [], f"并发 execute 出现 {len(errors)} 个异常: {errors}"
    assert store.get_operation("TENANT-A", op.operation_id).status.value == "executed"
    store.close()


# ---------------------------------------------------------------------------
# I2: 不同租户同 idempotency_key 并发 → 互不覆盖（租户级幂等）
# ---------------------------------------------------------------------------
def test_cross_tenant_same_idempotency_key_no_overwrite(tmp_path):
    store = SqliteStore(str(tmp_path / "i2.db"))
    _seed(store)
    engine_a = ExecutionEngine(store, mode=ExecutionMode.LIVE,
                               provider=CountingProvider("success"), callback_secret=SECRET)
    engine_b = ExecutionEngine(store, mode=ExecutionMode.LIVE,
                               provider=CountingProvider("success"), callback_secret=SECRET)
    # 两租户用"同一个 idempotency_key 字符串"。
    op_a = store.create_operation("TENANT-A", "th-A", "ORD-001", PendingAction.REFUND,
                                  "opkey:i2-cross", time.time())
    op_b = store.create_operation("TENANT-B", "th-B", "ORD-001", PendingAction.REFUND,
                                  "opkey:i2-cross", time.time())
    order = SimpleNamespace(total_amount=100.0)
    n = 16
    barrier = threading.Barrier(2 * n)
    exids_a: list[str] = []
    exids_b: list[str] = []
    errors: list[str] = []

    def worker_a():
        barrier.wait()
        try:
            exids_a.append(engine_a.execute(op_a, order).execution_id)
        except Exception as e:  # noqa: BLE001
            # 并发下偶发非资金异常（submitted->submitted），不改变幂等锚点；如实记录不掩盖。
            errors.append(str(e))

    def worker_b():
        barrier.wait()
        try:
            exids_b.append(engine_b.execute(op_b, order).execution_id)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    ts = ([threading.Thread(target=worker_a) for _ in range(n)]
          + [threading.Thread(target=worker_b) for _ in range(n)])
    [t.start() for t in ts]
    [t.join() for t in ts]

    rec_a = store.get_execution_by_operation("TENANT-A", op_a.operation_id)
    rec_b = store.get_execution_by_operation("TENANT-B", op_b.operation_id)
    assert rec_a is not None and rec_b is not None
    # 各租户各自 1 条，互不覆盖、execution_id 不同、租户作用域正确。
    assert len(set(exids_a)) == 1
    assert len(set(exids_b)) == 1
    assert rec_a.tenant_id == "TENANT-A" and rec_b.tenant_id == "TENANT-B"
    assert rec_a.execution_id != rec_b.execution_id
    # 单执行守卫（t4 修复）：并发 execute 不再产生 submitted->submitted 等状态跃迁异常。
    assert errors == [], f"并发 execute 出现 {len(errors)} 个异常: {errors}"
    store.close()


# ---------------------------------------------------------------------------
# I3: 同 thread/同 nonce 回调并发 → CAS 只一个 confirmed，其余 terminal_locked/replay
# ---------------------------------------------------------------------------
def test_same_nonce_concurrent_callback_single_terminal(tmp_path):
    store = SqliteStore(str(tmp_path / "i3.db"))
    _seed(store)
    op = store.create_operation("TENANT-A", "th-A", "ORD-001", PendingAction.REFUND,
                                "opkey:i3", time.time())
    rec = store.create_execution_record(
        "TENANT-A", operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-001", idempotency_key=op.idempotency_key,
        mode=ExecutionMode.LIVE, amount=100.0, now=time.time())
    rec = store.update_execution_record("TENANT-A", rec.execution_id,
                                        status=ExecutionStatus.SUBMITTED,
                                        external_txn_id="txn-i3", submitted_at=time.time())
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE,
                             provider=MockFundsProvider(result="success"), callback_secret=SECRET)
    payload = {"tenant_id": "TENANT-A", "execution_id": rec.execution_id,
               "external_txn_id": rec.external_txn_id, "status": "succeeded",
               "amount": rec.amount, "nonce": "nonce-SAME"}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sig = build_callback_signature(SECRET, payload)
    n = 32
    barrier = threading.Barrier(n)
    results: list[tuple] = []

    def worker():
        barrier.wait()
        r = engine.apply_callback(body, sig)
        results.append((r.applied, r.reason))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    applied = [r for r in results if r[0] is True]
    replays = [r for r in results if r[0] is False and r[1] == "replay"]
    # 恰一次生效，其余全部判为重放（只重放不重复生效）。
    assert len(applied) == 1
    assert len(replays) == n - 1
    final = store.get_execution_record("TENANT-A", rec.execution_id)
    assert final.status is ExecutionStatus.CONFIRMED
    # 幂等锚点不变：仅一条执行记录。
    assert len(store.list_execution_records("TENANT-A")) == 1
    store.close()


# ---------------------------------------------------------------------------
# I3b: 异 nonce 并发回调 → 终态封闭，收敛到单一终态（不重复扣款）
# ---------------------------------------------------------------------------
def test_diff_nonce_concurrent_callback_converges_single_terminal(tmp_path):
    store = SqliteStore(str(tmp_path / "i3b.db"))
    _seed(store)
    op = store.create_operation("TENANT-A", "th-A", "ORD-001", PendingAction.REFUND,
                                "opkey:i3b", time.time())
    rec = store.create_execution_record(
        "TENANT-A", operation_id=op.operation_id, pending_action=PendingAction.REFUND,
        order_id="ORD-001", idempotency_key=op.idempotency_key,
        mode=ExecutionMode.LIVE, amount=100.0, now=time.time())
    rec = store.update_execution_record("TENANT-A", rec.execution_id,
                                        status=ExecutionStatus.SUBMITTED,
                                        external_txn_id="txn-i3b", submitted_at=time.time())
    engine = ExecutionEngine(store, mode=ExecutionMode.LIVE,
                             provider=MockFundsProvider(result="success"), callback_secret=SECRET)
    n = 16
    barrier = threading.Barrier(n)
    results: list[tuple] = []

    def worker(i):
        payload = {"tenant_id": "TENANT-A", "execution_id": rec.execution_id,
                   "external_txn_id": rec.external_txn_id, "status": "succeeded",
                   "amount": rec.amount, "nonce": f"nonce-{i}"}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sig = build_callback_signature(SECRET, payload)
        barrier.wait()
        r = engine.apply_callback(body, sig)
        results.append((r.applied, r.reason))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    confirmed = [r for r in results if r[0] is True and r[1] == "confirmed"]
    locked = [r for r in results if r[0] is False and r[1] == "terminal_locked"]
    # 终态封闭：只允许一个 confirmed，其余 terminal_locked（绝不重复扣款）。
    assert len(confirmed) == 1
    assert len(confirmed) + len(locked) == n
    final = store.get_execution_record("TENANT-A", rec.execution_id)
    assert final.status is ExecutionStatus.CONFIRMED
    assert final.receipt["amount"] == 100.0
    assert len(store.list_execution_records("TENANT-A")) == 1
    store.close()
